# Smoke Site Catalog Report

- Generated at: `2026-04-03T19:15:00.701687+00:00`
- Catalog path: `C:\Project\web_listening\config\smoke_site_catalog.json`
- Sites checked: `37`
- Required smoke targets passed: `24/24`
- Optional expected issues: `2`
- Resolved by rescue ladder: `8`

| Site | Required | Expectation | Outcome | Status | Words | Min words | Resolved by | JS |
|---|---:|---|---|---:|---:|---:|---|---:|
| A2ii | no | known_blocked | rescued_browser | 200 | 607 | 100 | browser | no |
| IAIS | no | pass_http_limited | ok | 200 | 602 | 20 | catalog | yes |
| IEA | yes | pass_http | ok | 200 | 547 | 200 | catalog | no |
| IPCC | yes | pass_http | ok | 200 | 875 | 300 | catalog | no |
| IRFF | yes | pass_http | ok | 200 | 341 | 150 | catalog | no |
| ISSA | no | known_blocked | expected_issue | 403 | 0 | 100 | expected-issue | no |
| ISSB | yes | pass_http | ok | 200 | 432 | 200 | catalog | no |
| OECD | no | known_blocked | rescued_browser | 200 | 965 | 100 | browser | no |
| PCAF | yes | pass_http | ok | 200 | 137 | 100 | catalog | no |
| PSI | yes | pass_http | ok | 200 | 1135 | 300 | catalog | no |
| TNFD | no | pass_http_limited | ok | 200 | 3 | 3 | catalog | yes |
| UNDP | no | known_blocked | rescued_sitemap | 200 | 14 | 100 | sitemap | no |
| FAO | yes | pass_http | ok | 200 | 1468 | 400 | catalog | no |
| UNEP | yes | pass_http | ok | 200 | 493 | 200 | catalog | no |
| WEF | no | known_blocked | rescued_browser | 200 | 9185 | 100 | browser | no |
| World Bank | yes | pass_http | ok | 200 | 825 | 300 | catalog | no |
| ADB | yes | pass_http | ok | 200 | 1152 | 300 | catalog | no |
| AFDB | no | known_blocked | rescued_browser | 200 | 317 | 100 | browser | no |
| BCBS | yes | pass_http | ok | 200 | 121 | 80 | catalog | no |
| BIS | yes | pass_http | ok | 200 | 186 | 120 | catalog | no |
| CAF | no | ssl_issue | rescued_browser | 200 | 1184 | 100 | browser | no |
| FIT | yes | pass_http | ok | 200 | 462 | 200 | catalog | no |
| FSB | yes | pass_http | ok | 200 | 499 | 200 | catalog | no |
| G20 | yes | pass_http_browser_ua | ok | 200 | 557 | 50 | catalog | no |
| GCA | yes | pass_http | ok | 200 | 597 | 250 | catalog | no |
| IFAC | yes | pass_http | ok | 200 | 414 | 150 | catalog | no |
| ILO | yes | pass_http_browser_ua | ok | 200 | 455 | 50 | catalog | no |
| IMF | yes | pass_http | ok | 200 | 636 | 80 | catalog | no |
| NGFS | yes | pass_http | ok | 200 | 252 | 100 | catalog | no |
| SIF | no | broken_upstream | expected_issue | - | 0 | 100 | expected-issue | no |
| UN Water | yes | pass_http | ok | 200 | 448 | 200 | catalog | no |
| UNCTAD | yes | pass_http | ok | 200 | 569 | 250 | catalog | no |
| UNFCCC | no | pass_http_limited | rescued_sitemap | 200 | 88 | 3 | sitemap | yes |
| WHO | no | pass_http_limited | ok | 200 | 264 | 5 | catalog | yes |
| WMO | no | known_blocked | rescued_browser | 200 | 475 | 100 | browser | no |
| WRI | yes | pass_http | ok | 200 | 693 | 250 | catalog | no |
| WTO | yes | pass_http | ok | 200 | 176 | 120 | catalog | no |

## Details

### A2ii

- Full name: `Access to Insurance Initiative`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `rescued_browser`
- Resolved: `yes`
- Resolved strategy: `browser`
- Monitor URL: `https://a2ii.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.cgap.org/topics/collections/access-to-insurance-initiative`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.cgap.org/topics/collections/access-to-insurance-initiative`
- Fetch mode: `browser`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `607` (expected minimum `100`)
- Link count: `73`
- Source kind: `html`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `2`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`200` words=`607` links=`73` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Official URL was supplemented. Requests from this environment redirected to a CGAP collection and returned 403 on 2026-04-03.`

