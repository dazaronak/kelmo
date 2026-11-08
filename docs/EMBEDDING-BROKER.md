# Use a custom embedding broker

The configured broker profile declares the complete embedding space and a local
tokenizer. Its `fds_env` names the channel variable. Provision those files and
select the profile with `kelmo models use --profile /absolute/embedding.json`.
Selection itself does not connect to a broker.

Wrap one Kelmo command with the broker's absolute stdio executable:

```sh
/absolute/runtime/bin/kelmo broker --command-json '["/absolute/custom-embedding-broker","--profile","/absolute/broker-config.json"]' -- /absolute/runtime/bin/kelmo --vault /srv/memory worker daemon --executor-config /srv/config/executor.json
```

The default channel variable is `KELMO_EMBED_FDS`; set `--fds-env` to the
profile's variable if different. The launcher passes two inherited pipe
descriptors to the wrapped command. The existing confined archive launcher
passes only that selected channel onward. The broker's stdout is reserved for
protocol replies; diagnostics go to stderr. No shell parses either argument
vector and no credentials are placed in the command by Kelmo.

The broker accepts one JSON request per line: `id`, `kind` and `texts`.
Kinds are `health`, `query` and `passage`. Replies repeat the request ID.
A health reply declares `ok: true`, the full embedding space and the input
policy: token limit, query/document prefixes and special-token additions.
Vector replies carry that space's `space_id` and the vectors. See
`BrokeredProvider` in `src/kelmo/embeddings.py` for the exact existing
contract and [ADOPTION-CONTRACT.md](ADOPTION-CONTRACT.md) for identity checks.
Input limits, tokenizer identity, vector dimensions and model-space changes
remain checked by the provider; the launcher does not relax them.

The broker and wrapped command have the same lifetime. The wrapper forwards
termination, waits briefly and reaps its owned processes. It returns the wrapped
command's exit status. It creates no daemon or connection outside that lifetime.
If the broker dies, provider calls report unavailability; it is not silently
replaced with another model. Restart the wrapped service after repairing the
broker.

Wrap `kelmo doctor` for a bounded live health check before starting a worker.
Remote endpoints, credentials and any connection window belong to the selected
broker's deployment. No remote broker is installed or activated by this package.
