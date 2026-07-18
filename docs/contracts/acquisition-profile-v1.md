# Acquisition Profile v1

## Purpose

`acquisition-profile.v1` is a control-plane contract for choosing how `web_listening` should acquire source pages before the fixed staged workflow runs.

The staged workflow remains:

```text
discover -> classify -> select -> task -> bootstrap -> run -> report -> manifest
```

That sequence is intentionally stable. Site-to-site variation belongs in the acquisition layer, where an operator or future probe can say whether a site should start with plain HTTP, rendered browser capture, sitemap/feed ingestion, an authorized stealth browser, or an explicit batch script.

PR1 defines the profile and capture-attempt contracts only. It does not integrate these contracts into crawler execution, staged workflow commands, reports, manifests, or CloakBrowser dependencies.

PR2 adds standalone capture evaluation helpers and built-in adapter wrappers for the existing HTTP and rendered-browser crawlers. These helpers evaluate `FetchResult` objects into `capture-attempt.v1` records, but they still do not alter crawler behavior or wire acquisition into CLI/API/staged workflow execution.

## File Format

- Encoding: UTF-8.
- Format: YAML object.
- Schema version: `acquisition-profile.v1`.
- Recommended generated filename: `acquisition_profile_<site_key>_<date>.yaml`.
- Static fixtures and examples may omit the date, such as `acquisition-profile-v1.sample.yaml`.
- Unknown or site-specific settings should live inside adapter `config`, adapter `safety`, `metadata`, or future namespaced extensions.
- Profiles must not contain API keys, cookies, authorization headers, passwords, private `.env` values, or unreviewed local credentials.

## Adapter IDs

Allowed adapter IDs are:

| Adapter | Intended role |
|---|---|
| `web_http` | Default low-cost HTTP capture for public pages. |
| `browser_rendered` | Rendered browser capture for pages that require JavaScript. |
| `sitemap` | Sitemap-driven discovery or capture. |
| `rss` | Feed-driven discovery or capture. |
| `cloakbrowser` | Authorized stealth-browser capture for approved access contexts only. |
| `browseract` | Optional isolated CLI with fixed read-only recipes; disabled by default. |
| `batch_python` | Explicit site-specific batch acquisition script or job. |

PR1 defines IDs and validation only. It does not install or invoke CloakBrowser.

## `AcquisitionProfile`

Required fields:

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Must be `acquisition-profile.v1`. |
| `profile_id` | string | Stable profile identifier. |
| `site_key` | string | Site key the profile applies to. |
| `generated_at` | string | ISO-8601 UTC timestamp. |

Defaulted fields:

| Field | Type | Description |
|---|---|---|
| `strategy` | string | Human-readable acquisition strategy name. |
| `default_adapter` | string | First adapter to try. Must be an allowed adapter ID. |
| `fallback_order` | array | Additional adapters to try when prior attempts fail quality gates. Every item must be an allowed adapter ID. |
| `quality_gates` | object | Minimum capture quality expectations. |
| `safety` | object | Site-level safety policy. |
| `adapters` | array | Adapter-specific enablement, reasons, config, and safety notes. |
| `notes` | array | Human-readable notes. |

### `AcquisitionAdapterConfig`

| Field | Type | Description |
|---|---|---|
| `adapter` | string | One allowed adapter ID. |
| `enabled` | boolean | Whether this adapter is allowed for the profile. |
| `reason` | string | Why this adapter is present. |
| `config` | object | Non-secret adapter settings. |
| `safety` | object | Adapter-specific safety notes or constraints. |

### `AcquisitionQualityGates`

| Field | Type | Description |
|---|---|---|
| `min_words` | integer | Minimum extracted word count for a successful page capture. |
| `min_links` | integer | Minimum link count when link discovery is expected. |
| `min_document_links` | integer | Minimum document/file link count when document discovery is expected. |
| `require_status_ok` | boolean | Whether HTTP status must be a 2xx success response. |
| `blocked_markers` | array | Text markers that indicate blocked, gated, or unusable content. |

