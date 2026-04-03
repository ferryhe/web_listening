# Agent Rescue Validation

- Generated at: `2026-04-03T19:15:00.741618+00:00`
- Catalog path: `C:\Project\web_listening\config\smoke_site_catalog.json`
- Sites checked: `37`
- Sites resolved by catalog-or-agent strategy: `35/37`
- Unresolved sites: `2`

| Site | Required | Resolved | Winning strategy | Attempts |
|---|---:|---:|---|---:|
| A2ii | no | yes | browser | 2 |
| IAIS | no | yes | catalog | 1 |
| IEA | yes | yes | catalog | 1 |
| IPCC | yes | yes | catalog | 1 |
| IRFF | yes | yes | catalog | 1 |
| ISSA | no | no | - | 4 |
| ISSB | yes | yes | catalog | 1 |
| OECD | no | yes | browser | 2 |
| PCAF | yes | yes | catalog | 1 |
| PSI | yes | yes | catalog | 1 |
| TNFD | no | yes | catalog | 1 |
| UNDP | no | yes | sitemap | 3 |
| FAO | yes | yes | catalog | 1 |
| UNEP | yes | yes | catalog | 1 |
| WEF | no | yes | browser | 2 |
| World Bank | yes | yes | catalog | 1 |
| ADB | yes | yes | catalog | 1 |
| AFDB | no | yes | browser | 2 |
| BCBS | yes | yes | catalog | 1 |
| BIS | yes | yes | catalog | 1 |
| CAF | no | yes | browser | 2 |
| FIT | yes | yes | catalog | 1 |
| FSB | yes | yes | catalog | 1 |
| G20 | yes | yes | catalog | 1 |
| GCA | yes | yes | catalog | 1 |
| IFAC | yes | yes | catalog | 1 |
| ILO | yes | yes | catalog | 1 |
| IMF | yes | yes | catalog | 1 |
| NGFS | yes | yes | catalog | 1 |
| SIF | no | no | - | 4 |
| UN Water | yes | yes | catalog | 1 |
| UNCTAD | yes | yes | catalog | 1 |
| UNFCCC | no | yes | sitemap | 3 |
| WHO | no | yes | catalog | 1 |
| WMO | no | yes | browser | 2 |
| WRI | yes | yes | catalog | 1 |
| WTO | yes | yes | catalog | 1 |

## Unresolved Sites

### ISSA

- Notes: `Homepage returned 403 from this environment on 2026-04-03.`
- `catalog` `http` `403` `http_403` `https://www.issa.int/`
- `browser` `browser` `403` `blocked_interstitial` `https://www.issa.int/`
- `sitemap` `http` `403` `http_403` `https://www.issa.int/sitemap.xml`
- `rss` `http` `403` `http_403` `https://www.issa.int/rss.xml`

### SIF

- Notes: `The domain currently resolves to a parked Linkila 404 page in this environment.`
- `catalog` `http` `None` `ConnectError` `https://www.sustainableinsuranceforum.org/`
- `browser` `browser` `None` `Error` `https://www.sustainableinsuranceforum.org/`
- `sitemap` `http` `None` `ConnectError` `https://www.sustainableinsuranceforum.org/sitemap.xml`
- `rss` `http` `None` `ConnectError` `https://www.sustainableinsuranceforum.org/rss.xml`

## Details

### A2ii

- Resolved: `yes`
- Winning strategy: `browser`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://a2ii.org/`
- Final URL: `https://www.cgap.org/topics/collections/access-to-insurance-initiative`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.cgap.org/topics/collections/access-to-insurance-initiative'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`200` words=`607` links=`73` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://a2ii.org/`
- Final URL: `https://www.cgap.org/topics/collections/access-to-insurance-initiative`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `Google Tag Manager (noscript) |  | End Google Tag Manager (noscript) |  | # Access to Insurance Initiative (A2ii) |  | - [About](https://www.cgap.org/topics/collections/access-to-insurance-initiative) | - [Innovation Lab](https://www.`
- Notes: `Official URL was supplemented. Requests from this environment redirected to a CGAP collection and returned 403 on 2026-04-03.`

