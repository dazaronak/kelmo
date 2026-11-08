"""Explicit registration of existing unregistered wiki files through the one publisher."""
import pytest

from test_editor_scoping import DRAFT, FEATURE, HUB, UNMANAGED, Scripted, build_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    yield from build_world(tmp_path, monkeypatch)


def test_preview_writes_nothing_and_apply_registers_the_committed_file(world):
    from kelmo.wiki import engine
    before = engine.git(world.root, 'rev-parse', 'HEAD')
    preview = world.service.register([DRAFT[:-3]])
    entry = preview['entries'][DRAFT]
    assert preview['applied'] is False and entry['layout'] == 'legacy' and entry['title'] == 'Draft'
    assert (entry['page_id'], entry['kind'], entry['category'], entry['links']) == (
        'projects-app-draft', 'topic', 'projects', [])
    assert DRAFT not in world.registry()['pages'] and engine.git(world.root, 'rev-parse', 'HEAD') == before

    result = world.service.register([DRAFT], apply=True)
    assert result['applied'] is True and result['registered'] == [DRAFT]
    assert world.registry()['pages'][DRAFT] == entry
    page = world.page(DRAFT)
    assert page.startswith('# Draft\n\n' + UNMANAGED) and '<!-- kb -->' in page
    assert engine.git(world.root, 'log', '-1', '--format=%s') == 'chore(knowledge): Register ' + DRAFT
    assert engine.git(world.root, 'status', '--porcelain') == ''

    again = world.service.register([DRAFT], apply=True)
    assert again == {'applied': False, 'registered': [], 'already_registered': [DRAFT], 'entries': {}}


def test_a_registered_file_takes_notes_like_any_page(world):
    from kelmo.executor import ModelOutcome
    world.service.register([DRAFT], apply=True)
    operation = world.service.create('The draft covers the export cutover.', project='app', selected=[DRAFT])
    fact = dict(fact_key='app.draft-scope', target=DRAFT, text='The draft covers the export cutover.',
                status='active', expected_revision=None)
    executor = Scripted(ModelOutcome('reconcile', {'facts': [{'text': fact['text'], 'target': DRAFT}]}),
                        lambda job: world.propose(facts=[fact]))
    assert world.service.run(operation['id'], executor=executor)['state'] == 'applied'
    assert world.fact('app.draft-scope')['target'] == DRAFT
    assert UNMANAGED in world.page(DRAFT)


def test_registration_does_not_read_through_a_symlinked_directory(world, tmp_path):
    from kelmo.errors import KelmoError
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'secret.md').write_text('# Outside title\n\nNot part of the wiki.\n')
    (world.service.config.wiki / 'projects/linked').symlink_to(outside, target_is_directory=True)
    with pytest.raises(KelmoError, match='No wiki file exists'):
        world.service.register(['projects/linked/secret.md'])


