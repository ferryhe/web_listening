# Agent Website Listening Phase 1-3 Execution Plan

> **For Hermes:** Execute this plan from the latest `origin/main` on a fresh feature branch, with small commits and tests after each task.

**Goal:** Turn the current artifact-oriented `web_listening` mainline into a clearer staged product interface: first unify the tree workflow behind packaged CLI commands and stronger baseline quality signals, then upgrade task/report contracts, then add job/status orchestration.

**Architecture:** Reuse the current staged workflow and existing storage-backed evidence model. Do not fork crawler logic. Move toward stable product interfaces by wrapping the existing planning/bootstrap/run/report path in reusable app/block helpers and thin CLI/API layers. Preserve legacy site-level monitoring as a compatibility path, not the primary product narrative.

**Tech Stack:** Python 3.11, Typer CLI, Pydantic models, APScheduler, pytest, existing SQLite-backed storage.

---

## Phase Boundaries

### Phase 1 — Mainline CLI + baseline quality + scheduler stability
- packaged staged workflow commands
- baseline quality summary
- scheduler timezone hardening
- docs update to make staged workflow the primary path

### Phase 2 — Task/report contract v2
- `MonitorTask` policy expansion
- `tracking_report` change bundle / priority summary / artifact index
- stable scope identity (`scope_id` / `scope_fingerprint`)

### Phase 3 — Job/status orchestration
- persistent job model
- `list-jobs` / `get-job`
- minimal REST entrypoints for bootstrap/run/report jobs

---

## Phase 1 — Detailed Execution Plan

### Task 1: Add a reusable staged workflow service layer

**Objective:** Stop making CLI integration depend on shelling out to top-level scripts.

**Files:**
- Create: `web_listening/blocks/staged_workflow.py`
- Read from: `tools/discover_site_sections.py`
- Read from: `tools/classify_site_sections.py`
- Read from: `tools/plan_monitor_scope.py`
- Read from: `tools/bootstrap_site_tree.py`
- Read from: `tools/run_site_tree.py`
- Test: `tests/test_cli.py`

**Steps:**
1. Extract or wrap the core logic from the current `tools/*.py` entrypoints into importable functions.
2. In `web_listening/blocks/staged_workflow.py`, add thin helpers such as:
   - `discover_sections(...)`
   - `classify_sections(...)`
   - `plan_scope(...)`
   - `bootstrap_scope(...)`
   - `run_scope(...)`
3. Ensure helpers return structured paths / ids / summary metadata instead of only printing.
4. Avoid changing crawler logic; only move interface and orchestration logic.

**Verification:**
```bash
python -m pytest tests/test_cli.py -q
```

**Commit:**
```bash
git add web_listening/blocks/staged_workflow.py tools/*.py tests/test_cli.py
git commit -m "refactor: add reusable staged workflow service layer"
```

---

### Task 2: Add packaged staged workflow CLI commands

**Objective:** Make the staged tree workflow accessible through `web-listening` directly.

**Files:**
- Modify: `web_listening/cli.py`
- Modify: `tests/test_cli.py`
- Optional docs touch later: `README.md`

**Commands to add:**
- `web-listening discover`
- `web-listening classify`
- `web-listening select` (if still artifact-driven, expose the selection file path clearly)
- `web-listening plan-scope`
- `web-listening bootstrap-scope`
- `web-listening run-scope`
- `web-listening report-scope`
- `web-listening export-manifest`

**Steps:**
1. Add failing CLI tests first for command registration and output path behavior.
2. Wire each command to the new staged workflow helpers or existing block-level helpers.
3. Make command output return structured, path-focused success messages.
4. Keep old site-level commands available.

**Verification:**
```bash
python -m pytest tests/test_cli.py -q
```

**Commit:**
```bash
git add web_listening/cli.py tests/test_cli.py
git commit -m "feat: expose staged workflow in packaged cli"
```

---

### Task 3: Add baseline quality summary to bootstrap outputs

**Objective:** Make bootstrap outputs explain whether the baseline is trustworthy, not only what was captured.

**Files:**
- Modify: `web_listening/blocks/bootstrap_summary.py`
- Possibly create: `web_listening/blocks/baseline_quality.py`
- Modify: `tests/test_bootstrap_summary.py`
- Read from: `web_listening/blocks/monitor_scope_planner.py`
- Read from: `web_listening/blocks/storage.py`

**Minimum fields to add:**
- `coverage_page_count`
- `coverage_file_count`
- `truncated_by_budget` / truncation reasons
- `selected_but_low_coverage_prefixes`
- `discovered_but_unselected_candidates` (best-effort, if available)
- `baseline_confidence`
- `recommended_followups`

**Steps:**
1. Write failing tests for quality statement rendering.
2. Reuse scope/run/storage facts already present; do not re-crawl.
3. Extend Markdown rendering with a distinct “baseline quality” section.
4. Keep summary useful for both humans and agents.

**Verification:**
```bash
python -m pytest tests/test_bootstrap_summary.py -q
```

**Commit:**
```bash
git add web_listening/blocks/bootstrap_summary.py tests/test_bootstrap_summary.py
git commit -m "feat: add baseline quality summary to bootstrap outputs"
```

---

### Task 4: Fix scheduler timezone instability

**Objective:** Remove the current environment-sensitive timezone failure from the test suite.

**Files:**
- Modify: `web_listening/blocks/scheduler.py`
- Modify: `tests/test_scheduler.py`

