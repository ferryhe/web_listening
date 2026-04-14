# web_listening

`web_listening` is a staged website monitoring project for human operators and AI agents.

The current product direction is:

```text
discover -> classify -> select -> task -> bootstrap -> run -> explain -> convert
```

Instead of monitoring a whole site blindly from the homepage, the repo now supports:

- structure-first section discovery
- section classification and scope planning
- bounded tree bootstrap for the selected scope
- later reruns against the same scope
- SHA-256 file dedupe with source-oriented tracked paths
- YAML artifacts for agent handoff plus Markdown reports for human review

Documentation index: [docs/README.md](C:/Project/web_listening/docs/README.md)

## Current Status

What is production-usable now:

- staged planning artifacts:
  - `section_inventory.yaml`
  - `section_classification.yaml`
  - `section_selection.yaml`
  - `monitor_scope.yaml`
  - `monitor_task.yaml`
- scope-driven tree bootstrap and reruns
- page snapshots, page edges, tracked files, and file observations in SQLite
- canonical blob storage under `data/downloads/_blobs`
- source-oriented tracked file views under `data/downloads/_tracked`
- bootstrap summary, tracking report, and document manifest export for AI or operator review

What still remains future-facing:

- exposing the staged tree workflow through REST or the packaged `web-listening` CLI
- persistent jobs and webhooks for long-running tree runs
- richer incremental change bundles and conversion routing

## Install

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -e .[dev]
```

Optional browser support:

```powershell
pip install -e .[browser]
playwright install chromium
```

## Configuration

Copy the template:

```powershell
Copy-Item .env.example .env
```

Important notes:

- `WL_OPENAI_API_KEY` is optional
- tree crawling, downloads, SHA-256 dedupe, and manifest export work without an API key
- the API key is only needed for OpenAI-backed explanation or summary layers

Main settings:

| Variable | Default | Description |
|---|---|---|
| `WL_DATA_DIR` | `./data` | Root data directory |
| `WL_DB_PATH` | `./data/web_listening.db` | SQLite database path |
| `WL_DOWNLOADS_DIR` | `./data/downloads` | Download root |
| `WL_OPENAI_API_KEY` | *(empty)* | Optional OpenAI API key |
| `WL_OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model for optional explanation |
| `WL_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible base URL |
| `WL_USER_AGENT` | `web-listening-bot/1.0` | Default HTTP user agent |
| `WL_REQUEST_TIMEOUT` | `30` | Request timeout in seconds |

## Recommended Workflow

### 1. Discover site sections

```powershell
.venv\Scripts\python tools\discover_site_sections.py --catalog dev
```

This produces a structure-first picture of reachable level-2 sections and sampled level-3 branches.

### 2. Classify sections

```powershell
.venv\Scripts\python tools\classify_site_sections.py --catalog dev
```

This adds business categories and importance hints such as:

- `research_publications`
- `news_announcements`
- `finance_reports`
- `membership_operations`

The current default project posture is conservative:

- recognize `exam_education`
- recognize `governance_management`
- but do not prioritize them unless the monitoring goal explicitly requires them

### 3. Plan the monitoring scope

```powershell
.venv\Scripts\python tools\plan_monitor_scope.py --selection-path data\plans\section_selection_soa_2026-04-07.yaml
```

This compiles the chosen sections into a runnable `monitor_scope.yaml`.

### 4. Bootstrap the selected scope

```powershell
.venv\Scripts\python tools\bootstrap_site_tree.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml --download-files
```

### 5. Export summaries for people and agents

```powershell
.venv\Scripts\python tools\summarize_scope_bootstrap.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
.venv\Scripts\python tools\export_scope_document_manifest.py --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
web-listening create-monitor-task --task-name soa-research-watch --site-url https://example.com --task-description "Track research updates" --goal "Find new pages and downloadable reports"
web-listening export-tracking-report --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
```

### 6. Run later incremental checks

```powershell
.venv\Scripts\python tools\run_site_tree.py --catalog dev --download-files
```

## Data Layout

The repo now uses three layers of artifacts:

- control plane:
  - `data/plans/*.yaml`
- explanation plane:
  - `data/reports/*.md`
- evidence plane:
  - `data/web_listening.db`
  - `data/downloads/_blobs`
  - `data/downloads/_tracked`

File storage rules:

- `_blobs` is the canonical SHA-256 deduped store
- `_tracked` is a source-oriented browsing view
- `documents.local_path` points at the canonical blob path
- `tracked_local_path` points at the source-oriented tracked path
- `preferred_display_path` prefers `_tracked` and falls back to `_blobs`

## Key Outputs

Typical artifacts from the staged workflow:

- `section_inventory_<site>_<date>.yaml`
- `section_classification_<site>_<date>.yaml`
- `section_selection_<site>_<date>.yaml`
- `monitor_scope_<site>_<date>.yaml`
- `monitor_task_<task>_<date>.yaml`
- `tree_bootstrap_scope_<site>_<date>.md`
- `bootstrap_scope_summary_<site>_<date>.md`
- `tracking_report_<site>_<date>.md`
- `tracking_report_<site>_<date>.yaml`
- `document_manifest_<site>_<date>.yaml`

## Legacy Interfaces

The packaged CLI and REST API still exist and are useful for site-level monitoring.
The packaged CLI now also exposes stable local artifact commands for agent workflows:

- `web-listening create-monitor-task`
- `web-listening export-tracking-report`

Legacy site-level commands remain:

- `web-listening add-site`
- `web-listening check`
- `web-listening download-docs`
- `web-listening analyze`
- `web-listening serve`

Current limitation:

- the staged discover/classify/plan/bootstrap/run orchestration still lives in `tools/*.py`
- the new CLI commands stabilize task/report artifacts, but do not replace the tool-driven tree workflow yet

## Validation

Use the live validation commands instead of relying on committed point-in-time snapshots:

```powershell
.venv\Scripts\pytest tests -q
.venv\Scripts\python tools\validate_real_sites.py
.venv\Scripts\python tools\run_dev_regression.py
.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only
.venv\Scripts\python tools\run_tree_catalog_validation.py
.venv\Scripts\python tools\run_agent_rescue_validation.py
```

Validation guide: [docs/validation/README.md](C:/Project/web_listening/docs/validation/README.md)

## Active Docs

Start with:

- [AGENT_SCOPE_PLANNING_DESIGN.md](C:/Project/web_listening/docs/design/AGENT_SCOPE_PLANNING_DESIGN.md)
- [TREE_MONITORING_DESIGN.md](C:/Project/web_listening/docs/design/TREE_MONITORING_DESIGN.md)
- [AGENT_SITE_MONITORING_MASTER_PLAN.md](C:/Project/web_listening/docs/roadmap/AGENT_SITE_MONITORING_MASTER_PLAN.md)
- [SMOKE_SITE_MANAGEMENT.md](C:/Project/web_listening/docs/operations/SMOKE_SITE_MANAGEMENT.md)
- [TREE_BUDGET_RULES.md](C:/Project/web_listening/docs/operations/TREE_BUDGET_RULES.md)
- [OPENCLAW_SKILL_USAGE.md](C:/Project/web_listening/docs/skills/OPENCLAW_SKILL_USAGE.md)
