# Wiki v3 file contract

Version: `wiki-file-contract/3.0-proposal.2`. This is a design record. Registry
version 3, described here, is the format this release reads and writes; the passages
about a proposal, a baseline and v1/v2 readers record the migration it came from.
The synthetic examples do not activate a schema or migrate a corpus. The offline
file validator implements the checks; runtime admission stays with the publisher.

## Start with the pages

Read [the full person context](wiki-v3-examples/person.md), then [the current project
map](wiki-v3-examples/project-map.md). The map links to [the Sailing Schedule
topic](wiki-v3-examples/sailing-schedule.md) and [one feature box](wiki-v3-examples/feature.md).
Harbor Demo and every identifier, source, action and event in these files are
fictional. Example filenames are transport filenames; frontmatter `target`
names the proposed wiki location. Links inside their managed regions resolve
against those logical targets. The links above open the local proposal files.
No employer or personal data is used.

The map says a push is complete, while merge and deployment remain pending. The
feature explains the behavior and keeps the older, unresolved release claim
visible. A reader can find the detail without loading its history at startup.
[records.json](wiki-v3-examples/records.json) illustrates the single registry;
[manifest.json](wiki-v3-examples/manifest.json) illustrates a worker proposal,
including embedded fictional source bytes and an illustrative controller census. Neither is an accepted store.

## What exists, what is required, what this proposes

| Class             | Mechanism or requirement                                                                                                              | Evidence / consequence                                                                                                                               |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------- |
| Existing          | Registry v1/v2, fact keys and 12-hex fact IDs, optimistic revisions, note/archive receipts, opt-in pages                              | `wiki/engine.py:reduce_changes`, `validate_registry`; `wiki/pages.py:to_v2`. Opening a store does not migrate it.                                    |
| Existing          | Six page fields, role-inclusive fact revision, status derived from facts                                                              | `pages.py:ENTRY_FIELDS`, `fact_revision`, `page_status`. Work status currently has no separate action owner.                                         |
| Existing          | One `kb` managed region, surrounding text preserved by rendering; scoped Git recovery                                                 | `engine.py:render_pages`, `commit_registry`, `recover_transaction`. This is no proof of semantic preservation.                                       |
| Existing          | Complete selected pages in HOT, explicit budget failure; bounded index and exact page read                                            | `hot.py:render`, `pages.py:page_index/page_facts`, `wiki/context.py`. HOT is not a coherent multi-page generation; discovery has a 200-page ceiling. |
| Existing          | Promotion requires fact checks; stale reconciliation receipts are refused                                                             | `docs/NOTE-CONTRACT.md`. Search receipts prove a search ran, not that a claim is unique or true.                                                     |
| Agreed target     | One publisher/registry; independently bound storage roots; exact EvidenceRef; typed action state independent of semantic processing   | No new authoritative registry.                                                                    |
| Agreed target     | All person facts plus protected rules, linked source-backed narrative, unmanaged-byte preservation, coherent maps, complete discovery | No history-to-HOT requirement.                                                                                                |
| Agreed target     | Workers and Dreaming submit checked, source-pinned proposals; deterministic migration and loss accounting                             | Mechanical validation cannot certify meaning.                                                                                |
| New format choice | Registry `version: 3`, stable page IDs, two explicit regions, action and narrative maps                                               | Proposed below, for controller/requester review. Existing v1/v2 readers must refuse v3, not render it partially.                                       |

`registry.py` is absent at this baseline. The registry implementation is
`src/kelmo/wiki/engine.py`; page grammar is in `pages.py`. This proposal
extends those mechanisms rather than adding another compiler or backing store.
The adoption API names stay as they are; supplemental file fields below are
proposals, not changes to that API.

## Three delivery layers

1. Startup and compact/resume receive ALL accepted person facts, including ordinary
   non-rule facts and records without a prior mandatory flag. Every condition and
   exception remains explicit. Include retained statuses, even retired facts;
   previous archive versions of the same fact are not separate fact identities.
   Also deliver the selected repository's current project map.
2. On-demand page reads supply explanations, feature boxes, decisions, related
   links and legacy facts. Discovery enumerates every page under a pinned generation
   with stable cursor, total and explicit omissions.
3. Archive reads supply original bytes/historical versions through exact references.
   Retrieved evidence is not current-state authority.

Delivery coverage and rule edit protection are separate properties. Mandatory
delivery is the complete controller-inventoried person fact set plus any other
mandatory records. Import marks every person record mandatory=true even if absent
or false before, preserving the original value/absence in compatibility. Moving a
fact off person.md does not remove its delivery membership. New person facts join
automatically; model classification as non-rule cannot exclude them.

Protected rules additionally have protected=true and rule-policy ownership.
Ordinary person facts may have protected=false and be updated through normal
semantic promotion; they still must be delivered in full. A missing/unclassified
person record cannot be demoted to archive-only delivery.

If any required person fact, condition, exception or other mandatory record is
omitted, refuse with incomplete-delivery, missing fact_keys and required size.
No top-k or detail link substitutes for full delivery. Historical project facts
outside this set remain discoverable without automatically entering HOT; previous
versions remain evidence. Only an attributable explicit correction/removal changes
accepted person content, subject to the separate protected-rule policy.

The map includes purpose, repository bindings, current constraints, action
subject/owner/kind/destination/state, relevant completions, unresolved-state counts
and feature links. General person/task actions appear in their subject context
without Git binding. Absence from a map never reopens a completed action.
Omissions name IDs and an exact-read route. A stale/missing repository binding
refuses repository-dependent decisions, not non-Git actions. Offline context
names its last accepted generation. No completion or retirement by age.

## Identities, paths and ownership

Keep every existing `fact_key`, `id`, target, alias and relationship. New fact IDs
continue `sha256(fact_key UTF-8)[:12]`; collisions refuse publication. New page,
narrative, feature, action and event IDs are opaque stable lowercase identifiers
matching `[a-z0-9][a-z0-9._-]{2,119}`. A feature ID is its feature page's `page_id`.
An action ID is assigned by the trusted state owner and never derived from a title
or a local worktree path. Shared features use the same ID in all repository maps.

Registry `pages` stays keyed by wiki-relative `.md` target. Retain Kelmo's current
target grammar and path checks, no absolute paths, traversal, dot segments,
symlinks or arbitrary worker output paths. A publisher-assigned page ID survives
a reviewed rename. `redirects` maps an old target to that page ID; no automatic
renames or redirects during import. Parents use existing target paths, must exist,
must be acyclic, and retain the current three-level depth ceiling.

New aliases are plain NFC strings, nonempty, unique within the authority, and
cannot shadow a canonical target or redirect. The publisher owns assignment.
Legacy ambiguous aliases retain their original lists in compatibility data and
return candidates; migration must not select a winner or erase the ambiguity.
Graph path IDs remain canonical paths, with a derived path-to-page-ID mapping.
This does not silently change public graph IDs. Preserve existing literal link
resolution and source-line evidence; unresolved links remain unresolved concepts
or candidate sets, never falsely verified edges.

