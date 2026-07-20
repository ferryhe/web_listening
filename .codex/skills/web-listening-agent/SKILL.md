---
name: web-listening-agent
description: Operate and extend the `web_listening` project as a governed website-monitoring platform for humans and AI agents.
---

# Web Listening Agent

## Read first

Read the repository root `README.md`. It is the sole active human-facing source for product scope, authority, interfaces, schemas, storage, safety, versions, and validation. Read `AGENTS.md` separately for repository workflow and boundary rules. Documents under `docs/archive/` are provenance only, not current authority.

## Canonical workflow

Use the packaged CLI:

1. `web-listening discover --catalog ...`
2. `web-listening classify --catalog ...`
3. review `section_selection.yaml`
4. `web-listening select --selection-path ...`
5. `web-listening plan-scope --selection-path ...`
6. compile/review the governed profile + Site Skill execution plan
7. `web-listening bootstrap-scope --scope-path ... --acquisition-profile-path ...`
8. `web-listening run-scope --scope-path ... --acquisition-profile-path ...`
9. `web-listening report-scope --scope-path ...`
10. `web-listening export-manifest --scope-path ...`

Do not bootstrap a new catalog before broad validation, draft-scope generation, and human scope confirmation. Lower-level `tools/*.py` programs are compatibility/developer wrappers.

## Authority and guardrails

- Formal bootstrap/run authority is the complete governed acquisition profile + Site Skill binding compiled into a validated non-empty execution plan before Storage opens.
- Picker and probe results are planning evidence, not formal authority.
- Keep `_blobs` as canonical SHA-256 storage and `_tracked` as the source-oriented view.
- Preserve scope/run/source/executor/Site Skill lineage, timestamps, and hashes in outputs.
- Keep traversal bounded and domain-constrained; require explicit authorization for stealth-capable runtimes.
- Site Skill registry inspection must remain static and side-effect free.

## Interface choice

Use `web-listening --help` and the root `README.md` for the full CLI inventory. Use `web-listening list-acquisition-tools --json` for the stable picker contract. Use the documented ten-tool MCP server when an MCP caller needs shared acquisition, scoped execution, job, or artifact operations. Do not claim full REST planning parity: discover/classify/select/plan-scope remain CLI-authoritative.

## Validation

Run focused tests for the touched surface, then the full offline suite when appropriate. At minimum verify `git diff --check` and relevant CLI `--help`; use the validation commands listed in the root `README.md` for live or catalog work.
