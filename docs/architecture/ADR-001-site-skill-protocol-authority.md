# ADR-001: Site-skill protocol authority

- Status: Accepted
- Date: 2026-07-16
- Scope: Contract and documentation freeze only

## Context

The repository has stable staged crawl behavior and existing planning/probing
contracts. Future acquisition executors need portable request/result evidence
without silently creating a second runtime authority or redefining legacy
fields.

## Decision

Freeze strict models for site skills, capture requests, capture results, and v2
acquisition attempts under `web_listening.contracts`. Keep execution authority
where it is today. Executor IDs describe protocol identity, not installation or
runtime registration. In particular, `browser_rendered` continues to identify
the existing Playwright compatibility adapter and `browseract` is only a
separate accepted contract value.

## Authority matrix

| Concern | Current authority | New contract role | Explicit non-authority |
|---|---|---|---|
| Bootstrap/run execution | Existing staged workflow and crawler blocks | None in PR-1 | Site-skill models do not dispatch work. |
| Legacy executor selection | `fetch_mode` and current callers | Documents deterministic mapping | Contracts do not reinterpret `auto`. |
| Legacy executor settings | `fetch_config_json` | A future bridge may copy only sanitized/redacted portable non-secret JSON | No raw legacy JSON copying, migration, or mutation here. |
| Playwright compatibility | Existing `browser_rendered` adapter | Preserves its ID | `browseract` is not an alias. |
| BrowserAct runtime | No current authority | Accepted ID only | No import, dependency, install, registration, or invocation. |
| Contract validation | `web_listening.contracts` models | `Model.model_validate(...)` and `Model.model_validate_json(...)` are the governed direct entrypoints for the four frozen schemas | Generic/composite `TypeAdapter` calls are excluded, remain Pydantic defaults, and must not weaken model policy; existing v1 models remain unchanged. |
| Capture acceptance | Producer of `acquisition-attempt.v2` | Records an explicit decision and reason | Does not alter crawler quality gates. |
| Persistence and transport | Existing storage/interfaces | JSON-serializable handoff records | No database, CLI, API, or MCP changes. |

## Consequences

Consumers can validate governed domains, recipes, executor bindings, portable
paths, non-secret JSON, and immutable capture lineage before runtime integration
exists. Unknown fields and coercive input fail closed. Existing behavior remains
independently testable. Nested mappings resist supported/public mutation APIs and ordinary direct
attribute assignment; this is not a security claim against deliberate
`object.__setattr__` reflection. A later implementation must explicitly bind a
validated executor to a runtime and satisfy the measurable retirement conditions in the
contract doc.
