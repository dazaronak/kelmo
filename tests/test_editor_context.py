"""Exact page discovery and the bounded editor context.

Discovery is what an editor call fits inside a bounded prompt; the exact read is what it uses to recover
anything discovery left out. The cases here ask which page is answered, which fact is named as omitted, and
what a store that never opted in answers to all three verbs.
"""
import json
import subprocess
import sys

import pytest

from kelmo.service import Service
from kelmo.wiki import context, engine, pages


@pytest.fixture
def service(tmp_path):
    Service.initialize(tmp_path / 'vault', 'synthetic')
    with Service(tmp_path / 'vault') as value:
        yield value


def fact(key, target, text, status='active', role=None):
    note_id, ark_id = engine.digest('note-' + key), engine.digest('archive-' + key)
    stored = {'fact_key': key, 'target': target, 'text': text, 'status': status,
              'expected_revision': None, 'replaces': [], 'note_id': note_id, 'ark_id': ark_id,
              'id': engine.digest(key)[:12], 'previous_revision': None}
    if role is not None:
        stored['role'] = role
    stored['revision'] = pages.fact_revision(stored)
    return stored


AUTHORITY = '00000000-0000-4000-8000-00000000000c'


def spec(*facts):
    notes = {fact['note_id']: {'filename': 'source.md', 'ark_id': fact['ark_id']} for fact in facts}
    return {'facts': {item['fact_key']: item for item in facts}, 'notes': notes}


def settings(entries):
    """Page entries as seeded pages: the given kind, category and fields under the legacy layout."""
    return {target: dict(entry, page_id=target[:-3].replace('/', '.'), layout='legacy')
            for target, entry in (entries or {}).items()}


def registry(*facts, entries=None):
    import registry_seed
    return registry_seed.build(spec(*facts), authority_id=AUTHORITY, pages_settings=settings(entries))


def seed(root, *facts, entries=None):
    import registry_seed
    return registry_seed.seed(root, spec(*facts), pages_settings=settings(entries))


def work(summary='One line that places the work.', priority='H2', parent=''):
    return {'kind': 'work', 'category': 'projects', 'parent': parent, 'title': 'Work page',
            'summary': summary, 'priority': priority}


def topic(summary='What the project is about.'):
    return {'kind': 'topic', 'category': 'projects', 'parent': '', 'title': 'Topic page',
            'summary': summary, 'priority': ''}


def test_a_store_with_no_pages_answers_all_three_verbs_with_an_empty_page_model(service):
    assert service.page_index() == {'version': 3, 'projects': {}}
    assert service.page_facts('projects/fixture/fixture.md')['page']['facts'] == []
    view = service.editor_context('fixture', 4000)
    assert view['pages'] == [] and view['open_digest'] == [] and view['truncated'] is False


def test_the_index_names_every_page_of_a_project_and_a_project_with_no_page_is_an_empty_answer(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'First.'),
                     fact('other.a', 'projects/other/other.md', 'Elsewhere.'),
                     entries={'projects/fixture/fixture.md': work(), 'projects/fixture/idea.md': topic(),
                              'projects/other/other.md': work()})
    index = pages.page_index(value)
    assert sorted(index) == ['fixture', 'other']
    assert [row['target'] for row in index['fixture']] == ['projects/fixture/fixture.md', 'projects/fixture/idea.md']
    assert index['fixture'][0]['kind'] == 'work' and index['fixture'][0]['summary']
    assert 'summary' not in index['fixture'][1], 'a topic page carries a work page summary'
    assert pages.page_index(value, ['nothing']) == {'nothing': []}
    assert pages.page_index(value, ['fixture']) == {'fixture': index['fixture']}


def test_the_index_carries_the_derived_status_a_page_has_now(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work.', 'in-progress'),
                     entries={'projects/fixture/fixture.md': work()})
    assert pages.page_index(value)['fixture'][0]['status'] == 'in-progress'
    value['facts']['fixture.a']['status'] = 'shipped'
    assert pages.page_index(value)['fixture'][0]['status'] == 'shipped'


