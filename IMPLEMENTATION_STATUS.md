# Implementation Status

> Branch: `docs/ai-agent-roadmap`  
> Last updated: 2026-04-03

## Completed in this branch

### 1. Normalized snapshot artifacts

- Added `raw_html`, `cleaned_html`, `markdown`, `fit_markdown`, `metadata_json`, `fetch_mode`, `final_url`, and `status_code` to `SiteSnapshot`.
- Switched change comparison to prefer `fit_markdown`, then `markdown`, then legacy `content_text`.
- Added `GET /api/v1/sites/{id}/snapshots/latest` so agents and traditional clients can read the latest normalized snapshot directly.

Reference commit:

- `6c1286c` `Add normalized snapshot artifacts`

### 2. Document Markdown handoff state

- Kept document conversion out of `web_listening`.
- Added `content_md_status` and `content_md_updated_at` to `Document`.
- Added `PATCH /api/v1/documents/{id}/content` so an external AI agent or `doc_to_md` workflow can write Markdown back after conversion.

Reference commit:

- `0681d4d` `Add document markdown handoff status`

### 3. Browser-ready fetch mode support

- Added `Site.fetch_mode` and `Site.fetch_config_json`.
- Added `HttpCrawler`, `BrowserCrawler`, and a dispatching `Crawler`.
- Kept `http` as the default.
- Added optional `browser` dependency in `pyproject.toml` for Playwright-based crawling.

Reference commit:

- `35d5280` `Add browser-ready site fetch modes`

### 4. Main-content-first normalization

- Tightened HTML normalization to prefer `main`, `article`, `#content`, or `role=main`.
- Reduced navigation noise on real institutional homepages, especially `soa.org`.

Reference commit:

- `e193124` `Prefer main content in HTML normalization`

### 5. Required live dev-site regression

- Added canonical required development targets for `SOA`, `CAS`, and `IAA`.
- Added `config/dev_test_sites.json` as the single source of truth for live regression targets.
- Added a live validation script and a live regression script that cover monitoring, hash stability, document discovery, and sample downloads.
- Tightened content hashing so whitespace-only differences do not create noisy SHA-256 changes.

Reference commit:

- `b85f024` `Add required dev-site regression scripts`

### 6. Dev-target validation and SHA strategy

- Added `web_listening/dev_targets.py` so scripts and tests validate the required target set instead of trusting ad-hoc JSON shape.
- Added minimum word-count thresholds per live target to catch empty or broken extractions earlier.
- Added repeat-download SHA-256 checks so sample downloads prove both byte stability and blob-path reuse.
- Added snapshot hash metadata so reports now show the hash basis and normalization strategy.

Reference commit:

- `e379f84` `Harden dev-target regression and hashing`

### 7. List-driven smoke catalog groundwork

- Added site-level HTTP user-agent overrides so selected targets can use a browser-like UA without changing the global default.
- Added `config/smoke_site_catalog.json` as the tracked curated smoke target list for the current supranational organization spreadsheet.
- Added `web_listening/smoke_sites.py` and `tools/run_smoke_site_catalog.py` so larger site lists can be validated without hardcoding spreadsheet logic into the runner.
- Added `SMOKE_SITE_MANAGEMENT.md` to document how raw ignored inputs should flow into tracked monitor targets.
- Recorded the first smoke validation baseline in `SMOKE_SITE_VALIDATION.md`.

### 8. Recursive tree monitoring design

- Added `TREE_MONITORING_DESIGN.md` to capture the next-step architecture for bounded recursive monitoring.
- Split the proposed recursive boundary into `page scope` and `file scope`, so centralized file storage can still be tracked without opening unrestricted page recursion.
- Kept byte-level SHA-256 as the final dedupe authority for downloaded files, even when files are discovered from multiple pages.

### 9. Recursive tree bootstrap scaffold

- Added bounded recursive bootstrap storage for `crawl_scopes`, `crawl_runs`, `tracked_pages`, `page_snapshots`, `page_edges`, `tracked_files`, and `file_observations`.
- Added `web_listening/blocks/tree_crawler.py` with BFS-based bootstrap, page snapshot persistence, page-edge recording, and same-origin file tracking.
- Split request-URL sanitation from canonical identity so trailing-slash-sensitive sites can still be fetched without breaking dedupe.
- Added `tools/run_tree_catalog_validation.py` so the curated smoke catalog can be tested as recursive trees rather than only single-page smoke checks.
- Added unit coverage for page/file scope rules, bootstrap persistence, and trailing-slash-sensitive recursive seeds.

### 10. Agent rescue and feed fallback

