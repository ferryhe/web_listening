# Agent Scope Planning Design

> Last updated: 2026-04-07
> Status: Active design and partially implemented workflow

## Goal

Add a planning layer before deep tree monitoring so the crawler does not decide business scope by accident.

The intended workflow is:

```text
discover -> classify -> select -> monitor_scope -> bootstrap -> run
```

## Core Principle

Monitoring scope should be driven by intent, not just by crawl reachability.

Examples:

- if the task is research tracking, keep `research`, `publications`, and `reports`
- if the task is business-core monitoring, drop `membership`, `directory`, and `contact`
- if the task does not care about exams or governance, still classify those sections, but do not prioritize them

## What Is Implemented

The repo now has these planning stages:

1. `tools/discover_site_sections.py`
2. `tools/classify_site_sections.py`
3. manual or agent-reviewed `section_selection.yaml`
4. `tools/plan_monitor_scope.py`
5. `tools/bootstrap_site_tree.py --scope-path ...`

The resulting outputs are durable local artifacts, not chat-only decisions.

## Current Discovery Strategy

Discovery is structure-first.

The current defaults aim to build a useful picture before deep crawling:

- cover reachable level-2 HTML pages
- sample deeper branches at level 3
- do not detect or download PDFs by default
- emit section and branch candidates for later selection

This stage is for site structure, not file inventory.

## Current Classification Model

The classifier uses:

- URL path
- sampled titles
- source page evidence
- section structure evidence

Current categories include:

- `research_publications`
- `news_announcements`
- `finance_reports`
- `membership_operations`
- `exam_education`
- `governance_management`
- `general_reference`

Current default project posture:

- recognize `exam_education`
- recognize `governance_management`
- but do not treat them as default priority areas unless the monitoring goal says otherwise

## Selection Model

Selection should be explicit.

The current workflow expects a reviewed `section_selection.yaml` with:

- `selected_sections`
- `rejected_sections`
- `deferred_sections`
- `excluded_categories`
- `selection_notes`

This makes the final monitoring scope explainable and reproducible.

## Scope Materialization

`tools/plan_monitor_scope.py` compiles selection output into a real monitoring scope:

- `allowed_page_prefixes`
- `allowed_file_prefixes`
- `selected_focus_prefixes`
- `excluded_page_prefixes`
- `max_depth`
- `max_pages`
- `max_files`
- `fetch_config_json`

That `monitor_scope.yaml` becomes the direct input for bootstrap.

## Agent-Readable Artifacts

The active control-plane artifacts are:

- `section_inventory_<site>_<date>.yaml`
- `section_classification_<site>_<date>.yaml`
- `section_selection_<site>_<date>.yaml`
- `monitor_scope_<site>_<date>.yaml`

The current explanation-plane artifacts are:

- bootstrap summary Markdown
- scope document manifest YAML and Markdown

The evidence plane stays in:

- `data/web_listening.db`
- `data/downloads/_blobs`
- `data/downloads/_tracked`

## Why This Fits OpenClaw-Style Agents

This design works well for OpenClaw and similar agent loops because each stage leaves a durable local artifact.

In practice:

- YAML is the control plane
- SQLite and downloads are the evidence plane
- Markdown is the explanation plane

That keeps the next step grounded in files, not only in conversation memory.

## What Is Still Missing

The planning stack is usable, but not complete.

Still missing or intentionally deferred:

- a first-class `monitor_intent.yaml`
- fully automatic selection from intent without human review
- direct REST exposure for the planning stages
- stronger branch-expansion heuristics for large sites

## Relationship To Tree Monitoring

This document defines how to decide what to monitor.

The crawler-side rules for actually bootstrapping and rerunning a selected scope live in:

- [TREE_MONITORING_DESIGN.md](C:/Project/web_listening/docs/design/TREE_MONITORING_DESIGN.md)
