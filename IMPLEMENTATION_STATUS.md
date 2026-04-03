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

## Current state

- Tests passing: `53`
- Validation command: `.venv\Scripts\python -m pytest tests -q`
- Local validation environment: project-local `.venv`
- Required live targets: `SOA`, `CAS`, `IAA`
- Required live validation commands:
  - `.venv\Scripts\python tools\validate_real_sites.py`
  - `.venv\Scripts\python tools\run_dev_regression.py`
  - `.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only`
- Live regression fallback:
  - `.venv\Scripts\python tools\run_dev_regression.py --report-only`
- Live regression policy doc:
  - `DEV_TEST_TARGETS.md`
- List-driven smoke policy doc:
  - `SMOKE_SITE_MANAGEMENT.md`
- List-driven smoke baseline report:
  - `SMOKE_SITE_VALIDATION.md`

## Key decisions still in force

- Keep the generic REST API as the stable backend contract.
- Keep document conversion outside this repo.
- Treat browser crawling as optional capability, not default dependency.
- Continue moving the system toward agent-friendly artifacts and durable evidence.

## Next recommended implementation steps

### 1. Jobs and async envelopes

- Add a `jobs` table
- Return `job_id`, `status`, and `accepted_at` from long-running write operations
- Add `GET /api/v1/jobs/{id}`

### 2. Browser execution hardening

- Validate Playwright mode on at least one live JS-heavy site
- Add richer browser config support such as `wait_for`, `wait_until`, and extra wait logic
- Consider page screenshot artifacts for failed browser crawls

### 3. Structured watch rules

- Add `watch_rules`
- Add structured extraction results
- Add field-level change payloads and evidence pointers

### 4. MCP layer

- Expose the stabilized REST-backed workflows as MCP tools and resources
- Keep MCP as an adapter over the same backend, not a separate execution path