### IAIS

- Full name: `International Association of Insurance Supervisors`
- Required: `no`
- Expectation: `pass_http_limited`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.iais.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.iais.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `602`
- Primary link count: `90`
- Final URL: `https://www.iais.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `602` (expected minimum `20`)
- Link count: `90`
- Source kind: `html`
- JS-heavy candidate: `yes`
- JS markers: `scripts=39`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`602` links=`90` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage was reachable over HTTP but only exposed limited text in raw HTML; keep it flagged as a browser candidate.`

### IEA

- Full name: `International Energy Agency`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://iea.org`
- Primary strategy: `catalog`
- Primary final URL: `https://www.iea.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `547`
- Primary link count: `134`
- Final URL: `https://www.iea.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `547` (expected minimum `200`)
- Link count: `134`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=21`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`547` links=`134` kind=`html` passed=`yes` reason=`content_ok`

### IPCC

- Full name: `Intergovernmental Panel on Climate Change`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.ipcc.ch/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.ipcc.ch/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `875`
- Primary link count: `128`
- Final URL: `https://www.ipcc.ch/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `875` (expected minimum `300`)
- Link count: `128`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=29`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`875` links=`128` kind=`html` passed=`yes` reason=`content_ok`

### IRFF

- Full name: `Insurance and Risk Finance Facility`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://irff.undp.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://irff.undp.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `341`
- Primary link count: `97`
- Final URL: `https://irff.undp.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `341` (expected minimum `150`)
- Link count: `97`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=39`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`341` links=`97` kind=`html` passed=`yes` reason=`content_ok`

### ISSA

- Full name: `International Social Security Association`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `expected_issue`
- Resolved: `no`
- Resolved strategy: `none`
- Monitor URL: `https://www.issa.int/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.issa.int/`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.issa.int/`
- Fetch mode: `http`
- Status code: `403`
- Word count: `0` (expected minimum `100`)
- Link count: `0`
- Source kind: `error`
- JS-heavy candidate: `no`
- Attempts tried: `4`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`403` words=`35` links=`2` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt `sitemap` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `rss` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.issa.int/rss.xml'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Notes: `Homepage returned 403 from this environment on 2026-04-03.`

### ISSB

- Full name: `International Sustainability Standards Board`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.ifrs.org/news-and-events/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.ifrs.org/news-and-events/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `432`
- Primary link count: `102`
- Final URL: `https://www.ifrs.org/news-and-events/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `432` (expected minimum `200`)
- Link count: `102`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=24`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`432` links=`102` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Smoke monitor points to the broader IFRS news page because the ISSB group page exposed almost no usable text over raw HTTP.`

### OECD

- Full name: `Organisation for Economic Co-operation and Development`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `rescued_browser`
- Resolved: `yes`
- Resolved strategy: `browser`
- Monitor URL: `https://www.oecd.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.oecd.org/`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.oecd.org/`
- Fetch mode: `browser`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `965` (expected minimum `100`)
- Link count: `416`
- Source kind: `html`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `2`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`200` words=`965` links=`416` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage and publications pages returned 403 from this environment on 2026-04-03.`

### PCAF

- Full name: `Partnership for Carbon Accounting Financials`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://carbonaccountingfinancials.com/`
- Primary strategy: `catalog`
- Primary final URL: `https://carbonaccountingfinancials.com/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `137`
- Primary link count: `52`
- Final URL: `https://carbonaccountingfinancials.com/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `137` (expected minimum `100`)
- Link count: `52`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`137` links=`52` kind=`html` passed=`yes` reason=`content_ok`

### PSI

