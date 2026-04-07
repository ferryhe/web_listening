---
name: web-listening-agent
description: Operate and extend the `web_listening` project for website monitoring, document discovery, AI summaries, and agent-facing integrations. Use when Codex needs to add or inspect monitored sites, trigger checks or document downloads, review changes or analyses, or implement the next stage of this repo such as browser crawling, markdown normalization, structured extraction, async jobs, webhooks, or an MCP server.
---

# Web Listening Agent

## Overview

Use this skill to treat `web_listening` as an agent-consumable web monitoring platform rather than a one-off crawler. Prefer machine-readable artifacts, evidence pointers, and reusable workflows over ad-hoc summaries.

## Operating Workflow

1. Read `README.md` for the current CLI and REST entry points.
2. Read `references/current-api.md` to understand the implemented blocks, endpoints, and current limits.
3. Read `references/agent-roadmap.md` before planning or implementing larger feature work.
4. Read `references/interface-strategy.md` before changing how agents or traditional programs call the system.
5. Prefer the REST API for agent-facing usage; fall back to the CLI when the API server is not running.
6. Keep the building-block split intact: crawling, diffing, storage, document download, analysis, and scheduling should remain composable.
7. Before closing crawler or download changes, run the required live dev targets through `tools/validate_real_sites.py` and `tools/run_dev_regression.py`.
8. When a task starts from a spreadsheet or site list, treat the raw input as local-only and convert it into a curated tracked catalog before wiring it into smoke or regression scripts.

## Current Usage Pattern

- Register or inspect sites with `POST /api/v1/sites`, `GET /api/v1/sites`, or the matching CLI commands.
- Set `fetch_mode` per site when a target may need browser rendering later.
- Trigger monitoring with `POST /api/v1/sites/{id}/check`.
- Read normalized page artifacts with `GET /api/v1/sites/{id}/snapshots/latest`.
- Trigger document downloads with `POST /api/v1/sites/{id}/download-docs`.
- Let downstream agents write converted Markdown back with `PATCH /api/v1/documents/{id}/content`.
- Generate analysis with `POST /api/v1/analyze`, then consume stored reports from `GET /api/v1/analyses`.
- Pull machine-consumable outputs from `GET /api/v1/changes`, `GET /api/v1/documents`, and `GET /api/v1/analyses`.

## Tree Workflow

- Use `tools/bootstrap_site_tree.py --catalog dev` to establish the first recursive baseline for the 3 development sites.
- Use `tools/bootstrap_site_tree.py --catalog smoke` to establish the first recursive baseline for the broader 30+ smoke catalog.
- Use `tools/run_site_tree.py --catalog dev` or `--catalog smoke` for later incremental runs against the stored tree baseline.
- Keep tree monitoring bounded: prefer explicit `max_depth`, `max_pages`, and `max_files` instead of open-ended recursion.

## Extension Priorities

- Add LLM-ready markdown normalization before expanding AI summarization.
- Add browser or Playwright capture before trying to support more JS-heavy sites.
- Add structured extraction and field-level diff before adding more delivery channels.
- Add persistent jobs and webhooks before scaling out scheduling.
- Add an MCP server only after the REST and storage contracts are stable.
- Keep the required live dev targets (`SOA`, `CAS`, `IAA`) in the regression loop while evolving the crawler and download logic.

## Guardrails

- Preserve evidence: keep original URL, snapshot IDs, document hashes, and timestamps visible in any new workflow.
- Make long-running actions job-based and idempotent.
- Prefer markdown or fit-markdown as the default agent input once available.
- Keep provider-specific services optional; local HTTP or browser execution should still work.
- Do not start with UI work unless the user explicitly asks for it; prioritize the content, job, and protocol layers first.

## References

- Read `references/current-api.md` for current capabilities and gaps.
- Read `references/agent-roadmap.md` for the target architecture and implementation order.
- Read `references/interface-strategy.md` for the protocol and compatibility decisions.
- Read `docs/operations/DEV_TEST_TARGETS.md` for the required live targets, regression matrix, and SHA-256 policy.
- Read `docs/operations/SMOKE_SITE_MANAGEMENT.md` for list-driven smoke monitoring and catalog management.
- Read `docs/validation/SMOKE_SITE_VALIDATION.md` for the current supranational smoke baseline before changing the catalog.
- Read `docs/design/TREE_MONITORING_DESIGN.md` before implementing recursive tree or section monitoring.