def test_an_exact_read_returns_the_page_the_index_only_named(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'First.', 'in-progress', role='constraint'),
                     fact('fixture.b', 'projects/fixture/fixture.md', 'Second.', 'backlog'),
                     entries={'projects/fixture/fixture.md': work()})
    read = pages.page_facts(value, 'projects/fixture/fixture.md')
    assert [item['fact_key'] for item in read['facts']] == ['fixture.a', 'fixture.b']
    assert read['status'] == 'in-progress' and read['hot'] == 'WIP'
    assert read['facts'][0]['role'] == 'constraint'
    assert read['facts'][0]['revision'] == value['facts']['fixture.a']['revision'], 'the read cannot be used as a revision'
    assert read['entry']['kind'] == 'work'
    empty = pages.page_facts(value, 'projects/fixture/unwritten.md')
    assert empty['facts'] == [] and empty['entry'] is None and empty['hot'] is None


def test_a_page_target_that_is_not_a_page_is_refused_without_echoing_it(service):
    value = registry(entries={'projects/fixture/fixture.md': work()})
    for bad in ('/etc/passwd', 'projects/fixture/../../secret.md', 'not a page'):
        with pytest.raises(ValueError, match='relative Markdown page'):
            pages.page_facts(value, bad)


def test_the_context_fits_the_index_and_the_open_digest_inside_the_budget(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work here.', 'in-progress'),
                     fact('fixture.b', 'projects/fixture/detail.md', 'Waiting there.', 'backlog'),
                     fact('fixture.c', 'projects/fixture/detail.md', 'Already shipped.'),
                     entries={'projects/fixture/fixture.md': work(), 'projects/fixture/detail.md': work()})
    view = context.editor_context(value, 'fixture', 4000)
    assert view['used_tokens'] <= 4000
    assert [row['target'] for row in view['pages']] == ['projects/fixture/detail.md', 'projects/fixture/fixture.md']
    assert {row['fact_key'] for row in view['open_digest']} == {'fixture.a', 'fixture.b'}
    assert view['truncated'] is False and view['omitted'] == {'pages': [], 'facts': []}


def test_a_budget_too_small_for_the_index_alone_is_a_refusal_and_not_a_shorter_answer(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work here.', 'in-progress'),
                     entries={'projects/fixture/fixture.md': work()})
    smallest = context.estimate(json.dumps(pages.page_index(value, ['fixture'])['fixture'][0],
                                           ensure_ascii=False, sort_keys=True))
    with pytest.raises(ValueError, match='page index alone'):
        context.editor_context(value, 'fixture', smallest - 1)


def test_a_selected_page_is_given_whole_or_named_as_omitted_and_the_order_is_the_callers(service):
    long_text = 'A long fact on the second page, long enough that its own row costs more than the first page. ' * 4
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'First page fact.'),
                     fact('fixture.b', 'projects/fixture/detail.md', long_text),
                     entries={'projects/fixture/fixture.md': work(), 'projects/fixture/detail.md': work()})
    room = context.editor_context(value, 'fixture', 4000)['used_tokens']
    cost = context.estimate(json.dumps(value['facts']['fixture.a'], ensure_ascii=False, sort_keys=True))
    view = context.editor_context(value, 'fixture', room + cost,
                                  ['projects/fixture/fixture.md', 'projects/fixture/detail.md'])
    assert view['selected'] == ['projects/fixture/fixture.md']
    assert [item['fact_key'] for item in view['facts']['projects/fixture/fixture.md']] == ['fixture.a']
    assert view['omitted']['pages'] == [{'target': 'projects/fixture/detail.md', 'facts': 1}]
    assert view['truncated'] is True
    assert 'projects/fixture/detail.md' not in view['facts'], 'a page was half given'
    both = context.editor_context(value, 'fixture', 4000,
                                  ['projects/fixture/detail.md', 'projects/fixture/fixture.md'])
    assert both['selected'] == ['projects/fixture/detail.md', 'projects/fixture/fixture.md']
    assert both['truncated'] is False and both['omitted'] == {'pages': [], 'facts': []}


