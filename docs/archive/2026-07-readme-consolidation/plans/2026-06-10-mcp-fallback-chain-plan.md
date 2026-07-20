# Web Listening MCP Fallback Chain Implementation Plan

> **Historical snapshot:** This plan preserves the design context at the time it was written. Its runtime prerequisites and setup commands are superseded by [Python Runtime Compatibility](../operations/PYTHON_RUNTIME.md), which is the active authority.

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add an MCP-facing acquisition fallback engine so agents can ask `web_listening` for usable website data and automatically escalate from one acquisition tool to the next when the current tool does not return enough information.

**Architecture:** Keep the existing `web_listening` core, CLI, and FastAPI surfaces. Add the fallback engine in the shared core layer, then add a thin MCP adapter over that core capability. Individual adapters still perform one acquisition attempt; the core orchestrator runs a resolved strategy chain, evaluates `has_data` / `data_status`, preserves every attempt, and stops only when data is usable or escalation is unsafe/impossible. MCP should be transport/schema glue, not the owner of fallback decisions.

**Tech Stack:** Python 3.10+, Pydantic v2, existing `web_listening.blocks.acquisition_*` modules, optional `mcp` Python SDK, Typer/FastAPI remain unchanged.

---

## Product Principle

`web_listening` MCP should not merely expose crawler commands. It should expose an **acquisition fallback engine**:

```text
agent asks for data
  -> web_http attempt
  -> if no usable data, browser_rendered attempt
  -> if still no usable data, sitemap/rss attempt
  -> if authorized and configured, cloakbrowser/batch_python attempt
  -> return either usable evidence or a structured explanation of why every tool failed
```

The agent should not have to infer this from unstructured logs or failed command output.

---

## Non-Goals

1. Do not replace the existing CLI or FastAPI API.
2. Do not expose every internal function as an MCP tool.
3. Do not automatically use stealth/authorized tools for arbitrary public URLs.
4. Do not inline huge page bodies, reports, manifests, or downloaded documents by default.
5. Do not change the existing staged monitoring workflow semantics in the first MCP PR.

---

## Existing Code Anchors

Current repo has useful primitives already:

- `web_listening.blocks.acquisition_profile.CaptureAttempt`
  - `status`
  - `word_count`
  - `link_count`
  - `document_link_count`
  - `failure_reason`
  - `recommended_next_adapter`
- `web_listening.blocks.acquisition_tools.probe_acquisition_url`
- `web_listening.blocks.acquisition_capture.run_capture_attempt`
- `web_listening.blocks.acquisition_profile.AcquisitionProfile.fallback_order`
- `web_listening.blocks.acquisition_profile.recommend_next_adapter(profile, attempts)`
- `web_listening.models.Job.to_delivery_payload()`
- `web_listening` CLI commands:
  - `list-acquisition-tools --json`
  - `probe-acquisition --json`
  - `bootstrap-scope --json`
  - `run-scope --json`
  - `report-scope --json`
  - `export-manifest --json`
  - `get-job --json`
- Existing artifact contracts:
  - `job_delivery.v1`
  - `artifact_contract.v1`
  - `web-listening-manifest.v1`

The first implementation should wrap and compose these instead of rewriting crawler logic. The fallback engine must live in `web_listening.blocks` (or another shared non-MCP package) so CLI/API/MCP can reuse the same behavior and avoid duplicate decision logic.

---

## Unified Result Envelope

This envelope is a shared project contract, not an MCP-only contract. Place it under a shared module such as `web_listening/contracts/tool_result.py` or `web_listening/blocks/acquisition_result.py`; MCP handlers then return `model_dump(mode="json")`. Every MCP-facing tool should return this shape, even when the underlying operation uses existing contracts internally:

```json
{
  "ok": true,
  "has_data": true,
  "data_status": "present",
  "data_count": 1,
  "tool": "browser_rendered",
  "data_quality": {
    "passed": true,
    "score": 0.86,
    "status_code": 200,
    "word_count": 860,
    "link_count": 42,
    "document_link_count": 7,
    "blocked": false,
    "failure_reasons": []
  },
  "quality_gates": {
    "requested": {
      "min_words": 120,
      "min_links": 3,
      "min_document_links": 1
    },
    "effective": {
      "min_words": 120,
      "min_links": 3,
      "min_document_links": 1
    }
  },
  "data": {},
  "artifacts": {},
  "attempts": [],
  "next_tool": null,
  "next_action": null,
  "warnings": [],
  "error": null,
  "stop_reason": "usable_data_found",
  "meta": {
    "contract_version": "web-listening-tool-result.v1"
  }
}
```

### Required Fields

| Field | Meaning |
|---|---|
| `ok` | Tool execution completed without an operational failure. |
| `has_data` | The result contains usable business data for the requested goal. |
| `data_status` | Precise machine-readable data state. |
| `data_count` | Count of primary data items, if applicable. |
| `tool` | Tool/adapter that produced this specific result. |
| `data_quality` | Observed quality evidence and pass/fail summary. |
| `quality_gates` | Requested and effective quality thresholds used to decide `has_data`. |
| `data` | Small structured payload safe to inline. |
| `artifacts` | Paths/metadata for large outputs. |
| `attempts` | Attempt history for orchestrated fallback tools. |
| `next_tool` | Suggested next adapter/tool when this result is insufficient. |
| `next_action` | Suggested next workflow action for the agent. |
| `warnings` | Non-fatal warnings. |
| `error` | Structured error object when applicable. |
| `stop_reason` | Why fallback stopped, for example `usable_data_found`, `max_attempts_reached`, `unsafe_escalation`, `no_available_adapter`, `not_found`, or `auth_required`. |
| `meta` | Contract version and debug-safe metadata. |

### `data_status` Enum

Use these values initially:

```text
present
empty
partial
failed_quality_gate
blocked
not_found
auth_required
permission_denied
error
artifact_only
not_applicable
running
```

### Status Semantics

| `data_status` | `ok` | `has_data` | Fallback? | Notes |
|---|---:|---:|---:|---|
| `present` | true | true | no | Usable inline result. |
| `artifact_only` | true | true | no | Usable result exists as artifact; use `read_artifact`. |
| `empty` | true | false | yes | Success but no relevant data. |
| `partial` | true | true/false | strategy-dependent | May continue if task requires stronger evidence. |
| `failed_quality_gate` | true | false | yes | Response exists but below quality threshold. |
| `blocked` | true | false | yes, if safer/stronger tool allowed | Captcha, Cloudflare, forbidden, JS wall, etc. |
| `not_found` | true | false | no | The tool completed and determined the URL/object does not exist. |
| `auth_required` | true | false | no | The tool completed and determined human/operator auth is required. |
| `permission_denied` | true | false | no | The tool completed and determined access is denied unless permissions change. |
| `error` | false | false | maybe | Continue only if retryable/safe. |
| `not_applicable` | true | false | context-dependent | Terminal for successful write/control operations with no data; non-terminal for skipped reserved adapters inside a fallback chain. |
| `running` | true | false | no | Poll job. |

---

## Mapping from Existing `CaptureAttempt`

| Existing `CaptureAttempt.status` | ToolResult mapping |
|---|---|
| `passed` | `ok=true`, `has_data=true`, `data_status=present` |
| `failed_quality_gate` | `ok=true`, `has_data=false`, `data_status=failed_quality_gate` |
| `blocked` | `ok=true`, `has_data=false`, `data_status=blocked` |
| `error` | `ok=false`, `has_data=false`, `data_status=error` |

Map `recommended_next_adapter` to `next_tool`.

Quality evidence maps directly:

```text
status_code -> data_quality.status_code
word_count -> data_quality.word_count
link_count -> data_quality.link_count
document_link_count -> data_quality.document_link_count
failure_reason -> data_quality.failure_reasons[]
```

---

## Error Schema

Do not leave `error` as an unstructured dict in the contract. Use a predictable object:

```json
{
  "code": "timeout",
  "message": "Request timed out",
  "retryable": true,
  "safe_to_escalate": true,
  "exception_type": "ReadTimeout"
}
```

Fallback may continue on `data_status=error` only when both `retryable=true` and `safe_to_escalate=true`. Retryable errors that are not safe to escalate must stop or back off instead of moving to a stronger adapter.

---

## HTTP / Capture Status Mapping

The first implementation must define deterministic mapping rules instead of treating every failure as generic `error`:

| Evidence | `data_status` | Fallback behavior |
|---|---|---|
| 2xx + passes quality gates | `present` | stop |
| 2xx + empty body / no useful text | `empty` or `failed_quality_gate` | continue |
| 2xx + blocked marker / JS wall / captcha | `blocked` | continue only to an allowed stronger adapter |
| 401 | `auth_required` | stop |
| 403 | `permission_denied` or `blocked` based on body/headers | stop or continue by policy; must be tested |
| 404/410 | `not_found` | stop |
| 429 | `error` with `retryable=true`, `safe_to_escalate=false` by default | normally stop / backoff, do not hammer |
| 5xx / timeout | `error` with retryability and `safe_to_escalate` | continue only when both retryable and safe to escalate |

---

## Fallback Strategies

### Adapter availability rule

First implementation must execute only probe-capable adapters returned by the runtime catalog. In the current codebase, `build_builtin_adapters()` only creates executable adapters for:

```text
web_http
browser_rendered
cloakbrowser
```

`sitemap`, `rss`, and `batch_python` are currently reserved/non-probe-capable. The fallback engine must either skip them with a structured skipped-attempt record or wait until real adapters exist. Do not let the default chain fail or stop merely because a reserved adapter appears in the strategy. A skipped reserved adapter with `data_status=not_applicable` is non-terminal in an acquisition fallback chain; continue to the next eligible adapter if one remains.

Skipped attempt example:

```json
{
  "tool": "sitemap",
  "ok": true,
  "has_data": false,
  "data_status": "not_applicable",
  "skipped": true,
  "reason": "adapter is reserved / not probe-capable in this build"
}
```

### `public_web_default`

```text
web_http -> browser_rendered -> sitemap -> rss
```

Use for ordinary public pages when the goal is broad monitoring/discovery.

### `document_discovery`

```text
web_http -> sitemap -> browser_rendered -> rss
```

Use when the goal is to discover reports, PDFs, downloadable filings, or document links. Static HTML and sitemaps are often cheaper and more reliable than rendering.

### `dynamic_page_default`

```text
web_http -> browser_rendered -> sitemap -> rss
```

Use when the operator or prior evidence suggests JavaScript-rendered pages.

### `authorized_fallback`

```text
web_http -> browser_rendered -> cloakbrowser -> batch_python
```

Use only when both are true:

```json
{
  "allow_stealth_browser": true,
  "require_authorized_access": true
}
```

`cloakbrowser` and `batch_python` must never be automatic fallbacks for arbitrary public URLs.

---

## Fallback Decision Rules

### Stop Conditions

Stop trying more tools when any condition is true:

```text
data_status == present
data_status == artifact_only
data_status == not_found
data_status == auth_required
data_status == permission_denied
data_status == not_applicable for a non-acquisition control/write operation or another explicitly terminal context
data_status == running
max_attempts reached
no next tool exists
next tool is unsafe for the current safety policy
```

### Continue Conditions

Continue to the next tool when:

```text
data_status == empty
data_status == failed_quality_gate
data_status == blocked
data_status == partial and strategy requires stronger evidence
data_status == error and error.retryable == true and error.safe_to_escalate == true
data_status == not_applicable and the attempt is a skipped reserved adapter in an acquisition fallback chain
```

### Partial Data Rule

`partial` should be goal-aware:

- If goal is “get page text” and `word_count` is high, stop.
- If goal is “find documents” and `document_link_count == 0`, continue.
- If goal is “discover site sections” and `link_count` is low, continue.