## Exact record shape

JSON is UTF-8. New objects use only the fields below. Optional means omitted,
not a null substitute; arrays/maps may be empty. Nullable legacy fields retain
their null values. `compatibility` is an opaque map of original fields/values and
their source locators, never an alternative authority. Unknown input fields go
there with a reported mapping or stop the affected scope.

Define `J(x)` as `json.dumps(x, ensure_ascii=False, sort_keys=True, indent=2) +
"\n"`, encoded in UTF-8. Entity `revision` is SHA-256 of J of the whole object
excluding `revision` and `previous_revision`. For facts, pages, actions and
narratives this includes ownership, roles, evidence, dependencies and compatibility
metadata. `previous_revision` is null for a new entity, otherwise the prior hash.
File hashes cover exact bytes, including line endings. Hashing does not normalize
legacy bytes. V1/v2 revision algorithms stay unchanged for those versions.

| Object            | Required fields                                                                                                                                                                                                                               | Optional fields / constraints                                                                                                                                                                                        |
| ----------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Registry          | `version: 3`, `authority_id`, `facts`, `notes`, `pages`, `actions`, `narratives`, `redirects`, `compatibility`                                                                                                                                | `session_applications`, retained exactly. Map keys equal entity keys/IDs. This is the existing accepted registry at its configured path.                                                                             |
| Page              | Existing `kind`, `category`, `parent`, `title`, `summary`, `priority`, plus `page_id`, `layout`, `aliases`, `tags`, `repositories`, `links`, `revision`, `previous_revision`, `compatibility`                                                 | `feature_id` only for feature layout, equal to `page_id`; legacy summary assessment fields require the legacy-summary-assessment adapter below. `links` is a list of page IDs.                                       |
| Fact              | Existing `fact_key`, `target`, `text`, `status`, `expected_revision`, `replaces`, `id`, `note_id`, `ark_id`, `revision`, `previous_revision`, plus `owner`, `protected`, `mandatory`, `evidence_resolution`, `evidence_refs`, `compatibility` | Existing `role`, `index_target` retained when present. `expected_revision` is the stored legacy check field, not fresh authority. `owner`: `rule-policy`, `semantic-editor`, `legacy-import`.                        |
| Note receipt      | Existing `filename`, `ark_id`, plus `evidence_resolution`, `evidence_refs`, `compatibility`                                                                                                                                                   | Key remains the SHA-256 original note identity. Receipt evidence must agree with referencing facts; no rebinding to today's archive head.                                                                            |
| EvidenceRef       | `archive_id`, `version`, `content_sha256`, `source_id`                                                                                                                                                                                        | Version is an integer >=1, hash is lowercase 64-hex. All four fields must match an immutable archive version. No `latest` reference.                                                                                 |
| Narrative         | narrative_id, page_id, text, evidence_refs, depends_on, state, revision, previous_revision, compatibility                                                                                                                                     | state: current/stale. depends_on is {facts: {fact_key: revision}, actions: {action_id: revision}}. Both maps required, may be empty. Never use 12-hex display IDs or page IDs.                                       |
| Action projection | action_id, subject_kind, subject_id, kind, state, owner, accepted_event, revision, previous_revision, compatibility                                                                                                                           | Common identity is independent of Git/features. owner: state-api; state: pending/in-progress/completed/cancelled/unresolved. Kind-specific fields below; optional feature_id equals a feature subject_id.            |
| Accepted event    | event_id, event_order, occurred_at, ingested_at, transition, evidence_ref, evidence_mark, correction_of, operation_receipt_id                                                                                                                 | PRODUCER/REQUESTER attribution comes from the owning coordinator receipt/trusted binding, never payload text. correction_of is null or predecessor ID; RFC3339 UTC timestamps, authority-assigned integer event_order. |

Use existing page enums (`work/topic/hub/prose`, categories and H1/H2/H3/empty
priority), title ceiling 120 and summary ceiling 200 characters, one plain line.
Layouts are `person`, `project-map`, `topic`, `feature`, `legacy`: respectively
topic/you, hub/projects, topic/projects, work/projects, or the retained old kind
and category. Aliases, tags, repositories and links are arrays of unique
strings; repository IDs must match controller enrollment, and link IDs must name
registered pages. Prose pages remain fact-free. All facts have registered page targets
in v3, including non-project pages. Facts keep the existing five status values
and single-line 400-character ceiling for new edits. Legacy text over a limit is
preserved in its original snapshot and compatibility representation; it blocks
adoption of that record until a reviewed lossless mapping exists, never truncates.

Protected person rules have `owner: rule-policy`, `protected: true`, `mandatory:
true`, complete rule text and its existing status. Ordinary editor, compactor,
resolution and Dreaming operations preserve text/status/marking byte-for-byte.
Only an attributable explicit requester rule-change source, admitted by the
publisher's rule policy, may change them. A worker's `REQUESTER` string is not proof.
Unprotected factual records do not become instructions merely by being on a page.

`evidence_resolution` is `exact` or `unresolved`. Exact records have nonempty
verified references. Unresolved legacy facts/receipts have empty references and
retain original `ark_id`/note identity plus a reason in compatibility. Never invent
a version, hash, source ID, validity date or receipt. They can be shown as labelled
legacy evidence, not accepted as a new completion or source for a current claim.
Unresolved person facts and other mandatory records remain delivered while evidence repair is open.

## Markdown regions and sections

New page frontmatter contains exactly `wiki_contract`, `page_id`, `target`,
`kind`, `category`, `parent`, `title`, `summary`, `priority`,
`aliases`, `tags`. These are readable projections, not editable authorities;
the page fields agree with the registry. Tags are derived/compatibility metadata,
not claims. Pages carry no publication number, so an unchanged page keeps its
bytes across publications; the Generation's `publication_id` is monotonic
within an authority. The accepted
Generation's `manifest_sha256` lives in the publication receipt/cache sidecar,
not in hashed page bytes, avoiding a self-referential digest.

After one H1 title, place one `<!-- kb -->` / `<!-- /kb -->` pair. Its headings:

| Layout      | Managed headings in order                                           |
| ----------- | ------------------------------------------------------------------- |
| person      | `## Mandatory rules`, `## Person facts`, `## General actions`       |
| project-map | `## Current map`, `## Constraints`, `## Actions`, `## Detail pages` |
| topic       | `## Current scope`, `## Feature boxes`, `## Legacy evidence`        |
| feature     | `## Behavior`, `## Action state`, `## Related pages`                |

