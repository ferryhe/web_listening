# Interface Strategy

## Decision

Keep three layers clear:

- packaged `web-listening` CLI as the canonical staged agent workflow entrypoint
- packaged REST for the older site-level monitoring contract
- `tools/*.py` as lower-level compatibility and developer-oriented wrappers around package blocks

## Current Reality

Today the staged tree workflow is real and exposed through first-class packaged CLI commands:

```text
web-listening discover -> classify -> select -> plan-scope -> bootstrap-scope -> run-scope -> report-scope -> export-manifest
```

The `tools/*.py` scripts still exist for compatibility and direct developer access to lower-level blocks, but new agent/operator handoffs should start from the packaged CLI.

## Near-Term Rule

When extending the staged tree workflow:

- make the packaged CLI the primary user-visible surface
- keep stable local artifact contracts first
- do not rush REST or MCP wrappers before the YAML, JSON, and storage outputs settle
- do not create separate business logic paths for CLI, tools, REST, or future MCP wrappers

## Long-Term Direction

Later, the staged workflow can also be exposed through REST or MCP.

When that happens:

- keep evidence pointers intact
- wrap the same underlying planning and crawl blocks
- preserve the packaged CLI contract as the local automation baseline
