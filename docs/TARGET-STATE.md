# Kelmo: target state and acceptance

Part 1 records the product target.
Part 2 preserves the local acceptance scenarios. The one approved amendment is
A01's no-model intake/count clause under `durable-text/1`; recall and degraded
search checks are unchanged. [ADOPTION-CONTRACT.md](ADOPTION-CONTRACT.md) states
what this release implements and what still requires controller execution.

Rules for reading this document:

- Part 2 is frozen except for A01's explicitly accepted missing-model intake/count amendment. No other scenario may be weakened, skipped or reinterpreted.
- The implementer plans the intermediate steps, writes the unit tests and chooses the order of work. None of that is prescribed here.
- The work is finished when every scenario in Part 2 passes in the implementer's environment and, separately, on the requester's machine. Unit tests written by the implementer do not finish the work.
- Everything needed is in this repository. There is no network. `docs/TECHNICAL.md` describes the current implementation, and the other files in `docs/` describe one command group each.
- Words in this document mean exactly what they say. "Exactly" means byte for byte. "Contains" means the item is present, possibly among others. "Returns" means the JSON on stdout, not a log line.

## Part 1. The finished product

### 1.1 What kelmo is

Kelmo keeps local mode usable through `kelmo` and its MCP server. Its
accepted target also supports enrolled shared clients using a restricted archive
boundary and one trusted publisher. Network/model transport is configured by the
deployment and has no private product defaults. It keeps three kinds of knowledge:

1. **Archive (COLD).** Original evidence bytes in SQLite with immutable exact versions. Same-ID updates retain replaced versions; visibility, supersession and physical deletion remain distinct.
2. **Wiki (DETAIL).** Ordinary Markdown pages in a Git repository. Short accepted facts live in managed sections of those pages. A fact enters the wiki only through a checked change, and only after its source note is in the archive. The pages also form a graph: links and parent relations between pages, derived from the Markdown and refreshed when the Markdown changes.
3. **HOT.** A small, explicitly configured set of whole wiki pages delivered to an agent at the start of a session, with a byte ceiling. Either all selected pages arrive, or none and a diagnostic.

One Service/coordinator and journal own durable processing. Local mode injects the
real archive adapter in process; shared mode substitutes an authenticated client transport.
Queued processing and retry retain original evidence and exact receipts.

### 1.2 Components

All paths are relative to the repository root. Every Python component runs on 3.12 or 3.13, with the standard library plus the dependencies pinned in `pyproject.toml` and `uv.lock`.

| Component                  | Files                                                                                                                                                                | Responsibility                                                                                                                           |
| -------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| CLI                        | `src/kelmo/interface.py`, `cli.py`, `entry.py`, `errors.py`                                                                                                    | One argument parser for every command; the JSON envelope; exit codes; vault selection.                                                   |
| Config                     | `src/kelmo/config.py`                                                                                                                                          | Reads and validates `kelmo.toml` in the vault: identity, wiki location, HOT selection, session settings.                                |
| Service and Journal        | `src/kelmo/service.py`, `journal.py`                                                                                                                           | Capture, queue, archive, apply, retry, trace, status, search. Claims and leases on operations.                                           |
| Fact checks and Resolution | `src/kelmo/fact_checks.py`, `resolution.py`                                                                                                                    | Reconciliation receipts; revision checks; durable resolution jobs that an external agent claims and answers.                             |
| Wiki engine                | `src/kelmo/wiki/engine.py`, `links.py`, `policy.py`                                                                                                            | Fact registry, managed-section rendering, link and size checks, Git transactions with crash recovery.                                    |
| Worker                     | `src/kelmo/daemon.py`, `executor.py`                                                                                                                           | Polls queued work; runs a configured executor as a bounded subprocess with JSON on stdin and stdout.                                     |
| Sessions                   | `src/kelmo/sessions.py`, `session_transcripts.py`, `session_worker.py`, `session_prompts.py`, `session_control.py`, `session_review.py`, `session_settings.py` | Immutable transcript snapshots, chunked extraction through the executor, whole-note archive, checked fact application, pre-clear review. |
| HOT and hooks              | `src/kelmo/hot.py`, `integration.py`, `install/hooks.py`                                                                                                       | Whole-page context with a byte ceiling; native lifecycle events (`SessionStart`, `Stop`, `PreCompact`, `SessionEnd`).                    |
| MCP                        | `src/kelmo/mcp.py`                                                                                                                                             | Fifteen tools over stdio around the same Service.                                                                                            |
| Archive backend            | `src/kelmo/store/memory/`, `src/kelmo/store/db.py`, `src/kelmo/store/schema.sql`, `src/kelmo/model_store.py`                                                               | SQLite plus sqlite-vec plus FTS5; the pinned embedding model imported from local files.                                                  |
| Graph                      | `src/kelmo/graph.py`, `src/kelmo/store/graph/`                                                                                                                     | Scans the selected wiki, builds entities and edges in the archive database, serves navigation and safe page reads.                       |
| Installer and lifecycle    | `install.py`, `src/kelmo/install/`, `src/kelmo/doctor.py`                                                                                                         | Runtime venv, model provisioning, client files for Claude Code and Codex, user service files, health checks, client removal.             |
| Launcher                   | `packaging/kelmo`, `packaging/gate.py`                                                                                                                              | The installed executable; runs Python with `-I -S`.                                                                                      |

