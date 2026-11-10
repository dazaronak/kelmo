"""Which installed-CLI calls go to the daemon, and which callers the state core starts a job for."""
import pytest
from state_core_support import profile_path
from test_action_state import world_factory as world_factory  # noqa: F401
from test_shared_context import shared as shared  # noqa: F401

from kelmo.cli import routed
from kelmo.core.context import Context, Origin
from kelmo.errors import KelmoError
from kelmo.interface import build_parser


def parsed(argv):
    parser, _ = build_parser('test')
    return parser.parse_args(argv)


@pytest.mark.parametrize('argv', [['worker', 'run'], ['dream', 'run'], ['dream', 'apply'],
                                  ['add', '--file', 'note.md']])
def test_daemon_commands_are_sent_to_the_daemon(argv):
    assert routed(parsed(argv))


@pytest.mark.parametrize('argv', [['worker', 'daemon'], ['dream', 'status'], ['admin', 'count'],
                                  ['worker', 'run', '--server-profile', '/tmp/profile.json'],
                                  ['dream', 'run', '--server-profile', '/tmp/profile.json']])
def test_other_calls_run_in_the_installed_cli(argv):
    assert not routed(parsed(argv))


def test_job_run_with_an_unknown_kind_is_refused_as_invalid_input(world_factory, core_factory):
    core = core_factory(world_factory().root)
    with pytest.raises(KelmoError) as refused:
        core.handle_job_run(Context.local('job-kind', 60), {'job': 'compact'})
    assert refused.value.code == 'E_INPUT'


def test_job_run_from_an_enrolled_origin_is_forbidden(shared, core_factory):
    core = core_factory(shared.world.root, profile_path=profile_path(shared))
    context = Context.local('job-origin', 60, origin=Origin('mac-a'))
    with pytest.raises(KelmoError) as refused:
        core.handle_job_run(context, {'job': 'worker'})
    assert refused.value.code == 'E_FORBIDDEN'
