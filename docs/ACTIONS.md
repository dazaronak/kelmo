# Apply an action update

An action records accepted work state, such as a push, deployment or booking.
The server administrator enrolls its identity and which clients may submit
requester statements. Ordinary note capture does not grant that authority.

With an enrolled shared-client profile:

```sh
kelmo action apply --vault /path/to/client --file /path/to/action.json --idempotency-key push-confirmation-1 --json
```

Use `--file -` for JSON on stdin. MCP exposes the same operation as
`memory_apply_action(update, idempotency_key)`. Local vaults return
`E_CAPABILITY`; local library integrations use `ActionState` with explicit
trusted bindings.

The JSON is the existing `ActionUpdate` record in
[ADOPTION-CONTRACT.md](ADOPTION-CONTRACT.md). It names the enrolled action,
subject and kind; an exact archived evidence reference; the expected current
revision (`null` only for its first state); an event order and transition.
Push and merge updates also name the enrolled repository, work revision and
remote ref. Corrections and retractions name their accepted predecessor.
Read the current action record and the source's archive receipt before preparing
the update. Do not invent revisions, identifiers or source hashes.

The server checks the authenticated client, evidence ownership and content,
action bindings, current revision and event order before publishing. An
`origin`, `requester` or role label in JSON cannot grant authority. Only an
explicit statement from the authorized person supports this route; an agent's
inference or Git command text does not become a requester statement.

Keep the same idempotency key and identical payload when retrying after an
interruption. A successful replay returns the original publication receipt.
Offline submissions remain in the durable client outbox; a queued result is
not a publication. Inspect `kelmo client status` and use `kelmo client flush`
after reconnecting. Reusing a key with different content is refused.

This interface reuses the server's `state_apply` operation. Automatic Git
producer capture still has a separate trusted-ingress contract; this command
does not replace a Git event hook.
