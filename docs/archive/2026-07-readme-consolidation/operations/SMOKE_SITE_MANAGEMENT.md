# Smoke Site Management

> Last updated: 2026-04-06
> Status: Active operations guide

## Goal

Treat large website lists as managed input data, not as ad-hoc one-off scripts.
This repo now uses a two-layer approach:

- local raw inputs live in ignored folders such as `input/` or `list/`
- the curated smoke catalog lives in `config/smoke_site_catalog.json`

That split keeps the repository reproducible while still letting us ingest spreadsheets or exports from outside the repo.

## Recommended workflow

### 1. Raw input

- drop upstream spreadsheets, CSVs, or drafts into `input/` or `list/`
- these folders are git-ignored on purpose
- keep the original file names so we can trace where a row came from

### 2. Curated catalog

- normalize the official homepage into `homepage_url`
- choose the actual monitoring target in `monitor_url`
- record the required fetch settings in `fetch_mode` and `fetch_config_json`
- mark whether the site should currently pass the smoke suite with `smoke_required`
- record the current access expectation in `smoke_expectation`
- mark `js_heavy_candidate=true` when raw HTTP only exposes thin shell HTML
- add `tree_seed_url` and `tree_page_prefixes` when recursive monitoring should start from a different section than the smoke monitor target
- add `tree_strategy`, `tree_budget_profile`, and optional `tree_max_*` overrides when the site should use a non-default recursive crawl budget
- put polite crawl pacing in `fetch_config_json` when a site needs slower request spacing:
  - `request_delay_ms`
  - `file_request_delay_ms`
  - `request_jitter_ms`

This is the file that scripts should load:

- `config/smoke_site_catalog.json`

## Why `homepage_url` and `monitor_url` are separate

The best page to monitor is often not the homepage.
For example:

- some homepages are blocked by WAF or bot protection
- some homepages are mostly JS shell content
- some news or publications pages are much richer and more stable for smoke tests

By keeping both fields, we preserve the official source while still giving the crawler the best current smoke target.

For recursive monitoring, `tree_seed_url` can point at a different HTML section root than `monitor_url`.
This matters when:

- the best smoke target is a feed or sitemap
- the homepage is a poor recursive seed
- a section page such as `/news` is much better for bounded tree crawling

For budget selection rules and the current proposed grouping of the 30+ catalog, read:

- `docs/operations/TREE_BUDGET_RULES.md`

## Current expectation values

- `pass_http`: should pass over the normal HTTP path
- `pass_http_browser_ua`: should pass over HTTP, but needs a browser-like user agent
- `pass_http_limited`: reachable, but server-rendered text is still thin; keep under observation
- `known_blocked`: currently blocked from this environment
- `broken_upstream`: source site or domain currently appears broken
- `ssl_issue`: source site currently fails TLS validation in this environment

## JS-heavy handling

Do not guess blindly from the organization name.
Use evidence from smoke runs:

- low extracted word count
- many script tags
- framework markers such as Next.js or React shell roots
- empty or near-empty fit-Markdown despite `200` responses

When that happens:

- leave the official homepage in `homepage_url`
- point `monitor_url` at a richer page if one exists
- otherwise keep the site in the catalog with `js_heavy_candidate=true`
- promote it to `fetch_mode=browser` only after Playwright validation is worth the cost

## Commands

Run the curated site smoke catalog:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py
```

This now uses the shared rescue ladder by default:

1. catalog target
2. browser retry on the same target
3. official `sitemap.xml`
4. official `rss.xml`

Run only a subset:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --site-key g20 --site-key ilo
```

If you only want the report and do not want a failing exit code:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only
```

If you want the older strict behavior that checks only the curated catalog target:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --primary-only --report-only
```

Run the agent rescue ladder:

```powershell
.venv\Scripts\python tools\run_agent_rescue_validation.py
```

## Current findings from the imported supranational list

- `G20` and `ILO` are currently reachable when the request uses a browser-like user agent.
- `IAIS`, `TNFD`, `UNFCCC`, and `WHO` are current JS-heavy or thin-HTML candidates.
- `A2ii`, `ISSA`, `OECD`, `UNDP`, `WEF`, `AFDB`, and `WMO` are currently blocked from this environment.
- `SIF` currently appears broken upstream.
- `CAF` currently has a TLS validation problem from this environment.

## Next refinement steps

- add an importer that rebuilds the curated catalog from new spreadsheets plus tracked overrides
- validate browser mode on a small number of confirmed JS-heavy targets
- add topic-specific monitor pages for blocked homepages where a stable public page exists
- add document-oriented monitor URLs for institutions where download tracking matters more than homepage monitoring
- when smoke targets graduate into deep recursive monitoring, use the separate page-scope and file-scope model from `../design/TREE_MONITORING_DESIGN.md`
- keep the rescue order explicit for agent usage:
  - catalog target first
  - browser on the same target second
  - official `sitemap.xml` or `rss.xml` third when HTML is blocked but inventory feeds stay public

## Current development workflow

For list-driven work, the branch now has a stable default flow:

1. update the curated catalog in `config/smoke_site_catalog.json`
2. run `tools/run_smoke_site_catalog.py` to see whether the list is agent-usable with the shared rescue ladder
3. run `tools/run_smoke_site_catalog.py --primary-only --report-only` when you need to understand the raw catalog target without rescue help
4. run `tools/run_tree_catalog_validation.py` for sites that are candidates for recursive monitoring
5. keep `SOA`, `CAS`, and `IAA` in the live regression loop with `tools/validate_real_sites.py` and `tools/run_dev_regression.py`
