"""Synthesized regression evidence, not current production measurements."""

import json
from pathlib import Path

from web_listening.contracts import acquisition_batch as batch
from web_listening.models import CrawlRun
from tests.test_discovered_items_producer import history, ROOT

FIXTURES = Path(__file__).parent / "fixtures"


def regression_evidence():
    evidence = []
    for label, count, status, changed, seen, classification in [
        ("updated", 33, "completed", 2, 3, None),
        ("unchanged", 9, "completed", 0, 3, None),
        ("blocked", 14, "failed", 0, 0, "http_403"),
        ("failed", 1, "failed", 0, 0, "executor_error"),
    ]:
        for index in range(count):
            key = f"{label}-{index:02}"
            evidence.append(
                dict(
                    site_key=key,
                    requested_url=f"https://{key}.example/",
                    artifact_id=f"manifest-{key}-1",
                    classification=classification,
                    run=CrawlRun(
                        id=len(evidence) + 1,
                        scope_id=len(evidence) + 1,
                        status=status,
                        pages_seen=seen,
                        pages_changed=changed,
                    ).model_dump(mode="json"),
                )
            )
    return evidence


def project(evidence):
    return [
        batch.acquisition_batch_result_v2_from_scope_run(
            CrawlRun(**entry["run"]), **{k: v for k, v in entry.items() if k != "run"}
        )
        for entry in evidence
    ]


def test_v2_57_aggregate_from_real_evidence():
    evidence = regression_evidence()
    result = batch.aggregate_batch_result_v2(project(evidence))
    counts = result["counts"]
    assert [
        counts[k] for k in ("updated", "unchanged", "blocked", "failed", "unresolved")
    ] == [33, 9, 14, 1, 0]
    assert result["summary"] == {"checked": 57, "succeeded": 42, "failed": 15}
    fixture = json.loads((FIXTURES / "acquisition_batch_v2_57_sample.json").read_text())
    assert "not current production" in fixture["comment"]
    assert fixture["evidence"] == evidence
    assert fixture["result"] == result


def read_manifest_items(payload):
    """Minimal public-shape consumer; no climate implementation is vendored here."""
    assert payload["schema_version"] == "web-listening-manifest.v1"
    return [
        {
            key: item[key]
            for key in ("item_id", "item_type", "url", "title", "item_state")
        }
        for item in payload["discovered_items"]
    ]


def test_v2_fixture_consumable_by_minimal_reader(history):
    add, export, _ = history
    payload = export(
        add([ROOT + "a", ROOT + "b", ROOT + "c"], file_url=ROOT + "report.pdf")
    )
    items = read_manifest_items(json.loads(json.dumps(payload)))
    assert len(items) == payload["status"]["counts"]["discovered_items"] == 4
    assert {item["item_type"] for item in items} == {"page_link", "file_link"}
    fixture = json.loads(
        (FIXTURES / "web_listening_manifest_page_link_sample.json").read_text()
    )
    assert read_manifest_items(fixture) == items
    counts = batch.aggregate_batch_result_v2(project(regression_evidence()))["counts"]
    assert counts["requested"] == sum(
        counts[k] for k in ("updated", "unchanged", "blocked", "failed", "unresolved")
    )
