"""Synthetic golden projections pin behavior of the preceding wiki renderer."""
import json
from pathlib import Path

import pytest

from kelmo.wiki import hot_views
from tests.test_hot_views import crowded, fact, page, registry

FIXTURE = json.loads((Path(__file__).parent / 'fixtures/hubs/wiki-parity.json').read_text())


@pytest.mark.parametrize('case', FIXTURE['cases'], ids=lambda case: case['name'])
def test_hub_preserves_wiki_projection(case):
    value = registry() if case['name'] == 'normal' else crowded(wip=30)
    actual = hot_views.render(value, FIXTURE['map'], FIXTURE['text'], case['budget'])
    assert actual == case['expected']


def test_same_basename_mandatory_pages_both_survive_in_every_full_view(tmp_path):
    value = registry()
    wiki, hot = tmp_path / 'wiki', tmp_path / 'hot'
    rendered = {}
    for directory, sentence in [('first', 'Keep the first condition.'),
                                ('second', 'Keep the second exception.')]:
        target = f'{directory}/rules.md'
        value['pages'][target] = page('Rules', 'hub', 'topic')
        value['facts'][directory] = dict(fact(target, sentence), mandatory=True)
        rendered[wiki / target] = '# Rules\n\n- ' + sentence + '\n- Retired. [status: retired]\n'
    views = hot_views.render_all(value, rendered, wiki, hot, generation=11)
    for name in ('mandatory.md', 'app.full.md', 'other.full.md'):
        assert 'Keep the first condition.' in views[hot / name]
        assert 'Keep the second exception.' in views[hot / name]
        assert 'Retired.' not in views[hot / name]


def test_generated_views_preserve_explicit_global_and_project_page_selection(tmp_path):
    value = registry()
    wiki, hot = tmp_path / 'wiki', tmp_path / 'hot'
    value['pages']['global-extra.md'] = page('Shared instructions', 'hub', 'topic')
    detail = 'projects/app/detail.md'
    value['pages'][detail] = page('Project instructions', 'topic', 'topic')
    rendered = {wiki / 'global-extra.md': '---\ncustom: keep\n---\n# Shared\n\nExtra global instruction.\n- History. [status: retired]\n',
                wiki / detail: '---\naliases: [detail]\n---\n# Detail\n\nExtra project instruction.\n- History. [status: retired]\n'}
    selection = {'pages': ['global-extra.md'], 'projects': {'app': {'pages': [detail]}}}
    views = hot_views.render_all(value, rendered, wiki, hot, generation=12, selection=selection)
    assert 'Extra global instruction.' in views[hot / 'mandatory.md']
    assert 'Extra project instruction.' in views[hot / 'app.md']
    assert 'Extra global instruction.' in views[hot / 'app.full.md']
    assert 'Extra project instruction.' in views[hot / 'app.full.md']
    assert 'Extra global instruction.' in views[hot / 'other.full.md']
    assert 'Extra project instruction.' not in views[hot / 'other.full.md']
    assert rendered[wiki / 'global-extra.md'] in views[hot / 'mandatory.md']
    assert rendered[wiki / detail] in views[hot / 'app.md']
