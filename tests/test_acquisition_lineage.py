from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from web_listening.blocks.acquisition_evidence import _portable_source_path, persisted_acquisition_evidence
from web_listening.blocks.acquisition_gateway import (
    AcquisitionOutcome,
    GovernedAcquisitionGateway,
    LegacyCrawlerGateway,
    redact_persisted_value,
)
import web_listening.blocks.storage as storage_module
from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_crawler import TreeCrawler
from web_listening.contracts import AcquisitionAttempt as ContractAcquisitionAttempt
from web_listening.contracts import CaptureContent, CaptureError, CaptureRequest, CaptureResult
from web_listening.models import AcquisitionAttempt, CrawlRun, CrawlScope, FileObservation, PageSnapshot, Site


def _attempt(**changes) -> AcquisitionAttempt:
    now = datetime.now(timezone.utc)
    values = dict(
        attempt_id="attempt-1", request_id="request-1", scope_id=1, run_id=1,
        position=0, profile_id="profile", executor_id="web_http", executor_version="1.0.0",
        site_skill_id="skill", site_skill_version="1.0.0",
        site_skill_package_sha256="a" * 64, recipe_id="recipe",
        requested_url="https://example.com/a", final_url="https://example.com/a",
        requested_at=now, started_at=now, finished_at=now,
        classification="accepted", accepted=True, validation={"decision": "accepted"},
    )
    values.update(changes)
    attempt = AcquisitionAttempt(**values)
    if attempt.authority_mode != "governed":
        attempt.canonical_json = json.dumps(
            attempt.model_dump(mode="json", exclude={"canonical_json", "artifacts"}),
            sort_keys=True, separators=(",", ":"),
        )
        return attempt
    request = CaptureRequest(
        request_id=attempt.request_id, site_key="demo", site_skill_id="skill",
        site_skill_version="1.0.0", site_skill_digest="a" * 64, recipe_id="recipe",
        run_id=str(attempt.run_id), scope_id=str(attempt.scope_id),
        executor_id=attempt.executor_id, url=attempt.requested_url,
        requested_at=attempt.requested_at,
        metadata={"profile_id": attempt.profile_id, "fallback_position": attempt.position,
                  "authority_mode": attempt.authority_mode, "content_kind": attempt.content_kind,
                  "executor_version": attempt.executor_version,
                  "acquisition_fingerprint": attempt.acquisition_fingerprint,
                  "scope_fingerprint": None, "entrypoint": None,
                  "script_sha256": attempt.script_sha256, "required_capabilities": [],
                  "executor_capabilities": [], "requires_authorized_access": False,
                  "verification_rules": [], "resource_limits": {}, "quality_gates": {},
                  "scope_budgets": {}},
    )
    lineage = {field: getattr(request, field) for field in (
        "request_id", "site_key", "site_skill_id", "site_skill_version",
        "site_skill_digest", "recipe_id", "run_id", "scope_id", "executor_id",
    )}
    result = CaptureResult(
        **lineage, state="succeeded" if attempt.accepted else "failed",
        started_at=attempt.started_at or attempt.requested_at,
        finished_at=attempt.finished_at or attempt.started_at or attempt.requested_at,
        final_url=attempt.final_url,
        content=CaptureContent(media_type="text/html", text="content") if attempt.accepted else None,
        error=None if attempt.accepted else CaptureError(code=attempt.classification, message=attempt.reason or attempt.classification),
        metadata={"acquisition_classification": attempt.classification,
                  "acquisition_validation": redact_persisted_value(
                      attempt.model_dump(mode="json")["validation"])},
    )
    contract = ContractAcquisitionAttempt.model_validate_json(json.dumps({
        "attempt_id": attempt.attempt_id,
        "request": json.loads(request.model_dump_json()),
        "result": json.loads(result.model_dump_json()),
        "accepted": attempt.accepted,
        "acceptance_reason": attempt.reason or attempt.classification,
    }))
    attempt.canonical_json = contract.model_dump_json()
    return attempt


