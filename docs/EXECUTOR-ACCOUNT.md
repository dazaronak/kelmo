# Provider login owned by another account

Use this only when the memory service and provider login already have different
OS owners. Keep the provider's credentials in that account. The service receives
permission to invoke one fixed wrapper with no command-line arguments; JSON task
input arrives on stdin. Neither a shell nor arbitrary provider commands are
granted through sudo.

Prepare files on a trusted machine:

```sh
python3 tools/prepare_executor_account.py --service-account memory --provider-account model --runtime /usr/local/share/kelmo/runtime --provider codex --provider-executable /home/model/.local/bin/codex --model YOUR_MODEL --effort high --output /absolute/private/executor-install
```

The generator creates the wrapper, an exact sudoers rule, executor.json and a
hash manifest. It creates no account, copies no credentials and changes no sudo
configuration. It supports a custom executable router as the provider too.

Before installation, an administrator checks:

- The runtime and wrapper are administrator-owned and not writable by the
  memory service. Their parent directories have the same ownership boundary.
- The provider executable and login belong to the selected non-root account.
  An interactive login, if needed, happens as that account.
- The generated paths and model are the intended ones. Native probes can use
  subscription capacity; the wrapper removes inherited API-key variables and
  provider-profile overrides so it uses the selected account's default login.
- `visudo -cf /absolute/private/executor-install/kelmo-executor.sudoers`
  accepts the rule. The empty quoted argument list is deliberate.

Stop the worker before replacing its executor. Back up existing wrapper and
sudoers bytes and modes, verify those backups, then install the wrapper at
`/usr/local/libexec/kelmo-executor` with root ownership/mode 0755 and the
rule at `/etc/sudoers.d/kelmo-executor` with root ownership/mode 0440.
Recheck sudoers syntax before starting the service. Keep the backups and the
generated manifest with the deployment report.

Put executor.json in the service's configuration directory and select it with
`kelmo setup --executor-config /absolute/executor.json`. Readiness invokes
the fixed wrapper with a synthetic probe. Confirm that passing any command-line
argument to the wrapper is refused, then check one scoped real source and its
publication receipt. A missing login or denied sudo rule is an executor failure,
not permission to broaden the grant.

For rollback, stop the worker, restore the saved files with their modes,
validate sudoers again and restart the previously selected configuration.
This restores the execution route; it never rolls back accepted memory writes.