- Full name: `Principles for Sustainable Insurance`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.unepfi.org/insurance/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.unepfi.org/insurance/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `1135`
- Primary link count: `206`
- Final URL: `https://www.unepfi.org/insurance/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `1135` (expected minimum `300`)
- Link count: `206`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=34`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`1135` links=`206` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Normalized the workbook URL to the canonical UNEP FI insurance landing page.`

### TNFD

- Full name: `Taskforce on Nature-related Financial Disclosures`
- Required: `no`
- Expectation: `pass_http_limited`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://tnfd.global/news/`
- Primary strategy: `catalog`
- Primary final URL: `https://tnfd.global/news/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `3`
- Primary link count: `127`
- Final URL: `https://tnfd.global/news/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `3` (expected minimum `3`)
- Link count: `127`
- Source kind: `html`
- JS-heavy candidate: `yes`
- JS markers: `scripts=64, low_text_high_script`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`3` links=`127` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage returned 403; the news page is reachable but still exposes very little server-rendered text.`

### UNDP

- Full name: `United Nations Development Programme`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `rescued_sitemap`
- Resolved: `yes`
- Resolved strategy: `sitemap`
- Monitor URL: `https://www.undp.org`
- Primary strategy: `catalog`
- Primary final URL: `https://www.undp.org`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.undp.org/sitemap.xml`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `14` (expected minimum `100`)
- Link count: `7`
- Source kind: `xml_sitemap`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `3`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`403` words=`15` links=`0` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt `sitemap` via `http`: status=`200` words=`14` links=`7` kind=`xml_sitemap` passed=`yes` reason=`sitemap_inventory`
- Notes: `Homepage and tested section pages returned 403 from this environment on 2026-04-03.`

### FAO

- Full name: `Food and Agriculture Organization`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.fao.org/home/en`
- Primary strategy: `catalog`
- Primary final URL: `https://www.fao.org/home/en`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `1468`
- Primary link count: `62`
- Final URL: `https://www.fao.org/home/en`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `1468` (expected minimum `400`)
- Link count: `62`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=30`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`1468` links=`62` kind=`html` passed=`yes` reason=`content_ok`

### UNEP

- Full name: `United Nations Environment Programme`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.unep.org`
- Primary strategy: `catalog`
- Primary final URL: `https://www.unep.org`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `493`
- Primary link count: `110`
- Final URL: `https://www.unep.org`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `493` (expected minimum `200`)
- Link count: `110`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=21`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`493` links=`110` kind=`html` passed=`yes` reason=`content_ok`

### WEF

- Full name: `World Economic Forum`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `rescued_browser`
- Resolved: `yes`
- Resolved strategy: `browser`
- Monitor URL: `https://www.weforum.org`
- Primary strategy: `catalog`
- Primary final URL: `https://www.weforum.org`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.weforum.org/`
- Fetch mode: `browser`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `9185` (expected minimum `100`)
- Link count: `130`
- Source kind: `html`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `2`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`200` words=`9185` links=`130` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage and tested section pages returned 403 from this environment on 2026-04-03.`

### World Bank

- Full name: `World Bank Group`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.worldbank.org/ext/en/home`
- Primary strategy: `catalog`
- Primary final URL: `https://www.worldbank.org/ext/en/home`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `825`
- Primary link count: `52`
- Final URL: `https://www.worldbank.org/ext/en/home`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `825` (expected minimum `300`)
- Link count: `52`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`825` links=`52` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Smoke monitor uses the resolved homepage URL observed in the current environment.`

### ADB

- Full name: `Asian Development Bank`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.adb.org/news`
- Primary strategy: `catalog`
- Primary final URL: `https://www.adb.org/news`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `1152`
- Primary link count: `293`
- Final URL: `https://www.adb.org/news`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `1152` (expected minimum `300`)
- Link count: `293`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=71`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`1152` links=`293` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Smoke monitor uses the news page because it is much richer than the homepage over raw HTTP.`

### AFDB

- Full name: `African Development Bank Group`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `rescued_browser`
- Resolved: `yes`
- Resolved strategy: `browser`
- Monitor URL: `https://www.afdb.org/en`
- Primary strategy: `catalog`
- Primary final URL: `https://www.afdb.org/en`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.afdb.org/en`
- Fetch mode: `browser`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `317` (expected minimum `100`)
- Link count: `222`
- Source kind: `html`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `2`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`200` words=`317` links=`222` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage and news page returned 403 from this environment on 2026-04-03.`

### BCBS

- Full name: `The Basel Committee on Banking Supervision`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.bis.org/bcbs/index.htm`
- Primary strategy: `catalog`
- Primary final URL: `https://www.bis.org/bcbs/index.htm`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `121`
- Primary link count: `33`
- Final URL: `https://www.bis.org/bcbs/index.htm`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `121` (expected minimum `80`)
- Link count: `33`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`121` links=`33` kind=`html` passed=`yes` reason=`content_ok`

### BIS

- Full name: `Bank for International Settlements`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.bis.org/index.htm`
- Primary strategy: `catalog`
- Primary final URL: `https://www.bis.org/index.htm`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `186`
- Primary link count: `43`
- Final URL: `https://www.bis.org/index.htm`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `186` (expected minimum `120`)
- Link count: `43`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`186` links=`43` kind=`html` passed=`yes` reason=`content_ok`

### CAF

