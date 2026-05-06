# Agent-Facing Authority Map Plan

- Date: `2026-05-05`
- Repo: `web_listening`
- Branch target: `feat/agent-facing-authority-map`
- Source analysis: `docs/reports/agent_facing_tracking_tool_evaluation.md`
- Constraint for this round: implement `PR1` only; do not change Python runtime behavior

## PR1: Documentation and Skill Authority Map Cleanup

### Goal

Make the packaged staged CLI the canonical agent entrypoint across repo-facing docs and skill references. Reposition `tools/*.py` as compatibility or lower-level entrypoints, and remove Windows-only absolute Markdown links.

### Files

- `README.md`
- `docs/README.md`
- `.codex/skills/web-listening-agent/SKILL.md`
- `.codex/skills/web-listening-agent/references/current-api.md`
- `.codex/skills/web-listening-agent/references/interface-strategy.md`
- `docs/skills/OPENCLAW_SKILL_USAGE.md`
- `.hermes/project-status.md`

### Acceptance

- Docs consistently state that `web-listening discover/classify/select/plan-scope/bootstrap-scope/run-scope/report-scope/export-manifest` are the current canonical staged workflow entrypoints.
- Docs consistently state that `tools/*.py` remain available as compatibility, lower-level, or developer-oriented entrypoints.
- No Python runtime or CLI behavior changes are introduced.
- Windows absolute Markdown links under repo docs are replaced with repository-relative links.

### Verification

```bash
python -m pytest tests -q
git diff --check
python -m web_listening.cli --help
```

## PR2: CLI JSON Consistency

### Goal

Normalize machine-readable JSON output behavior across the staged CLI so agent orchestration can rely on one consistent pattern.

### Files

- `web_listening/cli.py`
- `tests/test_cli.py`
- `tests/test_tracking_report.py`
- `tests/test_manifest_contract_fixture.py`
- any focused contract docs that must reflect the CLI JSON surface

### Acceptance

- Staged CLI commands that expose structured results follow one consistent `--json` contract.
- Human-readable console output remains usable.
- Tests cover success-path JSON envelopes and stable keys for affected commands.

### Verification

```bash
python -m pytest tests/test_cli.py tests/test_tracking_report.py tests/test_manifest_contract_fixture.py -q
git diff --check
python -m web_listening.cli --help
python -m web_listening.cli report-scope --help
python -m web_listening.cli export-manifest --help
```

## PR3: Report Surface Convergence

### Goal

Reduce ambiguity in report-producing surfaces so the canonical report path is obvious to agents and operators.

### Files

- `web_listening/cli.py`
- `web_listening/blocks/tracking_report.py`
- `README.md`
- `docs/README.md`
- related tests for report commands and artifacts

### Acceptance

- Canonical report commands and compatibility aliases are explicitly separated.
- Report outputs, filenames, and handoff guidance are documented from one authority path.
- No duplicated or conflicting operator guidance remains in repo-facing docs.

### Verification

```bash
python -m pytest tests/test_cli.py tests/test_tracking_report.py -q
git diff --check
python -m web_listening.cli --help
```

## PR4: `monitor_intent` Artifact

### Goal

Introduce a single intent artifact that captures why a site is monitored, the business boundary, and the focus topics that currently live across multiple files.

### Files

- `web_listening/models.py`
- `web_listening/cli.py`
- `web_listening/blocks/monitor_scope_planner.py`
- `README.md`
- `docs/contracts/*` or staged artifact docs as needed
- focused tests covering artifact creation/loading

### Acceptance

- A stable `monitor_intent` artifact exists and is documented.
- The artifact can be created or derived without breaking existing scope/task workflows.
- Validation covers artifact shape and idempotent reruns.

### Verification

```bash
python -m pytest tests -q
git diff --check
python -m web_listening.cli --help
```