Maps and feature panels resolve action IDs from the single action projection.
Map rows show subject, optional feature link, kind-specific repository/destination,
push/merge/deploy separately and owner. Constraints and behavior come from exact facts. Topic legacy evidence
shows full preserved facts with unresolved labels. `links` makes feature boxes
discoverable from both map and topic; the feature links back. Do not hide links in
comments only. Emit `[f:<id>]`, `[a:<action_id>]` and evidence `source_id` labels
next to derived rows; exact references are available in the registry/page read.

Then, if the page has narrative, one `<!-- narrative -->` / `<!-- /narrative -->`
pair containing `## Explanation`, named narrative IDs, `current`/`stale` labels,
their full text and an evidence table. Tables identify source ID, archive ID and
version; the registry holds exact content hashes. A changed dependency makes the narrative stale in the
same publication; readers see that label until a checked reconciliation updates
it. A stale block cannot assert current action state. Editors propose complete
narrative records; the publisher renders them. They cannot edit the kb panel.

Dependency keys have exact types: facts uses the existing fact_key string, actions
the existing action_id string, each mapped to a 64-hex revision. A normal fact
correction updates the same fact_key with expected_revision and an existing
verified_match; it is not an ActionUpdate and cannot complete work. StateAPI
updates the same action_id with its expected revision and trusted event, not a
new semantic fact. Role/evidence changes, target moves and legacy resolution also
change fact revisions. Changed dependencies atomically mark narrative stale;
missing dependencies refuse a current label.

Both explanations depend on demo.preview.behavior and demo.legacy.release.
The topic also depends on demo.agent.scope and demo.agent.confirmation; the
feature also depends on all three release action_ids. Thus cancellation behavior
changes and resolution of the legacy release claim invalidate both explanations.

Each marker must occupy its own line, with no nesting, duplicates, or reversed
pairs. Record text cannot contain these markers. Region-free legacy prose stays
unmanaged until explicitly adopted; migration never treats its location after kb
as automatic consent. Bytes outside adopted regions, including whitespace,
frontmatter, comments, manual links and trailing text, remain unchanged. An
adopted old frontmatter/header is a separate explicit migration unit with original
bytes preserved. Do not append a new format to an unclassified marker collision.

Examples are draft projections. Existing header adoption/insertion is individually
scoped in the fictional census; source bytes remain preserved. The topic also
contains a manual field note outside both regions to demonstrate byte ownership.
Legacy pages may use `layout: legacy` while gradual adoption preserves their old
sections; a mechanical migration is not permission to rewrite their prose.

## Action state has a separate owner

StateAPI accepts trusted ActionUpdate evidence and publishes action projections
through the common publisher while semantic editor/Dreaming are paused.

Common identity is action_id plus subject_kind/subject_id. Subject kind is feature,
topic, person or task; its ID is controller-enrolled, not a title/local path or
mandatory page reference. A feature subject resolves to its feature page ID.
Person/task subjects need no repository or feature page. Kinds are enrolled
operation names; this pilot uses push, merge, deploy and book.

| Kind   | Required additional fields               | Optional fields                                                                                       |
| ------ | ---------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| push   | repository_id, work_revision, remote_ref | feature_id for a feature subject                                                                      |
| merge  | repository_id, work_revision, remote_ref | feature_id for a feature subject                                                                      |
| deploy | environment, artifact_id                 | repository_id and work_revision together for a Git-derived artifact; feature_id for a feature subject |
| book   | none                                     | No repository, feature, environment or Git revision required                                          |

Push/merge remote_ref identifies enrolled remote/destination ref; work_revision
identifies the commit/work, not a page revision. Deploy environment identifies its
target and artifact_id what is deployed. The sample Git-derived deploy separately
pins its work revision. A second worktree maps to the same action/subject.

Push completion needs a successful attributable producer receipt or explicit
requester statement. Failed commands do not complete work. A REQUESTER statement
records the stated action as completed, without a pending-verification caveat.
The coordinator verifies attribution and stores an owning operation receipt.
A worker's REQUESTER string grants no trust. The person example books a fictional
paper-prototyping workshop without Git fields; source, principal/client binding
and owning receipt are static fixtures, not an actual booking.

Event order/occurrence differ from ingestion time. Duplicates return the original
receipt; concurrent writes compare revisions under the publisher lock. Old pending
evidence cannot reopen completion after rebase. Corrections/retractions name and
retain the predecessor accepted event. New work gets a new action ID. Ordinary
transitions match the projection state; StateAPI owns transition legality.

Push does not imply merge; merge does not imply deploy. Separate IDs/destinations
remain mandatory. Semantic workers cannot write actions, complete a legacy fact,
override state in narrative or declare a feature shipped in metadata. Panels show
individual actions. Legacy-only state remains unresolved; v2 page_status is not
feature completion.

An ActionUpdate does not carry a shared feature ID or environment for every kind. Keep action_id, subject_kind/subject_id,
expected_revision, event/source order, transition and EvidenceRef common; make
feature_id and Git/deployment bindings conditional by kind as above. This keeps the general-action scope without another authority or API.

## Migration: account for every field and byte

No model calls or semantic replay are needed for deterministic import. Inventory
the pinned registry, all wiki files and queue/receipt objects first. Every source
identity, JSON leaf and page byte range receives one disposition: `mapped`,
`retained-evidence`, or `unresolved`, with destination and reason. Zero discarded
items is necessary; it does not establish semantic equivalence.

| Old input                                                               | V3 mapping / preservation                                                                                                                                                     |
| ----------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Registry version, original serialization                                | New version only on explicit adoption; exact old bytes retained as immutable source and pinned by hash.                                                                       |
| facts map, fact_key, id                                                 | Same keys/IDs; collisions block, no regenerated identities.                                                                                                                   |
| target, text, status                                                    | Same values until separately reviewed edit; classify owner explicitly, unknowns legacy-import. No automatic completion from shipped text.                                     |
| expected_revision, replaces                                             | Retain unchanged as historical proposal fields; never rerun replacements during import.                                                                                       |
| note_id, ark_id                                                         | Preserve exact identities, resolve exact version only when receipt evidence proves it; otherwise unresolved.                                                                  |
| revision, previous_revision                                             | Total old/new mapping; preserve full original chain/snapshot, new hash includes role/ownership; never overwrite history with only one previous hash.                          |
| role, index_target                                                      | Preserve values and absence; keep routing/detail links. Role-only differences invalidate old proposals.                                                                       |
| notes.filename, notes.ark_id and extra receipt fields                   | Preserve filename/ID; unknown receipt fields in compatibility with locator, original receipt bytes retained.                                                                  |
| session_applications and all children                                   | Exact copy, including note/proposal replay identities; no replay of published work.                                                                                           |
| pages.kind/category/parent/title/summary/priority/revision              | Same fields; new IDs assigned by publisher with mapping. Preserve parent relationships; hash change mapped explicitly.                                                        |
| summary_input_hash, summary_assessed_by                                 | `pages[target].compatibility.legacy_fields` with original names/values; preserve assessment input/assessor semantics, don't declare reassessed.                               |
| aliases, tags, updated, status and any other frontmatter                | Preserve original bytes; map known aliases/tags with conflict report. Legacy page status remains evidence, not action authority. Unknown fields retained with source locator. |
| managed facts, headings, source comments/links                          | Inventory every range; map facts to same IDs, retain original bytes in snapshot. Reformat only after preservation review.                                                     |
| all unmanaged Markdown bytes                                            | Keep in place byte-for-byte; explicit adoption requires separate source mapping and review.                                                                                   |
| any unknown registry/page/fact field                                    | Opaque compatibility value plus original exact snapshot/locator, or block the affected scope. Never drop to pass validation.                                                  |
| pending sources, attempts, conflicts, queues, shadow outcomes, receipts | Retain original IDs/outcomes; one disposition per source; stale proposals quarantine/revalidate, import grants no acceptance.                                                 |

