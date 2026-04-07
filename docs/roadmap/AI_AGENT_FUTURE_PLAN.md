# AI Agent Future Plan

> Last updated: 2026-04-03  
> Context: This plan is derived from [RESEARCH_REPORT.md](../research/RESEARCH_REPORT.md) and the repo skill at `.codex/skills/web-listening-agent`.

## Goal

Turn `web_listening` from a website change detector into an agent-ready perception layer that can be safely used by:

- AI agents through MCP, tool calls, webhooks, and machine-readable artifacts
- traditional programs through stable REST APIs and the CLI
- human operators through auditable evidence, deterministic jobs, and structured outputs

## Executive Decision

`web_listening` should **keep the generic REST API**.

The recommended interface hierarchy is:

1. `blocks/*` as the canonical execution layer
2. REST API as the canonical remote contract
3. MCP server as the agent-first adapter over the same backend
4. CLI as the local operator and automation entry point
5. Skill as the agent operating guide

Do **not** replace the REST API with MCP.  
Do **not** make agent-facing logic bypass the REST or storage contracts.

## Product Positioning

The project should evolve toward this product shape:

```text
web_listening
  = acquisition + normalization + change intelligence + orchestration + agent interfaces
```

The most important shift is:

```text
from: "page changed"
to:   "here is the changed artifact, structured diff, evidence chain, and async state"
```

## Design Principles

### 1. Agent-readable first

Prefer:

- markdown over raw text
- structured JSON over prose-only summaries
- durable IDs over transient strings
- evidence pointers over opaque conclusions

### 2. One backend, many interfaces

Every new capability should be implemented once in the core blocks and then exposed through:

- REST
- CLI
- MCP
- scheduled automation

### 3. Async by default for long tasks

Checks, crawls, document downloads, extraction, and analysis should become durable jobs.

### 4. Evidence preservation

Every result should be traceable to:

- site
- snapshot
- document
- change
- analysis
- job
- URL and timestamp

### 5. Backward-compatible evolution

Existing REST clients should continue to work while the project gains agent-first features.

## Target Capabilities

The target agent-ready system should support:

| Capability | Why it matters for agents |
|---|---|
| Browser and HTTP acquisition | Agents need dynamic-site coverage, not only static HTML |
| Markdown and fit-markdown snapshots | Agents need concise content that is easier to reason over |
| Structured extraction and field-level diff | Agents need to answer what changed, not only that something changed |
| Durable jobs | Agents need polling, retries, and safe orchestration |
| Webhooks | Agents and automations should not be forced to poll |
| MCP tools and resources | Agents should consume the system as tools, not handwritten HTTP sequences |
| Stable REST API | Traditional programs, debugging, and integrations still need a generic contract |

## Workstreams

## Workstream A: Content Plane

Goal: produce better page artifacts for downstream AI usage.

Deliverables:

- `raw_html`
- `cleaned_html`
- `markdown`
- `fit_markdown`
- `metadata_json`
- `fetch_mode`
- `final_url`
- `status_code`

Primary files:

- `web_listening/models.py`
- `web_listening/blocks/crawler.py`
- `web_listening/blocks/storage.py`
- `tests/test_crawler.py`
- `tests/test_storage.py`

## Workstream B: Monitoring Intelligence

Goal: move from full-page diff toward rule-driven and schema-driven change detection.

Deliverables:

- recursive scope and tree monitoring
- selector-based watch rules
- document-link watch rules
- JSON schema extraction
- prompt-guided extraction hooks
- field-level diff payloads
- change severity and evidence links

Primary files:

- `web_listening/blocks/diff.py`
- `web_listening/blocks/tree_crawler.py` (new)
- `web_listening/blocks/extractor.py` (new)
- `web_listening/blocks/storage.py`
- `web_listening/models.py`
- `tests/test_diff.py`
- `tests/test_storage.py`

## Workstream C: Orchestration Plane

Goal: make checks and downloads durable, inspectable, and retryable.

Deliverables:

- `jobs` table
- job lifecycle states
- idempotent write requests
- webhook subscriptions and delivery logs
- scheduler that enqueues jobs instead of doing all work inline

Primary files:

