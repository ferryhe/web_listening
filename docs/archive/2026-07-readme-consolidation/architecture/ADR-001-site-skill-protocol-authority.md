# ADR-001: Site-skill protocol authority

- Status: Accepted
- Date: 2026-07-16
- Scope: Protocol authority and formal staged-runtime integration

## Context

The repository has stable staged crawl behavior and existing planning/probing
contracts. Future acquisition executors need portable request/result evidence
without silently creating a second runtime authority or redefining legacy
fields.

## Decision

Freeze strict models for site skills, capture requests, capture results, and v2
acquisition attempts under `web_listening.contracts`. The original PR-1 decision
kept execution authority in the existing staged workflow; formal bootstrap/run
now compiles that authority from the complete governed binding and acquisition
profile. Executor IDs describe protocol identity, not installation. In
particular, `browser_rendered` continues to identify the existing Playwright
compatibility adapter and `browseract` is a distinct optional protocol identity.

## Authority matrix

| Concern | Current authority | New contract role | Explicit non-authority |
|---|---|---|---|
| Bootstrap/run execution | Existing staged workflow and crawler blocks, supplied with a compiled governed plan and gateway | PR-1 models did not dispatch work; current formal integration uses their governed authority | Site-skill models alone do not prove runtime availability. |
| Formal staged executor selection | Complete governed binding plus acquisition profile | Compiles an exact non-empty governed plan before Storage | Legacy fields remain lineage/rollback data only. |
| Legacy executor settings | `fetch_config_json` | A future bridge may copy only sanitized/redacted portable non-secret JSON | No raw legacy JSON copying, migration, or mutation here. |
| Playwright compatibility | Existing `browser_rendered` adapter | Preserves its ID | `browseract` is not an alias. |
| BrowserAct runtime | Compiled governed Site Skill step plus safety and runtime checks | Distinct optional executor identity | Not bundled, always available, or aliased to Playwright. |
| Contract validation | `web_listening.contracts` models | `Model.model_validate(...)` and `Model.model_validate_json(...)` are the governed direct entrypoints for the four frozen schemas | Generic/composite `TypeAdapter` calls are excluded, remain Pydantic defaults, and must not weaken model policy; existing v1 models remain unchanged. |
| Capture acceptance | Producer of `acquisition-attempt.v2` | Records an explicit decision and reason | Does not alter crawler quality gates. |
| Persistence and transport | Existing storage/interfaces | JSON-serializable handoff records | No database, CLI, API, or MCP changes. |

## Consequences

Consumers can validate governed domains, recipes, executor bindings, portable
paths, non-secret JSON, and immutable capture lineage. Formal execution validates
optional runtime availability and may construct and dispatch BrowserAct only
when the compiled governed Site Skill step plus safety and runtime requirements
authorize it. Unknown fields and coercive input fail closed. Existing behavior remains
independently testable. Nested mappings resist supported/public mutation APIs and ordinary direct
attribute assignment; this is not a security claim against deliberate
`object.__setattr__` reflection. Physical legacy-field removal remains deferred
until the measurable retirement conditions in the contract document are met.