Required legacy adapter: legacy-summary-assessment/1. The registry v2 reader accepts only
six page entry fields plus revision and rejects summary_input_hash and
summary_assessed_by. Parse the pinned legacy snapshot through this adapter before
target validation. Preserve names, values, input/assessor relationship and original
bytes in pages[target].compatibility.legacy_fields. Do not strip fields, feed the
packet directly to the unmodified v2 validator, or claim a new assessment. This
offline adapter is implemented in `file_contract.py`: validate the original six-field
view and preserve the complete source object in compatibility. It neither edits
the v2 reader nor reassesses a summary. Absent fields remain absent; an original
null assessment pair remains null.

demo.person.sketchbook keeps its old fact_key/12-hex ID, text, status and evidence.
The old record lacks mandatory/protected fields; the target adds mandatory=true
and protected=false while retaining the original object and absent-field distinction.
Omitting this ordinary person fact from startup or compact refuses delivery.

Static evidence includes demo.archive.design v1 and v2; citations still pin v1.
Hidden evidence and its superseding replacement remain distinct from retraction
or purge; only the owning receipt may verify the hidden version. The unresolved
legacy release has three source revisions/two predecessor links, all retained and
mapped without pretending that its unmatched archive receipt was repaired.

Absent metadata stays absent, distinct from empty/null. A mapped revision is not
permission to rebase old semantic work: prove unchanged semantic baseline before
revalidation. Unmatched exact evidence blocks acceptance of the affected new claim;
the old content remains preserved and visibly unresolved. A rule with unresolved
provenance still cannot be removed from mandatory delivery. Archive visibility,
supersession, claim retraction and physical deletion remain separate operations.
This contract grants no purge or live migration authority.

## Worker result manifest and publisher apply

Manifest schema: `wiki-proposal/1`. Required top-level fields are `schema_version`
(1), `kind` (`wiki-proposal`), `proposal_id`, `producer` (`page-worker`, `dreaming`,
`migration`), `status` (`proposed`), `contract`, `baseline`, `scope`, `inputs`,
`outputs`, `changes`, `coverage`, `losses`, `unresolved`, `revision_map`, `checks`,
`review`, `fact_checks`. Only the synthetic example has an additional `fixture` object; production
admission refuses it. The result manifest is not the publication manifest.

| Field        | Exact shape / meaning                                                                                                                                                                                                                                                                            |
| ------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| contract     | `{version, path, sha256}`: exact contract bytes, not just a name.                                                                                                                                                                                                                                |
| baseline     | `{repository_head, generation, registry_sha256, writer_epoch}`. Generation has `{authority_id, publication_id, manifest_sha256}`. Registry hash is the expected accepted file hash, not the output hash.                                                                                         |
| scope        | `{page_ids, output_paths, adoption_unit_ids}`: controller-assigned page IDs and disjoint allowlisted result paths.                                                                                                                                                                               |
| inputs       | {files, evidence_refs, census, state_receipts}. Files: {source_id, root, path, sha256} under controller-enrolled repository/reference/packet/review roots. census: {source_id, locator, sha256}; state_receipts: [{action_id, operation_receipt_id}]. Only examples emulate files under fixture. |
| outputs      | `{files}`. Each file `{path, target, sha256, expected_sha256}`; expected is null only when target is absent. Records target the one registry path; page targets are wiki-relative.                                                                                                               |
| changes      | Array {entity, id, expected_revision, proposed_revision, validation}; entity page/fact/narrative/action. validation: deterministic-import/semantic-promotion/narrative-edit/page-projection/state-import. Null expected only for new identities. State import requires owning receipt.           |
| fact_checks  | Existing {fact_key, verified_match} shapes, exactly one per semantic fact promotion, empty for pure deterministic import. Retain original wiki-changes/fact-checks note blocks and journal search receipts.                                                                                      |
| coverage     | {items, totals}; item {unit_id, source_id, locator, unit, sha256, disposition, destination, reason}, matching a controller census unit. Field/claim locators are JSON pointers or UTF-8 bytes:start:end ranges; bytes units use ranges. No claim: pseudo-locator. Totals reconcile; discarded=0. |
| losses       | Array `{source_id, locator, reason}`. Nonempty blocks lossless adoption; retaining evidence alone does not authorize removing mandatory/current content.                                                                                                                                         |
| unresolved   | Array `{source_id, locator, reason, blocks}`; blocks lists affected entity IDs. No guessed resolution.                                                                                                                                                                                           |
| revision_map | Array `{entity, id, old_revision, new_revision, disposition}` for every imported entity revision, including unchanged ones.                                                                                                                                                                      |
| checks       | Array `{id, status, detail}` with `pass/fail/not_run`; validation lane distinct from semantic review. Worker assertions require controller receipts.                                                                                                                                             |
| review       | `{semantic_preservation, protected_rules, approval}`: `pending/accepted/rejected` for first two, `not_requested/pending/accepted/rejected` for approval. Workers return pending; only controller binds actual review/approval to output hashes.                                                  |

The controller hashes a census in the existing input packet before workers start:
{schema_version:1, census_id, issuer, baseline, scope, sources, units, adoption_units}.
Issuer/baseline/scope match the trusted assignment; a worker cannot substitute a
self-made census with a fresh hash. Sources pin source_id/root/path/sha256.

Each unit is {unit_id, source_id, locator, unit, sha256, owner, operation,
allowed_dispositions, destination}, optionally delivery with enum startup+compact/detail/evidence.
Owner is registry/managed/unmanaged/archive; operation preserve/project/adopt.
The controller enumerates semantic claims, every owned byte and every JSON leaf,
including empty containers. Source code/reference files are pinned but not corpus
rewrite units. Claims use pointers or actual UTF-8 byte ranges, never claim: labels.

