"""Plain-text intake through the existing archive, editor and operation journal."""
from contextlib import nullcontext
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import time

from . import outcomes, quota, stopping
from .daemon import execute, validate_config
from .errors import HeadRefusal, KelmoError, Stopping, sqlite_busy
from .journal import now as journal_now
from .executor import ModelOutcome, normalize_answer
from .wiki import engine, pages


class CommandExecutor:
    """An installed executor adapter selected by the vault owner."""

    def __init__(self, argv, timeout=120, should_stop=None):
        self.argv, self.timeout, self.should_stop = argv, timeout, should_stop

    def execute(self, job):
        if self.should_stop is not None and self.should_stop():
            raise Stopping()
        try:
            payload = dict(job.payload, task=job.task)
            if job.conversation is not None:
                # `kelmo executor` takes it back out of the payload before the model sees anything.
                payload['conversation'] = job.conversation
            answer = quota.checked(execute(self.argv, payload, timeout=self.timeout))
            return normalize_answer(answer)
        except quota.QuotaWait as wait:
            return ModelOutcome('deferred', reset_at=wait.reset_at)
        except KelmoError as error:
            return ModelOutcome('failure', error_code=error.code, reason=str(error))


def configured_executor(service):
    path = Path(service.config.sessions.get('executor_config') or
                service.config.internal('.state/executor.json'))
    if not path.exists():
        return None
    try:
        config = validate_config(json.loads(path.read_text(encoding='utf-8')))
    except (OSError, ValueError, UnicodeError) as error:
        raise KelmoError('E_CONFIG', 'The configured executor file is not readable JSON.') from error
    return CommandExecutor(config['argv'], config['timeout_seconds']) if config['argv'] else None


WORK_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{2,80}')


_FRONTMATTER_PRIORITY = re.compile(r'\A---\n(?:.*\n)*?priority:[ \t]*["\']?(H[123])["\']?[ \t]*\n(?:.*\n)*?---(?:\n|\Z)')


def requested_work(args, content=None):
    """The `--new-work SLUG --title … [--summary …] [--priority …]` arguments as one request field, or None.

    Without `--priority`, a `priority: H1|H2|H3` line in the note's leading frontmatter sets the page
    priority, so a note written for the editor carries it once.
    """
    priority = getattr(args, 'priority', None)
    if args.new_work is None:
        if args.title is not None or args.summary is not None or priority is not None:
            raise KelmoError('E_INPUT', '--title, --summary and --priority describe a page opened with --new-work.')
        return None
    if args.title is None:
        raise KelmoError('E_INPUT', '--new-work needs --title.')
    work = {'slug': args.new_work, 'title': args.title}
    if args.summary is not None:
        work['summary'] = args.summary
    if priority is None and isinstance(content, str):
        match = _FRONTMATTER_PRIORITY.match(content)
        priority = match.group(1) if match else None
    if priority is not None:
        work['priority'] = priority
    return work


def slug_of(text):
    """A work page slug derived from free text (a title, a filename stem): lowercase words joined by
    hyphens, at least three characters, or None when nothing usable is left."""
    words = re.findall(r'[a-z0-9]+', str(text).lower())
    slug = '-'.join(words)[:81].rstrip('-')
    return slug if WORK_SLUG.fullmatch(slug) else None


def _taken(service, registry, target, page_id):
    return (target in registry['pages'] or target in registry['redirects']
            or page_id in engine.identity_namespace(registry) or os.path.lexists(service.config.wiki / target))


def new_work_page(service, registry, project, new_work, *, allocate=False, existing=None):
    """The work page one request opens: a fresh target and identity under the project's entry page.

    A work page comes to exist through `kelmo create --new-work` or through the editor asking for
    one (`open_work`); the model never writes the entry itself. The page hangs from the project map,
    or from the project's own index page when it has no map. A registered target, a taken page ID or
    any existing file at the target is refused, so an unregistered legacy page is never claimed;
    with `allocate` (the editor's path) the slug takes a `-2`, `-3`, … suffix instead.
    """
    if (not isinstance(new_work, dict) or not {'slug', 'title'} <= new_work.keys()
            or new_work.keys() - {'slug', 'title', 'summary', 'priority'}
            or not all(isinstance(value, str) for value in new_work.values())):
        raise KelmoError('E_INPUT', 'A new work page names a slug and a title, and optionally a summary '
                                    'and a priority.')
    slug, title = new_work['slug'], new_work['title'].strip()
    summary = (new_work.get('summary') or title).strip()
    priority = new_work.get('priority', '')
    if priority not in pages.PRIORITIES:
        raise KelmoError('E_INPUT', 'A work page priority is H1, H2 or H3.')
    if not WORK_SLUG.fullmatch(slug):
        raise KelmoError('E_INPUT', 'A work page slug uses 3-81 lowercase letters, digits and hyphens.')
    for name, value, limit in (('title', title, 120), ('summary', summary, 200)):
        if not value or len(value) > limit or '\n' in value or '\r' in value:
            raise KelmoError('E_INPUT', f'A new work page {name} is one plain line of at most {limit} characters.')
    parent = pages.project_entry(registry, project)
    if parent is None:
        raise KelmoError('E_INPUT', 'No project map or index page is registered for ' + project + ' under '
                        + pages.project_directory(project) + '/, and a new work page hangs from one; select '
                        'an existing page of ' + project + ' with --target instead.')
    directory = pages.project_directory(project)
    candidates = [slug] + ([f'{slug[:77]}-{number}' for number in range(2, 100)] if allocate else [])
    for candidate in candidates:
        target, page_id = f'{directory}/{candidate}.md', f'{directory.replace("/", "-")}-{candidate}'
        opened = {'target': target, 'page_id': page_id, 'title': title, 'summary': summary, 'parent': parent}
        if priority:
            opened['priority'] = priority
        if existing is not None and all(existing.get(key) == value for key, value in opened.items()
                                        if key != 'page_id'):
            return dict(existing)
        if not _taken(service, registry, target, page_id):
            return opened
    target = f'{directory}/{slug}.md'
    if target in registry['pages'] or target in registry['redirects']:
        raise KelmoError('E_INPUT', 'Page ' + target + ' is already registered; select it with --target.')
    if f'{directory.replace("/", "-")}-{slug}' in engine.identity_namespace(registry):
        raise KelmoError('E_INPUT', 'Page ID ' + f'{directory.replace("/", "-")}-{slug}'
                        + ' already names a page, narrative or action.')
    raise KelmoError('E_INPUT', 'An unregistered file already exists at ' + target + '; a new work page '
                               'never claims it. Choose another slug.')


