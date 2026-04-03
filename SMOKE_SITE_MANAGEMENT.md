# Smoke Site Management

> Branch: `docs/ai-agent-roadmap`  
> Last updated: 2026-04-03

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

This is the file that scripts should load:

- `config/smoke_site_catalog.json`

## Why `homepage_url` and `monitor_url` are separate

The best page to monitor is often not the homepage.
For example:

- some homepages are blocked by WAF or bot protection
- some homepages are mostly JS shell content
- some news or publications pages are much richer and more stable for smoke tests

By keeping both fields, we preserve the official source while still giving the crawler the best current smoke target.

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

Run only a subset:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --site-key g20 --site-key ilo
```

If you only want the report and do not want a failing exit code:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only
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
- when smoke targets graduate into deep recursive monitoring, use the separate page-scope and file-scope model from `TREE_MONITORING_DESIGN.md`
