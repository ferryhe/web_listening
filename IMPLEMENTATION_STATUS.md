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

## Current state

- Tests passing: `43`
- Validation command: `.venv\Scripts\python -m pytest tests -q`
- Local validation environment: project-local `.venv`

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