def test_legacy_database_opens_without_backfill_and_nullable_lineage(tmp_path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.executescript("""
        CREATE TABLE page_snapshots (id INTEGER PRIMARY KEY, scope_id INTEGER, page_id INTEGER,
          run_id INTEGER, captured_at TEXT, content_hash TEXT, raw_html TEXT, cleaned_html TEXT,
          content_text TEXT, markdown TEXT, fit_markdown TEXT, metadata_json TEXT, fetch_mode TEXT,
          final_url TEXT, status_code INTEGER, links TEXT);
        CREATE TABLE file_observations (id INTEGER PRIMARY KEY, scope_id INTEGER, run_id INTEGER,
          page_id INTEGER, file_id INTEGER, document_id INTEGER, discovered_url TEXT,
          download_url TEXT, tracked_local_path TEXT);
    """)
    connection.close()
    storage = Storage(db)
    assert "attempt_id" in {row["name"] for row in storage.conn.execute("PRAGMA table_info(page_snapshots)")}
    assert storage.list_acquisition_attempts(1, 1) == []
    storage.close()


def test_attempt_persistence_is_idempotent_and_conflicts_fail_closed(tmp_path):
    storage = Storage(tmp_path / "lineage.db")
    first = _attempt()
    assert storage.add_acquisition_attempt(first).attempt_id == first.attempt_id
    assert storage.add_acquisition_attempt(first).attempt_id == first.attempt_id
    conflict = first.model_copy(update={"classification": "blocked", "canonical_json": first.canonical_json + " "})
    with pytest.raises(ValueError, match="conflicting"):
        storage.add_acquisition_attempt(conflict)
    assert [item.classification for item in storage.list_acquisition_attempts(1, 1)] == ["accepted"]
    storage.close()


@pytest.mark.parametrize(("classification", "accepted", "reason", "valid"), [
    ("accepted", True, "", True),
    ("accepted", True, "accepted", True),
    ("accepted", False, "accepted", False),
    ("blocked", True, "accepted", False),
    ("accepted", True, "blocked", False),
    ("blocked", False, "blocked", True),
    ("timeout", False, "timeout", True),
    ("executor_error", False, "executor_error", True),
])
def test_attempt_semantic_acceptance_matrix_rejects_contradictions_before_mutation(
        tmp_path, classification, accepted, reason, valid):
    storage = Storage(tmp_path / "semantic-matrix.db")
    attempt = _attempt(classification=classification, accepted=accepted, reason=reason)
    if valid:
        assert storage.add_acquisition_attempt(attempt).accepted is accepted
    else:
        with pytest.raises(ValueError, match="accepted"):
            storage.add_acquisition_attempt(attempt)
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == int(valid)
    storage.close()


def test_rejected_attempt_reason_must_match_classification_before_mutation(tmp_path):
    storage = Storage(tmp_path / "rejected-semantic-conflict.db")
    attempt = _attempt(classification="not_found", accepted=False, reason="timeout")
    with pytest.raises(ValueError, match="classification or reason"):
        storage.add_acquisition_attempt(attempt)
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == 0
    storage.close()


@pytest.mark.parametrize(("field", "value"), [("position", 99), ("profile_id", "different-profile")])
def test_persistence_rejects_relational_authority_that_contradicts_canonical(field, value, tmp_path):
    storage = Storage(tmp_path / "semantic-conflict.db")
    attempt = _attempt(profile_id="profile")
    canonical = json.loads(attempt.canonical_json)
    canonical["request"]["metadata"].update({"profile_id": "profile", "fallback_position": 0})
    attempt.canonical_json = json.dumps(canonical)
    contradictory = attempt.model_copy(update={field: value})
    with pytest.raises(ValueError, match="conflicting canonical acquisition authority"):
        storage.add_acquisition_attempt(contradictory)
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == 0
    storage.close()


@pytest.mark.parametrize(("section", "field"), [
    *(("request", field) for field in (
        "acquisition_fingerprint", "scope_fingerprint", "profile_id", "authority_mode",
        "content_kind", "fallback_position", "executor_version", "entrypoint",
        "script_sha256", "required_capabilities", "executor_capabilities",
        "requires_authorized_access", "verification_rules", "resource_limits",
        "quality_gates", "scope_budgets")),
    ("result", "acquisition_classification"), ("result", "acquisition_validation"),
])
def test_governed_persistence_requires_complete_canonical_authority_before_mutation(
        tmp_path, section, field):
    storage = Storage(tmp_path / f"missing-{field}.db")
    attempt = _attempt()
    canonical = json.loads(attempt.canonical_json)
    canonical[section]["metadata"].pop(field)
    attempt.canonical_json = json.dumps(canonical)
    with pytest.raises(ValueError, match="lacks required"):
        storage.add_acquisition_attempt(attempt)
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == 0
    storage.close()


def test_snapshot_requires_accepted_same_run_scope_attempt(tmp_path):
    storage = Storage(tmp_path / "linkage.db")
    site = storage.add_site(Site(url="https://example.com", name="demo"))
    scope = storage.add_crawl_scope(CrawlScope(site_id=site.id, seed_url=site.url,
        allowed_origin=site.url))
    run = storage.add_crawl_run(CrawlRun(scope_id=scope.id))
    page = storage.upsert_tracked_page(scope_id=scope.id, canonical_url=site.url,
        depth=0, run_id=run.id)
    accepted = _attempt(scope_id=scope.id, run_id=run.id)
    storage.add_acquisition_attempt(accepted)
    snapshot = storage.add_page_snapshot(PageSnapshot(scope_id=scope.id, run_id=run.id,
        page_id=page.id, attempt_id=accepted.attempt_id, content_hash="hash"))
    assert snapshot.attempt_id == accepted.attempt_id
    rejected = _attempt(attempt_id="rejected", request_id="rejected", scope_id=scope.id,
        run_id=run.id, accepted=False, classification="blocked")
    storage.add_acquisition_attempt(rejected)
    with pytest.raises(ValueError, match="accepted acquisition attempt"):
        storage.add_page_snapshot(PageSnapshot(scope_id=scope.id, run_id=run.id,
            page_id=page.id, attempt_id=rejected.attempt_id, content_hash="other"))
    storage.close()


def test_page_snapshot_redacts_all_text_surfaces_and_recomputes_hash(tmp_path):
    storage = Storage(tmp_path / "page-text-redaction.db")
    storage.add_acquisition_attempt(_attempt())
    secret = "PAGE-TEXT-SECRET-CANARY"
    url = f"https://example.com/callback?token={secret}&ok=visible"
    surfaces = {
        "raw_html": f'<a href="{url}">raw</a>',
        "cleaned_html": f'<a href="{url}">clean</a>',
        "content_text": f"text {url}",
        "markdown": f"[markdown]({url})",
        "fit_markdown": f"[fit]({url})",
    }
    snapshot = storage.add_page_snapshot(PageSnapshot(
        scope_id=1, run_id=1, page_id=1, attempt_id="attempt-1",
        content_hash=hashlib.sha256(surfaces["fit_markdown"].encode()).hexdigest(),
        metadata_json={"hash_basis": "fit_markdown"}, **surfaces,
    ))
    row = dict(storage.conn.execute("SELECT * FROM page_snapshots").fetchone())
    exported = storage.list_page_snapshots_for_run(1, 1)[0].model_dump_json()
    for field in surfaces:
        assert secret not in row[field]
        assert "visible" in row[field]
    assert secret not in exported
    assert snapshot.content_hash == hashlib.sha256(snapshot.fit_markdown.encode()).hexdigest()
    assert snapshot.content_hash != hashlib.sha256(surfaces["fit_markdown"].encode()).hexdigest()

    long_text = "A" * 5000
    plain = storage.add_page_snapshot(PageSnapshot(
        scope_id=1, run_id=1, page_id=2, attempt_id="attempt-1",
        content_hash=hashlib.sha256(long_text.encode()).hexdigest(),
        raw_html=long_text, cleaned_html=long_text, content_text=long_text,
        markdown=long_text, fit_markdown=long_text,
        metadata_json={"hash_basis": "fit_markdown"},
    ))
    for field in surfaces:
        assert getattr(plain, field) == long_text
    assert plain.content_hash == hashlib.sha256(long_text.encode()).hexdigest()
    storage.close()


def test_governed_failed_attempt_preserves_null_final_url_and_redacts_host_path():
    now = datetime.now(timezone.utc)
    request = CaptureRequest(
        request_id="failed-request", site_key="demo", site_skill_id="skill",
        site_skill_version="1.0.0", site_skill_digest="a" * 64, recipe_id="recipe",
        run_id="1", scope_id="1", executor_id="web_http",
        url="https://example.com/a", requested_at=now,
    )
    result = CaptureResult(
        request_id=request.request_id, site_key=request.site_key,
        site_skill_id=request.site_skill_id, site_skill_version=request.site_skill_version,
        site_skill_digest=request.site_skill_digest, recipe_id=request.recipe_id,
        run_id=request.run_id, scope_id=request.scope_id, executor_id=request.executor_id,
        state="failed", started_at=now, finished_at=now, final_url=None,
        error=CaptureError(code="executor_exception",
                           message="failed reading /root/private/browser/profile.json"),
    )
    gateway = object.__new__(GovernedAcquisitionGateway)
    gateway.plan = type("Plan", (), {"profile_id": "profile", "acquisition_fingerprint": "b" * 64,
                                      "quality_gates": {"blocked_markers": ()}})()
    attempt = gateway._attempt(
        request, {"position": 0, "executor_version": "1.0.0", "script_sha256": None},
        "page", "executor_error", result,
    )
    canonical = json.loads(attempt.canonical_json)
    assert attempt.final_url is None
    assert canonical["result"]["final_url"] is None
    assert "/root/private/browser/profile.json" not in attempt.canonical_json
    assert canonical["result"]["error"]["code"] == "executor_exception"


def test_tracked_state_requires_typed_lineage_ownership(tmp_path):
    storage = Storage(tmp_path / "typed.db")
    page_attempt = _attempt(content_kind="page")
    document_attempt = _attempt(attempt_id="document", request_id="document", content_kind="document")
    storage.add_acquisition_attempt(page_attempt)
    storage.add_acquisition_attempt(document_attempt)
    with pytest.raises(ValueError, match="content_kind=page"):
        storage.add_page_snapshot(PageSnapshot(
            scope_id=1, run_id=1, page_id=1, attempt_id=document_attempt.attempt_id,
            content_hash="hash",
        ))
    with pytest.raises(ValueError, match="content_kind=document"):
        storage.add_file_observation(FileObservation(
            scope_id=1, run_id=1, page_id=1, file_id=1,
            attempt_id=page_attempt.attempt_id, discovered_url="https://example.com/a.pdf",
            download_url="https://example.com/a.pdf",
        ))
    storage.close()


def test_inline_artifact_admission_verifies_and_publishes_portable_path(tmp_path):
    storage = Storage(tmp_path / "artifact.db")
    attempt = _attempt()
    storage.add_acquisition_attempt(attempt)
    data = b"\x89PNG\r\n\x1a\nminimal-png-fixture"
    descriptor = {"kind": "screenshot", "mime_type": "image/png",
        "size_bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(),
        "data_base64": base64.b64encode(data).decode()}
    artifacts = storage.admit_inline_acquisition_artifacts(attempt.attempt_id, [descriptor])
    assert not artifacts[0].portable_path.startswith("/")
    assert (tmp_path / artifacts[0].portable_path).read_bytes() == data
    bad = {**descriptor, "sha256": "0" * 64}
    with pytest.raises(ValueError, match="SHA-256"):
        storage.admit_inline_acquisition_artifacts(attempt.attempt_id, [bad])
    assert not list((tmp_path / "acquisition_artifacts" / attempt.attempt_id).glob("*.tmp"))
    storage.close()


def test_textual_artifact_is_structurally_redacted_before_publication_and_row_commit(tmp_path):
    storage = Storage(tmp_path / "artifact-redaction.db")
    storage.add_acquisition_attempt(_attempt())
    original = b'{"headers":{"Authorization":"Bearer ARTIFACT-SECRET-CANARY"},"ok":"visible"}'
    descriptor = {
        "kind": "trace", "mime_type": "application/json", "size_bytes": len(original),
        "sha256": hashlib.sha256(original).hexdigest(),
        "data_base64": base64.b64encode(original).decode(),
    }
    artifact = storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))[0]
    published = (tmp_path / artifact.portable_path).read_bytes()
    row = dict(storage.conn.execute("SELECT * FROM acquisition_artifacts").fetchone())
    assert b"ARTIFACT-SECRET-CANARY" not in published
    assert b"visible" in published
    assert row["redaction_status"] == artifact.redaction_status == "structurally_redacted"
    assert row["size_bytes"] == len(published)
    assert row["sha256"] == hashlib.sha256(published).hexdigest()
    storage.close()


