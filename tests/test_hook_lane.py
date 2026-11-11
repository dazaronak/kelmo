"""Hooks and their acknowledgements run on the `hook` lane of both cores, never on `handler`: a session waits on
its SessionStart hook, which must not queue behind the tool calls of busy sessions."""
import importlib.util
from pathlib import Path

import pytest
from test_action_state import world_factory as world_factory  # noqa: F401

from kelmo.core import spec
from kelmo.core.state_core import StateCore

TESTS = Path(__file__).resolve().parent


def _conftest(directory):
    """The fixtures of another test directory's conftest.py, loaded under a name of its own."""
    module_spec = importlib.util.spec_from_file_location(
        f'{directory}_conftest', TESTS / directory / 'conftest.py')
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


_edge = _conftest('edge')
edge = _edge.edge
profile = _edge.profile


def lanes_of(built, name):
    return next(op['lanes'] for op in built['operations'] if op['name'] == name)


@pytest.fixture
def state_core(world_factory):
    core = StateCore(world_factory().root)
    yield core
    core.close()


def test_state_spec_serves_hooks_and_acks_on_the_hook_lane(tmp_path):
    built = spec.build(tmp_path / 'vault')
    assert 'hook' in built['cores'][0]['lanes']
    assert lanes_of(built, 'state.hook') == ['hook']
    assert lanes_of(built, 'state.ack') == ['hook']
    assert 'hook' not in lanes_of(built, 'state.tool')


def test_edge_spec_serves_hooks_and_acks_on_the_hook_lane(profile):
    built = spec.build_edge(profile.root)
    assert 'hook' in built['cores'][0]['lanes']
    assert lanes_of(built, 'edge.hook') == ['hook']
    assert lanes_of(built, 'edge.ack') == ['hook']
    assert 'hook' not in lanes_of(built, 'edge.tool')


def test_edge_core_maps_hooks_and_acks_to_the_hook_lane(edge):
    core, _client, _wakes = edge
    handlers = core.handlers()
    assert handlers['edge.hook'][1] == 'hook'
    assert handlers['edge.ack'][1] == 'hook'
    assert handlers['edge.tool'][1] == 'handler'


def test_state_core_maps_hooks_and_acks_to_the_hook_lane(state_core):
    handlers = state_core.handlers()
    assert handlers['state.hook'][1] == 'hook'
    assert handlers['state.ack'][1] == 'hook'