def resolve_target(registry, name):
    """The registered page one selection names, in any spelling the read tools hand out.

    graph_query and graph_read name a page by its wiki path without `.md`; the registry keys keep it;
    a page also answers to its page ID and its aliases. Exactly one page must match, else the
    selection is refused with the nearest registered targets as the repair.
    """
    registered = registry['pages']
    by_id = {entry.get('page_id'): target for target, entry in registered.items()}
    stripped = name.strip().lstrip('/')
    candidates = [stripped, stripped + '.md'] if not stripped.endswith('.md') else [stripped]
    for candidate in candidates:
        if candidate in registered:
            return candidate
        # A redirect names the page ID that now holds a moved target.
        moved = registry.get('redirects', {}).get(candidate)
        if moved in by_id:
            return by_id[moved]
    key = stripped[:-3] if stripped.endswith('.md') else stripped
    matches = sorted({target for target, entry in registered.items()
                      if entry.get('page_id') == key or key in (entry.get('aliases') or ())})
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise KelmoError('E_INPUT', 'Target ' + name + ' names several pages: ' + ', '.join(matches[:5])
                        + '; select one by its path.')
    import difflib
    near = difflib.get_close_matches(key + '.md', list(registered), n=3, cutoff=0.6)
    raise KelmoError('E_INPUT', 'Page is not registered: ' + name
                    + ('; nearest registered: ' + ', '.join(near) if near else '')
                    + '. Select a registered page, register an existing file with `kelmo register PAGE --apply`, '
                      'or open a new work page with --new-work.')


PENDING_MAX = 240


def pending_line(pending):
    """The one-line summary a note carries while it waits for publication, or None."""
    if pending is None:
        return None
    if not isinstance(pending, str):
        raise KelmoError('E_INPUT', 'A pending summary is one plain line of text.')
    line = ' '.join(pending.split())
    if not line or len(line) > PENDING_MAX:
        raise KelmoError('E_INPUT', f'A pending summary is one plain line of 1 to {PENDING_MAX} characters.')
    return line


def request(service, project, selected, rule_change, budget_tokens, new_work=None, pending=None, *, previous=None):
    if not isinstance(project, str) or not pages.PROJECT.fullmatch(project):
        raise KelmoError('E_INPUT', 'Provide a project name using lowercase letters, digits, _ or -.')
    if (type(rule_change) is not bool or type(budget_tokens) is not int or budget_tokens <= 0
            or not isinstance(selected, (list, tuple))
            or not all(isinstance(target, str) for target in selected)):
        raise KelmoError('E_INPUT', 'Select page targets and a positive integer context budget.')
    pending = pending_line(pending)
    registry = engine.load_registry(service.root)
    known = {pages.project_of(target) for target in registry['pages']}
    known.discard(None)
    known.add('person')
    if project not in known:
        from .projects import canonical_project
        project = canonical_project(registry, project) or project
    if project not in known:
        raise KelmoError('E_INPUT', 'No project map is registered for ' + project + '. Known projects: '
                         + (', '.join(sorted(known)) or 'none') + '. Use kelmo project add ' + project + '.')
    selected = list(dict.fromkeys(resolve_target(registry, target) for target in selected))
    if not selected:
        selected = sorted(target for target, page in registry['pages'].items()
                          if pages.project_of(target) == project and page.get('layout') == 'project-map')
        if not selected and project == 'person':
            selected = sorted(engine.person_targets(registry))
        if not selected and project + '/' + project + '.md' in registry['pages']:
            # A project kept at the wiki top level (`<project>/`) is entered through its own index page.
            selected = [project + '/' + project + '.md']
    if not selected:
        raise KelmoError('E_INPUT', 'No project map is registered for ' + project + '; select an existing '
                                   'page with --target, or enroll its project map first.')
    if rule_change:
        try:
            service.bindings.require_requester(service.origin)
        except ValueError as error:
            raise KelmoError('E_ORIGIN', 'Rule changes need a requester-capable enrolled origin.') from error
    selection = {'project': project, 'selected': selected, 'rule_change': rule_change,
                 'budget_tokens': budget_tokens}
    if new_work is not None:
        saved_work = current_request(registry, previous).get('new_work') if previous else None
        opened = new_work_page(service, registry, project, new_work, existing=saved_work)
        if opened['parent'] not in selected:
            selected.append(opened['parent'])
        selection['new_work'] = opened
    if pending is not None:
        selection['pending'] = pending
    return selection


# Editor attempts a source gets before its refusal is final: each is one model consultation, the next
# one carrying the previous refusal as its hint. Infrastructure failures (E_BUSY, E_CLAIM, quota) are
# never charged.
PARK_ATTEMPTS = 3
UNCHARGED = ('E_BUSY', 'E_CLAIM')
PENDING_STATES = ('archived', 'parked')


def current_request(registry, request):
    """Follow published project redirects without rewriting captured source metadata."""
    from .projects import canonical_project
    previous = request.get('project')
    current = canonical_project(registry, previous)
    if current is None or current == previous:
        return request
    result = dict(request, project=current)
    result['selected'] = [resolve_target(registry, target) for target in request.get('selected', [])]
    if request.get('new_work') is not None:
        work = dict(request['new_work'])
        prefix = pages.project_directory(previous) + '/'
        if work['target'].startswith(prefix):
            work['target'] = pages.project_directory(current) + '/' + work['target'][len(prefix):]
        work['parent'] = resolve_target(registry, work['parent'])
        result['new_work'] = work
    return result


