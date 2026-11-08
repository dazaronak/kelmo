# Synchronize the knowledge repository

Kelmo publishes into its configured knowledge Git checkout. It does not
implicitly push. When that checkout shares a remote with other writers, use:

```sh
kelmo --vault /srv/memory sync --remote origin --branch master --json
kelmo --vault /srv/memory sync --remote origin --branch master --apply --push --json
```

The first command inspects local state without contacting the remote. Apply
requires a clean checkout on the named branch. It takes the worker and publication
locks, retains the original head under `refs/kelmo/sync/`, writes a recovery
receipt, fetches the named branch and checks a divergent merge with
`git merge-tree --write-tree` before changing the checkout. Git must support that
command. A normal content conflict leaves the working tree untouched.

`--push` is explicit. Before pushing, the command reports the commit count and
changed paths relative to the fetched branch. It never force-pushes. A concurrent
remote update can reject the push; retain the merged local commits and rerun
after inspecting the receipt. Each Git command has a 120-second timeout.

Receipts live under the vault's configured state directory in `sync/`. They name
the original head, fetched head, resulting head and retained backup reference.
No automatic backup-reference deletion is performed. Unexpected Git failures,
including a local hook failure, require inspecting the checkout before retrying;
the command does not discard local files to hide a failed merge.

An operator can schedule the second command with the host's existing service
manager after a manual successful run. Scheduling and remote credentials belong
to the deployment. This checkout does not install or activate a timer.