---

## Proposed MCP Tools

### 1. `web_listening_list_acquisition_tools`

Purpose: Return available acquisition adapters and their safety/runtime status.

Output should wrap existing `acquisition_tools_catalog()` in `ToolResult`.

### 2. `web_listening_probe_tool_once`

Purpose: Run exactly one adapter attempt.

Input:

```json
{
  "url": "https://example.com",
  "site_key": "example",
  "adapter": "web_http",
  "quality_gates": {
    "min_words": 120,
    "min_links": 3,
    "min_document_links": 0
  },
  "safety": {
    "allowed_domains": ["example.com"],
    "allow_stealth_browser": false,
    "require_authorized_access": false
  }
}
```

Output: `ToolResult` mapped from `CaptureAttempt`.

### 3. `web_listening_recommend_next_tool`

Purpose: Pure decision helper. It does not fetch network resources.

Input:

```json
{
  "strategy": "public_web_default",
  "attempts": [
    {
      "tool": "web_http",
      "data_status": "failed_quality_gate",
      "data_quality": {
        "word_count": 10,
        "link_count": 1
      }
    }
  ],
  "safety": {
    "allow_stealth_browser": false,
    "require_authorized_access": false
  }
}
```

Output:

```json
{
  "ok": true,
  "has_data": false,
  "data_status": "not_applicable",
  "data": {
    "next_tool": "browser_rendered",
    "reason": "HTTP returned too little text; browser rendering may expose JS-rendered content."
  }
}
```

### 4. `web_listening_acquire_with_fallback`

Purpose: The main agent-facing high-level tool. It automatically runs a strategy chain.

Input:

```json
{
  "url": "https://example.com",
  "site_key": "example",
  "goal": "find public reports and document links",
  "strategy": "document_discovery",
  "max_attempts": 4,
  "quality_gates": {
    "min_words": 120,
    "min_links": 3,
    "min_document_links": 1
  },
  "safety": {
    "allowed_domains": ["example.com"],
    "allow_stealth_browser": false,
    "require_authorized_access": false
  },
  "inline_content_limit": 20000
}
```

Output:

```json
{
  "ok": true,
  "has_data": true,
  "data_status": "present",
  "tool": "browser_rendered",
  "winning_tool": "browser_rendered",
  "attempt_count": 2,
  "attempts": [
    {
      "tool": "web_http",
      "has_data": false,
      "data_status": "failed_quality_gate",
      "reason": "word_count 10 < min_words 120"
    },
    {
      "tool": "browser_rendered",
      "has_data": true,
      "data_status": "present",
      "word_count": 860,
      "link_count": 42,
      "document_link_count": 7
    }
  ],
  "data": {
    "final_url": "https://example.com",
    "content_text_preview": "...",
    "links_preview": []
  }
}
```

---

## Returning Actual Data

A `CaptureAttempt` currently records quality metrics, but it does not preserve the actual `FetchResult` content. The fallback engine must decide how usable data is returned. Recommended first version:

1. Return quality metrics for every attempt.
2. Return small safe previews for the winning attempt:
   - `final_url`
   - `content_text_preview` or `markdown_preview`
   - optional `links_preview` when available
3. If content exceeds `inline_content_limit`, save an artifact under `WL_DATA_DIR` and return `data_status=artifact_only` or `present` with an artifact reference.
4. Never return raw cookies, auth headers, secrets, or local environment details.

This avoids a fake success where the engine says `has_data=true` but gives the agent no usable evidence.

---

## Quality Gates

Caller-provided quality gates must actually affect the capture evaluation. First implementation must support at least:

```json
{
  "min_words": 120,
  "min_links": 3,
  "min_document_links": 1
}
```

Implementation options:

1. Extend `probe_acquisition_url` / lower-level acquisition helpers to accept `quality_gates`; or
2. Build an `AcquisitionProfile` inside the core fallback engine and inject the quality gates before calling `run_capture_attempt`.

Acceptance test: the same fake adapter response must stop when `min_document_links=0` and continue/fail when `min_document_links=1` with `document_link_count=0`.