- Full name: `CAF - Development Bank of Latin America and the Caribbean`
- Required: `no`
- Expectation: `ssl_issue`
- Outcome: `rescued_browser`
- Resolved: `yes`
- Resolved strategy: `browser`
- Monitor URL: `https://www.caf.com/en/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.caf.com/en/`
- Primary fetch mode: `http`
- Primary status code: `None`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.caf.com/en/`
- Fetch mode: `browser`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `1184` (expected minimum `100`)
- Link count: `87`
- Source kind: `html`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `2`
- Attempt `catalog` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt `browser` via `browser`: status=`200` words=`1184` links=`87` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage currently fails certificate validation from this environment.`

### FIT

- Full name: `Forum for Insurance Transition to Net Zero`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `462`
- Primary link count: `51`
- Final URL: `https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `462` (expected minimum `200`)
- Link count: `51`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=41`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`462` links=`51` kind=`html` passed=`yes` reason=`content_ok`

### FSB

- Full name: `Financial Stability Board`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.fsb.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.fsb.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `499`
- Primary link count: `85`
- Final URL: `https://www.fsb.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `499` (expected minimum `200`)
- Link count: `85`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`499` links=`85` kind=`html` passed=`yes` reason=`content_ok`

### G20

- Full name: `Group of Twenty`
- Required: `yes`
- Expectation: `pass_http_browser_ua`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://g20.org`
- Primary strategy: `catalog`
- Primary final URL: `https://g20.org`
- Primary fetch mode: `http`
- Primary request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Primary status code: `200`
- Primary word count: `557`
- Primary link count: `217`
- Final URL: `https://g20.org`
- Fetch mode: `http`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `557` (expected minimum `50`)
- Link count: `217`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=47`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`557` links=`217` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage returned 404 to the default bot UA but 200 to a browser-like UA in this environment.`

### GCA

- Full name: `Global Center on Adaptation`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://gca.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://gca.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `597`
- Primary link count: `76`
- Final URL: `https://gca.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `597` (expected minimum `250`)
- Link count: `76`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=60`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`597` links=`76` kind=`html` passed=`yes` reason=`content_ok`

### IFAC

- Full name: `International Federation of Accountants`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.ifac.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.ifac.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `414`
- Primary link count: `82`
- Final URL: `https://www.ifac.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `414` (expected minimum `150`)
- Link count: `82`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`414` links=`82` kind=`html` passed=`yes` reason=`content_ok`

### ILO

- Full name: `International Labour Organization`
- Required: `yes`
- Expectation: `pass_http_browser_ua`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.ilo.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.ilo.org/`
- Primary fetch mode: `http`
- Primary request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Primary status code: `200`
- Primary word count: `455`
- Primary link count: `64`
- Final URL: `https://www.ilo.org/`
- Fetch mode: `http`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `455` (expected minimum `50`)
- Link count: `64`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`455` links=`64` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage returned 403 to the default bot UA but 200 to a browser-like UA in this environment.`

### IMF

- Full name: `International Monetary Fund`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.imf.org/en/news`
- Primary strategy: `catalog`
- Primary final URL: `https://www.imf.org/en/news`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `636`
- Primary link count: `200`
- Final URL: `https://www.imf.org/en/news`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `636` (expected minimum `80`)
- Link count: `200`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `nextjs, scripts=25`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`636` links=`200` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Smoke monitor uses the news page because it exposes more stable text than the homepage.`

### NGFS

- Full name: `Network for Greening the Financial System`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.ngfs.net/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.ngfs.net/en`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `252`
- Primary link count: `53`
- Final URL: `https://www.ngfs.net/en`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `252` (expected minimum `100`)
- Link count: `53`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`252` links=`53` kind=`html` passed=`yes` reason=`content_ok`

### SIF

- Full name: `Sustainable Insurance Forum`
- Required: `no`
- Expectation: `broken_upstream`
- Outcome: `expected_issue`
- Resolved: `no`
- Resolved strategy: `none`
- Monitor URL: `https://www.sustainableinsuranceforum.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.sustainableinsuranceforum.org/`
- Primary fetch mode: `http`
- Primary status code: `None`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://www.sustainableinsuranceforum.org/`
- Fetch mode: `http`
- Status code: `None`
- Word count: `0` (expected minimum `100`)
- Link count: `0`
- Source kind: `error`
- JS-heavy candidate: `no`
- Attempts tried: `4`
- Attempt `catalog` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt `browser` via `browser`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`Error`
- Attempt `sitemap` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt `rss` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Error: `ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1006)`
- Notes: `The domain currently resolves to a parked Linkila 404 page in this environment.`

### UN Water