### 1.3 Storage layout of a vault

A vault is one directory selected with `--vault`. Two vaults never share state.

| Path inside the vault                | Content                                                                                                                                                        |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `kelmo.toml`                        | Configuration: `schema_version`, `vault_id` (UUID), `node_label`, `writer = "local"`, optional `wiki_root`, `[hot]`, `[sessions]`.                             |
| `.state/journal.sqlite3`             | Operations, events, claims, resolution jobs, session jobs.                                                                                                     |
| `.state/sources/<sha256>.md`         | Immutable captured Markdown, named by content hash.                                                                                                            |
| `.state/` session snapshots          | Raw transcript bytes and extraction results.                                                                                                                   |
| `archive/archive.db`                     | Archive records, FTS index, vectors, graph tables.                                                                                                             |
| `.cache/embeddings/`                 | The five pinned model files (`config.json`, `model.onnx`, `special_tokens_map.json`, `tokenizer.json`, `tokenizer_config.json`), verified by size and SHA-256. |
| `knowledge/provenance/registry.json` | Accepted facts with revisions, their source note and archive IDs, session application receipts.                                                                |
| `knowledge/wiki/`                    | The Markdown wiki, a Git repository together with the vault (default layout). `init --wiki` selects a separate, empty one instead.                              |
| `.state/wiki-transaction.json`       | Recovery state of an interrupted wiki commit.                                                                                                                  |

### 1.4 Contracts

#### CLI

`kelmo <command> [--vault PATH] [--json] [--non-interactive]`. The command list is in `src/kelmo/interface.py` and in `kelmo --help`. With `--json`, stdout is exactly one JSON object:

- success: `{"schema_version": 1, "ok": true, "result": {...}}`, exit code 0;
- failure: `{"schema_version": 1, "ok": false, "error": {"code": "E_...", "message": "..."}}`, exit code 1 (2 for a usage error); some failures also carry a `result` with the operation that failed.

Error codes are stable strings starting with `E_` (examples: `E_NOT_INITIALIZED`, `E_NOT_FOUND`, `E_INPUT`, `E_FACT_CHECK`, `E_HOT`, `E_AMBIGUOUS`, `E_PROCESSING`, `E_TRANSCRIPT`). A failure never prints a Python traceback to stdout and never creates storage. Without `--json`, output is human-readable and the exit code is the same.

Operation states, in order of progress: `captured` → `queued` → `archived` → `applied`; side exits `failed`, `needs_resolution`, `needs_review`. `kelmo trace OPERATION_ID` lists the stages in order with timestamps. `kelmo status` counts operations by state and reports `facts` and `archive_records`.

#### Roles and intents

`kelmo add` captures evidence with a declared role (`collector`, `implementer`, `reviewer`, `lead`, `unknown`). `collector` and `unknown` stay `captured` and are never archived automatically. `implementer`, `reviewer` and `lead` are archived. `kelmo promote` (role `lead`) queues wiki fact changes; it requires a `wiki-changes` block and a `fact-checks` block in the note (see `docs/NOTE-CONTRACT.md`). Roles are declarations, not authentication.

#### Note contract

A promoted note is Markdown with YAML frontmatter and two fenced JSON arrays:

- `wiki-changes`: items with `fact_key`, `target` (wiki-relative page path), `text` (one line, at most 400 characters), `status` (`active`, `shipped`, `in-progress`, `backlog`, `retired`), `expected_revision` (`null` for a new fact, otherwise the current revision hash), `replaces` (list).
- `fact-checks`: one item per fact, with `verified_match` either `{"result": "not_found", "search_id": "<receipt>"}` from `kelmo reconcile`, or `{"result": "existing", "fact_key": "...", "revision": "..."}`.

A receipt is valid only against the registry snapshot it was taken from. A receipt taken before another change to the registry is rejected at capture time (`E_FACT_CHECK`); a change that becomes stale after capture produces a resolution job (see A05).

#### Executor

The worker runs the configured executor as a subprocess. Configuration is a JSON file: `{"argv": ["/absolute/program", "args"...], "timeout_seconds": N, "max_attempts": N, "poll_seconds": N}`. The program reads one JSON object on stdin and writes one JSON object on stdout. The `task` field selects the job:

