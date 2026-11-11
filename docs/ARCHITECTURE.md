# Architecture

Kelmo runs as one daemon per machine, `kelmod`, written in Rust. It owns the vault (or, on a shared client, the
client root) and starts the processes that do the work as its children, called cores. In an installed Kelmo,
every agent session, hook and CLI command talks to `kelmod` instead of opening the vault's files itself.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/kelmo-runtime-dark.png">
    <img alt="One daemon per machine, kept running by systemd or launchd: agent sessions (MCP over local HTTP), hooks (kelmod hook over a local socket), CLI commands and, on a server, remote clients (SSH gate, kelmod attach) all reach kelmod, one Rust process whose router owns every call, with a limit and a queue per lane, and whose supervisor pings each core every 5 seconds, watches memory and restarts cores. kelmod starts three cores as its children: the state core (journal and background jobs, one writer and a pool of readers), the archive core (the only process that opens archive.db, running confined) and the embedding core (counts tokens and embeds with the vault's tokenizer)." src="assets/kelmo-runtime-light.png" width="900">
  </picture>
</p>

## Roles

A vault machine runs three cores:

- the **state core** (Python): the journal, the background jobs (queue pass, Dreaming, backfill, graph rebuild),
  one writer thread and a pool of readers;
- the **archive core** (Python): the only process that opens `archive.db`. Its worker runs confined
  (`sandbox-exec` on macOS, bubblewrap on Linux);
- the **embedding core**: `kelmod embed`, which counts tokens and computes embeddings with the vault's tokenizer.

A shared client machine (see [edge](EDGE.md)) runs one **edge core** that owns the client root's outbox and
caches and reaches the authority through the profile's channel.

## How clients reach it

- **MCP sessions** use Streamable HTTP on `127.0.0.1`, at the port setup chose (47821 unless told otherwise).
  Each request carries a bearer token from a file only this user can read. The client configuration holds no
  token: Claude Code and Codex run `kelmod http-headers`, which hands the token out only while a live daemon of
  this user answers on that port.
- **Hooks** run `kelmod hook ACTION`, which talks to the daemon's local socket.
- **CLI commands** go through the daemon when the installed `kelmo` launcher has `kelmod` beside it.
- **Remote clients of a server** connect over an SSH forced command (`serve-client`), which becomes
  `kelmod attach` on the server (see [SSH gate](SSH-GATE.md)).

The local socket lives in a short runtime directory (a socket path is limited to 103 bytes on macOS and 107 on
Linux), and
`kelmod` serves only peers with its own uid.

## The router

One task inside `kelmod` owns every call in flight. Each operation belongs to a lane of one core, and a lane has
a limit of calls in flight, a queue length and an order: first in first out, or round robin between clients
(each MCP session counts as its own client), so one busy session cannot starve the others. Hooks have their own
lane, so a session's start hook does not wait in the same queue as other sessions' searches.

| Core      | Lanes (in flight / queued)                                      |
| --------- | --------------------------------------------------------------- |
| state     | handler 32/512, hook 4/256, read 4/512, write 8/512, admin 4/64 |
| archive   | write 1/512, read 4/512                                         |
| embedding | count 8/1024, query 2/256, passage 2/256                        |
| edge      | handler 8/512, hook 4/256                                       |

A call that finds its queue full is refused at once with `E_OVERLOADED`, retryable after 100 ms. Calls carry
deadlines and can be cancelled. When a core restarts during a call, a read or a write that carries an
idempotency key is sent again once; any other call is answered `E_CORE_RESTARTED`, not retryable, because its
effect is unknown.

## Supervision

`kelmod` pings every core every 5 seconds and restarts one that misses three pings. It samples each core's
memory every 10 seconds against a soft and a hard budget:

| Core      | Soft / hard budget |
| --------- | ------------------ |
| state     | 1536 / 3072 MiB    |
| archive   | 768 / 1536 MiB     |
| embedding | 450 / 900 MiB      |
| edge      | 150 / 512 MiB      |

A core over its hard budget is killed and started again. A core over its soft budget is asked to drop its
caches; if it is still over at the next sample, it is restarted once it has no call in flight. More than five
restarts within a minute end `kelmod` with exit code 70, and the service manager starts it again.

Two locks keep ownership single: `daemon.lock` allows one `kelmod` per vault, and `state-core.lock` allows one
writing state core, even while a crashed daemon's old core is still finishing.

## Stopping

On SIGTERM `kelmod` stops taking calls: a new call gets `E_UNAVAILABLE`, retryable after one second. Calls already
admitted finish, for up to 10 seconds. Then every core is told to stop and gets up to 20 seconds to finish its
own work (`drain_timeout`, at most 25 seconds); a core still running after that is killed. The HTTP endpoint
closes only after the calls have drained, so their replies still reach the clients. The whole stop fits the
40-second timeout of both service managers.

## Service managers

On Linux the daemon is a systemd user service (`kelmod.service`):

- `Type=notify`: `kelmod` reports ready once every core is ready.
- `WatchdogSec=30`: `kelmod` sends a watchdog ping only while its router keeps handling events, so a stuck
  router, not only a stuck process, gets the daemon restarted.
- `KillMode=mixed`: a stop signals `kelmod` alone, which stops its cores itself; whatever outlives it is killed.
- `OOMPolicy=continue`: a core the kernel kills at the memory limit is restarted by `kelmod`, and the other
  sessions keep their daemon.
- `MemoryHigh` and `MemoryMax`: the cores' soft and hard budgets added up, plus 64 MiB.
- `Restart=on-failure`, except exit code 78, which means a spec the daemon cannot serve.

On macOS the daemon is a launchd agent (`org.kelmo.daemon`) restarted on failure. launchd has no watchdog, so
`kelmod` ends itself with exit code 70 when its router has handled no event for 60 seconds.

At start `kelmod` raises its open-file limit to the hard limit, at most 10240: service managers start daemons
with a low limit (256 under launchd, 1024 under systemd), and every connection holds a descriptor. The cores
start with the limit `kelmod` was given. The HTTP
endpoint accepts up to 1280 connections and 512 sessions; an MCP session uses two connections. A request without
a valid token is refused and its connection closed.

## Without a service

When no daemon runs for a vault, the CLI starts a temporary one. It ends 2 seconds after its last client leaves,
or 10 seconds after it started if no client came. A vault with an installed service never gets a temporary
daemon; the CLI asks you to start the service instead.

See [daemon](DAEMON.md) for the background jobs, [setup](SETUP.md) for installing the service and
[protocols](PROTOCOLS.md) for the wire formats.
