# Shared clients through an SSH forced command

The authenticated SSH key selects the enrolled client identity. The caller
must not choose a client id, vault path, profile, executable or shell fragment.
The installed `kelmo.ssh_gate` module accepts exactly the remote command
`kelmo-client` and starts the fixed `serve-client` process.

An administrator can use the following forced-command shape for a key, with
the actual runtime, control directory, profile and client substituted:

```text
restrict,command="/srv/kelmo-runtime/bin/python -I -m kelmo.ssh_gate --executable /srv/kelmo-runtime/bin/kelmo --vault /srv/memory-control --profile /srv/memory-profile/server.json --client-id laptop" ssh-ed25519 PUBLIC_KEY
```

Use a separate enrollment per trusted client. Keep the key entry, runtime and
server profile controlled by the administrator; the connecting client must not
be able to edit them. Disable forwarding and PTYs for these keys. If the host
already has a forced-command dispatcher, add this one fixed route to it instead
of replacing unrelated routes. Do not use `eval` or execute the requested
command as a shell string.
The key-option behavior is documented in
[OpenSSH's authorized_keys reference](https://man.openbsd.org/sshd.8#AUTHORIZED_KEYS_FILE_FORMAT).

The client channel is:

```json
["/usr/bin/ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
 "kelmo-host", "kelmo-client"]
```

Configure the shared authority with `kelmo client init`, then perform a
`client status --probe` in the approved connection window. That handshake
must report the expected authority and client. An attempt to pass a different
client id or a shell command must be refused.

Locally, register `kelmo mcp --reconnect`, not this SSH command, with the agent.
The gate speaks the internal shared-client protocol; the local process speaks
MCP and retains its stdio connection when the channel reconnects.

This module does not create keys, edit SSH configuration, start a tunnel, grant
privileges or activate a server.
