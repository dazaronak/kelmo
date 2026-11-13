# Install Kelmo for your user

You are a coding agent asked to install Kelmo. Follow these steps in order. Ask before each
step that changes the user's files or spends their model quota.

## 1. Check the machine

Stop and report if any check fails.

```sh
python3 --version          # 3.12 or 3.13
git config user.name       # must print a value
git config user.email      # must print a value
claude --version           # or: codex --version
```

macOS on Apple Silicon or Linux (x86_64 or aarch64) only; Intel Macs are not supported. The agent CLI must already be logged in; Kelmo never copies credentials.

## 2. Ask the user four things

Do not choose these for them.

1. **Project name**, for example the repository's name.
2. **Client:** `claude`, `codex` or `both`.
3. **Editor model and provider.** The background daemon calls this model for every note, on the
   user's subscription or API key. On the author's vault Claude Haiku cost a median of $0.13 per
   note on an API key. Ask which model they want. There is no fallback.
4. **Session capture.** `off` is the default. `auto` or `archive-only` saves session transcripts
   to the local archive, so ask explicitly.

## 3. Install the package

```sh
pipx install kelmo         # or: uv tool install kelmo
```

## 4. Preview, then show the plan

Run this from the user's repository. It writes nothing, downloads nothing and calls no model.

```sh
kelmo setup --non-interactive --disable-native-memory \
  --client CLIENT --provider PROVIDER --model MODEL \
  --project NAME --link "$PWD" --session-memory MODE --dry-run
```

Use the session capture mode the user chose for `MODE`. `auto` and `archive-only` also run model-backed
readiness checks when applied. Show the user the `files` list and `warnings`, and wait for their yes.

`--disable-native-memory` adds a line to the agent's instruction file telling it to use Kelmo
instead of the client's built-in memory. If the user wants to keep both, use
`--allow-native-memory`.

## 5. Apply

Run the same command without `--dry-run`. It creates the vault, downloads the pinned embedding
model once (87 MB), registers MCP and hooks, makes one real model call to check the editor, and
starts `kelmod`, the user-level background daemon. Changed files get verified backups.

## 6. Verify

```sh
kelmo doctor --json
kelmo projects --json
kelmo create --project NAME --text 'Kelmo was installed on this machine today.' --wait --json
kelmo pages
```

`kelmo create` costs one editor run. If it reports an operation id without a final state, follow
it with `kelmo trace OPERATION_ID --json`: `applied` means the wiki changed and `failed` names the
refusal. `archived` is a final evidence-only result only when the operation is terminal; before that
the note is still waiting for the editor.

## 7. Hand over

Tell the user to restart their agent client. Codex asks them to trust the new hooks in `/hooks`;
you cannot approve that for them. In the new session the project's hub arrives at startup.

To undo the client integration:

```sh
kelmo uninstall --home "$HOME" --executable "$(command -v kelmo)" --vault VAULT --client CLIENT --json
```

Add `--apply` after showing the preview. This disconnects the clients and keeps the `kelmod`
daemon and the data; `kelmo daemon uninstall --apply` then removes the daemon service. See
[docs/REMOVAL.md](docs/REMOVAL.md).

More options, a shared server and custom models are in [docs/SETUP.md](docs/SETUP.md).
