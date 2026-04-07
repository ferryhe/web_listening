# Tree Monitoring Design

> Last updated: 2026-04-07
> Status: Active design with implemented bootstrap and rerun tooling

## Goal

Run bounded recursive monitoring after scope selection.

This layer is responsible for:

- crawling the selected HTML tree
- discovering and optionally downloading same-origin files
- persisting page and file evidence
- comparing later runs against the stored baseline

For the planning layer that chooses what should be monitored, read:

- [AGENT_SCOPE_PLANNING_DESIGN.md](C:/Project/web_listening/docs/design/AGENT_SCOPE_PLANNING_DESIGN.md)

## Core Boundary Rule

Do not treat page recursion and file acceptance as the same boundary.

Use two related but different scopes:

- page scope:
  which HTML pages are allowed to continue recursion
- file scope:
  which file URLs are allowed to be accepted and downloaded

This matters because the linking page often lives under `/research/...` while the file itself is hosted under `/files/...` or `/globalassets/...`.

## Current Scope Model

A crawl scope is defined by:

- `seed_url`
- `allowed_origin`
- `allowed_page_prefixes`
- `allowed_file_prefixes`
- `max_depth`
- `max_pages`
- `max_files`
- `fetch_mode`
- `fetch_config_json`

Current production-oriented defaults:

- `max_depth = 4`
- `max_pages = 120`
- `max_files = 40`

## URL Identity Rules

Tracked URLs are canonicalized before persistence:

- drop `#fragment`
- lowercase scheme and host
- drop tracking query parameters such as `utm_*`, `fbclid`, and `gclid`
- normalize trailing slash behavior for identity

The system keeps a distinction between:

- sanitized request URL for fetching
- canonical tracked URL for identity and dedupe

## Crawl Algorithm

Use bounded BFS.

Why:

- level coverage is predictable
- depth limits are easier to enforce
- wide section coverage happens earlier than with DFS

Queue items are effectively:

- `url`
- `depth`
- `from_page_id`

## Implemented Persistence Model

SQLite remains the default store.

The main tree tables are:

- `crawl_scopes`
- `crawl_runs`
- `tracked_pages`
- `page_snapshots`
- `page_edges`
- `tracked_files`
- `file_observations`

Important file-related records:

- `documents`
  logical downloaded document record
- `document_blobs`
  canonical physical blob store keyed by `sha256`
- `file_observations`
  per-run evidence linking source page, tracked file, optional document, and tracked local path

## File Storage Model

The project now uses two file-path layers:

### Canonical store

- location: `data/downloads/_blobs`
- dedupe key: `SHA-256`
- stored in:
  - `document_blobs.canonical_path`
  - `documents.local_path`

### Source-oriented view

- location: `data/downloads/_tracked`
- organized by:
  - source host
  - source page path
  - file name plus `sha256[:8]`
- stored in:
  - `file_observations.tracked_local_path`
- exposed to agents through:
  - `preferred_display_path`
  - document manifest export

Rule:

- `_blobs` is the canonical dedupe layer
- `_tracked` is the browsing and explanation layer

## Bootstrap Behavior

Bootstrap creates a baseline.

It should:

1. create or update the scope
2. crawl bounded pages
3. discover accepted file links
4. optionally download those files
5. persist page snapshots and file observations
6. mark the scope initialized
7. record the baseline run id

Bootstrap should not be treated as a normal alerting event.

## Incremental Reruns

Later runs should:

1. crawl the same selected scope
2. compare page hashes with the latest tracked page state
3. compare downloaded file SHA-256 values with the latest tracked file state
4. detect:
   - new pages
   - changed pages
   - missing pages
   - new files
   - changed files
   - missing files

Current user-facing run tooling:

- `tools/bootstrap_site_tree.py`
- `tools/run_site_tree.py`
- `tools/summarize_scope_bootstrap.py`
- `tools/export_scope_document_manifest.py`
- `tools/explain_tree_bootstrap.py`

## Dedupe Rule

The final file dedupe authority is `SHA-256`.

Never treat URL equality as final file identity because:

- different URLs can serve the same file
- the same URL can later serve different bytes

The current model intentionally keeps:

- logical file identity by tracked URL
- physical blob identity by `SHA-256`

## Polite Crawling

Tree monitoring uses pacing controls from `fetch_config_json`:

- `request_delay_ms`
- `file_request_delay_ms`
- `request_jitter_ms`

These settings are especially important when increasing first-run coverage.

## Current Limits

Tree monitoring is implemented through dedicated tools, not the packaged CLI subcommands or REST API.

In other words:

- the crawler and persistence model are real
- the staged tree workflow is operational
- but its interface is still tool-driven rather than fully API-driven
