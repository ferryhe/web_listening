# Agent Rescue Validation

> Validation date: 2026-04-03  
> Validation source: `config/smoke_site_catalog.json`  
> Validation command: `.venv\Scripts\python tools\run_agent_rescue_validation.py`

## Goal

This report tracks what happens when a site fails its primary smoke target and an AI agent is allowed to try a controlled rescue ladder:

1. use the curated catalog target as-is
2. retry the same target in browser mode
3. fall back to official `sitemap.xml` or `rss.xml` when HTML remains blocked

The point is not to hide failures.
The point is to measure whether the site can still become agent-usable through an evidence-preserving fallback.

## Summary

- Catalog size: `37`
- Sites resolved by primary or rescue strategy: `35 / 37`
- Unresolved sites: `2`
- Required smoke targets resolved: `24 / 24`

## Winning strategy groups

### Catalog target already good enough

These sites did not need a rescue path in the current environment:

- `IAIS`
- `IEA`
- `IPCC`
- `IRFF`
- `ISSB`
- `PCAF`
- `PSI`
- `TNFD`
- `FAO`
- `UNEP`
- `World Bank`
- `ADB`
- `BCBS`
- `BIS`
- `FIT`
- `FSB`
- `G20`
- `GCA`
- `IFAC`
- `ILO`
- `IMF`
- `NGFS`
- `UN Water`
- `UNCTAD`
- `WHO`
- `WRI`
- `WTO`

### Resolved by browser mode

These sites failed the primary HTTP smoke target, but succeeded when a real browser was used:

- `A2ii`
- `OECD`
- `WEF`
- `AFDB`
- `CAF`
- `WMO`

### Resolved by official sitemap fallback

These sites still resisted HTML monitoring, but exposed enough structured official inventory through their sitemap for agent use:

- `UNDP`
- `UNFCCC`

## Unresolved sites

### ISSA

- Primary HTML target returns `403`
- Browser mode reaches a security-verification interstitial, not the real site content
- Official sitemap and RSS endpoints also return `403`
- Current conclusion:
  - not safely automatable from this environment without a sanctioned browser session or a different allowed integration path

### SIF

- Primary site fails TLS handshake or upstream availability checks
- Browser mode does not recover it
- Sitemap and RSS endpoints are also unavailable
- Current conclusion:
  - the source domain appears broken upstream, so there is no reliable public fallback to automate today

## Practical agent policy from this run

- Keep the normal smoke catalog as the first-choice path.
- Allow browser fallback for sites where public content is available but anti-bot or richer rendering blocks plain HTTP.
- Allow official sitemap fallback when the HTML surface is blocked but the site still publishes a public inventory feed.
- Do not silently treat challenge pages or parked domains as success.
- Preserve which rescue strategy won so later agents know whether they are consuming HTML content, browser-rendered content, or sitemap inventory.