def opened_work_page(registry, project, new_work):
    """The registered page a stored `new_work` already opened, or None when its target holds no work page.

    A work page registered at the request's target, in this project's own directory, is the page the request
    opened, whatever its title, summary or parent say now: the slug was checked against the live registry
    when the note was captured, so a page at that target was opened after it, by a note about the same work
    or by the request's own earlier attempt, and the editor may have retitled it since. The page is used as
    the registry holds it and never re-created under the request's identity. A page of another kind at the
    target is no work page, and a target outside the project's directory belongs to another project: both
    stay unresolved, and the editor refuses the request."""
    if not isinstance(new_work, dict):
        return None
    target = new_work.get('target')
    entry = registry['pages'].get(target)
    if entry is None or pages.project_of(target) != project or entry.get('kind') != 'work':
        return None
    return target


def _editor_request(service, request):
    """The keyword arguments Editor.edit takes from one stored creation request.

    The budget is the larger of the stored one and the vault's current `[editor] budget_tokens`: a
    note captured before the budget was raised is edited under the raised budget, not refused under
    the one that no longer holds the mandatory pages.

    A `new_work` whose page another note already opened is resolved here, the one place both the
    single-source and the batched path read a stored request: the request keeps `new_work` and selects
    that page, which is how the editor tells an opening request from one whose page is already there.
    A request never rewrites its captured metadata, exactly as `current_request` never does; a target
    registered as a page of another kind is left unresolved and stays the editor's refusal."""
    registry = engine.load_registry_view(service.root)
    request = current_request(registry, request)
    result = {name: value for name, value in request.items() if name not in ('pending',)}
    result['budget_tokens'] = max(request['budget_tokens'], service.config.editor_budget)
    opened = opened_work_page(registry, request['project'], request.get('new_work'))
    if opened is not None and opened not in result['selected']:
        result['selected'] = [*result['selected'], opened]
    return result


# Refusals the vault itself causes: the generation moved while the model was consulted (E_STALE), the
# outcome store refused the reason of a publication (E_OUTCOME), the controller enrollment did not match
# the pages a proposal would change (E_BINDING). None of them is the note's own content, so the note is
# parked and retried with a fresh baseline like any refusal; the first FREE_STALE_REFUSALS of them are not
# charged, so a busy wiki or a broken vault delays a note without failing it, and a note that meets the
# same refusal forever still ends in the owner's alert instead of consulting the model endlessly.
UNCHARGED_REFUSALS = ('E_STALE', 'E_OUTCOME', 'E_BINDING')
FREE_STALE_REFUSALS = 5


def _budget_events(trace):
    """The events of this source's current attempt budget: everything after its last `retry_budget_reset`.

    `kelmo queue retry` records that event, and the attempts it renewed must not be spent by the parks an
    earlier run of the same note accumulated: a retried note gets PARK_ATTEMPTS again, not one."""
    events = trace['events']
    reset = max((index for index, event in enumerate(events) if event['stage'] == 'retry_budget_reset'),
                default=-1)
    return events[reset + 1:]


def _parked(trace):
    """(parked attempts so far, attempts charged among them, the last parked event's details or None).

    Counted over `_budget_events`, so a retry's reset starts the count again."""
    parked = [event for event in _budget_events(trace) if event['stage'] == 'parked']
    details = [event.get('details') or event for event in parked]
    free = 0
    charged = 0
    for detail in details:
        if detail.get('error_code') in UNCHARGED_REFUSALS and free < FREE_STALE_REFUSALS:
            free += 1
        else:
            charged += 1
    return len(parked), charged, details[-1] if details else None


def _free_refusal(trace, error):
    """Whether this refusal is one of the uncharged refusals a note may still spend.

    The FREE_STALE_REFUSALS already spent belong to the current budget: after a `retry_budget_reset` the
    note has them again."""
    if error.code not in UNCHARGED_REFUSALS:
        return False
    spent = sum((event.get('details') or event).get('error_code') in UNCHARGED_REFUSALS
                for event in _budget_events(trace) if event['stage'] == 'parked')
    return spent < FREE_STALE_REFUSALS


def park(service, operation_id, error, token, attempts):
    """Hold a source whose editor attempt was refused, for the next `kelmo dream apply` pass.

    Content refusals (a malformed proposal, a wrong receipt, a stale generation, an executor that
    produced no answer) are never final on their own: the source keeps its archive and outcome
    `unresolved` under state `parked`, and the next pass consults the model again with this refusal
    as the hint. The first FREE_STALE_REFUSALS refusals the vault itself causes (UNCHARGED_REFUSALS)
    spend none of the source's attempts, so a broken outcome store or a moved enrollment never fails a
    note that the model placed correctly. After PARK_ATTEMPTS charged attempts the refusal is final:
    the source fails and the vault owner is alerted, since a note the model cannot place three times,
    or one the vault refuses again and again, needs a person. Both counts run over the attempts since
    the source's last `retry_budget_reset`, so `kelmo retry` gives them back.
    """
    reason = outcomes.repair_reason(error.code, error)
    if not _free_refusal(service.trace(operation_id), error) and attempts + 1 >= PARK_ATTEMPTS:
        service.journal.stage(operation_id, 'failed', terminal=True, error_code=error.code, phase='editor',
                              claim_token=token, outcome=('unresolved', reason))
        from .alerts import LoginWatch
        LoginWatch.for_service(service).send(
            f'note {operation_id} could not be placed after {attempts + 1} editor attempts ({error.code}): '
            + ' '.join(str(error).split())[:300] + '. Requeue it with a target, or dismiss it.')
    else:
        service.journal.stage(operation_id, 'parked', error_code=error.code, phase='editor', claim_token=token,
                              outcome=('unresolved', reason))
    return service.journal.get(operation_id)