- `web_listening/blocks/storage.py`
- `web_listening/blocks/scheduler.py`
- `web_listening/api/routes.py`
- `web_listening/models.py`
- `tests/test_scheduler.py`
- new tests for jobs and webhooks

## Workstream D: Agent Interface Plane

Goal: expose the same backend safely to both agents and traditional callers.

Deliverables:

- improved REST contracts
- MCP server
- repo skill updates
- CLI alignment with new job-based flows

Primary files:

- `web_listening/api/routes.py`
- `web_listening/api/app.py`
- `web_listening/cli.py`
- `mcp_server/*` or `web_listening/mcp/*` (new)
- `.codex/skills/web-listening-agent/*`

## Detailed Phase Plan

## Phase 1: Snapshot Normalization Foundation

Goal: create agent-readable page artifacts without breaking current behavior.

Implementation:

- Add snapshot fields for HTML, markdown, fit-markdown, metadata, fetch mode, final URL, and status code.
- Add a `normalizer` block to transform fetched HTML into normalized artifacts.
- Keep `content_text` temporarily for backward compatibility.
- Shift future diff logic to prefer `fit_markdown`, then `markdown`, then `content_text`.

Suggested schema additions:

- `site_snapshots.raw_html`
- `site_snapshots.cleaned_html`
- `site_snapshots.markdown`
- `site_snapshots.fit_markdown`
- `site_snapshots.metadata_json`
- `site_snapshots.fetch_mode`
- `site_snapshots.final_url`
- `site_snapshots.status_code`

Acceptance criteria:

- A new snapshot stores both legacy text and markdown-oriented artifacts.
- Existing tests still pass after schema migration.
- New tests prove that snapshots can be created and read with the extended schema.
- API responses expose enough fields for downstream agents to consume the snapshot directly.

## Phase 2: Browser Acquisition

Goal: handle JS-rendered pages and limited interactive flows.

Implementation:

- Split the current crawler into `HttpCrawler` and `BrowserCrawler`.
- Add optional Playwright support.
- Add site-level fetch configuration.
- Add browser debug artifacts when fetches fail or when pages require interaction.

Suggested configuration fields:

- `fetch_mode=http|browser|auto`
- `wait_for`
- `browser_steps`
- `storage_state_path`

Acceptance criteria:

- The system can capture a JS-rendered page through browser mode.
- Link extraction works on browser-rendered HTML.
- Browser mode remains optional and does not break HTTP-only installs.

## Phase 3: Structured Watch Rules

Before moving fully into selector or schema watch rules, the project should add bounded recursive scope monitoring.
See `../design/TREE_MONITORING_DESIGN.md` for the recommended model.

Key rules from that design:

- default recursive depth should be `3`
- HTML recursion should stay inside the scope page prefix
- file acceptance should be allowed to leave the direct parent path as long as the file stays under the same root web
- file dedupe should be driven by byte-level SHA-256, not just by file URL
- first recursive runs should bootstrap inventory rather than emitting normal change alerts

Suggested new tables before or alongside `watch_rules`:

- `crawl_scopes`
- `crawl_runs`
- `tracked_pages`
- `tracked_files`
- `page_edges`
- `file_observations`

Goal: model monitoring intent explicitly instead of treating all sites the same.

Implementation:

- Add watch rules per site.
- Support rule types for:
  - full page
  - CSS selector
  - XPath
  - document links
  - JSON schema extraction
  - prompt extraction
- Store extraction results and field-level diffs separately from plain text diffs.

Suggested new tables:

- `watch_rules`
- `extraction_results`

Suggested change payload fields:

- `change_payload_json`
- `severity`
- `evidence_snapshot_id`

Acceptance criteria:

- A site can have multiple watch rules.
- A rule can produce structured fields and field-level diffs.
- Changes expose machine-friendly payloads in addition to human summaries.

## Phase 4: Durable Jobs and Webhooks

Goal: make the system safe for autonomous orchestration.

Implementation:

- Add a `jobs` table and job state machine.
- Return jobs from all long-running write actions.
- Add webhook subscription support and delivery tracking.
- Convert the scheduler to enqueue work.

Suggested job types:

- `check_site`
- `download_documents`
- `analyze_changes`
- `crawl_site_section`
- `recompute_extraction`

Suggested job states:

- `queued`
- `running`
- `completed`
- `failed`
- `cancelled`

Acceptance criteria:

- `POST /sites/{id}/check` returns a job envelope.
- `GET /jobs/{id}` returns status, inputs, outputs, and errors.
- A webhook subscriber can receive state changes for jobs or crawl events.
- Duplicate submissions can be safely deduplicated or rejected via idempotency.

## Phase 5: Agent-Native Interface Layer

Goal: make the project directly usable by AI agents without removing the generic API.

Implementation:

- Add an MCP server on top of the same backend.
- Expose stable tools for common tasks.
- Expose resources for recent changes, latest snapshots, and analyses.
- Update the repo skill to match the final tool contracts.

Minimum MCP tools:

- `add_site`
- `list_sites`
- `check_site`
- `list_changes`
- `download_documents`
- `list_documents`
- `run_analysis`
- `get_latest_snapshot`
- `get_site_state`

Minimum MCP resources:

- `site://{id}/latest`
- `site://{id}/state`
- `changes://recent`
- `documents://site/{id}`
- `analysis://latest`

Acceptance criteria:

- An MCP client can operate the main workflows without custom scripting.
- MCP responses map cleanly to existing backend entities and IDs.
- MCP does not create a second source of truth or duplicate business logic.

## Phase 6: Hardening and Production Readiness

Goal: support more autonomous and high-confidence usage.

Implementation:

- Add observability for jobs, crawls, and webhook deliveries.
- Add rate-limit, retry, timeout, and circuit-breaker controls.
- Add artifact retention rules.
- Add compatibility tests across REST, CLI, and MCP.

Acceptance criteria:

- The same workflow can be invoked through REST, CLI, and MCP with equivalent results.
- Failure reasons are inspectable.
- Artifacts and evidence can be retained and queried predictably.

## API Strategy

## Recommendation

Keep the generic REST API and make it the stable backend contract.

Reasoning:

- Traditional programs still need a generic transport-neutral interface.
- MCP is excellent for agents, but it should adapt a stable backend, not replace it.
- REST is easier to test, debug, document, and monitor outside agent environments.
- Webhooks, batch jobs, and service-to-service integrations naturally build on the REST model.
- The CLI already aligns well with a protocol-neutral backend and should continue to do so.

## What should change in the API

Do not keep the API exactly as-is. Keep it, but upgrade it.

Required changes:

- return job envelopes from long-running writes
- expose machine-readable payloads, not only prose summaries
- provide explicit IDs and evidence links
- add latest-state and latest-snapshot endpoints
- add versioning discipline for new fields and breaking changes

Suggested contract direction:

```json
{
  "job_id": "123",
  "status": "queued",
  "accepted_at": "2026-04-03T12:00:00Z",
  "resource": {
    "site_id": 1
  }
}
```

## Interface layering model

Use this layering consistently:

```text
core blocks
  -> REST API
    -> MCP tools/resources
    -> CLI commands
    -> webhook/event consumers
```

This gives the project:

- one execution model
- one storage model
- one evidence model
- multiple entry points

## What not to do

- Do not build agent-only logic that bypasses the REST and storage contracts.
- Do not deprecate the REST API just because MCP is added.
- Do not make the CLI and MCP implement separate orchestration semantics.

## First 3 Iterations

If the team wants the highest-leverage path, use this order:

### Iteration 1

- Phase 1 only
- plus API envelope design for future jobs

### Iteration 2

- Phase 2
- Phase 4 job model for checks and downloads

### Iteration 3

- Phase 3 structured rules
- Phase 5 MCP server

## Delivery Checklist

- [ ] Snapshot schema extended with markdown-oriented artifacts
- [ ] Normalizer block added
- [ ] Browser crawler added behind optional dependency
- [ ] Watch rules and extraction results modeled
- [ ] Jobs and webhook subscriptions added
- [ ] REST responses upgraded for async workflows
- [ ] MCP server added on top of the stable backend
- [ ] Skill updated to reflect final contracts
- [ ] Cross-interface tests added
