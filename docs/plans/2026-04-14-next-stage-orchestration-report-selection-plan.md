# Next Stage Execution Plan: Orchestration v2, Report Contract v3, and Agent-First Selection

> **For Hermes:** Execute this plan after PRing the current `feat/mainline-cli-quality-foundation` branch. Use small commits, tests after each task, and keep the staged workflow as the primary product path.

**Goal:** Advance `web_listening` from a stable Phase 1-3 foundation into a more production-ready agent backend by deepening orchestration, making report delivery more actionable, and starting the first real agent-first differentiation layer.

**Architecture:** Build on the current staged workflow, task/report contracts, and storage-backed job model. Do not replace the deterministic evidence pipeline. Instead, make jobs more execution-aware, make reports more directly consumable by downstream agents, and add recommendation/semantic layers on top of existing deterministic artifacts.

**Tech Stack:** Python 3.11, FastAPI, Typer, Pydantic, SQLite-backed storage, pytest.

---

## Workstreams

### Workstream A — Job orchestration v2

**Objective:** Upgrade the current synchronous job records into a more execution-aware orchestration layer.

**Target outcomes:**
- real job lifecycle states: `queued`, `running`, `completed`, `failed`
- progress values that reflect actual execution stage
- structured error payloads instead of plain strings
- cleaner artifact payloads for polling and webhook-style delivery

### Workstream B — Tracking report delivery contract v3

**Objective:** Make tracking reports more actionable for downstream agents and operators.

**Target outcomes:**
- stronger severity engine
- richer review queue semantics
- standardized artifact index
- explicit handoff payload / next action contract

### Workstream C — Agent-assisted selection + semantic diff foundation

**Objective:** Start adding genuine agent-first differentiation without weakening the deterministic core.

**Target outcomes:**
- selection recommendation output
- semantic diff bundle scaffolding
- importance ranking primitives
- downstream handoff hooks for future automation

---

## Workstream A — Detailed Plan

### Task A1: Expand the job model into a richer execution contract

**Files:**
- Modify: `web_listening/models.py`
- Modify: `web_listening/blocks/storage.py`
- Modify: `tests/test_storage.py`

**Add fields:**
- `stage`
- `stage_message`
- `progress`
- `error_code`
- `error_detail`
- `is_retryable`
- `artifact_summary`

**Acceptance criteria:**
- jobs can distinguish stage from status
- failures are no longer only free-form strings
- storage round-trip preserves the richer job payload

---

### Task A2: Add staged progress updates to workflow job execution

**Files:**
- Modify: `web_listening/blocks/job_orchestration.py`
- Possibly modify: `web_listening/blocks/staged_workflow.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_api.py`

**Execution stages to support:**
- `accepted`
- `loading_scope`
- `executing_workflow`
- `writing_artifacts`
- `completed`
- `failed`

**Acceptance criteria:**
- long-running staged operations update stage/progress in storage
- CLI/API job inspection can surface those fields

---

### Task A3: Add webhook-ready job serialization

**Files:**
- Modify: `web_listening/models.py`
- Modify: `web_listening/api/routes.py`
- Add tests for job payload structure

**Acceptance criteria:**
- one stable job payload can be reused for polling or webhook delivery
- produced artifacts and next recommended action are included consistently

---

### Task A4: Add optional webhook registration stub / payload contract

**Files:**
- Modify: `web_listening/api/routes.py`
- Possibly create: `web_listening/blocks/webhook_delivery.py`
- Add tests for payload generation (not necessarily network delivery yet)

**Acceptance criteria:**
- minimal webhook schema exists
- repo documents what a job completion event payload looks like
- delivery can remain stubbed or local in this stage if needed

---

## Workstream B — Detailed Plan

### Task B1: Upgrade severity rules from flat mapping to structured policy evaluation

**Files:**
- Modify: `web_listening/models.py`
- Modify: `web_listening/blocks/monitor_task.py`
- Modify: `web_listening/blocks/tracking_report.py`
- Modify: `tests/test_monitor_task.py`
- Modify: `tests/test_tracking_report.py`