def _attempt(service, operation_id):
    """(idempotency key, hint, charged attempts) of this source's next editor attempt, from its own trace.

    The key counts this source's own queued and parked attempts, so a batch never shares a key between
    members and a source's attempt accounting stays per source. The parked part restarts after a
    `retry_budget_reset` while the queued part counts every queued event the source ever had, so an
    attempt of a retried note is never the key an earlier attempt already finished under.
    """
    trace = service.trace(operation_id)
    parked, attempts, last = _parked(trace)
    queued = sum(event['stage'] == 'queued' for event in trace['events'])
    key = f'create:{operation_id}:{queued}' + (f':park{parked}' if parked else '')
    hint = ({'code': last.get('error_code'), 'reason': (service.journal.get(operation_id).get('outcome') or {})
             .get('reason', '')} if last else None)
    return key, hint, attempts


def publish(service, operation_id, request, executor=None, *, now=False):
    """Run the editor on one archived source: resume its open attempt, or open the next one.

    In the vault's `nightly` editor mode a source waits (event `awaiting_dreaming`) until `kelmo dream
    apply` runs with `now`; in `immediate` mode the worker publishes it as soon as it is archived.
    """
    if service.config.editor_mode == 'nightly' and not now:
        service.journal.event_once(operation_id, 'awaiting_dreaming',
                                   check_writer=service.archive_api.assert_writer,
                                   reason='The next kelmo dream apply pass publishes this source.')
        return service.journal.get(operation_id)
    executor = executor or configured_executor(service)
    if executor is None:
        service.journal.event_once(operation_id, 'awaiting_executor',
                                   check_writer=service.archive_api.assert_writer,
                                   reason='Configure an executor and run the worker to publish this source.')
        return service.journal.get(operation_id)
    token = service.journal.claim(operation_id, lease_seconds=3600)
    if token is None:
        raise KelmoError('E_BUSY', 'This source is already being edited.')
    try:
        key, hint, attempts = _attempt(service, operation_id)
        try:
            service.editor.edit(operation_id, origin=service._operation_origin(operation_id),
                                idempotency_key=key, executor=executor, contexts=service.context_bindings,
                                hint=hint, **_editor_request(service, request))
        except quota.QuotaWait:
            return service.journal.get(operation_id)
        except KelmoError as error:
            if stopping.requested():
                raise Stopping() from error  # the core's stop caused it; the note waits, uncharged
            if error.code in UNCHARGED:
                raise
            return park(service, operation_id, error, token, attempts)
        service.journal.stage(operation_id, 'applied', terminal=True, claim_token=token)
        return service.journal.get(operation_id)
    finally:
        service.journal.release(operation_id, token)


# A batch failure that names the batch's shape rather than a member's content: only the batch's head is
# charged for it, because that is the source the editor could not consult. The rest of the batch spent no
# model call of its own and stays waiting, exactly as a source dropped by the budget shrink does.
BATCH_SHAPE = ('E_STATE', 'E_ORIGIN', 'E_PERMISSION', 'E_RECEIPT')


def _batchable(service, operation, request):
    """The origin this source may be edited under in a batch, or None when it stays single-source.

    A source is left out of the batch when its quota deferral has not reset, or when its owning origin
    cannot be read. A source whose own stored request opens a work page joins the batch: the page is
    opened with the batch's commit, named in the payload with the source whose request opened it, so the
    note that needs a page is not left on the path where a lost conversation leaves it with nothing.
    A batch carries one origin, because a rule change is attributable only to the origin that archived
    its source.
    """
    if service.editor.deferred_until(operation['id']) is not None:
        return None
    try:
        return service._operation_origin(operation['id'])
    except KelmoError:
        return None


def batch_members(service, waiting, ceiling, alone=frozenset()):
    """The batch one apply pass takes from the head of its waiting list, in capture order.

    The oldest waiting source names the batch — its project and its owning origin — and later waiting
    sources of that project and origin join it up to the ceiling. When the head cannot join a batch the
    pass publishes it alone and the next batch starts with the next pass: a batch never passes an older
    note, and a source left out keeps its place in the pass's order. A later source of the same project
    that cannot join (its quota deferral has not reset, or it belongs to another origin) ends the batch
    there, because the notes after it would overtake it; a source of another project is skipped, since
    it waits for its own batch anyway.

    `alone` names the sources this pass already sent through a batch whose answer never accounted for
    them (see `apply_pending`): each of them runs the single-source path in this same pass, so none of
    them joins a batch again. As the head it ends the batch, because the pass publishes it alone first;
    later in the batch's own project it ends the batch there, exactly as a source that cannot join does,
    because the notes after it would overtake a note that is about to be published alone.
    """
    if not waiting or ceiling < 1:
        return []
    first, first_request = waiting[0]
    if first['id'] in alone:
        return []
    origin = _batchable(service, first, first_request)
    if origin is None:
        return []
    batch = [(first, first_request, origin)]
    for operation, request in waiting[1:]:
        if len(batch) >= ceiling:
            break
        if request.get('project') != first_request['project']:
            continue
        if operation['id'] in alone:
            break
        if _batchable(service, operation, request) != origin:
            break
        batch.append((operation, request, origin))
    return batch


