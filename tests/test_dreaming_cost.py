"""Dreaming's apply pass: the replay benchmark that measures what it costs, before and after batching.

Run it as a benchmark (one command, both figures):

    PYTHONPATH=src python tests/test_dreaming_cost.py

Run it as cases (the same pass, pinned):

    PYTHONPATH=src python -m pytest -q tests/test_dreaming_cost.py

The fixture is the synthetic vault of `tests/test_editor_scoping.py` with a second project
(`other_project=True`): two projects, 12 creation sources captured under `[editor] mode = "nightly"` and
archived, so `apply_pending` finds them waiting. The executor answers the way the nightly model answers
here — one page expansion, then a proposal that records the note's own line on the page it was sent —
and counts every `ModelJob` it receives. Nothing here changes the editor or `apply_pending`: the pass
runs as it is.

The single-source figure, printed by the benchmark command with `[editor] batch_apply` off (the default):

    single  sources=12 published=12 parked=0 model_calls=24 calls_per_published_note=2.00 publications=12
            payload_tokens=35926
            payload_context=32806 payload_source=3096 payload_receipts=24 execute_calls=24

so twelve waiting notes across two projects cost twenty-four model calls and twelve commits, and of the
35926 estimated tokens the payloads carried, 32806 were pages, mandatory rules and the project index —
re-sent on every call — while 3096 carried the notes themselves. A real model can sit above 2.00 because it may also
spend its second expansion and its repair rounds (`MAX_EXPANSIONS`, `MAX_REPAIRS`); what this fixture
reproduces is the multiplier they share — one conversation per source over the same project pages, and one
commit per source.

The batched figure is the same fixture and the same model behaviour under `[editor] batch_apply = true`,
printed by the same command beside it:

    batched sources=12 published=12 parked=0 model_calls=4 calls_per_published_note=0.33 publications=2
            payload_tokens=7516
            payload_context=3888 payload_source=3624 payload_receipts=4 execute_calls=4

so the same twelve notes cost four model calls and two commits — one batch per project, the app batch
held to `DEFAULT_BATCH_MAX_SOURCES` (8) and the ops batch of 4 — and the payload they carried was 7516
estimated tokens against 35926. The notes themselves cost a little more in a batch (3624 against 3096: both
passes send 24 source blocks, and a batch block names its 36-character `operation_id`, measured here by
re-costing the same 24 blocks without that key — 22 tokens each, 528 over the pass), while the project
pages, mandatory rules and index that the single-source pass re-sent on all twenty-four calls are sent four
times instead (3888 against 32806): the saving is the context, not the notes. `calls_per_published_note` is
below 1 because a batch publishes more than one note per conversation; the per-source accounting the
single-source figure shows is unchanged for a source the batch drops or leaves for its own attempt
(`tests/test_dreaming_batched_apply.py`).
"""
import sys

import pytest

from test_editor_scoping import FEATURE, NOTES, OPS, build_world
from test_dreaming_apply import nightly

COUNTS = {'app': 8, 'ops': 4}
PROJECTS = {'app': dict(selected=[FEATURE], expand=[NOTES]),
            'ops': dict(selected=[OPS], expand=['projects/ops/ops.md'])}
HEADLINE = ('sources', 'published', 'parked', 'model_calls', 'calls_per_published_note', 'publications',
            'payload_tokens')
EXTRA = ('payload_context', 'payload_source', 'payload_receipts', 'execute_calls')


def note_text(project, index):
    return (f'{project.title()} note {index}: the {project} pipeline moves to '
            f'{7 + index:02d}:{index:02d} after the audit.')


class ReplayExecutor:
    """A fake model that answers like the nightly one and counts every job it is handed.

    Round one asks for the project's other registered page (the expansion the real model asks for when a
    note concerns a page outside its selection); round two proposes one fact carrying the note's own line
    onto the page it was sent. `broken` names note texts whose proposal round is the executor failure
    that parks a source instead of publishing it.
    """

    def __init__(self, world, broken=()):
        self.world, self.broken, self.jobs = world, set(broken), []

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        text = payload['source']['text']
        wanted = [page for page in PROJECTS[payload['project']]['expand'] if page in payload['expandable']]
        if payload['expansions_left'] and wanted:
            return ModelOutcome('expand', {'pages': wanted})
        if text in self.broken:
            return ModelOutcome('failure', error_code='E_EXECUTOR', reason='the replay produced no proposal')
        evidence = payload['source']['evidence']['content_sha256']
        fact = dict(fact_key=f"{payload['project']}.replay-{evidence[:10]}", target=payload['pages'][0]['target'],
                    text=text.strip(), status='active', expected_revision=None)
        return self.world.propose(facts=[fact])


