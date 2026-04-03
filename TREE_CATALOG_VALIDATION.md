# Tree Catalog Validation

- Generated at: `2026-04-03T19:05:33.722145+00:00`
- Catalog path: `C:\Project\web_listening\config\smoke_site_catalog.json`
- Max depth: `3`
- Max pages per scope: `8`
- Max files per scope: `1`
- Download files: `no`
- Sites checked: `37`
- Sites not meeting current tree expectation: `12`

| Site | Required | Outcome | Pages | Child pages | Files | Failures |
|---|---:|---|---:|---:|---:|---:|
| A2ii | no | blocked_root | 0 | 0 | 0 | 1 |
| IAIS | no | ok | 8 | 7 | 1 | 0 |
| IEA | yes | ok | 8 | 7 | 0 | 0 |
| IPCC | yes | ok | 8 | 7 | 1 | 0 |
| IRFF | yes | ok | 8 | 7 | 1 | 0 |
| ISSA | no | blocked_root | 0 | 0 | 0 | 1 |
| ISSB | yes | ok | 8 | 7 | 1 | 0 |
| OECD | no | blocked_root | 0 | 0 | 0 | 1 |
| PCAF | yes | ok | 8 | 7 | 1 | 0 |
| PSI | yes | ok | 8 | 7 | 1 | 0 |
| TNFD | no | unstable_tree | 6 | 5 | 0 | 128 |
| UNDP | no | blocked_root | 0 | 0 | 0 | 1 |
| FAO | yes | ok | 8 | 7 | 0 | 0 |
| UNEP | yes | unstable_tree | 3 | 2 | 0 | 123 |
| WEF | no | blocked_root | 0 | 0 | 0 | 1 |
| World Bank | yes | ok | 8 | 7 | 0 | 0 |
| ADB | yes | ok | 8 | 7 | 1 | 0 |
| AFDB | no | blocked_root | 0 | 0 | 0 | 1 |
| BCBS | yes | ok | 8 | 7 | 0 | 0 |
| BIS | yes | ok | 8 | 7 | 0 | 0 |
| CAF | no | blocked_root | 0 | 0 | 0 | 1 |
| FIT | yes | ok | 8 | 7 | 1 | 0 |
| FSB | yes | ok | 8 | 7 | 1 | 0 |
| G20 | yes | ok | 7 | 6 | 0 | 2 |
| GCA | yes | ok | 8 | 7 | 1 | 0 |
| IFAC | yes | ok | 8 | 7 | 0 | 0 |
| ILO | yes | ok | 8 | 7 | 1 | 0 |
| IMF | yes | ok | 8 | 7 | 1 | 0 |
| NGFS | yes | ok | 8 | 7 | 1 | 0 |
| SIF | no | blocked_root | 0 | 0 | 0 | 1 |
| UN Water | yes | ok | 8 | 7 | 1 | 0 |
| UNCTAD | yes | ok | 8 | 7 | 1 | 0 |
| UNFCCC | no | root_only | 1 | 0 | 0 | 0 |
| WHO | no | ok | 8 | 7 | 0 | 0 |
| WMO | no | blocked_root | 0 | 0 | 0 | 1 |
| WRI | yes | ok | 8 | 7 | 0 | 0 |
| WTO | yes | ok | 8 | 7 | 1 | 0 |

## Sites Not Meeting Current Tree Expectation

### A2ii

- Homepage URL: `https://a2ii.org/`
- Monitor URL: `https://a2ii.org/`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Official URL was supplemented. Requests from this environment redirected to a CGAP collection and returned 403 on 2026-04-03.`

### ISSA

- Homepage URL: `https://www.issa.int/`
- Monitor URL: `https://www.issa.int/`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage returned 403 from this environment on 2026-04-03.`

### OECD

- Homepage URL: `https://www.oecd.org/`
- Monitor URL: `https://www.oecd.org/`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage and publications pages returned 403 from this environment on 2026-04-03.`

### TNFD

- Homepage URL: `https://tnfd.global/`
- Monitor URL: `https://tnfd.global/news/`
- Outcome: `unstable_tree`
- Required smoke target: `no`
- Pages discovered: `6`
- Child pages discovered: `5`
- File links accepted: `0`
- Page failures: `128`
- Skipped external pages: `18`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage returned 403; the news page is reachable but still exposes very little server-rendered text.`

### UNDP

- Homepage URL: `https://www.undp.org`
- Monitor URL: `https://www.undp.org`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage and tested section pages returned 403 from this environment on 2026-04-03.`

### UNEP

- Homepage URL: `https://www.unep.org`
- Monitor URL: `https://www.unep.org`
- Outcome: `unstable_tree`
- Required smoke target: `yes`
- Pages discovered: `3`
- Child pages discovered: `2`
- File links accepted: `0`
- Page failures: `123`
- Skipped external pages: `52`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`

### WEF

- Homepage URL: `https://www.weforum.org`
- Monitor URL: `https://www.weforum.org`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage and tested section pages returned 403 from this environment on 2026-04-03.`

### AFDB

- Homepage URL: `https://www.afdb.org/en`
- Monitor URL: `https://www.afdb.org/en`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage and news page returned 403 from this environment on 2026-04-03.`

### CAF

- Homepage URL: `https://www.caf.com/en/`
- Monitor URL: `https://www.caf.com/en/`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage currently fails certificate validation from this environment.`

### SIF

- Homepage URL: `https://www.sustainableinsuranceforum.org/`
- Monitor URL: `https://www.sustainableinsuranceforum.org/`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `The domain currently resolves to a parked Linkila 404 page in this environment.`

### UNFCCC

- Homepage URL: `https://unfccc.int/`
- Monitor URL: `https://unfccc.int/documents`
- Outcome: `root_only`
- Required smoke target: `no`
- Pages discovered: `1`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `0`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Smoke monitor uses the news page, which is reachable but still exposes almost no server-rendered text.`

### WMO

- Homepage URL: `https://wmo.int/`
- Monitor URL: `https://wmo.int/`
- Outcome: `blocked_root`
- Required smoke target: `no`
- Pages discovered: `0`
- Child pages discovered: `0`
- File links accepted: `0`
- Page failures: `1`
- Skipped external pages: `0`
- Skipped external files: `0`
- Off-prefix same-origin files: `0`
- Notes: `Homepage and news page returned 403 from this environment on 2026-04-03.`

