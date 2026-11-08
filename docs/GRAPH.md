# Wiki graph navigation

The graph describes explicit relationships in the wiki selected by the vault's
configuration. It needs no embedding model. CLI and MCP use the same Service,
archive database and validation.

## Commands and tools

| CLI                                               | MCP                                                          | Result                                                |
| ------------------------------------------------- | ------------------------------------------------------------ | ----------------------------------------------------- |
| `kelmo graph query search --query TEXT`          | `graph_query(action="search", query=TEXT)`                   | Matching pages or ambiguity candidates                |
| `kelmo graph query neighbors --id ID`            | `graph_query(action="neighbors", entity_id=ID)`              | Undirected neighbors, with distance                   |
| `kelmo graph query impact --id ID`               | `graph_query(action="impact", entity_id=ID)`                 | Direct inbound relationships                          |
| `kelmo graph query path --id ID --target TARGET` | `graph_query(action="path", entity_id=ID, target_id=TARGET)` | Deterministic shortest path                           |
| `kelmo graph query details --id ID`              | `graph_query(action="details", entity_id=ID)`                | Edge source file/line and direction                   |
| `kelmo graph query communities [--community N]`  | `graph_query(action="communities", community_id=N)`          | Derived topic groups of documents                     |
| `kelmo graph query god_nodes`                    | `graph_query(action="god_nodes")`                            | Derived high-degree hubs with degree and threshold    |
| `kelmo graph query surprises [--min-surprise S]` | `graph_query(action="surprises", min_surprise=S)`            | Explicit cross-group links ranked by structural score |
| `kelmo graph read ID`                            | `graph_read(entity_id=ID)`                                   | Full current Markdown from the selected snapshot      |
| `kelmo graph stats`                              | `graph_stats()`                                              | Counts and freshness without rebuilding               |
| `kelmo graph rebuild`                            | none (CLI only)                                              | Explicit cache rebuild                                |

Add `--vault /path/to/vault --json` to select a CLI vault and receive the versioned
JSON envelope. MCP's vault is fixed when the server starts. Query results contain
`result`, `graph` metadata and the derived-topology `analysis` label. Rebuild/stats return `counts`,
`graph` and, for a current snapshot, `analysis`.

## Identity and relationships

A page ID is its wiki-relative path without `.md`, normalized to Unicode NFC.
Case and punctuation remain significant. For example, `a/Setup` and `b/Setup`
are different pages. A rename changes the ID. Actual filename spelling remains
in `file_path`.

Exact paths take precedence. Short names and aliases name pages only and work
only when their convenience lookup has one page match; otherwise
`ambiguous_entity` lists the page candidates. A dangling link never makes a
page's short name ambiguous. Use an explicit relative wikilink such as
`[[./Setup]]` to select a sibling page. Anchors and display labels do not change
the target page. Tags and unresolved targets have separate `tag:` and
`unresolved:` namespaces and are reached by their exact IDs. A filename alone
does not exclude a document: `_template.md` and other template-named files retain
their canonical identities and links. The earlier archive skipped underscore-prefixed
templates; Kelmo keeps its existing path-discovery contract.

A link without `./` or `../` resolves in the order the publisher's link check uses: the exact page
ID, then the page of that name in the linking page's own folder, then a unique short name. So
`[[setup]]` on `x/one` names `x/setup` even when `y/setup` exists; from a folder with no `setup`
page it stays ambiguous and becomes `unresolved:setup`.

Wikilinks create `links_to`, frontmatter `parent` creates `child_of`, and tags
create `tagged_with`. Code examples and embedded links are excluded. An edge
records structure, not a claim that one software component depends on another.
Read the page before using its facts or interpreting its status.

Repeat CLI `--relation` to restrict traversed relationships; MCP accepts a list
in `relation_filter`. Names are matched without case sensitivity. An empty list
selects no relationships. Neighbor radius is at most 2 and result limit at most 200. A path longer than the result ceiling returns `path_limit_exceeded` with
the required node count. Other capped results may have additional matches.

## Derived topology

Communities, hubs and surprises are structural suggestions computed from the explicit
`links_to` and `child_of` edges between documents (tags and unresolved targets excluded). They
are never verified facts and never evidence that one page depends on another; read the pages.
Every analytics result carries `analysis` with `kind: derived_topology`, `verified: false`, the
algorithm, its seed and `snapshot`, the `source_digest` it was computed from.

They are computed when a snapshot is published, in the same savepoint as its rows, from a graph
built in identifier order: Louvain with weight `weight` and seed 0 on the documents left after
removing hubs, each document its own group when no edge remains. A hub has a degree above
`max(4, ceil(1.5 × median), 99th percentile)`. A surprise is an edge between two different
groups, neither endpoint a hub, scored `1 − Jaccard` of the endpoints' other neighbours. The same
snapshot yields the same groups. `--exclude-god-nodes` (MCP `exclude_god_nodes`) removes edges
touching hubs from neighbors, impact, path and details.

Every node, in navigation and analytics results alike, keeps the legacy fields
`community_id`, `surprise_score` and `is_god_node`. Because navigation nodes carry these derived
values too, and `--exclude-god-nodes` filters by derived hubs, every query answer (navigation
included) carries the same `analysis` label. `analysis` also names
`implementation` (analysis version and networkx release) and `assignment_sha256`, the hash of
every document's stored group, hub flag and score.

A changed wiki publishes a new snapshot and new analytics. Pass `--snapshot DIGEST` (MCP
`snapshot`) or `--assignment HASH` (MCP `assignment`) to refuse with `E_GRAPH_STALE` instead of
being answered from a newer snapshot or assignment. The binding carries `analysis_version` and
`analysis_implementation`: a snapshot published before analytics, or by another networkx release,
is stale and rebuilt on the next query, so a new implementation never answers under the earlier
assignment's hash. `graph stats` adds `analysis` counts only for a current snapshot.

Publication also rebuilds `entities_fts` from the entity rows. `kelmo admin entity-index`
repeats that repair alone, and `kelmo admin graph` rebuilds everything (see [CLI.md](CLI.md)).

## Freshness and failures

Each query/read scans the selected wiki and compares a digest of paths and bytes
with its saved graph binding. Missing or changed caches refresh before returning
results. This includes manual edits, equal-size edits, rename/delete and restored
vaults. Metadata identifies the selected vault/wiki, indexed digest and build
time. Reads return the bytes from that same scan; later edits belong to the next
request.

Stats and doctor report `absent`, `current`, `stale` or `failed` without creating
cache state. A valid empty wiki has a current empty index. A missing wiki,
malformed metadata, unsafe path or scan-time change fails explicitly. Failed
queries retain prior graph rows but do not serve them as current. Repair the
source and query again, or request an explicit rebuild. A database error while
reading already published rows is returned as `E_GRAPH_DB` without recording the
snapshot as failed; only scan, schema/metadata and publication failures are
recorded.

Publishing replaces graph rows, their FTS effects and snapshot metadata
atomically. Existing caller transactions remain uncommitted. Archive evidence,
embeddings, wiki files and Git history are unchanged by graph operations.

The default launcher retains the user's OS permissions. Graph path checks are
not an operating-system sandbox. Native client discovery and service lifecycle
acceptance remain separate release gates.
