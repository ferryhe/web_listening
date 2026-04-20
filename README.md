# web_listening

`web_listening` is a staged website monitoring project for human operators and AI agents.

The current product direction is:

```text
discover -> classify -> select -> task -> bootstrap -> run -> report -> manifest
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

- packaged staged workflow commands:
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
- staged planning artifacts:
  - `section_selection.yaml`
  - `monitor_scope.yaml`
  - `monitor_task.yaml`
- scope-driven tree bootstrap and reruns
- page snapshots, page edges, tracked files, and file observations in SQLite
- canonical blob storage under `data/downloads/_blobs`
- source-oriented tracked file views under `data/downloads/_tracked`
- bootstrap summary, bootstrap quality summary, tracking report, and document manifest export for AI or operator review

What still remains future-facing:

- persistent jobs and webhooks for longer-running or external delivery workflows
- richer incremental change bundles and conversion routing
- REST/API expansion beyond the current staged workflow and compatibility surfaces

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

The packaged `web-listening` CLI is now the primary entrypoint for the staged tree workflow.
The older `tools/*.py` scripts still exist, but they should be treated as lower-level compatibility entrypoints.

### First-time initialization rule

For a new site list or a newly imported catalog, **do not jump straight into bootstrap or recurring runs**.
The initialization sequence should always be:

1. run a broad smoke or tree-validation pass to understand what is reachable and what kind of site shape we are dealing with
2. classify the sites into suggested scope profiles such as `blocked_hold`, `thin_html_watch`, `section_news`, `section_documents`, or `homepage_standard`
3. generate draft `section_selection.yaml` and `monitor_scope.yaml` artifacts from those suggestions
4. send the draft scope artifacts to a human operator for confirmation or adjustment
5. only after that confirmation, run `bootstrap-scope`, later `run-scope`, and then generate tracking reports

Why this matters:

- first-pass smoke results are often enough to tell us the site is blocked, thin HTML, or should start from a better section seed
- the correct monitoring boundary is a product decision, not just a crawler default
- generating a draft scope first avoids wasting bootstrap runs on the wrong homepage or the wrong subtree

### Suggested initialization flow for large catalogs

For large lists such as the 30+ or 37-site smoke catalog, the expected sequence is:

```text
smoke / tree validation -> suggested scope profiles -> draft section_selection + monitor_scope -> human review -> bootstrap -> rerun -> tracking report
```

The draft scope artifacts are therefore **review artifacts**, not final production monitoring state.
They become production-ready only after explicit confirmation.

### 1. Discover site sections

```powershell
web-listening discover --catalog dev
```

This produces a structure-first picture of reachable level-2 sections and sampled level-3 branches.

### 2. Classify sections

```powershell
web-listening classify --catalog dev
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

### 3. Review the section selection artifact

```powershell
web-listening select --selection-path data\plans\section_selection_soa_2026-04-07.yaml
```

This surfaces the reviewed selection artifact clearly before scope compilation.

### 4. Plan the monitoring scope

```powershell
web-listening plan-scope --selection-path data\plans\section_selection_soa_2026-04-07.yaml
```

This compiles the chosen sections into a runnable `monitor_scope.yaml`.

### 5. Bootstrap the selected scope

```powershell
web-listening bootstrap-scope --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml --download-files --include-summary
```

`bootstrap-scope` now supports an additional bootstrap summary output with baseline quality signals, including coverage, budget truncation hints, confidence, and recommended follow-up actions.

### 6. Export summaries for people and agents

```powershell
web-listening report-scope --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
web-listening export-manifest --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml
web-listening create-monitor-task --task-name soa-research-watch --site-url https://example.com --task-description "Track research updates" --goal "Find new pages and downloadable reports"
```

### 7. Run later incremental checks

```powershell
web-listening run-scope --scope-path data\plans\monitor_scope_soa_2026-04-07.yaml --download-files
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

Current status:

- the packaged `web-listening` CLI now exposes the staged discover/classify/select/plan/bootstrap/run/report/manifest flow directly
- the lower-level `tools/*.py` scripts still exist as compatibility entrypoints and developer-oriented wrappers around the same workflow

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