- Improved content root selection so thin placeholder `main` nodes no longer beat richer content containers.
- Added XML sitemap and RSS normalization plus XML link extraction, so official `sitemap.xml` and `rss.xml` endpoints can act as agent-friendly fallback inputs.
- Added optional `tree_seed_url` and `tree_page_prefixes` to the smoke catalog so recursive validation can use a better section root than the smoke monitor target.
- Added `tools/run_agent_rescue_validation.py` to evaluate the rescue ladder: catalog target, browser retry, then official sitemap or RSS.
- Confirmed a full-catalog rescue baseline where `35 / 37` sites are usable with either the primary target or an agent fallback strategy.

### 11. Shared rescue ladder in main smoke and REST

- Added `web_listening/blocks/rescue.py` as the shared rescue implementation for scripts and API callers.
- Switched `tools/run_smoke_site_catalog.py` to use the same rescue ladder by default, with `--primary-only` available when we need strict catalog-only checks.
- Added `POST /api/v1/sites/{id}/rescue-check` so AI agents can request a rescue evaluation and receive the winning normalized snapshot without changing the stored monitoring baseline.
- Refactored `tools/run_agent_rescue_validation.py` to reuse the same core ladder instead of maintaining a duplicate script-only implementation.
- Added local tests for the rescue endpoint and smoke outcome handling.
- Tightened rescue correctness so returned snapshots keep the real `site_id`, and browser rescue now defaults to a browser user-agent profile instead of the bot UA.

### 12. Persistent daily dev monitoring

- Added `tools/run_dev_daily_monitor.py` to persist daily `SOA`, `CAS`, and `IAA` monitoring snapshots into the main SQLite database.
- The daily monitor reuses the stored snapshots on later runs, so tomorrow's execution will compare against today's persisted baseline instead of starting from scratch.
- Added optional sample-document download support so daily development runs can also refresh a small set of real downloaded files in the shared blob store.
- Added local coverage to prove the daily monitor initializes the baseline once, then reuses the same database on the next run.

## Current state

- Tests passing: `72`
- Validation command: `.venv\Scripts\python -m pytest tests -q`
- Local validation environment: project-local `.venv`
- Required live targets: `SOA`, `CAS`, `IAA`
- Required live validation commands:
  - `.venv\Scripts\python tools\validate_real_sites.py`
  - `.venv\Scripts\python tools\run_dev_regression.py`
  - `.venv\Scripts\python tools\run_dev_daily_monitor.py --download-samples`
  - `.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only`
  - `.venv\Scripts\python tools\run_smoke_site_catalog.py --primary-only --report-only`
  - `.venv\Scripts\python tools\run_tree_catalog_validation.py`
  - `.venv\Scripts\python tools\run_agent_rescue_validation.py`
- Live regression fallback:
  - `.venv\Scripts\python tools\run_dev_regression.py --report-only`
- Live regression policy doc:
  - `DEV_TEST_TARGETS.md`
- List-driven smoke policy doc:
  - `SMOKE_SITE_MANAGEMENT.md`
- List-driven smoke baseline report:
  - `SMOKE_SITE_VALIDATION.md`
- Recursive tree design doc:
  - `TREE_MONITORING_DESIGN.md`
- Recursive tree live baseline report:
  - `TREE_CATALOG_VALIDATION.md`
- Agent rescue live baseline report:
  - `AGENT_RESCUE_VALIDATION.md`
- PR recommendation:
  - `PR_RECOMMENDATION.md`

## Key decisions still in force

- Keep the generic REST API as the stable backend contract.
- Keep document conversion outside this repo.
- Treat browser crawling as optional capability, not default dependency.
- Continue moving the system toward agent-friendly artifacts and durable evidence.

## Next recommended implementation steps

### 1. PR cut and review

- Open a draft PR now from `docs/ai-agent-roadmap`
- Treat the current branch as the first agent-ready foundation milestone
- Keep follow-up work such as jobs, watch rules, and MCP in later PRs

### 2. Jobs and async envelopes

- Add a `jobs` table
- Return `job_id`, `status`, and `accepted_at` from long-running write operations
- Add `GET /api/v1/jobs/{id}`

### 3. Browser execution hardening

- Validate Playwright mode on at least one live JS-heavy site
- Add richer browser config support such as `wait_for`, `wait_until`, and extra wait logic
- Consider page screenshot artifacts for failed browser crawls

### 4. Structured watch rules

- Add `watch_rules`
- Add structured extraction results
- Add field-level change payloads and evidence pointers

### 5. MCP layer

- Expose the stabilized REST-backed workflows as MCP tools and resources
- Keep MCP as an adapter over the same backend, not a separate execution path