**Add support for:**
- rule-by-change-type
- rule-by-prefix
- rule-by-file-type
- rule-by-keyword (simple first pass)

**Acceptance criteria:**
- report priority summary is driven by structured policy, not only fixed defaults

---

### Task B2: Enrich the review queue

**Files:**
- Modify: `web_listening/blocks/tracking_report.py`
- Modify: `tests/test_tracking_report.py`

**Review queue entries should include:**
- `reason`
- `severity`
- `entity_type`
- `entity_url`
- `recommended_action`

**Acceptance criteria:**
- review queue is usable directly by downstream agents or humans

---

### Task B3: Standardize artifact index and handoff payload

**Files:**
- Modify: `web_listening/blocks/tracking_report.py`
- Possibly modify: `web_listening/blocks/document_manifest.py`
- Modify docs and tests

**Artifact groups:**
- control plane
- evidence plane
- explanation plane
- status plane

**Acceptance criteria:**
- tracking report includes a stable artifact index structure
- one handoff payload clearly states what downstream system should read next

---

### Task B4: Add explicit top-level next-action contract

**Files:**
- Modify: `web_listening/blocks/tracking_report.py`
- Modify: `tests/test_tracking_report.py`

**Fields to add:**
- `next_action`
- `escalation_needed`
- `review_required_count`
- `high_priority_count`

**Acceptance criteria:**
- report becomes a direct decision object, not only a descriptive artifact

---

## Workstream C — Detailed Plan

### Task C1: Add recommendation output to section selection flow

**Files:**
- Modify: `web_listening/blocks/section_classifier.py`
- Possibly create: `web_listening/blocks/selection_recommendation.py`
- Modify or add tests for recommendation output

**Recommendation buckets:**
- `selected_recommended`
- `deferred_recommended`
- `rejected_recommended`

**Each recommendation should include:**
- reason
- expected value
- risk note
- likely effect on coverage

**Acceptance criteria:**
- selection stage can emit a first-pass machine recommendation without removing human review

---

### Task C2: Add semantic diff bundle scaffolding

**Files:**
- Create: `web_listening/blocks/semantic_diff.py`
- Modify: `web_listening/blocks/tracking_report.py`
- Add tests

**First-pass semantic units:**
- heading changes
- list item additions/removals
- table row additions/removals
- publish-date / title changes

**Acceptance criteria:**
- deterministic semantic bundle exists, even before any AI summary layer

---

### Task C3: Add lightweight importance ranking primitives

**Files:**
- Modify: `web_listening/blocks/tracking_report.py`
- Possibly modify: `web_listening/models.py`
- Add tests

**Inputs:**
- severity rules
- semantic diff bundle
- file type rules
- focus topics

**Outputs:**
- ranked changes
- rationale snippets

**Acceptance criteria:**
- report can say not only what changed, but why it probably matters

---

### Task C4: Add downstream handoff hooks

**Files:**
- Modify: `web_listening/blocks/tracking_report.py`
- Possibly create: `web_listening/blocks/handoff_payload.py`
- Add tests/docs

**Acceptance criteria:**
- report can emit a handoff-friendly payload for future doc extraction, alerting, or downstream agent action

---

## Suggested PR Split

### PR 1
**Title:** `feat: upgrade job orchestration lifecycle and delivery contract`

Scope:
- Workstream A

### PR 2
**Title:** `feat: enrich tracking report delivery contract`

Scope:
- Workstream B

### PR 3
**Title:** `feat: add selection recommendations and semantic diff foundation`

Scope:
- Workstream C

---

## Verification Strategy

### For every PR

Run focused tests first, then full suite.

**Focused examples:**
```bash
python -m pytest tests/test_storage.py tests/test_cli.py tests/test_api.py -q
python -m pytest tests/test_monitor_task.py tests/test_tracking_report.py -q
```

**Full suite:**
```bash
python -m pytest tests -q
```

---

## Final Acceptance Criteria

By the end of the next stage:
- jobs are execution-aware, not just completed records
- reports are directly actionable and handoff-friendly
- selection emits recommendation artifacts
- semantic diff scaffolding exists and feeds importance ranking
- deterministic evidence remains the foundation under all higher-level intelligence