def test_a_fact_left_out_of_the_digest_is_named_and_the_exact_read_returns_it(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work here.', 'in-progress'),
                     fact('fixture.b', 'projects/fixture/fixture.md', 'Also open, later in the order.', 'backlog'),
                     entries={'projects/fixture/fixture.md': work()})
    room = context.editor_context(value, 'fixture', 4000)
    one = context.editor_context(value, 'fixture', room['used_tokens'] - 1)
    assert one['truncated'] is True
    assert one['omitted']['facts'] == [{'fact_key': 'fixture.b', 'target': 'projects/fixture/fixture.md'}]
    assert [row['fact_key'] for row in one['open_digest']] == ['fixture.a']
    exact = pages.page_facts(value, 'projects/fixture/fixture.md')
    assert {item['fact_key'] for item in exact['facts']} == {'fixture.a', 'fixture.b'}, 'the omission is not absence'


def test_the_context_measure_is_the_one_the_wiki_budget_uses(service):
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work here.', 'in-progress'),
                     entries={'projects/fixture/fixture.md': work()})
    view = context.editor_context(value, 'fixture', 4000)
    assert view['metric'] == 'estimated tokens: UTF-8 bytes / 2.5'
    from kelmo.wiki import policy
    assert policy.TOKEN_ESTIMATE == 2.5
    rows = view['pages'] + view['open_digest'] + [item for held in view['facts'].values() for item in held]
    assert view['used_tokens'] == sum(context.estimate(json.dumps(row, ensure_ascii=False, sort_keys=True))
                                      for row in rows) + 40 * len(view['open_digest'])


def test_the_cli_answers_the_three_verbs_for_a_store_that_opted_in(service, tmp_path):
    from kelmo import cli
    from kelmo.interface import build_parser
    parser, commands = build_parser('test')
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work here.', 'in-progress'),
                     entries={'projects/fixture/fixture.md': work()})
    seed(service.root, *value['facts'].values(), entries={'projects/fixture/fixture.md': work()})
    args = parser.parse_args(['--vault', str(service.root), 'pages', '--json'])
    answer = cli.dispatch(args, commands)
    assert answer['version'] == 3 and answer['projects']['fixture'][0]['status'] == 'in-progress'
    args = parser.parse_args(['--vault', str(service.root), 'page', 'projects/fixture/fixture.md', '--json'])
    answer = cli.dispatch(args, commands)
    assert answer['page']['facts'][0]['fact_key'] == 'fixture.a'
    args = parser.parse_args(['--vault', str(service.root), 'context', '--project', 'fixture',
                              '--budget-tokens', '4000', '--select', 'projects/fixture/fixture.md', '--json'])
    answer = cli.dispatch(args, commands)
    assert answer['selected'] == ['projects/fixture/fixture.md'] and answer['truncated'] is False


def run_cli(vault, *args):
    return subprocess.run([sys.executable, '-m', 'kelmo.cli', '--vault', str(vault), *map(str, args)],
                          capture_output=True, text=True, timeout=20, check=False)


def row_cost(row):
    return context.estimate(json.dumps(row, ensure_ascii=False, sort_keys=True))


def test_the_first_selected_page_can_be_the_one_that_does_not_fit_and_the_pages_behind_it_are_still_given(service):
    """First-note overflow: an item too large for the budget is skipped and
    named, never fatal, so what sits behind it still reaches the caller."""
    long_text = 'A long fact on the first page, long enough that its own row costs more than the rest. ' * 4
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', long_text),
                     fact('fixture.b', 'projects/fixture/detail.md', 'Second page fact.'),
                     entries={'projects/fixture/fixture.md': work(), 'projects/fixture/detail.md': work()})
    index = pages.page_index(value, ['fixture'])['fixture']
    second = row_cost(value['facts']['fixture.b'])
    view = context.editor_context(value, 'fixture', sum(row_cost(row) for row in index) + second,
                                  ['projects/fixture/fixture.md', 'projects/fixture/detail.md'])
    assert view['selected'] == ['projects/fixture/detail.md']
    assert view['omitted']['pages'] == [{'target': 'projects/fixture/fixture.md', 'facts': 1}]
    assert view['truncated'] is True
    assert {item['fact_key'] for item in view['facts']['projects/fixture/detail.md']} == {'fixture.b'}
    exact = pages.page_facts(value, 'projects/fixture/fixture.md')
    assert {item['fact_key'] for item in exact['facts']} == {'fixture.a'}, 'the skipped page is still readable'


