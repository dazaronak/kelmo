# Remove client integrations

Preview changes using the executable and vault binding recorded by setup:

```sh
kelmo uninstall --home /home/user --executable /runtime/bin/kelmo --vault /data/memory --client both --json
```

Add `--apply` to write the removal. `--client claude` or `--client codex` limits it
to one profile. The explicit vault does not need to exist or open successfully.

Removal covers the MCP entry, exact SessionStart/Stop hook members and generated
instruction block. It matches the executable/vault binding and current generated
contents. Claude also requires the installer ownership receipt; Codex requires its
marked MCP region. Changed or unmarked entries stop removal before writes. User
text outside the instruction region, unrelated settings and hook siblings remain.
Codex's shared hooks feature stays enabled because other hooks may use it.

This disconnects clients. It retains the runtime, the background service, vault, wiki
and backups. A running `kelmod` daemon (or, on a server, the worker) keeps processing
queued work. The response reports `background_retained=true` and `data_retained=true`,
not a complete package uninstall, and its `next_step` names `kelmo daemon uninstall
--apply`, which stops and removes the daemon service. Restart affected clients after
applying: a client that is still open keeps its connection to the daemon, or the
`kelmo mcp` process it started, until it exits.

Preview writes nothing. Apply preflights all files, uses the client installer lock,
compares observed bytes and verifies backups before atomic per-file replacement.
Concurrent changes stop the operation. Files are not updated as one transaction:
an interruption can leave some integrations removed. Resolve the conflict and rerun;
absent integrations are a no-op. Ownership receipts remain for recovery/reinstall.
Empty files and hook lists remain rather than deleting user-owned files.

Historical generated blocks that differ from this version also conflict. Inspect
them manually; removal does not guess whether a prior installer or the user changed
them. It never restores historical backups over later user edits.
