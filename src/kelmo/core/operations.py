"""The operation table of kelmo/2: every op a core serves, the lanes it may run on, whether it reads or
writes, and how the daemon may send it again after the core restarted (its replay class).

Replay classes, the only rule `kelmod` follows when it re-dispatches a call that entered through the gateway:
  read   sent again once
  keyed  sent again once under the same key; the request ledger makes it at-most-once
  never  reported as unsettled and retryable by the caller's own rule

`access` is a claim that phase 3 proves: an op marked `read` must run on a read-only journal.
"""
import inspect
from dataclasses import dataclass

READ, WRITE = 'read', 'write'
REPLAY_READ, REPLAY_KEYED, REPLAY_NEVER = 'read', 'keyed', 'never'


@dataclass(frozen=True)
class Operation:
    name: str
    lanes: tuple
    access: str
    replay: str
    arguments: dict  # name -> {'types': [...], 'required': bool}

    def to_dict(self):
        return {'name': self.name, 'lanes': list(self.lanes), 'access': self.access, 'replay': self.replay,
                'arguments': self.arguments}


_PY_TYPES = {str: 'string', int: 'integer', bool: 'boolean', list: 'array', dict: 'object',
             type(None): 'null', float: 'number'}


def _client_arguments(spec):
    return {name: {'types': sorted(_PY_TYPES[t] for t in types), 'required': required}
            for name, (types, required) in sorted(spec.items())}


# Client reads that write to the journal all the same, found by running every read on a read-only journal:
# reconcile issues a receipt. They run on the writer; the client protocol still calls them reads (no key).
JOURNAL_WRITING_READS = frozenset({'reconcile'})


def _state_operations():
    from ..server import OPERATIONS
    table = []
    for name, (mutating, spec) in sorted(OPERATIONS.items()):
        if name in JOURNAL_WRITING_READS:
            table.append(Operation('state.' + name, ('write',), WRITE, REPLAY_NEVER, _client_arguments(spec)))
        elif mutating is None:  # compat: a tool call whose effect depends on the tool
            table.append(Operation('state.' + name, ('handler',), WRITE, REPLAY_KEYED, _client_arguments(spec)))
        elif mutating:
            table.append(Operation('state.' + name, ('write',), WRITE, REPLAY_KEYED, _client_arguments(spec)))
        else:
            table.append(Operation('state.' + name, ('read',), READ, REPLAY_READ, _client_arguments(spec)))
    any_object = {'types': ['object'], 'required': True}
    table += [
        # A kelmo-client/1 request as a client sends it, run by ClientSession._dispatch; its key is the request key.
        Operation('state.client_request', ('handler', 'read', 'write'), WRITE, REPLAY_KEYED,
                  {'request': any_object}),
        # One MCP tool call: {tool, arguments}. Tools mix reads and keyless writes, so never replayed.
        Operation('state.tool', ('handler', 'read', 'write'), WRITE, REPLAY_NEVER,
                  {'tool': {'types': ['string'], 'required': True}, 'arguments': any_object}),
        Operation('state.hook', ('hook',), WRITE, REPLAY_NEVER,
                  {'action': {'types': ['string'], 'required': True}, 'payload': any_object,
                   'context': any_object}),
        # One installed-CLI call for a command the daemon runs (cli.DAEMON_COMMANDS), with its source
        # files and standard input as content.
        Operation('state.cli', ('handler',), WRITE, REPLAY_NEVER,
                  {'argv': {'types': ['array'], 'required': True},
                   'sources': {'types': ['object'], 'required': False},
                   'stdin': {'types': ['string'], 'required': False}}),
        Operation('state.job_run', ('admin',), WRITE, REPLAY_NEVER,
                  {'job': {'types': ['string'], 'required': True},
                   'options': {'types': ['object'], 'required': False}}),
    ]
    return table


# Archive writes and their replay class. A write without a key is never sent again: its effect may have
# happened (the rule ArchiveProcess keeps today).
_ARCHIVE_WRITES = {'capture': REPLAY_KEYED, 'hide': REPLAY_KEYED, 'update': REPLAY_NEVER,
                   'supersede': REPLAY_NEVER, 'graph_publish': REPLAY_NEVER, 'bind_legacy_receipt': REPLAY_NEVER}


def _archive_operations():
    from ..server import ArchiveWorker
    table = []
    for attribute, method in sorted(vars(ArchiveWorker).items()):
        if not attribute.startswith('op_') or attribute == 'op_close':
            continue
        name = attribute[3:]
        parameters = list(inspect.signature(method).parameters.values())[1:]
        arguments = {p.name: {'types': ['any'], 'required': p.default is inspect.Parameter.empty}
                     for p in parameters}
        if name in _ARCHIVE_WRITES:
            table.append(Operation('archive.' + name, ('write',), WRITE, _ARCHIVE_WRITES[name], arguments))
        else:
            table.append(Operation('archive.' + name, ('read',), READ, REPLAY_READ, arguments))
    return table


def _string(required=True):
    return {'types': ['string'], 'required': required}


def _fixed_operations():
    texts = {'types': ['array'], 'required': True}
    tool = {'tool': _string(), 'arguments': {'types': ['object'], 'required': True}}
    hook = {'action': _string(), 'payload': {'types': ['object'], 'required': True},
            'context': {'types': ['object'], 'required': True}}
    return [
        Operation('embed.describe', ('count',), READ, REPLAY_READ, {}),
        Operation('embed.count', ('count',), READ, REPLAY_READ,
                  {'texts': texts, 'prefix': _string(required=False)}),
        Operation('embed.embed', ('query', 'passage'), READ, REPLAY_READ,
                  {'purpose': _string(), 'texts': texts}),
        Operation('edge.tool', ('handler', 'read', 'write'), WRITE, REPLAY_NEVER, tool),
        Operation('edge.hook', ('hook',), WRITE, REPLAY_NEVER, hook),
        Operation('edge.ack', ('hook',), WRITE, REPLAY_NEVER, {'token': _string()}),
        Operation('state.ack', ('hook',), WRITE, REPLAY_NEVER, {'token': _string()}),
        Operation('edge.cli', ('handler',), WRITE, REPLAY_NEVER,
                  {'argv': {'types': ['array'], 'required': True},
                   'sources': {'types': ['object'], 'required': False},
                   'stdin': {'types': ['string'], 'required': False}}),
        Operation('uplink.call', ('call',), WRITE, REPLAY_NEVER,
                  {'request': {'types': ['object'], 'required': True}}),
    ]


ADMIN = ('ping', 'stop', 'trim', 'status', 'drain', 'refresh')
CORES = ('state', 'archive', 'embed', 'edge', 'uplink')


def _admin_operations():
    return [Operation(f'admin.{name}', ('admin',), READ if name in ('ping', 'status') else WRITE, REPLAY_NEVER, {})
            for name in ADMIN]


def table():
    """Every operation, sorted by name."""
    operations = _state_operations() + _archive_operations() + _fixed_operations() + _admin_operations()
    return sorted(operations, key=lambda op: op.name)


def by_name():
    return {op.name: op for op in table()}
