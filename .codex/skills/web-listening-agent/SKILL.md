---
name: web-listening-agent
description: Operate and extend the `web_listening` project as a staged website monitoring platform for humans and AI agents.
---

# Web Listening Agent

## Overview

Use this skill when the task is to inspect, operate, or extend the `web_listening` repository.

Treat the repo as a monitoring platform with three layers:

- control plane:
  YAML planning artifacts
- evidence plane:
  SQLite plus downloaded files
- explanation plane:
  Markdown reports

## Read First

1. `README.md`
2. `references/current-api.md`
3. `references/agent-roadmap.md`
4. `references/interface-strategy.md`

## Current Recommended Workflow

For new site-tree monitoring work, prefer the staged tool flow:

1. `tools/discover_site_sections.py`
2. `tools/classify_site_sections.py`
3. reviewed `section_selection.yaml`
4. `tools/plan_monitor_scope.py`
5. `tools/bootstrap_site_tree.py --scope-path ...`
6. `tools/summarize_scope_bootstrap.py`
7. `tools/export_scope_document_manifest.py`
8. `tools/run_site_tree.py`

Use the packaged CLI and REST API mainly for the older site-level monitoring layer.

## Guardrails

- keep crawling, storage, document handling, and analysis composable
- preserve evidence pointers such as `scope_id`, `run_id`, `page_url`, `download_url`, `sha256`, and timestamps
- keep `_blobs` as canonical storage and `_tracked` as a source-oriented view
- do not replace SHA-256 dedupe with URL-based identity
- prefer generated YAML over hidden prompt-only state when planning scopes

## Validation

Before closing crawling or download changes, use the live validation commands rather than relying on committed snapshot docs:

- `tools/validate_real_sites.py`
- `tools/run_dev_regression.py`
- `tools/run_smoke_site_catalog.py --report-only`
- `tools/run_tree_catalog_validation.py`
- `tools/run_agent_rescue_validation.py`

See `docs/validation/README.md` for the validation map.

## References

- `references/current-api.md`
- `references/agent-roadmap.md`
- `references/interface-strategy.md`
- `docs/operations/DEV_TEST_TARGETS.md`
- `docs/operations/SMOKE_SITE_MANAGEMENT.md`
- `docs/operations/TREE_BUDGET_RULES.md`
- `docs/design/AGENT_SCOPE_PLANNING_DESIGN.md`
- `docs/design/TREE_MONITORING_DESIGN.md`
