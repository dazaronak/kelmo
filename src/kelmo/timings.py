"""Opt-in stage timings for one call: where the milliseconds of a memory call went.

Off by default. `kelmo --timings ...` or `KELMO_TIMINGS=1` writes exactly one JSON line to stderr after
the answer, so a caller that reads stdout is untouched:

  {"schema_version":1,"timings":{"command":"search","parse_ms":1.4,"engine_import_ms":198.2,
   "model_load_ms":452.0,"operation_ms":660.9,"process_ms":663.1}}

`process_ms` counts from the first line of this package's own code in the process to the moment the answer was
written. A launcher's shell and the interpreter's startup run before that first line and are outside every
number here: time the whole process and subtract `process_ms` to see them. `operation_ms` is the service work,
and `engine_import_ms` and `model_load_ms` are parts of it, so a warm process reports them as absent. A stage
that did not happen is absent, and `model_load_ms` is named only by the call that paid for the load: a process
that had already loaded the model when the call began reports no load for it, whoever loaded it. Each thread
measures its own call, so a long-lived server can report one line per request.
"""
import json
import os
import sys
import threading
import time
from contextlib import contextmanager

ENV_VAR = 'KELMO_TIMINGS'
TRUTHY = frozenset({'1', 'true', 'yes', 'on'})
_UNSET = object()
_thread_state = threading.local()


def active():
    """The call this thread is measuring, or None. A long-lived server measures one call per worker thread."""
    return getattr(_thread_state, 'call', None)


def enabled_by_environment():
    """The environment switch, so a wrapper can measure every call of a session without editing each command."""
    return os.environ.get(ENV_VAR, '').strip().lower() in TRUTHY


class Call:
    def __init__(self, command=None, *, reporting=False):
        self.command = command
        self.reporting = reporting
        self.started = time.monotonic()
        self.stages = {}
        self.model_load_paid_before = _model_load_already_paid()

    def ms(self):
        return round((time.monotonic() - self.started) * 1000, 1)

    @contextmanager
    def stage(self, name):
        began = time.monotonic()
        try:
            yield
        finally:
            self.stages[name] = round((time.monotonic() - began) * 1000, 1)

    def as_dict(self, *, model_load_ms=None):
        result = {'command': self.command}
        for name in ('parse', 'engine_import', 'operation'):
            if name in self.stages:
                result[name + '_ms'] = self.stages[name]
        if model_load_ms is not None:
            result['model_load_ms'] = model_load_ms
        result['process_ms'] = self.ms()
        return result


def start(command=None, *, reporting=False):
    """Begin measuring this thread's call; nothing is printed unless the flag or the environment asked for it."""
    call = Call(command, reporting=reporting)
    _thread_state.call = call
    return call


def set_command(command):
    call = active()
    if call is not None:
        call.command = command


def enable():
    call = active()
    if call is not None:
        call.reporting = True


def disarm():
    """Stop the call-level report: a long-lived server reports its own per-call lines instead."""
    call = active()
    if call is not None:
        call.reporting = False


@contextmanager
def stage(name):
    call = active()
    if call is None:
        yield
        return
    with call.stage(name):
        yield


def model_load_ms():
    """What the embedding model cost this process, or None while it loaded none."""
    embed = sys.modules.get('kelmo.store.memory.embed')
    if embed is None:
        try:
            import kelmo.store.memory.embed  # noqa: F401 - the number exists only once that module does.
        except ImportError:
            return None
        embed = sys.modules['kelmo.store.memory.embed']
    loaded = getattr(embed, 'model_load_ms', None)
    return loaded() if callable(loaded) else None


def _model_load_already_paid():
    """The load time this process had already paid when a call began, without importing the embedder."""
    embed = sys.modules.get('kelmo.store.memory.embed')
    loaded = getattr(embed, 'model_load_ms', None) if embed is not None else None
    return loaded() if callable(loaded) else None


def report(*, stream=None, loaded_ms=_UNSET):
    """Write the one JSON line on stderr and stop measuring. Returns the dict written, or None when nothing
    was measured, so a caller can assert the line it expects instead of reading stderr. Pass `loaded_ms=None`
    to say this call loaded no model, even though the process loaded one earlier."""
    call, _thread_state.call = active(), None
    if call is None or not call.reporting:
        return None
    if loaded_ms is not _UNSET:
        loaded = loaded_ms
    elif call.model_load_paid_before is not None:
        loaded = None  # the model was already warm when this call began, so this call loaded nothing.
    else:
        loaded = model_load_ms()
    line = call.as_dict(model_load_ms=loaded)
    print(json.dumps({'schema_version': 1, 'timings': line}, ensure_ascii=False),
          file=stream if stream is not None else sys.stderr)
    return line

