import json
from pathlib import Path


FIXTURE_PATH = Path("docs/testing/fixtures/web-listening-manifest-v1.sample.json")


def test_web_listening_manifest_v1_sample_fixture_has_expected_contract_shape():
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "web-listening-manifest.v1"
    assert payload["producer"]["contract_version"] == "web-listening-manifest.v1"
    assert payload["artifact_root"] == "."
    assert payload["status"]["stage"] == "export_manifest"

    reports = payload["artifacts"]["reports"]
    structured_exports = payload["artifacts"]["structured_exports"]
    compatibility_exports = payload["artifacts"]["compatibility_exports"]
    downloaded_assets = payload["downloaded_assets"]

    assert any(item["kind"] == "tracking_report_md" for item in reports)
    assert any(item["kind"] == "tracking_report_yaml" for item in structured_exports)
    assert any(item["kind"] == "document_manifest_yaml" for item in compatibility_exports)
    assert downloaded_assets

    asset = downloaded_assets[0]
    assert asset["local_path"] == asset["tracked_path"]
    assert asset["canonical_blob_path"].startswith("data/downloads/_blobs/")
    assert asset["tracked_path"].startswith("data/downloads/_tracked/")
    assert asset["source_item_id"] in {item["item_id"] for item in payload["discovered_items"]}
