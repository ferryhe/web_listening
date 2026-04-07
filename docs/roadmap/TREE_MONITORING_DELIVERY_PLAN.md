# Tree Monitoring Delivery Plan

## Goal

Turn `web_listening` from entry-page monitoring into bounded recursive site-tree monitoring that can:

- bootstrap a site tree from one seed URL
- persist page and file inventories in the main SQLite database
- compare later runs against the bootstrap baseline
- report new pages, changed pages, missing pages, new files, changed files, and missing files
- optionally download files when file discovery or file changes matter

## Current Status

- `tools/bootstrap_site_tree.py` bootstraps tree scopes for the `dev`, `smoke`, or `all` target sets.
- `tools/run_site_tree.py` runs incremental comparisons against initialized scopes.
- `web_listening/blocks/tree_crawler.py` now supports both bootstrap and incremental runs.
- `web_listening/tree_targets.py` normalizes the 3 development targets and the 30+ smoke targets into one tree-target model.

## Delivery Phases

### Phase 1: Bootstrap baseline

- done: persist tree scopes and bootstrap runs in the main database
- done: support the 3 development sites and the 30+ smoke catalog
- done: write dated Markdown reports for bootstrap runs

### Phase 2: Incremental monitoring

- done: compare later runs against stored page hashes and tracked file SHA-256 values
- done: report new, changed, and missing pages/files
- next: add miss-count aging and inactive-state handling for disappeared pages/files

### Phase 3: Operator clarity

- done: move root strategy and validation docs into `docs/`
- done: add dated daily and tree reports
- next: expose the tree commands through the main CLI and/or REST API

### Phase 4: Agent skill packaging

- done: keep the repo skill current for Codex
- next: publish a workspace `skills/` version that OpenClaw can load directly
- next: keep both skill variants aligned to the same bootstrap/run workflow

## Recommended Operating Sequence

1. Bootstrap development tree scopes:
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --catalog dev`
2. Bootstrap broader smoke scopes:
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --catalog smoke`
3. Wait for the next observation window.
4. Run incremental comparison:
   - `.venv\Scripts\python tools\run_site_tree.py --catalog dev`
   - `.venv\Scripts\python tools\run_site_tree.py --catalog smoke`
5. Review the dated reports under `data/reports/`.

## Next Implementation Priorities

- add tree-run output to REST or CLI
- add report links to the skill instructions
- add inactive/missing-item aging
- add richer per-change evidence snippets for changed pages and changed files
