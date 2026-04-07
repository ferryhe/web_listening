# Agent Site Monitoring Master Plan

> Last updated: 2026-04-06
> Status: Active roadmap

## Goal

Turn `web_listening` into a staged website monitoring platform for human operators and AI agents.

The target operating model is:

```text
discover -> classify -> select -> bootstrap -> run -> explain -> convert
```

The project should not jump straight from homepage crawling to whole-site monitoring.
It should first decide what part of the site matters for the user's monitoring goal.

## Decisions In Force

These decisions are now the active baseline:

- keep SHA-256 as the final file dedupe authority
- keep bounded tree crawling with explicit `max_depth`, `max_pages`, and `max_files`
- treat page scope and file scope as separate boundaries
- keep tree evidence in SQLite and downloaded files in the blob store
- use agent-readable YAML or config artifacts between planning stages
- treat Markdown reports as explanation output, not the only machine interface

## Current State

Already implemented:

- bounded tree bootstrap and incremental runs
- page and file inventory persistence
- source-page classification for baseline explanation
- OpenAI-optional explanation overlays
- polite request pacing
- shared dev and smoke target catalogs
- OpenClaw-loadable workspace skill
- staged section discovery output
- staged section classification output

Still missing from the intended system:

- section selection driven by monitoring intent
- generated scope YAML files
- targeted bootstrap from selected sections instead of one broad homepage seed
- change explanation that flows directly from selected business sections

## Active Design Documents

Read these together:

- `docs/design/AGENT_SCOPE_PLANNING_DESIGN.md`
- `docs/design/TREE_MONITORING_DESIGN.md`
- `docs/operations/TREE_BUDGET_RULES.md`

Their roles are:

- scope planning design: how to choose what to monitor
- tree monitoring design: how to crawl it safely
- budget rules: how much to crawl for different site shapes

## Delivery Streams

### Stream 1: Section Discovery

Goal:

- map second- and third-level site structure before full monitoring

Planned outputs:

- `section_inventory.yaml`
- compact Markdown section summary

Planned tooling:

- `tools/discover_site_sections.py`

Success criteria:

- a new site can cover all reachable level-2 HTML pages into a readable section map
- discovery samples deeper candidates under each level-2 branch instead of blindly exhausting the whole site tree
- discovery is depth-bounded and structure-first, without PDF detection by default

### Stream 2: Section Classification

Goal:

- classify discovered sections by business meaning

Planned outputs:

- section-level categories
- section-level evidence
- AI-assisted or heuristic classification reasons

Planned tooling:

- `tools/classify_site_sections.py`

Success criteria:

- major sections fall into stable categories such as `exam_education`, `research_publications`, or `governance_management`
- classification can run without OpenAI, with optional AI refinement

### Stream 3: Intent And Scope Selection

Goal:

- let a human or agent choose what matters for the monitoring task

Planned outputs:

- `monitor_intent.yaml`
- `section_selection.yaml`
- `monitor_scope.yaml`

Planned tooling:

- `tools/plan_monitor_scope.py`

Success criteria:

- the chosen scope is explicit and reproducible
- unwanted sections can be excluded before deep crawling starts

### Stream 4: Targeted Bootstrap

Goal:

- bootstrap only the selected business-relevant tree

Planned outputs:

- `bootstrap_manifest.yaml`
- dated bootstrap report

Planned tooling:

- extend `bootstrap_site_tree.py` to accept generated scope files

Success criteria:

- bootstrap can be driven from generated scope config rather than only built-in catalog defaults
- the resulting baseline is narrower and more business-focused than a raw homepage crawl

### Stream 5: Incremental Monitoring And Explanation

Goal:

- monitor the selected scope repeatedly and explain meaningful changes

Planned outputs:

- evidence-rich incremental reports
- machine-readable change bundles
- prioritized candidate files for `doc_to_md`

Planned tooling:

- extend `run_site_tree.py`
- add deterministic change-bundle output

Success criteria:

- a two-day rerun can answer what changed in under one minute
- new files and changed files carry source-page evidence and local blob references

### Stream 6: Conversion Routing

Goal:

- send only the right files into `doc_to_md`

Planned outputs:

- file-level conversion priorities
- handoff manifests for downstream conversion

Selection rule:

- `high`: convert automatically when new or changed
- `medium`: convert selectively
- `low` or `skip`: keep as evidence only

### Stream 7: 30+ Site Rollout

Goal:

- apply the staged approach to the smoke catalog

Rollout order:

1. start with stable `homepage_standard` sites
2. upgrade strong document or research sites into section-based scopes
3. keep `thin_html_watch` and `blocked_hold` sites out of deep bootstrap until validated

Success criteria:

- the 30+ catalog is no longer managed only by raw crawl budgets
- each site has an explicit monitoring strategy and section scope

## Phases

### Phase 0: Docs Consolidation

Status: current

Deliverables:

- archive outdated roadmap notes
- keep one active master plan
- separate planning design from crawling design

### Phase 1: Discovery Layer

Deliverables:

- shallow section crawler
- `section_inventory.yaml`
- operator-facing discovery report

### Phase 2: Classification Layer

Deliverables:

- section and source-page classification
- classification reasons
- category summary per site

### Phase 3: Scope Planning Layer

Deliverables:

- `monitor_intent.yaml`
- `section_selection.yaml`
- `monitor_scope.yaml`
- selected-prefix compilation

### Phase 4: Targeted Monitoring Layer

Deliverables:

- bootstrap from selected scope config
- incremental reruns from the same selected scope
- stored manifests linking scope config to run IDs

### Phase 5: Explanation And Conversion Layer

Deliverables:

- deterministic change explanation reports
- optional AI narrative overlay
- conversion routing for `doc_to_md`

### Phase 6: Agent Interface Layer

Deliverables:

- stable YAML handoff contracts
- skill guidance for Codex and OpenClaw
- later REST or MCP exposure for staged planning outputs

## What Should Be Archived, Not Continued

The following older notes were useful milestones but are no longer the main plan:

- early branch-wide AI future plan
- PR recommendation note
- point-in-time implementation status
- earlier tree-only delivery plan
- earlier focused change-explanation plan

They remain under `docs/archive/2026-04-roadmap-history/`.

## Near-Term Next Steps

1. implement section discovery for depth `2` or `3`
2. define YAML schemas for intent, inventory, selection, scope, and manifest
3. compile selected sections into `allowed_page_prefixes` and `allowed_file_prefixes`
4. rerun the 3 dev sites through the staged flow
5. use those results to set per-site strategy for the stable 30+ smoke sites

## Success Criteria

The roadmap is successful when:

- a new site is not monitored blindly from the homepage
- an agent can read one YAML artifact and know what to do next
- monitoring scope reflects business goals rather than crawl accidents
- later file conversion is prioritized from source-page meaning, not file count alone
- the same staged workflow works for both humans and OpenClaw-style agents
