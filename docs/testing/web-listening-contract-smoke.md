# Web Listening Contract Smoke

This smoke fixture documents the first contract-only handoff from `web_listening` to downstream modules.

## Fixture

- Contract: [`docs/contracts/web-listening-manifest-v1.md`](../contracts/web-listening-manifest-v1.md)
- Sample manifest: [`docs/testing/fixtures/web-listening-manifest-v1.sample.json`](fixtures/web-listening-manifest-v1.sample.json)

## Smoke intent

A downstream agent or CLI should be able to:

1. Parse the sample as JSON.
2. Assert `schema_version == "web-listening-manifest.v1"`.
3. Resolve artifact and asset paths relative to the manifest location plus `artifact_root`.
4. Read `downloaded_assets[]` as the preferred handoff list for `doc_to_md`.
5. Preserve `asset_id`, `source_item_id`, `checksum`, and `provenance` into its own Markdown corpus manifest.

Important boundary:

- The sample fixture represents the planned `web-listening-manifest.v1` artifact body.
- The current CLI command `web-listening export-manifest --json` still emits `job_delivery.v1` about the existing YAML/Markdown outputs.
- Runtime export work should keep those two layers distinct unless and until the CLI contract is explicitly changed.

## Minimal local check

```bash
python -m json.tool docs/testing/fixtures/web-listening-manifest-v1.sample.json >/tmp/web-listening-manifest-v1.sample.pretty.json
python -m pytest tests/test_manifest_contract_fixture.py -q
```

This verifies only that the fixture is parseable JSON. Runtime export support will need focused CLI tests once `web-listening export-manifest --json` emits this v1 envelope.

## PR-ready validation note

Before merging a PR that changes runtime export behavior, follow the repo validation policy:

```bash
python -m pytest tests -q
python tools/validate_real_sites.py
python tools/run_dev_regression.py --report-only
python tools/run_smoke_site_catalog.py --report-only
python tools/run_tree_catalog_validation.py
```

For this contract-only smoke, the full live catalog validation is intentionally deferred because no crawler, workflow, API, storage, or CLI runtime behavior is changed.
