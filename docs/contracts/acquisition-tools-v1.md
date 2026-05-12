# Acquisition Tools v1

## Purpose

`acquisition-tools.v1` is the stable tool-picker catalog for acquisition planning UIs, agents, and delivery integrations.

The authoritative runtime surfaces are:

- API: `GET /api/v1/acquisition/tools`
- CLI: `web-listening list-acquisition-tools --json`
- Python helper: `web_listening.blocks.acquisition_tools.acquisition_tools_catalog()`

The catalog describes what a frontend or agent may present to an operator before building or probing an `acquisition-profile.v1`. It does not change the fixed staged crawl execution path:

```text
discover -> classify -> select -> task -> bootstrap -> run -> report -> manifest
```

In this build, only `web_http`, `browser_rendered`, and `cloakbrowser` are probe-capable. `sitemap`, `rss`, and `batch_python` are stable reserved choices for planning and future integrations.

## Top-Level Object

| Field | Type | Description |
|---|---|---|
| `contract_version` | string | Must be `acquisition-tools.v1`. |
| `catalog_version` | string | Catalog content version for fixture/runtime drift checks. |
| `tool_selection_rules` | array | Stable mapping from operator/agent acquisition signals to adapter IDs. |
| `tools` | array | Ordered tool entries suitable for frontend picker rendering. |

## Tool Selection Mapping

| Acquisition signal | Tool |
|---|---|
| ordinary public HTML | `web_http` |
| dynamic JS | `browser_rendered` |
| authorized stealth browser/CDP-like context | `cloakbrowser` |
| bulk structured/site-specific scrape | `batch_python` |
| sitemap discovery | `sitemap` |
| RSS/feed discovery | `rss` |

`sitemap` and `rss` are reserved discovery/feed choices. `batch_python` is reserved for reviewed structured or site-specific acquisition jobs.

## Tool Entry Fields

Existing fields remain backward-compatible:

| Field | Type | Description |
|---|---|---|
| `adapter` | string | Stable adapter ID. |
| `category` | string | Coarse tool category. |
| `purpose` | string | Human-readable tool purpose. |
| `built_in_now` | boolean | Whether this build includes a built-in implementation or wrapper. |
| `implemented_for_pr3_probing` | boolean | Compatibility flag for the PR3 probing surface. |
| `probe_capable` | boolean | Whether `probe-acquisition` / `/acquisition/probe` may execute the adapter. |
| `optional_runtime` | object | Present only when a tool needs an optional dependency. |
| `safety_notes` | array | Operator-facing safety constraints. |

Frontend/agent-ready fields:

| Field | Type | Description |
|---|---|---|
| `recommended_when` | array | Signals where this tool is a good first choice. |
| `not_for` | array | Signals where a frontend or agent should steer away. |
| `operator_inputs` | array | Non-secret inputs a picker may request before profile/probe calls. |
| `requires_profile_safety` | object | Required `acquisition-profile.v1` safety gates for this tool. |
| `output_contract` | object | Related contracts and the explicit no-execution-change boundary. |
| `runtime_status` | string | One of `available`, `optional_runtime`, or `reserved`. |
| `frontend_control` | object | Picker label, grouping, control kind, selectability, and sort order. |

## Runtime Status Semantics

- `available`: selectable and probe-capable with the installed core/runtime prerequisites for this build.
- `optional_runtime`: selectable and probe-capable only when the named optional runtime is installed and safety gates pass.
- `reserved`: visible in the picker contract but not executable as a probe adapter in this build.

## Safety Boundary

`cloakbrowser` requires both profile safety gates:

```json
{
  "allow_stealth_browser": true,
  "require_authorized_access": true
}
```

The catalog is not a credential store. Tool entries, fixtures, profiles, and picker requests must not include API keys, cookies, authorization headers, passwords, private `.env` values, or unreviewed local credentials.

## Sample Fixture

The canonical sample fixture is:

- [`docs/testing/fixtures/acquisition-tools-v1.sample.json`](../testing/fixtures/acquisition-tools-v1.sample.json)

Focused fixture drift checks live in:

- `tests/test_acquisition_tools_contract_fixture.py`
