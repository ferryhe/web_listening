# Change Explanation Report Plan

## Goal

Turn tree monitoring output from a counts-only operator report into a human-readable and agent-usable change explanation layer.

The report should answer the questions people actually care about:

- what changed
- where it changed
- whether the change is new content, a missing page, or a new file
- whether a new file was downloaded
- what evidence supports the conclusion

## Why this is the next step

`bootstrap_site_tree.py` and `run_site_tree.py` now solve the bounded tree monitoring problem:

- scoped recursive crawl from one seed URL
- page and file inventory persistence
- later comparison against a stored baseline
- SHA-256 based file tracking and dedupe

What is still missing is the final presentation layer.
Today the tree run report is operationally useful, but it mostly gives totals:

- pages seen
- files seen
- new pages
- changed pages
- missing pages
- new files
- changed files
- missing files

That is enough for engineering validation, but not yet enough for a person or downstream agent to quickly understand the meaning of the run.

## Design Principle

Keep the monitoring pipeline split into two layers:

1. deterministic detection and evidence collection
2. optional explanation and summarization

The detection layer must keep working without any LLM dependency.
The explanation layer may later use OpenAI, but only as an optional add-on.

## DeerFlow-Inspired Funnel

DeerFlow 2's `newsletter-generation` skill uses a three-step funnel:

1. search
2. fetch
3. understand

That pattern is worth keeping, but our system already owns the source boundary and the stored baseline.
So for `web_listening`, the adapted funnel should be:

1. detect
2. fetch evidence
3. explain

### 1. Detect

Use the existing tree monitoring run to identify:

- new pages
- changed pages
- missing pages
- new files
- changed files
- missing files

This step is already implemented in `web_listening/blocks/tree_crawler.py` and surfaced by `tools/run_site_tree.py`.

### 2. Fetch Evidence

For each changed item, load the evidence that explains the result:

- current and previous page snapshots
- current and previous page hashes
- current and previous `fit_markdown`
- file observation records
- source page where a file was discovered
- downloaded document metadata
- SHA-256 values and blob reuse

This step should stay bounded to stored monitoring evidence rather than open web search.

### 3. Explain

Produce a report that translates evidence into conclusions:

- what changed in plain language
- why the change likely matters
- what the user should inspect next
- what URLs or files support that statement

The first implementation should be deterministic Markdown.
Later we can add an optional AI explanation layer when `WL_OPENAI_API_KEY` is present.

## Key Difference From DeerFlow

DeerFlow starts from the open web, so it needs `web_search` to find candidate sources and `web_fetch` to read the most relevant pages.

Our tree monitoring flow is different:

- the site boundary is already defined by `seed_url` and scope rules
- the crawler already fetched the relevant in-scope pages
- the storage layer already keeps historical evidence

So our "search" step is not internet search.
It is candidate selection inside the stored change set.

## Report Requirements

Every change explanation report should lead with a final conclusion block that includes a time label.

It should then summarize the monitoring depth and the quantities people care about most:

- monitored targets
- `max_depth`
- pages seen
- files seen
- new pages
- changed pages
- missing pages
- new files
- changed files
- missing files
- file download failures

After that, it should answer the qualitative questions.

### Human-facing questions

- Did the site publish something new?
- Did a known page materially change?
- Did a document library add a new file?
- Did an existing file change at the same URL?
- Did a previously tracked page disappear?

### Agent-facing questions

- Which URLs should be read next?
- Which pages need a content diff?
- Which files were newly downloaded and where are they stored?
- Which evidence IDs or hashes support the claim?

## Proposed Report Shape

### Section 1: Final Conclusion

- report timestamp
- catalog and target count
- crawl limits
- one-sentence conclusion
- total change counts

Example:

- `2026-04-08T10:00:00Z`
- `dev` catalog, `3` targets, `max_depth=2`
- conclusion: `1 site changed, 1 new page detected, no new files`