| `task`                                | Input                                                                                             | Required reply                                                                                   |
| ------------------------------------- | ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `probe`                               | `{"task": "probe"}`                                                                               | `{"ok": true}`                                                                                   |
| `resolve_facts`                       | `changes`, `facts` (in scope, with revisions), `candidates`, `source`, `source_hash`, `archive_id` | `{"changes": [...]}` in the wiki-changes schema, or `{"unresolved": true}`                       |
| `extract_checkpoint`, `extract_final` | `messages` (list of `{record, role, text}`), `part`, `parts`, `omitted_pieces`, `pages`           | `{"note_markdown": "...", "candidate_facts": [{"text": "...", "target": "..."}], "skip": false}` |
| `reconcile_session`                   | `note`, `candidates`                                                                              | `{"decisions": [{"index": i, "action": "new", "fact_key": "..."}]}`                              |

The executor's success is never the receipt of a durable effect. Only the Service's validation and commit are. An executor can be a model CLI, a local model server behind a small adapter, or a deterministic script (the acceptance scenarios use a script).

#### MCP

`kelmo mcp --vault PATH` serves exactly these fifteen tools over stdio: `memory_create`, `memory_store`, `memory_update`, `memory_apply_action`, `memory_search(query_en, query_ru, ...)`, `memory_get`, `memory_history`, `memory_list`, `memory_stats`, `memory_status`, `memory_trace`, `memory_doctor`, `graph_query`, `graph_read`, `graph_stats`. Each tool returns the same `result` object as the matching CLI command. The vault is fixed when the server starts. Reconciling a fact, forgetting a record and rebuilding the graph are CLI verbs (`kelmo reconcile`, `kelmo forget`, `kelmo graph`), not tools.

#### Search

`kelmo search QUERY --limit N` keeps `{"items": [...], "search": {"mode": "hybrid" | "lexical", "degraded": bool, "reason": string | null}}`. Hybrid combines vectors and words. An unavailable model produces explicitly degraded lexical results, including empty results; storage errors propagate. Ordinary eligible intake persists text/FTS with pending vectors. Legacy supersession admission still requires indexing before it hides the target. `status` exposes `archive_contract_version` and `archive_intake_behavior` additively.

#### Graph

Page IDs are wiki-relative paths without `.md`, Unicode NFC, case and punctuation preserved. Relations: `links_to` (a wikilink), `child_of` (frontmatter `parent`), `tagged_with` (frontmatter `tags`). Each edge records the source file and line. Every query and read first compares a digest of the wiki's files with the stored graph and rebuilds if they differ, so a query never serves stale structure. `graph stats` and `doctor` report `absent`, `current`, `stale` or `failed` without rebuilding. A short name that matches more than one page is `E_AMBIGUOUS` with candidates in search results. Neighbor radius is at most 2, results at most 200.

#### HOT

`[hot]` in `kelmo.toml` lists global pages; `[hot.projects.NAME]` lists project `roots` (absolute directories, worktrees included) and `pages`. `kelmo hot --cwd DIR` and the `SessionStart` hook deliver the selected pages whole, globals first, in a block whose UTF-8 size must be at or under `max_bytes` (default 48000). One missing, invalid or oversize page fails the whole block (`E_HOT`); nothing is truncated. The deepest matching root selects the project. No `[hot]` table means empty context and `configured = false`.

#### Sessions

`[sessions]` in `kelmo.toml`: `mode` (`off`, `archive-only`, `auto`), `paused`, `checkpoint_turns` (default 30), size ceilings, and `[sessions.sources]` with the enrolled transcript directories per provider. The hooks `session-stop`, `session-checkpoint` and `session-end` copy transcript bytes into an immutable snapshot and queue a job; they call no model. `kelmo sessions run` (or the worker daemon) extracts a note through the executor, archives the whole note, then applies checked candidate facts in `auto` mode. A complete malformed JSONL record fails the capture; an incomplete last line is kept in the snapshot and reported, and a `final` job refuses to run until the transcript is complete.

### 1.5 Invariants

These hold everywhere, in every command and tool. A change that breaks one of them is wrong even if every scenario passes.

1. Captured evidence bytes and exact versions are immutable. Supersession creates a new record; a same-ID update keeps every replaced version. Forget hides without deleting. No automatic physical deletion.
2. Archive before wiki. A fact is applied to the wiki only after the note it came from is in the archive with a receipt. If the wiki step fails, the archive receipt is kept and the operation is retried, never re-archived.
3. One Service, one journal, one worker. No second scheduler, queue database or publication path is added.
4. Replay is safe. Retrying an operation reuses its source and archive receipt; it does not create a second archive record or a second wiki commit for the same change.
5. Roles are declared, not authenticated. `collector` evidence never becomes a wiki fact without an explicit promotion by a `lead`.
6. HOT delivers whole pages or nothing. No summaries, no truncation, no recursive imports, no symlinks.
7. The graph is derived. It is rebuilt from the Markdown whenever the Markdown changes; it never carries facts of its own.
8. Unmanaged Markdown is preserved. The engine edits only the managed `<!-- kb -->` sections and registry-owned frontmatter; everything else on a page is kept byte for byte.
9. Degradation is explicit. Lexical fallback, empty HOT, stale graph and pending work are reported as such; nothing silently returns less.
10. Local intake and lexical recall need no hosted service. External embedding/client adapters are explicitly configured; no request selects a network command or credentials, and no command downloads a model implicitly (`kelmo setup` downloads the default model unless a local source, a profile or `--skip-model` is given).
11. No private defaults. No model name, price, host name, account or personal path is a default in code or configuration.
12. Two vaults never share state; a vault copied to another path works at the new path.

