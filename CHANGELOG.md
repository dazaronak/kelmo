# Changelog

Every change a user can see, newest first. A version heading matches the tag that published it: `v0.2.0` is
`## [0.2.0]`. A feature reaches `main` through a merge commit whose message carries the same notes as its
entry here.

## [Unreleased]

### Added

- `kelmo dream apply --ready` publishes only the projects that have at least `[editor] batch_ready_sources`
  notes waiting for their first pass. It needs `[editor] batch_apply = true`; without it the option is refused
  with a message that names the setting.
- `[editor] batch_ready_sources` sets how many waiting notes make a project ready. It defaults to
  `batch_max_sources`.
- `kelmo dream status` reports `waiting` (the first-pass notes of each project), `ready` and `ready_at`.
- An outage alert. When the vault cannot take a publication, the owner hears about it once when it starts,
  once a day while it lasts and once when it ends. It goes through the `[alerts]` command; without one, the
  worker's journal gets the same line once a day.

### Changed

- A vault that cannot take any publication no longer parks notes or spends their attempts. That covers
  uncommitted edits in the wiki, a commit that cannot be written, a wiki over its budget or not rendering as
  it stands, and a prepared publication the recovery cannot finish. The pass stops, every note keeps its
  place and its attempts, and the owner is alerted. Before, every note was charged and could fail for good
  after three passes.
- A note refused for a reason the vault caused (the wiki moved while the model was asked, the note's request
  changed, a batch commit was refused as a whole) holds back the newer notes of its project until it is
  asked again. A newer note no longer publishes ahead of it.
- A note whose publication was committed before a crash settles from that commit on its next turn. It is not
  asked again and not parked.
- A read that fails is tried again before it counts against a note, and a lock or a busy database never
  costs a note an attempt.
- `kelmo dream apply` through a running daemon answers `{"state": "running"}` when its wait ends before the
  pass does. The pass goes on in the daemon, and `kelmo daemon status` lists it under jobs.
- Batched apply (`[editor] batch_apply`, still off by default) now runs on the 0.2 daemon: one conversation
  for a batch of a project's waiting notes, each note settled on its own. A note the batch cannot settle goes
  through the single-note path in the same pass, uncharged.

Nothing was removed or renamed. Error codes keep their meaning, and no stored data needs a migration.

## [0.2.0] - 2026-11-13

- Kelmo runs as one Rust daemon per machine, `kelmod`, with single-owner cores.

## [0.1.1] - 2026-11-09

- A refusal the vault causes no longer spends a note's attempts, a retried note gets its editor attempts
  back, and a budget refusal names the pages it left out with their numbers.
- Batched apply behind `[editor] batch_apply`, off by default.

## [0.1.0] - 2026-11-08

- First public release.
