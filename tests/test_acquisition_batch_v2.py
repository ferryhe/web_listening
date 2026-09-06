import builtins
from copy import deepcopy
import json
import socket
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from web_listening.contracts import acquisition_batch as batch


def item(key="a", state="updated"):
    return dict(
        task_id=key,
        site_key=key,
        requested_url=f"https://{key}.example/",
        disposition=state,
        reason="scope.completed",
        artifact_id=f"manifest-{key}-1" if state in {"updated", "unchanged"} else None,
    )


def build(items=None, **kwargs):
    return batch.build_acquisition_batch_result_v2(items or [item()], **kwargs)


@pytest.mark.parametrize(
    "state", ["updated", "unchanged", "blocked", "failed", "unresolved"]
)
def test_v2_counts_conservation(state):
    payload = build([item(str(i), state) for i in range(3)])
    assert payload["counts"]["requested"] == payload["counts"][state] == 3
    payload["counts"]["requested"] += 1
    with pytest.raises(ValueError):
        batch.AcquisitionBatchResultV2.model_validate_json(json.dumps(payload))


def test_v2_unique_task_id_required():
    with pytest.raises(ValueError, match="unique"):
        build([item(), item()])


def test_v2_artifact_id_only_for_success():
    with pytest.raises(ValueError, match="artifact"):
        build([{**item(state="blocked"), "artifact_id": "bad"}])
    with pytest.raises(ValueError, match="artifact"):
        build([{**item(), "artifact_id": None}])


def test_v2_optional_succeeded_matches_derived():
    payload = build()
    assert payload["counts"]["succeeded"] == 1
    payload["counts"]["succeeded"] = 2
    with pytest.raises(ValueError):
        batch.AcquisitionBatchResultV2.model_validate_json(json.dumps(payload))
    del payload["counts"]["succeeded"]
    batch.AcquisitionBatchResultV2.model_validate_json(json.dumps(payload))


def test_aggregate_pure_no_io(monkeypatch):
    from web_listening.blocks.storage import Storage

    inputs = [build()]
    original = deepcopy(inputs)

    def forbidden(*args, **kwargs):
        pytest.fail("aggregator performed I/O")

    with monkeypatch.context() as patch:
        patch.setattr(Storage, "__init__", forbidden)
        patch.setattr(builtins, "open", forbidden)
        patch.setattr(socket, "socket", forbidden)
        result = batch.aggregate_batch_result_v2(inputs)
    assert inputs == original
    assert result["counts"]["requested"] == 1


def test_aggregate_order_independent():
    inputs = [build([item("z")]), build([item("a", "blocked")])]
    assert batch.aggregate_batch_result_v2(inputs) == batch.aggregate_batch_result_v2(
        inputs[::-1]
    )
    assert [
        x["site_key"] for x in batch.aggregate_batch_result_v2(inputs)["dispositions"]
    ] == ["a", "z"]


def test_aggregate_duplicate_equivalence():
    a = build([item()], valid_snapshots=2)
    assert batch.aggregate_batch_result_v2([a, a]) == batch.aggregate_batch_result_v2(
        [a]
    )


def test_aggregate_conflict_raises():
    with pytest.raises(ValueError, match="conflict"):
        batch.aggregate_batch_result_v2(
            [build(), build([{**item(), "requested_url": "https://other.example/"}])]
        )


def test_v1_still_works():
    result = batch.acquisition_batch_result_from_scope_run(
        SimpleNamespace(
            site_key="demo",
            seed_url="https://example.com/",
            run_id=1,
            status="completed",
            pages_seen=1,
            files_seen=0,
            page_failures=0,
            file_failures=0,
        )
    )
    assert batch.AcquisitionBatchResult.model_validate_json(
        json.dumps(result)
    ).full_success
    assert batch.ACQUISITION_BATCH_RESULT_VERSION == "acquisition-batch-result.v1"


@pytest.mark.parametrize(
    "status,seen,changed,classification,expected",
    [
        ("completed", 1, 1, None, "updated"),
        ("completed", 1, 0, None, "unchanged"),
        ("completed", 0, 0, None, "failed"),
        ("failed", 0, 0, "http_403", "blocked"),
        ("failed", 0, 0, "executor_error", "failed"),
        ("running", 0, 0, None, "unresolved"),
        ("cancelled", 1, 1, None, "unresolved"),
        ("failed", 2, 1, None, "failed"),
    ],
)
def test_real_terminal_projection(status, seen, changed, classification, expected):
    from web_listening.models import CrawlRun

    run = CrawlRun(
        id=1, scope_id=1, status=status, pages_seen=seen, pages_changed=changed
    )
    result = batch.acquisition_batch_result_v2_from_scope_run(
        run,
        site_key="a",
        requested_url="https://a.example/",
        artifact_id="manifest-a-1",
        classification=classification,
    )
    assert result["dispositions"][0]["disposition"] == expected


def test_aggregate_cli(tmp_path, monkeypatch):
    from web_listening.cli import app
    import web_listening.cli as cli

    monkeypatch.setattr(cli, "_get_storage", lambda: pytest.fail("CLI opened Storage"))
    path = tmp_path / "input.json"
    data = build()
    path.write_text(json.dumps(data))
    runner = CliRunner()
    result = runner.invoke(
        app, ["aggregate-batch-result", "--input", str(path), "--json"]
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == batch.aggregate_batch_result_v2([data])
    assert (
        runner.invoke(
            app, ["aggregate-batch-result", "--input", str(path), "--json"]
        ).output
        == result.output
    )
    human = runner.invoke(app, ["aggregate-batch-result", "--input", str(path)])
    assert human.exit_code == 0 and "checked=1" in human.output
    path.write_text(
        json.dumps(
            [data, build([{**item(), "requested_url": "https://other.example/"}])]
        )
    )
    assert (
        runner.invoke(
            app, ["aggregate-batch-result", "--input", str(path), "--json"]
        ).exit_code
        == 2
    )
    data["dispositions"].append(data["dispositions"][0])
    path.write_text(json.dumps(data))
    assert (
        runner.invoke(app, ["aggregate-batch-result", "--input", str(path)]).exit_code
        == 2
    )


def test_aggregate_conflicting_authority_is_not_order_dependent():
    a = build()
    b = build(authoritative_status="failed")
    for records in ([a, b], [b, a]):
        with pytest.raises(ValueError, match="conflict"):
            batch.aggregate_batch_result_v2(records)


def test_v2_public_exports_and_schema():
    from web_listening.contracts import (
        AcquisitionBatchResultV2,
        AcquisitionBatchCountsV2,
        AcquisitionDispositionV2,
    )

    assert (
        AcquisitionBatchResultV2.model_json_schema()["properties"]["schema_version"][
            "const"
        ]
        == "acquisition-batch-result.v2"
    )
    assert AcquisitionBatchCountsV2 and AcquisitionDispositionV2


def test_aggregate_optional_succeeded_is_equivalent():
    explicit = build()
    implicit = deepcopy(explicit)
    del implicit["counts"]["succeeded"]
    assert batch.aggregate_batch_result_v2(
        [explicit, implicit]
    ) == batch.aggregate_batch_result_v2([explicit])