- Full name: `UN-Water`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.unwater.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.unwater.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `448`
- Primary link count: `84`
- Final URL: `https://www.unwater.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `448` (expected minimum `200`)
- Link count: `84`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`448` links=`84` kind=`html` passed=`yes` reason=`content_ok`

### UNCTAD

- Full name: `United Nations Conference on Trade and Development`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://unctad.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://unctad.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `569`
- Primary link count: `218`
- Final URL: `https://unctad.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `569` (expected minimum `250`)
- Link count: `218`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`569` links=`218` kind=`html` passed=`yes` reason=`content_ok`

### UNFCCC

- Full name: `United Nations Framework Convention on Climate Change`
- Required: `no`
- Expectation: `pass_http_limited`
- Outcome: `rescued_sitemap`
- Resolved: `yes`
- Resolved strategy: `sitemap`
- Monitor URL: `https://unfccc.int/news`
- Primary strategy: `catalog`
- Primary final URL: `https://unfccc.int/news`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `6`
- Primary link count: `0`
- Final URL: `https://unfccc.int/sitemap.xml`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `88` (expected minimum `3`)
- Link count: `44`
- Source kind: `xml_sitemap`
- Rescue ladder: `yes`
- JS-heavy candidate: `yes`
- Attempts tried: `3`
- Attempt `catalog` via `http`: status=`200` words=`6` links=`0` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt `browser` via `browser`: status=`200` words=`6` links=`0` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt `sitemap` via `http`: status=`200` words=`88` links=`44` kind=`xml_sitemap` passed=`yes` reason=`sitemap_inventory`
- Notes: `Smoke monitor uses the news page, which is reachable but still exposes almost no server-rendered text.`

### WHO

- Full name: `World Health Organization`
- Required: `no`
- Expectation: `pass_http_limited`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.who.int/news-room`
- Primary strategy: `catalog`
- Primary final URL: `https://www.who.int/news-room`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `264`
- Primary link count: `237`
- Final URL: `https://www.who.int/news-room`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `264` (expected minimum `5`)
- Link count: `237`
- Source kind: `html`
- JS-heavy candidate: `yes`
- JS markers: `scripts=37`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`264` links=`237` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Smoke monitor uses the news room, but the page still exposes very little server-rendered text.`

### WMO

- Full name: `World Meteorological Organization`
- Required: `no`
- Expectation: `known_blocked`
- Outcome: `rescued_browser`
- Resolved: `yes`
- Resolved strategy: `browser`
- Monitor URL: `https://wmo.int/`
- Primary strategy: `catalog`
- Primary final URL: `https://wmo.int/`
- Primary fetch mode: `http`
- Primary status code: `403`
- Primary word count: `0`
- Primary link count: `0`
- Final URL: `https://wmo.int/`
- Fetch mode: `browser`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Status code: `200`
- Word count: `475` (expected minimum `100`)
- Link count: `85`
- Source kind: `html`
- Rescue ladder: `yes`
- JS-heavy candidate: `no`
- Attempts tried: `2`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt `browser` via `browser`: status=`200` words=`475` links=`85` kind=`html` passed=`yes` reason=`content_ok`
- Notes: `Homepage and news page returned 403 from this environment on 2026-04-03.`

### WRI

- Full name: `World Resources Institute`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.wri.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.wri.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `693`
- Primary link count: `103`
- Final URL: `https://www.wri.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `693` (expected minimum `250`)
- Link count: `103`
- Source kind: `html`
- JS-heavy candidate: `no`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`693` links=`103` kind=`html` passed=`yes` reason=`content_ok`

### WTO

- Full name: `World Trade Organization`
- Required: `yes`
- Expectation: `pass_http`
- Outcome: `ok`
- Resolved: `yes`
- Resolved strategy: `catalog`
- Monitor URL: `https://www.wto.org/`
- Primary strategy: `catalog`
- Primary final URL: `https://www.wto.org/`
- Primary fetch mode: `http`
- Primary request user agent: `web-listening-bot/1.0`
- Primary status code: `200`
- Primary word count: `176`
- Primary link count: `41`
- Final URL: `https://www.wto.org/`
- Fetch mode: `http`
- Request user agent: `web-listening-bot/1.0`
- Status code: `200`
- Word count: `176` (expected minimum `120`)
- Link count: `41`
- Source kind: `html`
- JS-heavy candidate: `no`
- JS markers: `scripts=31`
- Attempts tried: `1`
- Attempt `catalog` via `http`: status=`200` words=`176` links=`41` kind=`html` passed=`yes` reason=`content_ok`

