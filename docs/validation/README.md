# Validation Guide

This folder now holds evergreen validation guidance, not committed point-in-time result snapshots.

Live validation outputs should be generated into `data/reports/` when needed.

## Recommended Validation Layers

### Unit and integration tests

```powershell
.venv\Scripts\pytest tests -q
```

### Required dev targets

Use the 3 required live targets:

- `SOA`
- `CAS`
- `IAA`

Commands:

```powershell
.venv\Scripts\python tools\validate_real_sites.py
.venv\Scripts\python tools\run_dev_regression.py
```

See [DEV_TEST_TARGETS.md](C:/Project/web_listening/docs/operations/DEV_TEST_TARGETS.md) for the current expectations.

### Smoke catalog validation

Validate the curated 30+ site list:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --report-only
```

Use `--primary-only` when you want to inspect the raw catalog target without the rescue ladder:

```powershell
.venv\Scripts\python tools\run_smoke_site_catalog.py --primary-only --report-only
```

### Tree readiness validation

Use the bounded tree validator before promoting a site into recursive monitoring:

```powershell
.venv\Scripts\python tools\run_tree_catalog_validation.py
```

### Rescue ladder validation

Validate the shared agent rescue path:

```powershell
.venv\Scripts\python tools\run_agent_rescue_validation.py
```

## Reporting Rule

Do not treat committed Markdown snapshots as the source of truth for validation status.

Instead:

- run the live commands
- write current reports into `data/reports/`
- keep the repo docs focused on procedures and expectations
