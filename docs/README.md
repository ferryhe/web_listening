# Docs Index

## Start Here

- [AGENT_SCOPE_PLANNING_DESIGN.md](design/AGENT_SCOPE_PLANNING_DESIGN.md)
- [TREE_MONITORING_DESIGN.md](design/TREE_MONITORING_DESIGN.md)
- [AGENT_SITE_MONITORING_MASTER_PLAN.md](roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md)

## What Is Active

These are the long-lived docs that describe the current system:

- design:
  - [AGENT_SCOPE_PLANNING_DESIGN.md](design/AGENT_SCOPE_PLANNING_DESIGN.md)
  - [TREE_MONITORING_DESIGN.md](design/TREE_MONITORING_DESIGN.md)
- operations:
  - [PYTHON_RUNTIME.md](operations/PYTHON_RUNTIME.md)
  - [BROWSERACT.md](operations/BROWSERACT.md)
  - [DEV_TEST_TARGETS.md](operations/DEV_TEST_TARGETS.md)
  - [SMOKE_SITE_MANAGEMENT.md](operations/SMOKE_SITE_MANAGEMENT.md)
  - [TREE_BUDGET_RULES.md](operations/TREE_BUDGET_RULES.md)
- roadmap:
  - [AGENT_SITE_MONITORING_MASTER_PLAN.md](roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md)
- skills:
  - [OPENCLAW_SKILL_USAGE.md](skills/OPENCLAW_SKILL_USAGE.md)
- validation guide:
  - [validation/README.md](validation/README.md)
- contracts:
  - [site-skill-protocol-v1.md](contracts/site-skill-protocol-v1.md)
  - [acquisition-tools-v1.md](contracts/acquisition-tools-v1.md)
  - [acquisition-profile-v1.md](contracts/acquisition-profile-v1.md)
  - [web-listening-manifest-v1.md](contracts/web-listening-manifest-v1.md)
- testing fixtures:
  - [site-skill-v1.sample.json](testing/fixtures/site-skill-v1.sample.json)
  - [capture-request-v1.sample.json](testing/fixtures/capture-request-v1.sample.json)
  - [capture-result-v1.sample.json](testing/fixtures/capture-result-v1.sample.json)
  - [acquisition-attempt-v2.sample.json](testing/fixtures/acquisition-attempt-v2.sample.json)
  - [acquisition-tools-v1.sample.json](testing/fixtures/acquisition-tools-v1.sample.json)
  - [acquisition-profile-v1.sample.yaml](testing/fixtures/acquisition-profile-v1.sample.yaml)
  - [web-listening-contract-smoke.md](testing/web-listening-contract-smoke.md)
- architecture decisions:
  - [ADR-001-site-skill-protocol-authority.md](architecture/ADR-001-site-skill-protocol-authority.md)

## Current Entry Points

### Acquisition tool picker

- stable delivery picker API: `GET /api/v1/acquisition/tools`
- stable delivery picker CLI: `web-listening list-acquisition-tools --json`

### Primary staged workflow

- canonical entrypoint for agents and operators:
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

`bootstrap-scope` and `run-scope` require `--acquisition-profile-path` and a
complete governed Site Skill binding. The packaged registry is the default;
formal execution never falls through to automatic legacy acquisition.

### Lower-level compatibility entrypoints

- compatibility and developer-oriented wrappers:
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
  - `acquisition_profile_<site_key>_<date>.yaml` (required with a complete governed Site Skill binding for formal `bootstrap-scope` and `run-scope`; unrelated API or MCP integration remains outside this PR; static fixtures/examples may omit the date)
  - `monitor_task_<task>_<date>.yaml`
  - `section_selection_<site>_<date>.yaml`
  - `monitor_scope_<site>_<date>.yaml`
- explanation plane:
  - `tracking_report_<site>_<date>.md`
  - `tracking_report_<site>_<date>.yaml`
- handoff plane:
  - `web_listening_manifest_<site>_<date>.json`
  - `document_manifest_<site>_<date>.yaml`
  - `document_manifest_<site>_<date>.md`

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

These legacy commands still work for site-level monitoring, while the staged tree workflow now lives in the packaged `web-listening` CLI and package-internal workflow modules. The `tools/*.py` scripts remain lower-level compatibility wrappers around that same authority path.

## Archive

- [2026-04-roadmap-history/README.md](archive/2026-04-roadmap-history/README.md)
## Optional BrowserAct adapter

- [BrowserAct runtime policy](operations/BROWSERACT.md)
- [Acquisition tools contract](contracts/acquisition-tools-v1.md)
- [Acquisition profile contract](contracts/acquisition-profile-v1.md)
