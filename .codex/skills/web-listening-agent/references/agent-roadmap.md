# Agent Roadmap

## Target architecture

Build the project in this order:

1. Acquisition: HTTP plus optional browser capture
2. Normalization: raw HTML, cleaned HTML, markdown, fit-markdown
3. Change intelligence: text diff, selector diff, schema diff, semantic summary
4. Orchestration: persistent jobs, retries, schedules, webhooks
5. Agent interface: REST, MCP tools, MCP resources, repo skill

## Preferred implementation order

1. Extend snapshots with markdown-oriented artifacts.
2. Split crawling into HTTP and browser drivers.
3. Add `watch_rules` and structured extraction results.
4. Replace ad-hoc background responses with persistent jobs.
5. Expose stable MCP tools on top of the existing blocks.

## Agent-facing contract rules

- Every write action should return `job_id`, `status`, and `accepted_at`.
- Every change should expose machine-friendly payload fields, not only human summaries.
- Every result should keep evidence pointers such as `snapshot_id`, `document_id`, `url`, and timestamps.
- Agent-default content should be markdown or fit-markdown once available.
- MCP should wrap stable backend contracts rather than inventing parallel behavior.

## Non-goals for the next iteration

- Building a Web UI first
- Distributed crawling first
- Replacing SQLite before the job model is proven
- Deep multi-tenant access control