class BatchReplay:
    """A fake model that answers a batch payload the way the nightly one does: one proposal naming every source.

    Used for the batched figure (`[editor] batch_apply = true`): the batch is one conversation, so the
    expansion round is asked once for the batch and the proposal round carries one fact per member, each
    naming the source it comes from. A single-source payload (the head of a batch the ceiling left over, or
    a source the budget shrank out) is answered exactly as `ReplayExecutor` answers it, so one executor
    covers both paths of a batched pass.

    `jobs` holds every `ModelJob` it received, batch payloads included, so the payload cost of a batched
    call is read from the same place the single-source figure reads it.
    """

    def __init__(self, world):
        self.world, self.jobs = world, []

    def execute(self, job):
        from kelmo.executor import ModelOutcome
        self.jobs.append(job)
        payload = job.payload
        wanted = [page for page in PROJECTS[payload['project']]['expand'] if page in payload['expandable']]
        if payload['expansions_left'] and wanted:
            return ModelOutcome('expand', {'pages': wanted})
        blocks = list(payload['sources']) if 'sources' in payload else [payload['source']]
        facts, checks = [], []
        for block in blocks:
            evidence = block['evidence']['content_sha256']
            fact = dict(fact_key=f"{payload['project']}.replay-{evidence[:10]}",
                        target=payload['pages'][0]['target'], text=block['text'].strip(), status='active',
                        expected_revision=None)
            check = {'fact_key': fact['fact_key'], 'verified_match': {'result': 'not_found',
                                                                      'search_id': self.world.service.reconcile(
                                                                          fact['text'], fact['target'])['search_id']}}
            if 'sources' in payload:
                fact['source'] = check['source'] = block['operation_id']
            facts.append(fact)
            checks.append(check)
        return ModelOutcome('proposal', {'facts': facts, 'fact_checks': checks})


class Counted:
    """The executor `apply_pending` is handed: it counts the `execute` calls made through it, so the fake
    model's own job list can be checked against the calls the editor actually made."""

    def __init__(self, inner):
        self.inner, self.calls = inner, 0

    def execute(self, job):
        self.calls += 1
        return self.inner.execute(job)


def commits(root):
    from kelmo.wiki import engine
    return int(engine.git(root, 'rev-list', '--count', 'HEAD'))


def capture(world, counts=COUNTS):
    """Capture and archive the fixture's waiting sources, oldest first, and return their texts."""
    texts = []
    for project, total in counts.items():
        for index in range(total):
            text = note_text(project, index)
            operation = world.service.create(text, project=project, selected=list(PROJECTS[project]['selected']))
            world.service.run(operation['id'])
            texts.append(text)
    return texts


def batch_mode(world, max_sources=None):
    """Write the `[editor]` table the batched pass runs under: nightly mode with `batch_apply` on."""
    from kelmo.config import Config
    lines = ['mode = "nightly"', 'batch_apply = true']
    if max_sources is not None:
        lines.append('batch_max_sources = ' + str(max_sources))
    toml = world.root / 'kelmo.toml'
    toml.write_text(toml.read_text(encoding='utf-8') + '\n[editor]\n' + '\n'.join(lines) + '\n',
                    encoding='utf-8')
    world.service.config = Config.load(world.root)


def blocks_of(payload):
    """The source blocks one payload carried: the batch's list, or the one source."""
    return list(payload['sources']) if 'sources' in payload else [payload['source']]


def numbers(world, executor, counted, report, counts, before):
    """The headline the benchmark prints, read off one finished pass."""
    from kelmo.wiki import views
    payloads = [job.payload for job in executor.jobs]
    source = sum(views._cost(block) for payload in payloads for block in blocks_of(payload))
    receipts = sum(views._cost(payload['reconciliations']) for payload in payloads)
    charged = sum(payload['budget']['used_tokens'] for payload in payloads)
    published = len(report['applied'])
    return {
        'sources': sum(counts.values()),
        'published': published,
        'parked': len(report['parked']),
        'model_calls': len(executor.jobs),
        'calls_per_published_note': round(len(executor.jobs) / published, 2) if published else 0,
        'publications': commits(world.root) - before,
        'payload_tokens': charged,
        'payload_context': charged - source - receipts,
        'payload_source': source,
        'payload_receipts': receipts,
        'execute_calls': counted.calls,
        'report': report, 'executor': executor,
    }


def measure(world, counts=COUNTS, broken=None, batch=False):
    """One `kelmo dream apply` pass over the fixture, with the numbers the pass is measured by."""
    from kelmo.creation import apply_pending
    texts = capture(world, counts)
    if batch:
        executor = BatchReplay(world)
    else:
        executor = ReplayExecutor(world, broken=() if broken is None else (texts[broken],))
    counted = Counted(executor)
    before = commits(world.root)
    return numbers(world, executor, counted, apply_pending(world.service, counted), counts, before)


@pytest.fixture
def world(tmp_path, monkeypatch):
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        yield next(built)
    finally:
        built.close()


@pytest.fixture
def batched_world(tmp_path, monkeypatch):
    """The same fixture under `[editor] mode = "nightly"` with `batch_apply = true`."""
    built = build_world(tmp_path, monkeypatch, other_project=True)
    try:
        world = next(built)
        batch_mode(world)
        yield world
    finally:
        built.close()


def test_the_replay_counter_counts_every_execute_call_the_editor_made(world):
    from kelmo.executor import ModelJob
    result = measure(world)
    assert all(isinstance(job, ModelJob) and job.task == 'edit_pages' for job in result['executor'].jobs)
    assert result['model_calls'] == result['execute_calls'] == len(result['executor'].jobs) > 0