### IAIS

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`602` links=`90` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.iais.org/`
- Final URL: `https://www.iais.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `START IAIS Home Slider REVOLUTION SLIDER 6.6.18 |  | ![](https://www.iais.org/mu-plugins/revslider/public/assets/assets/dummy.png) [Read for an overview of IAIS’ progress and achievements in 2025.](https://www.iais.org/2026/`
- Notes: `Homepage was reachable over HTTP but only exposed limited text in raw HTML; keep it flagged as a browser candidate.`

### IEA

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`547` links=`134` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://iea.org`
- Final URL: `https://www.iea.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `iOS Android Facebook / Open Graph globals Twitter globals IEA – International Energy Agency [if lt IE 9]> | <script src="https://cdnjs.cloudflare.com/ajax/libs/html5shiv/3.7.3/html5shiv.min.js"></script> | <![endif] [if IE]>`

### IPCC

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`875` links=`128` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.ipcc.ch/`
- Final URL: `https://www.ipcc.ch/`
- Request user agent: `web-listening-bot/1.0`
- Head: `[IPCC-64](https://www.ipcc.ch/meeting-doc/ipcc-64/) |  | [Vacancies](https://www.ipcc.ch/about/vacancies/) |  | ![](https://www.ipcc.ch/site/assets/themes/ipcc/resources/img/splash_logos.png) |  | Through its assessments, the IPCC d`

### IRFF

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`341` links=`97` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://irff.undp.org/`
- Final URL: `https://irff.undp.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `# Home |  | # Building financial resilience |  | We use insurance and risk finance to strengthen resilience, innovation and development. |  | [Read More](https://irff.undp.org/about-us) |  | Our | Mission In an era of rising uncertainty, `

### ISSA

