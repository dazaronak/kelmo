# Persistent local MCP

Register the installed command as:

```sh
/absolute/runtime/bin/kelmo --vault /absolute/client mcp --reconnect
```

The client directory selects the shared server and its channel. Kelmo's
shared client reconnects that channel after a remote failure. The optional
`--reconnect` supervisor also keeps the local MCP stdio process alive if its
backend process exits. It starts the same runtime again on the next request
and replays only the accepted protocol opening.

An interrupted write returns an unknown-outcome error and is never resent by the
supervisor: inspect its operation receipt or retry with the same idempotency key.
An interrupted read (a listing, `ping`, or one of the read-only tools
`memory_search`, `memory_get`, `memory_history`, `memory_list`, `memory_stats`,
`memory_status`, `memory_trace`, `memory_doctor`, `graph_query`, `graph_read`,
`graph_stats`) is sent once more to the replacement backend, because asking again
changes nothing; a second loss is reported as an error. A newly received request is
sent only after the replacement backend accepts the opening. Failed reconnection
leaves the supervisor available for a later request; it does not reconnect in an
idle loop.

A request the client cancels gets no answer from the supervisor, even when the
backend still sends one: Claude Code treats an answer to an ID it has dropped as a
broken connection and restarts the server.

Use the native tool names, including `memory_create`, for every integration.
Do not place a remote `serve-client` command directly in an MCP registration:
that command speaks the Kelmo client protocol, not MCP.

Changing registration requires reconnecting or restarting the host application.
This command does not alter profiles, enable services, or open a background
tunnel. Closing the MCP client's stdin terminates the backend and its owned
process group. Backend children cannot keep running after a supervisor shutdown
or replacement merely because their parent has exited.

## Switch existing profiles

Kelmo does not edit existing client profiles. To move a Claude Code or Codex profile to the shared client, back up
its configuration file, replace the old MCP entry by hand with the registration above, keep every unrelated
server and setting, and restart the client. Then check native tool discovery and a bounded read:

```sh
/absolute/runtime/bin/kelmo --vault /absolute/client client status --probe --json
```

`--probe` opens the configured channel, verifies the pinned authority and reports client identity, writer epoch
and fencing without flushing queued writes. A plain `client status` inspects only local state and proves no
remote availability. Use a stable idempotency key if exercising a write. To undo the switch, restore the backup
and restart the client.