### 1.6 Boundaries

Separate from local implementation acceptance:

- Native acceptance on a real macOS or Linux host: launchd and systemd user services, installation from `install.py` on a clean machine, real Claude Code and Codex clients delivering hook events.
- Licence and provenance clearance, public release, package publication, remote CI receipts.
- Shared clients/outboxes, configured embedding services, privacy-safe telemetry,
  exact version history and derived graph analytics are required adoption work.
  Their deployment is not activated by local acceptance.
- Automatic provider/model fallback remains a separate decision.
- Any copy of the requester's private deployment: host names, accounts, service files, sudo rules, paths.

### 1.7 How to run from a checkout

```sh
cd REPO
export PYTHONPATH=REPO/src
python3 -m kelmo.cli --help
python3 -m kelmo.cli init --vault /path/to/vault --label NAME --json --non-interactive
python3 -m kelmo.cli models import --vault /path/to/vault --from MODEL_DIR --json
python3 -m pytest tests --ignore=tests/test_runtime.py --ignore=tests/test_runtime_macos.py --ignore=tests/test_linux_runtime.py --ignore=tests/test_installed_cli.py -q
```

`REPO` is this checkout. `MODEL_DIR` is a directory holding the five pinned model files listed in section 1.3, named explicitly by whoever runs the scenarios. The four ignored test files need an installed runtime or an OS sandbox and are not part of the source suite. The environment is fixed: a missing module, tool or file is reported, never worked around in code.

## Part 2. Acceptance scenarios

Ten scenarios. Each one has preconditions, steps a person can type, and the exact observation that passes it. A scenario passes only when every "Pass when" line holds. The requester owns the scripts that run them (`tests/acceptance/`, one script per scenario); the implementer may read those scripts and may not change them.

Conventions used below:

- `V` is a fresh vault created by `kelmo init --vault V --label acceptance --json --non-interactive` followed by `kelmo models import --vault V --from MODEL_DIR --json`, unless the scenario says otherwise.
- `STUB` is the deterministic executor script shipped with the scenarios. It answers `probe` with `{"ok": true}`, `extract_*` with a fixed note and one candidate fact, `reconcile_session` with `action = "new"` for every candidate, and `resolve_facts` with a fixed proposal named in the scenario.
- Every command is run with `--json`, and "returns" refers to the parsed JSON on stdout.
- Where a scenario says "a note promoting fact K with text T", the note is built exactly as in `docs/NOTE-CONTRACT.md`: `kelmo reconcile --text T --target projects/demo/demo.md` first, then a note whose `fact-checks` carries the returned `search_id`.

### A01. Written once, found by meaning

Purpose: an agent saves what it learned at the end of a run and finds it at the start of the next run by asking a question in different words.

Preconditions: `V`.

Steps:

1. Capture 50 distinct notes with `kelmo add --vault V --file - --role implementer --wait`, each a different two-sentence English text about unrelated topics. One of them is exactly: `The espresso machine in the office kitchen needs descaling every two months.`
2. Run `kelmo search --vault V --limit 3 'barista equipment cleaning routine'`.
3. Run `kelmo search --vault V --limit 3 'descaling'`.
4. Rename `V/.cache/embeddings` to `V/.cache/embeddings.off`. Run step 3 again. Run `kelmo add --vault V --file - --role implementer --wait` with a new text.
5. Rename the directory back.

Pass when:

- Step 1: each call returns `ok = true`, `operation.state = "archived"` and a non-null `operation.archive_id`. `kelmo status` returns `archive_records = 50`.
- Step 2: `search.mode = "hybrid"`, `search.degraded = false`, and `items` contains the espresso record (same `id` as its `archive_id`). The query shares no word with the record.
- Step 3: `items` contains the espresso record.
- Step 4: the search returns `ok = true`, `search.mode = "lexical"`, `search.degraded = true`, a non-null `search.reason`, and `items` still contains the espresso record. Under `durable-text/1`, the ordinary `add` returns `ok = true`, `operation.state = "archived"` and an archive ID; `kelmo status` returns `archive_records = 51`. Text/FTS and an owning exact receipt are durable while vectors are pending.

### A02. Restart and relocation keep everything

Purpose: nothing lives only in a process; the data directory is the whole truth and can be moved.

Preconditions: `V` after A01, or a fresh `V` with at least 3 archived records and 1 applied fact (use A04).

Steps:

1. Start `kelmo mcp --vault V` as a child process speaking MCP over stdio. Call `memory_store` with `content = "Restart probe."`, `kind = "event"`, `domain = "dev"`, `project = "demo"`. Note the returned operation id and record id. Kill the process with SIGKILL.
2. Start `kelmo mcp --vault V` again. Call `memory_trace` with that operation id. Call `memory_status`. Call `memory_get` with the record id and `full = true`.
3. Stop the server. Copy the whole directory `V` to a new path `V2` (`cp -R`). Run `kelmo doctor --vault V2`, `kelmo status --vault V2`, `kelmo search --vault V2 --limit 3 'descaling'` and `kelmo graph stats --vault V2`.
4. Run `kelmo worker run --vault V2`.

Pass when:

- Step 2: `memory_trace` returns the operation with stages `captured` and `archived` present; `memory_status` returns `archive_records` one higher than before the kill, the same `facts`, and one more operation; `memory_get` returns the probe in full.
- Step 3: `doctor` returns `healthy = true`; `status` on `V2` equals `status` on `V` field by field; the search finds the espresso record; `graph stats` returns `ok = true`.
- Step 4: the worker run returns `ok = true`; it processes the restart probe to `archived`, and `kelmo status --vault V` is unchanged (the two vaults are independent).

### A03. Forgotten means not found

Purpose: a user or agent can take a record out of circulation, and the removal is visible and durable.

A forgotten record is hidden from search and list and stays on disk. Nothing ever removes it automatically; no background process, no retention rule, no cleanup on upgrade. Freeing disk space is a separate manual flow (`kelmo admin purge-plan`, `purge-authorize`, `purge`) with its own preview; it is not part of this scenario. `forget` exists only in the CLI; the MCP tool list in A09 does not change.

Preconditions: `V` with the espresso record from A01, archive id `R`.

Steps:

1. Run `kelmo forget R --vault V`.
2. Run `kelmo search --vault V --limit 10 'descaling'`, `kelmo list --vault V --limit 100`, `kelmo show R --vault V`.
3. Run `kelmo forget R --vault V` again. Run `kelmo forget 00000000-0000-0000-0000-000000000000 --vault V`.
4. Run `kelmo trace OP --vault V`, where `OP` is the operation that archived `R`.
5. Restart nothing; copy `V` to `V3` and run step 2 against `V3`.

Pass when:

- Step 1 returns `ok = true` and `result.forgotten = true` with `result.id = R`.
- Step 2: `search.items` does not contain `R`; `list.items` does not contain `R`; `show` returns `ok = false`, `error.code = "E_FORGOTTEN"`.
- Step 3: the repeated forget returns `ok = true` with `result.forgotten = false` (already forgotten) and no error; the unknown id returns `ok = false`, `error.code = "E_NOT_FOUND"`.
- Step 4: the trace still lists the original `captured`, `archived` stages and a new stage `forgotten` with a timestamp. The immutable source file under `.state/sources/` still exists, and `archive/archive.db` still holds the record's bytes.
- Step 5: the same observations hold on the copy.
- At no point does any command, worker tick or upgrade delete the record's bytes. `kelmo doctor` after the forget returns `healthy = true`.

### A04. A fact enters the wiki only through a checked change

Purpose: the end-to-end write path: evidence, receipt, archive, managed page, Git commit.

Preconditions: fresh `V`.

Steps:

1. Run `kelmo reconcile --vault V --text 'The demo project stores its archive in SQLite.' --target projects/demo/demo.md`.
2. Build a note promoting fact `demo.archive` with that text, `expected_revision = null`, `fact-checks` result `not_found` and the `search_id` from step 1. Run `kelmo promote --vault V --file NOTE --role lead --wait`.
3. Read `V/knowledge/wiki/projects/demo/demo.md`, `V/knowledge/provenance/registry.json`, and `git -C V log --oneline`.
4. Run `kelmo trace OP --vault V` with the operation id from step 2.
5. Run step 2 again with the same note file.
6. Build a second note with the same `wiki-changes` but a `search_id` that was never returned by `reconcile` (for example `0000`). Run `kelmo promote` with it.
7. Run `kelmo add --vault V --file NOTE --role collector --wait` with the note from step 2.

Pass when:

- Step 1 returns `candidates = []`, `proves_absence = false`, and a `search_id`.
- Step 2 returns `operation.state = "applied"`, `operation.archive_id` non-null.
- Step 3: the page contains a line that starts with `- The demo project stores its archive in SQLite. [f:` inside `<!-- kb -->` and `<!-- /kb -->`; the registry has a fact `demo.archive` whose `ark_id` equals `operation.archive_id`; the Git log contains one commit whose subject contains the first eight characters of the operation id.
- Step 4: the stages appear in this order: `captured`, `queued`, `archived`, `applied`.
- Step 5 returns the same `operation.id` as step 2 (idempotent), and `git log` has no new commit.
- Step 6 returns `ok = false`, `error.code = "E_FACT_CHECK"`; the page and the registry are unchanged.
- Step 7 returns `operation.state = "captured"`; `kelmo status` reports `facts = 1`; the page is unchanged. Collector evidence does not become a fact.