def test_raw_capture_html_embedded_fragment_credentials_are_redacted_with_truthful_metadata(tmp_path):
    storage = Storage(tmp_path / "artifact-fragment-redaction.db")
    storage.add_acquisition_attempt(_attempt())
    secret = "RAW-FRAGMENT-SECRET-CANARY"
    data = (
        '<html><a href="https://example.com/callback#access_token/' + secret
        + '&state=visible">one</a><script>window.next="https://example.com/callback#access_token='
        + secret + '&state=visible"</script></html>'
    ).encode()
    descriptor = {"kind": "raw_capture", "mime_type": "text/html", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    artifact = storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))[0]
    published = (tmp_path / artifact.portable_path).read_bytes()
    row = dict(storage.conn.execute("SELECT * FROM acquisition_artifacts").fetchone())
    assert secret.encode() not in published
    assert b"state=visible" in published
    assert row["size_bytes"] == artifact.size_bytes == len(published)
    assert row["sha256"] == artifact.sha256 == hashlib.sha256(published).hexdigest()
    assert row["redaction_status"] == artifact.redaction_status == "structurally_redacted"
    storage.close()


def test_unknown_attempt_artifact_rejected_before_filesystem_or_database_mutation(tmp_path):
    storage = Storage(tmp_path / "unknown.db")
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    with pytest.raises(ValueError, match="existing persisted attempt"):
        storage.admit_inline_acquisition_artifacts("unknown", (descriptor,))
    assert not (tmp_path / "acquisition_artifacts").exists()
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


