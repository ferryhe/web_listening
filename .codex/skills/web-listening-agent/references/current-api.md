# Current API

## Implemented blocks

- `web_listening/blocks/crawler.py`: HTTP fetch via `httpx`, HTML cleanup via BeautifulSoup, snapshot text plus link extraction.
- `web_listening/blocks/crawler.py`: HTTP and optional browser dispatch via `fetch_mode`, returning normalized snapshot artifacts.
- `web_listening/blocks/normalizer.py`: HTML normalization into cleaned HTML, Markdown, fit-Markdown, and metadata.
- `web_listening/blocks/diff.py`: SHA-256 hash comparison, unified diff, new-link detection, document-link filtering.
- `web_listening/blocks/document.py`: document download, blob dedupe by SHA-256, metadata persistence, no content conversion.
- `web_listening/blocks/storage.py`: SQLite storage for sites, snapshots, changes, documents, blobs, and analyses.
- `web_listening/blocks/tree_crawler.py`: bounded recursive bootstrap for page inventories, page snapshots, page edges, tracked files, and file observations.
- `web_listening/blocks/analyzer.py`: weekly Markdown summary via OpenAI or local fallback.
- `web_listening/blocks/scheduler.py`: APScheduler-based periodic execution.
- `web_listening/dev_targets.py`: required live development target validation for `SOA`, `CAS`, and `IAA`.
- `web_listening/smoke_sites.py`: curated smoke site catalog validation for larger list-driven monitoring.

## Implemented REST endpoints

- `GET /api/v1/sites`
- `POST /api/v1/sites`
- `GET /api/v1/sites/{id}`
- `GET /api/v1/sites/{id}/snapshots/latest`
- `DELETE /api/v1/sites/{id}`
- `POST /api/v1/sites/{id}/check`
- `POST /api/v1/sites/{id}/download-docs`
- `GET /api/v1/changes`
- `GET /api/v1/documents`
- `PATCH /api/v1/documents/{id}/content`
- `POST /api/v1/analyze`
- `GET /api/v1/analyses`

## Implemented CLI flows

- `add-site`
- `list-sites`
- `check`
- `list-changes`
- `download-docs`
- `list-docs`
- `analyze`
- `serve`

## Current limitations

- Browser mode is scaffolded, but still needs live Playwright validation and operational hardening.
- Recursive tree bootstrap exists as an internal block, but is not yet exposed via REST or CLI.
- No selector-based or schema-based watch rules.
- No persistent job table, webhook delivery, or idempotency keys.
- No MCP server yet.
- Large external site lists still need a dedicated importer; the current tracked smoke catalog is curated manually from local raw inputs.

## Working assumptions

- Keep SQLite as the default store until agent-facing contracts stabilize.
- Keep document conversion outside this repo; use `content_md` and its status fields as handoff fields.
- Treat browser support as optional capability rather than a required install.
- Reuse existing blocks when adding new interfaces; do not duplicate core crawling or storage logic.
- Site-level HTTP requests can now override the user agent through `fetch_config_json.user_agent` or `fetch_config_json.user_agent_profile`.

## Required live dev targets

Every live development validation should include the required default target set:

- `SOA`
- `CAS`
- `IAA`

The canonical definition lives in `config/dev_test_sites.json`.
Use `tools/validate_real_sites.py` and `tools/run_dev_regression.py` to exercise them.
`tools/run_dev_regression.py` fails on regression issues by default; use `--report-only` only when you need a non-failing report.
Use `DEV_TEST_TARGETS.md` for the current baseline expectations and SHA-256 rules.

## Curated smoke catalog

For bigger monitored lists, keep raw spreadsheets in local ignored folders and promote the actual runnable targets into:

- `config/smoke_site_catalog.json`

Use:

- `tools/run_smoke_site_catalog.py`

Read `SMOKE_SITE_MANAGEMENT.md` for the catalog lifecycle, expectation types, and JS-heavy handling.
Read `SMOKE_SITE_VALIDATION.md` for the current live baseline.
Use `tools/run_tree_catalog_validation.py` and `TREE_CATALOG_VALIDATION.md` when evaluating whether a list target can support bounded recursive tree monitoring instead of only root-page smoke checks.