### A05. A concurrent change becomes a resolution job that an agent settles

Purpose: two writers touch the same fact; nothing is lost, nothing is applied blindly, and an external agent can resolve it through a durable job.

Preconditions: fresh `V`.

Steps:

1. Build note N1 promoting new fact `demo.poll` with text `The demo worker polls every two seconds.` and a fresh `search_id`. Run `kelmo promote --vault V --file N1 --role lead` (no `--wait`).
2. Build note N2 promoting new fact `demo.poll` with text `The demo worker polls every five seconds.` and a fresh `search_id`. Run `kelmo promote --vault V --file N2 --role lead --wait`.
3. Run `kelmo worker run --vault V`.
4. Run `kelmo resolution list --vault V`, then `kelmo resolution claim --vault V`.
5. Write a proposal on stdin to `kelmo resolution submit JOB --token TOKEN --vault V`: `{"changes": [{"fact_key": "demo.poll", "target": "projects/demo/demo.md", "text": "The demo worker polls every two seconds (changed from five).", "status": "active", "expected_revision": "<revision of demo.poll from the claim payload>", "replaces": []}]}`.
6. Run `kelmo trace OP1 --vault V` for N1's operation, and read the page.
7. Run `kelmo resolution claim --vault V` again.

Pass when:

- Step 1 returns `operation.state = "queued"`. Step 2 returns `operation.state = "applied"`.
- Step 3 returns N1's operation with `state = "needs_resolution"`, a non-null `archive_id`, and `error_code = "E_FACT_CHECK"`. The page still shows the five-second text.
- Step 4: `list` returns one job with `state = "pending"` and `operation_id = OP1`; `claim` returns the job with a `token` and a `payload` whose `task = "resolve_facts"`, whose `changes` are N1's changes, whose `facts["demo.poll"].text` is the five-second text with its `revision`, and whose `archive_id` equals N1's `archive_id`.
- Step 5 returns `operation.state = "applied"`.
- Step 6: the trace contains `archived` before `needs_resolution` before `applied`, and exactly one `archived` stage; the page contains the "(changed from five)" text and no longer the plain five-second text.
- Step 7 returns no claimable job (`ok = false` with an `E_` code, or `ok = true` with a null job).

### A06. The graph follows the wiki

Purpose: an agent can find a page, see what links to it, walk to related pages, and read the current text, without a model and without a manual rebuild.

Preconditions: fresh `V`; then create these files under `V/knowledge/wiki/`:

- `projects/demo/demo.md` with frontmatter `title: Demo`, `tags: [knowledge]`, body `# Demo`.
- `projects/demo/setup.md` with frontmatter `title: Setup`, `parent: projects/demo/demo.md`, body containing `See [[projects/demo/demo]].`
- `projects/other/setup.md` with frontmatter `title: Setup`, body `Other setup.`

Steps:

1. Run `kelmo graph stats --vault V`.
2. Run `kelmo graph query search --query setup --vault V`.
3. Run `kelmo graph read setup --vault V`, then `kelmo graph read projects/demo/setup --vault V`.
4. Run `kelmo graph query neighbors --id projects/demo/demo --vault V`, `kelmo graph query impact --id projects/demo/demo --vault V`, `kelmo graph query details --id projects/demo/setup --vault V`, and `kelmo graph query path --id projects/other/setup --target projects/demo/demo --vault V`.
5. Append the line `Also see [[projects/demo/missing]].` to `projects/demo/setup.md`. Run `kelmo graph stats --vault V`, then `kelmo graph query neighbors --id projects/demo/setup --vault V`, then `kelmo graph stats --vault V` again.
6. Delete `projects/other/setup.md`. Run `kelmo graph read setup --vault V`.

Pass when:

- Step 1: `graph.status = "absent"` (no index yet); `ok = true`.
- Step 2: `result.error = "ambiguous_entity"` and `result.candidates` contains both `projects/demo/setup` and `projects/other/setup` (a short name that matches two pages is reported as ambiguous, not as a list of hits); `graph.status = "current"`.
- Step 3: the short name returns `ok = false`, `error.code = "E_AMBIGUOUS"`; the full ID returns `content` equal to the file's bytes and `file_path = "projects/demo/setup.md"`.
- Step 4: `neighbors` contains `projects/demo/setup`; `impact` contains `projects/demo/setup`; `details` lists an edge with `relation = "links_to"` from `projects/demo/setup` to `projects/demo/demo` carrying `source_file = "projects/demo/setup.md"` and a positive `source_line`, and an edge `child_of`; `path` returns an empty path (the two pages are not connected).
- Step 5: the first `stats` reports `graph.status = "stale"`; `neighbors` returns `ok = true` with an entry whose `id = "unresolved:projects/demo/missing"` and `graph.status = "current"`; the second `stats` reports `current` with a `source_digest` different from step 1.
- Step 6: `read setup` now returns `ok = true` for `projects/demo/setup` (the name became unique).