### Section 2: Monitoring Depth

- pages seen per site
- files seen per site
- accepted file count
- page failures
- file failures

### Section 3: What Changed

For each site, group the findings by type:

- new pages
- changed pages
- missing pages
- new files
- changed files
- missing files

Each item should include:

- canonical URL
- change type
- short explanation
- evidence pointers

### Section 4: New Content and New Files

This is the most important operator section.
It should call out:

- newly discovered page URLs
- newly discovered file URLs
- whether the file was downloaded
- local document path if available
- SHA-256 for evidence and dedupe

### Section 5: Evidence Appendix

Keep the evidence explicit and machine-friendly:

- `scope_id`
- `run_id`
- `snapshot_id` or `document_id`
- previous hash
- current hash
- source page URL
- download URL
- local path

## Output Modes

### Mode A: Deterministic report

Default mode.
No API key required.

Rules:

- always available
- generated entirely from stored evidence
- no network dependence beyond the crawl itself
- safe for cron and baseline monitoring

### Mode B: AI explanation overlay

Optional mode.
Requires `WL_OPENAI_API_KEY`.

Rules:

- never required for detection
- runs after deterministic evidence exists
- summarizes the most important evidence-backed changes without dropping whole sites by default
- must preserve URLs, hashes, and evidence pointers in the final output

## API Key Policy

The current project should continue to work without `.env` OpenAI credentials for:

- `bootstrap_site_tree.py`
- `run_site_tree.py`
- page snapshot creation
- file discovery
- file download
- SHA-256 dedupe
- deterministic Markdown reporting

OpenAI credentials should remain optional and only power:

- AI summaries
- richer natural-language change explanations
- future agent-facing grouping or wording improvements

## Implementation Phases

### Phase 1: Deterministic evidence report

Add a first-class report builder that turns raw tree-run outputs into evidence-rich Markdown.

Planned work:

- add a report builder module such as `web_listening/blocks/change_report.py`
- load previous and current page snapshots for changed pages
- load file observations and downloaded document metadata for file changes
- upgrade `tools/run_site_tree.py` to render explanation sections instead of only totals

### Phase 2: Coverage-first change organization

The report should stay compact without hiding parts of the monitored tree.
Prefer grouping and section-aware presentation over hard top-N cutoffs.

Planned work:

- group changes by site section, document area, and change type
- surface new files and changed files ahead of generic navigation churn
- preserve whole-site coverage while keeping each section concise

### Phase 3: Optional AI narrative

Use a compact evidence bundle to generate a better narrative summary when an API key is present.

Planned work:

- extend or complement `web_listening/blocks/analyzer.py`
- summarize changed items without losing explicit evidence links
- keep deterministic fallback as the default path

### Phase 4: API and skill exposure

Make the report available through the main product surfaces.

Planned work:

- expose the report through CLI and REST
- point both repo skill variants at the report output
- keep the output suitable for OpenClaw and other agent skill consumers

## Recommended Implementation Order

1. Build deterministic page and file evidence loaders.
2. Replace the counts-only `run_site_tree` Markdown with a richer explanation report.
3. Add coverage-first grouping for section hubs, document areas, and file changes.
4. Add optional OpenAI explanation on top of deterministic evidence.
5. Expose the report through stable CLI and REST interfaces.

## Initial File Touch Points

- `tools/run_site_tree.py`
- `web_listening/blocks/tree_crawler.py`
- `web_listening/blocks/storage.py`
- `web_listening/blocks/analyzer.py`
- `skills/web-listening-tree-monitor/SKILL.md`
- `.codex/skills/web-listening-agent/SKILL.md`

## Success Criteria

We should consider this work successful when a two-day incremental run lets a reader answer the following in under one minute:

- Which sites changed?
- Was the change a page update or a file update?
- What new content or document appeared?
- Was the file downloaded?
- What evidence proves that conclusion?
