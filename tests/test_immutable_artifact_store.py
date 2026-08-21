from __future__ import annotations

import gzip
import hashlib
import json
import os
import secrets
import sqlite3
import tempfile
import threading
import zlib
from dataclasses import replace
from pathlib import Path

import pytest

from web_listening.blocks import immutable_artifacts as immutable_artifacts_module
from web_listening.blocks.acquisition_contract import (
    MAX_PORTABLE_JSON_INTEGER,
    artifact_id_for_identity,
)
from web_listening.blocks.immutable_artifacts import (
    ArtifactStore,
    ArtifactStoreError,
    artifact_lineage_id,
    artifact_version_id,
)
from web_listening.blocks.storage import Storage
from web_listening.models import Document


RUN_ID = "source-run-20260821-immutable-001"
RETRIEVED_AT = "2026-08-21T12:00:00Z"
HTML = b"<!doctype html><html><body>original source</body></html>"
PDF = b"%PDF-1.7\n1 0 obj\n<<>>\nendobj\n%%EOF\n"


@pytest.fixture
def artifact_store(tmp_path: Path):
    storage = Storage(tmp_path / "web-listening.db")
    store = ArtifactStore(storage, root=tmp_path / "downloads")
    try:
        yield store
    finally:
        storage.close()


def _store_html(
    store: ArtifactStore,
    *,
    source_identity: str = "https://example.invalid/reports/annual",
    source_run_id: str = RUN_ID,
    entity_bytes: bytes = HTML,
    retrieved_at: str = RETRIEVED_AT,
    **changes,
):
    values = {
        "source_run_id": source_run_id,
        "normalized_source_identity": source_identity,
        "entity_bytes": entity_bytes,
        "response_content_type": "text/html; charset=utf-8",
        "requested_url": source_identity,
        "source_url": source_identity,
        "final_url": source_identity,
        "retrieved_at": retrieved_at,
        "http_status": 200,
        "wire_encoding": "gzip",
        "content_encoding": "identity",
        "access_decision_id": "access-decision-1111111111111111",
        "adapter_id": "web_http",
        "adapter_version": "1.0.0",
    }
    values.update(changes)
    return store.store_observation(**values)


def _count(store: ArtifactStore, table: str) -> int:
    row = store.storage.conn.execute(
        f"SELECT COUNT(*) AS count FROM {table}"
    ).fetchone()
    return int(row["count"])


def _make_unreferenced_blob(store: ArtifactStore):
    stored = _store_html(store)
    store.storage.conn.execute(
        "DELETE FROM artifact_observations WHERE artifact_id = ?",
        (stored.observation.artifact_id,),
    )
    store.storage.conn.execute(
        "DELETE FROM artifact_versions WHERE version_id = ?",
        (stored.version.version_id,),
    )
    store.storage.conn.commit()
    return stored, store.resolve_blob_path(stored.blob.sha256)


def _store_derived(store: ArtifactStore, source):
    normalized_identity = (
        f"urn:web-listening:derived:{source.observation.artifact_id}:markdown"
    )
    return store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity=normalized_identity,
        entity_bytes=b"# Derived\n",
        response_content_type="text/markdown",
        requested_url="https://example.invalid/derived.md",
        source_url="https://example.invalid/derived.md",
        final_url="https://example.invalid/derived.md",
        retrieved_at="2026-08-21T12:03:00Z",
        http_status=200,
        artifact_role="derived",
        parent_artifact_id=source.observation.artifact_id,
        source_artifact_id=source.observation.artifact_id,
        derived_from_artifact_ids=(source.observation.artifact_id,),
    )


def test_repeated_identical_acquisition_is_one_blob_version_and_observation(
    artifact_store: ArtifactStore,
) -> None:
    first = _store_html(artifact_store)
    second = _store_html(artifact_store)

    expected_digest = hashlib.sha256(HTML).hexdigest()
    expected_artifact_id = artifact_id_for_identity(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/reports/annual",
        sha256=expected_digest,
    )
    assert first == second
    assert first.observation.artifact_id == expected_artifact_id
    assert (
        first.version.version_id
        == f"version-{expected_artifact_id.removeprefix('artifact-')}"
    )
    assert first.blob.sha256 == expected_digest
    assert first.blob.artifact_uri == f"artifact:sha256:{expected_digest}"
    assert not Path(first.blob.storage_path).is_absolute()
    assert _count(artifact_store, "artifact_blobs") == 1
    assert _count(artifact_store, "artifact_versions") == 1
    assert _count(artifact_store, "artifact_observations") == 1


def test_canonical_entity_hash_survives_at_rest_compression_and_exact_replay(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    blob_path = artifact_store.resolve_blob_path(stored.blob.sha256)
    compressed = blob_path.read_bytes()
    decompressed = gzip.decompress(compressed)

    assert stored.blob.storage_encoding == "gzip"
    assert compressed != HTML
    assert hashlib.sha256(compressed).hexdigest() != stored.blob.sha256
    assert decompressed == HTML
    assert hashlib.sha256(decompressed).hexdigest() == stored.blob.sha256
    replay = artifact_store.replay_observation(stored.observation.artifact_id)
    assert replay.entity_bytes == HTML
    assert replay.observation.wire_encoding == "gzip"
    assert replay.observation.content_encoding == "identity"


def test_cross_url_equal_bytes_share_blob_without_collapsing_provenance(
    artifact_store: ArtifactStore,
) -> None:
    first = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/reports/annual.pdf",
        entity_bytes=PDF,
        response_content_type="application/pdf",
        requested_url="https://example.invalid/reports/annual.pdf",
        source_url="https://example.invalid/reports/annual.pdf",
        final_url="https://example.invalid/reports/annual.pdf",
        retrieved_at=RETRIEVED_AT,
        http_status=200,
    )
    second = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity="https://cdn.example.invalid/archive/annual.pdf",
        entity_bytes=PDF,
        response_content_type="application/pdf",
        requested_url="https://cdn.example.invalid/archive/annual.pdf",
        source_url="https://cdn.example.invalid/archive/annual.pdf",
        final_url="https://cdn.example.invalid/archive/annual.pdf",
        retrieved_at="2026-08-21T12:01:00Z",
        http_status=200,
    )

    assert first.blob == second.blob
    assert first.version.version_id != second.version.version_id
    assert first.observation.artifact_id != second.observation.artifact_id
    assert _count(artifact_store, "artifact_blobs") == 1
    assert _count(artifact_store, "artifact_versions") == 2
    assert _count(artifact_store, "artifact_observations") == 2


def test_cross_url_equal_bytes_with_different_valid_mime_share_only_the_blob(
    artifact_store: ArtifactStore,
) -> None:
    first_url = "https://example.invalid/page.html"
    second_url = "https://cdn.example.invalid/page.xhtml"
    first = _store_html(artifact_store, source_identity=first_url)
    second = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity=second_url,
        entity_bytes=HTML,
        response_content_type="application/xhtml+xml",
        requested_url=second_url,
        source_url=second_url,
        final_url=second_url,
        retrieved_at="2026-08-21T12:01:00Z",
        http_status=200,
    )

    blob_columns = {
        row["name"]
        for row in artifact_store.storage.conn.execute(
            "PRAGMA table_info(artifact_blobs)"
        )
    }
    version_columns = {
        row["name"]
        for row in artifact_store.storage.conn.execute(
            "PRAGMA table_info(artifact_versions)"
        )
    }
    assert first.blob == second.blob
    assert first.version.mime_type == "text/html"
    assert second.version.mime_type == "application/xhtml+xml"
    assert first.observation.mime_type == first.version.mime_type
    assert second.observation.mime_type == second.version.mime_type
    assert "mime_type" not in blob_columns
    assert "mime_type" in version_columns
    assert artifact_store.get_observation(first.observation.artifact_id) == first
    assert artifact_store.get_observation(second.observation.artifact_id) == second
    assert _count(artifact_store, "artifact_blobs") == 1
    assert _count(artifact_store, "artifact_versions") == 2
    assert _count(artifact_store, "artifact_observations") == 2


def test_later_run_same_source_and_bytes_reuses_blob_but_keeps_version_and_observation(
    artifact_store: ArtifactStore,
) -> None:
    first = _store_html(artifact_store)
    second = _store_html(
        artifact_store,
        source_run_id="source-run-20260821-immutable-002",
        retrieved_at="2026-08-21T13:00:00Z",
    )

    assert first.blob == second.blob
    assert first.version.version_id != second.version.version_id
    assert first.observation.artifact_id != second.observation.artifact_id
    assert _count(artifact_store, "artifact_blobs") == 1
    assert _count(artifact_store, "artifact_versions") == 2
    assert _count(artifact_store, "artifact_observations") == 2


def test_same_url_new_content_is_a_new_version_without_overwriting_history(
    artifact_store: ArtifactStore,
) -> None:
    first = _store_html(artifact_store)
    changed_bytes = b"<!doctype html><html><body>changed source</body></html>"
    second = _store_html(
        artifact_store,
        entity_bytes=changed_bytes,
        retrieved_at="2026-08-21T12:05:00Z",
        parent_artifact_id=first.observation.artifact_id,
    )

    assert first.version.version_id != second.version.version_id
    assert first.observation.artifact_id != second.observation.artifact_id
    assert (
        artifact_store.replay_observation(first.observation.artifact_id).entity_bytes
        == HTML
    )
    assert (
        artifact_store.replay_observation(second.observation.artifact_id).entity_bytes
        == changed_bytes
    )
    assert second.lineage[0].related_artifact_id == first.observation.artifact_id
    assert _count(artifact_store, "artifact_blobs") == 2
    assert _count(artifact_store, "artifact_versions") == 2
    assert _count(artifact_store, "artifact_observations") == 2


@pytest.mark.parametrize(
    ("content_type", "url", "body", "reason_code"),
    [
        (
            "application/pdf",
            "https://example.invalid/report.pdf",
            HTML,
            "mime.magic_mismatch",
        ),
        (
            "text/html",
            "https://example.invalid/report.pdf",
            HTML,
            "mime.extension_mismatch",
        ),
        (
            "application/x-unknown",
            "https://example.invalid/report.bin",
            b"opaque",
            "mime.unsupported",
        ),
    ],
)
def test_header_extension_and_magic_mismatch_fail_closed_without_state(
    artifact_store: ArtifactStore,
    content_type: str,
    url: str,
    body: bytes,
    reason_code: str,
) -> None:
    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.store_observation(
            source_run_id=RUN_ID,
            normalized_source_identity=url,
            entity_bytes=body,
            response_content_type=content_type,
            requested_url=url,
            source_url=url,
            final_url=url,
            retrieved_at=RETRIEVED_AT,
            http_status=200,
        )

    assert error.value.reason_code == reason_code
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert [path for path in artifact_store.root.rglob("*") if path.is_file()] == []


@pytest.mark.parametrize(
    ("content_type", "url", "body"),
    [
        ("text/html", "https://example.invalid/page.html", HTML),
        ("application/pdf", "https://example.invalid/report.pdf", PDF),
        (
            "application/zip",
            "https://example.invalid/archive.zip",
            b"PK\x05\x06" + b"\0" * 18,
        ),
    ],
)
def test_html_pdf_and_allowed_attachment_use_one_immutable_write_path(
    artifact_store: ArtifactStore,
    content_type: str,
    url: str,
    body: bytes,
) -> None:
    stored = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity=url,
        entity_bytes=body,
        response_content_type=content_type,
        requested_url=url,
        source_url=url,
        final_url=url,
        retrieved_at=RETRIEVED_AT,
        http_status=200,
    )

    assert artifact_store.read_blob(stored.blob.artifact_uri) == body