Every `ToolResult` must include both requested and effective quality gates, not only observed `data_quality`. `requested` records the caller input; `effective` records the thresholds actually applied after defaults, goal presets, and adapter-specific normalization. If the caller omits a threshold, include the default in `effective` and omit or null it in `requested`.

---

## Safety Rules

1. Validate that all input URLs are `http` or `https`.
2. Enforce `allowed_domains` for every adapter attempt, including redirects: after redirects complete, validate the `final_url` host still belongs to `allowed_domains` before marking the result usable or returning previews/artifacts.
3. Do not pass secrets, cookies, auth headers, local `.env` values, or browser profiles through MCP responses.
4. Do not enable `cloakbrowser` unless the profile says both:
   - `allow_stealth_browser=true`
   - `require_authorized_access=true`
5. `batch_python` must remain disabled/reserved until there is a reviewed script allowlist.
6. Do not return full HTML/content when it exceeds `inline_content_limit`; write/read artifacts instead.
7. Return blocked/auth/permission states structurally instead of hiding them as generic errors.

---

## Implementation Tasks

### Task 1: Add the shared MCP result model

**Objective:** Create a reusable `ToolResult` envelope and conversion helper for acquisition attempts.

**Files:**

- Create: `web_listening/contracts/__init__.py`
- Create: `web_listening/contracts/tool_result.py`
- Test: `tests/test_tool_result.py`

**Implementation Notes:**

Define:

```python
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, Field

DataStatus = Literal[
    "present",
    "empty",
    "partial",
    "failed_quality_gate",
    "blocked",
    "not_found",
    "auth_required",
    "permission_denied",
    "error",
    "artifact_only",
    "not_applicable",
    "running",
]

class DataQuality(BaseModel):
    passed: bool = False
    score: float | None = None
    status_code: int | None = None
    word_count: int = 0
    link_count: int = 0
    document_link_count: int = 0
    blocked: bool = False
    failure_reasons: list[str] = Field(default_factory=list)

class QualityGateSet(BaseModel):
    min_words: int | None = None
    min_links: int | None = None
    min_document_links: int | None = None

class AppliedQualityGates(BaseModel):
    requested: QualityGateSet = Field(default_factory=QualityGateSet)
    effective: QualityGateSet = Field(default_factory=QualityGateSet)

class ToolResult(BaseModel):
    ok: bool
    has_data: bool
    data_status: DataStatus
    data_count: int = 0
    tool: str = ""
    data_quality: DataQuality = Field(default_factory=DataQuality)
    quality_gates: AppliedQualityGates = Field(default_factory=AppliedQualityGates)
    data: dict[str, Any] = Field(default_factory=dict)
    artifacts: dict[str, Any] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    next_tool: str | None = None
    next_action: dict[str, Any] | None = None
    warnings: list[str] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    stop_reason: str | None = None
    meta: dict[str, Any] = Field(default_factory=lambda: {
        "contract_version": "web-listening-tool-result.v1"
    })
```

Add:

```python
def tool_result_from_capture_attempt(attempt: CaptureAttempt) -> ToolResult:
    ...
```

**Tests:**

- `passed` maps to `present` / `has_data=true`.
- `failed_quality_gate` maps to `failed_quality_gate` / `has_data=false`.
- `blocked` maps to `blocked` / `has_data=false`.
- `error` maps to `error` / `ok=false`.
- `recommended_next_adapter` maps to `next_tool`.
- Requested/effective `quality_gates` are present and reflect caller input plus applied defaults.

---

### Task 2: Add core fallback strategy and decision helpers

**Objective:** Implement deterministic fallback chain selection in the shared core layer so CLI/API/MCP can all reuse it.

**Files:**

- Create: `web_listening/blocks/acquisition_fallback.py`
- Test: `tests/test_acquisition_fallback.py`

**Implementation Notes:**

Add strategy chains:

