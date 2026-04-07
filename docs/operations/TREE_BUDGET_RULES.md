# Tree Budget Rules

## Goal

Set recursive tree monitoring budgets by site type instead of forcing every site into one fixed crawl size.

This matters because the 30+ smoke catalog includes very different targets:

- stable homepage-style sites
- news or press sections
- documents or publications libraries
- JS-heavy or thin-HTML targets
- blocked or unstable sites

The crawler budget should match the site shape.

## Keep SHA-256 As The Dedupe Authority

Downloaded files should keep using SHA-256 as the true dedupe standard.

That means:

- blob storage should stay keyed by `sha256`
- URL equality should not be treated as final file identity
- a source-oriented folder layout should be an optional view layer, not the canonical dedupe layer

For now, keep the current blob-first layout.
If we later want easier operator browsing by source site, add site-host aliases or hardlinks on top of the blob store instead of replacing it.

## Classification Dimensions

Decide the tree budget from five questions:

1. Is the site currently reachable from this environment?
2. Is the best seed a homepage, a news section, or a documents section?
3. Does raw HTTP expose enough server-rendered content for recursive crawling?
4. Is document discovery the main goal?
5. Is the site high-priority enough to justify a larger baseline crawl?

## Polite Crawling Rules

When we expand coverage for a full-site picture, do not rely on raw tight loops.
Use explicit pacing to reduce the chance of WAF triggers or temporary IP blocks.

Recommended `fetch_config_json` keys:

- `request_delay_ms`
- `file_request_delay_ms`
- `request_jitter_ms`

Meaning:

- `request_delay_ms`: minimum gap before the next request of any kind
- `file_request_delay_ms`: optional slower gap before file downloads
- `request_jitter_ms`: random extra delay to avoid perfectly regular timing

Recommended starting values:

- homepage or section HTML crawl:
  - `request_delay_ms=1200`
  - `request_jitter_ms=600`
- document-heavy sections:
  - `request_delay_ms=1500`
  - `file_request_delay_ms=2500`
  - `request_jitter_ms=800`
- thin HTML or browser-sensitive sites:
  - `request_delay_ms=2000`
  - `file_request_delay_ms=3000`
  - `request_jitter_ms=1000`

These are starting points, not permanent truths.
If a site is clearly stable, we can later tighten them slightly.
If a site shows blocking symptoms, slow down before trying broader coverage.

## Recommended Profiles

### 1. `blocked_hold`

Use when:

- `smoke_expectation` is `known_blocked`
- or the site is currently `broken_upstream`
- or the site has an `ssl_issue`

Behavior:

- do not enable tree bootstrap yet
- keep only smoke or rescue monitoring until a stable public target exists

### 2. `thin_html_watch`

Use when:

- `js_heavy_candidate=true`
- or `smoke_expectation` is `pass_http_limited`
- or the site only works with browser-like UA and still exposes thin content

Recommended budget:

- `max_depth=2`
- `max_pages=30`
- `max_files=10`
- `request_delay_ms=2000`
- `file_request_delay_ms=3000`
- `request_jitter_ms=1000`

Behavior:

- keep the crawl bounded
- prefer a section seed over the raw homepage
- promote to browser-backed tree crawling only after explicit validation

### 3. `section_news`

Use when:

- the best recursive seed is a news, updates, or press section
- the section is rich in article pages but not primarily a document library

Recommended budget:

- `max_depth=3`
- `max_pages=80`
- `max_files=20`
- `request_delay_ms=1000`
- `file_request_delay_ms=1800`
- `request_jitter_ms=500`

Behavior:

- crawl the bounded news subtree
- accept same-origin files linked from those pages

### 4. `section_documents`

Use when:

- the seed is a documents, publications, reports, or annual-reports section
- new files matter more than homepage churn

Recommended budget:

- `max_depth=3`
- `max_pages=60`
- `max_files=80`
- `request_delay_ms=1500`
- `file_request_delay_ms=2500`
- `request_jitter_ms=800`

Behavior:

- bias toward file discovery
- keep file scope broad enough for `/files`, `/media`, or `/uploads`

### 5. `homepage_standard`

Use when:

- the homepage is reachable
- raw HTTP exposes enough content
- no better section seed is known yet

Recommended budget:

- `max_depth=4`
- `max_pages=120`
- `max_files=40`
- `request_delay_ms=1200`
- `file_request_delay_ms=1800`
- `request_jitter_ms=600`

Behavior:

- this is the default whole-site baseline for stable sites

### 6. `priority_full`

Use when:

- the site is strategic
- stable enough for deeper crawling
- and we want a richer initial site-tree baseline

Recommended budget:

- `max_depth=5`
- `max_pages=200`
- `max_files=80`
- `request_delay_ms=1400`
- `file_request_delay_ms=2200`
- `request_jitter_ms=700`

Behavior:

- use for a small number of high-value sites only

## Section Hub Strategy

For homepage-style sites, the right bootstrap is usually:

1. Crawl the homepage and collect depth-1 section hubs.
2. Keep hubs such as `news`, `publications`, `about`, `research`, and `resources`.
3. Expand those hubs before spending the rest of the page budget on generic alphabetical tails.

That is a better fit than a flat one-pass crawl from the homepage.

Current status:

- the project now has bounded recursive tree monitoring
- section-hub-first expansion is still a recommended next enhancement, not a completed feature

## Proposed Catalog Fields

Add these optional fields to `config/smoke_site_catalog.json`:

- `tree_strategy`
- `tree_budget_profile`
- `tree_max_depth`
- `tree_max_pages`
- `tree_max_files`

Recommended meanings:

- `tree_strategy=homepage_full`
- `tree_strategy=section_news`
- `tree_strategy=section_documents`
- `tree_strategy=thin_html_watch`
- `tree_strategy=blocked_hold`

`tree_budget_profile` should describe the chosen profile name.
The explicit `tree_max_*` fields are per-site overrides when a profile needs adjustment.

## Initial Grouping For The Current 37-Site Catalog

Based on the current smoke catalog:

### `blocked_hold`

`a2ii`, `issa`, `oecd`, `undp`, `wef`, `afdb`, `caf`, `sif`, `wmo`

Count: `9`

### `thin_html_watch`

`iais`, `tnfd`, `g20`, `ilo`, `unfccc`, `who`

Count: `6`

### `section_news`

`iea`

Count: `1`

### `homepage_standard`

`ipcc`, `irff`, `issb`, `pcaf`, `psi`, `fao`, `unep`, `world-bank`, `adb`, `bcbs`, `bis`, `fit`, `fsb`, `gca`, `ifac`, `imf`, `ngfs`, `un-water`, `unctad`, `wri`, `wto`

Count: `21`

## Suggested Next Pass For The 30+ Sites

1. Leave the `blocked_hold` group out of tree bootstrap for now.
2. Start with `homepage_standard` for the 21 stable sites.
3. Keep `thin_html_watch` out of full production tree bootstrap until we validate either:
   - a better section seed
   - a browser-backed fetch path
4. Upgrade document-heavy sites from `homepage_standard` to `section_documents` when we identify a stable publications or reports seed.
5. Reserve `priority_full` for a small subset of strategic sites after the first stable pass.

## Practical Starting Point

For the next smoke-tree rollout:

- bootstrap only the 21 `homepage_standard` sites
- keep the `6` thin or browser-sensitive sites in observation mode
- keep the `9` blocked or unstable sites out of tree bootstrap

Then, in the next pass, add section-specific seeds for document-heavy organizations.
