"""Page status derived from facts: a page is as open as its most open fact."""
from kelmo.wiki import engine, pages
from test_editor_context import fact, registry, topic, work


def test_page_status_is_derived_from_the_facts_and_a_page_closes_with_its_last_open_fact():
    target, parent = 'projects/fixture/fixture.md', 'projects/fixture/fixture-topic.md'
    value = registry(fact('fixture.fact', target, 'A durable fact.', role='state'),
                     entries={parent: dict(topic(), title='Fixture topics'), target: work(parent=parent)})
    facts = value['facts']
    assert pages.page_status(target, facts) == 'active'
    assert pages.hot_section(target, value['pages'][target], facts) is None
    facts['fixture.fact']['status'] = 'in-progress'
    assert pages.page_status(target, facts) == 'in-progress'
    assert pages.hot_section(target, value['pages'][target], facts) == 'WIP'
    facts['fixture.other'] = dict(facts['fixture.fact'], fact_key='fixture.other', text='Waiting.', status='backlog',
                                  id=engine.digest('fixture.other')[:12], revision='c' * 64)
    assert pages.hot_section(target, value['pages'][target], facts) == 'WIP'
    del facts['fixture.other']
    facts['fixture.fact']['status'] = 'shipped'
    assert pages.page_status(target, facts) == 'shipped'
    assert pages.hot_section(target, value['pages'][target], facts) is None
    facts['fixture.topicfact'] = dict(facts['fixture.fact'], fact_key='fixture.topicfact', target=parent,
                                      status='active', id=engine.digest('fixture.topicfact')[:12])
    assert pages.hot_section(parent, value['pages'][parent], facts) is None


def test_an_older_registry_format_is_refused_with_its_version_and_the_way_forward():
    import pytest
    from kelmo.errors import KelmoError
    for version in (1, 2, '3', None):
        with pytest.raises(KelmoError) as raised:
            engine.validate_registry({'version': version, 'facts': {}, 'notes': {}})
        assert raised.value.code == 'E_CAPABILITY'
        assert isinstance(raised.value, ValueError)
        assert 'version ' + repr(version) in str(raised.value) and 'kelmo create' in str(raised.value)
