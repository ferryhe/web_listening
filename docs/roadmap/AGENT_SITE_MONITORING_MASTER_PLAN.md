# Agent Site Monitoring Master Plan

> Last updated: 2026-04-07
> Status: Active roadmap

## Goal

Turn `web_listening` into a practical website listening layer for people and AI agents.

The active operating model is:

```text
discover -> classify -> select -> bootstrap -> run -> explain -> convert
```

## Decisions In Force

These decisions are now the project baseline:

- keep `SHA-256` as the final file dedupe authority
- keep `_blobs` as canonical storage and `_tracked` as a source-oriented view
- keep page scope and file scope separate
- keep bounded tree crawling with explicit `max_depth`, `max_pages`, and `max_files`
- keep YAML artifacts as the planning and handoff layer
- keep Markdown reports as explanation output, not the only machine interface

## Current State

Implemented:

- section discovery
- section classification
- section selection artifacts
- `monitor_scope.yaml` compilation
- scope-driven tree bootstrap
- scope-driven reruns
- bootstrap summary export
- document manifest export with:
  - `sha256`
  - `local_path`
  - `tracked_local_path`
  - `preferred_display_path`
  - `page_url`
  - `download_url`
  - `downloaded_at`

Partially implemented:

- bootstrap explanation overlays
- source-category and business-importance hints

Still missing:

- first-class `monitor_intent.yaml`
- stronger incremental change bundles from selected business sections
- conversion-routing output for downstream `doc_to_md`
- REST and packaged CLI exposure for the staged tree workflow
- a stable rollout of section-aware scopes across the 30+ smoke catalog

## Active Streams

### Stream 1: Planning Layer

Status: active and usable

Artifacts:

- `section_inventory.yaml`
- `section_classification.yaml`
- `section_selection.yaml`
- `monitor_scope.yaml`

Next upgrades:

- intent-first planning
- better automatic branch expansion

### Stream 2: Tree Evidence Layer

Status: active and usable

Artifacts:

- page snapshots
- page edges
- tracked files
- file observations
- canonical blobs
- tracked file views

Next upgrades:

- richer rerun summaries
- better missing-page and missing-file handling over time

### Stream 3: Agent Output Layer

Status: active but incomplete

Artifacts:

- bootstrap scope summary
- document manifest
- optional explanation report

Next upgrades:

- deterministic change bundles for reruns
- conversion candidate manifests
- simpler agent-default outputs

### Stream 4: 30+ Site Rollout

Status: planned

Rollout order:

1. stable `homepage_standard` sites
2. sites that should use section-driven scopes
3. thin or blocked sites after validation

The long-term goal is not just per-site crawl budgets, but per-site scope decisions.

## Near-Term Priorities

1. finish doc and skill consolidation around the staged workflow
2. make scope-driven summaries and manifests the default post-bootstrap outputs
3. extend rerun reporting so changes are grouped by selected business branches
4. define how the 30+ catalog should move from smoke validation into section-aware scopes
5. add conversion-routing artifacts so only the right files go into `doc_to_md`

## Success Criteria

This roadmap is successful when:

- a new site is not monitored blindly from the homepage
- an agent can read one YAML artifact and know the next step
- a downloaded file can always be traced back to source page, SHA-256, and tracked path
- later reruns explain what changed in business-relevant sections rather than only counting URLs
- the same workflow works for Codex, OpenClaw-style agents, and human operators
