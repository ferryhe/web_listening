---
name: web_listening_tree_monitor
description: Bootstrap and run bounded recursive tree monitoring in this repository, using the built-in dev and smoke target catalogs to create page and file baselines, detect later changes, and generate dated reports.
---

# Web Listening Tree Monitor

Use this skill when the task is to create or operate recursive site-tree monitoring in this repo.

## Workflow

1. Read `README.md` for the main repo entry points.
2. Read `docs/design/TREE_MONITORING_DESIGN.md` before changing tree scope behavior.
3. Read `docs/roadmap/TREE_MONITORING_DELIVERY_PLAN.md` for the current rollout sequence.
4. Bootstrap first:
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --catalog dev`
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --catalog smoke`
5. Run later incremental checks:
   - `.venv\Scripts\python tools\run_site_tree.py --catalog dev`
   - `.venv\Scripts\python tools\run_site_tree.py --catalog smoke`
6. Read the dated reports in `data/reports/`.

## Guardrails

- Treat `bootstrap_site_tree.py` as baseline creation, not alert generation.
- Treat `run_site_tree.py` as the change-detection step.
- Keep recursion bounded with explicit `max_depth`, `max_pages`, and `max_files`.
- Preserve evidence: keep scope IDs, run IDs, page URLs, file URLs, and SHA-256 values visible in reports.
- Prefer the shared target loaders in `web_listening/tree_targets.py` instead of hardcoding site lists.
