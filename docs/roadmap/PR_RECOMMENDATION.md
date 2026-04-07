# PR Recommendation

> Branch: `docs/ai-agent-roadmap`  
> Recommendation date: 2026-04-03

## Recommendation

Open a draft PR now.

This branch is already large enough, and it now forms a coherent milestone:

- normalized snapshots for agent consumption
- document Markdown handoff for downstream `doc_to_md` or AI agents
- optional browser acquisition
- curated smoke catalog and list-driven validation
- recursive tree bootstrap scaffold
- shared agent rescue ladder in smoke scripts and REST API

That is a strong first "agent-ready monitoring foundation" PR.

## Why now

- The branch has moved beyond isolated experiments and into stable repo contracts.
- The rescue ladder is no longer script-only; smoke, validation, and REST now share the same core logic.
- We have a clear validation story across local tests, required live dev targets, smoke catalog, tree validation, and rescue validation.
- The remaining work is better handled as follow-up PRs than by continuing to grow this branch.

## Suggested PR scope

Suggested title:

- `Build agent-ready monitoring foundation`

Suggested PR summary:

- add normalized snapshot artifacts and latest-snapshot API
- keep document conversion external, but add Markdown handoff fields and API
- add browser-ready crawling and stronger normalization
- add curated smoke catalog, live regression targets, and SHA-256 policy
- add recursive tree bootstrap scaffold
- add shared rescue ladder plus `POST /api/v1/sites/{id}/rescue-check`

## Keep out of this PR

These should stay as follow-up PRs:

- persistent jobs and async envelopes
- webhooks
- structured watch rules and field-level diff
- MCP adapter layer
- broader browser hardening for difficult JS-heavy sites

## Pre-PR checklist

- run `.venv\Scripts\python -m pytest tests -q`
- run `.venv\Scripts\python tools\validate_real_sites.py`
- run `.venv\Scripts\python tools\run_dev_regression.py`
- run `.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only`
- run `.venv\Scripts\python tools\run_tree_catalog_validation.py`
- run `.venv\Scripts\python tools\run_agent_rescue_validation.py`

## Known follow-up gaps to call out in the PR

- `ISSA` still fails the public rescue ladder from this environment because both HTML and official feeds return `403`
- `SIF` still appears broken upstream
- recursive tree monitoring is scaffolded, but not yet exposed through REST or CLI
- long-running actions still use in-process background execution instead of durable jobs
