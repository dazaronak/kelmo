"""Controller-run source-to-session cases: accepted state reaches SessionStart from one generation.

They reuse test_action_state's real archive, journal, publisher and forbidden semantic executor,
and test_git_ingress's real disposable remote and second worktree. The delivery path is the
native one: hook('start') -> hot.render -> SessionStart additionalContext. Crash cases stop one
real step with a BaseException; no Git, archive or state call is mocked.
"""

import json
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from test_action_state import APP_PAGE, WEB_PAGE, PAGES, TOPIC_PAGE, world_factory as world_factory  # noqa: F401
from test_git_ingress import git, git_world as git_world, packet, push_receipt  # noqa: F401


class Crash(BaseException):
    """Process death at one boundary; nothing below it runs."""


@pytest.fixture
def world(world_factory):
    return world_factory()


def configure(root, pages, projects=None, max_bytes=None):
    """Explicit HOT selection in the vault's own configuration; a previous [hot] block is replaced."""
    path = root / 'kelmo.toml'
    text = path.read_text().split('\n[hot]')[0].rstrip('\n') + '\n\n[hot]\n'
    text += 'pages = ' + json.dumps(pages) + '\n'
    if max_bytes is not None:
        text += f'max_bytes = {max_bytes}\n'
    for name, (roots, selected) in (projects or {}).items():
        text += (f'\n[hot.projects.{name}]\nroots = ' + json.dumps([str(item) for item in roots])
                 + '\npages = ' + json.dumps(selected) + '\n')
    path.write_text(text)


def start(service, cwd=None):
    from kelmo.integration import hook
    payload = {} if cwd is None else {'cwd': str(cwd)}
    output = hook(service, 'start', payload)['hookSpecificOutput']
    assert output['hookEventName'] == 'SessionStart'
    return output['additionalContext']


def published(world, path, generation):
    return world.service.knowledge.page(path, generation).content.decode('utf-8')


def block(world, path, generation):
    return f'## {path}\n\n' + published(world, path, generation)


def header(context):
    return context.split('\n\n', 1)[0]


def feature_hot(fixture):
    configure(fixture.world.root, [WEB_PAGE], {'app': ([fixture.repo, fixture.other], [APP_PAGE])})


def test_real_push_reaches_the_next_session_from_the_committed_generation(git_world, tmp_path):
    fixture, world = git_world, git_world.world
    feature_hot(fixture)
    pending = world.archive('Deployment is pending.', key='deploy-pending')
    world.state.apply(world.update(pending, 'deploy-feature', transition='pending', event_order=1),
                      world.origins['requester'], 'deploy-pending')
    deployment = world.action('deploy-feature')
    before = start(world.service, fixture.repo)
    assert '[a:push-feature]' not in before

    payload = push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')
    assert payload['tool_response']['exit_code'] == 0
    assert git(fixture.remote, 'rev-parse', 'refs/heads/feature/shared').stdout.strip() == fixture.revision
    source = packet(payload)
    receipt = world.ingress.ingest(source, world.origins['producer'], 'push-receipt')
    ref = receipt.evidence_refs[0]
    assert world.service.archive_api.get(ref.archive_id, ref.version).source.content == source.content
    action = world.action('push-feature')
    assert action['state'] == 'completed' and action['accepted_event']['evidence_ref'] == ref.to_dict()
    assert action['accepted_event']['evidence_mark'] == 'PRODUCER'
    assert world.service.journal.prepared_actions() == []
    assert world.action('deploy-feature') == deployment and deployment['state'] == 'pending'

    generation = receipt.generation
    assert world.service.knowledge.generation() == generation
    context = start(world.service, fixture.repo)
    # The native command has no service; it reads the same committed generation.
    assert start(SimpleNamespace(root=world.root), fixture.repo) == context
    assert f'generation {generation.publication_id} ' in header(context)
    assert 'current, complete' in header(context)
    for path in ('person.md', WEB_PAGE, APP_PAGE):
        assert block(world, path, generation) in context
    assert '[a:push-feature]' in published(world, APP_PAGE, generation)
    assert f'## {TOPIC_PAGE}' not in context
    elsewhere = start(world.service, tmp_path / 'unenrolled')
    assert f'## {APP_PAGE}' not in elsewhere and block(world, WEB_PAGE, generation) in elsewhere


