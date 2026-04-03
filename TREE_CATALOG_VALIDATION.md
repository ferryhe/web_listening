# Tree Catalog Validation

> Validation date: 2026-04-03  
> Validation source: `config/smoke_site_catalog.json`  
> Validation command: `.venv\Scripts\python tools\run_tree_catalog_validation.py`

## What this checks

This report answers a narrower question than the normal smoke pass:

- can the site root or curated `monitor_url` be fetched
- can the system discover child pages within a bounded tree
- can the tree stay stable enough for recursive monitoring
- can same-origin file links still be accepted inside the tree scope

Run configuration for this baseline:

- `max_depth = 3`
- `max_pages = 8`
- `max_files = 1`
- file downloads were disabled for the full-catalog tree run

Download behavior is still covered separately by the required live dev regression on `SOA`, `CAS`, and `IAA`.

## Summary

- Catalog size: `37`
- Sites meeting current bounded-tree expectation: `25`
- Sites not meeting current bounded-tree expectation: `12`
- Required smoke targets meeting bounded-tree expectation: `23 / 24`
- Required smoke targets currently below tree expectation: `UNEP`

## Current interpretation

- `ok`: root is reachable and the bounded crawl can discover at least one child page without excessive failures
- `root_only`: root is reachable, but no child pages are discovered inside the current scope
- `unstable_tree`: child pages exist, but failure volume is high enough that the tree is not yet reliable for automation
- `blocked_root`: the root or monitor entry point cannot be fetched from the current environment

## Sites Not Yet Meeting Tree Expectation

| Site | Required | Outcome | Pages | Child pages | Files | Failures | Notes |
|---|---:|---|---:|---:|---:|---:|---|
| A2ii | no | blocked_root | 0 | 0 | 0 | 1 | Redirects to CGAP collection and returns `403` |
| ISSA | no | blocked_root | 0 | 0 | 0 | 1 | `403` from this environment |
| OECD | no | blocked_root | 0 | 0 | 0 | 1 | `403` from this environment |
| TNFD | no | unstable_tree | 6 | 5 | 0 | 128 | Reachable news root, but recursive fetches fail heavily |
| UNDP | no | blocked_root | 0 | 0 | 0 | 1 | `403` from this environment |
| UNEP | yes | unstable_tree | 3 | 2 | 0 | 123 | Reachable root, but recursive fetches fail heavily |
| WEF | no | blocked_root | 0 | 0 | 0 | 1 | `403` from this environment |
| AFDB | no | blocked_root | 0 | 0 | 0 | 1 | `403` from this environment |
| CAF | no | blocked_root | 0 | 0 | 0 | 1 | TLS certificate validation fails in this environment |
| SIF | no | blocked_root | 0 | 0 | 0 | 1 | Upstream domain appears broken or parked |
| UNFCCC | no | root_only | 1 | 0 | 0 | 0 | Curated news root is reachable, but still behaves like a thin single page |
| WMO | no | blocked_root | 0 | 0 | 0 | 1 | `403` from this environment |

## Key findings

- The trailing-slash-sensitive seed issue was real. After splitting request URL sanitation from canonical identity, `TNFD` moved from `blocked_root` to `unstable_tree`, which is a much more accurate classification.
- `IEA` now passes bounded-tree validation when the catalog uses a dedicated recursive seed at `/news` instead of the homepage.
- `TNFD` and `UNEP` are the most important recursive-protocol failures right now. They are reachable, but the current bounded crawl sees too many failing descendants for dependable automation.
- The major hard blockers are still environment or upstream access problems: `A2ii`, `ISSA`, `OECD`, `UNDP`, `WEF`, `AFDB`, `CAF`, `SIF`, `WMO`.
- Some previously thin-HTML candidates are structurally better than expected. `WHO` now passes bounded-tree discovery even though its root-page text is still sparse over raw HTTP.

## Recommended next actions

- Add per-site `tree_scope` overrides so a catalog entry can choose a better recursive seed than the homepage.
- Add include/exclude patterns before promoting unstable trees like `TNFD` or `UNEP` into required recursive targets.
- Keep `SOA`, `CAS`, and `IAA` as the download-and-hash regression set while tree monitoring is still maturing.
- Validate Playwright on at least one of `TNFD`, `UNFCCC`, `WHO`, or `IAIS` before deciding whether browser mode should be used for recursive expansion.
