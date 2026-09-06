from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from web_listening.blocks.document_manifest import build_web_listening_manifest_v1
from web_listening.blocks.storage import Storage
from web_listening.models import (
    CrawlRun,
    CrawlScope,
    PageSnapshot,
    Site,
    FileObservation,
)

NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)
ROOT = "https://example.com/"


@pytest.fixture
def history(tmp_path):
    storage = Storage(tmp_path / "history.db")
    site = storage.add_site(Site(url=ROOT, name="Demo"))
    scope = storage.add_crawl_scope(
        CrawlScope(
            site_id=site.id,
            seed_url=ROOT,
            allowed_origin=ROOT.rstrip("/"),
            allowed_page_prefixes=["/"],
            allowed_file_prefixes=["/"],
        )
    )
    path = tmp_path / "monitor_scope.yaml"
    path.write_text(
        yaml.safe_dump(
            dict(
                scope_id=scope.id,
                site_key="demo",
                seed_url=ROOT,
                homepage_url=ROOT,
                display_name="Demo",
                catalog="test",
                allowed_page_prefixes=["/"],
                allowed_file_prefixes=["/"],
            )
        )
    )
    runs = []

    def add(links, *, file_url=None, page_url=ROOT, content_hash=None):
        run = storage.add_crawl_run(
            CrawlRun(
                scope_id=scope.id,
                status="completed",
                run_type="incremental" if runs else "bootstrap",
                started_at=NOW,
                finished_at=NOW,
                pages_seen=1,
            )
        )
        page = storage.upsert_tracked_page(
            scope_id=scope.id, canonical_url=page_url, depth=0, run_id=run.id
        )
        attempt = storage.add_legacy_compatibility_attempt(
            scope_id=scope.id, run_id=run.id, identity=page_url
        )
        snapshot = storage.add_page_snapshot(
            PageSnapshot(
                scope_id=scope.id,
                page_id=page.id,
                run_id=run.id,
                attempt_id=attempt.attempt_id,
                captured_at=NOW,
                content_hash=content_hash or str(run.id),
                final_url=page_url,
                links=links,
            )
        )
        storage.upsert_tracked_page(
            scope_id=scope.id,
            canonical_url=page_url,
            depth=0,
            run_id=run.id,
            latest_snapshot_id=snapshot.id,
            latest_hash=snapshot.content_hash,
        )
        if file_url:
            file = storage.upsert_tracked_file(
                scope_id=scope.id, canonical_url=file_url, run_id=run.id
            )
            attempt = storage.add_legacy_compatibility_attempt(
                scope_id=scope.id,
                run_id=run.id,
                identity=file_url,
                content_kind="document",
            )
            storage.add_file_observation(
                FileObservation(
                    scope_id=scope.id,
                    run_id=run.id,
                    page_id=page.id,
                    file_id=file.id,
                    attempt_id=attempt.attempt_id,
                    discovered_url=file_url,
                    download_url=file_url,
                )
            )
        storage.update_crawl_scope(
            scope.model_copy(update={"baseline_run_id": run.id, "is_initialized": True})
        )
        runs.append(run)
        return run

    def export(run):
        return build_web_listening_manifest_v1(
            path, storage=storage, run_id=run.id, generated_at=NOW
        )

    yield add, export, storage
    storage.close()


def pages(payload):
    return {
        item["url"]: item
        for item in payload["discovered_items"]
        if item["item_type"] == "page_link"
    }


def test_producer_emits_page_link_items(history):
    add, export, _ = history
    result = pages(export(add([ROOT + "a", ROOT + "b", ROOT + "c"])))
    assert set(result) == {ROOT + "a", ROOT + "b", ROOT + "c"}
    assert result[ROOT + "a"]["discovered_from"] == ROOT
    assert result[ROOT + "a"]["metadata"]["source_run_id"] == 1


def test_producer_run_bound_to_specified_run(history):
    add, export, _ = history
    first = add([ROOT + "a"])
    second = add([ROOT + "b"])
    assert set(pages(export(second))) == {ROOT + "a", ROOT + "b"}
    assert set(pages(export(first))) == {ROOT + "a"}


def test_producer_first_bootstrap_items_are_existing(history):
    add, export, _ = history
    result = export(add([ROOT + "a"], file_url=ROOT + "a.pdf"))["discovered_items"]
    assert len(result) == 2
    assert all(item["status"] == item["item_state"] == "existing" for item in result)


