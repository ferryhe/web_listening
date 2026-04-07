# Current API And Workflow Status

## Core Blocks

- `web_listening/blocks/crawler.py`
  site-level fetch and normalization entry
- `web_listening/blocks/normalizer.py`
  cleaned HTML, Markdown, fit-Markdown, metadata
- `web_listening/blocks/diff.py`
  page hash comparison, new links, document links
- `web_listening/blocks/document.py`
  file download, SHA-256 blob storage, tracked-view materialization
- `web_listening/blocks/storage.py`
  SQLite persistence for sites, tree scopes, runs, documents, blobs, and observations
- `web_listening/blocks/tree_crawler.py`
  bounded tree bootstrap and reruns
- `web_listening/blocks/section_discovery.py`
  structure-first section discovery
- `web_listening/blocks/section_classifier.py`
  section classification
- `web_listening/blocks/monitor_scope_planner.py`
  selection-to-scope compilation
- `web_listening/blocks/bootstrap_summary.py`
  bootstrap scope summary
- `web_listening/blocks/document_manifest.py`
  agent-readable document manifest export
- `web_listening/blocks/analyzer.py`
  optional OpenAI-backed summary layer

## REST Endpoints

Implemented REST endpoints still focus on the site-level monitoring layer:

- `GET /api/v1/sites`
- `POST /api/v1/sites`
- `GET /api/v1/sites/{id}`
- `GET /api/v1/sites/{id}/snapshots/latest`
- `POST /api/v1/sites/{id}/rescue-check`
- `DELETE /api/v1/sites/{id}`
- `POST /api/v1/sites/{id}/check`
- `POST /api/v1/sites/{id}/download-docs`
- `GET /api/v1/changes`
- `GET /api/v1/documents`
- `PATCH /api/v1/documents/{id}/content`
- `POST /api/v1/analyze`
- `GET /api/v1/analyses`

## Packaged CLI

The packaged `web-listening` CLI still focuses on the older site-level flow:

- `add-site`
- `list-sites`
- `check`
- `list-changes`
- `download-docs`
- `list-docs`
- `analyze`
- `serve`

## Tool-Driven Tree Workflow

The staged tree workflow is implemented through dedicated tools:

- `tools/discover_site_sections.py`
- `tools/classify_site_sections.py`
- `tools/plan_monitor_scope.py`
- `tools/bootstrap_site_tree.py`
- `tools/summarize_scope_bootstrap.py`
- `tools/export_scope_document_manifest.py`
- `tools/explain_tree_bootstrap.py`
- `tools/run_site_tree.py`

This means:

- tree monitoring is implemented and usable
- but it is not yet exposed as first-class REST or packaged CLI commands

## Current Agent-Readable Outputs

- planning YAML:
  - `section_inventory.yaml`
  - `section_classification.yaml`
  - `section_selection.yaml`
  - `monitor_scope.yaml`
- explanation outputs:
  - bootstrap summary Markdown
  - document manifest YAML and Markdown
- evidence outputs:
  - `data/web_listening.db`
  - `data/downloads/_blobs`
  - `data/downloads/_tracked`

## Current Limitations

- no first-class `monitor_intent.yaml`
- no REST or packaged CLI entry point for staged scope planning
- no stable rerun change-bundle export yet
- no persistent job model for long tree runs
- no MCP server yet

## Validation

Use live validation commands rather than committed snapshot docs:

- `tools/validate_real_sites.py`
- `tools/run_dev_regression.py`
- `tools/run_smoke_site_catalog.py --report-only`
- `tools/run_tree_catalog_validation.py`
- `tools/run_agent_rescue_validation.py`

See `docs/validation/README.md`.
