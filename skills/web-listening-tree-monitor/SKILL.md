---
name: web_listening_tree_monitor
description: Discover, classify, scope, bootstrap, and rerun bounded site-tree monitoring in this repository.
---

# Web Listening Tree Monitor

Use this skill when the task is to operate the staged tree-monitoring workflow in this repo.

The repo now has two stable artifact commands in the packaged CLI:
- `web-listening create-monitor-task`
- `web-listening export-tracking-report`

## Read First

1. `README.md`
2. `docs/design/AGENT_SCOPE_PLANNING_DESIGN.md`
3. `docs/design/TREE_MONITORING_DESIGN.md`
4. `docs/roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md`

## Core Workflow

### First-time initialization workflow

For a brand-new catalog or a newly imported multi-site list, the skill should **not** jump directly to bootstrap.
The correct initialization flow is:

1. run a broad smoke or tree-validation pass across the full list
2. summarize what is reachable, blocked, thin HTML, or section-seed-sensitive
3. convert that result into **suggested scope profiles** such as:
   - `blocked_hold`
   - `thin_html_watch`
   - `section_news`
   - `section_documents`
   - `homepage_standard`
4. generate draft `section_selection.yaml` and `monitor_scope.yaml` artifacts for each site
5. send those draft scope artifacts to a human for confirmation or adjustment
6. only after the human confirms the scope, run bootstrap and later reruns

This rule exists because the first pass is meant to define the monitoring boundary, not just prove that a crawler can fetch a homepage.
For large lists, draft scope artifacts are **review artifacts first** and only become production monitoring artifacts after confirmation.

### Standard staged workflow after scope confirmation

Primary path:

1. Discover site structure:
   - `web-listening discover --catalog dev`
2. Classify discovered sections:
   - `web-listening classify --catalog dev`
3. Review or edit `section_selection.yaml`.
4. Compile the monitoring scope:
   - `web-listening plan-scope --selection-path data\\plans\\section_selection_soa_2026-04-07.yaml`
5. Bootstrap the selected scope:
   - `web-listening bootstrap-scope --scope-path data\\plans\\monitor_scope_soa_2026-04-07.yaml --download-files`
6. Export human and agent summaries:
   - `web-listening report-scope --scope-path data\\plans\\monitor_scope_soa_2026-04-07.yaml`
   - `web-listening export-manifest --scope-path data\\plans\\monitor_scope_soa_2026-04-07.yaml`
7. Run later reruns:
   - `web-listening run-scope --scope-path data\\plans\\monitor_scope_soa_2026-04-07.yaml --download-files`

Compatibility path:

- The older `tools/*.py` scripts remain available as lower-level wrappers around the same package workflow modules.
- Use them only when a downstream integration still expects those legacy entrypoints.

## Important Data Rules

- `_blobs` is the canonical SHA-256 deduped file store.
- `_tracked` is the source-oriented browsing view.
- `preferred_display_path` should be treated as the best default file path for agents.
- `downloaded_at`, `page_url`, `download_url`, and `sha256` should stay visible in agent-facing outputs.

## Guardrails

- Do not expand the whole site blindly when the planning workflow is available.
- Treat `bootstrap_site_tree.py` as baseline creation, not alert generation.
- Treat `run_site_tree.py` as the change-detection step.
- Keep recursion bounded with explicit `max_depth`, `max_pages`, and `max_files`.
- Preserve evidence pointers in reports and manifests.

## Current Limitation

The staged tree workflow is now exposed as a first-class packaged `web-listening` CLI workflow, with package-internal workflow modules as the execution authority.
Some lower-level scripts still exist for compatibility, but they are no longer the primary product path.