Adoption units are {unit_id, source_id, locator, expected_sha256, operation,
replacement_region, output_sha256, authorization}. Only the controller issues them. Adopting an
unmanaged range needs explicit permission plus review bound to source/output
hashes; location alone never grants model ownership. `replacement_region` names an
output span as `relative-output.md#span:left:offset:right:offset`. Anchors are
`bof`, `eof`, `kb-start`, `kb-end`, `narrative-start`, `narrative-end`; offsets are
nonnegative UTF-8 byte counts. Region starts precede their opening marker; region
ends follow their closing marker. Each referenced marker must be unique and occupy
its own line. Missing/ambiguous anchors or out-of-bounds spans refuse. Fixed
`#bytes:start:end` destinations remain available for explicit fixed positions.
An empty source range can authorize insertion only inside its assigned output
span. `output_sha256` pins the generated structural bytes of that span, excluding
only complete marked managed/narrative regions already checked separately. Thus a
header or separator ending at a relocating marker cannot absorb another claim;
even an EOF anchor cannot absorb unpinned trailing bytes. The preserved source
units, hashes, ownership and permissions stay frozen during a worker edit.
Adoption records correspond one-to-one with census units whose operation is
`adopt`; source, locator, extracted hash and worker coverage must agree before the
record can cover output bytes. An orphan record grants no output region.
The sample preserves all old
unmarked map bytes as an unmanaged tail. Only insertion of a generated region at
its empty start range is proposed. The person title/header change has its own separate proposal-only adopt unit.
The topic manual range stays unmanaged/preserved.
These fictional proposal-only permissions are not actual approval.

The example embeds fictional packet files under fixture to stay within the seven
draft files. String values represent raw UTF-8 file bytes; object values represent
J(value) bytes. Real admission refuses fixture and needs actual controller files.
The census is an input artifact, not another storage/ownership authority.

Worker coverage matches census one-to-one by unit_id, source_id, locator, type,
hash, allowed disposition and destination. Workers cannot invent/omit units,
change person delivery, expand adoption scope or demote semantic content to
retained-evidence. Cold retention is allowed only for designated evidence units;
required person/current claims still need mapped delivery destinations.
For bytes hash the raw range; for fields/claims hash J(original value), obtained
at the JSON pointer or decoded UTF-8 range. Claim counts do not prove meaning.
Losses or unauthorized dispositions block apply.

Apply has distinct validation lanes:

- Deterministic import preserves existing fact_key/IDs, accepted text/status,
  history, original fact-check note blocks/journal receipts and source dispositions.
  Verify snapshot/identity/revision map and census. Do not replay old promotion or
  invent new reconcile searches.
- Semantic promotion of new/changed facts retains NOTE-CONTRACT: exactly one
  existing verified_match with the same fact_key/current revision/expected_revision,
  or a matching not_found/search_id receipt from reconcile. Check the durable
  receipt for exact text/target/current snapshot through existing fact_checks,
  together with outer expected generation/hashes. Fresh archive evidence does not
  check duplicates. Missing/extra/stale checks or unresolved replacement adjudication
  refuse apply; narrative-only edits cannot smuggle new facts past this gate.
- State import needs the owning accepted StateAPI receipt/enrolled origin.
  Narrative/page projections need census ownership, complete dependencies and
  semantic preservation review.

The main fixture deterministically imports existing fact IDs, so fact_checks is
empty. Separate non-applied promotion cases illustrate an existing reference,
new-fact search receipt shape and a duplicate with only fresh evidence that must
refuse. These are not runtime receipt verification. Current fact_checks uses a
bounded lexical candidate snapshot, fenced by outer publication generation checks;
it does not prove semantic absence. No replacement deduplication system is proposed.

Publisher verifies trusted assignment/census/contract, all baselines/epoch/input
hashes/entity revisions under one lock, exact evidence and the appropriate lane,
coverage/adoption/links/protected bytes, complete person delivery and dependencies.
Rewritten meaning needs semantic preservation review. Stage one coherent generation
through existing journal/scoped Git recovery; acceptance and delivery stay distinct.
Any stale/ownership/coverage/check mismatch refuses without mutation. Workers never
edit live managed wiki/registry/checklist; no automatic partial apply.

Publication uses a separate manifest of registry/map/page hashes and derived-index
baseline. Generation manifest hashes do not hash themselves. Readers expose only
the complete accepted generation; a crash resumes the same intent or refuses a
conflicting tail, never serves a mixed generation as fresh. State updates retain
their own accepted receipts when semantic publication fails or is paused.

Dreaming uses this identical manifest and admission path. Similarity/graph topology
only proposes links; it never supplies factual proof. Merge/split proposals preserve
identity/redirect/coverage maps and require review. No direct writes, completion by
age, protected-rule rewrite, or pause bypass. Unresolved findings are reported with
durable identity once; failure does not block trusted StateAPI updates.

## Offline validator candidate

The API is `kelmo.wiki.file_contract.validate_family(root, manifest_path,
controller=ControllerInput(...), allow_fixture=False)`. It returns a frozen
`ValidationResult` with `structural_valid`, typed `errors`/`unresolved`, coverage
totals, exact identities and `runtime_accepted=False`. It has no CLI/MCP registration
and never publishes. Identities include actual manifest, contract, census, source
and output hashes plus the expected baseline. Errors use stable codes and sorted
paths/details. An early structural failure leaves later coverage unmeasured.

`root` is an explicit transport directory. The manifest, pinned contract and
assigned outputs use canonical relative paths beneath it. Descendant symlinks
and a root that is itself a symlink refuse. Files must resolve beneath the resolved
root. An ordinary parent alias, such as macOS `/tmp` resolving to `/private/tmp`,
is allowed. These checks cover supplied file paths; they do not create an OS
security boundary or protect against hostile concurrent filesystem replacement.
Sources arrive in `ControllerInput.sources`, keyed by source_id; the
validator never opens a source path from the worker manifest. The caller must
retain its own independent assignment. A Python type cannot authenticate a caller.

`ControllerInput` contains these controller snapshot arguments:

- `contract`, `baseline`, `scope`, `census` and `sources` (raw bytes).
- `baseline_manifest` (raw bytes); `registry_source_id`, `state_source_id`,
  `evidence_source_id`, `history_source_id`, `owning_receipts_source_id` select
  separately hashed census sources. Role snapshots outside the census refuse.
- `registry_target` is the independently assigned canonical registry destination.
  It must resolve to the pinned registry hash in the supplied baseline manifest.
  The worker cannot choose a new JSON target or use a null expected hash to bypass
  that assignment. The caller supplies its configured path; no vault path is built in.
- `repositories` names enrolled repository IDs. `protected_fact_keys` retains
  the protected-rule inventory independently of worker flags.
- `evidence_reads` binds a hidden source ID to its owning operation receipt.
  That receipt must also bind all four exact EvidenceRef fields.
- `narrative_dependencies` maps each narrative ID to reviewed `facts` and
  `actions` key sets. Revisions are checked against proposed records; a worker
  cannot substitute a shorter dependency map.