def test_conflicting_replay_never_mutates_the_frozen_observation(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    before = artifact_store.storage.db_path.read_bytes()

    with pytest.raises(ArtifactStoreError) as error:
        _store_html(
            artifact_store,
            final_url="https://example.invalid/reports/annual?changed=1",
            redirect_chain=(
                {
                    "ordinal": 0,
                    "from_url": "https://example.invalid/reports/annual",
                    "to_url": "https://example.invalid/reports/annual?changed=1",
                    "http_status": 301,
                    "access_decision_id": "access-decision-1111111111111111",
                    "decision": "allow",
                },
            ),
        )

    assert error.value.reason_code == "observation.conflict"
    assert artifact_store.storage.db_path.read_bytes() == before
    assert (
        artifact_store.replay_observation(stored.observation.artifact_id).entity_bytes
        == HTML
    )


def test_existing_observation_replay_cannot_add_lineage_to_an_empty_set(
    artifact_store: ArtifactStore,
) -> None:
    parent = _store_html(
        artifact_store,
        source_identity="https://example.invalid/parent.html",
    )
    child = _store_html(
        artifact_store,
        source_identity="https://example.invalid/child.html",
    )
    counts_before = {
        table: _count(artifact_store, table)
        for table in (
            "artifact_blobs",
            "artifact_versions",
            "artifact_observations",
            "artifact_lineage",
        )
    }

    with pytest.raises(ArtifactStoreError) as error:
        _store_html(
            artifact_store,
            source_identity="https://example.invalid/child.html",
            parent_artifact_id=parent.observation.artifact_id,
        )

    assert error.value.reason_code == "lineage.conflict"
    assert {
        table: _count(artifact_store, table) for table in counts_before
    } == counts_before
    assert artifact_store.get_observation(child.observation.artifact_id).lineage == ()


def test_cancellation_after_blob_publication_rolls_back_file_rows_and_temporaries(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt("cancelled")

    monkeypatch.setattr(artifact_store, "_insert_metadata", interrupt)

    with pytest.raises(KeyboardInterrupt, match="cancelled"):
        _store_html(artifact_store)

    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert [path for path in artifact_store.root.rglob("*") if path.is_file()] == []


def test_cas_publication_uses_anchored_exclusive_temporary_not_path_mkstemp(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def reject_path_temporary(*args, **kwargs):
        raise AssertionError("path-based mkstemp must not publish CAS bytes")

    monkeypatch.setattr(
        tempfile,
        "mkstemp",
        reject_path_temporary,
    )

    stored = _store_html(artifact_store)

    assert (
        artifact_store.replay_observation(stored.observation.artifact_id).entity_bytes
        == HTML
    )


def test_failure_after_blob_and_version_rows_rolls_back_without_dangling_reference(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_observation(*args, **kwargs):
        raise SystemExit("stop after version")

    monkeypatch.setattr(artifact_store, "_observation_values", fail_observation)

    with pytest.raises(SystemExit, match="stop after version"):
        _store_html(artifact_store)

    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert _count(artifact_store, "artifact_lineage") == 0
    assert [path for path in artifact_store.root.rglob("*") if path.is_file()] == []


def test_preexisting_conflicting_cas_leaf_is_never_overwritten_or_removed(
    artifact_store: ArtifactStore,
) -> None:
    digest = hashlib.sha256(HTML).hexdigest()
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    target.parent.mkdir(parents=True)
    conflict = b"preexisting conflicting bytes"
    target.write_bytes(conflict)

    with pytest.raises(ArtifactStoreError) as error:
        _store_html(artifact_store)

    assert error.value.reason_code == "blob.conflict"
    assert target.read_bytes() == conflict
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0


def test_missing_lineage_reference_rolls_back_new_blob_and_all_rows(
    artifact_store: ArtifactStore,
) -> None:
    with pytest.raises(ArtifactStoreError) as error:
        _store_html(
            artifact_store,
            parent_artifact_id="artifact-000000000000000000000000",
        )

    assert error.value.reason_code == "lineage.missing_reference"
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert [path for path in artifact_store.root.rglob("*") if path.is_file()] == []


def test_derived_identity_must_bind_the_selected_source_artifact(
    artifact_store: ArtifactStore,
) -> None:
    first = _store_html(
        artifact_store,
        source_identity="https://example.invalid/first.html",
    )
    second = _store_html(
        artifact_store,
        source_identity="https://example.invalid/second.html",
    )
    mismatched_identity = (
        f"urn:web-listening:derived:{first.observation.artifact_id}:markdown"
    )

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.store_observation(
            source_run_id=RUN_ID,
            normalized_source_identity=mismatched_identity,
            entity_bytes=b"# Derived\n",
            response_content_type="text/markdown",
            requested_url="https://example.invalid/derived.md",
            source_url="https://example.invalid/derived.md",
            final_url="https://example.invalid/derived.md",
            retrieved_at="2026-08-21T12:03:00Z",
            http_status=200,
            artifact_role="derived",
            source_artifact_id=second.observation.artifact_id,
            derived_from_artifact_ids=(second.observation.artifact_id,),
        )

    assert error.value.reason_code == "lineage.invalid"
    assert _count(artifact_store, "artifact_observations") == 2
    assert _count(artifact_store, "artifact_lineage") == 0


@pytest.mark.parametrize(
    "changes",
    [
        {"access_decision_id": "not-an-access-decision"},
        {"adapter_id": "Web_HTTP"},
        {"adapter_version": "1.0"},
        {
            "redirect_chain": (
                {
                    "ordinal": 1,
                    "from_url": "https://example.invalid/reports/annual",
                    "to_url": "https://example.invalid/reports/annual",
                    "http_status": 301,
                    "access_decision_id": "access-decision-1111111111111111",
                    "decision": "allow",
                },
            )
        },
        {
            "discovered_from": {
                "kind": "seed",
                "artifact_id": None,
                "source_url": None,
                "extra": "not closed",
            }
        },
    ],
)
def test_invalid_frozen_provenance_fails_before_any_mutation(
    artifact_store: ArtifactStore,
    changes,
) -> None:
    with pytest.raises(ArtifactStoreError) as error:
        _store_html(artifact_store, **changes)

    assert error.value.reason_code == "observation.provenance_invalid"
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert [path for path in artifact_store.root.rglob("*") if path.is_file()] == []


def test_default_provenance_is_manifest_compatible_seed_evidence(
    artifact_store: ArtifactStore,
) -> None:
    url = "https://example.invalid/default.html"
    stored = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity=url,
        entity_bytes=HTML,
        response_content_type="text/html",
        requested_url=url,
        source_url=url,
        final_url=url,
        retrieved_at=RETRIEVED_AT,
        http_status=200,
    )

    assert stored.observation.access_decision_id == ("access-decision-0000000000000000")
    assert json.loads(stored.observation.discovered_from_json) == {
        "kind": "seed",
        "artifact_id": None,
        "source_url": None,
    }


def test_new_artifact_tables_reject_dangling_inserts_and_deletes(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    connection = artifact_store.storage.conn

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO artifact_versions
               (version_id, manifest_version, source_run_id,
                normalized_source_identity, sha256, artifact_uri, mime_type,
                created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "version-000000000000000000000000",
                "acquisition-manifest.v1",
                "source-run-dangling",
                "https://example.invalid/dangling",
                "0" * 64,
                f"artifact:sha256:{'0' * 64}",
                "text/html",
                RETRIEVED_AT,
            ),
        )
    connection.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            """INSERT INTO artifact_lineage
               (lineage_id, artifact_id, relation, related_artifact_id,
                ordinal, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                "lineage-000000000000000000000000",
                "artifact-000000000000000000000000",
                "parent",
                stored.observation.artifact_id,
                0,
                RETRIEVED_AT,
            ),
        )
    connection.rollback()

    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM artifact_versions WHERE version_id = ?",
            (stored.version.version_id,),
        )
    connection.rollback()
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        )
    connection.rollback()
    child = _store_html(
        artifact_store,
        entity_bytes=b"<!doctype html><html><body>child</body></html>",
        parent_artifact_id=stored.observation.artifact_id,
    )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM artifact_observations WHERE artifact_id = ?",
            (stored.observation.artifact_id,),
        )
    connection.rollback()
    assert (
        artifact_store.replay_observation(stored.observation.artifact_id).entity_bytes
        == HTML
    )
    assert artifact_store.get_observation(child.observation.artifact_id).lineage


def test_loaded_source_observation_rejects_source_or_derived_lineage_edges(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    related = _store_html(
        artifact_store,
        source_identity="https://example.invalid/related.html",
    )
    lineage_id = artifact_lineage_id(
        artifact_id=stored.observation.artifact_id,
        relation="source",
        related_artifact_id=related.observation.artifact_id,
        ordinal=0,
    )
    artifact_store.storage.conn.execute(
        "INSERT INTO artifact_lineage VALUES (?, ?, ?, ?, ?, ?)",
        (
            lineage_id,
            stored.observation.artifact_id,
            "source",
            related.observation.artifact_id,
            0,
            RETRIEVED_AT,
        ),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"


def test_loaded_lineage_rejects_a_missing_related_observation(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    missing = "artifact-000000000000000000000000"
    artifact_store.storage.conn.execute(
        "DROP TRIGGER artifact_lineage_reference_insert_guard"
    )
    artifact_store.storage.conn.execute(
        "INSERT INTO artifact_lineage VALUES (?, ?, ?, ?, ?, ?)",
        (
            artifact_lineage_id(
                artifact_id=stored.observation.artifact_id,
                relation="parent",
                related_artifact_id=missing,
                ordinal=0,
            ),
            stored.observation.artifact_id,
            "parent",
            missing,
            0,
            RETRIEVED_AT,
        ),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "lineage.missing_reference"


@pytest.mark.parametrize(
    "storage_path",
    [
        "_blobs/00/{digest}.gz",
        "../outside/{digest}.gz",
        "C:/outside/{digest}.gz",
    ],
)
def test_loaded_blob_rejects_noncanonical_or_nonportable_storage_path(
    artifact_store: ArtifactStore,
    storage_path: str,
) -> None:
    stored = _store_html(artifact_store)
    artifact_store.storage.conn.execute(
        "UPDATE artifact_blobs SET storage_path = ? WHERE sha256 = ?",
        (storage_path.format(digest=stored.blob.sha256), stored.blob.sha256),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "blob.path_invalid"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("access_decision_id", "not-an-access-decision"),
        ("adapter_id", "Web_HTTP"),
        ("adapter_version", "1.0"),
        (
            "redirect_chain_json",
            json.dumps(
                [
                    {
                        "ordinal": 1,
                        "from_url": "https://example.invalid/reports/annual",
                        "to_url": "https://example.invalid/reports/annual",
                        "http_status": 301,
                        "access_decision_id": "access-decision-1111111111111111",
                        "decision": "allow",
                    }
                ]
            ),
        ),
        ("discovered_from_json", "{}"),
    ],
)
def test_loaded_frozen_provenance_tamper_fails_stably(
    artifact_store: ArtifactStore,
    column: str,
    value: str,
) -> None:
    stored = _store_html(artifact_store)
    artifact_store.storage.conn.execute(
        f"UPDATE artifact_observations SET {column} = ? WHERE artifact_id = ?",
        (value, stored.observation.artifact_id),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "observation.provenance_invalid"


def test_derived_observation_load_requires_contiguous_lineage_ordinals(
    artifact_store: ArtifactStore,
) -> None:
    source = _store_html(artifact_store)
    derived = _store_derived(artifact_store, source)
    edge = next(item for item in derived.lineage if item.relation == "derived_from")
    changed_id = artifact_lineage_id(
        artifact_id=edge.artifact_id,
        relation=edge.relation,
        related_artifact_id=edge.related_artifact_id,
        ordinal=1,
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_lineage SET lineage_id = ?, ordinal = 1 WHERE lineage_id = ?",
        (changed_id, edge.lineage_id),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(derived.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"


def test_derived_observation_load_requires_exact_parent_source_binding(
    artifact_store: ArtifactStore,
) -> None:
    source = _store_html(artifact_store)
    derived = _store_derived(artifact_store, source)
    artifact_store.storage.conn.execute(
        "DELETE FROM artifact_lineage WHERE artifact_id = ? AND relation = 'parent'",
        (derived.observation.artifact_id,),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(derived.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"


def test_loaded_lineage_rejects_unknown_relations_after_identity_validation(
    artifact_store: ArtifactStore,
) -> None:
    parent = _store_html(artifact_store)
    child = _store_html(
        artifact_store,
        entity_bytes=b"<!doctype html><html><body>child relation</body></html>",
        parent_artifact_id=parent.observation.artifact_id,
    )
    edge = child.lineage[0]
    changed_id = artifact_lineage_id(
        artifact_id=edge.artifact_id,
        relation="unknown",
        related_artifact_id=edge.related_artifact_id,
        ordinal=edge.ordinal,
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_lineage SET lineage_id = ?, relation = 'unknown' "
        "WHERE lineage_id = ?",
        (changed_id, edge.lineage_id),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(child.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"


def test_retention_deletes_only_a_blob_with_no_new_or_legacy_reference(
    artifact_store: ArtifactStore,
) -> None:
    first = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/a.pdf",
        entity_bytes=PDF,
        response_content_type="application/pdf",
        requested_url="https://example.invalid/a.pdf",
        source_url="https://example.invalid/a.pdf",
        final_url="https://example.invalid/a.pdf",
        retrieved_at=RETRIEVED_AT,
        http_status=200,
    )
    second = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/b.pdf",
        entity_bytes=PDF,
        response_content_type="application/pdf",
        requested_url="https://example.invalid/b.pdf",
        source_url="https://example.invalid/b.pdf",
        final_url="https://example.invalid/b.pdf",
        retrieved_at="2026-08-21T12:01:00Z",
        http_status=200,
    )
    digest = first.blob.sha256
    blob_path = artifact_store.resolve_blob_path(digest)

    artifact_store.storage.conn.execute(
        "DELETE FROM artifact_observations WHERE artifact_id = ?",
        (first.observation.artifact_id,),
    )
    artifact_store.storage.conn.execute(
        "DELETE FROM artifact_versions WHERE version_id = ?",
        (first.version.version_id,),
    )
    artifact_store.storage.conn.commit()
    assert artifact_store.prune_unreferenced_blob(digest) is False
    assert artifact_store.resolve_blob_path(digest).exists()

    artifact_store.storage.conn.execute(
        "DELETE FROM artifact_observations WHERE artifact_id = ?",
        (second.observation.artifact_id,),
    )
    artifact_store.storage.conn.execute(
        "DELETE FROM artifact_versions WHERE version_id = ?",
        (second.version.version_id,),
    )
    artifact_store.storage.conn.commit()
    assert artifact_store.prune_unreferenced_blob(digest) is True
    assert not blob_path.exists()
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (digest,)
        ).fetchone()
        is None
    )


def test_legacy_document_blob_remains_readable_and_protects_retention(
    tmp_path: Path,
) -> None:
    storage = Storage(tmp_path / "legacy.db")
    legacy_root = tmp_path / "downloads"
    legacy_path = legacy_root / "_blobs" / "legacy.pdf"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(PDF)
    digest = hashlib.sha256(PDF).hexdigest()
    storage.upsert_blob(
        sha256=digest,
        canonical_path=str(legacy_path),
        file_size=len(PDF),
        content_type="application/pdf",
    )
    store = ArtifactStore(storage, root=legacy_root)
    try:
        assert store.read_blob(f"artifact:sha256:{digest}") == PDF
        assert store.prune_unreferenced_blob(digest) is False
        assert legacy_path.read_bytes() == PDF
    finally:
        storage.close()


def test_database_schema_upgrade_is_additive_and_legacy_document_reads_still_work(
    tmp_path: Path,
) -> None:
    database = tmp_path / "legacy.db"
    legacy = sqlite3.connect(database)
    legacy.execute(
        """CREATE TABLE documents (
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               site_id INTEGER NOT NULL,
               title TEXT DEFAULT '',
               url TEXT NOT NULL,
               download_url TEXT NOT NULL,
               institution TEXT DEFAULT '',
               page_url TEXT DEFAULT '',
               published_at TEXT,
               downloaded_at TEXT,
               local_path TEXT DEFAULT '',
               doc_type TEXT DEFAULT '',
               sha256 TEXT DEFAULT '',
               file_size INTEGER,
               content_type TEXT DEFAULT '',
               etag TEXT DEFAULT '',
               last_modified TEXT DEFAULT '',
               content_md TEXT DEFAULT '',
               content_md_status TEXT DEFAULT 'pending',
               content_md_updated_at TEXT
           )"""
    )
    legacy.execute(
        """INSERT INTO documents
           (site_id, title, url, download_url, local_path, sha256)
           VALUES (1, 'Legacy PDF', ?, ?, ?, ?)""",
        (
            "https://example.invalid/report.pdf",
            "https://example.invalid/report.pdf",
            "data/downloads/_blobs/legacy.pdf",
            "a" * 64,
        ),
    )
    legacy.commit()
    legacy.close()

    reopened = Storage(database)
    try:
        assert (
            reopened.get_document_by_download_url(
                "https://example.invalid/report.pdf"
            ).title
            == "Legacy PDF"
        )
        tables = {
            row["name"]
            for row in reopened.conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {
            "artifact_blobs",
            "artifact_versions",
            "artifact_observations",
            "artifact_lineage",
        } <= tables
    finally:
        reopened.close()


def test_pre_release_artifact_schema_is_compatibly_read_and_mime_is_backfilled(
    tmp_path: Path,
) -> None:
    database = tmp_path / "pre-release-artifacts.db"
    downloads = tmp_path / "downloads"
    digest = hashlib.sha256(HTML).hexdigest()
    artifact_id = artifact_id_for_identity(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/legacy.html",
        sha256=digest,
    )
    version_id = artifact_version_id(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/legacy.html",
        sha256=digest,
    )
    stored_bytes = gzip.compress(HTML, compresslevel=9, mtime=0)
    relative_path = f"_blobs/{digest[:2]}/{digest}.gz"
    blob_path = downloads.joinpath(*relative_path.split("/"))
    blob_path.parent.mkdir(parents=True)
    blob_path.write_bytes(stored_bytes)
    legacy = sqlite3.connect(database)
    legacy.executescript(
        """
        CREATE TABLE artifact_blobs (
            sha256 TEXT PRIMARY KEY,
            artifact_uri TEXT NOT NULL UNIQUE,
            storage_path TEXT NOT NULL UNIQUE,
            entity_size_bytes INTEGER NOT NULL,
            stored_size_bytes INTEGER NOT NULL,
            mime_type TEXT NOT NULL,
            storage_encoding TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifact_versions (
            version_id TEXT PRIMARY KEY,
            manifest_version TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            normalized_source_identity TEXT NOT NULL,
            sha256 TEXT NOT NULL,
            artifact_uri TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifact_observations (
            artifact_id TEXT PRIMARY KEY,
            version_id TEXT NOT NULL,
            manifest_version TEXT NOT NULL,
            source_run_id TEXT NOT NULL,
            normalized_source_identity TEXT NOT NULL,
            requested_url TEXT NOT NULL,
            source_url TEXT NOT NULL,
            final_url TEXT NOT NULL,
            retrieved_at TEXT NOT NULL,
            http_status INTEGER NOT NULL,
            mime_type TEXT NOT NULL,
            size_bytes INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            artifact_uri TEXT NOT NULL,
            wire_encoding TEXT NOT NULL,
            content_encoding TEXT NOT NULL,
            artifact_role TEXT NOT NULL,
            artifact_status TEXT NOT NULL,
            access_decision_id TEXT NOT NULL,
            adapter_id TEXT NOT NULL,
            adapter_version TEXT NOT NULL,
            redirect_chain_json TEXT NOT NULL,
            discovered_from_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE artifact_lineage (
            lineage_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            related_artifact_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )
    legacy.execute(
        "INSERT INTO artifact_blobs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            digest,
            f"artifact:sha256:{digest}",
            relative_path,
            len(HTML),
            len(stored_bytes),
            "text/html",
            "gzip",
            RETRIEVED_AT,
        ),
    )
    legacy.execute(
        "INSERT INTO artifact_versions VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            version_id,
            "acquisition-manifest.v1",
            RUN_ID,
            "https://example.invalid/legacy.html",
            digest,
            f"artifact:sha256:{digest}",
            RETRIEVED_AT,
        ),
    )
    legacy.execute(
        "INSERT INTO artifact_observations VALUES "
        f"({', '.join('?' for _ in range(24))})",
        (
            artifact_id,
            version_id,
            "acquisition-manifest.v1",
            RUN_ID,
            "https://example.invalid/legacy.html",
            "https://example.invalid/legacy.html",
            "https://example.invalid/legacy.html",
            "https://example.invalid/legacy.html",
            RETRIEVED_AT,
            200,
            "text/html",
            len(HTML),
            digest,
            f"artifact:sha256:{digest}",
            "identity",
            "identity",
            "source",
            "completed",
            "access-decision-0000000000000000",
            "web_http",
            "1.0.0",
            "[]",
            json.dumps(
                {"artifact_id": None, "kind": "seed", "source_url": None},
                separators=(",", ":"),
                sort_keys=True,
            ),
            RETRIEVED_AT,
        ),
    )
    legacy.commit()
    legacy.close()

    storage = Storage(database)
    store = ArtifactStore(storage, root=downloads)
    try:
        replay = store.replay_observation(artifact_id)
        assert replay.entity_bytes == HTML
        assert replay.version.mime_type == "text/html"
        assert replay.observation.filename == ""
        assert not hasattr(replay.blob, "mime_type")
    finally:
        storage.close()


def test_lineage_identity_is_deterministic_and_payload_is_canonical_json(
    artifact_store: ArtifactStore,
) -> None:
    parent = _store_html(artifact_store)
    child_url = "https://example.invalid/report.pdf"
    child = artifact_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity=child_url,
        entity_bytes=PDF,
        response_content_type="application/pdf",
        requested_url=child_url,
        source_url=child_url,
        final_url=child_url,
        retrieved_at="2026-08-21T12:02:00Z",
        http_status=200,
        parent_artifact_id=parent.observation.artifact_id,
        discovered_from={
            "kind": "link",
            "artifact_id": parent.observation.artifact_id,
            "source_url": parent.observation.final_url,
        },
    )

    row = artifact_store.storage.conn.execute(
        "SELECT discovered_from_json FROM artifact_observations WHERE artifact_id = ?",
        (child.observation.artifact_id,),
    ).fetchone()
    assert row["discovered_from_json"] == json.dumps(
        {
            "artifact_id": parent.observation.artifact_id,
            "kind": "link",
            "source_url": parent.observation.final_url,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    assert child.lineage[0].lineage_id.startswith("lineage-")
    assert child.lineage[0].relation == "parent"


def test_shared_storage_serializes_cross_thread_artifact_transactions(
    artifact_store: ArtifactStore,
) -> None:
    barrier = threading.Barrier(2)
    results = []
    failures = []

    def store_source(url: str) -> None:
        try:
            barrier.wait(timeout=5)
            results.append(_store_html(artifact_store, source_identity=url))
        except BaseException as exc:
            failures.append(exc)

    threads = [
        threading.Thread(
            target=store_source,
            args=(f"https://example.invalid/thread-{index}.html",),
        )
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not any(thread.is_alive() for thread in threads)
    assert failures == []
    assert len(results) == 2
    assert _count(artifact_store, "artifact_blobs") == 1
    assert _count(artifact_store, "artifact_versions") == 2
    assert _count(artifact_store, "artifact_observations") == 2


def test_cross_thread_failure_cannot_rollback_another_thread_transaction(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_inserted = threading.Event()
    release_first = threading.Event()
    second_entered_metadata = threading.Event()
    original_insert = artifact_store._insert_metadata
    results = []
    failures = []

    def controlled_insert(prepared) -> None:
        if prepared.observation.source_url.endswith("thread-first.html"):
            original_insert(prepared)
            first_inserted.set()
            if not release_first.wait(timeout=5):
                raise TimeoutError("first transaction was not released")
            return
        second_entered_metadata.set()
        raise RuntimeError("second transaction failed")

    monkeypatch.setattr(artifact_store, "_insert_metadata", controlled_insert)

    def store_first() -> None:
        try:
            results.append(
                _store_html(
                    artifact_store,
                    source_identity="https://example.invalid/thread-first.html",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    def store_second() -> None:
        try:
            _store_html(
                artifact_store,
                source_identity="https://example.invalid/thread-second.html",
            )
        except BaseException as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=store_first)
    second_thread = threading.Thread(target=store_second)
    first_thread.start()
    assert first_inserted.wait(timeout=5)
    second_thread.start()
    second_entered_before_release = second_entered_metadata.wait(timeout=0.25)
    release_first.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered_before_release is False
    assert len(results) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], RuntimeError)
    assert str(failures[0]) == "second transaction failed"
    assert (
        artifact_store.replay_observation(
            results[0].observation.artifact_id
        ).entity_bytes
        == HTML
    )
    assert _count(artifact_store, "artifact_observations") == 1


def test_cross_thread_store_waits_for_prior_post_commit_readback(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_readback_started = threading.Event()
    release_first_readback = threading.Event()
    second_entered_metadata = threading.Event()
    original_get = artifact_store.get_observation
    original_insert = artifact_store._insert_metadata
    first_thread_id = None
    results = []
    failures = []

    def controlled_get(artifact_id):
        if threading.get_ident() == first_thread_id:
            first_readback_started.set()
            if not release_first_readback.wait(timeout=5):
                raise TimeoutError("first readback was not released")
        return original_get(artifact_id)

    def controlled_insert(prepared) -> None:
        if prepared.observation.source_url.endswith("readback-second.html"):
            second_entered_metadata.set()
        original_insert(prepared)

    monkeypatch.setattr(artifact_store, "get_observation", controlled_get)
    monkeypatch.setattr(artifact_store, "_insert_metadata", controlled_insert)

    def store_first() -> None:
        nonlocal first_thread_id
        first_thread_id = threading.get_ident()
        try:
            results.append(
                _store_html(
                    artifact_store,
                    source_identity="https://example.invalid/readback-first.html",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    def store_second() -> None:
        try:
            results.append(
                _store_html(
                    artifact_store,
                    source_identity="https://example.invalid/readback-second.html",
                )
            )
        except BaseException as exc:
            failures.append(exc)

    first_thread = threading.Thread(target=store_first)
    second_thread = threading.Thread(target=store_second)
    first_thread.start()
    assert first_readback_started.wait(timeout=5)
    second_thread.start()
    second_entered_before_readback = second_entered_metadata.wait(timeout=0.25)
    release_first_readback.set()
    first_thread.join(timeout=5)
    second_thread.join(timeout=5)

    assert not first_thread.is_alive()
    assert not second_thread.is_alive()
    assert second_entered_before_readback is False
    assert failures == []
    assert len(results) == 2
    assert _count(artifact_store, "artifact_observations") == 2


def test_legacy_add_document_waits_for_paused_artifact_store_lifecycle(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    readback_started = threading.Event()
    release_readback = threading.Event()
    document_finished = threading.Event()
    original_get = artifact_store.get_observation
    store_thread_id = None
    failures = []

    def pause_get(artifact_id):
        if threading.get_ident() == store_thread_id:
            readback_started.set()
            if not release_readback.wait(timeout=5):
                raise TimeoutError("artifact readback was not released")
        return original_get(artifact_id)

    monkeypatch.setattr(artifact_store, "get_observation", pause_get)

    def store_artifact() -> None:
        nonlocal store_thread_id
        store_thread_id = threading.get_ident()
        try:
            _store_html(artifact_store)
        except BaseException as exc:
            failures.append(exc)

    def add_legacy_document() -> None:
        try:
            artifact_store.storage.add_document(
                Document(
                    site_id=1,
                    title="legacy",
                    url="https://example.invalid/legacy.pdf",
                    download_url="https://example.invalid/legacy.pdf",
                )
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            document_finished.set()

    store_thread = threading.Thread(target=store_artifact)
    document_thread = threading.Thread(target=add_legacy_document)
    store_thread.start()
    assert readback_started.wait(timeout=5)
    document_thread.start()
    document_finished_before_readback = document_finished.wait(timeout=0.25)
    release_readback.set()
    store_thread.join(timeout=5)
    document_thread.join(timeout=5)

    assert document_finished_before_readback is False
    assert failures == []
    assert _count(artifact_store, "artifact_observations") == 1
    assert artifact_store.storage.get_document_by_download_url(
        "https://example.invalid/legacy.pdf"
    )


def test_legacy_add_document_failure_cannot_join_paused_retention_transaction(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    retention_started = threading.Event()
    release_retention = threading.Event()
    legacy_commit_entered = threading.Event()
    original_quarantine = artifact_store._quarantine_blob_file
    retention_results = []
    legacy_failures = []

    def pause_quarantine(*args, **kwargs):
        retention_started.set()
        if not release_retention.wait(timeout=5):
            raise TimeoutError("retention was not released")
        return original_quarantine(*args, **kwargs)

    def fail_legacy_commit() -> None:
        legacy_commit_entered.set()
        raise RuntimeError("legacy commit failed")

    monkeypatch.setattr(artifact_store, "_quarantine_blob_file", pause_quarantine)
    monkeypatch.setattr(artifact_store.storage, "_commit", fail_legacy_commit)

    def retain_blob() -> None:
        retention_results.append(
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
        )

    def add_failing_document() -> None:
        try:
            artifact_store.storage.add_document(
                Document(
                    site_id=1,
                    title="failing legacy",
                    url="https://example.invalid/failing.pdf",
                    download_url="https://example.invalid/failing.pdf",
                )
            )
        except BaseException as exc:
            legacy_failures.append(exc)

    retention_thread = threading.Thread(target=retain_blob)
    document_thread = threading.Thread(target=add_failing_document)
    retention_thread.start()
    assert retention_started.wait(timeout=5)
    document_thread.start()
    legacy_entered_before_retention = legacy_commit_entered.wait(timeout=0.25)
    release_retention.set()
    retention_thread.join(timeout=5)
    document_thread.join(timeout=5)

    assert legacy_entered_before_retention is False
    assert retention_results == [True]
    assert len(legacy_failures) == 1
    assert isinstance(legacy_failures[0], RuntimeError)
    assert not target.exists()
    with sqlite3.connect(artifact_store.storage.db_path) as observer:
        assert observer.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    if artifact_store.storage.conn.in_transaction:
        artifact_store.storage.conn.rollback()


def test_same_thread_execution_transaction_remains_reentrant(
    artifact_store: ArtifactStore,
) -> None:
    artifact_store.storage.begin_execution_transaction()
    artifact_store.storage.begin_execution_transaction()
    stored = _store_html(artifact_store)
    observer = sqlite3.connect(artifact_store.storage.db_path)
    try:
        artifact_store.storage.commit_execution_transaction()
        assert (
            observer.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
            == 0
        )
        artifact_store.storage.commit_execution_transaction()
        assert (
            observer.execute("SELECT COUNT(*) FROM artifact_observations").fetchone()[0]
            == 1
        )
    finally:
        observer.close()
    assert (
        artifact_store.replay_observation(stored.observation.artifact_id).entity_bytes
        == HTML
    )


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("ancestor_kind", ["symlink", "invalid"])
def test_retention_rejects_a_symlinked_blob_ancestor_without_touching_victim(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
    ancestor_kind: str,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure invalid-ancestor check requires native POSIX dir_fd")
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    stored, target = _make_unreferenced_blob(artifact_store)
    original_bytes = target.read_bytes()
    original_parent = target.parent
    displaced_parent = target.parent.with_name(f"{target.parent.name}-real")
    original_parent.rename(displaced_parent)
    invalid_marker = b"invalid ancestor victim"
    if ancestor_kind == "symlink":
        try:
            original_parent.symlink_to(displaced_parent, target_is_directory=True)
        except OSError as exc:
            displaced_parent.rename(original_parent)
            pytest.skip(f"directory symlink creation unavailable: {exc}")
    else:
        original_parent.write_bytes(invalid_marker)
    victim = displaced_parent / target.name

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert error.value.reason_code == "blob.path_invalid"
    assert victim.read_bytes() == original_bytes
    if ancestor_kind == "symlink":
        assert original_parent.is_symlink()
    else:
        assert original_parent.read_bytes() == invalid_marker
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
            (stored.blob.sha256,),
        ).fetchone()
        is None
    )


def test_retention_quarantines_before_identity_check_and_preserves_replacement(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    replacement = b"unrelated replacement must survive"
    original_transfer = Storage._rename_directory_no_replace
    swapped = False

    def replace_before_quarantine(
        source, destination, *, src_dir_fd=None, dst_dir_fd=None
    ):
        nonlocal swapped
        source_name = os.fspath(source)
        if not swapped and (
            source_name == os.fspath(target) or source_name == target.name
        ):
            swapped = True
            target.unlink()
            target.write_bytes(replacement)
        return original_transfer(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        Storage,
        "_rename_directory_no_replace",
        staticmethod(replace_before_quarantine),
    )

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert error.value.reason_code == "blob.path_changed"
    assert swapped
    assert target.read_bytes() == replacement
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("replacement_level", ["root", "ancestor", "parent"])
def test_retention_rejects_ancestor_replacement_and_preserves_both_trees(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
    replacement_level: str,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure ancestor replacement requires native POSIX dir_fd")
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    stored, target = _make_unreferenced_blob(artifact_store)
    real_rename = os.rename
    original_transfer = Storage._rename_directory_no_replace
    replaced_directory = {
        "root": artifact_store.root,
        "ancestor": artifact_store.root / "_blobs",
        "parent": target.parent,
    }[replacement_level]
    displaced_parent = replaced_directory.with_name(
        f"{replaced_directory.name}-displaced"
    )
    victim = target.parent / target.name
    replaced = False
    replacement_error = None

    def replace_parent_before_quarantine(
        source, destination, *, src_dir_fd=None, dst_dir_fd=None
    ):
        nonlocal replaced, replacement_error
        source_name = os.fspath(source)
        if not replaced and (
            source_name == os.fspath(target) or source_name == target.name
        ):
            try:
                real_rename(replaced_directory, displaced_parent)
            except OSError as exc:
                replacement_error = exc
                return original_transfer(
                    source,
                    destination,
                    src_dir_fd=src_dir_fd,
                    dst_dir_fd=dst_dir_fd,
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            victim.write_bytes(b"replacement-tree victim")
            replaced = True
        return original_transfer(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        Storage,
        "_rename_directory_no_replace",
        staticmethod(replace_parent_before_quarantine),
    )

    try:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    except ArtifactStoreError as exc:
        error = exc
    else:
        error = None

    if replacement_error is not None:
        pytest.skip(f"ancestor replacement unavailable: {replacement_error}")

    assert error is not None and error.reason_code == "blob.path_changed"
    assert replaced
    assert victim.read_bytes() == b"replacement-tree victim"
    original_candidates = [
        path
        for path in displaced_parent.rglob("*")
        if path.is_file()
        and path.read_bytes().startswith(b"\x1f\x8b")
        and gzip.decompress(path.read_bytes()) == HTML
    ]
    assert len(original_candidates) == 1
    row = artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()
    if row is None:
        assert artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
            (stored.blob.sha256,),
        ).fetchone()
    else:
        assert artifact_store.read_blob(stored.blob.sha256) == HTML


def test_retention_final_check_preserves_a_quarantine_replacement_victim(
    artifact_store: ArtifactStore,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    real_connection = artifact_store.storage.conn
    replacement = b"final-check replacement victim"
    swapped = False

    class ReplacementConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        def execute(self, statement, parameters=()):
            nonlocal swapped
            if not swapped and statement.lstrip().startswith(
                "DELETE FROM artifact_blobs"
            ):
                candidates = list(
                    target.parent.glob(".web-listening-rollback-*/candidate")
                )
                assert len(candidates) == 1
                candidates[0].unlink()
                candidates[0].write_bytes(replacement)
                swapped = True
            return real_connection.execute(statement, parameters)

    artifact_store.storage.conn = ReplacementConnection()
    try:
        with pytest.raises(ArtifactStoreError) as error:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    finally:
        artifact_store.storage.conn = real_connection

    assert error.value.reason_code == "blob.path_changed"
    assert swapped
    assert not target.exists()
    assert (
        real_connection.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )
    assert real_connection.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()
    candidates = list(target.parent.glob(".web-listening-rollback-*/candidate"))
    assert len(candidates) == 1
    assert candidates[0].read_bytes() == replacement


def test_retention_postcommit_restore_collision_keeps_delete_and_primary(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    primary = KeyboardInterrupt("postcommit delete interrupted")
    replacement = b"postcommit replacement victim"

    def fail_before_delete(quarantine) -> None:
        target.write_bytes(replacement)
        raise primary

    monkeypatch.setattr(artifact_store, "_delete_quarantined_blob", fail_before_delete)

    with pytest.raises(KeyboardInterrupt) as caught:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert caught.value is primary
    assert target.read_bytes() == replacement
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )
    candidates = list(target.parent.glob(".web-listening-rollback-*/candidate"))
    assert len(candidates) == 1
    assert gzip.decompress(candidates[0].read_bytes()) == HTML
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()


def test_retention_quarantine_collision_never_clobbers_either_victim(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    token = "0" * 32
    legacy_collision = target.with_name(f".{target.name}.retention-{token}")
    legacy_collision.write_bytes(b"legacy collision victim")
    secure_collision = target.parent / ".web-listening-rollback-fixed"
    secure_collision.mkdir()
    (secure_collision / "victim").write_bytes(b"secure collision victim")
    monkeypatch.setattr(
        Storage,
        "_quarantine_name",
        staticmethod(lambda: ".web-listening-rollback-fixed"),
    )

    with pytest.raises((ArtifactStoreError, FileExistsError, OSError)):
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert target.exists()
    assert legacy_collision.read_bytes() == b"legacy collision victim"
    assert (secure_collision / "victim").read_bytes() == b"secure collision victim"
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()


@pytest.mark.parametrize("commit_effected", [False, True])
def test_retention_commit_base_exception_reconciles_row_and_quarantined_file(
    artifact_store: ArtifactStore,
    commit_effected: bool,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    real_connection = artifact_store.storage.conn
    failure = KeyboardInterrupt(f"retention-commit-{commit_effected}")

    class CommitFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            if commit_effected:
                real_connection.commit()
            raise failure

    artifact_store.storage.conn = CommitFaultConnection()
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    finally:
        artifact_store.storage.conn = real_connection

    assert caught.value is failure
    row_exists = (
        real_connection.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is not None
    )
    assert row_exists is (not commit_effected)
    assert target.exists() is (not commit_effected)
    retirement_exists = (
        real_connection.execute(
            "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
            (stored.blob.sha256,),
        ).fetchone()
        is not None
    )
    assert retirement_exists is commit_effected


def test_retention_post_commit_base_exception_restores_row_and_file(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    original_delete = artifact_store._delete_quarantined_blob
    interrupted = False

    def interrupt_once(quarantine) -> None:
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt("retention cleanup interrupted")
        original_delete(quarantine)

    monkeypatch.setattr(artifact_store, "_delete_quarantined_blob", interrupt_once)

    with pytest.raises(KeyboardInterrupt, match="retention cleanup interrupted"):
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert interrupted
    assert target.exists()
    assert artifact_store.read_blob(stored.blob.sha256) == HTML
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
            (stored.blob.sha256,),
        ).fetchone()
        is None
    )


def test_retention_fallback_rmdir_interrupt_after_unlink_keeps_durable_delete(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    primary = KeyboardInterrupt("fallback quarantine rmdir interrupted")
    real_rmdir = os.rmdir
    monkeypatch.setattr(os, "supports_dir_fd", set())

    def interrupt_empty_quarantine(path, *args, **kwargs):
        if Path(os.fspath(path)).name.startswith(".web-listening-rollback-"):
            raise primary
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", interrupt_empty_quarantine)

    with pytest.raises(KeyboardInterrupt) as caught:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert caught.value is primary
    assert not target.exists()
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )
    quarantines = list(target.parent.glob(".web-listening-rollback-*"))
    assert len(quarantines) == 1
    assert list(quarantines[0].iterdir()) == []


def test_retention_durable_commit_primary_precedes_later_rmdir_failure(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    real_connection = artifact_store.storage.conn
    primary = KeyboardInterrupt("durable commit interrupted")
    secondary = SystemExit("quarantine rmdir interrupted")
    real_rmdir = os.rmdir
    monkeypatch.setattr(os, "supports_dir_fd", set())

    class DurableCommitFaultConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            real_connection.commit()
            raise primary

    def interrupt_empty_quarantine(path, *args, **kwargs):
        if Path(os.fspath(path)).name.startswith(".web-listening-rollback-"):
            raise secondary
        return real_rmdir(path, *args, **kwargs)

    artifact_store.storage.conn = DurableCommitFaultConnection()
    monkeypatch.setattr(os, "rmdir", interrupt_empty_quarantine)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    finally:
        artifact_store.storage.conn = real_connection

    assert caught.value is primary
    assert not target.exists()
    assert (
        real_connection.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )


@pytest.mark.skipif(
    os.name != "posix"
    or not all(
        operation in os.supports_dir_fd
        for operation in (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    ),
    reason="secure retention quarantine requires native POSIX dir_fd operations",
)
def test_retention_secure_rmdir_interrupt_after_unlink_keeps_durable_delete(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    primary = KeyboardInterrupt("secure quarantine rmdir interrupted")
    real_rmdir = os.rmdir

    def interrupt_empty_quarantine(path, *args, **kwargs):
        if os.fspath(path).startswith(".web-listening-rollback-"):
            raise primary
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(os, "rmdir", interrupt_empty_quarantine)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(real_rmdir)
    supported_dir_fd.add(interrupt_empty_quarantine)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)

    with pytest.raises(KeyboardInterrupt) as caught:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert caught.value is primary
    assert not target.exists()
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )


@pytest.mark.skipif(
    os.name != "posix",
    reason="open temporary replacement requires POSIX unlink semantics",
)
def test_temporary_cleanup_preserves_a_replacement_and_rolls_back_publication(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replacement = b"temporary replacement must survive"
    replaced_path = None
    real_link = artifact_store._link_pinned_leaf

    def replace_after_publication(pinned, source_name, target_name) -> None:
        nonlocal replaced_path
        real_link(pinned, source_name, target_name)
        replaced_path = pinned.target.parent / source_name
        replaced_path.unlink()
        replaced_path.write_bytes(replacement)

    monkeypatch.setattr(artifact_store, "_link_pinned_leaf", replace_after_publication)

    with pytest.raises(ArtifactStoreError) as error:
        _store_html(artifact_store)

    assert error.value.reason_code == "blob.path_changed"
    assert replaced_path is not None
    assert replaced_path.read_bytes() == replacement
    digest = hashlib.sha256(HTML).hexdigest()
    assert not (artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz").exists()
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0


def test_exclusive_temporary_collision_is_no_clobber(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = hashlib.sha256(HTML).hexdigest()
    parent = artifact_store.root / "_blobs" / digest[:2]
    parent.mkdir(parents=True)
    temporary = parent / f".{digest}.{'0' * 32}.tmp"
    victim = b"exclusive temporary collision victim"
    temporary.write_bytes(victim)
    monkeypatch.setattr(secrets, "token_hex", lambda size: "0" * (size * 2))

    with pytest.raises(ArtifactStoreError):
        _store_html(artifact_store)

    assert temporary.read_bytes() == victim
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert not (artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz").exists()


def test_parent_replacement_during_publication_cannot_escape_or_clobber(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = hashlib.sha256(HTML).hexdigest()
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    displaced = target.parent.with_name(f"{target.parent.name}-displaced")
    replacement = b"replacement-tree victim"
    real_link = artifact_store._link_pinned_leaf
    swapped = False

    def replace_parent(pinned, source_name, target_name) -> None:
        nonlocal swapped
        try:
            pinned.target.parent.rename(displaced)
        except OSError as exc:
            pytest.skip(f"open-directory replacement unavailable: {exc}")
        pinned.target.parent.mkdir()
        pinned.target.write_bytes(replacement)
        swapped = True
        real_link(pinned, source_name, target_name)

    monkeypatch.setattr(artifact_store, "_link_pinned_leaf", replace_parent)

    with pytest.raises(ArtifactStoreError):
        _store_html(artifact_store)

    assert swapped
    assert target.read_bytes() == replacement
    assert not any(path.name.endswith(".tmp") for path in displaced.iterdir())
    assert not (displaced / target.name).exists()
    assert _count(artifact_store, "artifact_blobs") == 0


@pytest.mark.skipif(
    os.name != "posix" or os.open not in os.supports_dir_fd,
    reason="secure ancestor traversal requires POSIX dir_fd open",
)
@pytest.mark.parametrize("operation", ["read", "publish"])
@pytest.mark.parametrize("failure_type", [NotADirectoryError, FileNotFoundError])
def test_secure_parent_traversal_oserror_has_a_stable_store_error(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_type: type[OSError],
) -> None:
    stored = _store_html(artifact_store)
    entity_bytes = b"<!doctype html><html><body>publish race</body></html>"
    digest = (
        stored.blob.sha256
        if operation == "read"
        else hashlib.sha256(entity_bytes).hexdigest()
    )
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    if operation == "publish":
        monkeypatch.setattr(
            artifact_store.storage,
            "ensure_execution_artifact_directory",
            lambda *args, **kwargs: None,
        )
    original_open = os.open

    def fail_ancestor(component, flags, *args, **kwargs):
        if (
            kwargs.get("dir_fd") is not None
            and os.fspath(component) == target.parent.name
        ):
            raise failure_type("injected secure ancestor race")
        return original_open(component, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", fail_ancestor)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(original_open)
    supported_dir_fd.add(fail_ancestor)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)
    counts_before = {
        table: _count(artifact_store, table)
        for table in ("artifact_blobs", "artifact_versions", "artifact_observations")
    }

    with pytest.raises(ArtifactStoreError) as error:
        if operation == "read":
            artifact_store.read_blob(stored.blob.sha256)
        else:
            _store_html(artifact_store, entity_bytes=entity_bytes)

    assert error.value.reason_code in {"blob.path_changed", "blob.path_invalid"}
    assert {
        table: _count(artifact_store, table) for table in counts_before
    } == counts_before
    assert not any(path.name.endswith(".tmp") for path in target.parent.iterdir())


@pytest.mark.parametrize("operation", ["read", "publish"])
@pytest.mark.parametrize("failure_type", [NotADirectoryError, FileNotFoundError])
def test_fallback_parent_open_oserror_has_a_stable_store_error(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure_type: type[OSError],
) -> None:
    stored = _store_html(artifact_store)
    entity_bytes = b"<!doctype html><html><body>fallback race</body></html>"
    digest = (
        stored.blob.sha256
        if operation == "read"
        else hashlib.sha256(entity_bytes).hexdigest()
    )
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    if operation == "publish":
        monkeypatch.setattr(
            artifact_store.storage,
            "ensure_execution_artifact_directory",
            lambda *args, **kwargs: None,
        )
    original_open_directory = (
        artifact_store.storage._open_execution_directory_descriptor
    )

    def fail_parent(path):
        if Path(path) == target.parent:
            raise failure_type("injected fallback ancestor race")
        return original_open_directory(path)

    monkeypatch.setattr(
        artifact_store.storage,
        "_open_execution_directory_descriptor",
        fail_parent,
    )
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(os.open)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)
    counts_before = {
        table: _count(artifact_store, table)
        for table in ("artifact_blobs", "artifact_versions", "artifact_observations")
    }

    with pytest.raises(ArtifactStoreError) as error:
        if operation == "read":
            artifact_store.read_blob(stored.blob.sha256)
        else:
            _store_html(artifact_store, entity_bytes=entity_bytes)

    assert error.value.reason_code == "blob.path_changed"
    assert {
        table: _count(artifact_store, table) for table in counts_before
    } == counts_before
    assert not any(path.name.endswith(".tmp") for path in target.parent.iterdir())


def test_parent_pin_preserves_baseexception_and_closes_open_descriptors(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _store_html(artifact_store)
    target = artifact_store.resolve_blob_path(stored.blob.sha256)
    primary = KeyboardInterrupt("parent traversal interrupted")
    opened_descriptors: list[int] = []
    original_open_directory = (
        artifact_store.storage._open_execution_directory_descriptor
    )

    def interrupt_parent(path):
        if Path(path) == target.parent:
            raise primary
        descriptor = original_open_directory(path)
        opened_descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(
        artifact_store.storage,
        "_open_execution_directory_descriptor",
        interrupt_parent,
    )
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(os.open)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)

    with pytest.raises(KeyboardInterrupt) as caught:
        artifact_store.read_blob(stored.blob.sha256)

    assert caught.value is primary
    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])


def test_read_rejects_a_symlinked_blob_parent_without_following_escape(
    artifact_store: ArtifactStore,
    tmp_path: Path,
) -> None:
    stored = _store_html(artifact_store)
    target = artifact_store.resolve_blob_path(stored.blob.sha256)
    displaced = target.parent.with_name(f"{target.parent.name}-displaced")
    escape = tmp_path / "escape"
    target.parent.rename(displaced)
    escape.mkdir()
    escaped_leaf = escape / target.name
    escaped_bytes = (displaced / target.name).read_bytes()
    escaped_leaf.write_bytes(escaped_bytes)
    try:
        target.parent.symlink_to(escape, target_is_directory=True)
    except OSError as exc:
        displaced.rename(target.parent)
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.read_blob(stored.blob.sha256)

    assert error.value.reason_code in {"blob.path_changed", "blob.path_invalid"}
    assert escaped_leaf.read_bytes() == escaped_bytes


def test_public_storage_operation_waits_for_foreign_transaction_owner(
    artifact_store: ArtifactStore,
) -> None:
    transaction_started = threading.Event()
    release_transaction = threading.Event()
    document_finished = threading.Event()
    failures: list[BaseException] = []

    def own_transaction() -> None:
        try:
            artifact_store.storage.begin_execution_transaction()
            transaction_started.set()
            if not release_transaction.wait(timeout=5):
                raise TimeoutError("transaction owner was not released")
            artifact_store.storage.commit_execution_transaction()
        except BaseException as exc:
            failures.append(exc)

    def add_document() -> None:
        try:
            artifact_store.storage.add_document(
                Document(
                    site_id=1,
                    title="foreign owner wait",
                    url="https://example.invalid/foreign-owner.pdf",
                    download_url="https://example.invalid/foreign-owner.pdf",
                )
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            document_finished.set()

    owner = threading.Thread(target=own_transaction)
    writer = threading.Thread(target=add_document)
    owner.start()
    assert transaction_started.wait(timeout=5)
    writer.start()
    finished_while_foreign_owned = document_finished.wait(timeout=0.25)
    release_transaction.set()
    owner.join(timeout=5)
    writer.join(timeout=5)

    assert finished_while_foreign_owned is False
    assert failures == []
    assert artifact_store.storage.get_document_by_download_url(
        "https://example.invalid/foreign-owner.pdf"
    )


def test_public_storage_operation_is_reentrant_for_transaction_owner(
    artifact_store: ArtifactStore,
) -> None:
    observer = sqlite3.connect(artifact_store.storage.db_path)
    artifact_store.storage.begin_execution_transaction()
    try:
        artifact_store.storage.add_document(
            Document(
                site_id=1,
                title="owner reentrant",
                url="https://example.invalid/owner-reentrant.pdf",
                download_url="https://example.invalid/owner-reentrant.pdf",
            )
        )
        assert observer.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
        artifact_store.storage.commit_execution_transaction()
        assert observer.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 1
    finally:
        if artifact_store.storage.execution_transaction_active:
            artifact_store.storage.rollback_execution_transaction()
        observer.close()


@pytest.mark.parametrize("api_name", ["get", "replay", "resolve", "read"])
def test_artifact_read_api_holds_complete_storage_turn(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    api_name: str,
) -> None:
    stored = _store_html(artifact_store)
    read_started = threading.Event()
    release_read = threading.Event()
    writer_finished = threading.Event()
    failures: list[BaseException] = []
    if api_name in {"get", "replay"}:
        original = artifact_store._validate_loaded_identity

        def pause(*args, **kwargs):
            read_started.set()
            if not release_read.wait(timeout=5):
                raise TimeoutError("read was not released")
            return original(*args, **kwargs)

        monkeypatch.setattr(artifact_store, "_validate_loaded_identity", pause)
    else:
        original = artifact_store._digest_from_reference

        def pause(*args, **kwargs):
            read_started.set()
            if not release_read.wait(timeout=5):
                raise TimeoutError("read was not released")
            return original(*args, **kwargs)

        monkeypatch.setattr(artifact_store, "_digest_from_reference", pause)

    def perform_read() -> None:
        try:
            if api_name == "get":
                artifact_store.get_observation(stored.observation.artifact_id)
            elif api_name == "replay":
                artifact_store.replay_observation(stored.observation.artifact_id)
            elif api_name == "resolve":
                artifact_store.resolve_blob_path(stored.blob.sha256)
            else:
                artifact_store.read_blob(stored.blob.sha256)
        except BaseException as exc:
            failures.append(exc)

    def add_document() -> None:
        try:
            artifact_store.storage.add_document(
                Document(
                    site_id=1,
                    title=f"read lifecycle {api_name}",
                    url=f"https://example.invalid/read-{api_name}.pdf",
                    download_url=f"https://example.invalid/read-{api_name}.pdf",
                )
            )
        except BaseException as exc:
            failures.append(exc)
        finally:
            writer_finished.set()

    reader = threading.Thread(target=perform_read)
    writer = threading.Thread(target=add_document)
    reader.start()
    assert read_started.wait(timeout=5)
    writer.start()
    finished_while_reading = writer_finished.wait(timeout=0.25)
    release_read.set()
    reader.join(timeout=5)
    writer.join(timeout=5)

    assert finished_while_reading is False
    assert failures == []


@pytest.mark.parametrize("api_name", ["get", "replay", "resolve", "read"])
def test_artifact_read_api_waits_for_foreign_execution_owner(
    artifact_store: ArtifactStore,
    api_name: str,
) -> None:
    stored = _store_html(artifact_store)
    transaction_started = threading.Event()
    release_transaction = threading.Event()
    read_finished = threading.Event()
    failures: list[BaseException] = []

    def own_transaction() -> None:
        try:
            artifact_store.storage.begin_execution_transaction()
            transaction_started.set()
            if not release_transaction.wait(timeout=5):
                raise TimeoutError("transaction owner was not released")
            artifact_store.storage.rollback_execution_transaction()
        except BaseException as exc:
            failures.append(exc)

    def perform_read() -> None:
        try:
            if api_name == "get":
                artifact_store.get_observation(stored.observation.artifact_id)
            elif api_name == "replay":
                artifact_store.replay_observation(stored.observation.artifact_id)
            elif api_name == "resolve":
                artifact_store.resolve_blob_path(stored.blob.sha256)
            else:
                artifact_store.read_blob(stored.blob.sha256)
        except BaseException as exc:
            failures.append(exc)
        finally:
            read_finished.set()

    owner = threading.Thread(target=own_transaction)
    reader = threading.Thread(target=perform_read)
    owner.start()
    assert transaction_started.wait(timeout=5)
    reader.start()
    finished_while_foreign_owned = read_finished.wait(timeout=0.25)
    release_transaction.set()
    owner.join(timeout=5)
    reader.join(timeout=5)

    assert finished_while_foreign_owned is False
    assert failures == []
    assert read_finished.is_set()


def test_artifact_read_apis_are_reentrant_for_execution_owner(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    artifact_store.storage.begin_execution_transaction()
    try:
        assert artifact_store.get_observation(stored.observation.artifact_id) == stored
        assert (
            artifact_store.replay_observation(
                stored.observation.artifact_id
            ).entity_bytes
            == HTML
        )
        assert artifact_store.resolve_blob_path(stored.blob.sha256).is_file()
        assert artifact_store.read_blob(stored.blob.sha256) == HTML
    finally:
        artifact_store.storage.rollback_execution_transaction()


def test_cross_thread_rollback_releases_a_waiting_artifact_read(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    read_started = threading.Event()
    read_finished = threading.Event()
    rollback_finished = threading.Event()
    failures: list[BaseException] = []
    artifact_store.storage.begin_execution_transaction()

    def perform_read() -> None:
        read_started.set()
        try:
            artifact_store.get_observation(stored.observation.artifact_id)
        except BaseException as exc:
            failures.append(exc)
        finally:
            read_finished.set()

    def rollback() -> None:
        try:
            artifact_store.storage.rollback_execution_transaction()
        except BaseException as exc:
            failures.append(exc)
        finally:
            rollback_finished.set()

    reader = threading.Thread(target=perform_read)
    rollback_thread = threading.Thread(target=rollback)
    reader.start()
    assert read_started.wait(timeout=5)
    finished_before_rollback = read_finished.wait(timeout=0.25)
    rollback_thread.start()
    rollback_thread.join(timeout=5)
    reader.join(timeout=5)

    assert finished_before_rollback is False
    assert rollback_finished.is_set()
    assert read_finished.is_set()
    assert failures == []
    assert artifact_store.storage.execution_transaction_active is False


def test_retirement_blocks_an_independent_legacy_reference_commit(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    independent = Storage(artifact_store.storage.db_path)
    retention_paused = threading.Event()
    release_retention = threading.Event()
    writer_finished = threading.Event()
    retention_results: list[bool] = []
    writer_failures: list[BaseException] = []
    original_verify = artifact_store._verify_quarantined_blob

    def pause_verify(*args, **kwargs):
        retention_paused.set()
        if not release_retention.wait(timeout=5):
            raise TimeoutError("retention was not released")
        return original_verify(*args, **kwargs)

    monkeypatch.setattr(artifact_store, "_verify_quarantined_blob", pause_verify)

    def retain() -> None:
        retention_results.append(
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
        )

    def add_legacy_reference() -> None:
        try:
            independent.add_document(
                Document(
                    site_id=1,
                    title="retired digest",
                    url="https://example.invalid/retired.pdf",
                    download_url="https://example.invalid/retired.pdf",
                    sha256=stored.blob.sha256,
                )
            )
        except BaseException as exc:
            writer_failures.append(exc)
        finally:
            writer_finished.set()

    retention_thread = threading.Thread(target=retain)
    writer_thread = threading.Thread(target=add_legacy_reference)
    try:
        retention_thread.start()
        assert retention_paused.wait(timeout=5)
        writer_thread.start()
        finished_before_retention = writer_finished.wait(timeout=0.25)
        release_retention.set()
        retention_thread.join(timeout=5)
        writer_thread.join(timeout=5)

        assert finished_before_retention is False
        assert retention_results == [True]
        assert len(writer_failures) == 1
        assert isinstance(writer_failures[0], sqlite3.IntegrityError)
        assert not target.exists()
        assert (
            independent.conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
            == 0
        )
    finally:
        independent.close()


def test_new_immutable_store_atomically_resurrects_a_retired_digest(
    artifact_store: ArtifactStore,
) -> None:
    stored, target = _make_unreferenced_blob(artifact_store)
    assert artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    assert not target.exists()
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()

    resurrected = _store_html(artifact_store)

    assert resurrected.blob.sha256 == stored.blob.sha256
    assert artifact_store.read_blob(resurrected.blob.sha256) == HTML
    assert (
        artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
            (stored.blob.sha256,),
        ).fetchone()
        is None
    )


def test_publication_effect_then_baseexception_is_journaled_and_removed(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("link interrupted after effect")
    original_link = artifact_store._link_pinned_leaf

    def link_then_interrupt(*args, **kwargs):
        original_link(*args, **kwargs)
        raise primary

    monkeypatch.setattr(artifact_store, "_link_pinned_leaf", link_then_interrupt)

    with pytest.raises(KeyboardInterrupt) as caught:
        _store_html(artifact_store)

    assert caught.value is primary
    digest = hashlib.sha256(HTML).hexdigest()
    parent = artifact_store.root / "_blobs" / digest[:2]
    assert not (parent / f"{digest}.gz").exists()
    assert not parent.exists() or not any(
        path.name.endswith(".tmp") for path in parent.iterdir()
    )
    assert _count(artifact_store, "artifact_blobs") == 0


@pytest.mark.parametrize("secure", [False, True])
def test_os_link_effect_then_baseexception_is_reconciled_without_orphan(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
) -> None:
    if secure and (os.name != "posix" or os.link not in os.supports_dir_fd):
        pytest.skip("secure publication reconciliation requires POSIX dir_fd")
    primary = KeyboardInterrupt("os.link interrupted after effect")
    original_link = os.link
    interrupted = False

    def link_then_interrupt(source, target, *args, **kwargs):
        nonlocal interrupted
        result = original_link(source, target, *args, **kwargs)
        if not interrupted:
            interrupted = True
            raise primary
        return result

    monkeypatch.setattr(os, "link", link_then_interrupt)
    supported_dir_fd = set(os.supports_dir_fd)
    supported_dir_fd.discard(original_link)
    if secure:
        supported_dir_fd.add(link_then_interrupt)
    monkeypatch.setattr(os, "supports_dir_fd", supported_dir_fd)

    with pytest.raises(KeyboardInterrupt) as caught:
        _store_html(artifact_store)

    assert caught.value is primary
    assert interrupted
    digest = hashlib.sha256(HTML).hexdigest()
    parent = artifact_store.root / "_blobs" / digest[:2]
    assert not (parent / f"{digest}.gz").exists()
    assert not parent.exists() or not any(
        path.name.endswith(".tmp") for path in parent.iterdir()
    )
    assert _count(artifact_store, "artifact_blobs") == 0


@pytest.mark.parametrize("secure", [False, True])
def test_quarantine_rename_effect_then_baseexception_restores_original(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
) -> None:
    if secure and (
        os.name != "posix"
        or not all(
            operation in os.supports_dir_fd
            for operation in (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
        )
    ):
        pytest.skip("secure quarantine reconciliation requires POSIX dir_fd")
    stored, target = _make_unreferenced_blob(artifact_store)
    original_bytes = target.read_bytes()
    primary = KeyboardInterrupt("rename interrupted after effect")
    original_transfer = Storage._rename_directory_no_replace
    interrupted = False

    def rename_then_interrupt(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal interrupted
        result = original_transfer(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if not interrupted:
            interrupted = True
            raise primary
        return result

    monkeypatch.setattr(
        Storage,
        "_rename_directory_no_replace",
        staticmethod(rename_then_interrupt),
    )
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())

    with pytest.raises(KeyboardInterrupt) as caught:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert caught.value is primary
    assert interrupted
    assert target.read_bytes() == original_bytes
    assert artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()
    assert list(target.parent.glob(".web-listening-rollback-*")) == []


@pytest.mark.parametrize(
    ("columns", "values"),
    [
        (("manifest_version",), ("acquisition-manifest.v0",)),
        (("source_run_id",), ("INVALID-RUN",)),
        (("retrieved_at",), ("2026-08-21T12:00:00+00:00",)),
        (("http_status",), (304,)),
        (("wire_encoding",), ("",)),
        (("content_encoding",), ("Identity",)),
    ],
)
def test_loaded_observation_revalidates_exact_frozen_contract_fields(
    artifact_store: ArtifactStore,
    columns: tuple[str, ...],
    values: tuple[object, ...],
) -> None:
    stored = _store_html(artifact_store)
    assignments = ", ".join(f"{column} = ?" for column in columns)
    artifact_store.storage.conn.execute(
        f"UPDATE artifact_observations SET {assignments} WHERE artifact_id = ?",
        (*values, stored.observation.artifact_id),
    )
    if "manifest_version" in columns or "source_run_id" in columns:
        artifact_store.storage.conn.execute(
            f"UPDATE artifact_versions SET {assignments} WHERE version_id = ?",
            (*values, stored.version.version_id),
        )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "observation.contract_invalid"


def test_loaded_observation_revalidates_mime_magic(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    artifact_store.storage.conn.execute(
        "UPDATE artifact_versions SET mime_type = 'application/pdf' WHERE version_id = ?",
        (stored.version.version_id,),
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_observations SET mime_type = 'application/pdf' WHERE artifact_id = ?",
        (stored.observation.artifact_id,),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "mime.magic_mismatch"


def test_loaded_lineage_rejects_a_global_cycle(
    artifact_store: ArtifactStore,
) -> None:
    first = _store_html(artifact_store)
    second = _store_html(
        artifact_store,
        entity_bytes=b"<!doctype html><html><body>cycle child</body></html>",
        parent_artifact_id=first.observation.artifact_id,
    )
    artifact_store.storage.conn.execute(
        "INSERT INTO artifact_lineage VALUES (?, ?, ?, ?, ?, ?)",
        (
            artifact_lineage_id(
                artifact_id=first.observation.artifact_id,
                relation="parent",
                related_artifact_id=second.observation.artifact_id,
                ordinal=0,
            ),
            first.observation.artifact_id,
            "parent",
            second.observation.artifact_id,
            0,
            RETRIEVED_AT,
        ),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(first.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"


@pytest.mark.parametrize("simulated_os_byte", [0, 3, 13])
def test_gzip_storage_normalizes_platform_os_header_byte(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    simulated_os_byte: int,
) -> None:
    original_compress = gzip.compress

    def platform_compress(*args, **kwargs):
        value = bytearray(original_compress(*args, **kwargs))
        value[9] = simulated_os_byte
        return bytes(value)

    monkeypatch.setattr(gzip, "compress", platform_compress)

    stored = _store_html(artifact_store)
    stored_bytes = artifact_store.resolve_blob_path(stored.blob.sha256).read_bytes()

    assert stored_bytes[9] == 255
    assert gzip.decompress(stored_bytes) == HTML


@pytest.mark.parametrize("secure", [False, True])
def test_retention_candidate_transfer_never_replaces_a_race_victim(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure retention transfer requires native POSIX dir_fd")
    stored, target = _make_unreferenced_blob(artifact_store)
    victim = b"candidate transfer race victim"
    original_transfer = Storage._rename_directory_no_replace
    transfer_called = False

    def collide(source, destination, *, src_dir_fd=None, dst_dir_fd=None):
        nonlocal transfer_called
        transfer_called = True
        if dst_dir_fd is None:
            Path(destination).write_bytes(victim)
        else:
            descriptor = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=dst_dir_fd,
            )
            try:
                os.write(descriptor, victim)
            finally:
                os.close(descriptor)
        return original_transfer(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )

    monkeypatch.setattr(
        Storage,
        "_rename_directory_no_replace",
        staticmethod(collide),
    )
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert error.value.reason_code == "blob.path_changed"
    assert transfer_called
    assert artifact_store.read_blob(stored.blob.sha256) == HTML
    candidates = list(target.parent.glob(".web-listening-rollback-*/candidate"))
    assert len(candidates) == 1
    assert candidates[0].read_bytes() == victim


@pytest.mark.parametrize("secure", [False, True])
def test_retention_uncertain_candidate_transfer_restores_original_and_victim(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure retention transfer requires native POSIX dir_fd")
    stored, target = _make_unreferenced_blob(artifact_store)
    primary = KeyboardInterrupt("candidate transfer interrupted after effect")
    victim = b"post-transfer target race victim"
    original_transfer = Storage._rename_directory_no_replace
    transfer_calls = 0

    def interrupt_after_effect(
        source,
        destination,
        *,
        src_dir_fd=None,
        dst_dir_fd=None,
    ):
        nonlocal transfer_calls
        transfer_calls += 1
        result = original_transfer(
            source,
            destination,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
        )
        if transfer_calls == 1:
            if src_dir_fd is None:
                Path(source).write_bytes(victim)
            else:
                descriptor = os.open(
                    source,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=src_dir_fd,
                )
                try:
                    os.write(descriptor, victim)
                finally:
                    os.close(descriptor)
            raise primary
        return result

    monkeypatch.setattr(
        Storage,
        "_rename_directory_no_replace",
        staticmethod(interrupt_after_effect),
    )
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())

    with pytest.raises(KeyboardInterrupt) as caught:
        artifact_store.prune_unreferenced_blob(stored.blob.sha256)

    assert caught.value is primary
    assert transfer_calls >= 2
    assert artifact_store.read_blob(stored.blob.sha256) == HTML
    preserved = [
        path
        for path in target.parent.glob(".web-listening-rollback-*/*")
        if path.is_file()
    ]
    assert len(preserved) == 1
    assert preserved[0].read_bytes() == victim


@pytest.mark.parametrize(
    ("final_url", "filename"),
    [
        ("https://example.invalid/report.txt", "report.html"),
        ("https://example.invalid/report.html", "report.pdf"),
    ],
)
def test_final_url_and_explicit_filename_are_independent_mime_evidence(
    artifact_store: ArtifactStore,
    final_url: str,
    filename: str,
) -> None:
    with pytest.raises(ArtifactStoreError) as error:
        _store_html(
            artifact_store,
            source_identity=final_url,
            final_url=final_url,
            filename=filename,
        )

    assert error.value.reason_code == "mime.extension_mismatch"
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert not any(path.is_file() for path in artifact_store.root.rglob("*"))


def test_explicit_filename_evidence_is_persisted_and_replayed(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(
        artifact_store,
        source_identity="https://example.invalid/report.html",
        filename="downloaded-report.html",
    )

    assert stored.observation.filename == "downloaded-report.html"
    assert (
        artifact_store.replay_observation(
            stored.observation.artifact_id
        ).observation.filename
        == "downloaded-report.html"
    )


def test_loaded_explicit_filename_evidence_is_revalidated(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(
        artifact_store,
        source_identity="https://example.invalid/report.html",
        filename="downloaded-report.html",
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_observations SET filename = 'tampered.pdf' "
        "WHERE artifact_id = ?",
        (stored.observation.artifact_id,),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == "mime.extension_mismatch"


def test_fallback_leaf_postcheck_baseexception_closes_and_cleans_owned_temp(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = KeyboardInterrupt("fallback parent postcheck interrupted")
    original_verify = artifact_store._verify_pinned_parent
    original_open = os.open
    verify_calls = 0
    opened_temporary_descriptors: list[int] = []

    def record_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if os.fspath(path).endswith(".tmp"):
            opened_temporary_descriptors.append(descriptor)
        return descriptor

    def interrupt_postcheck(pinned) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 4:
            raise primary
        original_verify(pinned)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(artifact_store, "_verify_pinned_parent", interrupt_postcheck)

    with pytest.raises(KeyboardInterrupt) as caught:
        _store_html(artifact_store)

    assert caught.value is primary
    assert len(opened_temporary_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_temporary_descriptors[0])
    assert not any(
        path.name.endswith(".tmp") for path in artifact_store.root.rglob("*")
    )
    assert _count(artifact_store, "artifact_blobs") == 0


@pytest.mark.skipif(
    os.name != "posix",
    reason="fallback actual-parent rename coverage requires POSIX open-file semantics",
)
def test_fallback_leaf_cleanup_uses_opened_context_and_preserves_lexical_victim(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest = hashlib.sha256(HTML).hexdigest()
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    temporary_name = f".{digest}.{'1' * 32}.tmp"
    displaced = target.parent.with_name(f"{target.parent.name}-opened")
    victim = b"lexical replacement temp victim"
    original_verify = artifact_store._verify_pinned_parent
    verify_calls = 0

    def replace_parent_on_postcheck(pinned) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 4:
            try:
                pinned.target.parent.rename(displaced)
            except OSError as exc:
                pytest.skip(f"open parent replacement unavailable: {exc}")
            pinned.target.parent.mkdir()
            (pinned.target.parent / temporary_name).write_bytes(victim)
        original_verify(pinned)

    monkeypatch.setattr(secrets, "token_hex", lambda size: "1" * (size * 2))
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(
        artifact_store,
        "_verify_pinned_parent",
        replace_parent_on_postcheck,
    )

    with pytest.raises(ArtifactStoreError) as error:
        _store_html(artifact_store)

    assert error.value.reason_code == "blob.path_changed"
    assert (target.parent / temporary_name).read_bytes() == victim
    assert not (displaced / temporary_name).exists()
    assert _count(artifact_store, "artifact_blobs") == 0


def _url_of_length(length: int) -> str:
    prefix = "https://example.invalid/"
    return prefix + "u" * (length - len(prefix))


@pytest.mark.parametrize(
    "changes",
    [
        {"adapter_version": "1.0." + "1" * 61},
        {
            "requested_url": _url_of_length(2049),
            "redirect_chain": (
                {
                    "ordinal": 0,
                    "from_url": _url_of_length(2049),
                    "to_url": "https://example.invalid/reports/annual",
                    "http_status": 302,
                    "access_decision_id": "access-decision-1111111111111111",
                    "decision": "allow",
                },
            ),
        },
        {
            "source_identity": _url_of_length(2049),
            "requested_url": "https://example.invalid/short",
            "source_url": _url_of_length(2049),
            "final_url": "https://example.invalid/short",
        },
        {
            "final_url": _url_of_length(2049),
            "redirect_chain": (
                {
                    "ordinal": 0,
                    "from_url": "https://example.invalid/reports/annual",
                    "to_url": _url_of_length(2049),
                    "http_status": 302,
                    "access_decision_id": "access-decision-1111111111111111",
                    "decision": "allow",
                },
            ),
        },
    ],
)
def test_frozen_contract_bounds_reject_write_before_mutation(
    artifact_store: ArtifactStore,
    changes: dict[str, object],
) -> None:
    source_identity = str(
        changes.pop("source_identity", "https://example.invalid/reports/annual")
    )

    with pytest.raises(ArtifactStoreError):
        _store_html(artifact_store, source_identity=source_identity, **changes)

    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_versions") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert not any(path.is_file() for path in artifact_store.root.rglob("*"))


def test_frozen_contract_derived_from_bound_rejects_write_before_mutation(
    artifact_store: ArtifactStore,
) -> None:
    source = _store_html(artifact_store)
    related = [source.observation.artifact_id]
    related.extend(f"artifact-{index:024x}" for index in range(1000))
    counts_before = {
        table: _count(artifact_store, table)
        for table in ("artifact_blobs", "artifact_versions", "artifact_observations")
    }

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.store_observation(
            source_run_id=RUN_ID,
            normalized_source_identity=(
                f"urn:web-listening:derived:{source.observation.artifact_id}:markdown"
            ),
            entity_bytes=b"# oversized lineage\n",
            response_content_type="text/markdown",
            requested_url="https://example.invalid/derived.md",
            source_url="https://example.invalid/derived.md",
            final_url="https://example.invalid/derived.md",
            retrieved_at="2026-08-21T12:03:00Z",
            http_status=200,
            artifact_role="derived",
            parent_artifact_id=source.observation.artifact_id,
            source_artifact_id=source.observation.artifact_id,
            derived_from_artifact_ids=related,
        )

    assert error.value.reason_code == "lineage.invalid"
    assert {
        table: _count(artifact_store, table) for table in counts_before
    } == counts_before


@pytest.mark.parametrize(
    ("field", "value", "reason_code"),
    [
        (
            "adapter_version",
            "1.0." + "1" * 61,
            "observation.provenance_invalid",
        ),
        ("requested_url", _url_of_length(2049), "identity.url_invalid"),
        ("source_url", _url_of_length(2049), "identity.url_invalid"),
        ("final_url", _url_of_length(2049), "identity.url_invalid"),
    ],
)
def test_loaded_observation_enforces_frozen_contract_string_bounds(
    artifact_store: ArtifactStore,
    field: str,
    value: str,
    reason_code: str,
) -> None:
    stored = _store_html(artifact_store)
    artifact_store.storage.conn.execute(
        f"UPDATE artifact_observations SET {field} = ? WHERE artifact_id = ?",
        (value, stored.observation.artifact_id),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(stored.observation.artifact_id)

    assert error.value.reason_code == reason_code


def test_loaded_derived_from_count_enforces_frozen_contract_bound(
    artifact_store: ArtifactStore,
) -> None:
    source = _store_html(artifact_store)
    derived = _store_derived(artifact_store, source)
    observation_columns = """artifact_id, version_id, manifest_version,
        source_run_id, normalized_source_identity, requested_url, source_url,
        final_url, retrieved_at, http_status, mime_type, size_bytes, sha256,
        artifact_uri, wire_encoding, content_encoding, artifact_role,
        artifact_status, access_decision_id, adapter_id, adapter_version,
        redirect_chain_json, discovered_from_json, created_at"""
    dummy_ids = [f"artifact-{index + 1:024x}" for index in range(1000)]
    artifact_store.storage.conn.executemany(
        f"""INSERT INTO artifact_observations ({observation_columns})
            SELECT ?, version_id, manifest_version, ?, ?, requested_url,
                   source_url, final_url, retrieved_at, http_status, mime_type,
                   size_bytes, sha256, artifact_uri, wire_encoding,
                   content_encoding, artifact_role, artifact_status,
                   access_decision_id, adapter_id, adapter_version,
                   redirect_chain_json, discovered_from_json, created_at
            FROM artifact_observations WHERE artifact_id = ?""",
        (
            (
                dummy_id,
                f"source-run-bound-{index}",
                f"https://example.invalid/bound/{index}",
                source.observation.artifact_id,
            )
            for index, dummy_id in enumerate(dummy_ids)
        ),
    )
    artifact_store.storage.conn.executemany(
        "INSERT INTO artifact_lineage VALUES (?, ?, ?, ?, ?, ?)",
        (
            (
                artifact_lineage_id(
                    artifact_id=derived.observation.artifact_id,
                    relation="derived_from",
                    related_artifact_id=dummy_id,
                    ordinal=index + 1,
                ),
                derived.observation.artifact_id,
                "derived_from",
                dummy_id,
                index + 1,
                RETRIEVED_AT,
            )
            for index, dummy_id in enumerate(dummy_ids)
        ),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(derived.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"


def test_frozen_contract_string_bounds_accept_exact_limits(
    artifact_store: ArtifactStore,
) -> None:
    bounded_url = _url_of_length(2048)
    bounded_adapter_version = "1.0." + "1" * 60

    stored = _store_html(
        artifact_store,
        source_identity=bounded_url,
        adapter_version=bounded_adapter_version,
    )

    assert stored.observation.requested_url == bounded_url
    assert stored.observation.source_url == bounded_url
    assert stored.observation.final_url == bounded_url
    assert stored.observation.adapter_version == bounded_adapter_version


def test_frozen_contract_derived_from_bound_accepts_exact_limit(
    artifact_store: ArtifactStore,
) -> None:
    source = _store_html(artifact_store)
    related = [source.observation.artifact_id]
    related.extend(f"artifact-{index:024x}" for index in range(999))

    lineage = artifact_store._prepare_lineage(
        artifact_id="artifact-ffffffffffffffffffffffff",
        artifact_role="derived",
        parent_artifact_id=source.observation.artifact_id,
        source_artifact_id=source.observation.artifact_id,
        derived_from_artifact_ids=related,
        created_at=RETRIEVED_AT,
    )

    assert len([edge for edge in lineage if edge.relation == "derived_from"]) == 1000


@pytest.mark.parametrize("with_lexical_victim", [False, True])
def test_fallback_created_leaf_cleanup_uses_actual_opened_context_outside_root(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    with_lexical_victim: bool,
) -> None:
    digest = hashlib.sha256(HTML).hexdigest()
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    temporary_name = f".{digest}.{'2' * 32}.tmp"
    outside = artifact_store.root.parent / "outside-open-context"
    outside.mkdir()
    outside_temporary = outside / temporary_name
    lexical_temporary = target.parent / temporary_name
    victim = b"lexical replacement victim"
    primary = KeyboardInterrupt("fallback opened-context postcheck interrupted")
    original_open = os.open
    original_verify = artifact_store._verify_pinned_parent
    verify_calls = 0
    opened_descriptors: list[int] = []

    def open_outside(path, flags, *args, **kwargs):
        if Path(os.fspath(path)).name == temporary_name:
            descriptor = original_open(outside_temporary, flags, *args, **kwargs)
            opened_descriptors.append(descriptor)
            if with_lexical_victim:
                lexical_temporary.write_bytes(victim)
            return descriptor
        return original_open(path, flags, *args, **kwargs)

    def interrupt_postcheck(pinned) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 4:
            raise primary
        original_verify(pinned)

    monkeypatch.setattr(secrets, "token_hex", lambda size: "2" * (size * 2))
    monkeypatch.setattr(os, "open", open_outside)
    monkeypatch.setattr(os, "supports_dir_fd", set())
    monkeypatch.setattr(artifact_store, "_verify_pinned_parent", interrupt_postcheck)

    with pytest.raises(KeyboardInterrupt) as caught:
        _store_html(artifact_store)

    assert caught.value is primary
    assert len(opened_descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(opened_descriptors[0])
    assert not outside_temporary.exists()
    if with_lexical_victim:
        assert lexical_temporary.read_bytes() == victim
    else:
        assert not lexical_temporary.exists()
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0


@pytest.mark.skipif(
    os.name != "posix" or not Path("/proc/self/fd").is_dir(),
    reason="native symlink escape cleanup requires POSIX procfs descriptor paths",
)
@pytest.mark.parametrize("with_lexical_victim", [False, True])
def test_fallback_symlink_escape_cleans_owned_leaf_and_preserves_victim(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    with_lexical_victim: bool,
) -> None:
    digest = hashlib.sha256(HTML).hexdigest()
    target = artifact_store.root / "_blobs" / digest[:2] / f"{digest}.gz"
    temporary_name = f".{digest}.{'3' * 32}.tmp"
    displaced = target.parent.with_name(f"{target.parent.name}-pinned")
    outside = artifact_store.root.parent / "outside-symlink-context"
    outside.mkdir()
    outside_temporary = outside / temporary_name
    victim = b"post-symlink lexical victim"
    original_open = os.open
    swapped = False

    def escape_before_open(path, flags, *args, **kwargs):
        nonlocal swapped
        if not swapped and Path(os.fspath(path)).name == temporary_name:
            target.parent.rename(displaced)
            target.parent.symlink_to(outside, target_is_directory=True)
            swapped = True
            descriptor = original_open(path, flags, *args, **kwargs)
            if with_lexical_victim:
                target.parent.unlink()
                target.parent.mkdir()
                (target.parent / temporary_name).write_bytes(victim)
            return descriptor
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(secrets, "token_hex", lambda size: "3" * (size * 2))
    monkeypatch.setattr(os, "open", escape_before_open)
    monkeypatch.setattr(os, "supports_dir_fd", set())

    with pytest.raises(ArtifactStoreError) as error:
        _store_html(artifact_store)

    assert error.value.reason_code == "blob.path_changed"
    assert swapped
    assert not outside_temporary.exists()
    assert not (displaced / temporary_name).exists()
    if with_lexical_victim:
        assert (target.parent / temporary_name).read_bytes() == victim
    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_retention_second_verify_replacement_never_resurrects_a_bad_row(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
    interrupt: bool,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure second-verify replacement requires native POSIX dir_fd")
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    stored, target = _make_unreferenced_blob(artifact_store)
    replacement = b"second verify replacement victim"
    primary = KeyboardInterrupt("second verify interrupted")
    original_verify = artifact_store._verify_quarantined_blob
    verify_calls = 0

    def replace_before_second_verify(quarantine, blob) -> None:
        nonlocal verify_calls
        verify_calls += 1
        if verify_calls == 2:
            candidate = (
                quarantine.target.parent / quarantine.quarantine_name / "candidate"
            )
            candidate.unlink()
            candidate.write_bytes(replacement)
            if interrupt:
                raise primary
        return original_verify(quarantine, blob)

    monkeypatch.setattr(
        artifact_store,
        "_verify_quarantined_blob",
        replace_before_second_verify,
    )

    if interrupt:
        with pytest.raises(KeyboardInterrupt) as caught:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
        assert caught.value is primary
    else:
        with pytest.raises(ArtifactStoreError) as error:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
        assert error.value.reason_code == "blob.path_changed"

    assert verify_calls == 2
    row = artifact_store.storage.conn.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()
    if row is None:
        assert artifact_store.storage.conn.execute(
            "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
            (stored.blob.sha256,),
        ).fetchone()
    else:
        assert artifact_store.read_blob(stored.blob.sha256) == HTML
    preserved = [
        path
        for path in target.parent.rglob("*")
        if path.is_file() and path.read_bytes() == replacement
    ]
    assert len(preserved) == 1


@pytest.mark.parametrize(
    "tamper_case",
    [
        "blob_sha",
        "blob_uri",
        "version_id",
        "version_uri",
        "artifact_id",
        "observation_uri",
        "access_decision",
        "adapter_id",
        "adapter_version",
        "redirect_json",
        "lineage_id",
        "lineage_relation",
    ],
)
def test_loaded_non_text_identity_and_provenance_is_stably_rejected(
    artifact_store: ArtifactStore,
    tamper_case: str,
) -> None:
    source = _store_html(artifact_store)
    stored = (
        _store_derived(artifact_store, source)
        if tamper_case.startswith("lineage_")
        else source
    )
    connection = artifact_store.storage.conn
    artifact_id: object = stored.observation.artifact_id
    binary = sqlite3.Binary(b"not-text")

    if tamper_case == "blob_sha":
        digest = sqlite3.Binary(stored.blob.sha256.encode("ascii"))
        connection.execute(
            "UPDATE artifact_blobs SET sha256 = ? WHERE sha256 = ?",
            (digest, stored.blob.sha256),
        )
        connection.execute(
            "UPDATE artifact_versions SET sha256 = ? WHERE version_id = ?",
            (digest, stored.version.version_id),
        )
        connection.execute(
            "UPDATE artifact_observations SET sha256 = ? WHERE artifact_id = ?",
            (digest, stored.observation.artifact_id),
        )
    elif tamper_case == "blob_uri":
        connection.execute(
            "UPDATE artifact_blobs SET artifact_uri = ? WHERE sha256 = ?",
            (binary, stored.blob.sha256),
        )
    elif tamper_case == "version_id":
        version_id = sqlite3.Binary(stored.version.version_id.encode("ascii"))
        connection.execute(
            "UPDATE artifact_versions SET version_id = ? WHERE version_id = ?",
            (version_id, stored.version.version_id),
        )
        connection.execute(
            "UPDATE artifact_observations SET version_id = ? WHERE artifact_id = ?",
            (version_id, stored.observation.artifact_id),
        )
    elif tamper_case == "version_uri":
        connection.execute(
            "UPDATE artifact_versions SET artifact_uri = ? WHERE version_id = ?",
            (binary, stored.version.version_id),
        )
    elif tamper_case == "artifact_id":
        artifact_id = sqlite3.Binary(stored.observation.artifact_id.encode("ascii"))
        connection.execute(
            "UPDATE artifact_observations SET artifact_id = ? WHERE artifact_id = ?",
            (artifact_id, stored.observation.artifact_id),
        )
    elif tamper_case == "observation_uri":
        connection.execute(
            "UPDATE artifact_observations SET artifact_uri = ? WHERE artifact_id = ?",
            (binary, stored.observation.artifact_id),
        )
    elif tamper_case == "access_decision":
        connection.execute(
            "UPDATE artifact_observations SET access_decision_id = ? "
            "WHERE artifact_id = ?",
            (binary, stored.observation.artifact_id),
        )
    elif tamper_case == "adapter_id":
        connection.execute(
            "UPDATE artifact_observations SET adapter_id = ? WHERE artifact_id = ?",
            (binary, stored.observation.artifact_id),
        )
    elif tamper_case == "adapter_version":
        connection.execute(
            "UPDATE artifact_observations SET adapter_version = ? WHERE artifact_id = ?",
            (binary, stored.observation.artifact_id),
        )
    elif tamper_case == "redirect_json":
        connection.execute(
            "UPDATE artifact_observations SET redirect_chain_json = ? "
            "WHERE artifact_id = ?",
            (binary, stored.observation.artifact_id),
        )
    elif tamper_case == "lineage_id":
        connection.execute(
            "UPDATE artifact_lineage SET lineage_id = ? WHERE artifact_id = ? "
            "AND relation = 'source'",
            (binary, stored.observation.artifact_id),
        )
    else:
        connection.execute(
            "UPDATE artifact_lineage SET relation = ? WHERE artifact_id = ? "
            "AND relation = 'source'",
            (binary, stored.observation.artifact_id),
        )
    connection.commit()

    with pytest.raises(ArtifactStoreError):
        artifact_store.get_observation(artifact_id)


@pytest.mark.parametrize(
    ("component", "value"),
    [
        ("blob_sha", 7),
        ("artifact_id", 7),
        ("version_id", 7),
        ("access_decision_id", 7),
        ("adapter_id", 7),
        ("lineage_id", 7),
    ],
)
def test_loaded_numeric_text_shapes_are_stably_rejected(
    artifact_store: ArtifactStore,
    component: str,
    value: int,
) -> None:
    source = _store_html(artifact_store)
    stored = _store_derived(artifact_store, source)
    blob = stored.blob
    version = stored.version
    observation = stored.observation
    lineage = stored.lineage
    if component == "blob_sha":
        blob = replace(blob, sha256=value)
    elif component == "artifact_id":
        observation = replace(observation, artifact_id=value)
    elif component == "version_id":
        version = replace(version, version_id=value)
    elif component == "access_decision_id":
        observation = replace(observation, access_decision_id=value)
    elif component == "adapter_id":
        observation = replace(observation, adapter_id=value)
    else:
        lineage = (replace(lineage[0], lineage_id=value), *lineage[1:])

    with pytest.raises(ArtifactStoreError):
        artifact_store._validate_loaded_identity(
            replace(
                stored,
                blob=blob,
                version=version,
                observation=observation,
                lineage=tuple(lineage),
            )
        )


@pytest.mark.parametrize(
    "mutation",
    ["candidate_replacement", "same_inode_content", "ancestor_replacement"],
)
def test_non_effected_retention_commit_never_restores_unproved_bytes(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    monkeypatch.setattr(os, "supports_dir_fd", set())
    stored, target = _make_unreferenced_blob(artifact_store)
    real_connection = artifact_store.storage.conn
    primary = KeyboardInterrupt(f"non-effected retention commit: {mutation}")
    displaced_parent = target.parent.with_name(f"{target.parent.name}-mutated")
    mutation_error: OSError | None = None
    failed_once = False

    class CommitMutationConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            nonlocal failed_once, mutation_error
            if failed_once:
                return real_connection.commit()
            failed_once = True
            candidate = next(target.parent.glob(".web-listening-rollback-*/candidate"))
            if mutation == "candidate_replacement":
                candidate.unlink()
                candidate.write_bytes(b"candidate replacement")
            elif mutation == "same_inode_content":
                candidate.write_bytes(b"same inode replacement")
            else:
                try:
                    target.parent.rename(displaced_parent)
                    target.parent.mkdir()
                    target.write_bytes(b"ancestor replacement")
                except OSError as exc:
                    mutation_error = exc
            raise primary

    artifact_store.storage.conn = CommitMutationConnection()
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    finally:
        artifact_store.storage.conn = real_connection

    if mutation_error is not None:
        pytest.skip(f"ancestor replacement unavailable: {mutation_error}")
    assert caught.value is primary
    assert (
        real_connection.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )
    assert real_connection.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()


def test_gzip_replay_caps_incremental_output_at_declared_entity_size(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _store_html(artifact_store)
    path = artifact_store.resolve_blob_path(stored.blob.sha256)
    bomb = gzip.compress(b"x" * (len(HTML) + 1_000_000), mtime=0)
    path.write_bytes(bomb)
    artifact_store.storage.conn.execute(
        "UPDATE artifact_blobs SET stored_size_bytes = ? WHERE sha256 = ?",
        (len(bomb), stored.blob.sha256),
    )
    artifact_store.storage.conn.commit()
    calls: list[tuple[int, int]] = []
    real_decompressobj = zlib.decompressobj

    class TrackingDecoder:
        def __init__(self, *args, **kwargs):
            self._decoder = real_decompressobj(*args, **kwargs)

        def decompress(self, data, max_length=0):
            calls.append((len(data), max_length))
            return self._decoder.decompress(data, max_length)

        def flush(self, *args, **kwargs):
            return self._decoder.flush(*args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._decoder, name)

    monkeypatch.setattr(zlib, "decompressobj", TrackingDecoder)

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.read_blob(stored.blob.sha256)

    assert error.value.reason_code == "blob.corrupt"
    assert calls
    assert all(
        input_size <= 64 * 1024 and 0 < output_limit <= len(HTML) + 1
        for input_size, output_limit in calls
    )


@pytest.mark.parametrize(
    "invalid_stream",
    [
        lambda value: value[:-1],
        lambda value: value + b"trailing",
        lambda value: value + gzip.compress(b"second-member", mtime=0),
    ],
)
def test_gzip_replay_requires_one_complete_stream_without_trailing_bytes(
    artifact_store: ArtifactStore,
    invalid_stream,
) -> None:
    stored = _store_html(artifact_store)
    path = artifact_store.resolve_blob_path(stored.blob.sha256)
    invalid = invalid_stream(path.read_bytes())
    path.write_bytes(invalid)
    artifact_store.storage.conn.execute(
        "UPDATE artifact_blobs SET stored_size_bytes = ? WHERE sha256 = ?",
        (len(invalid), stored.blob.sha256),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.read_blob(stored.blob.sha256)

    assert error.value.reason_code == "blob.corrupt"


def test_write_sizes_are_bounded_before_any_mutation(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        immutable_artifacts_module,
        "MAX_PORTABLE_JSON_INTEGER",
        len(HTML) - 1,
        raising=False,
    )

    with pytest.raises(ArtifactStoreError):
        _store_html(artifact_store)

    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0
    assert not list(artifact_store.root.rglob("*.gz"))


@pytest.mark.parametrize(
    "size_field",
    ["entity_size_bytes", "stored_size_bytes", "observation_size_bytes"],
)
def test_loaded_sizes_reject_bool_even_when_it_equals_one(
    artifact_store: ArtifactStore,
    size_field: str,
) -> None:
    identity_store = ArtifactStore(
        artifact_store.storage,
        root=artifact_store.root / "identity",
        storage_encoding="identity",
    )
    stored = identity_store.store_observation(
        source_run_id=RUN_ID,
        normalized_source_identity="https://example.invalid/one.txt",
        entity_bytes=b"x",
        response_content_type="text/plain",
        requested_url="https://example.invalid/one.txt",
        source_url="https://example.invalid/one.txt",
        final_url="https://example.invalid/one.txt",
        filename="one.txt",
        retrieved_at=RETRIEVED_AT,
        http_status=200,
    )
    blob = stored.blob
    observation = stored.observation
    if size_field == "entity_size_bytes":
        blob = replace(blob, entity_size_bytes=True)
        observation = replace(observation, size_bytes=True)
    elif size_field == "stored_size_bytes":
        blob = replace(blob, stored_size_bytes=True)
    else:
        observation = replace(observation, size_bytes=True)

    with pytest.raises(ArtifactStoreError):
        identity_store._validate_loaded_identity(
            replace(stored, blob=blob, observation=observation)
        )


def test_database_size_text_is_not_coerced_during_load_or_read(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(artifact_store)
    binary_entity_size = sqlite3.Binary(str(len(HTML)).encode("ascii"))
    binary_stored_size = sqlite3.Binary(
        str(stored.blob.stored_size_bytes).encode("ascii")
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_blobs SET entity_size_bytes = ?, stored_size_bytes = ? "
        "WHERE sha256 = ?",
        (binary_entity_size, binary_stored_size, stored.blob.sha256),
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_observations SET size_bytes = ? WHERE artifact_id = ?",
        (binary_entity_size, stored.observation.artifact_id),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError):
        artifact_store.get_observation(stored.observation.artifact_id)
    with pytest.raises(ArtifactStoreError):
        artifact_store.read_blob(stored.blob.sha256)


def test_portable_size_boundary_is_exact(
    artifact_store: ArtifactStore,
) -> None:
    assert (
        artifact_store._validate_portable_size(MAX_PORTABLE_JSON_INTEGER)
        == MAX_PORTABLE_JSON_INTEGER
    )
    for invalid in (True, -1, MAX_PORTABLE_JSON_INTEGER + 1):
        with pytest.raises(ArtifactStoreError):
            artifact_store._validate_portable_size(invalid)


def test_redirect_ordinal_rejects_bool_on_write_without_mutation(
    artifact_store: ArtifactStore,
) -> None:
    with pytest.raises(ArtifactStoreError):
        _store_html(
            artifact_store,
            source_identity="https://example.invalid/report.html",
            requested_url="https://example.invalid/start.html",
            redirect_chain=(
                {
                    "ordinal": False,
                    "from_url": "https://example.invalid/start.html",
                    "to_url": "https://example.invalid/report.html",
                    "http_status": 301,
                    "access_decision_id": "access-decision-1111111111111111",
                    "decision": "allow",
                },
            ),
        )

    assert _count(artifact_store, "artifact_blobs") == 0
    assert _count(artifact_store, "artifact_observations") == 0


def test_redirect_ordinal_rejects_bool_on_load(
    artifact_store: ArtifactStore,
) -> None:
    stored = _store_html(
        artifact_store,
        source_identity="https://example.invalid/report.html",
        requested_url="https://example.invalid/start.html",
        redirect_chain=(
            {
                "ordinal": 0,
                "from_url": "https://example.invalid/start.html",
                "to_url": "https://example.invalid/report.html",
                "http_status": 301,
                "access_decision_id": "access-decision-1111111111111111",
                "decision": "allow",
            },
        ),
    )
    tampered = stored.observation.redirect_chain_json.replace(
        '"ordinal":0', '"ordinal":false'
    )
    artifact_store.storage.conn.execute(
        "UPDATE artifact_observations SET redirect_chain_json = ? "
        "WHERE artifact_id = ?",
        (tampered, stored.observation.artifact_id),
    )
    artifact_store.storage.conn.commit()

    with pytest.raises(ArtifactStoreError):
        artifact_store.get_observation(stored.observation.artifact_id)


def test_filename_length_boundary_is_symmetric_on_write_and_load(
    artifact_store: ArtifactStore,
) -> None:
    boundary = f"{'a' * 250}.html"
    overlong = f"{'a' * 251}.html"
    assert len(boundary) == 255
    assert len(overlong) == 256

    stored = _store_html(artifact_store, filename=boundary)
    assert stored.observation.filename == boundary

    blob_count = _count(artifact_store, "artifact_blobs")
    observation_count = _count(artifact_store, "artifact_observations")
    with pytest.raises(ArtifactStoreError):
        _store_html(
            artifact_store,
            source_run_id="source-run-20260821-filename-overlong",
            source_identity="https://example.invalid/overlong.html",
            filename=overlong,
        )
    assert _count(artifact_store, "artifact_blobs") == blob_count
    assert _count(artifact_store, "artifact_observations") == observation_count

    artifact_store.storage.conn.execute(
        "UPDATE artifact_observations SET filename = ? WHERE artifact_id = ?",
        (overlong, stored.observation.artifact_id),
    )
    artifact_store.storage.conn.commit()
    with pytest.raises(ArtifactStoreError):
        artifact_store.get_observation(stored.observation.artifact_id)


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("mutation", ["replace", "truncate", "ancestor"])
@pytest.mark.parametrize("stage", ["insert", "postcommit"])
def test_retention_compensation_holds_exact_canonical_bytes_through_commit(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
    mutation: str,
    stage: str,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure compensation race requires native POSIX dir_fd")
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())
    stored, target = _make_unreferenced_blob(artifact_store)
    real_connection = artifact_store.storage.conn
    primary = KeyboardInterrupt(f"compensation primary: {stage}/{mutation}/{secure}")
    replacement = f"compensation replacement: {stage}/{mutation}".encode()
    displaced_parent = target.parent.with_name(f"{target.parent.name}-compensation")
    compensating = False
    mutated = False
    mutation_error: OSError | None = None

    def mutate_canonical() -> None:
        nonlocal mutated, mutation_error
        if mutated:
            return
        mutated = True
        if mutation == "replace":
            target.unlink()
            target.write_bytes(replacement)
        elif mutation == "truncate":
            target.write_bytes(replacement)
        else:
            try:
                target.parent.rename(displaced_parent)
                target.parent.mkdir()
                target.write_bytes(replacement)
            except OSError as exc:
                mutation_error = exc

    class CompensationRaceConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def execute(self, statement, parameters=()):
            if (
                compensating
                and stage == "insert"
                and statement.lstrip().startswith("INSERT INTO artifact_blobs")
            ):
                mutate_canonical()
            return real_connection.execute(statement, parameters)

        def commit(self):
            if compensating and stage == "postcommit" and not mutated:
                real_connection.commit()
                mutate_canonical()
                return None
            return real_connection.commit()

    def interrupt_cleanup(quarantine) -> None:
        nonlocal compensating
        compensating = True
        raise primary

    artifact_store.storage.conn = CompensationRaceConnection()
    monkeypatch.setattr(artifact_store, "_delete_quarantined_blob", interrupt_cleanup)
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
    finally:
        artifact_store.storage.conn = real_connection

    if mutation_error is not None:
        pytest.skip(f"ancestor replacement unavailable: {mutation_error}")
    assert caught.value is primary
    assert mutated
    assert target.read_bytes() == replacement
    assert (
        real_connection.execute(
            "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
        ).fetchone()
        is None
    )
    assert real_connection.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()


def test_loaded_migrated_lineage_rejects_duplicate_derived_from_ids(
    artifact_store: ArtifactStore,
) -> None:
    source = _store_html(artifact_store)
    derived = _store_derived(artifact_store, source)
    connection = artifact_store.storage.conn
    connection.executescript(
        """
        ALTER TABLE artifact_lineage RENAME TO artifact_lineage_with_unique;
        CREATE TABLE artifact_lineage (
            lineage_id TEXT PRIMARY KEY,
            artifact_id TEXT NOT NULL,
            relation TEXT NOT NULL,
            related_artifact_id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );
        INSERT INTO artifact_lineage
        SELECT * FROM artifact_lineage_with_unique;
        DROP TABLE artifact_lineage_with_unique;
        """
    )
    duplicate_id = artifact_lineage_id(
        artifact_id=derived.observation.artifact_id,
        relation="derived_from",
        related_artifact_id=source.observation.artifact_id,
        ordinal=1,
    )
    connection.execute(
        "INSERT INTO artifact_lineage VALUES (?, ?, ?, ?, ?, ?)",
        (
            duplicate_id,
            derived.observation.artifact_id,
            "derived_from",
            source.observation.artifact_id,
            1,
            RETRIEVED_AT,
        ),
    )
    connection.commit()

    with pytest.raises(ArtifactStoreError) as error:
        artifact_store.get_observation(derived.observation.artifact_id)

    assert error.value.reason_code == "lineage.invalid"
    assert (
        connection.execute(
            "SELECT COUNT(*) FROM artifact_lineage "
            "WHERE artifact_id = ? AND relation = 'derived_from'",
            (derived.observation.artifact_id,),
        ).fetchone()[0]
        == 2
    )


@pytest.mark.parametrize("secure", [False, True])
@pytest.mark.parametrize("mutation", ["replace", "truncate", "ancestor"])
def test_compensation_guard_blocks_a_queued_legacy_reference_until_postaudit(
    artifact_store: ArtifactStore,
    monkeypatch: pytest.MonkeyPatch,
    secure: bool,
    mutation: str,
) -> None:
    required = (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
    if secure and (
        os.name != "posix"
        or not all(operation in os.supports_dir_fd for operation in required)
    ):
        pytest.skip("secure compensation writer race requires native POSIX dir_fd")
    if not secure:
        monkeypatch.setattr(os, "supports_dir_fd", set())

    stored, target = _make_unreferenced_blob(artifact_store)
    independent = Storage(artifact_store.storage.db_path)
    real_connection = artifact_store.storage.conn
    real_writer_connection = independent.conn
    primary = KeyboardInterrupt(f"guarded compensation: {mutation}/{secure}")
    replacement = f"queued writer replacement: {mutation}".encode()
    displaced_parent = target.parent.with_name(f"{target.parent.name}-queued-writer")
    writer_release = threading.Event()
    writer_attempting_insert = threading.Event()
    writer_finished = threading.Event()
    writer_failures: list[BaseException] = []
    compensating = False
    compensation_commits = 0
    compensation_verifications = 0
    mutated = False
    mutation_error: OSError | None = None

    def mutate_canonical() -> None:
        nonlocal mutated, mutation_error
        if mutated:
            return
        mutated = True
        if mutation == "replace":
            target.unlink()
            target.write_bytes(replacement)
        elif mutation == "truncate":
            target.write_bytes(replacement)
        else:
            try:
                target.parent.rename(displaced_parent)
                target.parent.mkdir()
                target.write_bytes(replacement)
            except OSError as exc:
                mutation_error = exc

    class CompensationConnection:
        def __getattr__(self, name):
            return getattr(real_connection, name)

        @property
        def in_transaction(self):
            return real_connection.in_transaction

        def commit(self):
            nonlocal compensation_commits
            if compensating and compensation_commits == 0:
                compensation_commits += 1
                writer_release.set()
                if not writer_attempting_insert.wait(timeout=5):
                    raise TimeoutError("legacy writer did not reach its insert")
                if writer_finished.wait(timeout=0.25):
                    raise AssertionError("legacy writer was not queued by compensation")
                real_connection.commit()
                mutate_canonical()
                return None
            return real_connection.commit()

    class WriterConnection:
        def __getattr__(self, name):
            return getattr(real_writer_connection, name)

        @property
        def in_transaction(self):
            return real_writer_connection.in_transaction

        def execute(self, statement, parameters=()):
            if statement.lstrip().startswith("INSERT INTO documents"):
                writer_attempting_insert.set()
            return real_writer_connection.execute(statement, parameters)

    original_verify = artifact_store._verify_pinned_canonical_blob

    def wait_for_writer_before_postaudit(canonical, blob) -> None:
        nonlocal compensation_verifications
        if compensating:
            compensation_verifications += 1
            if compensation_verifications == 3 and not writer_finished.wait(timeout=5):
                raise TimeoutError("legacy writer did not finish after compensation")
        original_verify(canonical, blob)

    def interrupt_cleanup(quarantine) -> None:
        nonlocal compensating
        compensating = True
        raise primary

    def add_legacy_reference() -> None:
        if not writer_release.wait(timeout=5):
            writer_failures.append(TimeoutError("writer was not released"))
            writer_finished.set()
            return
        try:
            independent.add_document(
                Document(
                    site_id=1,
                    title="queued compensation reference",
                    url="https://example.invalid/queued.pdf",
                    download_url="https://example.invalid/queued.pdf",
                    sha256=stored.blob.sha256,
                )
            )
        except BaseException as exc:
            writer_failures.append(exc)
            independent.conn.rollback()
        finally:
            writer_finished.set()

    writer = threading.Thread(target=add_legacy_reference)
    artifact_store.storage.conn = CompensationConnection()
    independent.conn = WriterConnection()
    monkeypatch.setattr(artifact_store, "_delete_quarantined_blob", interrupt_cleanup)
    monkeypatch.setattr(
        artifact_store,
        "_verify_pinned_canonical_blob",
        wait_for_writer_before_postaudit,
    )
    writer.start()
    try:
        with pytest.raises(KeyboardInterrupt) as caught:
            artifact_store.prune_unreferenced_blob(stored.blob.sha256)
        writer.join(timeout=5)
        document_count = real_writer_connection.execute(
            "SELECT COUNT(*) FROM documents"
        ).fetchone()[0]
    finally:
        artifact_store.storage.conn = real_connection
        independent.conn = real_writer_connection
        if writer.is_alive():
            writer_release.set()
            writer.join(timeout=5)
        independent.close()

    if mutation_error is not None:
        pytest.skip(f"ancestor replacement unavailable: {mutation_error}")
    assert caught.value is primary
    assert mutated
    assert target.read_bytes() == replacement
    assert writer_finished.is_set()
    assert len(writer_failures) == 1
    assert isinstance(writer_failures[0], sqlite3.IntegrityError)
    assert document_count == 0
    row = real_connection.execute(
        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (stored.blob.sha256,)
    ).fetchone()
    marker = real_connection.execute(
        "SELECT 1 FROM artifact_blob_retirements WHERE sha256 = ?",
        (stored.blob.sha256,),
    ).fetchone()
    if row is not None:
        assert marker is None
        assert artifact_store.read_blob(stored.blob.sha256) == HTML
    else:
        assert marker is not None
