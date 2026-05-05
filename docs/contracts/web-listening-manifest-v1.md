# Web Listening Manifest v1

## Purpose

`web-listening-manifest.v1` is the stable, machine-readable handoff contract from `web_listening` to downstream tools such as `doc_to_md`, `md_to_rag`, `rag_to_agent`, and the future `ai_interface` console.

The manifest describes one staged monitoring run and the artifacts it produced. It must be path-portable, provenance-preserving, and safe to store or pass between agents. It must not contain API keys, cookies, passwords, authorization headers, or private local configuration.

This contract is intentionally narrower than the current internal database and workflow models. Internal tables remain the execution authority; the manifest is the reviewed export boundary.

## Current implementation boundary

Today, `web-listening export-manifest` writes:

- `document_manifest_<site>_<date>.yaml`
- `document_manifest_<site>_<date>.md`

Today, `web-listening export-manifest --json` returns the existing CLI/API delivery envelope `job_delivery.v1`, which points at those YAML/Markdown artifacts through `artifact_contract.v1`.

This document defines the next export artifact payload, not a rename of `job_delivery.v1`. Future runtime work may add a JSON file output, a new flag, or a different response mode, but downstream readers should treat `web-listening-manifest.v1` as the target manifest content.

## Current output inventory

The current staged workflow already produces several operator and agent artifacts:

| Workflow stage | Typical artifact | Current shape | Manifest role |
|---|---|---|---|
| discover | `section_inventory_<site>_<date>.yaml` | YAML | source discovery evidence |
| classify | `section_classification_<site>_<date>.yaml` | YAML | source classification evidence |
| select | `section_selection_<site>_<date>.yaml` | YAML | human-reviewed scope input |
| plan-scope | `monitor_scope_<site>_<date>.yaml` | YAML | runnable scope input |
| bootstrap-scope | `tree_bootstrap_scope_<site>_<date>.md` | Markdown | human-readable bootstrap report |
| bootstrap-scope | `bootstrap_scope_summary_<site>_<date>.md` | Markdown | quality summary |
| run-scope/report-scope | `tracking_report_<site>_<date>.md` | Markdown | human-readable tracking report |
| run-scope/report-scope | `tracking_report_<site>_<date>.yaml` | YAML | structured tracking report |
| export-manifest | `document_manifest_<site>_<date>.yaml` | YAML | current document list export |

`web-listening-manifest.v1` does not remove these files. It provides a stable JSON envelope that points to them and to downloaded assets with explicit provenance.

## File format

- Encoding: UTF-8.
- Format: JSON object.
- Recommended filename: `web_listening_manifest_<run_id>.json` or `web_listening_manifest_<site_slug>_<YYYYMMDD>.json`.
- All paths inside the manifest are relative to `artifact_root` unless explicitly marked as an external URI.
- Timestamps are ISO-8601 UTC strings with a trailing `Z`.
- Unknown fields are allowed only under `metadata`, `extensions`, or `deprecated`.

## Top-level object

Required fields:

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Must be `web-listening-manifest.v1`. |
| `manifest_id` | string | Stable export ID for this manifest file. |
| `generated_at` | string | ISO-8601 UTC timestamp for manifest generation. |
| `producer` | object | Tool identity and command that produced the manifest. |
| `artifact_root` | string | Relative root for local artifacts, usually `.` from the manifest directory or `data`. |
| `run` | object | One staged run summary. |
| `source` | object | Monitored source/site information. |
| `status` | object | Export/run status and counts. |
| `artifacts` | object | Reports, current structured exports, and compatibility artifacts. |
| `discovered_items` | array | Pages or records discovered during the run. |
| `downloaded_assets` | array | Downloaded files/assets ready for downstream conversion. |

Optional fields:

| Field | Type | Description |
|---|---|---|
| `job` | object/null | Internal job record summary if this manifest came from a persisted job. |
| `provenance` | object | Shared provenance defaults for the manifest. |
| `errors` | array | Non-fatal or fatal errors encountered during discovery/download/export. |
| `deprecated` | object | Legacy fields preserved for backward compatibility. |
| `metadata` | object | Free-form non-secret metadata. |
| `extensions` | object | Namespaced future additions. |

## Field definitions

### `producer`

```json
{
  "name": "web-listening",
  "version": "0.1.0",
  "command": "web-listening export-manifest --scope-path data/plans/monitor_scope_soa_2026-04-07.yaml",
  "contract_version": "web-listening-manifest.v1"
}
```

Rules:

- `command` should include the public command shape, but must redact secrets and machine-local credentials.
- If the package version is unavailable, use `null` rather than inventing a value.

### `run`

Required fields:

| Field | Type | Description |
|---|---|---|
| `run_id` | string | Stable run identifier within this project or export. |
| `run_type` | string | One of `discover`, `classify`, `select`, `plan_scope`, `bootstrap_scope`, `run_scope`, `report_scope`, `export_manifest`, or `composite`. |
| `started_at` | string/null | Run start timestamp. |
| `completed_at` | string/null | Run completion timestamp. |
| `input_paths` | array | Relative input artifact paths used for this run. |
| `output_paths` | array | Relative output artifact paths produced by this run. |
| `idempotency_key` | string | Stable key derived from source, scope, and run parameters. |

Optional fields:

| Field | Type | Description |
|---|---|---|
| `parent_run_id` | string/null | Parent run for reruns or derived reports. |
| `scope_path` | string/null | Relative path to the monitor scope YAML. |
| `selection_path` | string/null | Relative path to the section selection YAML. |
| `parameters` | object | Non-secret run parameters. |

Idempotency rule: re-exporting the same completed run should keep the same `run.run_id` and `run.idempotency_key`. The manifest file path may differ, but downstream consumers should be able to detect an equivalent run from the idempotency key.

### `job`

`job` is optional and summarizes internal job state when available:

| Field | Type | Description |
|---|---|---|
| `job_id` | integer/string | Internal job ID. |
| `job_type` | string | Internal job type, for example `monitor_task.create` or `scope_report.export`. |
| `status` | string | Internal job status. |
| `created_at` | string/null | Job creation timestamp. |
| `updated_at` | string/null | Last job update timestamp. |

Do not expose database-only details that downstream modules do not need.

### `source`

Required fields:

| Field | Type | Description |
|---|---|---|
| `source_id` | string | Stable source/site identifier. |
| `site_url` | string | Canonical source URL. |
| `site_name` | string | Human-readable site name. |
| `scope_profile` | string/null | Suggested or approved scope profile such as `section_documents`. |

Optional fields:

| Field | Type | Description |
|---|---|---|
| `tree_seed_url` | string/null | Seed URL used for tree crawling. |
| `tree_page_prefixes` | array | Approved page URL prefixes. |
| `tree_file_prefixes` | array | Approved file URL prefixes. |
| `catalog_key` | string/null | Catalog key such as `soa`, `cas`, or `iaa`. |

### `status`

Required fields:

| Field | Type | Description |
|---|---|---|
| `state` | string | One of `pending`, `running`, `completed`, `completed_with_warnings`, `failed`, `partial`, or `cancelled`. |
| `stage` | string | Current or final workflow stage. |
| `counts` | object | Counts for discovered pages, downloaded assets, warnings, and errors. |
| `message` | string | Short human-readable summary. |

Recommended `counts` keys:

```json
{
  "discovered_items": 2,
  "downloaded_assets": 1,
  "changed_items": 1,
  "warnings": 0,
  "errors": 0
}
```

### `artifacts`

`artifacts` points to the files produced by the staged workflow. Use relative paths and classify each path by purpose.

Recommended keys:

| Field | Type | Description |
|---|---|---|
| `reports` | array | Human-readable Markdown reports. |
| `structured_exports` | array | YAML/JSON artifacts meant for machines. |
| `compatibility_exports` | array | Existing legacy exports preserved during migration. |