- Resolved: `no`
- Winning strategy: `none`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.issa.int/`
- Final URL: `https://www.issa.int/`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.issa.int/'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`403` words=`35` links=`2` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt URL: `https://www.issa.int/`
- Final URL: `https://www.issa.int/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `![Icon for www.issa.int](https://www.issa.int/favicon.ico) |  | # www.issa.int |  | ## Performing security verification |  | This website uses a security service to protect against malicious bots. This page is displayed while the we`
- Attempt `sitemap` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.issa.int/sitemap.xml`
- Final URL: `https://www.issa.int/sitemap.xml`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.issa.int/sitemap.xml'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `rss` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.issa.int/rss.xml`
- Final URL: `https://www.issa.int/rss.xml`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.issa.int/rss.xml'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Notes: `Homepage returned 403 from this environment on 2026-04-03.`

### ISSB

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`432` links=`102` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.ifrs.org/news-and-events/`
- Final URL: `https://www.ifrs.org/news-and-events/`
- Request user agent: `web-listening-bot/1.0`
- Head: `Start of HubSpot Embed Code |  | include the clientlib |  | add the hs script dynamically if analytics are accepted |  | End of HubSpot Embed Code |  | Google Tag Manager (noscript) |  | End Google Tag Manager (noscript) |  | Add start-of-conte`
- Notes: `Smoke monitor points to the broader IFRS news page because the ISSB group page exposed almost no usable text over raw HTTP.`

### OECD

- Resolved: `yes`
- Winning strategy: `browser`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.oecd.org/`
- Final URL: `https://www.oecd.org/`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.oecd.org/'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`200` words=`965` links=`416` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.oecd.org/`
- Final URL: `https://www.oecd.org/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `# OECD |  | ## Top stories |  | [View all press releases](https://www.oecd.org/en/about/newsroom.html) |  | INTERIM ECONOMIC OUTLOOK |  | [The conflict in the Middle East is testing the resilience of the global economy](https://www.oecd`
- Notes: `Homepage and publications pages returned 403 from this environment on 2026-04-03.`

### PCAF

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`137` links=`52` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://carbonaccountingfinancials.com/`
- Final URL: `https://carbonaccountingfinancials.com/`
- Request user agent: `web-listening-bot/1.0`
- Head: `Menu |  | # Enabling financial institutions to assess and disclose greenhouse gas emissions associated with financial activities |  | ![](https://carbonaccountingfinancials.com/files/pageimages/2026update/CoverFlowchart-2026-01.`

### PSI

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`1135` links=`206` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.unepfi.org/insurance/`
- Final URL: `https://www.unepfi.org/insurance/`
- Request user agent: `web-listening-bot/1.0`
- Head: `# Insurance |  | Strengthening the insurance industry’s contribution to building resilient, inclusive and sustainable communities and economies |  | ### About |  | Launched at the 2012 UN Conference on Sustainable Development, the U`
- Notes: `Normalized the workbook URL to the canonical UNEP FI insurance landing page.`

### TNFD

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`3` links=`127` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://tnfd.global/news/`
- Final URL: `https://tnfd.global/news/`
- Request user agent: `web-listening-bot/1.0`
- Head: `[Skip to content](https://tnfd.global/news/#content)`
- Notes: `Homepage returned 403; the news page is reachable but still exposes very little server-rendered text.`

### UNDP

- Resolved: `yes`
- Winning strategy: `sitemap`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.undp.org`
- Final URL: `https://www.undp.org`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.undp.org'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`403` words=`15` links=`0` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt URL: `https://www.undp.org`
- Final URL: `https://www.undp.org/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `# Access Denied |  | You don't have permission to access "http://www.undp.org/" on this server. |  | Reference #18.9ec44543.1775243669.186d276 |  | https://errors.edgesuite.net/18.9ec44543.1775243669.186d276`
- Attempt `sitemap` via `http`: status=`200` words=`14` links=`7` kind=`xml_sitemap` passed=`yes` reason=`sitemap_inventory`
- Attempt URL: `https://www.undp.org/sitemap.xml`
- Final URL: `https://www.undp.org/sitemap.xml`
- Request user agent: `web-listening-bot/1.0`
- Head: `# Sitemap |  | - [https://www.undp.org/sitemap.xml?page=1](https://www.undp.org/sitemap.xml?page=1) (2026-03-31T08:00:06-04:00) |  | - [https://www.undp.org/sitemap.xml?page=2](https://www.undp.org/sitemap.xml?page=2) (2026-03-3`
- Notes: `Homepage and tested section pages returned 403 from this environment on 2026-04-03.`

### FAO

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`1468` links=`62` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.fao.org/home/en`
- Final URL: `https://www.fao.org/home/en`
- Request user agent: `web-listening-bot/1.0`
- Head: `Google Tag Manager (noscript) |  | End Google Tag Manager (noscript) |  | ##### Share |  | - [![facebook](https://www.fao.org/ResourcePackages/FAO/assets/dist/img/social-icons/social-icon-facebook.svg)](https://www.facebook.com/shar`

### UNEP

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`493` links=`110` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.unep.org`
- Final URL: `https://www.unep.org`
- Request user agent: `web-listening-bot/1.0`
- Head: `THEME DEBUG |  | THEME HOOK: 'page' |  | FILE NAME SUGGESTIONS: | ▪️ page--front.html.twig | ▪️ page--node.html.twig | ✅ page.html.twig |  | 💡 BEGIN CUSTOM TEMPLATE OUTPUT from 'themes/custom/UNEP_3Spot/templates/layout/page.html.twig' |  | S`

### WEF

- Resolved: `yes`
- Winning strategy: `browser`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.weforum.org`
- Final URL: `https://www.weforum.org`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.weforum.org'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`200` words=`9185` links=`131` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.weforum.org`
- Final URL: `https://www.weforum.org/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `![logo](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAC4AAAAuCAYAAABXuSs3AAAAAXNSR0IArs4c6QAAAARnQU1BAACxjwv8YQUAAAAJcEhZcwAADsMAAA7DAcdvqGQAAAdMSURBVGhD7ZdrcFXVFce3JhFM7vueewlJSCJEbMNDIKKiMmEMEsi9ASJGqa+gqEBQRsH4JEaFe/`
- Notes: `Homepage and tested section pages returned 403 from this environment on 2026-04-03.`

### World Bank

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`825` links=`52` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.worldbank.org/ext/en/home`
- Final URL: `https://www.worldbank.org/ext/en/home`
- Request user agent: `web-listening-bot/1.0`
- Head: `![](https://www.worldbank.org/ext/en/media_157d730311e72a3678dc971944fa544f20554c098.jpg?width=750&format=jpg&optimize=medium) |  | The World’s Waste Crisis Is Growing Fast |  | The world is generating waste faster than systems `
- Notes: `Smoke monitor uses the resolved homepage URL observed in the current environment.`

### ADB

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`1152` links=`293` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.adb.org/news`
- Final URL: `https://www.adb.org/news`
- Request user agent: `web-listening-bot/1.0`
- Head: `[![ADB Approves New Emergency Financing Option to Accelerate Crisis Response Across Asia and the Pacific](https://www.adb.org/sites/default/files/styles/large/public/content-media/956041-adb-2013-phi-adj-4689-1.jpg?itok=`
- Notes: `Smoke monitor uses the news page because it is much richer than the homepage over raw HTTP.`

### AFDB

- Resolved: `yes`
- Winning strategy: `browser`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://www.afdb.org/en`
- Final URL: `https://www.afdb.org/en`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://www.afdb.org/en'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`200` words=`317` links=`222` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.afdb.org/en`
- Final URL: `https://www.afdb.org/en`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `[![Africa's Economic Resilience Holds Firm Amid Global Headwinds, Says New AfDB Report](https://www.afdb.org/sites/default/files/a1-2026-meo-launch-photo_0.jpg)](https://www.afdb.org/en/news-and-events/press-releases/afr`
- Notes: `Homepage and news page returned 403 from this environment on 2026-04-03.`

### BCBS

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`121` links=`33` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.bis.org/bcbs/index.htm`
- Final URL: `https://www.bis.org/bcbs/index.htm`
- Request user agent: `web-listening-bot/1.0`
- Head: `[![The Bank for International Settlements](https://www.bis.org/img/bis-logo-short.gif)](https://www.bis.org/) |  | https://www.bis.org/bcbs/index.htm#accessibilityLinks |  | https://www.bis.org/bcbs/index.htm#center |  | https://www`

### BIS

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`186` links=`43` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.bis.org/index.htm`
- Final URL: `https://www.bis.org/index.htm`
- Request user agent: `web-listening-bot/1.0`
- Head: `[![The Bank for International Settlements](https://www.bis.org/img/bis-logo-short.gif)](https://www.bis.org/) |  | https://www.bis.org/index.htm#accessibilityLinks |  | https://www.bis.org/index.htm#center |  | https://www.bis.org/i`

### CAF

- Resolved: `yes`
- Winning strategy: `browser`
- Attempt `catalog` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt URL: `https://www.caf.com/en/`
- Final URL: `https://www.caf.com/en/`
- Error: `ConnectError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer certificate (_ssl.c:1006)`
- Attempt `browser` via `browser`: status=`200` words=`1184` links=`87` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.caf.com/en/`
- Final URL: `https://www.caf.com/en/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `![Previous slide](https://www.caf.com/css/design-2024/images/svg/chevron-left.svg) |  | ![Next slide](https://www.caf.com/css/design-2024/images/svg/chevron-right.svg) |  | ### [CAF Approves St. Vincent and the Grenadines as New`
- Notes: `Homepage currently fails certificate validation from this environment.`

### FIT

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`462` links=`51` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/`
- Final URL: `https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/`
- Request user agent: `web-listening-bot/1.0`
- Head: `<nav class="breadcrumb"><a href="https://www.unepfi.org/">Home</a><span class="divider">&nbsp;/&nbsp;</span><a class="nav-link" href="https://www.unepfi.org/forum-for-insurance-transition-to-net-zero/" title="Forum for I`

### FSB

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`499` links=`85` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.fsb.org/`
- Final URL: `https://www.fsb.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `![Annual Report 2025](https://www.fsb.org/uploads/ar-2025-1.jpg) |  | ## 24 March 2026 Promoting Global Financial Stability: 2025 FSB Annual Report |  | Against a backdrop of rising vulnerabilities, in 2025 the FSB delivered wor`

### G20

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`557` links=`217` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://g20.org`
- Final URL: `https://g20.org`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `⨉ |  | Help us improve |  | ![President Trump with fist in the air. Accompanied by G20 logo and text reading "the best is yet to come."](https://g20.org/wp-content/uploads/sites/259/2026/02/G20-Hero-Graphic-v2.jpg) |  | Modal Close `
- Notes: `Homepage returned 404 to the default bot UA but 200 to a browser-like UA in this environment.`

### GCA

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`597` links=`76` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://gca.org/`
- Final URL: `https://gca.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `![New organization banner (2)](https://gca.org/wp-content/uploads/2026/03/New-organization-banner-2.jpg) |  | ## New GCA Chair President Ameenah Gurib-Fakim to Advance Global Adaptation Agenda |  | [Explore more](https://gca.org`

### IFAC

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`414` links=`82` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.ifac.org/`
- Final URL: `https://www.ifac.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `- ![IFAC Member Value Proposition](https://www.ifac.org/sites/default/files/styles/1920x640/public/2026-01/Social%20Media%20Banner%20-%20IFAC%20MVP_Updated%20Jan%202026.png?itok=1CmGI9k1) | - The latest [IFAC Revises State`

### ILO

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`455` links=`64` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.ilo.org/`
- Final URL: `https://www.ilo.org/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `![AI illustration depicting people on a weighing scale](https://www.ilo.org/sites/default/files/styles/max_1440px_width/public/2026-03/hero-image-GenAI-and-gender-podcast-2048.png.webp?itok=DIPLDYJE) |  | Podcast |  | [How is ge`
- Notes: `Homepage returned 403 to the default bot UA but 200 to a browser-like UA in this environment.`

### IMF

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`636` links=`200` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.imf.org/en/news`
- Final URL: `https://www.imf.org/en/news`
- Request user agent: `web-listening-bot/1.0`
- Head: `$ |  | /$ |  | $? |  | #### Loading component... |  | /$ |  | $ |  | # News |  | /$ |  | $ |  | /$ |  | $ |  | English |  | [العربية](https://www.imf.org/ar/news) |  | [español](https://www.imf.org/es/News/SearchNews) |  | [français](https://www.imf.org/fr/News/SearchNews) |  | [`
- Notes: `Smoke monitor uses the news page because it exposes more stable text than the homepage.`

### NGFS

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`252` links=`53` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.ngfs.net/`
- Final URL: `https://www.ngfs.net/en`
- Request user agent: `web-listening-bot/1.0`
- Head: `Remove exposed search filter block from content. Cause it's decoupled ! |  | here the block title should not appear in FE in some blocks |  | However since BE guys wanted to keep trace of that, we though to apply d-none class |  | W`

### SIF

- Resolved: `no`
- Winning strategy: `none`
- Attempt `catalog` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt URL: `https://www.sustainableinsuranceforum.org/`
- Final URL: `https://www.sustainableinsuranceforum.org/`
- Error: `ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1006)`
- Attempt `browser` via `browser`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`Error`
- Attempt URL: `https://www.sustainableinsuranceforum.org/`
- Final URL: `https://www.sustainableinsuranceforum.org/`
- Error: `Error: Page.goto: net::ERR_SSL_PROTOCOL_ERROR at https://www.sustainableinsuranceforum.org/
Call log:
  - navigating to "https://www.sustainableinsuranceforum.org/", waiting until "domcontentloaded"
`
- Attempt `sitemap` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt URL: `https://www.sustainableinsuranceforum.org/sitemap.xml`
- Final URL: `https://www.sustainableinsuranceforum.org/sitemap.xml`
- Error: `ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1006)`
- Attempt `rss` via `http`: status=`None` words=`0` links=`0` kind=`error` passed=`no` reason=`ConnectError`
- Attempt URL: `https://www.sustainableinsuranceforum.org/rss.xml`
- Final URL: `https://www.sustainableinsuranceforum.org/rss.xml`
- Error: `ConnectError: [SSL: TLSV1_ALERT_INTERNAL_ERROR] tlsv1 alert internal error (_ssl.c:1006)`
- Notes: `The domain currently resolves to a parked Linkila 404 page in this environment.`

### UN Water

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`448` links=`84` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.unwater.org/`
- Final URL: `https://www.unwater.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `# Homepage |  | [Main content](https://www.unwater.org/#main-content) |  | [![A woman farmer watering a crop of beans](data:image/jpeg;base64,/9j/4AAQSkZJRgABAQEAYABgAAD//gA8Q1JFQVRPUjogZ2QtanBlZyB2MS4wICh1c2luZyBJSkcgSlBFRyB2Nj`

### UNCTAD

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`569` links=`218` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://unctad.org/`
- Final URL: `https://unctad.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `[![Share](https://unctad.org/themes/custom/newyork_b5/images/icons/icon_share.png)](https://unctad.org/) |  | [![Facebook](https://unctad.org/themes/custom/newyork_b5/images/icons/icon_facebook.png)](https://www.facebook.com`

### UNFCCC

- Resolved: `yes`
- Winning strategy: `sitemap`
- Attempt `catalog` via `http`: status=`200` words=`6` links=`0` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt URL: `https://unfccc.int/news`
- Final URL: `https://unfccc.int/news`
- Request user agent: `web-listening-bot/1.0`
- Head: `Request unsuccessful. Incapsula incident ID: 575000201446999431-780219863493707049`
- Attempt `browser` via `browser`: status=`200` words=`6` links=`0` kind=`html` passed=`no` reason=`blocked_interstitial`
- Attempt URL: `https://unfccc.int/news`
- Final URL: `https://unfccc.int/news`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `Request unsuccessful. Incapsula incident ID: 575000201446999431-1899582181571757365`
- Attempt `sitemap` via `http`: status=`200` words=`88` links=`44` kind=`xml_sitemap` passed=`yes` reason=`sitemap_inventory`
- Attempt URL: `https://unfccc.int/sitemap.xml`
- Final URL: `https://unfccc.int/sitemap.xml`
- Request user agent: `web-listening-bot/1.0`
- Head: `# Sitemap |  | - [https://unfccc.int/sitemap.xml?page=1](https://unfccc.int/sitemap.xml?page=1) (2026-04-01T03:40:01+02:00) |  | - [https://unfccc.int/sitemap.xml?page=2](https://unfccc.int/sitemap.xml?page=2) (2026-04-01T03:40:`
- Notes: `Smoke monitor uses the news page, which is reachable but still exposes almost no server-rendered text.`

### WHO

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`264` links=`237` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.who.int/news-room`
- Final URL: `https://www.who.int/news-room`
- Request user agent: `web-listening-bot/1.0`
- Head: `# Newsroom |  | ## Latest news from WHO |  | [All →](https://www.who.int/news) |  | [3 April 2026 Departmental update WHO’s head of emergencies reaffirms support to Lebanon’s health system amid escalating needs](https://www.who.int/`
- Notes: `Smoke monitor uses the news room, but the page still exposes very little server-rendered text.`

### WMO

- Resolved: `yes`
- Winning strategy: `browser`
- Attempt `catalog` via `http`: status=`403` words=`0` links=`0` kind=`error` passed=`no` reason=`http_403`
- Attempt URL: `https://wmo.int/`
- Final URL: `https://wmo.int/`
- Error: `HTTPStatusError: Client error '403 Forbidden' for url 'https://wmo.int/'
For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403`
- Attempt `browser` via `browser`: status=`200` words=`475` links=`85` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://wmo.int/`
- Final URL: `https://wmo.int/`
- Request user agent: `Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/135.0.0.0 Safari/537.36`
- Head: `Let us ensure that Earth information is not only collected — but also understood, accessible, and actionable for all. |  | Celeste Saulo, WMO Secretary-General |  | [Publication State of the Global Climate 2025![Numerous small b`
- Notes: `Homepage and news page returned 403 from this environment on 2026-04-03.`

### WRI

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`693` links=`103` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.wri.org/`
- Final URL: `https://www.wri.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `[![Home](https://www.wri.org/sites/default/files/nav_logo_white_0.svg)](https://www.wri.org/) |  | - [Donate](https://giving.wri.org/campaign/694046/donate) |  | [Click to see more](https://www.wri.org/#scroll) |  | What can we help`

### WTO

- Resolved: `yes`
- Winning strategy: `catalog`
- Attempt `catalog` via `http`: status=`200` words=`176` links=`41` kind=`html` passed=`yes` reason=`content_ok`
- Attempt URL: `https://www.wto.org/`
- Final URL: `https://www.wto.org/`
- Request user agent: `web-listening-bot/1.0`
- Head: `[![](https://www.wto.org/images/mc14/logobannerhome_e.png)](https://www.wto.org/english/thewto_e/minist_e/mc14_e/mc14_e.htm) |  | 26-29 |  | Mar |  | BARRE ICONES UNIFIEE |  | [![Calendar](https://www.wto.org/images/mc14/mc14-calendaric`

