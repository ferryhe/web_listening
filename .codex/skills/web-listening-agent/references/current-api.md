# Current API

## Implemented blocks

- `web_listening/blocks/crawler.py`: HTTP fetch via `httpx`, HTML cleanup via BeautifulSoup, snapshot text plus link extraction.
- `web_listening/blocks/normalizer.py`: HTML normalization into cleaned HTML, Markdown, fit-Markdown, and metadata.
- `web_listening/blocks/diff.py`: SHA-256 hash comparison, unified diff, new-link detection, document-link filtering.
- `web_listening/blocks/document.py`: document download, blob dedupe by SHA-256, metadata persistence, no content conversion.
- `web_listening/blocks/storage.py`: SQLite storage for sites, snapshots, changes, documents, blobs, and analyses.
- `web_listening/blocks/analyzer.py`: weekly Markdown summary via OpenAI or local fallback.
- `web_listening/blocks/scheduler.py`: APScheduler-based periodic execution.

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

- No JS rendering or browser automation.
- No selector-based or schema-based watch rules.
- No persistent job table, webhook delivery, or idempotency keys.
- No MCP server yet.

## Working assumptions

- Keep SQLite as the default store until agent-facing contracts stabilize.
- Keep document conversion outside this repo; use `content_md` and its status fields as handoff fields.
- Reuse existing blocks when adding new interfaces; do not duplicate core crawling or storage logic.
