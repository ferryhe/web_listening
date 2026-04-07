# Tree Monitoring Design

> Last updated: 2026-04-06
> Status: Active design for crawling after scope selection

## Goal

Extend `web_listening` from single-page checks into bounded recursive site monitoring.

This design assumes the monitoring scope has already been chosen.
For the newer pre-bootstrap planning layer, read:

- `docs/design/AGENT_SCOPE_PLANNING_DESIGN.md`

The system should support:

- recursive HTML discovery within a controlled scope
- same-site file discovery and deduplicated download
- stable SQLite-backed inventory for large monitored trees
- first-run bootstrap without noisy change alerts

## Core decision

Do not treat page recursion and file acceptance as the same boundary.

Use two related but different rules:

- `page scope`: which HTML pages may continue the recursive crawl
- `file scope`: which file links may be accepted and downloaded

This matters because many organizations keep documents in centralized paths such as `/files/`, `/media/`, or `/uploads/`, even when the linking page lives much deeper in the tree.

## Default boundary model

### 1. Page scope

HTML pages should only be followed when all of these are true:

- same origin as the scope seed URL
- normalized URL stays under the scope page prefix
- current depth is less than `max_depth`

Default:

- `max_depth = 3`
- `allowed_page_prefixes = [seed_path_prefix]`

Example:

- seed: `https://example.org/news/`
- page recursion allowed:
  - `https://example.org/news/article-a`
  - `https://example.org/news/2026/update`
- page recursion denied:
  - `https://example.org/reports/annual-2026`
  - `https://other.example.org/news/`

### 2. File scope

Files should be accepted under a slightly wider rule:

- same origin as the scope seed URL by default
- file URL may leave the direct parent page path
- file URL may leave the page scope prefix
- file URL must still stay inside the root web boundary for the scope

Default:

- `allowed_file_prefixes = ["/"]`

That means a page under `/news/` may still accept:

- `https://example.org/files/report.pdf`
- `https://example.org/uploads/2026/slides.pptx`

but should reject:

- `https://cdn.example.net/report.pdf`
- `https://other.org/report.pdf`

## Terminology

### Root web

For the first implementation, treat the root web as:

- the same URL origin as the seed URL

This is the safest default.
If later we need to support document hosts like `downloads.example.org`, add:

- `allowed_file_hosts`

but do not start there by default.

### Scope seed

The scope should be defined by:

- `seed_url`
- `allowed_origin`
- `allowed_page_prefixes`
- `allowed_file_prefixes`
- `max_depth`

When a curated site catalog provides both `homepage_url` and `monitor_url`, use:

- `monitor_url` as the recursive `seed_url`
- `homepage_url` only as descriptive metadata

## URL handling rules

Every discovered URL should be canonicalized before matching or persistence:

- drop `#fragment`
- lowercase scheme and host
- normalize trailing slash rules
- sort query parameters
- drop tracking parameters such as `utm_*`, `fbclid`, `gclid`
- keep only query parameters that are explicitly allowed when needed

This canonical form should drive:

- queue dedupe
- tracked page identity
- tracked file identity
- subtree hashing

Do not blindly use the canonical identity URL as the network request URL.

Recommended split:

- request URL sanitation:
  - drop `#fragment`
  - drop tracking parameters
  - preserve path shape such as trailing `/` when requesting
- canonical identity:
  - use the final URL after fetch when possible
  - then normalize trailing slash rules for dedupe and hashing

## Recommended SQLite model

Keep one SQLite database per deployment by default.
Do not create one SQLite file per page.

The logical split should be by `scope_id`, not by file.

### New tables

- `crawl_scopes`
  - `id`
  - `site_id`
  - `seed_url`
  - `allowed_origin`
  - `allowed_page_prefixes_json`
  - `allowed_file_prefixes_json`
  - `max_depth`
  - `follow_files`
  - `is_initialized`
  - `baseline_run_id`
  - `created_at`
  - `updated_at`
- `crawl_runs`
  - `id`
  - `scope_id`
  - `run_type=bootstrap|incremental`
  - `status=queued|running|completed|failed`
  - `started_at`
  - `finished_at`
  - `pages_seen`
  - `files_seen`
  - `pages_changed`
  - `files_changed`
- `tracked_pages`
  - `id`
  - `scope_id`
  - `canonical_url`
  - `depth`
  - `first_seen_run_id`
  - `last_seen_run_id`
  - `miss_count`
  - `is_active`
  - `latest_snapshot_id`
  - `latest_hash`