### `AcquisitionSafetyPolicy`

| Field | Type | Description |
|---|---|---|
| `allowed_domains` | array | Optional allowlist of non-empty domain strings. |
| `allow_stealth_browser` | boolean | Operator approval for stealth-browser acquisition. |
| `require_authorized_access` | boolean | Confirms the target context is authorized for access. |

Safety rule:

- `cloakbrowser` must not be the default adapter, must not be enabled, and must not appear in `fallback_order` unless both `safety.allow_stealth_browser` and `safety.require_authorized_access` are `true`.
- `allowed_domains`, when present, must contain only non-empty strings.

## `CaptureAttempt`

`capture-attempt.v1` records one acquisition attempt result. It is suitable for future report, manifest, or probing extensions, but those integrations are outside PR1.

| Field | Type | Description |
|---|---|---|
| `schema_version` | string | Must be `capture-attempt.v1`. |
| `adapter` | string | Adapter ID used for the attempt. |
| `status` | string | Attempt status such as `passed`, `failed_quality_gate`, `blocked`, or `error`. |
| `url` | string | Requested URL. |
| `final_url` | string | Final URL after redirects, if known. |
| `status_code` | integer/null | HTTP status code, if available. |
| `word_count` | integer | Extracted word count. |
| `link_count` | integer | Extracted link count. |
| `document_link_count` | integer | Extracted document/file link count. |
| `failure_reason` | string | Human-readable failure reason. |
| `recommended_next_adapter` | string | Optional next adapter recommendation captured at attempt time. Must be an allowed adapter ID or an empty string. |
| `metadata` | object | Non-secret adapter-specific result metadata. |

## Capture Evaluation Helpers

`web_listening.blocks.acquisition_capture` provides helper APIs for evaluating capture quality before fixed staged workflow integration:

- `evaluate_fetch_result(adapter, url, result, quality_gates)` converts a crawler `FetchResult` into a `CaptureAttempt`.
- `evaluate_capture_attempt(attempt, quality_gates)` re-evaluates count/status/metadata gates while preserving existing blocked or error evidence that cannot be reconstructed from a stored attempt alone.
- `run_capture_attempt(url, adapter, profile, prior_attempts=None)` executes one adapter, catches adapter exceptions as `status: error`, and records the next adapter recommended by `recommend_next_adapter`.
- `build_builtin_adapters()` exposes `web_http`, `browser_rendered`, and optional safety-gated `cloakbrowser` probe adapters. `cloakbrowser` remains limited to explicit authorized acquisition probing and is not used by `bootstrap-scope` or `run-scope`.

Evaluation passes only when required HTTP status is a 2xx response, extracted word count meets `min_words`, metadata link counts meet `min_links`, metadata document-link counts meet `min_document_links`, and no blocked marker is found case-insensitively in captured text or metadata.

Current limitation: `FetchResult` does not expose extracted link objects directly. The helpers therefore read `metadata.link_count` and `metadata.document_link_count`, defaulting missing values to `0`, until crawler integration provides richer link and document evidence.

## Recommendation Semantics

`recommend_next_adapter(profile, attempts)` returns:

- an empty string when any attempt has `status: passed`
- otherwise, the first untried adapter from `default_adapter` followed by `fallback_order`
- explicitly disabled adapters in `profile.adapters` are skipped
- an empty string when every adapter in that sequence has already been attempted

## Future Extensions

Later PRs may:

- wire profiles into bootstrap/run commands
- persist capture attempts in reports or manifests
- add more acquisition tool runtimes behind explicit contracts and safety rules

Those changes must preserve the fixed staged workflow and keep site-specific acquisition variation in this profile layer.
## BrowserAct profile metadata

`browseract` is a valid optional adapter id. Default profiles include it as disabled metadata only; it is never added to `fallback_order`. Enabling or selecting it requires both `safety.allow_stealth_browser=true` and `safety.require_authorized_access=true`. Runtime inspection and fixed-recipe validation are separate executor concerns.