- Optional `page_catalog` supplies raw JSON bytes, paired with the independently
  supplied `page_catalog_pin: {catalog_id, authority_id, sha256}`. Both must be
  supplied together. The hash covers the exact bytes; identity and authority must
  agree with the body and assigned authority. Omitting both restricts references
  to the assigned family. Worker manifest/registry/fixture copies never establish
  or replace this input. Returned identities include its actual and expected hash.

The read-only catalog is `{schema_version:1, catalog_id, authority_id, pages,
redirects}`. `pages` maps canonical targets to `{page_id, parent, aliases}`;
`redirects` maps old canonical targets to page IDs. The controller supplies the
external metadata needed for reference resolution and complete parent chains.
Targets and entity IDs are disjoint from the assigned output family. Missing
parents, cycles, conflicting targets/IDs, alias collisions, invalid redirects and
ambiguous references refuse. Catalog redirects cannot be replaced by worker
redirects, and worker redirects cannot introduce an external catalog identity.
Canonical/local/suffix, redirect and alias resolution use the same existing
grammar over the combined metadata. Links and parents may resolve externally;
output page IDs, output paths, facts and rendered pages remain exactly assigned.
No external page output or write permission is required or granted.

For example, a caller can supply fictional catalog bytes containing
`"projects/outside/reference.md": {"page_id":"demo.outside.reference",
"parent":"", "aliases":["Outside reference"]}` under `pages`, hash those bytes
independently, and retain a managed `[[Outside reference]]` link without adding that
page to the proposed registry or output set. This is reference metadata, not a
second registry, live graph or publication authority. The caller establishes its
currency and authenticity; matching a supplied pin does not prove either at runtime.

These are transient assignment inputs, not a new registry or authentication
service. State projections must equal the separately pinned StateAPI snapshot.
REQUESTER/PRODUCER attribution is checked for consistency with supplied receipts and
client bindings. A hidden owning receipt must match the binding's authority and
machine as well as the enrolled client and assignment authority. Actual
authentication and transition acceptance remain runtime
work. The narrow file adapter supports legacy registry v1/v2 deterministic import
into the proposed v3 family, not editing an already active v3 store.

Packaged Draft 2020-12 schemas validate fields, types and enums. `jsonschema`
4.26.0 and `referencing` 0.37.0 use an explicit in-memory registry; unknown
references refuse through a callback that raises `NoSuchResource`, without network
retrieval. Domain checks extract JSON leaves and UTF-8 byte ranges, recompute
hashes/revisions, compare destinations, preserve original objects/history/receipts,
check person membership and resolve parents/aliases/links and rendered projections.
Counts alone cannot verify preservation. Existing page grammar, entry checks and
legacy fact revision helpers are reused. Literal wiki links retain the existing
resolver's exact/local/suffix ordering without its disk-wide scan.
Unique registered aliases are a fallback through the same page identity map after
literal canonical/local/suffix and redirect resolution. Ambiguous or shadowing
identities still refuse; no alias winner is guessed.

The candidate requires the full assigned family, an explicit change for every
proposed entity and one registry output. Every corpus source needs complete byte
accounting; structured sources also need every JSON leaf, including empty containers.
Destinations use a JSON pointer, an anchored span or `bytes:start:end`. The example's proposal-only
generated-prefix insertion is an explicit adoption unit. Unmanaged mappings compare
exact destination bytes. Claim mappings compare original values with projected
text; semantic equivalence is never inferred.
Preservation destinations and output references share one path resolver: an exact
canonical relative output path wins, followed only by a unique basename fallback.
Traversal, malformed paths, missing files and ambiguous basenames refuse.
Source-backed fact/narrative claim matching uses that same resolved path and the
decoded JSON-pointer segments, rather than comparing a hardcoded registry
basename. Exact relative paths and unique basenames identify the same assigned
registry field. Extraction still verifies the actual value and source hash.

Output coverage is total as well: every Markdown byte must belong to a marked
managed/narrative region, an exact preserved unmanaged span or an explicit
controller adoption range. Gaps, inserted claims and added whitespace at EOF
refuse. Widening a preserve destination is not adoption; exact range mappings must
equal the original bytes in full. These checks neither strip nor rewrite content.
Trusted source locators and permitted destinations are immutable through worker
edits. Splitting an unmanaged source span preserves its declared source-relative
order and non-overlap through those fixed destinations; swapping/duplicating its
worker bytes refuses exact preservation, and overlapping worker destination claims
refuse the census identity check. Census array order is not source order. A separate
controller assignment can explicitly authorize rearranged destinations; that is
not a worker exploit. Overlapping evidence-retention mappings remain valid. No
blanket source-span overlap or census-array-order restriction is imposed.

Opaque originals and active legacy fields are checked separately. Imported facts
retain `expected_revision`, `replaces`, `role`, `index_target` and their original
presence/absence along with identity, text and evidence linkage. Active note
filenames and archive IDs remain unchanged. New revision/ownership/delivery
metadata follows the documented migration transform; storing an original in
compatibility does not permit another active-field transformation. Preserved JSON
uses canonical J(value) equality, so `true`, `1` and `1.0` remain distinct.

The implemented acceptance lane is deterministic import with page/narrative
projections and receipt-consistent StateAPI import. New facts cannot enter through
the import lane. Semantic promotion is identified separately, requires exactly one
fact check per identity and currently refuses with `reconciliation`, directing it
to the existing runtime receipt path. A shape-valid search ID is not authenticated
here. Stale dependencies must carry a visible stale label; omitted dependency
identities refuse. Independent meaning/loss review, actual startup/compact delivery
and overflow refusal, archive visibility/history, publication locking and recovery
remain runtime obligations. A structural pass never supplies those receipts.

Visible projections are read from the existing markdown-it token stream with
tables enabled. HTML comments and fenced/indented code cannot supply visible
facts, narrative state or navigation. Inline code remains visible within factual
text, as in `Use **git add** only.` or ``Use `git add` only.``; factual text and
its rendered projection are compared through the same Markdown grammar, retaining
emphasis and code. Structural identifiers and narrative state declarations inside
inline code cannot supply identity or narrative state evidence. Action field
values retain visible inline code as described below. The full-page parse retains
the surrounding comment/code context before selecting each managed region.
Raw HTML in required fact/narrative source text returns `unsupported-html`,
including comments or image attributes carrying an exception. Non-comment HTML
tokens anywhere in the page body also refuse: wrappers and styles outside a
selected region can change its visibility. Pure page comments remain allowed for
markers/audit context, but never provide delivery evidence. The comment-only
exception accepts only well-formed comment syntax, consuming each comment through
its first `-->`. Its body cannot begin with `>` or `->`, contain `<!--`, `-->`
or `--!>`, or end with `<!-`. Empty bodies and bodies ending in `<!` are valid.
Multiple comments may be separated by whitespace. Other markup between them and
malformed comments return `unsupported-html`, even within one Markdown HTML-block
token. Escaped literal markup keeps its behavior.
The validator does not sanitize HTML, execute a browser or infer CSS visibility. HTML tokens are
retained in factual comparisons; no source, unmanaged or evidence bytes are
stripped or rewritten. An unsupported projection does not skip preservation or
coverage checks. Escaped literal angle brackets, emphasis, inline code and ordinary
prose retain their existing Markdown behavior.