def test_producer_increment_new_items_have_new_status(history):
    add, export, _ = history
    add([ROOT + "old", ROOT + "keep"])
    result = pages(export(add([ROOT + "new", ROOT + "keep"])))
    assert result[ROOT + "new"]["status"] == "new"
    assert result[ROOT + "old"]["status"] == "missing"
    assert result[ROOT + "keep"]["status"] == "existing"


def test_producer_does_not_recompute_from_latest_snapshot(history, monkeypatch):
    add, export, storage = history
    add([ROOT + "old"])
    migration = add([ROOT + "old", ROOT + "this-week"])
    expected = export(migration)
    add([ROOT + "later"])
    monkeypatch.setattr(
        storage, "get_latest_snapshot", lambda *a: pytest.fail("latest snapshot used")
    )
    result = export(migration)
    assert pages(result) == pages(expected)
    assert pages(result)[ROOT + "this-week"]["item_state"] == "new"


def test_producer_removed_not_promoted_to_new(history):
    add, export, _ = history
    add([ROOT + "old"])
    absent = add([])
    add([ROOT + "old"])
    assert pages(export(absent))[ROOT + "old"]["status"] == "missing"


def test_producer_does_not_fabricate_title(history):
    add, export, _ = history
    result = export(add([ROOT + "a"], file_url=ROOT + "a.pdf"))["discovered_items"]
    assert len(result) == 2
    assert all(item["title"] is None for item in result)


def test_producer_changed_target_and_file_dedup(history):
    add, export, storage = history
    first = add([ROOT + "a", ROOT + "a.pdf"], file_url=ROOT + "a.pdf")
    second = add([ROOT + "a", ROOT + "a.pdf"], file_url=ROOT + "a.pdf")
    # A fetched link has its own hash evidence, unlike its referring list page.
    page = storage.upsert_tracked_page(
        scope_id=1, canonical_url=ROOT + "a", depth=1, run_id=first.id
    )
    for run, value in [(first, "before"), (second, "after")]:
        attempt = storage.add_legacy_compatibility_attempt(
            scope_id=1, run_id=run.id, identity=ROOT + "a"
        )
        storage.add_page_snapshot(
            PageSnapshot(
                scope_id=1,
                page_id=page.id,
                run_id=run.id,
                attempt_id=attempt.attempt_id,
                captured_at=NOW,
                content_hash=value,
                final_url=ROOT + "a",
            )
        )
        storage.upsert_tracked_page(
            scope_id=1,
            canonical_url=ROOT + "a",
            depth=1,
            run_id=run.id,
            latest_hash=value,
        )
    result = export(second)
    assert pages(result)[ROOT + "a"]["item_state"] == "changed"
    assert (
        len([x for x in result["discovered_items"] if x["url"] == ROOT + "a.pdf"]) == 1
    )


def test_producer_rejects_foreign_run(history):
    add, export, storage = history
    add([ROOT + "a"])
    foreign = storage.add_crawl_run(CrawlRun(scope_id=999, status="completed"))
    with pytest.raises(ValueError, match="scope"):
        export(foreign)


def test_file_without_document_never_borrows_later_document(history):
    from web_listening.models import Document

    add, export, storage = history
    first = add([], file_url=ROOT + "a.pdf")
    before = export(first)
    later = add([], file_url=ROOT + "a.pdf")
    doc = storage.add_document(
        Document(
            site_id=1,
            title="Later body",
            url=ROOT + "a.pdf",
            download_url=ROOT + "a.pdf",
            sha256="a" * 64,
            local_path="absent.pdf",
        )
    )
    storage.upsert_tracked_file(
        scope_id=1,
        canonical_url=ROOT + "a.pdf",
        run_id=later.id,
        latest_document_id=doc.id,
        latest_sha256=doc.sha256,
    )
    after = export(first)
    assert before["discovered_items"] == after["discovered_items"]
    assert after["downloaded_assets"] == []
    assert after["deprecated"]["scope_document_manifest"]["documents"] == []


def test_failed_unvisited_page_is_not_missing(history):
    add, export, storage = history
    add([ROOT + "keep"])
    run = storage.add_crawl_run(CrawlRun(scope_id=1, status="failed"))
    assert export(run)["discovered_items"] == []


