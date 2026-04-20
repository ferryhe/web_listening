# Staged Workflow Authority Map

Generated for PR1 (`feat/pr1-staged-workflow-authority`).

## Conclusion

The packaged `web-listening` CLI and FastAPI routes are the **primary staged workflow surfaces**.
The package module `web_listening.blocks.staged_workflow` is the **authority layer** for discover / classify / select / plan / bootstrap / run / report / manifest orchestration.
Legacy `tools/*.py` scripts remain available, but they are compatibility wrappers and should not be treated as the primary execution layer.

---

## CLI / API / Package Authority Map

| Capability | CLI command | API route | Current package authority | Legacy `tools/*.py` dependency status |
|---|---|---|---|---|
| Discover sections | `web-listening discover` | none | `web_listening.blocks.staged_workflow.discover_sections` | `tools/discover_site_sections.py` now delegates to package authority |
| Classify sections | `web-listening classify` | none | `web_listening.blocks.staged_workflow.classify_sections` | `tools/classify_site_sections.py` now delegates to package authority |
| Inspect selection | `web-listening select` | none | `web_listening.blocks.staged_workflow.inspect_selection` | no legacy dependency in authority path |
| Plan scope | `web-listening plan-scope` | none | `web_listening.blocks.staged_workflow.plan_scope` | `tools/plan_monitor_scope.py` now delegates to package authority |
| Bootstrap scope | `web-listening bootstrap-scope` | `POST /api/v1/monitor-scopes/{scope_id}/bootstrap` | `web_listening.blocks.staged_workflow.bootstrap_scope` + `web_listening.blocks.tree_bootstrap_workflow` | `tools/bootstrap_site_tree.py` now delegates to package workflow module |
| Run scope | `web-listening run-scope` | `POST /api/v1/monitor-scopes/{scope_id}/run` | `web_listening.blocks.staged_workflow.run_scope` + `web_listening.blocks.tree_run_workflow` | `tools/run_site_tree.py` now delegates to package workflow module |
| Report scope | `web-listening report-scope` | `POST /api/v1/monitor-scopes/{scope_id}/report` and `GET /api/v1/monitor-scopes/{scope_id}/reports/latest` | `web_listening.blocks.staged_workflow.report_scope` | no legacy dependency in authority path |
| Export manifest | `web-listening export-manifest` | `GET /api/v1/monitor-scopes/{scope_id}/manifest/latest` | `web_listening.blocks.staged_workflow.export_manifest` | `tools/export_scope_document_manifest.py` now delegates to package authority |
| Create monitor task | `web-listening create-monitor-task` | `POST /api/v1/monitor-tasks` | CLI/API currently call package task builders directly (`web_listening.blocks.monitor_task`) | no legacy `tools/*.py` authority involvement |

---

## Practical Interpretation

### Primary surfaces

- packaged CLI commands in `web_listening/cli.py`
- staged API routes in `web_listening/api/routes.py`
- package workflow modules under `web_listening/blocks/`

### Legacy compatibility surfaces

- `tools/discover_site_sections.py`
- `tools/classify_site_sections.py`
- `tools/plan_monitor_scope.py`
- `tools/bootstrap_site_tree.py`
- `tools/run_site_tree.py`
- `tools/export_scope_document_manifest.py`

These scripts still work, but they should be understood as wrappers around the same package-owned workflow logic.

---

## Verification used in this PR stage

- `python -m pytest tests/test_staged_workflow_authority.py -q`
- `python -m pytest tests/test_cli.py tests/test_api.py -q`
- `python -m pytest tests -q`