Every narrative needs one visible state declaration matching its record. Fact
text before its identity marker must match completely. Action projections compare
the existing display fields below against the accepted action record. Missing or
contradictory required declarations refuse; multiple Subject representations must
agree. Feature/map panels retain their existing tables and general non-Git actions
their sentence format, without adding columns or requiring fabricated Git fields.

| Existing display                                          | Record binding                                                                                   |
| --------------------------------------------------------- | ------------------------------------------------------------------------------------------------ |
| `[a:…]`, Kind/Action label, State, Owner                  | `action_id`, `kind`, `state`, `owner`                                                            |
| General Subject, map Feature link, feature shared Subject | `subject_id`; the Feature link resolves through the same page/catalog graph                      |
| Feature shared Subject kind and Feature ID                | `subject_kind`, `feature_id` (or the feature subject identity when the optional field is absent) |
| Table Repository                                          | `repository_id` when present; absent non-Git binding stays absent                                |
| Table Destination/environment for push/merge              | `remote_ref`                                                                                     |
| Table Destination/environment for deploy                  | `environment`                                                                                    |
| Feature Work revision                                     | `work_revision` for push/merge and Git-derived deploy; non-Git deploy has no fabricated revision |
| Feature Deployment artifact                               | `artifact_id` for deploy                                                                         |
| Table Evidence `source vN`                                | `accepted_event.evidence_ref.source_id` and `.version`                                           |
| General Evidence mark, source/version and owning receipt  | `accepted_event.evidence_mark`, its exact EvidenceRef source/version, and `operation_receipt_id` |

Action values include all visible cell/declaration text, including inline code.
An exact value entirely inside code, with or without emphasis, remains valid;
additional code text changes the value and must agree with the record. Unsupported
content such as images invalidates the bound field instead of disappearing.
A map Feature cell contains one complete subject link without additional content.
Its caption uses the same supported text/formatting tokens as action values;
images and unsupported caption children refuse. The destination still resolves
the feature identity, including registered aliases; caption wording is not a
semantic authority check.
A dedicated Action ID cell cannot hide a visible prefix behind a separate Kind
column. Each structural action marker must occur at its matched position in a
plain text token; a code marker and an unrelated plain-text decoy cannot combine
to supply identity. These rules retain the existing non-Git action bindings.

Other source/receipt fields remain checked in their supplied snapshots. Comparing
these displays establishes offline consistency, not receipt authentication.

Wiki links use the existing source-aware inline grammar before escape decoding,
including alias/fragment syntax and embed refusal. Escaped brackets, comments and
code do not create edges. Unique registered aliases and canonical links retain the
same resolution and ambiguity refusal. Factual wiki-link projections also retain
their source label; keeping the target does not permit changing visible factual
wording. Original Markdown bytes and unmanaged
ranges are never rewritten by these checks.

Excessive JSON/structured-input nesting returns a stable `input` error at the
public API boundary. It never creates a structurally valid result or changes the
interpreter's recursion limit.
Role snapshots and their nested maps are structurally validated against packaged
schemas before use. Malformed registry, baseline, state, history, evidence or owning
receipt shapes return `input` issues; the API does not catch arbitrary programming
errors to manufacture a result.

### Portable fixtures and audit context

Required fictional inputs now use canonical `packet` paths. A demo caller uses
`fixture.source_bindings` to extract raw strings or J(object) bytes, freezes an
independent census/assignment, then passes bytes explicitly. The validator never
uses those bindings to construct trusted inputs. Missing bytes refuse. Default
admission refuses `fixture` unless the caller enables example mode. A fixture-free
test retains the original inputs in the same registry's opaque `compatibility`
map and uses ordinary JSON-pointer destinations; no second store is created.

`fixture.owning_receipts` supplies an accepted synthetic receipt for
`demo.operation.hidden-sketch`, bound to the exact v1 `demo.archive.old-sketch`
EvidenceRef and fictional client origin. Its file hash, every leaf and raw bytes
are included in source/census/coverage. The hidden-receipt correction added ten
units to the original census. With the explicit output bounds, the current totals
are 469 input, 155 mapped, 313 retained-evidence, one unresolved, zero discarded.
Wrong-operation and
mismatched-version variants refuse even when supplied bytes have been rehashed.
Hidden/superseded records remain fixtures; no archive read or visibility policy
is implemented.

Markdown retains full facts, identity markers, explanations and navigation.
Narrative evidence tables omit repeated hashes, which remain in the registry and
manifest. Person now visibly links to its registered project map. Informal
unmanaged anchors became exact destination byte ranges. These are mechanical
fixture corrections, not approval of a different page model.

The preservation follow-up pins eight proposal-only adoption ranges. Six new
insertion units account for the topic/new-feature headers and separators; the
feature's original-empty input is supplied explicitly as fictional zero bytes in
the same packet mechanism. No Markdown content was changed. Old packets with only symbolic
adoption names need a controller-issued range-pinned snapshot; workers cannot
upgrade their own assignment.

The round-two fixture replaces absolute output offsets with anchored spans and
adds structural output hashes to those same eight adoption records. Original
source units, source locators/hashes, scope and proposal-only permissions remain
unchanged. This initial
fixture correction does not require reissuing an assignment after worker edits.
The positive alias scenario freezes the controller first, changes a managed link
to a different-length registered alias, and updates only worker output hashes.
No test helper reanchors the census. Explicit added permissions still use a
distinct controller snapshot.

### Focused verification

Run from the source tree, selecting its `src` ahead of an installed copy, with an
existing interpreter containing the locked dependencies:

```sh
PYTHONPATH=/path/to/candidate/src /path/to/python -m pytest -q tests/test_wiki_file_contract.py
```

The repeatable scoped review command also runs lint and the tracked diff check:

```sh
PYTHONPATH=/path/to/candidate/src /path/to/python -m pytest -q tests/test_wiki_file_contract.py --tb=short && /path/to/python -m ruff check src/kelmo/wiki/file_contract.py tests/test_wiki_file_contract.py && git diff --check
```

The pure-file module covers a portable valid family, fixture-free transport,
no mutation and stable results; missing ordinary person content, lost rule
exception, forged census/disposition, stale baseline/source/target/contract,
forged predecessor, evidence version/head rebinding, missing legacy metadata/history,
unmanaged-byte loss, invalid ownership/promotion/adoption, malformed paths/JSON/markers,
aliases/parents, action projections and stale dependencies. Unknown remote schema
references refuse. The controller owns wheel/native integration and exact-commit review.