Each artifact entry uses:

| Field | Type | Description |
|---|---|---|
| `artifact_id` | string | Stable ID within this manifest. |
| `kind` | string | Example: `tracking_report_md`, `tracking_report_yaml`, `document_manifest_yaml`, `scope_yaml`. |
| `path` | string | Relative path from `artifact_root`. |
| `media_type` | string | MIME type or close equivalent. |
| `sha256` | string/null | SHA-256 checksum if known. |
| `created_at` | string/null | Artifact creation timestamp. |
| `provenance` | object | Input/source/run references for this artifact. |

### `discovered_items`

A `discovered_item` represents a page, listing entry, or remote resource observed by the crawl.

Required fields:

| Field | Type | Description |
|---|---|---|
| `item_id` | string | Stable item ID. Recommended: deterministic hash of canonical URL and scope. |
| `item_type` | string | `page`, `file_link`, `listing_entry`, or `unknown`. |
| `url` | string | Canonical URL. |
| `title` | string/null | Extracted title or link text. |
| `status` | string | `new`, `unchanged`, `changed`, `removed`, `skipped`, or `error`. |
| `observed_at` | string | Observation timestamp. |
| `provenance` | object | Where this item came from. |

Optional fields:

| Field | Type | Description |
|---|---|---|
| `parent_item_id` | string/null | Parent page/listing item. |
| `content_type` | string/null | HTTP content type if known. |
| `http_status` | integer/null | HTTP response status if fetched. |
| `checksum` | object/null | Checksum over fetched content if known. |
| `metadata` | object | Non-secret metadata. |

### `downloaded_assets`

A `downloaded_asset` is a local evidence file, usually intended for `doc_to_md`.

Required fields:

| Field | Type | Description |
|---|---|---|
| `asset_id` | string | Stable asset ID. Recommended: SHA-256-prefixed ID when available. |
| `source_item_id` | string | `discovered_items[].item_id` that produced this asset. |
| `url` | string | Remote URL. |
| `local_path` | string | Portable relative path to the preferred local copy. |
| `canonical_blob_path` | string/null | Relative `_blobs` path when deduped storage is available. |
| `tracked_path` | string/null | Relative `_tracked` source-oriented path when available. |
| `filename` | string | Display filename. |
| `media_type` | string|null | MIME type if known. |
| `bytes` | integer|null | File size in bytes if known. |
| `checksum` | object | Checksum object. |
| `status` | string | `downloaded`, `already_present`, `skipped`, or `error`. |
| `provenance` | object | Source/run references. |

### `checksum`

Use the same checksum shape for artifacts, discovered content, and assets:

```json
{
  "algorithm": "sha256",
  "value": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}
```

If a checksum is not available, use `null`; do not use empty strings.

### `provenance`

Every derived artifact should preserve a chain back to source and run input.

Recommended fields:

| Field | Type | Description |
|---|---|---|
| `source_id` | string | Source/site identifier. |
| `run_id` | string | Run that produced the item. |
| `input_artifacts` | array | Relative paths or artifact IDs used as input. |
| `parent_item_id` | string/null | Parent discovered item if relevant. |
| `observed_at` | string|null | Observation timestamp. |
| `extraction_method` | string|null | Method such as `http`, `browser`, `tree_crawler`, or `manual_review`. |

### `errors`

Errors are safe, structured diagnostics. Do not include secret-bearing URLs or headers.

| Field | Type | Description |
|---|---|---|
| `error_id` | string | Stable ID within the manifest. |
| `severity` | string | `warning`, `error`, or `fatal`. |
| `stage` | string | Stage where the error occurred. |
| `message` | string | Redacted human-readable message. |
| `item_id` | string/null | Related discovered item, if any. |
| `url` | string/null | Related public URL, if safe. |
| `retryable` | boolean | Whether retry may help. |

