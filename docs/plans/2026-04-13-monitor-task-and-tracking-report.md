# Monitor Task + Tracking Report Implementation Plan

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** Add a first-class task artifact and a unified tracking report artifact so `web_listening` is easier to drive as an agent skill and easier to review after bootstrap/rerun workflows.

**Architecture:** Keep the current near-term interface strategy intact: stabilize local artifact contracts first instead of rushing REST/MCP wrappers. Add a new control-plane artifact (`monitor_task.yaml`) and a new explanation artifact (`tracking_report.md` / YAML) that wrap existing planning, bootstrap, manifest, and rerun data without inventing a second crawler path.

**Tech Stack:** Python 3.11, Pydantic models, dataclass-based artifact renderers, Typer CLI, pytest.

---

## Scope for this branch

This branch intentionally focuses on the highest-leverage, low-regret items from the review:

1. **First-class monitor task contract**
   - Define a stable task artifact that captures task description, goal, focus topics, file preferences, report style, and severity rules.
2. **Unified tracking report artifact**
   - Build one report that combines task context + scope context + run summary + document summary + recommended next actions.
3. **Packaged CLI entrypoints for the new artifacts**
   - Add commands that create/export the task artifact and tracking report from existing workflow outputs.
4. **Tests and docs**
   - Add regression tests and README/docs updates.

This branch does **not** attempt REST exposure, persistent jobs, or full auto-selection.

---

## Task 1: Add failing tests for the new monitor task artifact

**Objective:** Define the expected contract for a first-class task artifact before implementation.

**Files:**
- Create: `tests/test_monitor_task.py`
- Modify later: `web_listening/models.py`
- Create later: `web_listening/blocks/monitor_task.py`

**Step 1: Write failing tests**

Add tests that verify:
- a `MonitorTask` model accepts structured fields
- list-like fields normalize from strings/lists
- severity rules default sanely
- YAML rendering includes key task fields
- loading from YAML round-trips cleanly

**Step 2: Run test to verify failure**

Run:
```bash
python3 -m pytest tests/test_monitor_task.py -q
```
Expected: FAIL because the model/module does not exist yet.

**Step 3: Commit after implementation passes**

```bash
git add tests/test_monitor_task.py web_listening/models.py web_listening/blocks/monitor_task.py
git commit -m "feat: add monitor task artifact model"
```

---

## Task 2: Implement the monitor task artifact

**Objective:** Create a reusable, agent-readable task contract and file-based artifact helpers.

**Files:**
- Modify: `web_listening/models.py`
- Create: `web_listening/blocks/monitor_task.py`
- Possibly modify: `web_listening/blocks/__init__.py`

**Step 1: Implement minimal model support**

Add a `MonitorTask` Pydantic model with fields such as:
- `task_name`
- `site_url`
- `task_description`
- `goal`
- `focus_topics`
- `must_track_prefixes`
- `exclude_prefixes`
- `prefer_file_types`
- `must_download_patterns`
- `report_style`
- `change_severity_rules`
- `handoff_requirements`
- `notes`

**Step 2: Implement file artifact helpers**

In `web_listening/blocks/monitor_task.py`, add:
- dataclass or helper structure for YAML serialization
- `build_monitor_task(...)`
- `load_monitor_task(path)`
- `render_yaml_text(task)`
- a default task-path helper under `data/plans/`

**Step 3: Run focused tests**

```bash
python3 -m pytest tests/test_monitor_task.py -q
```
Expected: PASS.

**Step 4: Run broader regression slice**

```bash
python3 -m pytest tests/test_monitor_scope_planner.py tests/test_document_manifest.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_monitor_task.py web_listening/models.py web_listening/blocks/monitor_task.py
git commit -m "feat: add monitor task artifact model"
```

---

## Task 3: Add failing tests for a unified tracking report artifact

**Objective:** Lock down the report format before adding implementation.

**Files:**
- Create: `tests/test_tracking_report.py`
- Create later: `web_listening/blocks/tracking_report.py`

**Step 1: Write failing tests**

Add tests that verify a tracking report can:
- load a monitor scope + optional monitor task
- summarize a bootstrap or rerun run
- include changed/new/missing page and file counts
- include document manifest rows / totals where available
- render Markdown with the user-friendly “briefing” structure
- render YAML for agent consumption

**Step 2: Run test to verify failure**

Run:
```bash
python3 -m pytest tests/test_tracking_report.py -q
```
Expected: FAIL because the report module does not exist yet.

**Step 3: Commit after implementation passes**

```bash
git add tests/test_tracking_report.py web_listening/blocks/tracking_report.py
git commit -m "feat: add unified tracking report artifact"
```

---

## Task 4: Implement the tracking report artifact

