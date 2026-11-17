# Session context

HOT supplies the memory an agent needs at session start and on later prompts.
For project creation, repository links, imports and Codex embedding, start with
[Project hubs](HUBS.md). This page describes the selection and rendering rules.

## Selection

The selected vault's `kelmo.toml` can name extra wiki pages:

```toml
[hot]
pages = ["profile.md", "rules.md"]
max_bytes = 48000

[hot.projects.shop]
roots = ["/projects/shop"]
pages = ["projects/shop/overview.md", "projects/shop/current.md"]
```

Paths are relative to the selected wiki, including one selected with `init --wiki`.
Roots are absolute or relative to the vault. `kelmo link` records a repository's
Git common directory and remotes, so its sibling worktrees need no separate root.
Selection checks the common directory, then repository remotes, then the deepest
matching root. Conflicting remote matches are an error. Path segments distinguish
`/projects/app` from `/projects/app-copy`. Missing cwd selects global memory;
a supplied cwd must be absolute. A project's `excluded_common_dirs` skips that
project during remote and root fallback. `unlink` records the exclusion; `link`
to that project clears it. An explicit common-directory mapping to another
project still takes precedence. `kelmo projects --json` includes the exclusions.

Without a `[hot]` table, selection is empty and `configured=false`. Explicit page
selection preserves order, removes duplicates and reads whole UTF-8 pages.
Mandatory pages arrive without the labels the publisher writes at the end of each
fact row: the fact id (`[f:...]`) and its sources (`[source:...]`). A status label
stays, and so does label-shaped text inside a fact or elsewhere on the page.
`graph_read` and the published page itself keep the labels.
Published knowledge is read from a pinned Git generation. Hand edits to managed
files are not a new published generation.

```sh
kelmo hot --vault /data/memory --cwd /projects/shop --json
kelmo hot --vault /data/memory --check-all --json
kelmo doctor --vault /data/memory --json
```

The preview returns context, ordered page hashes, byte count and fingerprint.
`--check-all` checks global memory and every configured project without returning
page contents. Archive embeddings and model execution are not needed to render
published HOT. Pending notes are a separate journal read.

`max_bytes` is a UTF-8 byte ceiling, not a token estimate: default 48000, maximum 1048576. Short selections stay short. A selection that exceeds this ceiling is
refused instead of silently cutting a fact or its exception.

## Generated views

```toml
[hot]
views = true
views_max_bytes = 48000
```

Project setup enables views. Each publication writes the following files beside
the wiki, normally under `knowledge/hot/`, in the same commit:

- `mandatory.md`: all mandatory pages, with frontmatter, retired fact rows and the fact and
  source labels of fact rows removed.
- `<project>.md`: the project's overview, constraints, trap index, WIP and Backlog.
- `<project>.full.md`: mandatory memory and that project's view together.

Each begins with `<!-- kelmo generation N -->`. Two project maps that would write
the same output name are refused. A combined mandatory file retains pages whose
basenames match; one cannot overwrite another. The manifest hash remains outside
these files. Explicit extra global/project HOT selections are included whole, without
stripping their frontmatter or historical rows. They remain part of the set's verified hashes.

Old-format generations keep their previous hook reader until the next publication.
Existing HOT selections without a project map still receive generated views containing
the selected pages. `mandatory-<basename>.md` aliases remain available for older instruction-file
imports; matching basenames combine their mandatory pages. Project names cannot
claim these mandatory output paths.

A project view preserves the map's prose and complete constraint facts. Other map
facts appear as a trap index: their opening clause, at most 80 characters, with a
pointer to the full map. Work pages supply WIP and Backlog rows with title,
priority, summary and link, grouped under their parent topic.

When a view exceeds `views_max_bytes`, summaries and rows are reduced in a fixed
order: Backlog summaries, Backlog counts, WIP H3 and unprioritized rows, trap
index, WIP H2 summaries, then WIP H1 summaries. Constraints and H1 titles remain.
Collapsed groups retain their counts and a link to the project page. A view still
over budget gets a marker, which `hot --check-all` and doctor report. View size
alone does not refuse an otherwise valid publication. Mandatory content is never
shortened by this process.

## Delivery

SessionStart and UserPromptSubmit rebuild the selected bundle. SessionStart always
delivers it, whether the session started, resumed, was cleared or was compacted. A
later prompt delivers it again when it changed, with a restart notice when facts may
have been removed or contradicted; an unchanged bundle is replaced by a one-line
generation marker when no instruction-file imports or blocks are selected, and by
nothing when verified imports or blocks carry it. A pending-only change delivers the
pending block alone. Pending notes remain visible until applied, with at most ten
rows displayed and a digest of the whole set. Delivery state advances only after the
hook's output is written and flushed; a record that cannot be written is reported on
stderr and the next prompt delivers again.

Claude's complete hook output is bounded to 10,000 characters. Mandatory pages
are never split; omitted context is named with a read/import instruction. Large
mandatory memory requires instruction-file imports: the hook says the pages are
not repeated, names their file, still delivers pending lines, and does not
resend the notice on later prompts of that session. Codex hooks use
`additionalContextLimit: 0`; the provider does not shorten their context.
New or changed Codex hooks still require native trust review through `/hooks`.

When a local hook omits published content, it saves a verified generation copy under
the selected state root. Shared clients keep these copies for offline use as well.
Omission notices point to a specific copy, so later updates do not change their contents
while the copy is kept (see the sweep below). Bound work and pending that cannot fit are saved
in private context files whose names identify their exact bytes, so offline pointers need
no server. Pending aliases use the same published registry as the selected HOT.

Shared clients verify a complete set from one publication before atomically
switching their `hot/current` pointer. Offline delivery retains the last complete
set for each repository/work binding and labels it stale. A known newer enrollment
revision invalidates older bindings, so offline fallback cannot restore revoked access.
Fresh verified responses can repair damaged content files when the cached metadata
still proves the generation and revision. Damaged metadata refuses automatic repair.
Shared pending notices obey the client's enrolled project boundary.
When it installs a set (a shared client on each delivery that brings one, a local
vault when it saves a copy), Kelmo removes, at most once a day, the sets older than
two days; a set installed again counts as new. It keeps the eight newest whatever
their age (the current set among them), on a shared client every set a cached
context names, so offline delivery keeps working, and every set a session's delivery
gave a path into while that session's delivery record is younger than 30 days, so
the session can still read the path. An online delivery points at the current set,
an offline one at the set its cache names, and an offline delivery whose set is gone
says that no cached generation is left. The same sweep removes a cached context no
delivery, online or from the cache, has used for 30 days: nobody has used its
binding since, and that binding then has no offline fallback until its next
delivery. It also removes a session's delivery record no delivery has written for 30
days; such a session, if it resumes, gets the whole context once, after a notice that
Kelmo has no record of what it was given and that a restart clears facts removed or
contradicted since. A saved context
reference that no delivery has shown for 30 days goes the same way; the one-line
marker of an unchanged prompt shows none.

Codex managed blocks contain the published combined view. Bound work and pending
notes travel through the hook and are not persisted into the repository's instruction file.
See [Project hubs](HUBS.md) for split versus strict
imports, owned AGENTS.md blocks, repair commands and delivery verification.