def test_moved_link_is_not_missing_or_new(history):
    add, export, storage = history
    add([ROOT + "move"])
    run = add([], page_url=ROOT)
    page = storage.upsert_tracked_page(
        scope_id=1, canonical_url=ROOT + "other-list", depth=1, run_id=run.id
    )
    attempt = storage.add_legacy_compatibility_attempt(
        scope_id=1, run_id=run.id, identity=page.canonical_url
    )
    storage.add_page_snapshot(
        PageSnapshot(
            scope_id=1,
            page_id=page.id,
            run_id=run.id,
            attempt_id=attempt.attempt_id,
            content_hash="other",
            links=[ROOT + "move"],
        )
    )
    result = pages(export(run))
    assert result[ROOT + "move"]["status"] == "existing"


def test_file_absent_from_observed_page_is_missing(history):
    add, export, _ = history
    add([], file_url=ROOT + "old.pdf")
    absent = add([])
    add([], file_url=ROOT + "old.pdf")
    result = export(absent)["discovered_items"]
    assert len(result) == 1
    assert result[0]["url"] == ROOT + "old.pdf"
    assert result[0]["status"] == result[0]["item_state"] == "missing"
    assert export(absent)["downloaded_assets"] == []


def test_file_changed_and_historical_assets_use_capture_digest(history, tmp_path):
    import hashlib
    import json
    from tests.test_acquisition_lineage import _attempt
    from web_listening.models import Document

    add, export, storage = history
    runs = []
    hashes = []
    for index, body in enumerate(("original", "changed", "later")):
        run = add([])
        digest = hashlib.sha256(body.encode()).hexdigest()
        blob_path = tmp_path / f"{digest}.pdf"
        blob_path.write_bytes(body.encode())
        storage.upsert_blob(
            sha256=digest,
            canonical_path=str(blob_path),
            file_size=len(body),
            content_type="application/pdf",
        )
        doc = storage.add_document(
            Document(
                site_id=1,
                title=body,
                url=ROOT + "a.pdf",
                download_url=ROOT + "a.pdf",
                sha256=digest,
                local_path=str(blob_path),
                downloaded_at=NOW,
            )
        )
        file = storage.upsert_tracked_file(
            scope_id=1,
            canonical_url=ROOT + "a.pdf",
            run_id=run.id,
            latest_document_id=doc.id,
            latest_sha256=digest,
        )
        attempt = _attempt(
            attempt_id=f"file-{index}",
            request_id=f"file-{index}",
            run_id=run.id,
            content_kind="document",
            requested_url=ROOT + "a.pdf",
            final_url=ROOT + "a.pdf",
            requested_at=NOW,
            started_at=NOW,
            finished_at=NOW,
        )
        payload = json.loads(attempt.canonical_json)
        payload["result"]["content"] = dict(
            media_type="application/pdf", text=body, sha256=digest, metadata={}
        )
        attempt.canonical_json = json.dumps(payload)
        storage.add_acquisition_attempt(attempt)
        storage.add_file_observation(
            FileObservation(
                scope_id=1,
                run_id=run.id,
                page_id=1,
                file_id=file.id,
                document_id=doc.id,
                attempt_id=attempt.attempt_id,
                discovered_url=ROOT + "a.pdf",
                download_url=ROOT + "a.pdf",
            )
        )
        runs.append(run)
        hashes.append(digest)
    assert export(runs[0])["downloaded_assets"][0]["checksum"]["value"] == hashes[0]
    assert export(runs[1])["downloaded_assets"][0]["checksum"]["value"] == hashes[1]
    item = export(runs[1])["discovered_items"][0]
    assert item["status"] == "changed"
    assert item["checksum"]["value"] == hashes[1]
    assert (
        export(runs[0])["deprecated"]["scope_document_manifest"]["documents"][0][
            "sha256"
        ]
        == hashes[0]
    )


def test_previously_discovered_file_is_not_new_when_first_observed(history):
    add, export, _ = history
    add([ROOT + "known.pdf"])
    run = add([ROOT + "known.pdf"], file_url=ROOT + "known.pdf")
    item = export(run)["discovered_items"][0]
    assert item["item_state"] == "existing"
