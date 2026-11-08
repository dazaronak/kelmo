---
wiki_contract: wiki-file-contract/3.0-proposal.2
page_id: demo.feature.update-preview
target: projects/harbor-demo/update-preview.md
kind: work
category: projects
parent: "projects/harbor-demo/sailing-schedule.md"
title: "Sailing update preview"
summary: "Review and confirm timetable-row changes before applying them."
priority: "H2"
aliases: ["Harbor update preview"]
tags: [fictional, harbor-demo]
---

# Sailing update preview

Fictional example proposal. No generation is published and no action here was executed.

<!-- kb -->

## Behavior

- The preview lists each proposed entry change before confirmation; cancelling the preview leaves the sailing unchanged. [f:9716c005794e] [source:demo-source-design]

## Action state

| Action ID                  | Repository           | Kind   | Destination/environment    | State     | Owner     | Evidence              |
| -------------------------- | -------------------- | ------ | -------------- | --------- | --------- | --------------------- |
| [a:demo.action.preview-push] | demo.repo.harbor-web | push   | origin/preview | completed | state-api | demo-source-events v1 |
| [a:demo.action.preview-merge] | demo.repo.harbor-web | merge  | origin/main    | pending   | state-api | demo-source-events v1 |
| [a:demo.action.preview-deploy] | demo.repo.harbor-web | deploy | sandbox        | pending   | state-api | demo-source-events v1 |

Subject: feature/demo.feature.update-preview. Feature ID: demo.feature.update-preview. Work revision: 1111111111111111111111111111111111111111. The current map selected for either repository resolves these same action IDs; the tablet has no independently completed action in this fixture.

Deployment artifact: demo.artifact.preview-build. Push/merge bind remote refs; deployment binds sandbox and its artifact. Exact owning receipts are retained in the registry input.

## Related pages

- [Sailing Schedule topic](sailing-schedule.md)
- [Current project map](harbor-demo.md)

<!-- /kb -->

<!-- narrative -->

## Explanation

Narrative: demo.narrative.preview. State: current.

The preview gives the user a chance to inspect each proposed value and cancel without changing the sailing. Push, merge and sandbox deployment are tracked separately. The action panel supplies their current state; the older handbook claim cannot close any of those actions.

| Source | Archive ID | Version |
| --- | --- | --- |
| demo-source-design | demo.archive.design | 1 |
| demo-source-events | demo.archive.events | 1 |

Static evidence also retains hidden demo.archive.old-sketch and its superseding demo.archive.new-sketch. Ordinary retrieval excludes the hidden record; its owning receipt may verify it. No claim retraction or purge is implied.

<!-- /narrative -->