```python
DEFAULT_CHAINS = {
    "public_web_default": ["web_http", "browser_rendered", "sitemap", "rss"],
    "document_discovery": ["web_http", "sitemap", "browser_rendered", "rss"],
    "dynamic_page_default": ["web_http", "browser_rendered", "sitemap", "rss"],
    "authorized_fallback": ["web_http", "browser_rendered", "cloakbrowser", "batch_python"],
}
```

Add:

```python
def should_continue(result: ToolResult, *, require_documents: bool = False) -> bool:
    ...


def choose_next_tool(
    strategy: str,
    attempts: list[ToolResult],
    *,
    allow_stealth_browser: bool = False,
    require_authorized_access: bool = False,
) -> str | None:
    ...
```

**Tests:**

- `failed_quality_gate` after `web_http` recommends next strategy tool.
- `present` stops.
- `not_found` stops.
- `blocked` can continue to `browser_rendered`.
- `cloakbrowser` is skipped unless both safety flags are true.
- `batch_python` is skipped until explicitly enabled in a later PR.

---

### Task 3: Add core acquisition result object and fallback execution

**Objective:** Create a core acquisition result and core fallback execution function before adding MCP handlers.

**Files:**

- Create: `web_listening/blocks/acquisition_result.py`
- Modify: `web_listening/blocks/acquisition_tools.py` or `web_listening/blocks/acquisition_capture.py` as needed
- Test: `tests/test_acquisition_fallback_execution.py`

**Core functions:**

```python
def probe_tool_once_result(...) -> ToolResult:
    ...


def acquire_with_fallback_result(...) -> ToolResult:
    ...
```

**Implementation Notes:**

- Do not implement fallback by repeatedly calling the existing CLI/API helper and parsing dicts.
- Build/resolve `AcquisitionProfile` once.
- Apply caller-provided quality gates to that profile before attempts run.
- Build executable adapters once.
- Loop through core capture attempts while preserving attempt summaries.
- Treat skipped reserved adapters (`sitemap`, `rss`, `batch_python` until implemented) as non-terminal skipped attempts; do not stop the default chain solely because a skipped attempt reports `not_applicable`.
- Return safe inline preview or artifact reference, not just quality metrics.
- Preserve all attempts in `attempts`.
- Stop as soon as `has_data=true` and `data_status=present` unless goal-specific rules require more.

**Tests:**

Use fake adapters, no network.

- First adapter fails quality gate, second passes.
- All adapters fail; result includes every attempt and `has_data=false`.
- Safety flags prevent `cloakbrowser` fallback.
- Skipped reserved adapters do not terminate a chain when later eligible adapters remain.
- `max_attempts` is respected.

---

### Task 4: Add MCP adapter functions and server entrypoint

**Objective:** Expose the core fallback functions through thin MCP handlers and a stdio MCP server.

**Files:**

- Create: `web_listening/mcp/__init__.py`
- Create: `web_listening/mcp/tools.py`
- Create: `web_listening/mcp/server.py`
- Modify: `pyproject.toml`
- Test: `tests/test_mcp_server.py` or a lightweight import/registration test

**Implementation Notes:**

Add optional dependency:

```toml
[project.optional-dependencies]
mcp = [
    "mcp>=1.0.0",
]
```

Add script:

```toml
[project.scripts]
web-listening = "web_listening.cli:app"
web-listening-mcp = "web_listening.mcp.server:main"
```

Expose tools:

```text
web_listening_list_acquisition_tools
web_listening_probe_tool_once
web_listening_recommend_next_tool
web_listening_acquire_with_fallback
```

Keep the server thin. It should validate inputs, call `web_listening.mcp.tools`, and return JSON-serializable `ToolResult.model_dump(mode="json")`.

---

### Task 5: Add docs and examples

**Objective:** Document how Hermes or another agent connects to the MCP server.

**Files:**

- Create: `docs/design/MCP_FALLBACK_CHAIN_DESIGN.md`
- Modify: `README.md`

**Docs should include:**

Hermes config example:

