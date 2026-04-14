# Docs Index

## Start Here

- [AGENT_SCOPE_PLANNING_DESIGN.md](C:/Project/web_listening/docs/design/AGENT_SCOPE_PLANNING_DESIGN.md)
- [TREE_MONITORING_DESIGN.md](C:/Project/web_listening/docs/design/TREE_MONITORING_DESIGN.md)
- [AGENT_SITE_MONITORING_MASTER_PLAN.md](C:/Project/web_listening/docs/roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md)

## What Is Active

These are the long-lived docs that describe the current system:

- design:
  - [AGENT_SCOPE_PLANNING_DESIGN.md](C:/Project/web_listening/docs/design/AGENT_SCOPE_PLANNING_DESIGN.md)
  - [TREE_MONITORING_DESIGN.md](C:/Project/web_listening/docs/design/TREE_MONITORING_DESIGN.md)
- operations:
  - [DEV_TEST_TARGETS.md](C:/Project/web_listening/docs/operations/DEV_TEST_TARGETS.md)
  - [SMOKE_SITE_MANAGEMENT.md](C:/Project/web_listening/docs/operations/SMOKE_SITE_MANAGEMENT.md)
  - [TREE_BUDGET_RULES.md](C:/Project/web_listening/docs/operations/TREE_BUDGET_RULES.md)
- roadmap:
  - [AGENT_SITE_MONITORING_MASTER_PLAN.md](C:/Project/web_listening/docs/roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md)
- skills:
  - [OPENCLAW_SKILL_USAGE.md](C:/Project/web_listening/docs/skills/OPENCLAW_SKILL_USAGE.md)
- validation guide:
  - [validation/README.md](C:/Project/web_listening/docs/validation/README.md)

## Current Entry Points

### Primary staged workflow

- `web-listening discover`
- `web-listening classify`
- `web-listening select`
- `web-listening plan-scope`
- `web-listening bootstrap-scope`
- `web-listening run-scope`
- `web-listening report-scope`
- `web-listening export-manifest`
- `web-listening list-jobs`
- `web-listening get-job`
- `web-listening create-monitor-task`
- `web-listening export-tracking-report`

### Lower-level compatibility entrypoints

- `tools/discover_site_sections.py`
- `tools/classify_site_sections.py`
- `tools/plan_monitor_scope.py`
- `tools/bootstrap_site_tree.py`
- `tools/summarize_scope_bootstrap.py`
- `tools/export_scope_document_manifest.py`
- `tools/explain_tree_bootstrap.py`
- `tools/run_site_tree.py`

## New Stable Artifacts

- control plane:
  - `monitor_task_<task>_<date>.yaml`
  - `section_selection_<site>_<date>.yaml`
  - `monitor_scope_<site>_<date>.yaml`
- explanation plane:
  - `tracking_report_<site>_<date>.md`
  - `tracking_report_<site>_<date>.yaml`

## Initialization Rule For New Site Lists

For a newly imported site list, the expected flow is:

1. run smoke and tree validation across the full catalog
2. generate suggested scope profiles and draft `section_selection` / `monitor_scope` artifacts
3. send the draft scope artifacts for human confirmation
4. only after confirmation, run bootstrap, reruns, and tracking reports

This is the default initialization rule for both human operators and AI agents.
Draft scope artifacts are review-stage outputs, not automatically approved production scopes.

## Legacy Entry Points

- `web-listening add-site`
- `web-listening check`
- `web-listening download-docs`
- `web-listening analyze`
- `web-listening serve`

These legacy commands still work for site-level monitoring, but the staged tree workflow currently lives in `tools/*.py`.

## Archive

- [2026-04-roadmap-history/README.md](C:/Project/web_listening/docs/archive/2026-04-roadmap-history/README.md)