def test_registration_refuses_what_it_cannot_claim(world):
    from kelmo.errors import KelmoError
    from kelmo.wiki import engine
    assert world.service.register([FEATURE, HUB, DRAFT])['already_registered'] == [FEATURE, HUB]
    with pytest.raises(KelmoError, match='already names a page'):
        engine.registration_entry(world.service.config.wiki, dict(world.registry(), pages={
            'projects/app/other.md': dict(world.registry()['pages'][HUB], page_id='projects-app-draft')}), DRAFT)
    with pytest.raises(KelmoError, match='No wiki file exists'):
        world.service.register(['projects/app/missing.md'])
    with pytest.raises(KelmoError, match='person page'):
        engine.registration_entry(world.service.config.wiki, dict(world.registry(), pages={}), 'person.md')
    with pytest.raises(KelmoError, match='needs a summary'):
        world.service.register([DRAFT], kind='work')
    # Two files whose paths flatten to one page ID are refused together, by name.
    for name in ('projects/app/a-b.md', 'projects/app/a/b.md'):
        (world.service.config.wiki / name).parent.mkdir(parents=True, exist_ok=True)
        (world.service.config.wiki / name).write_text('# Twin\n')
    with pytest.raises(KelmoError, match='projects/app/a-b.md and projects/app/a/b.md would share') as refused:
        world.service.register(['projects/app/a-b.md', 'projects/app/a/b.md'])
    assert refused.value.code == 'E_INPUT'
    for name in ('projects/app/a-b.md', 'projects/app/a/b.md'):
        (world.service.config.wiki / name).unlink()
    (world.service.config.wiki / 'projects/app/a').rmdir()
    # An edited file is uncommitted knowledge: nothing is published, the edit stays.
    path = world.service.config.wiki / DRAFT
    path.write_text(path.read_text() + 'An uncommitted edit.\n')
    with pytest.raises(engine.DirtyKnowledge):
        world.service.register([DRAFT], apply=True)
    assert DRAFT not in world.registry()['pages'] and path.read_text().endswith('An uncommitted edit.\n')
    # A file nobody committed is never claimed either.
    untracked = world.service.config.wiki / 'projects/app/untracked.md'
    untracked.write_text('# Untracked\n')
    engine.git(world.root, 'checkout', '--', 'knowledge/wiki/' + DRAFT)
    with pytest.raises(engine.DirtyKnowledge):
        world.service.register(['projects/app/untracked.md'], apply=True)
    assert 'projects/app/untracked.md' not in world.registry()['pages']


def test_the_publisher_accepts_only_the_registration_it_was_given(world):
    from kelmo.wiki import engine
    registry = world.registry()
    entry = engine.registration_entry(world.service.config.wiki, registry, DRAFT)
    linked = dict(entry, links=['app'])
    linked['revision'] = engine.entity_revision(linked)
    with pytest.raises(engine.OwnershipConflict, match='unlinked legacy page'):
        engine.register_pages(world.root, {DRAFT: linked})
    other = dict(entry, title='Another title')
    other['revision'] = engine.entity_revision(other)
    with pytest.raises(engine.OwnershipConflict, match='differs from its explicit page entries'):
        engine._commit_registry(world.root, engine.apply_registration(registry, {DRAFT: other}), 'x',
                                world.service.config.wiki, _registration={DRAFT: entry})
    # Without the registration the same registry change is an ordinary direct write, refused as before.
    with pytest.raises(Exception, match='created page cannot claim an existing file|direct registry writes'):
        engine._commit_registry(world.root, engine.apply_registration(registry, {DRAFT: entry}), 'x',
                                world.service.config.wiki)
    assert DRAFT not in world.registry()['pages']


def test_the_cli_previews_then_registers(world, capsys):
    import json
    from kelmo.cli import call
    root = str(world.root)
    assert call(['register', DRAFT, '--vault', root, '--json']) == 0
    assert json.loads(capsys.readouterr().out)['result']['entries'][DRAFT]['page_id'] == 'projects-app-draft'
    assert DRAFT not in world.registry()['pages']
    assert call(['register', DRAFT, '--vault', root, '--apply', '--json']) == 0
    assert json.loads(capsys.readouterr().out)['result']['registered'] == [DRAFT]


def test_a_shared_client_previews_and_registers_through_the_server(world):
    from kelmo.server import ClientSession, ServerProfile
    profile = ServerProfile(world.bindings, world.contexts(), {}, {}, False, 'in-process')
    with ClientSession(world.root, profile, 'editor') as session:
        preview = session._dispatch('register_preview', {'targets': [DRAFT]}, None)
        assert preview['result']['entries'][DRAFT]['title'] == 'Draft'
        written = session._dispatch('register', {'targets': [DRAFT]}, 'register-draft')
        assert written['ok'] and written['result']['registered'] == [DRAFT]
        assert session._dispatch('register', {'targets': [DRAFT]}, 'register-draft')['replayed']
    assert DRAFT in world.registry()['pages']