## Example

See [`docs/testing/fixtures/web-listening-manifest-v1.sample.json`](../testing/fixtures/web-listening-manifest-v1.sample.json) for a parseable fixture.

Inline abbreviated example:

```json
{
  "schema_version": "web-listening-manifest.v1",
  "manifest_id": "manifest-soa-research-20260407",
  "generated_at": "2026-04-07T15:30:00Z",
  "producer": {
    "name": "web-listening",
    "version": null,
    "command": "web-listening export-manifest --scope-path data/plans/monitor_scope_soa_2026-04-07.yaml",
    "contract_version": "web-listening-manifest.v1"
  },
  "artifact_root": ".",
  "run": {
    "run_id": "run-soa-20260407T153000Z",
    "run_type": "composite",
    "started_at": "2026-04-07T15:00:00Z",
    "completed_at": "2026-04-07T15:29:30Z",
    "input_paths": ["data/plans/monitor_scope_soa_2026-04-07.yaml"],
    "output_paths": ["data/reports/tracking_report_soa_2026-04-07.md"],
    "idempotency_key": "soa|section_documents|2026-04-07"
  },
  "source": {
    "source_id": "soa",
    "site_url": "https://www.soa.org/",
    "site_name": "Society of Actuaries",
    "scope_profile": "section_documents",
    "tree_seed_url": "https://www.soa.org/research/",
    "tree_page_prefixes": ["https://www.soa.org/research/"],
    "tree_file_prefixes": ["https://www.soa.org/resources/research-reports/"],
    "catalog_key": "soa"
  },
  "status": {
    "state": "completed",
    "stage": "export_manifest",
    "counts": {
      "discovered_items": 1,
      "downloaded_assets": 1,
      "changed_items": 1,
      "warnings": 0,
      "errors": 0
    },
    "message": "Manifest exported for downstream document conversion."
  },
  "artifacts": {
    "reports": [],
    "structured_exports": [],
    "compatibility_exports": []
  },
  "discovered_items": [],
  "downloaded_assets": [],
  "errors": []
}
```

## Path portability rules

1. `artifact_root` defines the base directory for relative paths.
2. `local_path`, `canonical_blob_path`, `tracked_path`, and artifact `path` values must be relative paths unless the field is explicitly a URI.
3. Do not emit machine-specific absolute paths such as `/home/...` or `C:\...` in portable manifests.
4. Downstream tools should resolve paths by joining the manifest file directory, `artifact_root`, and the relative path.
5. If a file is remote-only and not downloaded, keep it in `discovered_items` and omit it from `downloaded_assets`.

## Compatibility and deprecation policy

- v1 consumers must ignore unknown keys under `metadata`, `extensions`, and `deprecated`.
- `job_delivery.v1` and `artifact_contract.v1` remain the current machine-readable CLI/API wrapper contracts for persisted jobs and artifact paths.
- Existing YAML exports such as `document_manifest_<site>_<date>.yaml` remain valid compatibility artifacts while runtime export support migrates to this JSON envelope.
- Legacy field names may appear under `deprecated`, but new consumers should read the normalized v1 fields first.
- Breaking field removals require a new `schema_version` such as `web-listening-manifest.v2`.

## Downstream expectations

`doc_to_md` should primarily consume `downloaded_assets[]` and use:

- `asset_id` as a stable source document key;
- `local_path` as the preferred input file path;
- `checksum` for idempotency and dedupe;
- `source_item_id` and `provenance` to preserve traceability in Markdown output manifests.

`md_to_rag` should not consume this manifest directly unless it is operating on raw web evidence. Its normal input should be the Markdown corpus produced by `doc_to_md`.

## Security requirements

The manifest must never contain:

- API keys, session cookies, bearer tokens, passwords, or private headers;
- unredacted `.env` values;
- private local configuration files;
- full command invocations that include credentials.

If a value may contain a secret, replace it with `[REDACTED]` before export.
