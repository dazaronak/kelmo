# Edge: the daemon of a shared client

A shared client is a machine whose memory lives on a server (see [setup](SETUP.md#shared-clients) and
[server operations](SERVER-OPERATIONS.md)). It keeps no archive, wiki or embedding model of its own. Its
`kelmod` runs one core, the **edge core**, which owns everything the client keeps locally: the outbox of writes
and the HOT cache. The edge core reaches the server through the channel the client profile names, usually an
SSH forced command.

## What goes through it

- **MCP sessions** call the local HTTP endpoint as on a vault machine. The edge core runs each tool against the
  server. A read goes to the server, and if the server cannot be reached the read fails; it is never answered
  from the cache. A write is stored in the outbox before it is sent and is retried under the same key until
  the server settles it.
- **Hooks** run `kelmod hook`, which hands the edge core the hook payload together with what only the hook's
  own process can see (its working directory and provider).
- **A deliverer thread** carries the outbox to the server. A hook that queued something wakes it; otherwise it
  tries every 30 seconds, so a write queued while the server was unreachable goes out once it answers. One
  delivery pass holds the client for at most 5 seconds.

The edge core keeps one channel to the server open, and the server answers several requests at once on it, so
reads from different sessions overlap. The router keeps up to 32 tool calls and 4 hooks in flight and queues
the rest, round robin between sessions.

Only what touches the outbox goes one caller at a time: a write, a delivery pass, a transcript capture. A hook
a session is waiting on takes the outbox ahead of waiting tool calls, but never twice in a row past one, so a
stream of hooks cannot shut the tools out either. A write that cannot get the outbox before its deadline is
refused as busy (`E_BUSY`) and can be retried.

A server older than 0.2.0 answers one request at a time. The edge core then sends its calls one after another,
as before, and offers the newer protocol again after five minutes.

## Hooks never block a session

A hook that finds no daemon within 300 ms does not wait for one. A capture (any hook other than SessionStart
and the prompt hook) is written to `hook-spool/` in the client's state directory, synced to disk, and the hook
exits 0; the edge core
takes the spool when it starts and the deliverer replays it. A SessionStart or prompt hook is not spooled,
because its context is only useful now: it prints a notice that no pages were delivered.

When the daemon does answer, a capture is still written first as a hidden in-flight file. It is removed once the
edge core has taken the event or refused it for good, and becomes a visible spool entry when the outcome is
unknown or the refusal is worth retrying. When a replayed spool entry is not a hook frame or is refused for
good, it is renamed `*.bad` and kept for inspection. A capture gives up after 7 seconds, inside the providers'
10-second hook timeout.

The pages a SessionStart or prompt hook delivers are recorded as shown only after `kelmod hook` has printed
them for the agent, so a hook that dies before printing does not mark them as seen.

## Limits

The edge core has a soft memory budget of 300 MiB and a hard one of 512 MiB, counting its channel to the server.
The soft budget leaves room for one long session's transcript snapshot, which the outbox holds in 8 MiB pieces:
Python keeps the memory a snapshot used, so a lower soft budget would restart the core after every session stop. Supervision, stopping and the service
are the same as on a vault machine; see [architecture](ARCHITECTURE.md).