def test_complete_large_project_discovery_keeps_small_project_isolated(service):
    """Complete discovery retains every page without leaking another project's facts."""
    entries = {f'projects/big/p{i:03d}.md': work(summary='one line') for i in range(pages.INDEX_ROW_LIMIT + 1)}
    entries['projects/small/one.md'] = work(summary='one line')
    value = registry(fact('small.a', 'projects/small/one.md', 'Open work here.', 'in-progress'),
                     fact('big.a', 'projects/big/p000.md', 'Open work there.', 'in-progress'), entries=entries)
    complete = pages.page_index(value)
    assert [row['target'] for row in complete['big']] == sorted(
        target for target in entries if target.startswith('projects/big/'))
    assert len(complete['big']) == pages.INDEX_ROW_LIMIT + 1
    small = pages.page_index(value, ['small'])
    assert [row['target'] for row in small['small']] == ['projects/small/one.md']
    view = context.editor_context(value, 'small', 4000)
    assert [row['fact_key'] for row in view['open_digest']] == ['small.a']
    assert view['truncated'] is False and view['omitted'] == {'pages': [], 'facts': []}


def test_every_open_fact_of_the_project_is_accounted_for_once_and_the_call_changes_nothing(service):
    """Unchanged-source coverage: a bounded view either carries a fact,
    digests it, or names it as left out, and the registry it read is byte-for-byte what it was."""
    value = registry(*[fact(f'fixture.f{i}', f'projects/fixture/p{i}.md', f'Open work {i}.', 'in-progress')
                       for i in range(6)],
                     entries={f'projects/fixture/p{i}.md': work(summary='one line') for i in range(6)})
    before = json.dumps(value, sort_keys=True)
    room = context.editor_context(value, 'fixture', 4000)
    tight = context.editor_context(value, 'fixture', room['used_tokens'] - 1)
    carried = {fact_row['fact_key'] for held in tight['facts'].values() for fact_row in held}
    digested = {row['fact_key'] for row in tight['open_digest']}
    named = {item['fact_key'] for item in tight['omitted']['facts']}
    named |= {fact_row['fact_key'] for page in tight['omitted']['pages']
              for fact_row in value['facts'].values() if fact_row['target'] == page['target']}
    assert tight['truncated'] is True
    assert carried | digested | named == set(value['facts']), 'a fact was neither carried nor named'
    assert not (carried & digested) and not (carried & named) and not (digested & named), 'a fact was counted twice'
    assert json.dumps(value, sort_keys=True) == before, 'the bounded view changed the registry it read'


def test_the_cli_names_what_a_page_call_got_wrong_instead_of_the_generic_input_error(service):
    """docs/TARGET-STATE.md: a failure names its repair. The CLI's ValueError path prints a generic sentence,
    so a page-model refusal that stayed a ValueError would reach a caller as 'Input or local storage could
    not be processed.' with no hint of which argument was wrong."""
    value = registry(fact('fixture.a', 'projects/fixture/fixture.md', 'Open work here.', 'in-progress'),
                     entries={'projects/fixture/fixture.md': work()})
    seed(service.root, *value['facts'].values(), entries={'projects/fixture/fixture.md': work()})
    refused = run_cli(service.root, 'context', '--project', 'fixture', '--budget-tokens', '0', '--json')
    assert refused.returncode != 0
    error = json.loads(refused.stdout)['error']
    assert error['code'] == 'E_INPUT'
    assert 'positive integer' in error['message']
    escaped = run_cli(service.root, 'page', 'projects/fixture/../../secret.md', '--json')
    assert escaped.returncode != 0
    assert json.loads(escaped.stdout)['error']['code'] == 'E_INPUT'
    assert 'relative Markdown page' in json.loads(escaped.stdout)['error']['message']
    assert not (service.root / 'knowledge/wiki/secret.md').exists()
