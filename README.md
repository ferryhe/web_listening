# web_listening

`web_listening` 1.1 is a governed website-monitoring platform for human operators and AI agents. It discovers site structure, turns reviewed monitoring intent into bounded scopes, captures repeatable evidence, detects later changes, and exports stable machine and human handoff artifacts.

The supported 1.0 scope is complete and stable. Future changes follow the semantic-version policy below. The canonical product flow is:

```text
discover -> classify -> select -> plan-scope -> bootstrap/run -> report/export
```

The packaged `web-listening` CLI is the canonical operator and agent interface. The REST API provides site-level, acquisition, job, execution, report, document, and analysis surfaces, but it does **not** provide full REST parity for the discover/classify/select/plan-scope planning flow.

Version 1.1 adds a producer-only, robots-first diagnostic command for new Agentic planning paths. It does not change the supported 1.0 `discover -> classify -> select -> plan-scope` behavior or grant acquisition authority.

## Robots and sitemap diagnosis

Before a new Agentic planning path proposes discovery or acquisition, run a bounded diagnosis with exact operator-supplied network boundaries:

```bash
web-listening diagnose-site \
  --url https://example.com/news \
  --site-key example \
  --allowed-domain example.com \
  --allowed-document-origin https://example.com \
  --output data/plans/site_diagnostic_example_2026-08-08.json \
  --json
```

`diagnose-site` always normalizes the requested origin and makes its first HTTP request to that canonical origin's `/robots.txt`. It then evaluates declared sitemap locations, or the single same-origin `/sitemap.xml` fallback when robots is absent or declares none. Sitemap indexes are processed FIFO. A cross-origin sitemap document is fetched only when its exact scheme/host/effective port was supplied in `--allowed-document-origin` and that exact origin has first passed its own robots preflight with the same identity. Page `<loc>` values are planning seeds only: the command never fetches them, accepts only canonical-origin page seeds, and records cross-origin pages as requiring a separate diagnosis.

The production transport is browser-free, proxy-free, credential-free, and fail-closed. Every robots, retry, redirect, and sitemap request repeats exact-origin gating and all-address public DNS validation, connects only to the validated address set, preserves the normalized `Host` and HTTPS SNI/certificate hostname, and verifies the actual public peer before sending HTTP request bytes. Canonical public IPv4/IPv6 literals are supported as allowed hosts (IPv6 URL and `Host` authorities remain bracketed); private, loopback, link-local, reserved, multicast, and unspecified literals are rejected before the first HTTP byte. Redirects cannot expand authority or downgrade HTTPS. Governed non-2xx status outcomes—including authority, empty, redirect, retryable, and terminal classes—are decided before body reads. Only 2xx response bodies are streamed under wire and decoded limits with bounded single-member gzip handling; unsafe XML constructs and non-sitemap roots are rejected.

The resulting `site-diagnostic.v1` is planning evidence, not permission, operator review, or an execution profile. Each origin policy includes the selected ordered `Allow`/`Disallow` rules, source line numbers, robots digest, and identity digest; `policy_id` and `policy_sha256` are recomputed from that visible evidence so later consumers can verify and replay the exact matching policy. Accepted page seeds carry their source sitemap queue ordinal, parent document digest, and source entry ordinal. Rejected scheduled sitemap documents likewise retain a normalized URL or raw rejected value, reason, queue ordinal, parent digest, and source entry ordinal; rejected scheduling consumes a bounded request slot without opening the network. Request-slot ordinals and non-secret counted-occurrence lineage let readers recompute the HTTP request, sitemap-document, and URL occurrence usage rather than trusting aggregate counters. Each accepted redirect attempt records its canonical, approved `redirect_target_url`; the next attempt and any redirect-policy rejection are bound to that exact target rather than merely its origin. The contract validates FIFO sitemap evidence, robots-to-root and index-to-child digest lineage, and requires a sitemap-seeded recommendation to contain matching accepted evidence. The artifact's canonical SHA-256 excludes only `artifact_sha256`; readers must verify that digest and freshness. Writes are atomic and idempotent, and refuse to replace a different existing artifact. PR1 will add the separate, digest-bound operator review and diagnosis consumer; 1.1 does not modify `discover`, REST, MCP, scope/profile/Site Skill authority, or the reserved sitemap acquisition adapter.

