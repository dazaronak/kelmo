# Snapshot and recovery commands

These commands expose the existing snapshot and publisher mechanisms. Mutation
requires `--apply`; the default prints a plan. Use absolute paths and independent
source and destination roots. Keep specs, reports and snapshots in private
operator storage, outside the product repository.

## Take a stopped snapshot

Save a JSON source specification:

```json
{
  "archive": {"kind": "sqlite", "path": "/srv/old/archive.db"},
  "vault": {"kind": "git", "path": "/srv/old/knowledge-repository"},
  "journal": {"kind": "sqlite", "path": "/srv/old/state/journal.sqlite3"}
}
```

Use `tree` for an additional plain directory, such as captured source bytes or a
queue. Omit the journal when the old system has none.

```sh
kelmo snapshot create --spec /srv/migration/sources.json --destination /srv/snapshots/before-migration --json
kelmo snapshot create --spec /srv/migration/sources.json --destination /srv/snapshots/before-migration --writers-stopped --apply --json
kelmo snapshot verify --snapshot /srv/snapshots/before-migration --json
kelmo snapshot inventory --snapshot /srv/snapshots/before-migration --json
```

`--writers-stopped` records the operator's precondition; it does not stop services.
The implementation compares source fingerprints before and after copying and
refuses a moving source. The Git source must be clean. Use `inventory --queue
SOURCE_NAME` to classify a copied queue and `--details` for every item.
Snapshot roots are private directories; database copies and JSON receipts use
owner-only file permissions. Import stages Git trees under a private directory
and exposes the verified destination only after the copy completes.

The inventory includes session jobs by ID and snapshot hash. Pending, running
and archived jobs resume after cutover; completed jobs are imported; review or
unknown states stay unresolved. Older snapshot manifests are supplemented from
their verified journal copy so an absent cached job list cannot hide pending work.

## Import into independent roots

Save an import specification naming sources from the snapshot:

```json
{
  "archive": "archive",
  "vault": "vault",
  "journal": "journal",
  "roots": {
    "state_root": "/srv/new/state",
    "archive_root": "/srv/new/archive",
    "knowledge_git_root": "/srv/new/knowledge"
  }
}
```

```sh
kelmo --vault /srv/new/control snapshot import --snapshot /srv/snapshots/before-migration --spec /srv/migration/import.json --json
kelmo --vault /srv/new/control snapshot import --snapshot /srv/snapshots/before-migration --spec /srv/migration/import.json --apply --json
```

The control directory is separate from all three roots. Repeat the same command
after interruption; completed import steps are retained. Existing destination
content that differs is refused. The archive keeps its authority and writer
epoch. Optional keys: `authority_id`, `label`, `registry_relative`,
`wiki_relative`, `settings` (carried TOML tables), `state_sources` (the named
tree containing captured source bytes), and `state_sessions` (the named tree
containing the state root's sessions directory) and `state_telemetry`. If the journal references
session snapshots, their complete hash-matching tree is required; missing
transcript bytes refuse import before any destination is created.
Identity and storage settings cannot be
overridden through the carried tables.

The import receipt binds the source selections, destination roots, authority,
layout and settings before the first copy. A changed specification cannot reuse
completed steps. Older partial-import receipts without this binding require a
fresh destination; keep their copied data for inspection.

## Cutover and forward recovery

```sh
kelmo --vault /srv/new/control snapshot mark-cutover --apply --json
kelmo --vault /srv/new/control snapshot tail --json
kelmo --vault /srv/new/control snapshot rollback-check --json
```

The rollback check refuses if the new runtime has accepted writes after the
baseline, including session captures and processing results that have not yet
produced archive records. `session_tables` names changed session tables without
printing their content. An older baseline that did not record session state
cannot prove existing sessions safe to discard, so it also refuses when such
state exists. The check does not restore or delete anything. Preserve those writes through
forward recovery, with a spec containing only the new independent `roots`:

```sh
kelmo --vault /srv/new/control snapshot recover --snapshot /srv/snapshots/recovery --destination /srv/recovered/control --spec /srv/migration/recovery-roots.json --writers-stopped --apply --json
```

Stop writers first. Recovery snapshots the current state, including the accepted
tail, imports it into fresh roots and marks a new baseline. Rebind executor and
service paths at the destination. Old runtime activation, service stop/start,
client switching and data retirement remain separate operator steps.

## Scheduled vault backup

```sh
kelmo --vault /srv/control snapshot backup --destination /srv/backups/vault --keep 7 --writers-stopped --apply --json
```

Without `--apply` it lists the sources and the last verified backup. With it,
the worker lock is held while the archive, journal, knowledge repository,
captured sources, transcripts and the server search log
(`telemetry.sqlite3`) are copied into a new `vault-<UTC time>-<id>`
directory; the double-read barrier refuses the copy if any of them changed,
and the copy is re-read against its manifest before it counts. The directory
also holds `restore.json`: the `snapshot import` specification without
`roots`, so a restore drill is that file plus fresh roots, imported into a
new control directory and verified by the same census as a migration. The
newest `--keep` backups this command made stay; older ones it made are
removed. The vault records the verified backup in
`<state root>/last-backup.json`, and `kelmo doctor` reports it as `backup`
with its age, warning when it is older than 36 hours.

A writer pause means: fence the server profile (`kelmo server fence --on
--apply`; a channel opened before the fence reads the profile again on every
write, so clients keep their writes queued, and a running worker waits between
passes while the fence holds), stop the worker, the vault's `kelmod`
(`kelmo daemon stop`) and vault sync, back up, then unfence and start them
again on every exit path. The embedding
model, caches, locks and the executor binding are not copied; they are
installed again on a restored host.

## SQLite snapshot access

If snapshot creation returns `E_SNAPSHOT` for SQLite access, inspect the source
database and its WAL/SHM file access under the account taking the snapshot.
An owner-provided consistent backup database is valid input. A text SQL dump
must first be restored to a separate SQLite file; it is not a database file.
Do not copy only the main file of a live WAL database. Stop writers before the
snapshot and retain any failed partial copy for inspection or choose a new
destination; a missing manifest is not a completed snapshot.

## Collect deployment evidence

`kelmo --vault /absolute/control snapshot report --destination /absolute/private/report
--include /absolute/server.json
--include /absolute/worker.service` previews an evidence bundle. Add `--apply`
to create it. Repeat `--include` for explicitly selected reports, profiles or
service definitions. The import receipt and cutover baseline are included when
present. Each captured file has its source path, byte count and SHA-256 in the
manifest; directories use mode 0700 and files 0600.

The destination must be new. No source is deleted, no service is contacted and
no model is called. Collection copies only named files, not the archive, wiki,
transcripts, provider credentials or the whole home directory. Do not select
credential files or publish the bundle: a selected profile/report can contain
private deployment information. This is an evidence bundle, not a consistent
data backup; use `snapshot create` with stopped writers for recovery data.
