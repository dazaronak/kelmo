"""What one context request costs in Git processes, on the server and on the client. A SessionStart hook waits on
both, and each Git call is a process, so the counts are pinned here."""
import pytest

from test_action_state import world_factory as world_factory  # noqa: F401 - fixture
from test_shared_context import git, publish_push, repository, shared as shared  # noqa: F401 - fixture

from kelmo import client as client_module
from kelmo.client import local_binding
from kelmo.context_binding import canonical_remote
from kelmo.errors import KelmoError
from kelmo.wiki import pages
from kelmo.wiki.pages import KnowledgeReader


@pytest.fixture
def git_reads(monkeypatch):
    """Every knowledge Git read from now on, as its arguments."""
    calls = []
    read = pages._read_git

    def recording(root, *args):
        calls.append(args)
        return read(root, *args)

    monkeypatch.setattr(pages, '_read_git', recording)
    return calls


def test_a_context_request_reads_only_the_head_once_its_generation_is_cached(shared, git_reads):
    client = shared.client('mac-a')
    first = client.context(str(shared.app))
    git_reads.clear()

    second = client.context(str(shared.app))

    assert second['generation'] == first['generation'] and second['context'] == first['context']
    # The head the whole answer is built on (the committed-registry check included), and the head again after it.
    assert [args[0] for args in git_reads] == ['rev-parse'] * 2


def test_reads_inside_a_hold_share_one_head_read_and_the_next_read_asks_again(shared, git_reads):
    reader = shared.world.service.knowledge
    generation = reader.generation()
    git_reads.clear()

    with reader.holding_head():
        assert reader.generation() == generation
        assert reader.generation() == generation
        reader.mandatory(generation)
        with reader.holding_head():  # a nested hold is the same hold
            assert reader.generation() == generation

    def head_reads():
        return [args for args in git_reads if args[0] == 'rev-parse']
    assert len(head_reads()) == 1  # the page itself is read once more, as a blob

    reader.generation()
    assert len(head_reads()) == 2


def test_a_publication_during_a_hold_shows_in_the_read_after_it(shared):
    reader = KnowledgeReader(shared.world.service.config)  # not the publisher's own reader
    with reader.holding_head():
        before = reader.generation()
        publish_push(shared.world)
        assert reader.generation() == before
    assert reader.generation() != before


def test_a_registry_first_committed_during_a_request_is_judged_on_the_request_s_own_head(shared, git_reads):
    """Inside a hold the committed-registry check and the generation read use one head: the check cannot answer
    for an older commit than the one the answer is then built on."""
    reader = KnowledgeReader(shared.world.service.config)
    with reader.holding_head():
        assert reader.adopted() is True
        reader.generation()
    assert len([args for args in git_reads if 'HEAD^{commit}' in args]) == 1


def test_a_kept_answer_still_checks_which_repository_it_is_asked_about(shared, monkeypatch):
    reader = KnowledgeReader(shared.world.service.config)
    assert reader.adopted() is True
    monkeypatch.setattr(reader, 'root', reader.root / 'elsewhere')
    monkeypatch.setattr(pages, 'committed_paths',
                        lambda config: (reader.root, reader.registry_path, reader.manifest_path))

    with pytest.raises(KelmoError) as refused:
        reader.adopted()
    assert refused.value.code in ('E_CONFIG', 'E_GIT')


def test_whether_a_commit_is_adopted_is_read_from_git_once(shared, git_reads):
    reader = KnowledgeReader(shared.world.service.config)
    assert reader.adopted() is True
    git_reads.clear()

    assert reader.adopted() is True
    assert [args[0] for args in git_reads] == ['rev-parse']  # the head; the commit's answer is kept

    publish_push(shared.world)
    git_reads.clear()
    assert reader.adopted() is True
    assert len(git_reads) > 1  # a new commit is read again


def test_the_local_binding_of_a_repository_takes_four_git_calls(tmp_path, monkeypatch):
    work = repository(tmp_path / 'app', 'git@example.invalid:team/origin-app.git')
    git(work, 'remote', 'add', 'upstream', 'git@example.invalid:team/upstream-app.git')
    git(work, 'config', 'branch.main.remote', 'upstream')
    nested = work / 'src' / 'deep'
    nested.mkdir(parents=True)
    calls = []
    run = client_module._git

    def recording(cwd, *args, deadline=None):
        calls.append(args[0])
        return run(cwd, *args, deadline=deadline)

    monkeypatch.setattr(client_module, '_git', recording)

    binding = local_binding(str(nested))

    assert calls == ['rev-parse', 'symbolic-ref', 'config', 'remote']
    assert binding['repository_status'] == 'repository' and binding['branch'] == 'main'
    # The branch's own remote names the repository; every remote is offered for the server's choice.
    assert binding['repository'] == canonical_remote('git@example.invalid:team/upstream-app.git')
    assert binding['repositories'] == sorted(canonical_remote(f'git@example.invalid:team/{name}-app.git')
                                             for name in ('origin', 'upstream'))
    # Both Git directories were found from a subdirectory: the work binding was looked for, and there is none.
    assert binding['binding_status'] == 'unbound'


def test_outside_a_repository_the_local_binding_takes_one_git_call(tmp_path, monkeypatch):
    calls = []
    run = client_module._git

    def recording(cwd, *args, deadline=None):
        calls.append(args[0])
        return run(cwd, *args, deadline=deadline)

    monkeypatch.setattr(client_module, '_git', recording)

    assert local_binding(str(tmp_path))['repository_status'] == 'not-a-repository'
    assert calls == ['rev-parse']