When `--output` is omitted, the CLI derives a safe filename component from `site_key` and includes the generated `diagnostic_id`, so separate same-day diagnoses do not collide. An explicit `--output` remains no-overwrite and may be repeated only for the byte-identical artifact.

## Product model and authority

The system separates three kinds of input:

- **Monitoring intent and scope**: section inventories, classifications, reviewed selections, monitor tasks, and compiled monitor scopes define what to observe.
- **Acquisition authority**: an `acquisition-profile.v1` defines quality gates, allowed domains, adapter availability, and safety approvals.
- **Site-specific authority**: a versioned `site-skill.v1` package defines governed domains, recipes, executor bindings, scripts, capabilities, and verification rules.

Formal `bootstrap-scope` and `run-scope` execution requires an acquisition profile and the complete six-field Site Skill binding in `monitor_scope.yaml`:

1. `acquisition_profile_id`
2. `site_skill_version`
3. `site_skill_package_sha256`
4. `site_skill_recipe_id`
5. `site_skill_script_sha256`
6. `executor_version`

The package resolves and validates the exact Site Skill, compiles a non-empty `acquisition-execution-plan.v1`, verifies executor capability and runtime policy, and constructs the gateway **before opening Storage or mutating state**. The compiled plan—not picker metadata, a probe result, `fetch_mode`, or `fetch_config_json`—is formal executor authority. Partial governed bindings fail closed. Legacy fetch fields retain compatibility and lineage meaning only.

Packaged Site Skills are discovered and validated statically: registry inspection does not import scripts, execute code, access the network, or resolve DNS. Package versions and SHA-256 digests make the selected authority reproducible.

## Install

Python 3.12.x is required. Create a fresh environment with an approved 3.12 interpreter.

Linux/macOS:

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

Windows PowerShell:

```powershell
py -3.12 --version
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
```

Optional installations are additive:

```bash
# Playwright rendered-browser support
python -m pip install -e ".[browser]"
python -m playwright install chromium

# Explicitly authorized CloakBrowser probing
python -m pip install -e ".[cloakbrowser]"

# MCP stdio server without the development extra
python -m pip install -e ".[mcp]"
```

