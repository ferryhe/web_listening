# Interface Strategy

## Decision

Keep the generic REST API and treat it as the canonical backend contract.

Build the agent-facing surface on top of it:

```text
blocks -> REST -> MCP/CLI/webhooks
```

## Why keep the REST API

- Traditional programs still need a standard interface.
- REST is easier to test and debug than agent-only protocols.
- MCP should adapt stable backend semantics instead of inventing new ones.
- Webhooks and background jobs fit naturally with a REST backend.

## What should change

- Long-running writes should return job envelopes.
- Responses should include durable IDs and evidence pointers.
- Machine-readable payloads should be first-class.
- CLI and MCP should reuse the same backend behavior.

## Guardrails

- Do not create agent-only business logic paths.
- Do not deprecate the REST API when MCP arrives.
- Do not let CLI, REST, and MCP drift into different orchestration models.
