# Required Dev Test Targets

> Branch: `docs/ai-agent-roadmap`  
> Last updated: 2026-04-03

## Required live target set

Every live development validation in this repo should include these three public sites:

- `SOA` -> `https://www.soa.org/` and `https://www.soa.org/publications/publications-landing/`
- `CAS` -> `https://www.casact.org/` and `https://www.casact.org/about/governance/annual-reports`
- `IAA` -> `https://actuaries.org/` and `https://actuaries.org/annual-reports/`

The canonical source of truth is `config/dev_test_sites.json`.
Do not hardcode a different target list in ad-hoc scripts.

## What every live regression must verify

### 1. Monitoring capture

- the monitor page returns `200`
- the snapshot stores a stable `final_url`
- the normalized snapshot reaches a minimum word count threshold
- the snapshot records which artifact produced the page hash, currently `fit_markdown`

### 2. Change comparison

- two immediate repeat fetches produce the same content hash
- the repeat fetch does not create a diff snippet
- whitespace-only or blank-line-only changes should not flip the page SHA-256

### 3. Document discovery

- the document page returns `200`
- the document page reaches a minimum word count threshold
- the document page exposes at least the configured minimum number of document links

### 4. Download validation

- at least one sample document downloads successfully per site
- the sample download has a non-zero file size
- the download SHA-256 is a valid 64-character lowercase hex string
- a repeat download of the same URL resolves to the same SHA-256 and blob path

## Current baseline on 2026-04-03

| Site | Monitor hash stable | Repeat diff | Doc links | Sample document SHA-256 |
|---|---:|---:|---:|---|
| `SOA` | yes | no | 2 | `ac837ba89e0d4653babafd4ab41f7026ee230dc19da5e36cc18613accee5b42c` |
| `CAS` | yes | no | 14 | `56cd4941e5202e4e32a57b09ddd994db821675833cc6f899de7486647c32bfe2` |
| `IAA` | yes | no | 16 | `cc7d57ae9b69b408f0be08301371e8786739a48396f1b0e30d25d94c84c5903d` |

## Additional recommended test content

- Add one optional JS-heavy public target later to validate browser mode without making Playwright a hard dependency for every developer.
- Add a redirect-heavy target to confirm `final_url` and canonical URL handling stay stable.
- Add at least one non-PDF downloadable document type such as `docx` or `xlsx`.
- Add a noise-focused fixture for timestamps, counters, or banner rotations so false-positive page changes stay under control.
- Add an agent handoff check that confirms downloaded documents can move into `content_md_status=pending -> converted` cleanly.

## SHA-256 policy

### Page snapshots

Page change hashes should be computed from the best normalized content artifact, in this order:

1. `fit_markdown`
2. `markdown`
3. `content_text`

Before hashing, the content should be canonicalized by:

- converting line endings to `\n`
- collapsing repeated spaces and tabs inside each line
- trimming leading and trailing whitespace
- collapsing repeated blank lines to a single blank separator

The current metadata marker is `hash_normalization=whitespace-normalized-v1`.

### Downloaded files

Downloaded documents should keep byte-level SHA-256 hashes.
These hashes should be computed from the raw file bytes, not from extracted text.
Blob dedupe should stay keyed by SHA-256 so repeated downloads of the same file land on the same canonical path.

## Commands

```powershell
.venv\Scripts\python -m pytest tests -q
.venv\Scripts\python tools\validate_real_sites.py
.venv\Scripts\python tools\run_dev_regression.py
```

If you only want the Markdown output without failing the shell on live regression issues:

```powershell
.venv\Scripts\python tools\run_dev_regression.py --report-only
```
