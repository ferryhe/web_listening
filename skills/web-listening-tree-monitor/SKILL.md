---
name: web_listening_tree_monitor
description: Discover, classify, select, plan, bootstrap, rerun, report, and export bounded governed site-tree monitoring in this repository.
---

# Web Listening Tree Monitor

Read the root `README.md` as the sole active human-facing product source. Read `AGENTS.md` for repository workflow rules. Never use documents under `docs/archive/` as current authority.

## Initialization

For a new catalog or imported site list:

1. run broad smoke/tree validation
2. classify reachable, blocked, thin-HTML, and section-seed-sensitive sites
3. generate draft selections and scopes
4. obtain human confirmation of the monitoring boundary
5. bind the confirmed scope to the governed acquisition profile and Site Skill
6. preview the compiled execution plan
7. only then bootstrap and run later checks

Draft scope artifacts are review artifacts, not automatic production approval.

## Canonical workflow

```text
discover -> classify -> select -> plan-scope -> bootstrap/run -> report/export
```

Use the packaged `web-listening` CLI and the exact commands/options documented by `web-listening --help` and the root `README.md`. Lower-level `tools/*.py` programs are compatibility/developer wrappers only.

## Data and safety rules

- `_blobs` is canonical SHA-256-deduplicated storage; `_tracked` is the source-oriented view.
- Preserve `scope_id`, `run_id`, source/final URLs, timestamps, hashes, and governed executor/Site Skill lineage.
- Keep recursion bounded by effective `max_depth`, `max_pages`, and `max_files`.
- A bootstrap creates a baseline; a run performs change detection.
- Formal execution requires the complete profile + Site Skill authority compiled and validated before Storage opens.
- Picker/probe output does not authorize execution; domain and runtime safety gates remain mandatory.

## Validation

Use the root `README.md` validation section. Run focused tests first, full offline tests as appropriate, `git diff --check`, and CLI `--help`; run live/network checks only in an authorized environment.