def publish_batch(service, batch, executor):
    """One model consultation and one publication for a batch of waiting sources of one project.

    Each member is claimed under its own token, so a batch never holds a lock over a source it does not
    settle, and a member another attempt holds ends the batch there: the sources after it stay out too,
    because a batch never passes an older note it could not claim. A member whose stored request cannot
    be read ends the batch there as well, and is refused on its own turn at the head of a later batch.
    `Editor.edit_batch` holds one conversation with the model and settles every member on its own request row; each
    disposition then reaches that source's journal exactly as a single source's does: `applied` for a
    receipt (its own or one this pass replayed), `parked`/`failed` for a refusal charged to that source's
    own attempt count, and nothing at all for a source the batch dropped or left busy: it spends no
    attempt. A dropped source stays waiting for the next batch of the same pass; one another attempt holds
    waits for the next pass, with the notes of its project behind it.

    A refusal that is the head's alone (`BATCH_SHAPE`, `HeadRefusal`, or the head's own unreadable request)
    charges the head and leaves the rest waiting. The head is the source the editor led with, which is a
    later one when the oldest is settled already. Any other refusal raised for the batch as a whole is the
    vault's or the environment's, and every claimed member is parked for it, as each would be alone.

    Returns {operation_id: {'state': 'applied' | 'parked' | 'failed' | 'deferred' | 'waiting',
    'code': the refusal or contention code, or None}} for every source of the batch. A `waiting` source
    spent no attempt: it is still in the waiting list and may join the next batch of the same pass, except
    the one the answer named `alone`, which carries `'alone': True` and is sent through the single-source
    path by this same pass (see `apply_pending`).
    """
    claimed, attempts, items, origins = {}, {}, [], {}
    # What ended the batch early: 'E_CLAIM' when another attempt holds the next source, which holds the
    # sources behind it back; None when the next source's own request could not be read, which only ends
    # the batch there, so that source takes its turn as the head of a later one.
    cut = None
    try:
        for operation, request, origin in batch:
            token = service.journal.claim(operation['id'], lease_seconds=3600)
            if token is None:
                cut = 'E_CLAIM'
                break
            claimed[operation['id']] = token
            key, hint, charged = _attempt(service, operation['id'])
            attempts[operation['id']] = charged
            origins[operation['id']] = origin
            try:
                entry = {'source_operation_id': operation['id'], 'idempotency_key': key,
                         'request': _editor_request(service, request)}
            except KelmoError as error:
                if stopping.requested():
                    raise Stopping() from error
                if error.code in UNCHARGED:
                    raise
                if items:
                    # A later source whose stored request cannot be read ends the batch before it. It is
                    # refused on its own turn, as the head of the next batch, never with the notes ahead of it.
                    break
                # The head's own request is the refusal: it is charged for it alone, as `publish` charges it.
                row = park(service, operation['id'], error, token, charged)
                return {member['id']: ({'state': 'failed' if row['state'] == 'failed' else 'parked',
                                        'code': error.code} if member['id'] == operation['id']
                                       else {'state': 'waiting', 'code': None})
                        for member, _request, _origin in batch}
            if hint is not None:
                entry['request']['hint'] = hint
            items.append(entry)
        head = items[0]['source_operation_id'] if items else batch[0][0]['id']
        waiting = {operation['id']: {'state': 'waiting', 'code': cut}
                   for operation, _request, _origin in batch if operation['id'] not in claimed}
        if not items:
            return waiting
        try:
            result = service.editor.edit_batch(items, origin=origins[head], executor=executor,
                                               contexts=service.context_bindings)
        except quota.QuotaWait:
            return dict(waiting, **{operation_id: {'state': 'deferred', 'code': 'E_QUOTA'}
                                    for operation_id in claimed})
        except KelmoError as error:
            if stopping.requested():
                raise Stopping() from error  # the core's stop caused it; every member waits, uncharged
            if error.code in UNCHARGED:
                raise
            if error.code in BATCH_SHAPE or isinstance(error, HeadRefusal):
                # The batch itself could not be opened (its head is dismissed, unarchivable, unowned), or it
                # shrank to a head the budget cannot carry: only the head is charged for it, exactly as a
                # single source would be. The head is the source the editor led with, which is a later one
                # when the oldest was settled by a pass that died before it recorded that.
                head = getattr(error, 'lead', head)
                row = park(service, head, error, claimed[head], attempts[head])
                return dict(waiting, **{operation_id: {'state': 'waiting', 'code': error.code}
                                        for operation_id in claimed if operation_id != head},
                            **{head: {'state': 'failed' if row['state'] == 'failed' else 'parked',
                                      'code': error.code}})
            outcome = dict(waiting)
            for operation_id in claimed:
                row = park(service, operation_id, error, claimed[operation_id], attempts[operation_id])
                outcome[operation_id] = {'state': 'failed' if row['state'] == 'failed' else 'parked',
                                         'code': error.code}
            return outcome
        outcome = dict(waiting)
        for operation, _request, _origin in batch:
            operation_id = operation['id']
            if operation_id not in claimed:
                continue
            disposition = result['dispositions'].get(operation_id) or {}
            kind = disposition.get('disposition')
            if kind in ('applied', 'settled', 'archive-only'):
                service.journal.stage(operation_id, 'applied', terminal=True, claim_token=claimed[operation_id])
                outcome[operation_id] = {'state': 'applied', 'code': None}
            elif kind == 'refused':
                refusal = KelmoError(disposition['code'], disposition['reason'])
                row = park(service, operation_id, refusal, claimed[operation_id], attempts[operation_id])
                outcome[operation_id] = {'state': 'failed' if row['state'] == 'failed' else 'parked',
                                         'code': disposition['code']}
            elif kind == 'alone':
                # The batch answer left this source unaccounted for and ended its attempt without a refusal:
                # it spent no attempt and stays waiting, and the pass names it here so the same pass
                # consults it alone instead of sending it into a later batch of the same pass.
                outcome[operation_id] = {'state': 'waiting', 'code': disposition.get('code'), 'alone': True}
            else:
                outcome[operation_id] = {'state': 'waiting', 'code': disposition.get('code')}
        return outcome
    finally:
        for operation_id, token in claimed.items():
            service.journal.release(operation_id, token)


def pending_operations(service, project=None, *, include_unarchived=False, registry=None):
    """Creation sources awaiting publication, oldest first: (operation, request) pairs.

    Editor batches select archived or parked sources. Context delivery also includes durable
    captures still waiting for archival, without making those captures eligible for editing.
    """
    states = ('captured', 'queued', 'archived', 'parked') if include_unarchived else ('archived', 'parked')
    placeholders = ','.join('?' for _ in states)
    rows = service.journal.conn.execute(
        "SELECT id FROM operations WHERE intent='create' AND terminal=0 AND state IN ("
        + placeholders + ') ORDER BY created_at', states).fetchall()
    result = []
    if registry is None and rows:
        registry = engine.load_registry_view(service.root)
    for row in rows:
        operation = service.journal.get(row['id'])
        saved = service.journal.source_metadata(row['id'])
        request = (saved or {}).get('metadata', {}).get('editor_request')
        if request is None:
            continue
        request = current_request(registry, request)
        if project is not None and request.get('project') != project:
            continue
        result.append((operation, request))
    return result