def test_failed_push_stays_open_and_leaves_session_context_unchanged(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    feature_hot(fixture)
    before_registry, before = world.registry(), start(world.service, fixture.repo)
    payload = push_receipt(fixture.repo, str(fixture.repo.parent / 'missing.git'), 'feature/shared', check=False)
    assert payload['tool_response']['exit_code'] != 0
    with pytest.raises(KelmoError):
        world.ingress.ingest(packet(payload), world.origins['producer'], 'failed-push')
    assert world.registry() == before_registry
    assert start(world.service, fixture.repo) == before


def test_duplicate_receipt_from_another_worktree_delivers_the_same_generation(git_world):
    fixture, world = git_world, git_world.world
    feature_hot(fixture)
    first = world.ingress.ingest(packet(push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')),
                                 world.origins['producer'], 'push-receipt')
    delivered = start(world.service, fixture.repo)
    rollback = push_receipt(fixture.repo, '--force-with-lease', 'origin',
                            fixture.base + ':refs/heads/feature/shared')
    assert world.ingress.ingest(packet(rollback, 'rollback-receipt'), world.origins['producer'],
                                'rollback-receipt').outcome == 'ignored'
    second = world.ingress.ingest(
        packet(push_receipt(fixture.other, 'origin', 'HEAD:refs/heads/feature/shared'), 'other-worktree'),
        world.origins['producer'], 'other-worktree')
    assert second.generation == first.generation and second.new_revision == first.new_revision
    assert world.service.knowledge.generation() == first.generation
    assert start(world.service, fixture.other) == delivered == start(world.service, fixture.repo)


def test_old_pending_cannot_reopen_the_delivered_push(git_world):
    from kelmo.errors import KelmoError
    fixture, world = git_world, git_world.world
    feature_hot(fixture)
    old = world.archive('Push is pending.', key='older-pending')
    update = world.update(old, 'push-feature', transition='pending', event_order=1)
    initial = world.state.apply(update, world.origins['requester'], 'pending')
    push_time = datetime.fromisoformat(world.action('push-feature')['accepted_event']['occurred_at']) + timedelta(seconds=1)
    source = replace(packet(push_receipt(fixture.repo, '-u', 'origin', 'feature/shared')),
                     observed_at=push_time.isoformat())
    pushed = world.ingress.ingest(source, world.origins['producer'], 'push-receipt')
    accepted, delivered = world.action('push-feature'), start(world.service, fixture.repo)
    assert accepted['state'] == 'completed'
    assert world.state.apply(update, world.origins['requester'], 'pending') == initial
    with pytest.raises(KelmoError) as error:
        world.state.apply(replace(update, expected_revision=pushed.new_revision, event_order=50),
                          world.origins['requester'], 'late-pending')
    assert error.value.code == 'E_STALE', 'an older source order is never advanced by a rebase'
    assert world.action('push-feature') == accepted
    assert start(world.service, fixture.repo) == delivered


def test_person_page_arrives_whole_with_ordinary_unflagged_facts(tmp_path, world_factory):
    import registry_seed
    from kelmo.config import Config
    from kelmo.service import Service
    from kelmo.wiki.pages import KnowledgeReader
    from test_editor_context import fact
    root = tmp_path / 'person-vault'
    Service.initialize(root, 'fixture-node')
    rule = fact('person.rule', 'person.md', 'Show the exact draft before sending; local drafts are exempt.')
    ordinary = fact('person.ordinary', 'person.md', 'Drinks green tea unless coffee is offered first.')
    spec = {'facts': {item['fact_key']: item for item in (rule, ordinary)},
            'notes': {item['note_id']: {'filename': 'source.md', 'ark_id': item['ark_id']}
                      for item in (rule, ordinary)}}
    registry_seed.seed(root, spec, pages_settings={'person.md': PAGES['person.md']},
                       protected_fact_keys=('person.rule',))
    configure(root, [])
    reader = KnowledgeReader(Config.load(root))
    person = reader.page('person.md', reader.generation()).content.decode('utf-8')
    assert rule['text'] in person and ordinary['text'] in person
    # These notes were never archived: their evidence stays unresolved, never exact.
    assert '[source:' not in person

    context = start(SimpleNamespace(root=root))

    assert f'## person.md\n\n{person}' in context
    assert 'mandatory pages: person.md.' in header(context)


def test_mandatory_record_on_a_project_page_is_delivered_whole_once(tmp_path, world_factory):
    import registry_seed
    from kelmo.config import Config
    from kelmo.contracts import ContextBinding, ContextBudget
    from kelmo.errors import KelmoError
    from kelmo.service import Service
    from kelmo.wiki.pages import KnowledgeReader
    from test_editor_context import fact
    root = tmp_path / 'mandatory-vault'
    Service.initialize(root, 'fixture-node')
    rule = fact('person.rule', 'person.md', 'Show the exact draft before sending; local drafts are exempt.')
    project = fact('app.rule', APP_PAGE, 'Never deploy the shared feature on Friday; hotfixes are exempt.')
    spec = {'facts': {item['fact_key']: item for item in (rule, project)},
            'notes': {item['note_id']: {'filename': 'source.md', 'ark_id': item['ark_id']}
                      for item in (rule, project)}}
    hub = dict(page_id='app-map', layout='project-map', title='App')
    registry_seed.seed(root, spec, pages_settings={'person.md': PAGES['person.md'], APP_PAGE: PAGES[APP_PAGE],
                                                           'projects/app/app.md': hub},
                protected_fact_keys=('person.rule', 'app.rule'))
    config = Config.load(root)
    reader = KnowledgeReader(config)
    generation = reader.generation()
    app = reader.page(APP_PAGE, generation).content.decode('utf-8')
    person = reader.page('person.md', generation).content.decode('utf-8')
    assert project['text'] in app

    for selected in ([], [APP_PAGE]):
        configure(root, selected)
        context = start(SimpleNamespace(root=root))
        assert f'## {APP_PAGE}\n\n{app}' in context and context.count(f'## {APP_PAGE}\n') == 1
        assert f'## person.md\n\n{person}' in context
        assert f'mandatory pages: person.md, {APP_PAGE}.' in header(context)
    # Mandatory delivery does not redefine the person project listing.
    assert [page.path for page in reader.pages('person', generation).entries] == ['person.md']
    assert [page.path for page in reader.mandatory(generation)] == ['person.md', APP_PAGE]

    for name in ('app', 'person'):
        bundle = reader.context(ContextBinding(config.vault_id, project=name), generation, ContextBudget(100000))
        assert bundle.mandatory_rules == (person, app) or bundle.mandatory_rules == (app, person)
        assert APP_PAGE not in [page.path for page in bundle.pages], 'a mandatory page is delivered once'
        assert bundle.omissions == ()
    with pytest.raises(KelmoError) as overflow:
        reader.context(ContextBinding(config.vault_id, project='app'), generation, ContextBudget(1))
    assert overflow.value.code == 'E_BUDGET'
    mandatory = len(start(SimpleNamespace(root=root)).encode('utf-8'))
    configure(root, [], max_bytes=mandatory - 1)
    assert start(SimpleNamespace(root=root)).startswith('Kelmo HOT incomplete (E_BUDGET)')


def test_unpublished_working_edits_are_never_delivered_as_current(world):
    from kelmo.config import Config
    configure(world.root, [APP_PAGE])
    ref = world.archive('I pushed the shared feature.')
    receipt = world.state.apply(world.update(ref), world.origins['requester'], 'complete-push')
    delivered = start(world.service)
    assert block(world, APP_PAGE, receipt.generation) in delivered
    wiki = Config.load(world.root).wiki
    for path in (APP_PAGE, 'person.md'):
        with (wiki / path).open('a') as page:
            page.write('\nUNPUBLISHED_SENTINEL\n')
    assert start(world.service) == delivered
    (wiki / 'projects/app/draft.md').write_text('DRAFT_SENTINEL\n')
    configure(world.root, [APP_PAGE, 'projects/app/draft.md'])
    refused = start(world.service)
    assert refused.startswith('Kelmo HOT unavailable (E_HOT)')
    assert 'SENTINEL' not in refused and '[a:push-app]' not in refused


@pytest.mark.parametrize('mutation', ['downgrade', 'missing', 'wrong-type', 'broken-without-working-manifest'])
def test_unpublished_registry_cannot_switch_to_working_pages(world, mutation):
    from kelmo.config import Config
    configure(world.root, [APP_PAGE])
    before = start(world.service)
    assert 'current, complete' in before
    config = Config.load(world.root)
    with (config.wiki / APP_PAGE).open('a') as stream:
        stream.write('\nUNPUBLISHED_MODE_SWITCH_SENTINEL\n')
    if mutation == 'missing':
        config.registry.unlink()
    elif mutation == 'broken-without-working-manifest':
        config.registry.write_text('{broken')
        config.registry.with_name('generation.json').unlink()
    else:
        registry = json.loads(config.registry.read_text())
        registry['version'] = 1 if mutation == 'downgrade' else '3'
        config.registry.write_text(json.dumps(registry))
    after = start(world.service)
    assert 'UNPUBLISHED_MODE_SWITCH_SENTINEL' not in after
    assert after == before


def test_mandatory_overflow_is_reported_incomplete_in_bytes(world):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    from kelmo.hot import render
    configure(world.root, [APP_PAGE])
    full = render(Config.load(world.root))
    assert full['complete'] is True and full['bytes'] == len(full['context'].encode('utf-8'))
    mandatory = len(full['context'].split(f'\n\n## {APP_PAGE}\n\n')[0].encode('utf-8'))

    configure(world.root, [APP_PAGE], max_bytes=mandatory - 1)
    with pytest.raises(KelmoError) as error:
        render(Config.load(world.root))
    assert error.value.code == 'E_BUDGET'
    context = start(world.service)
    assert context.startswith('Kelmo HOT incomplete (E_BUDGET)') and '## person.md' not in context

    configure(world.root, [APP_PAGE], max_bytes=mandatory)
    assert start(world.service).startswith('Kelmo HOT unavailable (E_HOT)')
    configure(world.root, [APP_PAGE], max_bytes=full['bytes'])
    assert start(world.service) == full['context']


def test_interrupted_publication_delivers_only_committed_generations(world, monkeypatch):
    from kelmo.wiki import engine
    configure(world.root, [APP_PAGE])
    before = start(world.service)
    update = world.update(world.archive('I pushed the shared feature.', key='pushed'))
    original = engine.publish_action

    def die(*args, **kwargs):
        raise Crash

    monkeypatch.setattr(engine, 'publish_action', die)
    with pytest.raises(Crash):
        world.state.apply(update, world.origins['requester'], 'crash-push')
    monkeypatch.setattr(engine, 'publish_action', original)
    assert len(world.service.journal.prepared_actions()) == 1
    assert start(world.service) == before

    world.reopen()
    receipt = world.state.apply(update, world.origins['requester'], 'crash-push')
    after = start(world.service)
    assert f'generation {receipt.generation.publication_id} ' in header(after)
    assert block(world, APP_PAGE, receipt.generation) in after and '[a:push-app]' in after


def test_pinned_generation_survives_a_deleted_and_recreated_manifest(world):
    from kelmo.config import Config
    from kelmo.errors import KelmoError
    manifest = Config.load(world.root).registry.with_name('generation.json')
    reader = world.service.knowledge
    first = reader.generation()
    first_bytes, first_app = manifest.read_bytes(), published(world, APP_PAGE, first)
    second = world.state.apply(world.update(world.archive('I pushed the shared feature.')),
                               world.origins['requester'], 'complete-push').generation
    second_bytes = manifest.read_bytes()
    git(world.root, 'rm', '-q', '--', manifest)
    git(world.root, 'commit', '--no-verify', '-m', 'test: delete the generation manifest')
    manifest.write_bytes(second_bytes)
    git(world.root, 'add', '--', manifest)
    git(world.root, 'commit', '--no-verify', '-m', 'test: recreate the generation manifest')
    assert reader.generation() == second

    assert published(world, APP_PAGE, first) == first_app and '[a:push-app]' not in first_app
    assert [page.path for page in reader.pages('person', first).entries] == ['person.md']
    for unmeasured in (replace(second, publication_id=second.publication_id + 5),
                       replace(first, manifest_sha256=second.manifest_sha256)):
        with pytest.raises(KelmoError) as error:
            reader.page(APP_PAGE, unmeasured)
        assert error.value.code == 'E_GENERATION', 'an unmeasured pin is never rebound to HEAD'

    # A present manifest that matches the pin but not its tree is corrupt evidence, never skipped.
    for raw, message in ((first_bytes, 'restore a mismatched manifest'), (second_bytes, 'restore head')):
        manifest.write_bytes(raw)
        git(world.root, 'add', '--', manifest)
        git(world.root, 'commit', '--no-verify', '-m', 'test: ' + message)
    assert reader.generation() == second
    with pytest.raises(KelmoError) as error:
        reader.page(APP_PAGE, first)
    assert error.value.code == 'E_GENERATION'


def test_corrupt_committed_generation_is_an_honest_refusal(world):
    from kelmo.config import Config
    configure(world.root, [APP_PAGE])
    manifest = Config.load(world.root).registry.with_name('generation.json')
    manifest.write_text('{}\n')
    git(world.root, 'add', '--', manifest)
    git(world.root, 'commit', '--no-verify', '-m', 'test: corrupt the generation manifest')
    context = start(world.service)
    assert context.startswith('Kelmo HOT unavailable (E_GENERATION)')
    assert '## person.md' not in context
