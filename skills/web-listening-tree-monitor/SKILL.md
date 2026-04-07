---
name: web_listening_tree_monitor
description: Bootstrap and run bounded recursive tree monitoring in this repository, using the built-in dev and smoke target catalogs to create page and file baselines, detect later changes, and generate dated reports.
---

# Web Listening Tree Monitor

Use this skill when the task is to create or operate recursive site-tree monitoring in this repo.

## Workflow

1. Read `README.md` for the main repo entry points.
2. Read `docs/design/TREE_MONITORING_DESIGN.md` before changing tree scope behavior.
3. Read `docs/design/AGENT_SCOPE_PLANNING_DESIGN.md` and `docs/roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md` for the current staged rollout direction.
4. Discover sections first when planning a new monitoring scope:
   - `.venv\Scripts\python tools\discover_site_sections.py --catalog dev`
   - `.venv\Scripts\python tools\discover_site_sections.py --catalog smoke`
   - Discovery now defaults to depth `3`, no document detection, full level-2 coverage, and adaptive level-3 sampling so the first artifact is a smarter section tree picture.
5. Classify discovered sections before selecting monitoring scope:
   - `.venv\Scripts\python tools\classify_site_sections.py --catalog dev`
   - `.venv\Scripts\python tools\classify_site_sections.py --catalog smoke`
6. Compile the reviewed section selection into a runnable scope:
   - `.venv\Scripts\python tools\plan_monitor_scope.py --selection-path data/plans/section_selection_soa_2026-04-07.yaml`
7. Bootstrap after scope planning:
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --catalog dev --download-files`
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --catalog smoke --download-files`
   - `.venv\Scripts\python tools\bootstrap_site_tree.py --scope-path data/plans/monitor_scope_soa_2026-04-07.yaml --download-files`
   - `.venv\Scripts\python tools\summarize_scope_bootstrap.py --scope-path data/plans/monitor_scope_soa_2026-04-07.yaml`
8. Run later incremental checks:
   - `.venv\Scripts\python tools\run_site_tree.py --catalog dev --download-files`
   - `.venv\Scripts\python tools\run_site_tree.py --catalog smoke --download-files`
9. Explain the first baseline when you need a human/agent interpretation:
   - `.venv\Scripts\python tools\explain_tree_bootstrap.py --catalog dev`
   - `.venv\Scripts\python tools\explain_tree_bootstrap.py --catalog smoke`
10. Read the dated reports in `data/reports/` and the planning YAML in `data/plans/`.

The current script defaults are production-oriented for whole-site monitoring:

- `max_depth=4`
- `max_pages=120`
- `max_files=40`

## Guardrails

- Treat `bootstrap_site_tree.py` as baseline creation, not alert generation.
- Treat `run_site_tree.py` as the change-detection step.
- Keep recursion bounded with explicit `max_depth`, `max_pages`, and `max_files`.
- Preserve evidence: keep scope IDs, run IDs, page URLs, file URLs, and SHA-256 values visible in reports.
- Prefer the shared target loaders in `web_listening/tree_targets.py` instead of hardcoding site lists.
