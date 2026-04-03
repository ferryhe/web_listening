# Real Site Validation

> Validation date: 2026-04-03  
> Validation mode: `http`  
> Environment: project-local `.venv`

## Scope

This validation used live public websites to check whether the current normalized snapshot layer is already useful on real actuarial organization sites.
The canonical target list and minimum thresholds now live in `config/dev_test_sites.json`.

Targets:

- `https://www.soa.org/`
- `https://www.soa.org/publications/publications-landing/`
- `https://www.casact.org/`
- `https://www.casact.org/about/governance/annual-reports`
- `https://actuaries.org/`
- `https://actuaries.org/annual-reports/`

## Summary

| Site | URL | Status | Words | Links | Doc links |
|---|---|---:|---:|---:|---:|
| SOA Monitor | `https://www.soa.org/` | 200 | 420 | 156 | 0 |
| SOA Documents | `https://www.soa.org/publications/publications-landing/` | 200 | 450 | 160 | 2 |
| CAS Monitor | `https://www.casact.org/` | 200 | 481 | 244 | 2 |
| CAS Documents | `https://www.casact.org/about/governance/annual-reports` | 200 | 103 | 247 | 14 |
| IAA Monitor | `https://actuaries.org/` | 200 | 514 | 104 | 0 |
| IAA Documents | `https://actuaries.org/annual-reports/` | 200 | 146 | 112 | 16 |

## Findings

### SOA Monitor

- The homepage became materially cleaner after the normalizer started preferring `#content`.
- HTTP mode is sufficient to capture the main promotional content on the public homepage.
- The homepage is not a strong document-discovery target because it exposed `0` document links in this run.

Markdown head:

```text
## Empower Your Actuarial Journey

A rewarding career with consistently bright prospects. Society of Actuaries (SOA) guides the way to your future as an actuary.
```

### SOA Documents

- This page is a much better monitoring target than the homepage for document-centric tracking.
- HTTP mode extracted clear body content and discovered `2` PDF links in this run.

Sample document links:

- `https://www.soa.org/globalassets/assets/files/pubs/naaj-instructions-for-authors-2023.pdf`
- `https://www.soa.org/globalassets/assets/files/static-pages/publications/naaj-soa-member-access-2025.pdf`

### CAS Monitor

- HTTP mode already works well on the public homepage.
- The normalized Markdown starts with the page title and organization description instead of mostly navigation.
- The homepage exposed `2` document links in this run, so it is already useful as a lightweight monitoring target.

Markdown head:

```text
# Casualty Actuarial Society

CAS members are sought after globally for their insights and ability to apply analytics to solve insurance and risk management problems.
```

Sample document links:

- `https://www.casact.org/sites/default/files/2021-02/about_thecas_organizational_chart.pdf`
- `https://www.casact.org/sites/default/files/2025-01/2025_SOBE.pdf#page=11`

### CAS Documents

- This is the strongest real-world document-monitoring target from this validation set.
- HTTP mode discovered `14` PDF links in this run.
- This confirms that the current architecture is already useful for public PDF archives without any browser dependency.

Sample document links:

- `https://www.casact.org/sites/default/files/2021-02/about_thecas_annual_reports_annual-report-2013.pdf`
- `https://www.casact.org/sites/default/files/2021-02/about_thecas_annual_reports_annual-report-2014.pdf`
- `https://www.casact.org/sites/default/files/2021-02/about_thecas_annual_reports_annual-report-2015.pdf`
- `https://www.casact.org/sites/default/files/2021-02/about_thecas_annual_reports_annual-report-2016.pdf`
- `https://www.casact.org/sites/default/files/2021-02/about_thecas_annual_reports_annual-report-2017.pdf`

### IAA Monitor

- HTTP mode captures the public homepage content in a usable Markdown form.
- The homepage is suitable as a monitoring target, but not strong by itself for document discovery.
- This makes it a good partner to the dedicated annual reports page in the required dev target set.

Markdown head:

```text
## 33rd International Congress of Actuaries

The International Congress of Actuaries (ICA) is a conference held under the auspices of the International Actuarial Association every three years.
```

### IAA Documents

- The annual reports page is a strong public document-monitoring target.
- HTTP mode discovered `16` PDF links in this run.
- This gives us public document coverage for `actuaries.org` without needing browser mode.

Sample document links:

- `https://actuaries.org/app/uploads/2026/01/2016AnnualReportEN.pdf`
- `https://actuaries.org/app/uploads/2026/01/2016AnnualReportFR.pdf`
- `https://actuaries.org/app/uploads/2026/01/2017_Annual_Report.pdf`
- `https://actuaries.org/app/uploads/2026/01/2017_Annual_Report_FR.pdf`
- `https://actuaries.org/app/uploads/2026/01/2018AnnualReport.pdf`

## Conclusion

For the tested public pages on `soa.org`, `casact.org`, and `actuaries.org`, **browser tooling is not required to begin monitoring effectively**. The current HTTP path is already good enough for:

- main-content snapshotting
- Markdown normalization
- document-link discovery on publication-heavy pages
- stable page hashing from `fit_markdown`

Browser tooling is still worth keeping and expanding as an optional capability for:

- JS-rendered pages with weak server-side HTML
- login-gated or interaction-dependent pages
- pages that require click, wait, or form steps before content appears

## Reproduction

Run:

```powershell
.venv\Scripts\python tools\validate_real_sites.py
```

For the full required dev regression, including repeated-fetch hash stability and sample downloads, run:

```powershell
.venv\Scripts\python tools\run_dev_regression.py
```

Use `--report-only` if you need the Markdown output without a failing exit code on regressions.