def test_the_before_figure_is_one_conversation_and_one_commit_per_waiting_source(world):
    numbers = {name: value for name, value in measure(world).items() if name in HEADLINE + EXTRA}
    assert numbers == {'sources': 12, 'published': 12, 'parked': 0, 'model_calls': 24,
                       'calls_per_published_note': 2.0, 'publications': 12, 'payload_tokens': 35926,
                       'payload_context': 32806, 'payload_source': 3096, 'payload_receipts': 24,
                       'execute_calls': 24}, \
        'the single-source pass changed shape; re-run the benchmark command and move the figure with it'


def test_every_waiting_source_of_every_project_is_published_by_one_pass(world):
    result = measure(world)
    report = result['report']
    assert (len(report['applied']), len(report['parked']), len(report['failed'])) == (12, 0, 0)
    assert report['stalled'] == [] and report['skipped'] == [] and report['deferred'] == []
    assert {job.payload['project'] for job in result['executor'].jobs} == set(COUNTS)


def test_a_parked_source_still_pays_its_conversation_and_no_commit(world):
    result = measure(world, broken=3)
    report = result['report']
    assert (result['published'], result['parked'], len(report['failed'])) == (11, 1, 0)
    assert result['model_calls'] == 24, 'the parked source spent its expansion and its failed proposal'
    assert result['calls_per_published_note'] == round(24 / 11, 2)
    assert result['publications'] == 11, 'a parked source costs a consultation and no commit'
    assert len(report['parked']) == 1


def test_the_pass_re_reads_the_same_project_pages_for_every_source_it_publishes(world):
    result = measure(world)
    sent = {}
    for payload in [job.payload for job in result['executor'].jobs]:
        sent.setdefault(payload['project'], []).append(tuple(block['target'] for block in payload['pages']))
    for project, rounds in sent.items():
        assert all(rounds[0] <= target for target in rounds), project
        assert len(rounds) == COUNTS[project] * 2, project
    assert result['payload_context'] > 9 * result['payload_source']


def waiting_batches(result):
    """The size of each batch payload the pass consulted, in the order it consulted them."""
    return [len(job.payload['sources']) for job in result['executor'].jobs if 'sources' in job.payload]


def test_the_batched_pass_publishes_every_waiting_source_of_the_same_fixture(batched_world):
    result = measure(batched_world, batch=True)
    report = result['report']
    assert (len(report['applied']), len(report['parked']), len(report['failed'])) == (12, 0, 0)
    assert report['stalled'] == [] and report['skipped'] == [] and report['deferred'] == []
    assert waiting_batches(result) == [8, 8, 4, 4], \
        'one batch per project, bounded by the ceiling of 8, each consulted twice: expansion then proposal'


def test_the_after_figure_is_one_conversation_and_one_commit_per_batch_of_one_project(batched_world):
    figures = {name: value for name, value in measure(batched_world, batch=True).items()
               if name in HEADLINE + EXTRA}
    assert figures == {'sources': 12, 'published': 12, 'parked': 0, 'model_calls': 4,
                       'calls_per_published_note': 0.33, 'publications': 2, 'payload_tokens': 7516,
                       'payload_context': 3888, 'payload_source': 3624, 'payload_receipts': 4,
                       'execute_calls': 4}, \
        'the batched pass changed shape; re-run the benchmark command and move the figure with it'


def main():
    import shutil
    import tempfile
    from pathlib import Path
    scratch = Path(tempfile.mkdtemp(prefix='dreaming-cost-'))
    results = {}
    try:
        with Patcher() as patcher:
            for label, batch in (('single', False), ('batched', True)):
                built = build_world(scratch / label, patcher, other_project=True)
                try:
                    world = next(built)
                    batch_mode(world) if batch else nightly(world)
                    results[label] = measure(world, batch=batch)
                finally:
                    built.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    for label, result in results.items():
        print(label + ' ' + ' '.join(f'{name}={result[name]:.2f}' if isinstance(result[name], float)
                                     else f'{name}={result[name]}' for name in HEADLINE))
        print(' ' * len(label) + ' ' + ' '.join(f'{name}={result[name]}' for name in EXTRA))
        print(' ' * len(label) + ' report ' + ' '.join(f'{key}={len(value)}'
                                                       for key, value in result['report'].items()))
    return 0


class Patcher:
    """Stands in for pytest's `monkeypatch` when the benchmark is run as a script: `build_world` patches
    the embedding provider, the provider executor and the Git identity, and a script run puts them back."""

    def __init__(self):
        self.undo, self.env = [], []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        import os
        for name, value in self.env:
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        for target, name, original in reversed(self.undo):
            setattr(target, name, original)
        return False

    def setattr(self, target, name=None, value=None, raising=True):
        if isinstance(target, str):
            import importlib
            module_name, _, attribute = target.rpartition('.')
            target, name, value = importlib.import_module(module_name), attribute, name
        self.undo.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def setenv(self, name, value):
        import os
        self.env.append((name, os.environ.get(name)))
        os.environ[name] = value


if __name__ == '__main__':
    sys.exit(main())
