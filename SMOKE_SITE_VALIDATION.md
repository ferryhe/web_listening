# Smoke Site Validation

> Validation date: 2026-04-03  
> Validation source: `config/smoke_site_catalog.json`  
> Validation command: `.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only`

## Summary

- Catalog size: `37` sites
- Required smoke targets passing: `24 / 24`
- Optional expected issues: `9`
- Current JS-heavy or thin-HTML candidates: `IAIS`, `TNFD`, `UNFCCC`, `WHO`

## Required targets currently passing

These sites are currently healthy enough to keep in the required smoke pass set:

- `IEA`
- `IPCC`
- `IRFF`
- `ISSB` via `https://www.ifrs.org/news-and-events/`
- `PCAF`
- `PSI`
- `FAO`
- `UNEP`
- `World Bank`
- `ADB` via `https://www.adb.org/news`
- `BCBS`
- `BIS`
- `FIT`
- `FSB`
- `G20` with `user_agent_profile=browser`
- `GCA`
- `IFAC`
- `ILO` with `user_agent_profile=browser`
- `IMF` via `https://www.imf.org/en/news`
- `NGFS`
- `UN-Water`
- `UNCTAD`
- `WRI`
- `WTO`

## Optional expected issues

These entries stay in the catalog, but are not yet required smoke passes:

### Environment or upstream access issues

- `A2ii`: redirected to a CGAP collection and returned `403`
- `ISSA`: returned `403`
- `OECD`: returned `403`
- `UNDP`: returned `403`
- `WEF`: returned `403`
- `AFDB`: returned `403`
- `WMO`: returned `403`
- `CAF`: TLS certificate validation issue
- `SIF`: upstream domain currently appears broken

### Reachable but still thin over raw HTTP

- `IAIS`: `21` extracted words, many script tags
- `TNFD`: reachable via `/news/`, but only `3` extracted words
- `UNFCCC`: reachable via `/news`, but only `6` extracted words
- `WHO`: reachable via `/news-room`, but only `7` extracted words

## Key decisions from this run

- Keep the raw spreadsheet local-only in ignored folders such as `input/` or `list/`.
- Keep the runnable monitor targets in tracked config at `config/smoke_site_catalog.json`.
- Keep `homepage_url` and `monitor_url` separate.
- Use site-level `fetch_config_json` for cases like `G20` and `ILO` that need a browser-like user agent without changing the global default.
- Do not mark thin-HTML sites as hard failures until we validate whether browser mode is worth the cost.

## Suggested next steps

- Add an importer that rebuilds `config/smoke_site_catalog.json` from new spreadsheets plus tracked overrides.
- Validate Playwright against one or two of `IAIS`, `TNFD`, `UNFCCC`, or `WHO`.
- Find more stable monitor pages for the current `403` sites before promoting them into the required smoke set.