# A held note is published only after every older note has left the intake, so the editor reads notes
# in the order they were captured, and a note that meets a held lock is retried in its place instead of
# being passed. A pass spends at most ORDER_WAIT_SECONDS waiting in all, and passes at once an older note
# no worker has touched for ORPHAN_SECONDS. Either is named in `stalled`, so one stuck note never stops
# the wiki, and kelmo doctor names the wait.
ORDER_WAIT_SECONDS = 3600
ORDER_POLL_SECONDS = 5
PUBLISH_RETRY_SECONDS = 30
ORPHAN_SECONDS = 86400


def notes_in_intake(service, before):
    """Create notes captured before `before` that the worker is still bringing to the editor's queue:
    queued (waiting or being archived), or failed on a cause it retries by itself, and not settled.
    A note waiting on a person (needs_review, needs_resolution) holds nothing back. Each row carries
    `moved_at`, the time of its last recorded event."""
    from .daemon import TRANSIENT_FAILURES
    connection = service.journal.conn
    outcomes.schema(connection)
    rows = connection.execute(
        "SELECT o.id, o.state, o.error_code, o.created_at, "
        "(SELECT MAX(e.created_at) FROM events e WHERE e.operation_id=o.id) AS moved_at "
        "FROM operations o WHERE o.intent='create' AND o.created_at < ? "
        "AND (o.state='queued' OR o.state='failed') ORDER BY o.created_at", (before,)).fetchall()
    return [dict(row) for row in rows
            if (row['state'] == 'queued' or row['error_code'] in TRANSIENT_FAILURES)
            and outcomes.get(connection, row['id']) is None]


def _age(stamp, clock):
    return clock() - datetime.fromisoformat(stamp).timestamp()


class _Turns:
    """The waiting one pass does: one shared budget of time spent waiting (not time spent editing), so
    N stuck notes cost one ORDER_WAIT_SECONDS, not N."""

    def __init__(self, report, sleep, clock):
        self.report, self.sleep, self.clock, self.waited = report, sleep, clock, 0.0

    def _nap(self, seconds):
        before = self.clock()
        self.sleep(seconds)
        self.waited += max(self.clock() - before, 0.0)

    def left(self):
        return self.waited < ORDER_WAIT_SECONDS

    def _stall(self, operation_id):
        if operation_id not in self.report['stalled']:
            self.report['stalled'].append(operation_id)

    def ahead(self, row):
        """Wait one poll for an older note still in the intake, or name it stalled."""
        if self.left() and _age(row['moved_at'] or row['created_at'], self.clock) < ORPHAN_SECONDS:
            self._nap(ORDER_POLL_SECONDS)
        else:
            self._stall(row['id'])

    def held(self, operation_id):
        """Wait before retrying a note that met a held lock; False once the pass budget is spent."""
        if self.left():
            self._nap(PUBLISH_RETRY_SECONDS)
            return True
        self._stall(operation_id)
        return False

    def busy_listing(self):
        """Wait out a busy database while the pending list is read; False once the budget is spent."""
        if self.left():
            self._nap(ORDER_POLL_SECONDS)
            return True
        return False


def first_pass_by_project(pairs):
    """{project: [operation_id]} of the (operation, request) pairs that wait for their first pass.

    Only state `archived` counts: a parked note is a retry, not a first pass, so it never makes a project
    ready and never stands in the way of the number a scheduler reads. Pairs keep the order they came in,
    which is capture order for the pending list.
    """
    waiting = {}
    for operation, request in pairs:
        project = request.get('project')
        if project is not None and operation['state'] == 'archived':
            waiting.setdefault(project, []).append(operation['id'])
    return waiting


def waiting_by_project(connection, registry=None):
    """{project: [operation_id]} of the notes waiting for their first pass, read from a journal connection.

    The same count `first_pass_by_project` makes from the pairs a pass already holds, answered straight
    from the journal so the caller opens no Service, no store and no model: `kelmo dream status` uses it to
    name how many first-pass notes each project has and which projects `dream apply --ready` would take
    now, so a scheduler can skip a tick it would publish nothing in. Rows come in capture order, so each
    project's first entry is its oldest waiting note and the mapping's own order is the order a ready pass
    would run its projects in. The stored request is read in Python rather than with SQLite's JSON
    functions, because a vault whose SQLite has no JSON1 still has to answer. `registry` (optional) follows
    published project redirects exactly as the pass does, so the number a scheduler reads is the number the
    pass counts. The connection must read rows as mapping rows (`sqlite3.Row`).
    """
    rows = connection.execute(
        "SELECT o.id, o.state, s.metadata FROM operations o "
        "JOIN operation_sources s ON s.operation_id = o.id "
        "WHERE o.intent='create' AND o.terminal=0 AND o.state='archived' ORDER BY o.created_at").fetchall()
    pairs = []
    for row in rows:
        request = (json.loads(row['metadata']) or {}).get('editor_request')
        if request is None:
            continue
        pairs.append(({'id': row['id'], 'state': row['state']},
                      current_request(registry, request) if registry is not None else request))
    return first_pass_by_project(pairs)