Round-one counterexamples were reproduced before the correction: 26 failed and
49 passed across 75 pure-file cases. The regressions include the controller's
unchanged registry-redirect test, foreign hidden-receipt origins with fully
rehashable census/coverage, hidden/duplicate narrative states, exact table/general
action fields, analogous hidden factual/narrative/navigation paths and deep JSON
in both manifest and caller-supplied source bytes. These are offline consistency
tests, not new authentication, publication or delivery receipts.

The remaining preservation defects reproduced as 9 failures with 79 passes before
the correction. The cases first accept a coherent fictional baseline, then test
EOF/outside insertions, active legacy-field divergence, an opaque original changed
from integer to boolean/float, and a registered alias. The type case pins its new
fictional source/census before the worker changes a single compatibility value.
A further widened-preserve counterexample reproduced as 1 failure with 89 passes;
an explicit EOF adoption has a separate positive test. The stale-comment cases
from the preceding correction remain in this same pure-file suite.

Round-two findings reproduced as 21 failures with 93 passes before correction,
including the frozen-assignment alias scenario. Added cases cover generated
header/separator additions, missing/ambiguous anchors, EOF range stretching,
adoption source/locator/hash/coverage binding, formatted factual text and malformed
role maps. The controller reruns actual sdist/wheel acceptance separately.
An additional visible-label counterexample reproduced as 1 failure with 126 passes:
changing a factual wiki link's displayed wording while retaining its target must
also refuse.

Round-three regressions first returned 26 failures with 130 passes: the catalog
input was absent, three Subject renderings accepted another identity, canonical
preservation paths failed, and a symlinked root passed. The unchanged-trust reorder
probe already refused with `preservation: original byte span lost at destination`;
an overlapping worker destination refused the census identity check. Distinct
controller-authorized rearrangement, reversed census array order and overlapping
evidence mappings have separate positive cases. No span behavior was changed for
that finding. Catalog cases cover external targets/aliases/redirects/parents,
stale/tampered pins, collisions, malformed shapes, missing references, worker copy
substitution and attempted output-scope expansion. Packaged checks remain the
controller's acceptance step.

The round-four baseline is checks-04: 168 cases each from source, sdist and wheel;
the review report's repeated 127 count refers to an older candidate. New
counterexamples returned 41 failures with 171 passes before correction: hidden or
styled facts/stale labels, exceptions lost with HTML tokens, altered displayed
action bindings, and canonical claim paths. Tests also cover a raw style block,
preservation refusal alongside unsupported HTML, and valid escaped angle brackets.
The correction changes no trusted census during a worker-only mutation and leaves
the Markdown examples, schemas and prior review packets intact. Historical
contract pins remain in the fixture audit context.

The round-four recheck baseline is checks-05: 214 cases independently passed from
source, sdist and wheel. The earlier 168 count is historical. The new parser
counterexamples produced 35 failures and 220 passes before correction. Focused
declaration cases then produced four failures with 254 deselected; stricter marker
origin and dedicated-ID cases produced two failures with 257 deselected. Worker
mutations retain the original controller, census, registry and receipts, updating
only output bookkeeping. The correction adds no schema, protocol or runtime
authority and leaves frozen packets intact. The old contract pin remains audit
context when the documentation pin is refreshed.

The next recheck baseline is checks-06: its hashes match the frozen 259-case
candidate for source, sdist and wheel. The acceptance report's contrary timing
claim is incorrect. New focused cases returned seven failures, nine passes and
258 deselected before correction: six strict-comment cases and the linked-image
caption checker failed. The public API already refused that linked image with
`projection`; the local caption checker now enforces the same token allowlist.
All worker-only mutations retain the original trusted assignments and source.

## Runtime acceptance and pilot

| Case                         | Required observation in the next stage                                                                                                                                                           |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| V01 valid family             | Four examples parse; target/page IDs, revisions, aliases, parents, links, action projections and exact hashes agree.                                                                             |
| V02 rule exception           | All person facts, including an ordinary fact without an old mandatory flag, reach startup/compact or omitted delivery/overflow refuses. Rule conditions/exception/status protection is separate. |
| V03 state independence       | Push remains distinct from merge/deploy; a non-Git book action completes from an attributable REQUESTER receipt while semantic work is paused.                                                     |
| V04 stale input              | Change any source, contract, generation, epoch, registry/page hash, role or dependency revision: no accepted mutation; old pending evidence cannot reopen completion after rebase.               |
| V05 exact evidence           | Same archive ID gains v2; a v1 citation still verifies v1. Missing historical version/hash stays unresolved; hidden evidence is read only by its owning receipt capability.                      |
| V06 byte preservation        | Unmanaged comments, frontmatter, whitespace and trailing bytes survive. Duplicate/nested markers, symlinks and traversal refuse. No silent legacy truncation.                                    |
| V07 total migration          | The named legacy adapter, two archive versions, hidden/superseded evidence, three revisions and original receipts survive. Controller census constrains dispositions/adoption.                   |
| V08 discovery                | More than 200 pages enumerate completely through pinned cursor; omitted detail expands exactly; missing/ambiguous aliases stay unresolved. Feature discoverable from map and topic.              |
| V09 semantic gap             | Valid JSON that omits ordinary person content or weakens a condition refuses. Fresh EvidenceRef without reconciliation receipts cannot promote facts.                                            |
| V10 one publisher/recovery   | Concurrent proposals cannot both overwrite the baseline; interruption never exposes mixed maps/registry. Accepted and delivered are separate receipts.                                           |
| V11 narrative freshness      | Changes to cancellation behavior or legacy release fact_keys stale both explanations; action_id updates stale dependent narrative. Typed maps reject display IDs.                                |
| V12 closed-action correction | Named attributable correction/retraction updates the action once, retaining predecessor evidence; unrelated new work uses a new ID.                                                              |

First pilot: one isolated copy containing the full person fact set, one project map, one
scheduler topic and one feature linked to two enrolled fictional
repositories. Include one exact protected exception, one unresolved legacy fact,
an ordinary person fact lacking an old mandatory flag, a non-Git REQUESTER-completed
action, preserved unmanaged ranges and separate push/merge/deploy IDs. Extend only
with the negative variants above. Real native delivery, archive version/visibility,
database and crash tests belong to the controller; these pages are fixtures, not
proof those mechanisms run.

## Controller checks after format review

The focused offline checks above exercise this candidate. Controller-owned existing
regression commands, with the locked interpreter, remain separate:

```sh
python -m pytest -q tests/test_pages.py
```

These tests exercise the current v1/v2 behavior and are not v3 acceptance. After
implementation, register V01-V12 in the existing gate, initially target V02/V04/V06/V09,
and have the controller run the isolated StateAPI, archive and native-delivery
lanes. The offline Python API does not register a v3 runtime command.