```yaml
mcp_servers:
  web_listening:
    command: "web-listening-mcp"
    args: []
    timeout: 300
    connect_timeout: 60
```

Example agent-facing call:

```json
{
  "url": "https://example.com/reports",
  "site_key": "example",
  "goal": "find public report/document links",
  "strategy": "document_discovery",
  "quality_gates": {
    "min_words": 120,
    "min_links": 3,
    "min_document_links": 1
  }
}
```

---

## Later PRs

### PR: Workflow MCP tools

Add:

```text
web_listening_bootstrap_scope
web_listening_run_scope
web_listening_report_scope
web_listening_export_manifest
web_listening_get_job
web_listening_read_artifact
```

Map job payloads into `ToolResult`:

- completed with artifact -> `artifact_only`
- running -> `running`
- failed -> `error`

### PR: Artifact reading policy

Add safe artifact reader:

- Only read files under `WL_DATA_DIR`.
- Refuse path traversal.
- Inline only below size limit.
- Return path + metadata for large artifacts.

### PR: Goal-aware quality policies

Add first-class goal presets:

```text
page_text
section_discovery
document_discovery
change_monitoring
```

Each preset maps to default quality gates and partial-data behavior.

---

## Review-Driven Changes Applied

An independent agent review flagged several issues. The accepted changes are:

1. Move fallback ownership from `web_listening/mcp/fallback.py` to shared core, likely `web_listening/blocks/acquisition_fallback.py`.
2. Keep MCP handlers thin; they serialize core results and expose tool schemas only.
3. Filter or skip reserved adapters (`sitemap`, `rss`, `batch_python`) until they are actually probe-capable.
4. Ensure caller-supplied quality gates affect `has_data` decisions.
5. Add a real data return policy because existing `CaptureAttempt` only carries metrics.
6. Define HTTP/status/error mapping, including 401/403/404/429/5xx behavior.
7. Add structured `error.retryable` and `error.safe_to_escalate`.
8. Add `stop_reason` for final result explainability.
9. Add explicit tests for adapter availability, reserved adapter skipping, domain/redirect safety, quality gate behavior, and attempt preservation.

Rejected or deferred review suggestions:

- Full goal preset system is deferred, but first PR should implement enough goal-aware behavior for `document_discovery` quality gates.
- Strong typed `AttemptSummary` is desirable; first PR may use a Pydantic model if it does not slow delivery, otherwise it should at least document the attempt dict shape.

---

## Acceptance Criteria for First MCP PR

1. Shared `ToolResult` exists outside the MCP transport layer and is tested.
2. `CaptureAttempt` maps into `ToolResult` correctly.
3. Core fallback engine, not MCP-only code, can recommend `browser_rendered` after `web_http` failure.
4. `acquire_with_fallback_result` tries a second adapter when the first has `has_data=false`.
5. Successful second attempt stops the chain and records `winning_tool`.
6. Failed full chain returns all attempts and a useful structured reason.
7. `cloakbrowser` is not used unless safety flags permit it.
8. Reserved adapters are skipped or reported clearly instead of causing accidental failures.
9. Caller-supplied quality gates affect fallback decisions.
10. No network is required for unit tests.
11. Existing CLI/API tests continue to pass.
12. Docs explain how an agent should use the fallback MCP entrypoint.

---

## Recommended Verification Commands

```bash
pytest tests/test_tool_result.py -v
pytest tests/test_acquisition_fallback.py -v
pytest tests/test_acquisition_fallback_execution.py -v
pytest tests/test_mcp_server.py -v
pytest tests/test_cli.py tests/test_api.py -v
```

If optional MCP SDK is installed:

```bash
python -m web_listening.mcp.server --help
web-listening-mcp --help
```

---

## Design Decision Summary

The most important product decision is this:

```text
Individual acquisition tools report whether they got usable data.
The fallback orchestrator, not the LLM, decides when to try tool #2 or #3.
The agent receives the full attempt chain plus a clean final state.
```

This makes `web_listening` much more reliable as an agent capability than a simple CLI wrapper would be.
