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

1. Discover site structure:
   - `.venv\Scripts\python tools\discover_site_sections.py --catalog dev`
2. Classify the discovered sections:
   - `.venv\Scripts\python tools\classify_site_sections.py --catalog dev`
3. Review or edit `section_selection.yaml`.
4. Compile the monitoring scope:
   - `.venv\Scripts\python tools\plan_monitor_scope.py --selection-path data\plans\section_selection_soa_2026-04-07.yaml`
5. Bootstrap the selected scope:
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml --download-files`
6. Export human and agent summaries:
   - `.venv\Scripts\python tools\summarize_scope_bootstrap.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml`
   - `.venv\Scripts\python tools\export_scope_document_manifest.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml`
7. Run later reruns:
   - `.venv\Scripts\python tools\run_site_tree.py --catalog dev --download-files`

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

The staged tree workflow is implemented through `tools/*.py`.
It is not yet exposed as a first-class REST or packaged `web-listening` CLI workflow.