**Objective:** Produce one explanation-layer artifact that combines task, scope, run, and document context.

**Files:**
- Create: `web_listening/blocks/tracking_report.py`
- Possibly modify: `web_listening/blocks/document_manifest.py`
- Possibly read from: `web_listening/blocks/bootstrap_summary.py`, `web_listening/blocks/storage.py`

**Step 1: Reuse existing storage data, do not fork logic**

The report should wrap existing persisted data rather than re-crawl or invent a second run pipeline.

**Step 2: Implement report builder**

Add functions like:
- `build_tracking_report(scope_path, storage, run_id=None, task_path=None)`
- `render_markdown(report)`
- `render_yaml_text(report)`

Report should include:
- report generation time
- task summary (if provided)
- scope identity
- run identity and status
- selected roots / focus prefixes
- totals for pages/files seen and changed
- document totals and sample rows
- high-signal conclusion bullets
- recommended next actions

**Step 3: Run focused tests**

```bash
python3 -m pytest tests/test_tracking_report.py -q
```
Expected: PASS.

**Step 4: Run neighboring regressions**

```bash
python3 -m pytest tests/test_bootstrap_summary.py tests/test_document_manifest.py tests/test_tree_crawler.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_tracking_report.py web_listening/blocks/tracking_report.py
 git commit -m "feat: add unified tracking report artifact"
```

---

## Task 5: Add packaged CLI commands for monitor-task and tracking-report

**Objective:** Expose the new stable artifact contracts through the packaged CLI without replacing the current tool-driven workflow.

**Files:**
- Modify: `web_listening/cli.py`
- Create/update tests: `tests/test_cli.py`

**Step 1: Write failing CLI tests**

Add tests for commands such as:
- `web-listening create-monitor-task`
- `web-listening export-tracking-report`

Verify:
- task file gets written
- report file gets written
- CLI output includes saved path

**Step 2: Run tests to verify failure**

```bash
python3 -m pytest tests/test_cli.py -q
```
Expected: FAIL for missing commands.

**Step 3: Implement commands in `cli.py`**

The commands should call the new block helpers and write artifacts to the default `data/plans` / `data/reports` locations unless an explicit output path is provided.

**Step 4: Re-run tests**

```bash
python3 -m pytest tests/test_cli.py -q
```
Expected: PASS.

**Step 5: Commit**

```bash
git add tests/test_cli.py web_listening/cli.py
git commit -m "feat: expose monitor task and tracking report in cli"
```

---

## Task 6: Update documentation and validation notes

**Objective:** Make the new artifact contracts discoverable to future humans and agents.

**Files:**
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `.codex/skills/web-listening-agent/references/current-api.md`
- Optionally modify: `skills/web-listening-tree-monitor/SKILL.md`

**Step 1: Document the new control-plane artifact**

Mention `monitor_task.yaml` in:
- current workflow docs
- control-plane artifact lists
- recommended agent workflow

**Step 2: Document the new explanation artifact**

Mention `tracking_report.md` / YAML in:
- report outputs
- agent-readable outputs
- next-step guidance

**Step 3: Run focused docs-adjacent regression tests**

```bash
python3 -m pytest tests/test_cli.py tests/test_tracking_report.py tests/test_monitor_task.py -q
```
Expected: PASS.

**Step 4: Commit**

```bash
git add README.md docs/README.md .codex/skills/web-listening-agent/references/current-api.md skills/web-listening-tree-monitor/SKILL.md
git commit -m "docs: document monitor task and tracking report artifacts"
```

---

## Task 7: Full verification, review, and PR prep

**Objective:** Verify the branch is stable and ready for review.

**Files:**
- No planned source changes unless fixes are needed

**Step 1: Run full test suite**

Run:
```bash
TZ=America/New_York python3 -m pytest tests -q
```
Expected: PASS, or at minimum no new failures beyond any known baseline.

**Step 2: Run targeted quality checks**

Run:
```bash
git diff --stat main...HEAD
python3 -m pytest tests/test_monitor_task.py tests/test_tracking_report.py tests/test_cli.py -q
```

**Step 3: Independent review**

Use the requesting-code-review skill before push/PR.

**Step 4: Push and open PR**

```bash
git push -u origin feat/monitor-task-tracking-report
```
Create a PR describing:
- new monitor task contract
- new tracking report artifact
- new packaged CLI commands
- tests added

---

## Acceptance Criteria

- `monitor_task.yaml` exists as a first-class, documented artifact.
- `tracking_report.md` and YAML exist as first-class, documented artifacts.
- The new report combines task + scope + run + document context in one place.
- The packaged CLI exposes the new artifact creation/export flow.
- Tests cover the new artifact builders and CLI commands.
- Changes land as multiple small commits with passing tests.
