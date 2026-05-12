# Web Listening Contract Smoke

This smoke fixture documents the handoff from `web_listening` to downstream modules.

## Acquisition picker contract

- Contract: [`docs/contracts/acquisition-tools-v1.md`](../contracts/acquisition-tools-v1.md)
- Sample catalog: [`docs/testing/fixtures/acquisition-tools-v1.sample.json`](fixtures/acquisition-tools-v1.sample.json)
- Runtime API: `GET /api/v1/acquisition/tools`
- Runtime CLI: `web-listening list-acquisition-tools --json`

Delivery UIs and agents should treat the API/CLI catalog as the acquisition tool picker contract. The contract maps ordinary public HTML to `web_http`, dynamic JavaScript pages to `browser_rendered`, authorized stealth-browser/CDP-like contexts to `cloakbrowser`, bulk structured/site-specific scrape jobs to reserved `batch_python`, and discovery/feed cases to reserved `sitemap` or `rss`. The catalog is planning/probing metadata and does not change `bootstrap-scope` or `run-scope` execution.

## Fixture and runtime artifact

- Contract: [`docs/contracts/web-listening-manifest-v1.md`](../contracts/web-listening-manifest-v1.md)
- Sample manifest: [`docs/testing/fixtures/web-listening-manifest-v1.sample.json`](fixtures/web-listening-manifest-v1.sample.json)
- Runtime producer: `web-listening export-manifest`, which writes `web_listening_manifest_<site>_<date>.json` alongside compatibility YAML/Markdown files.

## Smoke intent

A downstream agent or CLI should be able to:

1. Parse the sample or runtime JSON manifest.
2. Assert `schema_version == "web-listening-manifest.v1"`.
3. Resolve artifact and asset paths relative to the manifest location plus `artifact_root`.
4. Read `downloaded_assets[]` as the preferred handoff list for `doc_to_md`.
5. Preserve `asset_id`, `source_item_id`, `checksum`, and `provenance` into its own Markdown corpus manifest.

Important boundary:

- The v1 JSON file is the stable downstream handoff artifact body.
- The CLI command `web-listening export-manifest --json` still emits the wrapper `job_delivery.v1` for automation that wants job status and artifact pointers.
- Runtime export keeps those two layers distinct: downstream conversion tools should read the JSON manifest file, while operator/console tooling may read the `job_delivery.v1` wrapper.

## Minimal local check

```bash
python -m json.tool docs/testing/fixtures/acquisition-tools-v1.sample.json >/tmp/acquisition-tools-v1.sample.pretty.json
python -m json.tool docs/testing/fixtures/web-listening-manifest-v1.sample.json >/tmp/web-listening-manifest-v1.sample.pretty.json
python -m pytest tests/test_acquisition_tools_contract_fixture.py tests/test_manifest_contract_fixture.py tests/test_document_manifest.py tests/test_cli.py -q
```

Together these checks verify that the fixtures are parseable JSON, that the runtime acquisition catalog matches the stable picker surface, that runtime manifest generation preserves the expected contract shape, and that the CLI continues to expose `job_delivery.v1` while pointing at the v1 JSON artifact.

## PR-ready validation note

Before merging a PR that changes runtime export behavior, follow the repo validation policy:

```bash
python -m pytest tests -q
python tools/validate_real_sites.py
python tools/run_dev_regression.py --report-only
python tools/run_smoke_site_catalog.py --report-only
python tools/run_tree_catalog_validation.py
```

If live target requests time out, rerun the live commands with `WL_REQUEST_TIMEOUT=120` to collect complete reports before classifying failures as regressions.