def apply_pending(service, executor, *, limit=100, sleep=time.sleep, clock=time.time, ready=False,
                  should_stop=None, step=None):
    """One `kelmo dream apply` pass: publish every note captured before the pass began through the
    editor, oldest first.

    Each source gets one attempt here; a refused one is parked for the next pass or fails after its
    last charged attempt (see `park`). A note waits while an older note is still in the intake, and a
    note that meets a held lock or a busy database is retried in its place (see ORDER_WAIT_SECONDS).
    With `[editor] batch_apply` on, the head of the waiting list and the later sources of its project
    are published as one batch (see `publish_batch`), never more of them than this pass may still
    handle; with the flag off every source goes through `publish` alone, exactly as before. A batch
    never passes an older note of its own project, and that rule holds across the batches of one pass:
    when a note is left waiting — a batch could not claim it, its quota deferral is still ahead, or the
    pass spent its waiting budget on it — the notes of its project captured after it are held back for the
    rest of the pass and named in `skipped`, so no newer note is published ahead of the older one in a
    batch or alone. The notes ahead of it are older and keep their turn. A source of another project keeps
    its own turn. A note whose turn fails with a fault that is no Kelmo refusal holds nothing back: it is
    named `E_INTERNAL` and the pass goes on (see below).
    A source a batch answer left unaccounted for (see `publish_batch` and `Editor._settle_batch`) is kept
    in this pass's own `alone` set for the rest of it: `batch_members` never takes it into a batch again,
    and the same pass runs it through the single-source path with a real model call, so a note never
    spends a second batch conversation in the pass that already sent it alone. The result counts what
    happened, by operation id, and names in `waiting` what it left: {project: notes of this pass's own
    snapshot it did not settle}, so a scheduler can read how much work remains without a second pass.

    With `ready` (the CLI's `--ready`) the pass runs over ready projects only: a project is ready when at
    least `[editor] batch_ready_sources` of its notes wait for their first pass (state `archived`; a parked
    note is a retry and counts towards no readiness number). Readiness is counted once, from the notes
    captured before the pass began, so a note that arrives during the pass counts towards no pass that is
    already running and a project that was not ready at the start stays out of it. Ready projects run in
    the order of their oldest waiting note, because the pass still takes the head of the waiting list.
    Inside a ready project the pass still works oldest first, in batches of at most `batch_max_sources`,
    and leaves that project once fewer than `batch_ready_sources` of its first-pass notes remain, so what
    is left waits for the next pass: 17 notes at 8 and 8 give two batches and one note left, 10 notes at
    ready 10 and 8 per batch give one batch of 8 and two left. A ready project whose oldest waiting note
    is left waiting is skipped for the rest of the pass (see the held-back rule above) while the other
    ready projects still run. A note a batch of the pass sent alone runs its single-source turn even when
    its project has dropped below the number since: the pass took it, so it does not leave it behind.

    In the state core `step` is the job step: each note, or each batch, is one step, so an interactive
    write waits for one unit at most, and the pass lets the baton go while it waits for an older note.
    `should_stop` is read before every unit and after every wait; once it is true the pass raises
    Stopping, with no note held.
    """
    if type(limit) is not int or limit < 1:
        raise KelmoError('E_INPUT', 'The apply limit is a positive integer.')
    if ready and not service.config.batch_apply:
        # Refused in the engine, not only in the CLI, so every caller of the ready pass meets the same
        # error: a ready project is published in batches, and with the flag off it would take no note.
        raise KelmoError('E_CONFIG', 'The ready apply pass needs [editor] batch_apply: it publishes a ready '
                                     'project in batches of batch_max_sources, and with batch_apply off it '
                                     'would take no note of that project.')
    ready_at = service.config.batch_ready_sources if ready else 0
    report = {'applied': [], 'parked': [], 'failed': [], 'deferred': [], 'skipped': [], 'stalled': [],
              'waiting': {}}
    step = step or nullcontext

    def rest(seconds):
        # A wait holds no journal transaction: interactive writes go first meanwhile.
        from .core.baton import model_call
        with model_call():
            sleep(seconds)
        if should_stop is not None and should_stop():
            raise Stopping()

    turns = _Turns(report, rest, clock)
    started = journal_now()
    seen, held_back = set(), {}
    # The sources this pass already sent through a batch whose answer never accounted for them: each of
    # them runs the single-source path in this same pass, so `batch_members` never takes one of them into
    # a batch again and no later batch of the pass spends a second conversation on a note that is about
    # to be published alone.
    alone = set()
    # The notes this pass began with, by project: `waiting` is read over them, so a note captured during
    # the pass belongs to the next one and is not counted as left behind. With --ready, the projects that
    # reached the number in that same first read (readiness is counted once, not re-counted per round).
    snapshot, ready_projects = None, None
    while len(seen) < limit:
        if should_stop is not None and should_stop():
            raise Stopping()
        with step():
            try:
                pending = [(operation, request) for operation, request in pending_operations(service)
                           if operation['created_at'] <= started]
                if snapshot is None:
                    snapshot = {operation['id']: request.get('project') for operation, request in pending}
                waiting = [(operation, request) for operation, request in pending if operation['id'] not in seen]
                # The later sources of a project this pass could not get past: they stay waiting, named here
                # before anything else narrows the list, so one blocked note never lets the notes behind it
                # overtake it inside the same pass and is never silently dropped from the pass's own report.
                # Only the notes captured after the blocker are held: a note ahead of it is older, overtakes
                # nothing, and one of them a batch sent alone still owes its single-source turn to this pass.
                place = {operation['id']: index for index, (operation, _request) in enumerate(pending)}
                for project, (blocker, code) in held_back.items():
                    if blocker not in place:
                        continue  # another attempt settled it meanwhile: nothing is left to overtake
                    first_held = place[blocker]
                    for operation, request in waiting:
                        if (request.get('project') != project or operation['id'] in seen
                                or place[operation['id']] < first_held):
                            continue
                        seen.add(operation['id'])
                        reason = ('held by another attempt' if operation['id'] == blocker
                                  else 'held back by the older note ' + blocker)
                        report['skipped'].append({'operation_id': operation['id'], 'error_code': code,
                                                  'reason': reason})
                waiting = [(operation, request) for operation, request in waiting if operation['id'] not in seen]
                if ready:
                    if ready_projects is None:
                        ready_projects = {project for project, ids in first_pass_by_project(waiting).items()
                                          if len(ids) >= ready_at}
                    remaining = first_pass_by_project(waiting)
                    active = {project for project in ready_projects
                              if len(remaining.get(project, ())) >= ready_at}
                    # A note a batch of this pass sent alone stays in the pass whatever its project's count
                    # has dropped to: the pass took it into that batch, so it owes it the single-source turn.
                    waiting = [(operation, request) for operation, request in waiting
                               if request.get('project') in active or operation['id'] in alone]
                if not waiting:
                    break
                operation, request = waiting[0]
                # Polling the intake alone is cheap. Once an older note leaves it, the pending list is read
                # again, because that note now goes first.
                moved = False
                while True:
                    ahead = [row for row in notes_in_intake(service, operation['created_at'])
                             if row['id'] not in report['stalled']]
                    if not ahead:
                        break
                    moved = True
                    turns.ahead(ahead[0])
            except sqlite3.Error as error:
                # The journal is busy before any note was chosen: wait, or leave the rest for the next pass.
                if not sqlite_busy(error):
                    raise
                if turns.busy_listing():
                    continue
                break
            if moved:
                continue
            try:
                batch = (batch_members(service, waiting, min(service.config.batch_max_sources, limit - len(seen)),
                                       alone=alone) if service.config.batch_apply else [])
                if len(batch) > 1:
                    # One conversation and one commit for the head of the waiting list and the later sources
                    # of its project; each member still settles on its own row and in its own bucket.
                    outcome = publish_batch(service, batch, executor)
                    moved, sent_alone = False, False
                    for operation_id, item in outcome.items():
                        if item['state'] == 'waiting':
                            if item.get('alone'):
                                alone.add(operation_id)
                                sent_alone = True
                            continue
                        seen.add(operation_id)
                        report[item['state']].append(operation_id)
                        moved = True
                    # The earliest member this batch could not claim, or one its quota deferral left
                    # unconsulted, holds the rest of its project back for the rest of the pass: a batch never
                    # passes an older note, and the pass must not start a later batch of that project that
                    # does. A source the batch dropped for its own budget (`E_BUDGET`, which no other attempt
                    # stands behind) or left behind an older note (no code) keeps its place in the next batch.
                    for member, _request, _origin in batch:
                        item = outcome.get(member['id']) or {}
                        if item.get('state') == 'deferred' or (item.get('state') == 'waiting'
                                                               and item.get('code') in UNCHARGED):
                            held_back[batch[0][1]['project']] = (member['id'], item.get('code') or 'E_CLAIM')
                            break
                    if not moved and not sent_alone:
                        # Nothing this batch touched left the waiting list: name the head and move on, so a
                        # source another attempt holds never spins the pass. A member the answer sent alone
                        # did leave it — the same pass publishes it on the single-source path — so the head
                        # of an exhausted batch is never named here: it would spend the pass's waiting budget
                        # on a note the batch charged nothing for, and the notes behind it would never be
                        # consulted.
                        seen.add(operation['id'])
                        report['skipped'].append({'operation_id': operation['id'],
                                                  'error_code': outcome[operation['id']]['code'] or 'E_BUSY'})
                    continue
                if service.editor.deferred_until(operation['id']) is not None:
                    # The head waits for its quota reset. Its project is held back for the rest of the pass,
                    # because the next note of that project would otherwise become the head and be published
                    # ahead of the older one.
                    seen.add(operation['id'])
                    report['deferred'].append(operation['id'])
                    held_back.setdefault(request['project'], (operation['id'], 'E_QUOTA'))
                    continue
                result = publish(service, operation['id'], request, executor, now=True)
            except (KelmoError, sqlite3.Error) as error:
                contention = (sqlite_busy(error) if isinstance(error, sqlite3.Error)
                              else error.code in UNCHARGED)
                if not contention:
                    raise
                if turns.held(operation['id']):
                    continue
                seen.add(operation['id'])
                code = 'E_BUSY' if isinstance(error, sqlite3.Error) else error.code
                report['skipped'].append({'operation_id': operation['id'], 'error_code': code})
                # The pass spent its waiting budget on this note and left it waiting: its project is held
                # back for the rest of the pass, so the notes behind it wait for it instead of overtaking it.
                held_back.setdefault(request['project'], (operation['id'], code))
                continue
            except AssertionError:
                raise  # a broken invariant (the baton's, a shared registry changed in place) ends the pass
            except Exception as error:  # noqa: BLE001 - one note's fault must not end the pass for every note
                # The note keeps its journal state: a publication it already committed is finished by recovery
                # on its next turn, as after a crash. It is named here, and the pass goes on with the next note,
                # of its own project too: unlike a held claim or a quota wait, a fault does not clear by
                # itself, so holding the project back behind it would stall that project on every pass.
                print(f'kelmo: note {operation["id"]} failed in the apply pass ({type(error).__name__}: {error}); '
                      'it is tried again on the next pass', file=sys.stderr, flush=True)
                seen.add(operation['id'])
                report['skipped'].append({'operation_id': operation['id'], 'error_code': 'E_INTERNAL'})
                continue
            seen.add(operation['id'])
            state = result['state']
            bucket = ('applied' if state == 'applied' else 'parked' if state == 'parked'
                      else 'failed' if state == 'failed' else 'deferred')
            report[bucket].append(operation['id'])
            if bucket == 'deferred':
                # The consultation met a quota signal and the note is still archived: its project waits for
                # its reset instead of letting the notes behind it publish ahead of it.
                held_back.setdefault(request['project'], (operation['id'], 'E_QUOTA'))
    if snapshot is not None:
        # What this pass left, by project: the notes it began with that it did not settle. A note that was
        # deferred, held or stopped short of the pass's own limit still waits, so it is named here.
        settled = set(report['applied']) | set(report['parked']) | set(report['failed'])
        left = {}
        for operation_id, project in snapshot.items():
            if operation_id not in settled:
                left[project] = left.get(project, 0) + 1
        report['waiting'] = left
    return report


def pending_summaries(service, project=None, limit=20, *, registry=None):
    """The pending lines of waiting notes, newest last, for a project (or every project)."""
    lines = []
    for operation, request in pending_operations(service, project, include_unarchived=True, registry=registry):
        if request.get('pending'):
            lines.append({'operation_id': operation['id'], 'project': request.get('project'),
                          'state': operation['state'], 'pending': request['pending']})
    return lines[-limit:]