### A07. HOT delivers whole pages or nothing

Purpose: an agent starting a session receives exactly the configured pages, complete, or a diagnostic.

Preconditions: `V` after A06 (the three pages exist). Create an empty directory `P` to act as a project root.

Steps:

1. Run `kelmo hot --vault V` (no `[hot]` configured yet).
2. Append to `V/kelmo.toml`:

   ```toml
   [hot]
   pages = ["projects/demo/demo.md"]
   max_bytes = 2000

   [hot.projects.demo]
   roots = ["P"]
   pages = ["projects/demo/setup.md"]
   ```

   Run `kelmo hot --vault V`, then `kelmo hot --vault V --cwd P`, then `kelmo hot --vault V --cwd P/deeper/dir` after creating that directory, then `kelmo hot --vault V --cwd /`.

3. Send `{"cwd": "P", "session_id": "s1"}` on stdin to `kelmo hook start --vault V`.
4. Change `max_bytes` to `100`. Run `kelmo hot --vault V --cwd P` and step 3 again.
5. Restore `max_bytes = 2000`. Add `"projects/demo/absent.md"` to the global `pages`. Run `kelmo hot --vault V`.

Pass when:

- Step 1 returns `configured = false`, `context = ""`, `pages = []`.
- Step 2: without `--cwd`, `pages` lists only `projects/demo/demo.md` and `context` contains that file's complete bytes; with `--cwd P` and with the deeper directory, `project = "demo"` and `pages` lists `projects/demo/demo.md` then `projects/demo/setup.md`, and `context` contains both files' complete bytes; with `--cwd /`, `project = null` and only the global page is listed. `bytes` is the UTF-8 length of `context`.
- Step 3 returns `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": CONTEXT}}` where `CONTEXT` equals the `context` from step 2 for `P`.
- Step 4: `hot` returns `ok = false`, `error.code = "E_HOT"`, and no `context`; the hook returns `additionalContext` that starts with `Kelmo HOT unavailable (E_HOT)` and contains no page text.
- Step 5 returns `ok = false` with an `E_` code and no partial context.

### A08. A conversation becomes a whole note and a checked fact

Purpose: session capture is evidence-first: the hook stores bytes, the worker extracts through the executor, the whole note is archived, and only checked facts reach the wiki.

Preconditions: fresh `V`. A directory `T` enrolled by appending to `V/kelmo.toml`:

```toml
[sessions]
mode = "auto"

[sessions.sources]
claude = ["T"]
```

An executor configuration `E` pointing at `STUB`. A transcript `T/s1.jsonl` with these three complete lines:

```json
{"type":"user","message":{"role":"user","content":"How do we store the archive?"}}
{"type":"assistant","message":{"role":"assistant","content":"In SQLite."}}
{"type":"user","message":{"role":"user","content":"Fine, keep SQLite."}}
```

Steps:

1. Send `{"session_id": "s1", "transcript_path": "T/s1.jsonl"}` on stdin to `kelmo hook session-end --provider claude --vault V`. Run `kelmo sessions list --vault V`.
2. Run `kelmo sessions run --vault V --executor-config E`.
3. Run `kelmo list --vault V --limit 5`, read `V/knowledge/wiki/projects/demo/demo.md`, and run `kelmo sessions review --vault V --provider claude --session-id s1 --transcript T/s1.jsonl`.
4. Append to `T/s1.jsonl` the bytes `{"type":"user","message":{"role":"user","content":"partial` without a newline. Run step 1 again with `session_id = "s1"`, then `kelmo sessions run --vault V --executor-config E`, then the review from step 3.
5. Set `mode = "off"` in `kelmo.toml`. Create `T/s2.jsonl` with the three lines above. Run step 1 with `session_id = "s2"`, then `kelmo sessions list --vault V`.
6. Send a payload whose `transcript_path` is outside `T` to the hook.

Pass when:

- Step 1: the hook returns `{}`; `sessions list` returns one job with `task = "final"`, `state = "pending"`, `end_record = 3`.
- Step 2 returns that job with `state = "wiki_applied"`, a non-null `archive_id` and `attempts = 1`.
- Step 3: `list.items` contains a record whose `id` equals the job's `archive_id` and whose `content` contains all three user and assistant texts (the whole note); the page contains the stub's candidate fact text inside the managed section; the review returns `capture.status = "captured"`, `processing.status` with no failures, `semantic_completeness_guaranteed = false`.
- Step 4: the hook returns `{}` and schedules no new job (the three complete records were already processed; the incomplete record waits for a later capture): the run returns `jobs = []`; the review returns `capture.partial_tail = true`, `processing.incomplete_tail = true` and `processing.status = "pending"`; `kelmo status` shows the archive record count unchanged by this step.
- Step 5: the hook returns `{}` and `sessions list` still shows exactly the one job of `s1` and none for `s2`.
- Step 6 returns `ok = false`, `error.code = "E_TRANSCRIPT"`, and no snapshot is created.

