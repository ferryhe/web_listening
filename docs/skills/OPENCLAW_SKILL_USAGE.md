# OpenClaw Skill Usage

- workspace skill path: `skills/web-listening-tree-monitor`
- main file: `skills/web-listening-tree-monitor/SKILL.md`
- UI metadata: `skills/web-listening-tree-monitor/agents/openai.yaml`

## What The Skill Is For

This workspace skill is for the staged tree-monitoring workflow, not just root-page smoke checks.

The expected OpenClaw exchange pattern is:

- YAML for planning and handoff
- SQLite plus downloads for evidence
- Markdown for human review

## Recommended Artifact Chain

OpenClaw should move through these local artifacts:

1. `section_inventory.yaml`
2. `section_classification.yaml`
3. `section_selection.yaml`
4. `monitor_scope.yaml`
5. bootstrap summary and document manifest

That means the agent can resume work from files on disk instead of relying on prompt memory.

## Recommended Commands

```powershell
.venv\Scripts\python -m web_listening.cli discover --catalog dev
.venv\Scripts\python -m web_listening.cli classify --catalog dev
.venv\Scripts\python -m web_listening.cli plan-scope --selection-path data\plans\section_selection_soa_2026-04-07.yaml
.venv\Scripts\python -m web_listening.cli bootstrap-scope --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml --download-files
.venv\Scripts\python -m web_listening.cli report-scope --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
.venv\Scripts\python -m web_listening.cli export-manifest --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
```

## Compatibility Commands

Lower-level wrappers remain available when direct tool entry is needed:

```powershell
.venv\Scripts\python tools\discover_site_sections.py --catalog dev
.venv\Scripts\python tools\classify_site_sections.py --catalog dev
.venv\Scripts\python tools\plan_monitor_scope.py --selection-path data\plans\section_selection_soa_2026-04-07.yaml
.venv\Scripts\python tools\bootstrap_site_tree.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml --download-files
.venv\Scripts\python tools\summarize_scope_bootstrap.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
.venv\Scripts\python tools\export_scope_document_manifest.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
```

## Current Caveat

The staged tree workflow is now exposed as a first-class packaged CLI workflow.
REST still focuses on the older site-level monitoring surface.