- `page_snapshots`
  - `id`
  - `scope_id`
  - `page_id`
  - `run_id`
  - `captured_at`
  - `content_hash`
  - `raw_html`
  - `cleaned_html`
  - `content_text`
  - `markdown`
  - `fit_markdown`
  - `metadata_json`
  - `fetch_mode`
  - `final_url`
  - `status_code`
  - `links`
- `page_edges`
  - `id`
  - `scope_id`
  - `from_page_id`
  - `to_page_id`
  - `run_id`
- `tracked_files`
  - `id`
  - `scope_id`
  - `canonical_url`
  - `first_seen_run_id`
  - `last_seen_run_id`
  - `miss_count`
  - `is_active`
  - `latest_document_id`
  - `latest_sha256`
- `file_observations`
  - `id`
  - `scope_id`
  - `run_id`
  - `page_id`
  - `file_id`
  - `discovered_url`
  - `download_url`

### Existing tables to reuse

- `site_snapshots` remains useful for the current site-level summary layer
- `documents` remains the logical downloaded-file record
- `document_blobs` remains the physical deduplicated blob store keyed by SHA-256

## First-run bootstrap

The first recursive run should initialize inventory, not emit normal change alerts.

### Bootstrap flow

1. create or update `crawl_scope`
2. enqueue a `crawl_run` with `run_type=bootstrap`
3. BFS crawl all allowed HTML pages up to `max_depth`
4. register all accepted file links
5. download files if file tracking is enabled
6. compute page hashes and file SHA-256 values
7. populate `tracked_pages`, `tracked_files`, and edges
8. set `is_initialized = 1`
9. record `baseline_run_id`

### Bootstrap behavior

- no normal `content_changed` alerts
- no normal `file_changed` alerts
- optional summary only:
  - pages discovered
  - files discovered
  - blocked pages
  - skipped out-of-scope links

## Incremental runs

After bootstrap, each run should:

1. start from the scope seed queue
2. crawl allowed HTML pages
3. compare page hash with the latest tracked page hash
4. discover allowed file links
5. compare downloaded file SHA-256 with the latest tracked file SHA-256
6. update subtree hashes
7. emit structured changes

### Missing pages or files

Do not mark a page or file as removed after one miss.

Recommended default:

- `remove_after_missed_runs = 2`

This reduces noise from temporary failures or navigation changes.

## Recursion algorithm

Use BFS, not DFS.

Why:

- depth limits are easier to enforce
- queue behavior is more predictable
- broad section coverage happens earlier in the run

Each queue item should include:

- `url`
- `canonical_url`
- `depth`
- `discovered_from_page_id`

## File dedupe and download policy

The final dedupe authority should be file SHA-256.

### Rules

- never trust URL equality alone as file identity
- two different URLs may point to the same file
- the same URL may later serve a different file
- always compute SHA-256 from raw bytes after download

### Storage behavior

- `document_blobs.sha256` stays the physical dedupe key
- `tracked_files.latest_sha256` stores the current logical file state for a scope
- multiple pages may reference the same file blob
- multiple scopes may reference the same file blob

### Practical result

This guarantees:

- no duplicate physical downloads after dedupe
- stable evidence when the same file appears in several parts of the tree
- correct detection when a file URL stays the same but bytes change

## Tree hashing

To help agents work at section level, add hashes at three layers:

- `page_hash`
- `subtree_hash`
- `scope_hash`

Suggested aggregation:

- sort `canonical_url + ":" + page_hash`
- hash the joined lines with SHA-256

Do the same for files when computing subtree or scope summaries.

## Suggested API direction

Recursive monitoring should not replace the current site endpoints.
It should extend them.

Suggested additions:

- `POST /api/v1/sites/{id}/scopes`
- `GET /api/v1/sites/{id}/scopes`
- `POST /api/v1/scopes/{id}/crawl`
- `GET /api/v1/scopes/{id}/pages`
- `GET /api/v1/scopes/{id}/files`
- `GET /api/v1/scopes/{id}/tree`
- `GET /api/v1/crawl-runs/{id}`

## Recommended implementation order

### Iteration 1

- add `crawl_scopes`
- add `crawl_runs`
- add `tracked_pages`
- add `tracked_files`
- add bootstrap mode

### Iteration 2

- add recursive BFS crawl
- enforce page scope and file scope separately
- persist page edges
- add subtree hashing

### Iteration 3

- add structured page and file change payloads
- add job envelopes
- expose recursive scope APIs

## Non-goals for the first recursive version

- unlimited whole-domain crawling
- cross-domain file following by default
- browser mode for every page by default
- distributed crawling
- per-page SQLite databases
