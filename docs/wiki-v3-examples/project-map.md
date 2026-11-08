---
wiki_contract: wiki-file-contract/3.0-proposal.2
page_id: demo.project.harbor
target: projects/harbor-demo/harbor-demo.md
kind: hub
category: projects
parent: ""
title: "Harbor Demo current map"
summary: "Fictional web and tablet project: current actions and links to detail."
priority: ""
aliases: ["Harbor Demo"]
tags: [fictional, harbor-demo]
---

# Harbor Demo current map

Fictional example proposal. No generation is published and no action here was executed.

<!-- kb -->

## Current map

- Harbor Demo connects a fictional web sailing page and tablet. Both repository bindings use the same sailing update preview feature. [f:5a4bcbb3ab5a] [source:demo-source-design]

| Repository binding      | Role             | Shared feature                              |
| ----------------------- | ---------------- | ------------------------------------------- |
| demo.repo.harbor-web    | Sailing page     | [Sailing update preview](update-preview.md) |
| demo.repo.harbor-tablet | Tablet companion | [Sailing update preview](update-preview.md) |

## Constraints

- Sailing edits require a visible entry-by-entry preview and an explicit user confirmation. [f:e2dd2ccf1271] [source:demo-source-design]

## Actions

| Feature                                     | Repository           | Action                                | Destination/environment    | State     | Owner     | Evidence              |
| ------------------------------------------- | -------------------- | ------------------------------------- | -------------- | --------- | --------- | --------------------- |
| [Sailing update preview](update-preview.md) | demo.repo.harbor-web | Push [a:demo.action.preview-push]     | origin/preview | completed | state-api | demo-source-events v1 |
| [Sailing update preview](update-preview.md) | demo.repo.harbor-web | Merge [a:demo.action.preview-merge]   | origin/main    | pending   | state-api | demo-source-events v1 |
| [Sailing update preview](update-preview.md) | demo.repo.harbor-web | Deploy [a:demo.action.preview-deploy] | sandbox        | pending   | state-api | demo-source-events v1 |

Unresolved legacy state: 1 claim in the [Sailing Schedule topic](sailing-schedule.md#legacy-evidence). It does not identify a release action.

## Detail pages

- [Sailing Schedule scope and explanation](sailing-schedule.md)
- [Sailing update preview feature box](update-preview.md)
- [Full person context](../../person.md)

<!-- /kb -->

Fictional legacy project map bytes.