@pytest.mark.parametrize("fault", ["fsync", "fdopen"])
def test_artifact_creation_faults_leave_no_temp_or_fd_leak(tmp_path, monkeypatch, fault):
    storage = Storage(tmp_path / f"{fault}.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    before = len(list(__import__("pathlib").Path("/proc/self/fd").iterdir()))
    if fault == "fsync":
        monkeypatch.setattr(storage_module.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("fsync")))
    else:
        monkeypatch.setattr(storage_module.os, "fdopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("fdopen")))
    with pytest.raises(OSError, match=fault):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    after = len(list(__import__("pathlib").Path("/proc/self/fd").iterdir()))
    assert after == before
    assert not list(tmp_path.rglob("*.tmp"))
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_inline_artifact_replay_is_conflict_safe_and_accepts_frozen_capture_sequence(tmp_path):
    storage = Storage(tmp_path / "artifact-replay.db")
    first = _attempt(attempt_id="failed-first", request_id="failed-first", accepted=False,
                     classification="blocked")
    second = _attempt(attempt_id="accepted-second", request_id="accepted-second", position=1)
    data1, data2 = b'{"event":"first"}', b'{"event":"second"}'
    def descriptor(data):
        return {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
                "data_base64": base64.b64encode(data).decode()}
    now = datetime.now(timezone.utc)
    result = CaptureResult(
        request_id="accepted-second", site_key="demo", site_skill_id="skill",
        site_skill_version="1.0.0", site_skill_digest="a" * 64, recipe_id="recipe",
        run_id="1", scope_id="1", executor_id="web_http", state="succeeded",
        started_at=now, finished_at=now, final_url="https://example.com/a", status_code=200,
        content=CaptureContent(media_type="text/html", text="accepted"),
        metadata={"inline_artifacts": [descriptor(data2)]})
    assert isinstance(result.metadata["inline_artifacts"], tuple)
    outcome = AcquisitionOutcome(None, result, object(), "accepted", ("blocked", "accepted"),
                                 True, (first, second), ((descriptor(data1),),
                                 tuple(result.metadata["inline_artifacts"])))
    TreeCrawler(storage=storage, acquisition_gateway=object())._persist_acquisition_outcome(
        outcome, requested_url="https://example.com/a", run_id=1, scope_id=1,
        content_kind="page")
    artifacts = storage.list_acquisition_attempts(1, 1)
    assert [len(item.artifacts) for item in artifacts] == [1, 1]
    storage.admit_inline_acquisition_artifacts(first.attempt_id, (descriptor(data1),))
    target = tmp_path / artifacts[0].artifacts[0].portable_path
    conflicting = descriptor(b'{"event":"other"}')
    with pytest.raises(ValueError, match="conflicting"):
        storage.admit_inline_acquisition_artifacts(first.attempt_id, (conflicting,))
    assert target.read_bytes() == data1
    storage.close()


@pytest.mark.parametrize("attempt_id", ["../escaped", "a/b", "a\\b", "/absolute", "C:drive", "bad.\n",
                                         "CON", "bad?", "name*"])
def test_inline_artifact_attempt_id_rejects_nonportable_components(tmp_path, attempt_id):
    storage = Storage(tmp_path / "unsafe.db")
    with pytest.raises(ValueError, match="portable"):
        storage.admit_inline_acquisition_artifacts(attempt_id, ({"unused": True},))
    storage.close()


def test_persistence_recomputes_redaction_for_relational_and_canonical_fields(tmp_path):
    storage = Storage(tmp_path / "redaction.db")
    secret_url = "https://example.com/a?token=top-secret&ok=visible"
    attempt = _attempt(requested_url=secret_url, final_url=secret_url,
                       classification="blocked", accepted=False,
                       reason="blocked", validation={"authorization": "Bearer abc",
                                                        "diagnostic": "password=hunter2"},
                       canonical_json=json.dumps({"token": "caller-lied"}))
    stored = storage.add_acquisition_attempt(attempt)
    raw = storage.conn.execute("SELECT * FROM acquisition_attempts").fetchone()
    serialized = json.dumps(dict(raw), sort_keys=True)
    assert "top-secret" not in serialized and "hunter2" not in serialized and "Bearer abc" not in serialized
    assert "caller-lied" not in serialized and "%5BREDACTED%5D" in stored.requested_url
    with pytest.raises(ValueError, match="non-null"):
        storage.add_page_snapshot(PageSnapshot(scope_id=1, run_id=1, page_id=1, content_hash="x"))
    storage.close()


def test_structural_redaction_handles_tuples_compound_keys_and_url_userinfo(tmp_path):
    storage = Storage(tmp_path / "structural-redaction.db")
    canaries = ("api-canary", "token-canary", "user-canary", "pass-canary")
    attempt = _attempt(
        requested_url="https://user-canary:pass-canary@example.com/a?APIKey=api-canary&ok=yes",
        final_url="https://example.com/a?session_token=token-canary",
        validation={"nested": ({"serviceCredentialValue": "token-canary"},),
                    "ordinary": ("visible",)}, authority_mode="legacy_compatibility",
    )
    storage.add_acquisition_attempt(attempt)
    relational = json.dumps(dict(storage.conn.execute("SELECT * FROM acquisition_attempts").fetchone()))
    exported = json.dumps(persisted_acquisition_evidence(storage, scope_id=1, run_id=1))
    for canary in canaries:
        assert canary not in relational
        assert canary not in exported
    assert "visible" in relational
    storage.close()


def test_url_fragment_credentials_are_redacted_on_relational_canonical_and_export_surfaces(tmp_path):
    storage = Storage(tmp_path / "fragment-redaction.db")
    secret_url = "https://example.com/callback#access_token=fragment-canary&state=visible"
    stored = storage.add_acquisition_attempt(_attempt(requested_url=secret_url, final_url=secret_url))
    relational = json.dumps(dict(storage.conn.execute("SELECT * FROM acquisition_attempts").fetchone()))
    exported = json.dumps(persisted_acquisition_evidence(storage, scope_id=1, run_id=1))
    assert "fragment-canary" not in relational + stored.canonical_json + exported
    assert "state=visible" in stored.requested_url
    storage.close()


def test_fragment_credential_carrier_key_is_fully_redacted_without_losing_ordinary_fragment(tmp_path):
    storage = Storage(tmp_path / "fragment-carrier-redaction.db")
    secret_url = "https://example.com/callback#access_token/FRAGMENT-SECRET-CANARY&state=visible"
    stored = storage.add_acquisition_attempt(_attempt(requested_url=secret_url, final_url=secret_url))
    relational = json.dumps(dict(storage.conn.execute("SELECT * FROM acquisition_attempts").fetchone()))
    exported = json.dumps(persisted_acquisition_evidence(storage, scope_id=1, run_id=1))
    assert "FRAGMENT-SECRET-CANARY" not in relational + stored.canonical_json + exported
    assert "state=visible" in stored.requested_url
    storage.close()


@pytest.mark.parametrize("mime,data", [
    ("image/png", b"not png"), ("image/jpeg", b"not jpeg"),
    ("image/webp", b"not webp"), ("application/zip", b"not zip"),
])
def test_inline_artifact_rejects_bytes_that_do_not_match_declared_mime(tmp_path, mime, data):
    storage = Storage(tmp_path / "mime.db")
    storage.add_acquisition_attempt(_attempt())
    descriptor = {"kind": "screenshot" if mime.startswith("image/") else "trace",
                  "mime_type": mime, "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    with pytest.raises(ValueError, match="declared MIME"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_persisted_canonical_attempt_validates_against_frozen_contract(tmp_path):
    storage = Storage(tmp_path / "canonical.db")
    stored = storage.add_acquisition_attempt(_attempt())
    ContractAcquisitionAttempt.model_validate_json(stored.canonical_json)
    evidence = persisted_acquisition_evidence(storage, scope_id=1, run_id=1)
    ContractAcquisitionAttempt.model_validate_json(json.dumps(evidence["attempts"][0]["canonical_attempt"]))
    storage.close()


def test_artifact_parent_replacement_cannot_escape_pinned_tree_or_commit_row(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "parent-race.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    outside = tmp_path / "outside"
    outside.mkdir()
    original_open = storage_module.os.open
    replaced = False

    def racing_open(path, flags, mode=0o777, *, dir_fd=None):
        nonlocal replaced
        fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if path == "attempt-1" and dir_fd is not None and not replaced:
            replaced = True
            parent = tmp_path / "acquisition_artifacts"
            moved = tmp_path / "pinned-original"
            parent.rename(moved)
            parent.symlink_to(outside, target_is_directory=True)
        return fd

    monkeypatch.setattr(storage_module.os, "open", racing_open)
    with pytest.raises(ValueError, match="changed during publication"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    assert not list(outside.rglob("*"))
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_pinned_directories_are_reverified_after_publication_before_commit(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "post-publication-parent-race.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    original_verify = storage._verify_pinned_artifact_directories
    calls = 0

    def replacing_verify(*args):
        nonlocal calls
        original_verify(*args)
        calls += 1
        if calls == 1:
            attempt_dir = tmp_path / "acquisition_artifacts" / "attempt-1"
            attempt_dir.rename(tmp_path / "detached-attempt")
            attempt_dir.mkdir()

    monkeypatch.setattr(storage, "_verify_pinned_artifact_directories", replacing_verify)
    with pytest.raises(ValueError, match="changed during publication"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    assert not (tmp_path / "detached-attempt" / "00-trace.json").exists()
    assert not (tmp_path / "acquisition_artifacts" / "attempt-1" / "00-trace.json").exists()
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_artifact_model_fault_after_temp_open_closes_fd_and_removes_exact_temp(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "model-fault.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    before = len(list(__import__("pathlib").Path("/proc/self/fd").iterdir()))
    monkeypatch.setattr(storage_module, "AcquisitionArtifact",
                        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("model fault")))
    with pytest.raises(RuntimeError, match="model fault"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    after = len(list(__import__("pathlib").Path("/proc/self/fd").iterdir()))
    assert after == before
    assert not list(tmp_path.rglob("*.tmp"))
    storage.close()


def test_first_temp_fstat_failure_closes_fd_and_preserves_named_replacement(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "first-fstat.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    replacement = b"replacement must survive"
    called = False

    original_stat = storage_module.os.stat

    def failing_stat(path, *args, **kwargs):
        nonlocal called
        if isinstance(path, int) and not called:
            called = True
            attempt_dir = tmp_path / "acquisition_artifacts" / "attempt-1"
            (attempt_dir / "replacement.bin").write_bytes(replacement)
            raise OSError("first descriptor stat")
        return original_stat(path, *args, **kwargs)

    before = len(list(__import__("pathlib").Path("/proc/self/fd").iterdir()))
    monkeypatch.setattr(storage_module.os, "stat", failing_stat)
    with pytest.raises(OSError, match="first descriptor stat"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    after = len(list(__import__("pathlib").Path("/proc/self/fd").iterdir()))
    assert after == before
    assert not list(tmp_path.rglob("*.tmp"))
    assert (tmp_path / "acquisition_artifacts" / "attempt-1" / "replacement.bin").read_bytes() == replacement
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_post_link_replacement_is_preserved_on_failed_commit(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "post-link-race.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    original_link = storage_module.os.link
    replacement = b"unrelated replacement bytes"

    def racing_link(source, target, **kwargs):
        original_link(source, target, **kwargs)
        parent_fd = kwargs["dst_dir_fd"]
        storage_module.os.unlink(target, dir_fd=parent_fd)
        fd = storage_module.os.open(
            target, storage_module.os.O_WRONLY | storage_module.os.O_CREAT | storage_module.os.O_EXCL,
            0o600, dir_fd=parent_fd,
        )
        try:
            storage_module.os.write(fd, replacement)
        finally:
            storage_module.os.close(fd)

    monkeypatch.setattr(storage_module.os, "link", racing_link)
    with pytest.raises(ValueError, match="conflicting acquisition artifact bytes"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    target = tmp_path / "acquisition_artifacts" / "attempt-1" / "00-trace.json"
    assert target.read_bytes() == replacement
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_named_target_replacement_during_verification_fails_without_false_digest(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "verify-replacement.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    original_stat = storage_module.os.stat
    replacement = b"replacement after descriptor open"
    replaced = False

    def racing_stat(path, *args, **kwargs):
        nonlocal replaced
        if path == "00-trace.json" and kwargs.get("dir_fd") is not None and not replaced:
            replaced = True
            parent_fd = kwargs["dir_fd"]
            storage_module.os.unlink(path, dir_fd=parent_fd)
            fd = storage_module.os.open(path, storage_module.os.O_WRONLY | storage_module.os.O_CREAT
                                        | storage_module.os.O_EXCL, 0o600, dir_fd=parent_fd)
            try:
                storage_module.os.write(fd, replacement)
            finally:
                storage_module.os.close(fd)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage_module.os, "stat", racing_stat)
    with pytest.raises(ValueError, match="changed during verification"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    target = tmp_path / "acquisition_artifacts" / "attempt-1" / "00-trace.json"
    assert target.read_bytes() == replacement
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_parent_replacement_after_target_verification_fails_before_commit(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "final-ordering-race.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    original_verify = storage._verify_existing_artifact
    calls = 0

    def replacing_verify(*args):
        nonlocal calls
        original_verify(*args)
        calls += 1
        if calls == 1:
            attempt_dir = tmp_path / "acquisition_artifacts" / "attempt-1"
            attempt_dir.rename(tmp_path / "detached-after-target-verify")
            attempt_dir.mkdir()

    monkeypatch.setattr(storage, "_verify_existing_artifact", replacing_verify)
    with pytest.raises(ValueError, match="changed during publication"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_attempt_directory_replacement_after_final_target_verifier_rolls_back_rows_and_owned_bytes(
        tmp_path, monkeypatch):
    storage = Storage(tmp_path / "final-directory-ordering-race.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    original_verify = storage._verify_existing_artifact
    calls = 0

    def replacing_after_final_verify(*args):
        nonlocal calls
        original_verify(*args)
        calls += 1
        if calls == 2:
            attempt_dir = tmp_path / "acquisition_artifacts" / "attempt-1"
            attempt_dir.rename(tmp_path / "invocation-owned-detached")
            attempt_dir.mkdir()

    monkeypatch.setattr(storage, "_verify_existing_artifact", replacing_after_final_verify)
    with pytest.raises(ValueError, match="changed during publication"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    assert calls == 2
    assert not (tmp_path / "invocation-owned-detached" / "00-trace.json").exists()
    assert not (tmp_path / "acquisition_artifacts" / "attempt-1" / "00-trace.json").exists()
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


def test_target_replacement_after_initial_verification_fails_before_commit(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "final-target-race.db")
    storage.add_acquisition_attempt(_attempt())
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    original_verify = storage._verify_existing_artifact
    replacement = b"replacement after initial verification"
    calls = 0

    def replacing_verify(parent_fd, target, artifact):
        nonlocal calls
        original_verify(parent_fd, target, artifact)
        calls += 1
        if calls == 1:
            storage_module.os.unlink(target, dir_fd=parent_fd)
            fd = storage_module.os.open(target, storage_module.os.O_WRONLY | storage_module.os.O_CREAT
                                        | storage_module.os.O_EXCL, 0o600, dir_fd=parent_fd)
            try:
                storage_module.os.write(fd, replacement)
            finally:
                storage_module.os.close(fd)

    monkeypatch.setattr(storage, "_verify_existing_artifact", replacing_verify)
    with pytest.raises(ValueError, match="conflicting acquisition artifact bytes"):
        storage.admit_inline_acquisition_artifacts("attempt-1", (descriptor,))
    target = tmp_path / "acquisition_artifacts" / "attempt-1" / "00-trace.json"
    assert target.read_bytes() == replacement
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_artifacts").fetchone()[0] == 0
    storage.close()


@pytest.mark.parametrize("basename", [
    "CON", "bad?", "name*", "trailing.", "trailing ", "control\x01.json", "COM1.txt",
])
def test_compatibility_source_paths_replace_nonportable_basenames_deterministically(basename):
    rendered = _portable_source_path("capture_attempt", basename)
    assert rendered.startswith("compatibility_inputs/capture_attempt/source-")
    assert rendered == _portable_source_path("capture_attempt", basename)
    assert basename not in rendered


def test_exact_run_chronology_drives_latest_evidence_not_accepted_attempt():
    storage = Storage(":memory:")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    blocked = _attempt(attempt_id="blocked", request_id="chain-a", position=0,
        requested_at=base, started_at=base, finished_at=base,
        accepted=False, classification="blocked")
    accepted = _attempt(attempt_id="accepted", request_id="chain-a", position=1,
        requested_at=base, started_at=base, finished_at=base, accepted=True)
    timeout = _attempt(attempt_id="timeout", request_id="chain-b", position=0,
        requested_at=base.replace(second=1), started_at=base.replace(second=1),
        finished_at=base.replace(second=1), accepted=False, classification="timeout")
    for attempt in (timeout, accepted, blocked):
        storage.add_acquisition_attempt(attempt)
    assert [item.attempt_id for item in storage.list_acquisition_attempts(1, 1)] == ["blocked", "accepted", "timeout"]
    evidence = persisted_acquisition_evidence(storage, scope_id=1, run_id=1)
    assert evidence["accepted_attempt_ids"] == ["accepted"]
    assert evidence["latest_attempt"]["attempt_id"] == "timeout"
    assert evidence["next_action"] == "review_probe_failure"
    storage.close()


def test_legacy_compatibility_replay_ignores_volatile_timestamps_but_conflicts_semantically(tmp_path):
    storage = Storage(tmp_path / "legacy-replay.db")
    first = storage.add_legacy_compatibility_attempt(
        scope_id=1, run_id=1, content_kind="page", identity="https://example.com/a")
    second = storage.add_legacy_compatibility_attempt(
        scope_id=1, run_id=1, content_kind="page", identity="https://example.com/a")
    assert second == first
    compatibility = json.loads(first.canonical_json)
    assert compatibility["schema_version"] == "acquisition-attempt-compatibility.v1"
    with pytest.raises(Exception):
        ContractAcquisitionAttempt.model_validate_json(first.canonical_json)
    storage.conn.execute("UPDATE acquisition_attempts SET content_kind='document' WHERE attempt_id=?", (first.attempt_id,))
    storage.conn.commit()
    with pytest.raises(ValueError, match="conflicting legacy"):
        storage.add_legacy_compatibility_attempt(
            scope_id=1, run_id=1, content_kind="page", identity="https://example.com/a")
    storage.close()


def test_inline_artifact_never_follows_or_removes_preexisting_symlink(tmp_path):
    storage = Storage(tmp_path / "symlink.db")
    attempt = _attempt()
    storage.add_acquisition_attempt(attempt)
    data = b'{"event":"trace"}'
    descriptor = {"kind": "trace", "mime_type": "application/json", "size_bytes": len(data),
                  "sha256": hashlib.sha256(data).hexdigest(),
                  "data_base64": base64.b64encode(data).decode()}
    outside = tmp_path / "outside"
    outside.write_bytes(b"keep")
    target_dir = tmp_path / "acquisition_artifacts" / attempt.attempt_id
    target_dir.mkdir(parents=True)
    target = target_dir / "00-trace.json"
    target.symlink_to(outside)
    with pytest.raises(ValueError, match="symlink|regular"):
        storage.admit_inline_acquisition_artifacts(attempt.attempt_id, (descriptor,))
    assert target.is_symlink() and outside.read_bytes() == b"keep"
    storage.close()


def test_legacy_exception_attempt_is_redacted_and_labeled():
    class Crawler:
        def fetch_page(self, *args, **kwargs):
            raise RuntimeError("token=top-secret")

    outcome = LegacyCrawlerGateway(Crawler(), fetch_mode="http", fetch_config_json={}).acquire(
        "https://example.com", run_id="1", scope_id="2")
    serialized = outcome.attempt_records[0].model_dump_json()
    assert "top-secret" not in serialized
    assert outcome.attempt_records[0].authority_mode == "legacy_runtime"
    ContractAcquisitionAttempt.model_validate_json(outcome.attempt_records[0].canonical_json)


@pytest.mark.parametrize("field", [
    "authority_mode", "content_kind", "fallback_position", "profile_id", "legacy_fetch_mode",
    "legacy_executor_label", "site_skill_lineage", "executor_version",
])
def test_legacy_runtime_requires_complete_canonical_authority_before_mutation(tmp_path, field):
    class Crawler:
        def fetch_page(self, *args, **kwargs):
            raise RuntimeError("expected")

    attempt = LegacyCrawlerGateway(Crawler(), fetch_mode="http", fetch_config_json={}).acquire(
        "https://example.com/", run_id="1", scope_id="2").attempt_records[0]
    canonical = json.loads(attempt.canonical_json)
    canonical["request"]["metadata"].pop(field)
    attempt.canonical_json = json.dumps(canonical)
    storage = Storage(tmp_path / f"legacy-missing-{field}.db")
    with pytest.raises(ValueError, match="lacks required"):
        storage.add_acquisition_attempt(attempt)
    assert storage.conn.execute("SELECT COUNT(*) FROM acquisition_attempts").fetchone()[0] == 0
    storage.close()
