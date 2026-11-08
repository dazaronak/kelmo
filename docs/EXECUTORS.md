# Execution providers

Choose an installed provider and model explicitly:

```sh
kelmo executor --provider claude --model MODEL --effort high
kelmo executor --provider codex --model MODEL --effort high
```

The adapter reads one JSON task from stdin and writes its JSON result to stdout.
`--provider-executable /absolute/path` pins the native CLI instead of searching
PATH. Omit `--effort` to retain the provider default. Claude accepts `low`,
`medium`, `high`, `xhigh` and `max`; Codex accepts through `xhigh`.

## Custom provider or router

Point at an executable implementing the protocol below:

```sh
kelmo executor --provider custom --provider-executable /absolute/path/to/router
```

For an interpreter plus arguments, use an argv array:

```sh
kelmo executor --provider custom --command-json '["/absolute/path/to/python3","/absolute/path/to/router.py"]'
```

No shell parses these arguments. Model and effort are optional for a custom
router, so it can select an available provider using its own usage policy.
Kelmo does not require crew. A deployment can implement this interface
with crew, another runner, or its own provider client.

The input object contains `protocol: "kelmo-executor/1"`, `task`, `instructions`,
`input`, `output_schema`, `model` and `effort`. The last three may be null when
not selected. The task instructions and schema match the native adapter. A
request from a multi-round editor job also carries `conversation` (below).

The configured adapter returns one envelope:

| kind        | Payload                                                                                  |
| ----------- | ---------------------------------------------------------------------------------------- |
| `proposal`  | `result`: the model's answer object                                                      |
| `expand`    | `result: {"pages": ["projects/example/detail.md"]}`                                      |
| `reconcile` | `result: {"facts": [{"text": "Uses SQLite.", "target": "projects/example/example.md"}]}` |
| `failure`   | `error_code`: an `E_` code; optional `reason`                                            |
| `deferred`  | `reset_at`: UTC epoch seconds within the next 24 hours                                   |

### Conversations across editor rounds

The editor answers one note in rounds: the model may ask for pages, a work page
or a fact search, or Kelmo may send a refused proposal back for repair (at most
five rounds). Each round still sends the complete `input`, and a request from a
multi-round job also carries:

```json
"conversation": {"key": "9f2c...", "turn": 1,
                 "message": {"update": "Only the payload keys below changed ...",
                             "changed": {"expansions_left": 1},
                             "appended": {"pages": [{"target": "projects/example/detail.md"}]},
                             "removed": []}}
```

`key` is the same for every round of one job attempt and reveals nothing about
it. Turn 0 has `message: null`. A later turn's `message` turns the previous
round's input into this one: `changed` replaces a key, `appended` adds items to
the end of a list, `removed` deletes a key. A provider that keeps the model
conversation of turn N-1 may continue it and send only `message`; the earlier
turns are then read from the provider's cache instead of being sent again. A
provider that cannot, or whose earlier turn ran elsewhere, starts a new
conversation with the complete `input`, exactly as without the field. Ignoring
the field is always correct. The built-in `claude` and `codex` adapters ignore
it. The worker passes the field beside the payload, and `kelmo executor` takes
it back out before anything reaches the model.

A quota deferral must come from the adapter's provider status, never from a
model's answer. The CLI translates it to the worker's `quota_wait` result so
waiting does not consume the failure budget; a built-in provider adds `reason`
(`limit` or `login`, see DAEMON.md). Invalid envelopes are refused.
Execution has the configured timeout and a one MiB limit on each output stream.

This executable probe demonstrates the protocol without calling a model:

```python
#!/usr/bin/env python3
import json
import sys

request = json.load(sys.stdin)
if request.get("protocol") != "kelmo-executor/1" or request.get("task") != "probe":
    json.dump({"kind": "failure", "error_code": "E_CAPABILITY",
               "reason": "This example only handles probe."}, sys.stdout)
else:
    json.dump({"kind": "proposal", "result": {"ok": True}}, sys.stdout)
```

## Worker configuration

Select the adapter in the existing executor JSON configuration:

```json
{
  "argv": [
    "/absolute/prefix/bin/kelmo",
    "executor",
    "--provider",
    "custom",
    "--provider-executable",
    "/absolute/path/to/router"
  ],
  "timeout_seconds": 600,
  "max_attempts": 3,
  "poll_seconds": 2
}
```

Run `kelmo worker daemon --vault /path/to/vault --executor-config /path/to/executor.json`.
For local `kelmo create --wait`, the vault's selected session executor or
`.state/executor.json` supplies the same configuration. Provider commands are
owner configuration; remote note contents cannot select them.

## Usage-based crew adapter

The optional `kelmo.crew_executor` module implements this custom protocol for the
`crew` helper launcher, a separate tool that is not part of this package.
It reads `crew recommend --json` before allocation and selects only a route from
its configuration. It uses crew's exclusion decisions and the recommended
account when eligible; otherwise it chooses the eligible configured account
with the lowest reported weekly use. It never changes the configured model or
launches an unlisted account.

Example router configuration:

```json
{
  "crew_executable": "/absolute/path/to/crew",
  "state_dir": "/absolute/private/path/crew-jobs",
  "timeout_seconds": 120,
  "routes": [
    {
      "account": "account-a",
      "provider": "agy",
      "model": "YOUR_MODEL",
      "effort": "medium"
    },
    {
      "account": "account-b",
      "provider": "codex",
      "model": "YOUR_MODEL",
      "effort": "medium"
    }
  ]
}
```

Configure the custom command as:

```json
[
  "/absolute/prefix/bin/python",
  "-m",
  "kelmo.crew_executor",
  "--config",
  "/absolute/path/to/router.json"
]
```

Keep the adapter deadline below the outer executor timeout, for example 120
seconds inside a 180-second worker timeout. The adapter asks crew to stop its
own invocation on timeout or interruption. It retains the allocation receipt
so a later call can collect the same run. Completed results are reused; a
failed invocation is retained as evidence and a later retry can allocate anew.
An allocation whose response was lost is refused as `E_LAUNCH_UNCERTAIN` until
the operator inspects crew's run list. It is never blindly submitted twice.

`state_dir` contains supplied source text and receipts. Keep it private and
outside the product repository. The adapter uses isolated, tool-free policies
for Claude and Gemini and crew's read-only policy for Codex. An isolation refusal
is an error, not permission to weaken the policy or change account. Fresh usage
collection remains crew's responsibility. No models are contacted merely by
installing this module.

If the provider login belongs to another Linux account, use the fixed-wrapper
preparation in [EXECUTOR-ACCOUNT.md](EXECUTOR-ACCOUNT.md). It generates reviewed
files without installing a sudo rule or transferring credentials.