There is no `core` extra: `python -m pip install .` installs the base package. BrowserAct is not a project dependency and must not be installed in the project environment; see [Version and Runtime Compatibility](#version-and-runtime-compatibility).

## Configuration

Copy `.env.example` to `.env` (`cp .env.example .env` on POSIX or `Copy-Item .env.example .env` in PowerShell).

| Variable | Default | Purpose |
|---|---|---|
| `WL_DATA_DIR` | `./data` | Control, report, and evidence root |
| `WL_DB_PATH` | `./data/web_listening.db` | SQLite database |
| `WL_DOWNLOADS_DIR` | `./data/downloads` | Download storage root |
| `WL_OPENAI_API_KEY` | empty | Optional OpenAI-backed explanation/summary only |
| `WL_OPENAI_MODEL` | `gpt-4o-mini` | Optional explanation model |
| `WL_OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint |
| `WL_USER_AGENT` | `web-listening-bot/1.0` | Default HTTP user agent |
| `WL_REQUEST_TIMEOUT` | `30` | Request timeout in seconds |

Discovery, crawling, downloads, SHA-256 deduplication, reports, and manifests do not require an OpenAI key.

## Quick start

A new catalog must be initialized through review rather than sent directly to production acquisition:

1. Run broad smoke/tree validation to identify reachable, blocked, thin-HTML, or section-seed-sensitive sites.
2. Run discovery and classification.
3. Generate draft selections and scopes, using profiles such as `blocked_hold`, `thin_html_watch`, `section_news`, `section_documents`, or `homepage_standard` where useful.
4. Have an operator review and confirm the monitoring boundary.
5. Bind the confirmed scope to the exact acquisition profile and Site Skill authority.
6. Preview the execution plan, then bootstrap the baseline.
7. Run incremental checks and export reports/manifests.

Draft selection and scope files are review artifacts, not implicit production approval.

After discovery, classification, and operator review have produced a selection, scope, and acquisition profile, use their actual paths in the canonical flow below:

```bash
SITE_KEY=example
RUN_DATE="$(date +%F)"
SELECTION_PATH="data/plans/section_selection_${SITE_KEY}_${RUN_DATE}.yaml"
SCOPE_PATH="data/plans/monitor_scope_${SITE_KEY}_${RUN_DATE}.yaml"
PROFILE_PATH="data/plans/acquisition_profile_${SITE_KEY}_${RUN_DATE}.yaml"

web-listening discover --catalog dev
web-listening classify --catalog dev
web-listening select --selection-path "$SELECTION_PATH"
web-listening plan-scope --selection-path "$SELECTION_PATH" --yaml-path "$SCOPE_PATH"
web-listening preview-execution-plan \
  --scope-path "$SCOPE_PATH" \
  --profile-path "$PROFILE_PATH" \
  --json
web-listening bootstrap-scope \
  --scope-path "$SCOPE_PATH" \
  --acquisition-profile-path "$PROFILE_PATH" \
  --download-files --include-summary
web-listening run-scope \
  --scope-path "$SCOPE_PATH" \
  --acquisition-profile-path "$PROFILE_PATH" \
  --download-files
web-listening report-scope --scope-path "$SCOPE_PATH"
web-listening export-manifest --scope-path "$SCOPE_PATH"
```

`SITE_KEY`, `RUN_DATE`, and all three paths above are templates: replace them with the artifacts generated and approved for the target site. Use `build-acquisition-profile --site-key ... --allowed-domain ... --output "$PROFILE_PATH"` to create the draft profile before review and Site Skill binding.

Use `web-listening COMMAND --help` for complete options. The lower-level `tools/*.py` programs remain compatibility/developer wrappers, not a second product authority.

## Full CLI inventory

### Canonical staged workflow

- `discover` — inventory reachable site sections.
- `classify` — attach categories and priority hints.
- `select` — inspect a reviewed section selection.
- `plan-scope` — compile a selection into a monitor scope.
- `bootstrap-scope` — create a governed baseline for the selected scope.
- `run-scope` — perform a later governed change-detection run.
- `report-scope` — produce a scope tracking report.
- `export-manifest` — export document and `web-listening-manifest.v1` handoff artifacts.
- `create-monitor-task` — create the monitoring-intent artifact.
- `export-tracking-report` — export tracking output from stored evidence.

### Governance and acquisition

- `diagnose-site` — probe robots.txt first and emit bounded, digest-verifiable sitemap planning evidence; it does not run discovery or grant authority.
- `list-site-skills`, `inspect-site-skill`, `validate-site-skill` — statically inspect governed Site Skill packages.
- `list-acquisition-tools` — return the stable acquisition picker catalog.
- `build-acquisition-profile` — create a reviewed profile input.
- `probe-acquisition` — collect one adapter probe as evidence; it does not grant formal run authority.
- `preview-execution-plan` — compile and inspect formal authority without executing it.
- `inspect-browseract` — perform the isolated, read-only BrowserAct identity/capability handshake.

### Jobs and delivery

- `list-jobs`, `get-job` — inspect persisted job state, progress, artifacts, and delivery envelopes.

### Site-level compatibility and service

- `add-site`, `list-sites` — manage monitored sites.
- `check`, `list-changes` — run/check site-level monitoring and inspect changes.
- `download-docs`, `list-docs` — acquire and inspect documents.
- `analyze` — generate analysis from stored evidence.
- `serve` — run the FastAPI service.

## MCP server

Install the `mcp` extra and run `web-listening-mcp` for stdio transport. The server exposes exactly ten thin wrappers around shared package services:

1. `web_listening_list_acquisition_tools`
2. `web_listening_probe_tool_once`
3. `web_listening_recommend_next_tool`
4. `web_listening_acquire_with_fallback`
5. `web_listening_bootstrap_scope`
6. `web_listening_run_scope`
7. `web_listening_report_scope`
8. `web_listening_export_manifest`
9. `web_listening_get_job`
10. `web_listening_read_artifact`

MCP tool responses use the stable `web-listening-tool-result.v1` envelope where applicable. Acquisition fallback and recommendation surfaces remain bounded by profile quality and safety rules; they do not supersede governed scope authority.

## REST API

Run `web-listening serve`; routes are under `/api/v1`. Current API groups are:

- **Acquisition**: tool catalog, default profile building, one-off probes, and execution-plan preview.
- **Sites**: create/list/get/deactivate sites, latest snapshots, rescue checks, and queued checks.
- **Jobs and delivery**: monitor-task creation, job status/payload retrieval, and a job-delivery webhook registration stub.
- **Scoped execution and artifacts**: bootstrap/run/report jobs plus latest report and manifest retrieval for stored scopes.
- **Evidence and analysis**: changes, documents, document-content updates/downloads, analysis creation, and analysis listing.

The CLI remains canonical for `discover`, `classify`, `select`, and `plan-scope`; do not infer full planning REST parity from the scoped execution routes.

## Stable schemas and artifacts

Stable machine contracts in the current surface include:

- `site-diagnostic.v1` (additive in 1.1; producer-only planning evidence)
- `site-skill.v1`
- `capture-request.v1`
- `capture-result.v1`
- `acquisition-attempt.v2`
- `acquisition-profile.v1`
- `acquisition-tools.v1`
- `acquisition-probe.v1`
- `acquisition-execution-plan.v1` and `acquisition-execution-plan-preview.v1`
- `acquisition-evidence.v1`
- `web-listening-manifest.v1`
- `web-listening-tool-result.v1`
- `artifact_contract.v1` and `job_delivery.v1`
- `site-skill-list.v1`, `site-skill-inspect.v1`, and `site-skill-validation.v1`
- `browseract-inspection.v1`

Canonical machine-readable examples remain active under `docs/testing/fixtures/`:

- [site-skill-v1.sample.json](docs/testing/fixtures/site-skill-v1.sample.json)
- [capture-request-v1.sample.json](docs/testing/fixtures/capture-request-v1.sample.json)
- [capture-result-v1.sample.json](docs/testing/fixtures/capture-result-v1.sample.json)
- [acquisition-attempt-v2.sample.json](docs/testing/fixtures/acquisition-attempt-v2.sample.json)
- [acquisition-profile-v1.sample.yaml](docs/testing/fixtures/acquisition-profile-v1.sample.yaml)
- [acquisition-tools-v1.sample.json](docs/testing/fixtures/acquisition-tools-v1.sample.json)
- [acquisition-execution-plan-v1.sample.json](docs/testing/fixtures/acquisition-execution-plan-v1.sample.json)
- [web-listening-manifest-v1.sample.json](docs/testing/fixtures/web-listening-manifest-v1.sample.json)
- [site-diagnostic-v1.sample.json](docs/testing/fixtures/site-diagnostic-v1.sample.json)

Typical durable workflow artifacts are:

- control: `section_inventory_<site>_<date>.yaml`, `section_classification_<site>_<date>.yaml`, `section_selection_<site>_<date>.yaml`, `monitor_scope_<site>_<date>.yaml`, `monitor_task_<task>_<date>.yaml`, `acquisition_profile_<site>_<date>.yaml`
- evidence: SQLite page snapshots/edges, tracked files and observations, `capture-attempt.v1` compatibility records, and acquisition evidence
- reports: `tree_bootstrap_scope_<site>_<date>.md`, `bootstrap_scope_summary_<site>_<date>.md`, `tracking_report_<site>_<date>.md` or `.yaml`
- handoff: `web_listening_manifest_<site>_<date>.json`, `document_manifest_<site>_<date>.yaml` or `.md`

## Storage, safety, and initialization rules

Data is split into three planes:

- control: `data/plans/*.yaml`
- explanation: `data/reports/*.md` and report YAML
- evidence: `data/web_listening.db` and `data/downloads/`

Storage rules:

- `data/downloads/_blobs` is the canonical SHA-256-addressed deduplicated store.
- `data/downloads/_tracked` is a source-oriented browsing view.
- `documents.local_path` points to the canonical blob; `tracked_local_path` points to the source view.
- `preferred_display_path` prefers the tracked view and falls back to the blob.
- Preserve `scope_id`, `run_id`, source/final URLs, timestamps, executor and Site Skill lineage, and SHA-256 values in agent-facing output.

Safety rules:

- Keep scopes bounded with explicit/effective `max_depth`, `max_pages`, and `max_files`; never expand a whole site blindly.
- Profile domains must be a non-empty subset of the governed Site Skill domains.
- Stealth or authorization-requiring executors need explicit profile approvals and runtime availability checks.
- CloakBrowser is optional and only for explicitly authorized probing or governed execution; it may download a browser binary on first launch.
- BrowserAct is disabled by default, excluded from automatic fallback, isolated from the project environment, and limited to its validated read-only contract.
- Treat acquisition picker/probe results as planning evidence, never as permission to bypass the reviewed scope, profile, Site Skill, or compiled plan.
- A bootstrap creates the baseline; a later run performs change detection.

## Version and Runtime Compatibility

Compatibility inventory last reviewed on **2026-08-07**.

| Component | Compatibility policy / observation |
|---|---|
| `web-listening` | `1.1.0` |
| Python | Declared `>=3.12,<3.13`; verified with 3.12.3 |
| FastAPI | Project environment verified at 0.139.2 |
| MCP | Declared `>=1.28.1,<2.0.0`; verified at 1.28.1; 2.x is not qualified |
| BrowserAct | Exact isolated contract `browser-act-cli==1.0.6`; latest observed 1.0.6 |
| Playwright | Declared `>=1.52.0`; external host observed 1.59.0; latest observed 1.61.0 |
| CloakBrowser | Declared `>=0.3.26`; external host observed 0.3.27; latest observed 0.4.12 |

External-host and latest-version observations are inventory signals, **not compatibility certification**. Do not raise lower bounds or upgrade deployed runtimes from those observations alone. Every upgrade requires qualification in an isolated environment, focused adapter/contract tests, the full project suite, and a rollback decision.

The `dev` and `mcp` extras both declare `mcp>=1.28.1,<2.0.0`. The stdio server uses the qualified MCP 1.x `FastMCP` API; MCP 2.x must not be installed until it is separately qualified.

BrowserAct has an exact isolated contract: it must run from a separate Python 3.12 tool environment, must not be added to project dependencies, and must pass `web-listening inspect-browseract --json` as version 1.0.6 with the expected read-only capabilities. Playwright and CloakBrowser remain optional extras governed by their declared minimums and runtime safety rules.

### Semantic-version decision rubric

- **Patch (`1.0.x`)**: backward-compatible bug, security, documentation, or packaging correction; no intended contract or workflow expansion.
- **Minor (`1.x.0`)**: backward-compatible capability, command/field/tool addition, optional integration, or additive schema evolution.
- **Major (`x.0.0`)**: incompatible CLI/API/MCP/schema/artifact/storage behavior, changed authority semantics, removed supported behavior, or a runtime/dependency change that requires consumer migration.

Dependency qualification can trigger any level: use patch only when the supported contract is unchanged, minor for additive newly supported runtime capability, and major when consumers or persisted artifacts must migrate.

### Weekly review policy

Once each week, maintainers review the project version, supported Python range, resolved project-environment versions, optional-runtime observations, upstream security/release notes, and available latest versions. Record whether each change is **observe**, **qualify**, **adopt**, **defer**, or **reject**. Adoption requires the qualification gates above and an explicit SemVer decision; the weekly review does not automatically modify dependency bounds.

## Validation

From the project environment:

```bash
python -m pytest tests -q
python tools/validate_real_sites.py
python tools/run_dev_regression.py
python tools/run_smoke_site_catalog.py --report-only
python tools/run_tree_catalog_validation.py
python tools/run_agent_rescue_validation.py
web-listening --help
```

Network/live catalog commands should be run only in an authorized environment. Contract and package checks should use the committed fixtures and offline test suite first.

## Documentation and archive policy

This root `README.md` is the sole active human-facing product document. Executable Markdown assets (`AGENTS.md`, `.codex/**/SKILL.md`, `skills/**/SKILL.md`, and packaged Site Skill `SKILL.md` files) remain active as runtime/agent instructions, not parallel product documentation. Machine-readable fixtures under `docs/testing/fixtures/` remain active contracts/examples.

Historical designs, plans, reports, contract prose, and operations notes are retained under `docs/archive/` for provenance only. They may contain superseded paths, versions, limitations, or future-phase language and are not current authority. The prior April roadmap history remains unchanged under `docs/archive/2026-04-roadmap-history/`; the final consolidation snapshot is under `docs/archive/2026-07-readme-consolidation/`. New product guidance must update this README rather than create another active prose document under `docs/`.