**Steps:**
1. Add failing regression coverage for conflicting local timezone config.
2. Ensure `IntervalTrigger` receives an explicit timezone rather than relying on local auto-detection.
3. Keep scheduler startup semantics unchanged.
4. Verify scheduler tests pass without depending on host `tzlocal` heuristics.

**Verification:**
```bash
python -m pytest tests/test_scheduler.py -q
python -m pytest tests -q
```

**Commit:**
```bash
git add web_listening/blocks/scheduler.py tests/test_scheduler.py
git commit -m "fix: harden scheduler timezone handling"
```

---

### Task 5: Update docs to make staged workflow the mainline

**Objective:** Align documentation with the new product entrypoints.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Possibly modify: skill docs referencing the old workflow shape

**Steps:**
1. Move packaged staged workflow commands into the primary “Recommended Workflow” section.
2. Move legacy site-level commands into a compatibility or legacy section.
3. Mention baseline quality summary in bootstrap outputs.
4. Keep examples consistent with current command names.

**Verification:**
```bash
python -m pytest tests/test_cli.py tests/test_bootstrap_summary.py -q
```

**Commit:**
```bash
git add README.md docs/README.md
git commit -m "docs: promote staged workflow as primary interface"
```

---

### Task 6: Phase 1 verification gate

**Objective:** Ensure Phase 1 is stable before moving on.

**Verification:**
```bash
python -m pytest tests/test_cli.py tests/test_bootstrap_summary.py tests/test_scheduler.py -q
python -m pytest tests -q
```

**Acceptance criteria:**
- packaged CLI exposes the staged workflow
- bootstrap outputs include baseline quality information
- scheduler tests are stable without manual TZ workarounds
- docs clearly present staged workflow as the primary path

---

## Phase 2 — Detailed Execution Plan

### Task 7: Expand `MonitorTask` into a stronger policy contract

**Files:**
- Modify: `web_listening/models.py`
- Modify: `web_listening/blocks/monitor_task.py`
- Modify: `tests/test_monitor_task.py`

**Add fields:**
- `run_schedule`
- `baseline_expectations`
- `file_policy`
- `report_policy`
- `alert_policy`
- `human_review_rules`

**Acceptance criteria:**
- YAML round-trip still works
- new policies are validated and documented
- task fields can drive later reporting decisions

---

### Task 8: Upgrade tracking report into a real change bundle

**Files:**
- Modify: `web_listening/blocks/tracking_report.py`
- Modify: `web_listening/blocks/storage.py`
- Modify: `tests/test_tracking_report.py`

**Add sections:**
- explicit `new/changed/missing` page and file bundles
- priority summary
- manual review queue
- artifact index

**Acceptance criteria:**
- report answers what changed, what matters, what needs review, and where artifacts live
- both Markdown and YAML remain supported

---

### Task 9: Add stable scope identity to scope/report flow

**Files:**
- Modify: `web_listening/blocks/monitor_scope_planner.py`
- Modify: `web_listening/blocks/scope_lookup.py`
- Modify: `tests/` for scope lookup/report generation

**Acceptance criteria:**
- reports can locate scopes by `scope_id` or `scope_fingerprint`
- path-matching is no longer the only lookup strategy

---

### Task 10: Phase 2 verification gate

**Verification:**
```bash
python -m pytest tests/test_monitor_task.py tests/test_tracking_report.py -q
python -m pytest tests/test_document_manifest.py tests/test_tree_crawler.py -q
python -m pytest tests -q
```

---

## Phase 3 — Detailed Execution Plan

### Task 11: Add persistent job model and storage support

**Files:**
- Modify: `web_listening/models.py`
- Modify: `web_listening/blocks/storage.py`
- Add tests for job persistence

**Fields:**
- `job_id`
- `job_type`
- `status`
- `progress`
- `scope_id`
- `run_id`
- `produced_artifacts`
- `error`
- `started_at`
- `finished_at`

---

### Task 12: Add CLI status commands

**Files:**
- Modify: `web_listening/cli.py`
- Modify: `tests/test_cli.py`

**Commands:**
- `web-listening list-jobs`
- `web-listening get-job`

---

### Task 13: Add minimal REST endpoints for bootstrap/run/report jobs

**Files:**
- Modify: `web_listening/api/app.py`
- Add API tests

**Endpoints:**
- `POST /monitor-tasks`
- `POST /monitor-scopes/{id}/bootstrap`
- `POST /monitor-scopes/{id}/run`
- `POST /monitor-scopes/{id}/report`
- `GET /jobs/{job_id}`
- `GET /monitor-scopes/{id}/reports/latest`
- `GET /monitor-scopes/{id}/manifest/latest`

---

### Task 14: Phase 3 verification gate

**Verification:**
```bash
python -m pytest tests/test_api.py tests/test_cli.py tests/test_storage.py -q
python -m pytest tests -q
```

---

## Working Rules for Execution

- small commits after each task
- run focused tests before broader tests
- preserve current crawler/storage logic where possible
- prefer reusable block/service helpers over CLI-only glue
- do not start semantic diff / AI enhancement until Phase 1 and 2 contracts are stable

---

## Final Acceptance Criteria

By the end of this plan:
- staged workflow is accessible through packaged CLI commands
- bootstrap outputs include baseline quality signals
- task/report contracts are strong enough for agent consumption
- jobs are queryable via CLI and minimal REST endpoints
- the repo narrative clearly points users toward the staged workflow, with legacy monitoring kept as compatibility functionality