### A09. The MCP contract

Purpose: an agent that speaks only MCP gets the same memory as the CLI, with the same shapes and refusals.

Preconditions: fresh `V`; the A04 fact promoted first (that renders `projects/demo/demo.md`), then the two setup pages of A06 (`projects/demo/setup.md`, `projects/other/setup.md`) written by hand.

Steps:

1. Start `kelmo mcp --vault V` over stdio and request `tools/list`.
2. Call `memory_search` with `query_en = "archive"`, `query_ru` set to the Russian word for archive, `limit = 5`. Call it with `limit = 0`.
3. Call `memory_store` with `content = "Via MCP."`, `kind = "event"`, `domain = "dev"`, `project = "demo"`. Then call `memory_capture` with the same content, and `memory_status`.
4. Run `kelmo reconcile --vault V --target projects/demo/demo.md --text "The demo project stores its archive in SQLite."`.
5. Call `graph_query` with `action = "search"`, `query = "setup"`; call `graph_read` with `entity_id = "setup"`; call `graph_read` with `entity_id = "projects/demo/setup"`.
6. Call `memory_get` with the `archive_id` of the A04 fact and `full = true`, then with `id = "missing"`.
7. Call `memory_trace` with the A04 operation id.

Pass when:

- Step 1 returns exactly the fifteen tool names of the MCP section and no others.
- Step 2: the first call returns an object with `items` and `search` (`mode`, `degraded`, `reason`) and finds the A04 record; the second call is a tool error (limit must be between 1 and 100).
- Step 3: `memory_store` returns the record id and version 1; `memory_capture` is a tool error (it is not a tool of this server), and `memory_status` afterwards shows `facts` unchanged.
- Step 4 returns one candidate with `fact_key = "demo.archive"` and a `revision`, plus a `search_id`.
- Step 5: search reports `ambiguous_entity` with both setup pages as `candidates`; the short-name read is a tool error naming ambiguity; the full-ID read returns the file bytes.
- Step 6: the first call returns the record with its full `content`; the second call is a tool error with "not found" semantics.
- Step 7 returns stages in the order `captured`, `queued`, `archived`, `applied`.

### A10. Failure after archive is retried without duplicates

Purpose: when the wiki step fails, the evidence and its archive receipt survive, the failure is visible, and one retry finishes the job exactly once.

Preconditions: fresh `V` with the A04 fact applied.

Steps:

1. Note `kelmo status` (`archive_records = A`, Git commit count `C`).
2. Make the page directory unwritable: `chmod 555 V/knowledge/wiki/projects/demo`.
3. Build a note promoting new fact `demo.cli` with text `The demo CLI prints JSON envelopes.` and a fresh `search_id`. Run `kelmo promote --vault V --file NOTE --role lead --wait`.
4. Run `kelmo trace OP --vault V`, `kelmo status --vault V`, `kelmo doctor --vault V`, `git -C V status --porcelain`.
5. Restore permissions: `chmod 755 V/knowledge/wiki/projects/demo`. Run `kelmo queue retry OP --vault V`, then `kelmo worker run --vault V`.
6. Run `kelmo trace OP --vault V`, `kelmo status --vault V`, `git -C V log --oneline`, and read the page.

Pass when:

- Step 3 returns `ok = false`, `result.operation.state = "failed"`, `result.operation.archive_id` non-null, `result.operation.durable_stage = "archived"`.
- Step 4: the trace ends with a `failed` stage whose details name `phase = "wiki_commit"`; `status` reports `archive_records = A + 1`; `doctor` returns `ok = true` (a failed operation is not corruption); the Git working tree has no modified tracked wiki file.
- Step 5: `retry` returns the operation with `state = "queued"` and the same `archive_id`; `worker run` returns it with `state = "applied"`.
- Step 6: the trace contains an `archived` stage before `failed`, and every stage that carries an `archive_id` carries the same one (the retry records the reuse of the receipt, it never creates a second record); `archive_records = A + 1` (no second record); the Git log has exactly `C + 1` commits; the page contains the new fact line once.

### Closing the work

The scripts live in `tests/acceptance/`: `run.sh` runs `a01_recall.py` to `a10_retry.py` in order with the interpreter named by `KELMO_PYTHON` (default `python3`) and the model directory named by `KELMO_MODEL_DIR`; each script prints `PASS A0N` or `FAIL A0N: <the first failing "Pass when" sentence>` and exits 0 or 1. `stub_executor.py` is the deterministic executor `STUB`; `_lib.py` holds the shared helpers. Run it like this:

```sh
cd REPO
KELMO_MODEL_DIR=MODEL_DIR tests/acceptance/run.sh
```

The work is closed when that run prints `PASS` for each of A01 to A10 in the implementer's environment, and the maintainer reproduces the same on a second machine. Partial results are reported per scenario, with the failing "Pass when" sentence quoted.
