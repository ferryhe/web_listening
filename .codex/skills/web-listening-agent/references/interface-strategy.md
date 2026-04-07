# Interface Strategy

## Decision

Keep two layers clear:

- packaged REST and CLI for the older site-level monitoring contract
- tool-driven YAML workflows for the newer staged tree-monitoring contract

## Current Reality

Today the staged tree workflow is real, but it is implemented through `tools/*.py`.

That is acceptable for now because:

- the artifact contracts are stabilizing
- the planning layer is file-based
- the crawler and storage layers are already reusable

## Near-Term Rule

When extending the staged tree workflow:

- prefer stable local artifact contracts first
- do not rush REST or MCP wrappers before the YAML and storage outputs settle

## Long-Term Direction

Later, the staged workflow can be exposed through REST, packaged CLI, or MCP.

When that happens:

- keep evidence pointers intact
- do not invent separate business logic paths for each interface
- wrap the same underlying planning and crawl blocks
