# Agent Scope Planning Design

> Last updated: 2026-04-06
> Status: Active design

## Goal

Add a planning layer before deep site monitoring so agents do not bootstrap an entire website blindly.

The new workflow should let a human or AI agent:

1. discover section structure
2. classify second- and third-level sections
3. select the business-relevant sections
4. materialize a bounded monitoring scope
5. only then bootstrap and monitor that selected tree

## Why This Layer Is Needed

The current tree crawler is already good at:

- bounded recursion
- page and file inventory
- SHA-256 file dedupe
- later incremental comparison

What it does not yet do is decide what part of the site deserves monitoring for a given business goal.

That decision should not be buried inside the crawler.
It should be a separate, agent-readable planning step.

## Core Principle

Monitoring scope should be driven by intent, not only by crawl reachability.

Examples:

- if the goal is exam or credential monitoring, keep `education`, `exam`, and related sections
- if the goal is research tracking, keep `research`, `publications`, `reports`
- if the goal is business-core monitoring, skip low-value `membership`, `directory`, `contact`, and event pages unless explicitly requested

## Staged Workflow

### Stage 1: Discover

Run a structure-first crawl to depth `3` so second- and third-level pages are mapped before deeper monitoring begins.
This stage should be depth-bounded rather than page-budget-bounded.
The preferred strategy is:

- cover all reachable level-2 HTML pages first
- sample deeper candidate pages under each level-2 branch
- emit explicit expansion candidates for branches that deserve a later deeper pass

By default it should not detect or download PDFs; it should focus on the HTML tree shape only.

Output should include:

- section paths
- sample URLs
- child section counts
- representative titles
- candidate category hints
- expansion candidate branches

### Stage 2: Classify

Classify each section or source page into a stable business category.

Suggested categories:

- `exam_education`
- `research_publications`
- `governance_management`
- `finance_reports`
- `membership_operations`
- `news_announcements`
- `general_reference`

This classification should be done from:

- URL path
- page title
- shallow content evidence
- section structure evidence

Current project default:

- still recognize `exam_education`
- still recognize `governance_management`
- but treat both as out of scope for default monitoring selection unless the user explicitly asks for those areas

### Stage 3: Select

Human or AI chooses what matters for the current monitoring goal.

This step should support:

- include rules
- exclude rules
- explicit must-keep paths
- explicit must-drop paths

### Stage 4: Materialize Scope

Turn the selection into a real crawl scope:

- `allowed_page_prefixes`
- `allowed_file_prefixes`
- `max_depth`
- `max_pages`
- `max_files`
- `fetch_config_json`

### Stage 5: Bootstrap and Monitor

Only after scope planning should the repo run:

- `bootstrap_site_tree.py`
- `run_site_tree.py`
- explanation and conversion routing

## Agent-Readable Artifacts

Each stage should write a machine-readable file that becomes the next stage's input.

### 1. `monitor_intent.yaml`

Defines what the user wants to monitor.

Suggested fields:

- `business_goal`
- `include_categories`
- `exclude_categories`
- `must_include_paths`
- `must_exclude_paths`
- `conversion_policy`

### 2. `section_inventory.yaml`

Captures the shallow site structure.

Suggested fields:

- `seed_url`
- `discovery_depth`
- `sections`
- `sample_pages`
- `child_section_count`
- `candidate_category`
- `page_limit_mode`
- `discovery_mode`

### 3. `section_selection.yaml`

Records the chosen and rejected sections.

Suggested fields:

- `selected_sections`
- `rejected_sections`
- `selection_reason`
- `review_status`

### 4. `monitor_scope.yaml`

Becomes the direct input for tree bootstrap.

Suggested fields:

- `seed_url`
- `allowed_page_prefixes`
- `allowed_file_prefixes`
- `max_depth`
- `max_pages`
- `max_files`
- `fetch_config_json`

### 5. `bootstrap_manifest.yaml`

Summarizes the resulting baseline.

Suggested fields:

- `scope_id`
- `run_id`
- `pages_seen`
- `files_seen`
- `top_source_pages`
- `next_step`

## OpenClaw And Similar Agents

This staged file-based design fits OpenClaw-style agent workflows well.

Why:

- each step leaves a durable local artifact
- the next agent step can read that artifact without depending on chat memory
- YAML is short, explicit, and easy for agents to edit or validate
- Markdown reports can still sit beside YAML for human review

In short:

- YAML should be the control plane
- SQLite should remain the evidence plane
- Markdown should remain the explanation plane

## Relationship To Existing Tree Design

This document does not replace `TREE_MONITORING_DESIGN.md`.

Instead:

- `AGENT_SCOPE_PLANNING_DESIGN.md` defines how to decide what to monitor
- `TREE_MONITORING_DESIGN.md` defines how to crawl and persist a chosen scope safely

## Near-Term Implementation Order

1. add section discovery output
2. add section classification output
3. add scope-selection output
4. compile selected sections into `monitor_scope.yaml`
5. point bootstrap tooling at those generated scopes
6. keep later change explanation and conversion routing downstream from the selected scope
