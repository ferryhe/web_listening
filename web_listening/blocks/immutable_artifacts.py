"""Immutable canonical-entity storage bound to ``acquisition-manifest.v1``.

The store keeps four identities separate: content blobs, logical source
versions, run observations, and lineage edges.  SHA-256 is always computed over
the canonical response entity bytes supplied by the governed reader.  Optional
at-rest compression never changes that identity.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import secrets
import stat
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Concatenate, Mapping, ParamSpec, Sequence, TypeVar
from urllib.parse import unquote, urlsplit

from web_listening.blocks.acquisition_contract import (
    CONTRACT_VERSION,
    MAX_PORTABLE_JSON_INTEGER,
    artifact_id_for_identity,
)
from web_listening.contracts._protocol import validate_portable_relative_path
from web_listening.contracts.access_decision import _canonical_url
from web_listening.contracts.site_diagnostic import canonical_json


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_RUN_RE = re.compile(r"^source-run-[a-z0-9][a-z0-9._-]{0,63}$")
_DERIVED_IDENTITY_RE = re.compile(
    r"^urn:web-listening:derived:artifact-[0-9a-f]{24}:[a-z0-9][a-z0-9._-]{0,31}$"
)
_VERSION_ID_RE = re.compile(r"^version-[0-9a-f]{24}$")
_LINEAGE_ID_RE = re.compile(r"^lineage-[0-9a-f]{24}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact-[0-9a-f]{24}$")
_ACCESS_DECISION_RE = re.compile(r"^access-decision-[0-9a-f]{16}$")
_ADAPTER_ID_RE = re.compile(r"^[a-z][a-z0-9._-]{0,63}$")
_SEMVER_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")

_MIME_EXTENSIONS: dict[str, frozenset[str]] = {
    "text/html": frozenset({".html", ".htm"}),
    "application/xhtml+xml": frozenset({".xhtml", ".html", ".htm"}),
    "application/pdf": frozenset({".pdf"}),
    "application/zip": frozenset({".zip"}),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": frozenset(
        {".docx"}
    ),
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": frozenset(
        {".xlsx"}
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": frozenset(
        {".pptx"}
    ),
    "application/msword": frozenset({".doc"}),
    "application/vnd.ms-excel": frozenset({".xls"}),
    "application/vnd.ms-powerpoint": frozenset({".ppt"}),
    "application/json": frozenset({".json"}),
    "application/xml": frozenset({".xml"}),
    "text/xml": frozenset({".xml"}),
    "text/plain": frozenset({".txt"}),
    "text/markdown": frozenset({".md", ".markdown"}),
    "image/png": frozenset({".png"}),
    "image/jpeg": frozenset({".jpg", ".jpeg"}),
    "image/gif": frozenset({".gif"}),
}
_ZIP_MIME_PREFIXES = {
    "application/zip": None,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": b"word/",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": b"xl/",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": b"ppt/",
}
_OLE_MIMES = frozenset(
    {
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
    }
)
_OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ALLOWED_STORAGE_ENCODINGS = frozenset({"identity", "gzip"})
_REDIRECT_KEYS = frozenset(
    {
        "ordinal",
        "from_url",
        "to_url",
        "http_status",
        "access_decision_id",
        "decision",
    }
)
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DISCOVERY_KEYS = frozenset({"kind", "artifact_id", "source_url"})
_DISCOVERY_KINDS = frozenset({"seed", "search", "link", "crawler", "derived"})
_DEFAULT_ACCESS_DECISION_ID = "access-decision-0000000000000000"
MAX_FILENAME_LENGTH = 255
IMMUTABLE_ARTIFACT_STORE_VERSION = "immutable-artifact-store.v1"
_GZIP_INPUT_CHUNK_SIZE = 64 * 1024
_P = ParamSpec("_P")
_R = TypeVar("_R")


class ArtifactStoreError(ValueError):
    """Fail-closed artifact-store error with a stable machine reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"immutable artifact store rejected input ({reason_code})")


def _serialized_store_lifecycle(
    method: Callable[Concatenate[Any, _P], _R],
) -> Callable[Concatenate[Any, _P], _R]:
    """Keep transaction, cleanup, and final readback on one Storage turn."""

    @wraps(method)
    def serialized(self: Any, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        condition = self.storage._execution_transaction_condition
        thread_id = threading.get_ident()
        with condition:
            while (
                self.storage._execution_transaction_depth > 0
                and self.storage._execution_transaction_owner != thread_id
            ):
                condition.wait()
            return method(self, *args, **kwargs)

    return serialized


@dataclass(frozen=True, slots=True)
class ArtifactBlob:
    sha256: str
    artifact_uri: str
    storage_path: str
    entity_size_bytes: int
    stored_size_bytes: int
    storage_encoding: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactVersion:
    version_id: str
    manifest_version: str
    source_run_id: str
    normalized_source_identity: str
    sha256: str
    artifact_uri: str
    mime_type: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactObservation:
    artifact_id: str
    version_id: str
    manifest_version: str
    source_run_id: str
    normalized_source_identity: str
    requested_url: str
    source_url: str
    final_url: str
    filename: str
    retrieved_at: str
    http_status: int
    mime_type: str
    size_bytes: int
    sha256: str
    artifact_uri: str
    wire_encoding: str
    content_encoding: str
    artifact_role: str
    artifact_status: str
    access_decision_id: str
    adapter_id: str
    adapter_version: str
    redirect_chain_json: str
    discovered_from_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ArtifactLineage:
    lineage_id: str
    artifact_id: str
    relation: str
    related_artifact_id: str
    ordinal: int
    created_at: str


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    blob: ArtifactBlob
    version: ArtifactVersion
    observation: ArtifactObservation
    lineage: tuple[ArtifactLineage, ...]


@dataclass(frozen=True, slots=True)
class ReplayedArtifact:
    blob: ArtifactBlob
    version: ArtifactVersion
    observation: ArtifactObservation
    lineage: tuple[ArtifactLineage, ...]
    entity_bytes: bytes


@dataclass(frozen=True, slots=True)
class _PreparedArtifact:
    blob: ArtifactBlob
    version: ArtifactVersion
    observation: ArtifactObservation
    lineage: tuple[ArtifactLineage, ...]
    stored_bytes: bytes


@dataclass(slots=True)
class _QuarantineAttempt:
    ownership_acquired: bool = False
    filesystem_effect: bool = False


@dataclass(slots=True)
class _QuarantinedBlob:
    target: Path
    cleanup_root: Path
    relative: Path
    quarantine_name: str
    pinned_descriptor: int
    candidate_descriptor: int
    expected_identity: tuple[int, int]
    parent_descriptor: int | None = None
    quarantine_descriptor: int | None = None
    directory_pins: tuple[tuple[Path, int, tuple[int, int]], ...] = ()
    candidate_unlinked: bool = False


@dataclass(slots=True)
class _PinnedBlobParent:
    target: Path
    root: Path
    root_descriptor: int
    parent_descriptor: int
    root_identity: tuple[int, int]
    parent_identity: tuple[int, int]
    secure_dir_fd: bool
    directory_pins: tuple[tuple[Path, int, tuple[int, int]], ...] = ()


@dataclass(slots=True)
class _PinnedCanonicalBlob:
    parent: _PinnedBlobParent
    descriptor: int
    expected_identity: tuple[int, int]


def artifact_version_id(
    *,
    source_run_id: str,
    normalized_source_identity: str,
    sha256: str,
    manifest_version: str = CONTRACT_VERSION,
) -> str:
    """Return the version identity over the exact frozen #48 tuple."""
    artifact_id = artifact_id_for_identity(
        source_run_id=source_run_id,
        normalized_source_identity=normalized_source_identity,
        sha256=sha256,
        manifest_version=manifest_version,
    )
    return f"version-{artifact_id.removeprefix('artifact-')}"


def artifact_lineage_id(
    *, artifact_id: str, relation: str, related_artifact_id: str, ordinal: int
) -> str:
    identity = {
        "artifact_id": artifact_id,
        "ordinal": ordinal,
        "related_artifact_id": related_artifact_id,
        "relation": relation,
    }
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return f"lineage-{digest[:24]}"


def _canonical_gzip_bytes(entity_bytes: bytes) -> bytes:
    compressed = bytearray(gzip.compress(entity_bytes, compresslevel=9, mtime=0))
    if len(compressed) < 10 or compressed[:3] != b"\x1f\x8b\x08":
        raise ArtifactStoreError("blob.compression_verification_failed")
    compressed[9] = 255
    return bytes(compressed)


class ArtifactStore:
    """Content-addressed immutable storage over an existing :class:`Storage`."""

    def __init__(
        self,
        storage,
        *,
        root: str | Path,
        storage_encoding: str = "gzip",
        allowed_mime_types: Sequence[str] | None = None,
    ) -> None:
        if storage_encoding not in _ALLOWED_STORAGE_ENCODINGS:
            raise ArtifactStoreError("encoding.storage_invalid")
        self.storage = storage
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.storage_encoding = storage_encoding
        self.allowed_mime_types = frozenset(
            allowed_mime_types if allowed_mime_types is not None else _MIME_EXTENSIONS
        )
        if not self.allowed_mime_types or not self.allowed_mime_types <= set(
            _MIME_EXTENSIONS
        ):
            raise ArtifactStoreError("mime.unsupported")

    @_serialized_store_lifecycle
    def store_observation(
        self,
        *,
        source_run_id: str,
        normalized_source_identity: str,
        entity_bytes: bytes,
        response_content_type: str,
        requested_url: str,
        source_url: str,
        final_url: str,
        retrieved_at: str | datetime,
        http_status: int,
        wire_encoding: str = "identity",
        content_encoding: str = "identity",
        artifact_role: str = "source",
        artifact_status: str = "completed",
        access_decision_id: str = _DEFAULT_ACCESS_DECISION_ID,
        adapter_id: str = "web_http",
        adapter_version: str = "1.0.0",
        redirect_chain: Sequence[Mapping[str, Any]] = (),
        discovered_from: Mapping[str, Any] | None = None,
        parent_artifact_id: str | None = None,
        source_artifact_id: str | None = None,
        derived_from_artifact_ids: Sequence[str] = (),
        filename: str | None = None,
        manifest_version: str = CONTRACT_VERSION,
    ) -> StoredArtifact:
        """Atomically persist one completed canonical-entity observation.

        ``entity_bytes`` must be the exact response entity after wire/content
        transfer decoding.  The supplied encoding labels are lineage metadata;
        at-rest gzip is independent and deterministic.
        """
        prepared = self._prepare(
            source_run_id=source_run_id,
            normalized_source_identity=normalized_source_identity,
            entity_bytes=entity_bytes,
            response_content_type=response_content_type,
            requested_url=requested_url,
            source_url=source_url,
            final_url=final_url,
            retrieved_at=retrieved_at,
            http_status=http_status,
            wire_encoding=wire_encoding,
            content_encoding=content_encoding,
            artifact_role=artifact_role,
            artifact_status=artifact_status,
            access_decision_id=access_decision_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            redirect_chain=redirect_chain,
            discovered_from=discovered_from,
            parent_artifact_id=parent_artifact_id,
            source_artifact_id=source_artifact_id,
            derived_from_artifact_ids=derived_from_artifact_ids,
            filename=filename,
            manifest_version=manifest_version,
        )

        owns_transaction = (
            not self.storage.execution_transaction_owned_by_current_thread
        )
        try:
            if owns_transaction:
                self.storage.begin_execution_transaction()
            self._publish_blob(prepared.blob, prepared.stored_bytes)
            self._insert_metadata(prepared)
            if owns_transaction:
                self.storage.commit_execution_transaction()
        except BaseException:
            if self.storage.execution_transaction_owned_by_current_thread:
                try:
                    self.storage.rollback_execution_transaction()
                except BaseException:
                    pass
            raise
        return self.get_observation(prepared.observation.artifact_id)

    @_serialized_store_lifecycle
    def get_observation(self, artifact_id: str) -> StoredArtifact:
        observation_row = self.storage.conn.execute(
            "SELECT * FROM artifact_observations WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if observation_row is None:
            raise ArtifactStoreError("observation.not_found")
        version_row = self.storage.conn.execute(
            "SELECT * FROM artifact_versions WHERE version_id = ?",
            (observation_row["version_id"],),
        ).fetchone()
        blob_row = self.storage.conn.execute(
            "SELECT * FROM artifact_blobs WHERE sha256 = ?",
            (observation_row["sha256"],),
        ).fetchone()
        if version_row is None or blob_row is None:
            raise ArtifactStoreError("reference.dangling")
        lineage_rows = self.storage.conn.execute(
            """SELECT * FROM artifact_lineage
               WHERE artifact_id = ?
               ORDER BY relation, ordinal, lineage_id""",
            (artifact_id,),
        ).fetchall()
        stored = StoredArtifact(
            blob=self._blob_from_row(blob_row),
            version=self._version_from_row(version_row),
            observation=self._observation_from_row(observation_row),
            lineage=tuple(self._lineage_from_row(row) for row in lineage_rows),
        )
        self._validate_loaded_identity(stored)
        return stored

    @_serialized_store_lifecycle
    def replay_observation(self, artifact_id: str) -> ReplayedArtifact:
        stored = self.get_observation(artifact_id)
        entity_bytes = self.read_blob(stored.blob.artifact_uri)
        if len(entity_bytes) != stored.observation.size_bytes:
            raise ArtifactStoreError("blob.corrupt")
        return ReplayedArtifact(
            blob=stored.blob,
            version=stored.version,
            observation=stored.observation,
            lineage=stored.lineage,
            entity_bytes=entity_bytes,
        )

    @_serialized_store_lifecycle
    def resolve_blob_path(self, digest_or_uri: str) -> Path:
        digest = self._digest_from_reference(digest_or_uri)
        row = self.storage.conn.execute(
            "SELECT storage_path, storage_encoding FROM artifact_blobs WHERE sha256 = ?",
            (digest,),
        ).fetchone()
        if row is not None:
            relative = row["storage_path"]
            storage_encoding = row["storage_encoding"]
            if type(relative) is not str or type(storage_encoding) is not str:
                raise ArtifactStoreError("reference.type_invalid")
            self._validate_blob_storage_path(
                digest,
                relative,
                storage_encoding,
            )
            return self.root.joinpath(*relative.split("/"))
        legacy = self.storage.conn.execute(
            "SELECT canonical_path FROM document_blobs WHERE sha256 = ?", (digest,)
        ).fetchone()
        if legacy is None:
            raise ArtifactStoreError("blob.not_found")
        return Path(str(legacy["canonical_path"]))

    @_serialized_store_lifecycle
    def read_blob(self, digest_or_uri: str) -> bytes:
        digest = self._digest_from_reference(digest_or_uri)
        row = self.storage.conn.execute(
            "SELECT * FROM artifact_blobs WHERE sha256 = ?", (digest,)
        ).fetchone()
        path = self.resolve_blob_path(digest)
        if row is None:
            stored_bytes = self._read_regular_file(path)
            entity_bytes = stored_bytes
        else:
            storage_encoding = row["storage_encoding"]
            if type(storage_encoding) is not str:
                raise ArtifactStoreError("reference.type_invalid")
            entity_size = self._validate_portable_size(row["entity_size_bytes"])
            stored_size = self._validate_portable_size(row["stored_size_bytes"])
            stored_bytes = self._read_pinned_blob_file(path, expected_size=stored_size)
            entity_bytes = self._decode_stored_bytes(
                stored_bytes,
                storage_encoding,
                expected_entity_size=entity_size,
                expected_stored_size=stored_size,
            )
            if len(stored_bytes) != stored_size or len(entity_bytes) != entity_size:
                raise ArtifactStoreError("blob.corrupt")
        if hashlib.sha256(entity_bytes).hexdigest() != digest:
            raise ArtifactStoreError("blob.corrupt")
        return entity_bytes

    @_serialized_store_lifecycle
    def prune_unreferenced_blob(self, digest_or_uri: str) -> bool:
        """Delete exactly one currently unreachable new-store blob.

        Legacy document/blob, acquisition-artifact, version, and observation
        references all protect the bytes.  Retention is rejected while a
        governed execution transaction is active.
        """
        if self.storage.execution_transaction_active:
            raise ArtifactStoreError("retention.transaction_active")
        digest = self._digest_from_reference(digest_or_uri)
        connection = self.storage.conn
        row = None
        blob: ArtifactBlob | None = None
        target: Path | None = None
        quarantine: _QuarantinedBlob | None = None
        quarantine_attempt = _QuarantineAttempt()
        transaction_started = False
        try:
            self.storage.begin_execution_transaction()
            transaction_started = True
            row = connection.execute(
                "SELECT * FROM artifact_blobs WHERE sha256 = ?", (digest,)
            ).fetchone()
            if row is None or self._blob_is_referenced(digest):
                self.storage.rollback_execution_transaction()
                transaction_started = False
                return False
            connection.execute(
                "INSERT OR IGNORE INTO artifact_blob_retirements (sha256, retired_at) "
                "VALUES (?, ?)",
                (digest, self._utc_now()),
            )
            target = self.resolve_blob_path(digest)
            blob = self._blob_from_row(row)
            quarantine = self._quarantine_blob_file(
                target,
                expected_identity=self._regular_file_identity(target),
                attempt=quarantine_attempt,
            )
            if quarantine is None:
                raise ArtifactStoreError("blob.path_changed")
            try:
                self._verify_quarantined_blob(quarantine, blob)
            except BaseException as primary:
                connection.execute(
                    "DELETE FROM artifact_blobs WHERE sha256 = ?", (digest,)
                )
                self._finalize_failed_retention(digest, blob, quarantine)
                transaction_started = False
                quarantine = None
                raise primary
            connection.execute("DELETE FROM artifact_blobs WHERE sha256 = ?", (digest,))
            try:
                self._verify_quarantined_blob(quarantine, blob)
            except BaseException as primary:
                self._finalize_failed_retention(digest, blob, quarantine)
                transaction_started = False
                quarantine = None
                raise primary
            self.storage.commit_execution_transaction()
            transaction_started = False
            try:
                self._delete_quarantined_blob(quarantine)
            except BaseException as primary:
                if quarantine.candidate_unlinked:
                    try:
                        self._close_quarantine_descriptors(quarantine)
                    except BaseException:
                        pass
                    quarantine = None
                else:
                    try:
                        self._restore_quarantined_blob(quarantine)
                    except BaseException:
                        # The durable delete is safer than a row which could name
                        # an attacker replacement.  The original remains in its
                        # no-overwrite quarantine when restoration collides.
                        quarantine = None
                        raise primary
                    quarantine = None
                    canonical: _PinnedCanonicalBlob | None = None
                    try:
                        canonical = self._pin_canonical_blob(target, blob)
                        self.storage.begin_execution_transaction()
                        transaction_started = True
                        self._insert_blob_row(row)
                        self._verify_pinned_canonical_blob(canonical, blob)
                        self.storage.commit_execution_transaction()
                        transaction_started = False
                        self._verify_pinned_canonical_blob(canonical, blob)
                        self.storage.begin_execution_transaction()
                        transaction_started = True
                        connection.execute(
                            "DELETE FROM artifact_blob_retirements WHERE sha256 = ?",
                            (digest,),
                        )
                        self.storage.commit_execution_transaction()
                        transaction_started = False
                    except BaseException:
                        if (
                            transaction_started
                            and self.storage.execution_transaction_owned_by_current_thread
                        ):
                            try:
                                self.storage.rollback_execution_transaction()
                            except BaseException:
                                pass
                        transaction_started = False
                        row_exists = (
                            connection.execute(
                                "SELECT 1 FROM artifact_blobs WHERE sha256 = ?",
                                (digest,),
                            ).fetchone()
                            is not None
                        )
                        marker_exists = (
                            connection.execute(
                                "SELECT 1 FROM artifact_blob_retirements "
                                "WHERE sha256 = ?",
                                (digest,),
                            ).fetchone()
                            is not None
                        )
                        safe_live_row = False
                        if row_exists and not marker_exists and canonical is not None:
                            try:
                                self._verify_pinned_canonical_blob(canonical, blob)
                            except BaseException:
                                pass
                            else:
                                safe_live_row = True
                        if row_exists and not safe_live_row:
                            try:
                                self._retire_unrestorable_blob(digest)
                            except BaseException as retirement_failure:
                                if (
                                    connection.execute(
                                        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?",
                                        (digest,),
                                    ).fetchone()
                                    is not None
                                ):
                                    raise ArtifactStoreError(
                                        "retention.restore_failed"
                                    ) from retirement_failure
                        raise primary
                    finally:
                        if canonical is not None:
                            self._close_pinned_canonical_blob(canonical)
                raise primary
            quarantine = None
            return True
        except BaseException as primary:
            if (
                transaction_started
                and row is not None
                and blob is not None
                and target is not None
                and quarantine is None
                and (
                    quarantine_attempt.ownership_acquired
                    or quarantine_attempt.filesystem_effect
                )
            ):
                try:
                    self._verify_blob_file(target, blob)
                except BaseException:
                    connection.execute(
                        "DELETE FROM artifact_blobs WHERE sha256 = ?", (digest,)
                    )
                    self._finalize_failed_retention(digest, blob, None)
                    transaction_started = False
                    raise primary
            if (
                transaction_started
                and self.storage.execution_transaction_owned_by_current_thread
            ):
                try:
                    self.storage.rollback_execution_transaction()
                except BaseException:
                    pass
            if quarantine is not None and row is not None:
                durable_delete = (
                    connection.execute(
                        "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (digest,)
                    ).fetchone()
                    is None
                )
                if durable_delete:
                    try:
                        self._delete_quarantined_blob(quarantine)
                    except BaseException:
                        try:
                            self._close_quarantine_descriptors(quarantine)
                        except BaseException:
                            pass
                else:
                    try:
                        self._verify_quarantined_blob(quarantine, blob)
                        self._restore_quarantined_blob(quarantine)
                        quarantine = None
                        self._verify_blob_file(target, blob)
                    except BaseException:
                        if quarantine is not None:
                            try:
                                self._close_quarantine_descriptors(quarantine)
                            except BaseException:
                                pass
                        quarantine = None
                        try:
                            self._retire_unrestorable_blob(digest)
                        except BaseException as retirement_failure:
                            if (
                                connection.execute(
                                    "SELECT 1 FROM artifact_blobs WHERE sha256 = ?",
                                    (digest,),
                                ).fetchone()
                                is not None
                            ):
                                raise ArtifactStoreError(
                                    "retention.restore_failed"
                                ) from retirement_failure
            raise

    def _finalize_failed_retention(
        self,
        digest: str,
        blob: ArtifactBlob,
        quarantine: _QuarantinedBlob | None,
    ) -> None:
        """Reconcile a staged delete without ever retaining an unsafe blob row."""
        try:
            self.storage.commit_execution_transaction()
        except BaseException:
            pass
        if self.storage.execution_transaction_owned_by_current_thread:
            try:
                self.storage.rollback_execution_transaction()
            except BaseException:
                pass
        safe_state = (
            self.storage.conn.execute(
                "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (digest,)
            ).fetchone()
            is None
        )
        if not safe_state and quarantine is not None:
            try:
                self._verify_quarantined_blob(quarantine, blob)
                original_bytes = self._verify_blob_descriptor(
                    quarantine.candidate_descriptor,
                    blob,
                )
                self._publish_blob(
                    blob,
                    original_bytes,
                    allow_missing_existing_row=True,
                )
                self._verify_blob_file(quarantine.target, blob)
            except BaseException:
                pass
            else:
                safe_state = True
        if not safe_state:
            row_exists = (
                self.storage.conn.execute(
                    "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (digest,)
                ).fetchone()
                is not None
            )
            if row_exists:
                try:
                    self._retire_unrestorable_blob(digest)
                except BaseException as retirement_failure:
                    raise ArtifactStoreError(
                        "retention.restore_failed"
                    ) from retirement_failure
        if quarantine is not None:
            try:
                self._close_quarantine_descriptors(quarantine)
            except BaseException:
                pass

    def _quarantine_blob_file(
        self,
        target: Path,
        *,
        expected_identity: tuple[int, int],
        attempt: _QuarantineAttempt,
    ) -> _QuarantinedBlob | None:
        anchored_target, anchored_root = self.storage._anchored_execution_path(
            target, self.root
        )
        relative = anchored_target.relative_to(anchored_root)
        supports_secure_dir_fd = all(
            operation in os.supports_dir_fd
            for operation in (os.open, os.stat, os.rename, os.mkdir, os.rmdir, os.link)
        )
        with self.storage._execution_cleanup_lock:
            try:
                if supports_secure_dir_fd:
                    return self._quarantine_blob_file_secure(
                        anchored_target,
                        anchored_root,
                        relative,
                        expected_identity,
                        attempt,
                    )
                return self._quarantine_blob_file_fallback(
                    anchored_target,
                    anchored_root,
                    relative,
                    expected_identity,
                    attempt,
                )
            except ArtifactStoreError:
                raise
            except OSError as exc:
                raise ArtifactStoreError("blob.path_changed") from exc

    def _quarantine_blob_file_fallback(
        self,
        target: Path,
        cleanup_root: Path,
        relative: Path,
        expected_identity: tuple[int, int],
        attempt: _QuarantineAttempt,
    ) -> _QuarantinedBlob | None:
        pinned_descriptor: int | None = None
        candidate_descriptor: int | None = None
        quarantine = target.parent / self.storage._quarantine_name()
        candidate = quarantine / "candidate"
        moved = False
        failure_active = False
        try:
            pinned_descriptor, opened = self.storage._open_anchored_execution_path(
                target, cleanup_root
            )
            if (opened.st_dev, opened.st_ino) != expected_identity:
                return None
            attempt.ownership_acquired = True
            quarantine.mkdir(mode=0o700)
            quarantine_info = quarantine.lstat()
            if not stat.S_ISDIR(quarantine_info.st_mode) or quarantine.is_symlink():
                raise ArtifactStoreError("retention.quarantine_invalid")
            try:
                self.storage._rename_directory_no_replace(target, candidate)
                moved = True
                attempt.filesystem_effect = True
            except BaseException as primary:
                try:
                    candidate_info = candidate.stat(follow_symlinks=False)
                    moved = (
                        stat.S_ISREG(candidate_info.st_mode)
                        and (
                            candidate_info.st_dev,
                            candidate_info.st_ino,
                        )
                        == expected_identity
                    )
                except OSError:
                    moved = False
                if moved:
                    attempt.filesystem_effect = True
                    try:
                        try:
                            target.stat(follow_symlinks=False)
                        except FileNotFoundError:
                            pass
                        else:
                            self.storage._rename_directory_no_replace(
                                target,
                                quarantine / "replacement",
                            )
                        self._restore_path_no_replace(candidate, target)
                        moved = False
                        try:
                            quarantine.rmdir()
                        except OSError:
                            pass
                    except BaseException:
                        pass
                raise primary
            candidate_descriptor, candidate_opened = (
                self.storage._open_anchored_execution_path(candidate, cleanup_root)
            )
            if (candidate_opened.st_dev, candidate_opened.st_ino) != expected_identity:
                self._restore_path_no_replace(candidate, target)
                moved = False
                os.close(candidate_descriptor)
                candidate_descriptor = None
                quarantine.rmdir()
                os.close(pinned_descriptor)
                pinned_descriptor = None
                return None
            return _QuarantinedBlob(
                target=target,
                cleanup_root=cleanup_root,
                relative=relative,
                quarantine_name=quarantine.name,
                pinned_descriptor=pinned_descriptor,
                candidate_descriptor=candidate_descriptor,
                expected_identity=expected_identity,
            )
        except BaseException:
            failure_active = True
            if moved and not target.exists():
                try:
                    self._restore_path_no_replace(candidate, target)
                    moved = False
                    quarantine.rmdir()
                except BaseException:
                    pass
            raise
        finally:
            if failure_active or not moved:
                for descriptor in (candidate_descriptor, pinned_descriptor):
                    if descriptor is not None:
                        try:
                            os.close(descriptor)
                        except BaseException:
                            pass

    def _quarantine_blob_file_secure(
        self,
        target: Path,
        cleanup_root: Path,
        relative: Path,
        expected_identity: tuple[int, int],
        attempt: _QuarantineAttempt,
    ) -> _QuarantinedBlob | None:
        directory_flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        leaf_flags = (
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        pinned_descriptor: int | None = None
        candidate_descriptor: int | None = None
        parent_descriptor: int | None = None
        quarantine_descriptor: int | None = None
        directory_pins: list[tuple[Path, int, tuple[int, int]]] = []
        quarantine_name = self.storage._quarantine_name()
        moved = False
        failure_active = False
        try:
            pinned_descriptor, opened = self.storage._open_anchored_execution_path(
                target, cleanup_root
            )
            if (opened.st_dev, opened.st_ino) != expected_identity:
                return None
            attempt.ownership_acquired = True
            chain_descriptor = os.open(cleanup_root, directory_flags)
            chain_opened = os.fstat(chain_descriptor)
            directory_pins.append(
                (
                    cleanup_root,
                    chain_descriptor,
                    (chain_opened.st_dev, chain_opened.st_ino),
                )
            )
            current_path = cleanup_root
            for component in relative.parts[:-1]:
                next_descriptor = os.open(
                    component, directory_flags, dir_fd=chain_descriptor
                )
                next_opened = os.fstat(next_descriptor)
                current_path /= component
                directory_pins.append(
                    (
                        current_path,
                        next_descriptor,
                        (next_opened.st_dev, next_opened.st_ino),
                    )
                )
                chain_descriptor = next_descriptor
            parent_descriptor = os.dup(chain_descriptor)
            os.mkdir(quarantine_name, mode=0o700, dir_fd=parent_descriptor)
            quarantine_descriptor = os.open(
                quarantine_name, directory_flags, dir_fd=parent_descriptor
            )
            quarantine_named = os.stat(
                quarantine_name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            quarantine_opened = os.fstat(quarantine_descriptor)
            if not stat.S_ISDIR(quarantine_named.st_mode) or (
                quarantine_named.st_dev,
                quarantine_named.st_ino,
            ) != (quarantine_opened.st_dev, quarantine_opened.st_ino):
                raise ArtifactStoreError("retention.quarantine_invalid")
            try:
                self.storage._rename_directory_no_replace(
                    relative.parts[-1],
                    "candidate",
                    src_dir_fd=parent_descriptor,
                    dst_dir_fd=quarantine_descriptor,
                )
                moved = True
                attempt.filesystem_effect = True
            except BaseException as primary:
                try:
                    candidate_info = os.stat(
                        "candidate",
                        dir_fd=quarantine_descriptor,
                        follow_symlinks=False,
                    )
                    moved = (
                        stat.S_ISREG(candidate_info.st_mode)
                        and (
                            candidate_info.st_dev,
                            candidate_info.st_ino,
                        )
                        == expected_identity
                    )
                except OSError:
                    moved = False
                if moved:
                    attempt.filesystem_effect = True
                    try:
                        try:
                            os.stat(
                                relative.parts[-1],
                                dir_fd=parent_descriptor,
                                follow_symlinks=False,
                            )
                        except FileNotFoundError:
                            pass
                        else:
                            self.storage._rename_directory_no_replace(
                                relative.parts[-1],
                                "replacement",
                                src_dir_fd=parent_descriptor,
                                dst_dir_fd=quarantine_descriptor,
                            )
                        self._restore_secure_no_replace(
                            parent_descriptor,
                            quarantine_descriptor,
                            relative.parts[-1],
                        )
                        moved = False
                        try:
                            os.rmdir(quarantine_name, dir_fd=parent_descriptor)
                        except OSError:
                            pass
                    except BaseException:
                        pass
                raise primary
            candidate_descriptor = os.open(
                "candidate", leaf_flags, dir_fd=quarantine_descriptor
            )
            candidate_opened = os.fstat(candidate_descriptor)
            candidate_named = os.stat(
                "candidate", dir_fd=quarantine_descriptor, follow_symlinks=False
            )
            if (
                not stat.S_ISREG(candidate_opened.st_mode)
                or (candidate_opened.st_dev, candidate_opened.st_ino)
                != (candidate_named.st_dev, candidate_named.st_ino)
                or (candidate_opened.st_dev, candidate_opened.st_ino)
                != expected_identity
            ):
                self._restore_secure_no_replace(
                    parent_descriptor,
                    quarantine_descriptor,
                    relative.parts[-1],
                )
                moved = False
                os.close(candidate_descriptor)
                candidate_descriptor = None
                os.rmdir(quarantine_name, dir_fd=parent_descriptor)
                os.close(quarantine_descriptor)
                quarantine_descriptor = None
                os.close(parent_descriptor)
                parent_descriptor = None
                os.close(pinned_descriptor)
                pinned_descriptor = None
                return None
            return _QuarantinedBlob(
                target=target,
                cleanup_root=cleanup_root,
                relative=relative,
                quarantine_name=quarantine_name,
                pinned_descriptor=pinned_descriptor,
                candidate_descriptor=candidate_descriptor,
                expected_identity=expected_identity,
                parent_descriptor=parent_descriptor,
                quarantine_descriptor=quarantine_descriptor,
                directory_pins=tuple(directory_pins),
            )
        except BaseException:
            failure_active = True
            if (
                moved
                and parent_descriptor is not None
                and quarantine_descriptor is not None
            ):
                try:
                    self._restore_secure_no_replace(
                        parent_descriptor,
                        quarantine_descriptor,
                        relative.parts[-1],
                    )
                    moved = False
                    os.rmdir(quarantine_name, dir_fd=parent_descriptor)
                except BaseException:
                    pass
            raise
        finally:
            if failure_active or not moved:
                for descriptor in (
                    candidate_descriptor,
                    quarantine_descriptor,
                    parent_descriptor,
                    pinned_descriptor,
                    *(pin[1] for pin in reversed(directory_pins)),
                ):
                    if descriptor is not None:
                        try:
                            os.close(descriptor)
                        except BaseException:
                            pass

    def _verify_quarantined_blob(
        self, quarantine: _QuarantinedBlob, blob: ArtifactBlob
    ) -> None:
        self._verify_quarantined_candidate_identity(quarantine)
        self._verify_blob_descriptor(quarantine.candidate_descriptor, blob)
        self._verify_quarantined_candidate_identity(quarantine)

    def _verify_quarantined_candidate_identity(
        self, quarantine: _QuarantinedBlob
    ) -> None:
        try:
            self._verify_quarantined_directory_pins(quarantine)
            opened = os.fstat(quarantine.candidate_descriptor)
            if (opened.st_dev, opened.st_ino) != quarantine.expected_identity:
                raise ArtifactStoreError("blob.path_changed")
            if quarantine.quarantine_descriptor is not None:
                named = os.stat(
                    "candidate",
                    dir_fd=quarantine.quarantine_descriptor,
                    follow_symlinks=False,
                )
            else:
                named = self.storage._lstat_anchored_execution_path(
                    quarantine.target.parent / quarantine.quarantine_name / "candidate",
                    quarantine.cleanup_root,
                )
        except ArtifactStoreError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactStoreError("blob.path_changed") from exc
        if (named.st_dev, named.st_ino) != quarantine.expected_identity:
            raise ArtifactStoreError("blob.path_changed")
        self._verify_quarantined_directory_pins(quarantine)

    @staticmethod
    def _verify_quarantined_directory_pins(quarantine: _QuarantinedBlob) -> None:
        try:
            for path, descriptor, expected_identity in quarantine.directory_pins:
                named = path.lstat()
                opened = os.fstat(descriptor)
                if (
                    path.is_symlink()
                    or not stat.S_ISDIR(named.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or (named.st_dev, named.st_ino) != expected_identity
                    or (opened.st_dev, opened.st_ino) != expected_identity
                ):
                    raise ArtifactStoreError("blob.path_changed")
            if quarantine.parent_descriptor is not None and quarantine.directory_pins:
                parent_opened = os.fstat(quarantine.parent_descriptor)
                if (parent_opened.st_dev, parent_opened.st_ino) != (
                    quarantine.directory_pins[-1][2]
                ):
                    raise ArtifactStoreError("blob.path_changed")
        except ArtifactStoreError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactStoreError("blob.path_changed") from exc

    def _restore_quarantined_blob(self, quarantine: _QuarantinedBlob) -> None:
        with self.storage._execution_cleanup_lock:
            operation_failure: BaseException | None = None
            try:
                self._verify_quarantined_candidate_identity(quarantine)
                if quarantine.parent_descriptor is not None:
                    self._restore_secure_no_replace(
                        quarantine.parent_descriptor,
                        quarantine.quarantine_descriptor,
                        quarantine.relative.parts[-1],
                    )
                    os.rmdir(
                        quarantine.quarantine_name,
                        dir_fd=quarantine.parent_descriptor,
                    )
                else:
                    candidate = (
                        quarantine.target.parent
                        / quarantine.quarantine_name
                        / "candidate"
                    )
                    self._restore_path_no_replace(candidate, quarantine.target)
                    candidate.parent.rmdir()
            except BaseException as exc:
                operation_failure = exc
                raise
            finally:
                try:
                    self._close_quarantine_descriptors(quarantine)
                except BaseException:
                    if operation_failure is None:
                        raise

    def _retire_unrestorable_blob(self, digest: str) -> None:
        """Durably remove a row when exact canonical bytes cannot be restored."""
        transaction_started = False
        try:
            self.storage.begin_execution_transaction()
            transaction_started = True
            if self._blob_is_referenced(digest):
                raise ArtifactStoreError("retention.restore_failed")
            self.storage.conn.execute(
                "INSERT OR IGNORE INTO artifact_blob_retirements "
                "(sha256, retired_at) VALUES (?, ?)",
                (digest, self._utc_now()),
            )
            self.storage.conn.execute(
                "DELETE FROM artifact_blobs WHERE sha256 = ?", (digest,)
            )
            self.storage.commit_execution_transaction()
            transaction_started = False
        except BaseException:
            if (
                transaction_started
                and self.storage.execution_transaction_owned_by_current_thread
            ):
                try:
                    self.storage.rollback_execution_transaction()
                except BaseException:
                    pass
            raise
        if (
            self.storage.conn.execute(
                "SELECT 1 FROM artifact_blobs WHERE sha256 = ?", (digest,)
            ).fetchone()
            is not None
        ):
            raise ArtifactStoreError("retention.restore_failed")

    def _delete_quarantined_blob(self, quarantine: _QuarantinedBlob) -> None:
        with self.storage._execution_cleanup_lock:
            try:
                self._verify_quarantined_directory_pins(quarantine)
                opened = os.fstat(quarantine.candidate_descriptor)
                if (opened.st_dev, opened.st_ino) != quarantine.expected_identity:
                    raise ArtifactStoreError("blob.path_changed")
                if quarantine.quarantine_descriptor is not None:
                    named = os.stat(
                        "candidate",
                        dir_fd=quarantine.quarantine_descriptor,
                        follow_symlinks=False,
                    )
                    if (named.st_dev, named.st_ino) != quarantine.expected_identity:
                        raise ArtifactStoreError("blob.path_changed")
                    os.unlink("candidate", dir_fd=quarantine.quarantine_descriptor)
                    quarantine.candidate_unlinked = True
                    os.rmdir(
                        quarantine.quarantine_name,
                        dir_fd=quarantine.parent_descriptor,
                    )
                else:
                    candidate = (
                        quarantine.target.parent
                        / quarantine.quarantine_name
                        / "candidate"
                    )
                    named = candidate.stat(follow_symlinks=False)
                    if (named.st_dev, named.st_ino) != quarantine.expected_identity:
                        raise ArtifactStoreError("blob.path_changed")
                    candidate.unlink()
                    quarantine.candidate_unlinked = True
                    candidate.parent.rmdir()
            except BaseException:
                if not quarantine.candidate_unlinked:
                    quarantine.candidate_unlinked = (
                        not self._quarantined_candidate_is_named(quarantine)
                    )
                raise
            self._close_quarantine_descriptors(quarantine)

    @staticmethod
    def _quarantined_candidate_is_named(quarantine: _QuarantinedBlob) -> bool:
        try:
            if quarantine.quarantine_descriptor is not None:
                os.stat(
                    "candidate",
                    dir_fd=quarantine.quarantine_descriptor,
                    follow_symlinks=False,
                )
            else:
                (
                    quarantine.target.parent / quarantine.quarantine_name / "candidate"
                ).stat(follow_symlinks=False)
        except OSError:
            return False
        return True

    @staticmethod
    def _close_quarantine_descriptors(quarantine: _QuarantinedBlob) -> None:
        failure: BaseException | None = None
        for descriptor in (
            quarantine.candidate_descriptor,
            quarantine.quarantine_descriptor,
            quarantine.parent_descriptor,
            quarantine.pinned_descriptor,
            *(pin[1] for pin in reversed(quarantine.directory_pins)),
        ):
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException as exc:
                    failure = failure or exc
        if failure is not None:
            raise failure

    @staticmethod
    def _restore_path_no_replace(candidate: Path, target: Path) -> None:
        try:
            if os.name == "nt":
                os.rename(candidate, target)
            else:
                os.link(candidate, target, follow_symlinks=False)
                candidate.unlink()
        except FileExistsError as exc:
            raise ArtifactStoreError("retention.restore_collision") from exc
        except OSError as exc:
            raise ArtifactStoreError("retention.restore_failed") from exc

    @staticmethod
    def _restore_secure_no_replace(
        parent_descriptor: int,
        quarantine_descriptor: int,
        leaf_name: str,
    ) -> None:
        try:
            os.link(
                "candidate",
                leaf_name,
                src_dir_fd=quarantine_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            os.unlink("candidate", dir_fd=quarantine_descriptor)
        except FileExistsError as exc:
            raise ArtifactStoreError("retention.restore_collision") from exc
        except OSError as exc:
            raise ArtifactStoreError("retention.restore_failed") from exc

    def _regular_file_identity(self, path: Path) -> tuple[int, int]:
        descriptor: int | None = None
        try:
            descriptor, opened = self.storage._open_anchored_execution_path(
                path, self.root
            )
            return opened.st_dev, opened.st_ino
        except (OSError, ValueError) as exc:
            raise ArtifactStoreError("blob.path_invalid") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _prepare(
        self,
        *,
        source_run_id: str,
        normalized_source_identity: str,
        entity_bytes: bytes,
        response_content_type: str,
        requested_url: str,
        source_url: str,
        final_url: str,
        retrieved_at: str | datetime,
        http_status: int,
        wire_encoding: str,
        content_encoding: str,
        artifact_role: str,
        artifact_status: str,
        access_decision_id: str,
        adapter_id: str,
        adapter_version: str,
        redirect_chain: Sequence[Mapping[str, Any]],
        discovered_from: Mapping[str, Any] | None,
        parent_artifact_id: str | None,
        source_artifact_id: str | None,
        derived_from_artifact_ids: Sequence[str],
        filename: str | None,
        manifest_version: str,
    ) -> _PreparedArtifact:
        if manifest_version != CONTRACT_VERSION:
            raise ArtifactStoreError("identity.manifest_version_invalid")
        if not isinstance(source_run_id, str) or not _SOURCE_RUN_RE.fullmatch(
            source_run_id
        ):
            raise ArtifactStoreError("identity.source_run_invalid")
        if not isinstance(entity_bytes, bytes):
            raise ArtifactStoreError("blob.entity_bytes_invalid")
        self._validate_portable_size(len(entity_bytes))
        if artifact_role not in {"source", "derived"} or artifact_status != "completed":
            raise ArtifactStoreError("observation.status_invalid")
        self._validate_identity_and_urls(
            artifact_role=artifact_role,
            normalized_source_identity=normalized_source_identity,
            requested_url=requested_url,
            source_url=source_url,
            final_url=final_url,
        )
        if artifact_role == "derived" and not normalized_source_identity.startswith(
            f"urn:web-listening:derived:{source_artifact_id}:"
        ):
            raise ArtifactStoreError("lineage.invalid")
        if type(http_status) is not int or not 200 <= http_status < 300:
            raise ArtifactStoreError("observation.http_status_invalid")
        wire_encoding = self._validate_encoding(wire_encoding, "wire")
        content_encoding = self._validate_encoding(content_encoding, "content")
        mime_type = self._validate_mime(
            response_content_type=response_content_type,
            entity_bytes=entity_bytes,
            filename_or_url=final_url,
            wire_encoding=wire_encoding,
        )
        if filename is not None:
            if not isinstance(filename, str) or len(filename) > MAX_FILENAME_LENGTH:
                raise ArtifactStoreError("mime.extension_mismatch")
            self._validate_mime(
                response_content_type=response_content_type,
                entity_bytes=entity_bytes,
                filename_or_url=filename,
                wire_encoding=wire_encoding,
            )
        filename_evidence = filename or ""
        retrieved = self._normalize_datetime(retrieved_at)
        if (
            not isinstance(access_decision_id, str)
            or not _ACCESS_DECISION_RE.fullmatch(access_decision_id)
            or not isinstance(adapter_id, str)
            or not _ADAPTER_ID_RE.fullmatch(adapter_id)
            or not isinstance(adapter_version, str)
            or len(adapter_version) > 64
            or not _SEMVER_RE.fullmatch(adapter_version)
        ):
            raise ArtifactStoreError("observation.provenance_invalid")
        redirect_json = self._validate_redirect_provenance(
            redirect_chain,
            requested_url=requested_url,
            final_url=final_url,
            access_decision_id=access_decision_id,
            artifact_status=artifact_status,
        )

        digest = hashlib.sha256(entity_bytes).hexdigest()
        artifact_uri = f"artifact:sha256:{digest}"
        storage_suffix = ".gz" if self.storage_encoding == "gzip" else ""
        storage_path = f"_blobs/{digest[:2]}/{digest}{storage_suffix}"
        stored_bytes = (
            _canonical_gzip_bytes(entity_bytes)
            if self.storage_encoding == "gzip"
            else entity_bytes
        )
        self._validate_portable_size(len(stored_bytes))
        if (
            hashlib.sha256(
                self._decode_stored_bytes(
                    stored_bytes,
                    self.storage_encoding,
                    expected_entity_size=len(entity_bytes),
                    expected_stored_size=len(stored_bytes),
                )
            ).hexdigest()
            != digest
        ):
            raise ArtifactStoreError("blob.compression_verification_failed")

        now = self._utc_now()
        version_id = artifact_version_id(
            source_run_id=source_run_id,
            normalized_source_identity=normalized_source_identity,
            sha256=digest,
            manifest_version=manifest_version,
        )
        artifact_id = artifact_id_for_identity(
            source_run_id=source_run_id,
            normalized_source_identity=normalized_source_identity,
            sha256=digest,
            manifest_version=manifest_version,
        )
        lineage = self._prepare_lineage(
            artifact_id=artifact_id,
            artifact_role=artifact_role,
            parent_artifact_id=parent_artifact_id,
            source_artifact_id=source_artifact_id,
            derived_from_artifact_ids=derived_from_artifact_ids,
            created_at=now,
        )
        discovered_json = self._validate_discovery_provenance(
            discovered_from,
            artifact_role=artifact_role,
            parent_artifact_id=parent_artifact_id,
            source_artifact_id=source_artifact_id,
        )
        blob = ArtifactBlob(
            sha256=digest,
            artifact_uri=artifact_uri,
            storage_path=storage_path,
            entity_size_bytes=len(entity_bytes),
            stored_size_bytes=len(stored_bytes),
            storage_encoding=self.storage_encoding,
            created_at=now,
        )
        version = ArtifactVersion(
            version_id=version_id,
            manifest_version=manifest_version,
            source_run_id=source_run_id,
            normalized_source_identity=normalized_source_identity,
            sha256=digest,
            artifact_uri=artifact_uri,
            mime_type=mime_type,
            created_at=now,
        )
        observation = ArtifactObservation(
            artifact_id=artifact_id,
            version_id=version_id,
            manifest_version=manifest_version,
            source_run_id=source_run_id,
            normalized_source_identity=normalized_source_identity,
            requested_url=requested_url,
            source_url=source_url,
            final_url=final_url,
            filename=filename_evidence,
            retrieved_at=retrieved,
            http_status=http_status,
            mime_type=mime_type,
            size_bytes=len(entity_bytes),
            sha256=digest,
            artifact_uri=artifact_uri,
            wire_encoding=wire_encoding,
            content_encoding=content_encoding,
            artifact_role=artifact_role,
            artifact_status=artifact_status,
            access_decision_id=access_decision_id,
            adapter_id=adapter_id,
            adapter_version=adapter_version,
            redirect_chain_json=redirect_json,
            discovered_from_json=discovered_json,
            created_at=now,
        )
        return _PreparedArtifact(
            blob=blob,
            version=version,
            observation=observation,
            lineage=lineage,
            stored_bytes=stored_bytes,
        )

    def _publish_blob(
        self,
        blob: ArtifactBlob,
        stored_bytes: bytes,
        *,
        allow_missing_existing_row: bool = False,
    ) -> None:
        target = self.root.joinpath(*blob.storage_path.split("/"))
        self.storage.ensure_execution_artifact_directory(
            target.parent, cleanup_root=self.root
        )
        pinned = self._pin_blob_parent(target)
        temporary_name: str | None = None
        temporary_descriptor: int | None = None
        temporary_identity: tuple[int, int] | None = None
        published_identity: tuple[int, int] | None = None
        primary_failure: BaseException | None = None
        try:
            existing_row = self.storage.conn.execute(
                "SELECT * FROM artifact_blobs WHERE sha256 = ?", (blob.sha256,)
            ).fetchone()
            if existing_row is not None:
                existing_blob = self._blob_from_row(existing_row)
                self._validate_blob_storage_path(
                    existing_blob.sha256,
                    existing_blob.storage_path,
                    existing_blob.storage_encoding,
                )
                if self._blob_semantics_without_stored_size(
                    existing_blob
                ) != self._blob_semantics_without_stored_size(blob):
                    raise ArtifactStoreError("blob.conflict")
                if self._pinned_leaf_identity(pinned, target.name) is not None:
                    if (
                        self._verify_pinned_blob(pinned, target.name, existing_blob)
                        != stored_bytes
                    ):
                        raise ArtifactStoreError("blob.conflict")
                    return
                if not allow_missing_existing_row:
                    raise ArtifactStoreError("blob.missing")
            if self._pinned_leaf_identity(pinned, target.name) is not None:
                try:
                    if (
                        self._verify_pinned_blob(pinned, target.name, blob)
                        != stored_bytes
                    ):
                        raise ArtifactStoreError("blob.conflict")
                except ArtifactStoreError as exc:
                    raise ArtifactStoreError("blob.conflict") from exc
                return

            temporary_name = f".{blob.sha256}.{secrets.token_hex(16)}.tmp"
            temporary_descriptor = self._open_pinned_leaf(
                pinned,
                temporary_name,
                os.O_RDWR | os.O_CREAT | os.O_EXCL,
                mode=0o600,
            )
            temporary_info = os.fstat(temporary_descriptor)
            temporary_identity = (temporary_info.st_dev, temporary_info.st_ino)
            remaining = memoryview(stored_bytes)
            while remaining:
                written = os.write(temporary_descriptor, remaining)
                if written <= 0:
                    raise ArtifactStoreError("blob.publish_failed")
                remaining = remaining[written:]
            os.fsync(temporary_descriptor)
            if self._verify_blob_descriptor(temporary_descriptor, blob) != stored_bytes:
                raise ArtifactStoreError("blob.compression_verification_failed")
            self._verify_pinned_leaf_identity(
                pinned, temporary_name, temporary_identity
            )

            try:
                self._link_pinned_leaf(
                    pinned,
                    temporary_name,
                    target.name,
                )
                published_identity = temporary_identity
            except FileExistsError:
                if self._verify_pinned_blob(pinned, target.name, blob) != stored_bytes:
                    raise ArtifactStoreError("blob.conflict")
            except BaseException as primary:
                try:
                    link_effected = (
                        self._pinned_leaf_identity(pinned, target.name)
                        == temporary_identity
                    )
                except BaseException:
                    link_effected = False
                if link_effected:
                    published_identity = temporary_identity
                    try:
                        self.storage.register_execution_created_path(
                            target,
                            cleanup_root=self.root,
                            expected_identity=temporary_identity,
                        )
                    except BaseException:
                        try:
                            self._unlink_pinned_leaf_if_identity(
                                pinned, target.name, temporary_identity
                            )
                        except BaseException:
                            pass
                        published_identity = None
                raise primary
            else:
                self._verify_pinned_leaf_identity(
                    pinned, target.name, temporary_identity
                )
                if self._verify_pinned_blob(pinned, target.name, blob) != stored_bytes:
                    raise ArtifactStoreError("blob.conflict")
                self._verify_pinned_parent(pinned)
                try:
                    self.storage.register_execution_created_path(
                        target,
                        cleanup_root=self.root,
                        expected_identity=temporary_identity,
                    )
                except BaseException:
                    self._unlink_pinned_leaf_if_identity(
                        pinned, target.name, temporary_identity
                    )
                    published_identity = None
                    raise
        except BaseException as exc:
            primary_failure = exc
            if published_identity is not None:
                try:
                    self._unlink_pinned_leaf_if_identity(
                        pinned, target.name, published_identity
                    )
                except BaseException:
                    pass
            raise
        finally:
            cleanup_failure: BaseException | None = None
            try:
                if temporary_descriptor is not None:
                    os.close(temporary_descriptor)
            except BaseException as exc:
                cleanup_failure = exc
            if temporary_name is not None and temporary_identity is not None:
                try:
                    self._unlink_pinned_leaf_if_identity(
                        pinned, temporary_name, temporary_identity
                    )
                except BaseException as exc:
                    cleanup_failure = cleanup_failure or exc
            self._close_pinned_blob_parent(pinned)
            if primary_failure is None and cleanup_failure is not None:
                raise cleanup_failure

    def _insert_metadata(self, prepared: _PreparedArtifact) -> None:
        self._validate_references(prepared)
        self.storage.conn.execute(
            "DELETE FROM artifact_blob_retirements WHERE sha256 = ?",
            (prepared.blob.sha256,),
        )
        blob_row = self.storage.conn.execute(
            "SELECT * FROM artifact_blobs WHERE sha256 = ?", (prepared.blob.sha256,)
        ).fetchone()
        if blob_row is None:
            columns = list(self._blob_columns())
            values = list(self._blob_values(prepared.blob))
            if self._blob_table_has_legacy_mime_column():
                index = columns.index("storage_encoding")
                columns.insert(index, "mime_type")
                values.insert(index, prepared.version.mime_type)
            placeholders = ", ".join("?" for _ in columns)
            self.storage.conn.execute(
                f"INSERT INTO artifact_blobs ({', '.join(columns)}) "
                f"VALUES ({placeholders})",
                tuple(values),
            )
        elif self._blob_semantics_without_stored_size(
            self._blob_from_row(blob_row)
        ) != self._blob_semantics_without_stored_size(prepared.blob):
            raise ArtifactStoreError("blob.conflict")

        version_row = self.storage.conn.execute(
            "SELECT * FROM artifact_versions WHERE version_id = ?",
            (prepared.version.version_id,),
        ).fetchone()
        if version_row is None:
            self.storage.conn.execute(
                """INSERT INTO artifact_versions
                   (version_id, manifest_version, source_run_id,
                    normalized_source_identity, sha256, artifact_uri, mime_type,
                    created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                self._version_values(prepared.version),
            )
        elif self._version_semantics(
            self._version_from_row(version_row)
        ) != self._version_semantics(prepared.version):
            raise ArtifactStoreError("version.conflict")

        observation_row = self.storage.conn.execute(
            "SELECT * FROM artifact_observations WHERE artifact_id = ?",
            (prepared.observation.artifact_id,),
        ).fetchone()
        observation_is_new = observation_row is None
        existing_lineage = self.storage.conn.execute(
            """SELECT * FROM artifact_lineage WHERE artifact_id = ?
               ORDER BY relation, ordinal, lineage_id""",
            (prepared.observation.artifact_id,),
        ).fetchall()
        if not observation_is_new:
            actual = {
                self._lineage_semantics(self._lineage_from_row(row))
                for row in existing_lineage
            }
            expected = {
                self._lineage_semantics(lineage) for lineage in prepared.lineage
            }
            if actual != expected:
                raise ArtifactStoreError("lineage.conflict")
        if observation_is_new:
            self.storage.conn.execute(
                """INSERT INTO artifact_observations
                   (artifact_id, version_id, manifest_version, source_run_id,
                    normalized_source_identity, requested_url, source_url,
                    final_url, filename, retrieved_at, http_status, mime_type,
                    size_bytes, sha256, artifact_uri, wire_encoding,
                    content_encoding, artifact_role, artifact_status,
                    access_decision_id, adapter_id, adapter_version,
                    redirect_chain_json, discovered_from_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                self._observation_values(prepared.observation),
            )
        elif self._observation_semantics(
            self._observation_from_row(observation_row)
        ) != self._observation_semantics(prepared.observation):
            raise ArtifactStoreError("observation.conflict")

        if observation_is_new:
            for lineage in prepared.lineage:
                self.storage.conn.execute(
                    """INSERT INTO artifact_lineage
                       (lineage_id, artifact_id, relation, related_artifact_id,
                        ordinal, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    self._lineage_values(lineage),
                )

    def _validate_references(self, prepared: _PreparedArtifact) -> None:
        for lineage in prepared.lineage:
            if lineage.related_artifact_id == prepared.observation.artifact_id:
                raise ArtifactStoreError("lineage.self_reference")
            row = self.storage.conn.execute(
                "SELECT 1 FROM artifact_observations WHERE artifact_id = ?",
                (lineage.related_artifact_id,),
            ).fetchone()
            if row is None:
                raise ArtifactStoreError("lineage.missing_reference")
        try:
            discovered = json.loads(prepared.observation.discovered_from_json)
        except json.JSONDecodeError as exc:
            raise ArtifactStoreError("observation.invalid") from exc
        related = (
            discovered.get("artifact_id") if isinstance(discovered, dict) else None
        )
        if related is not None:
            row = self.storage.conn.execute(
                "SELECT 1 FROM artifact_observations WHERE artifact_id = ?", (related,)
            ).fetchone()
            if row is None:
                raise ArtifactStoreError("lineage.missing_reference")

    def _validate_loaded_identity(self, stored: StoredArtifact) -> None:
        blob = stored.blob
        version = stored.version
        observation = stored.observation
        self._validate_loaded_text_types(stored)
        self._validate_portable_size(blob.entity_size_bytes)
        self._validate_portable_size(blob.stored_size_bytes)
        self._validate_portable_size(observation.size_bytes)
        try:
            if (
                version.manifest_version != CONTRACT_VERSION
                or observation.manifest_version != CONTRACT_VERSION
                or not isinstance(version.source_run_id, str)
                or not _SOURCE_RUN_RE.fullmatch(version.source_run_id)
                or not isinstance(observation.source_run_id, str)
                or not _SOURCE_RUN_RE.fullmatch(observation.source_run_id)
                or not isinstance(observation.retrieved_at, str)
                or self._normalize_datetime(observation.retrieved_at)
                != observation.retrieved_at
                or type(observation.http_status) is not int
                or not 200 <= observation.http_status < 300
                or self._validate_encoding(observation.wire_encoding, "wire")
                != observation.wire_encoding
                or self._validate_encoding(observation.content_encoding, "content")
                != observation.content_encoding
                or not isinstance(observation.mime_type, str)
                or observation.mime_type != observation.mime_type.casefold()
                or observation.mime_type not in self.allowed_mime_types
                or not isinstance(observation.filename, str)
                or len(observation.filename) > MAX_FILENAME_LENGTH
            ):
                raise ArtifactStoreError("observation.contract_invalid")
        except ArtifactStoreError as exc:
            if exc.reason_code == "observation.contract_invalid":
                raise
            raise ArtifactStoreError("observation.contract_invalid") from exc
        expected_uri = f"artifact:sha256:{blob.sha256}"
        if (
            not _SHA256_RE.fullmatch(blob.sha256)
            or blob.artifact_uri != expected_uri
            or version.sha256 != blob.sha256
            or version.artifact_uri != expected_uri
            or observation.sha256 != blob.sha256
            or observation.artifact_uri != expected_uri
            or observation.size_bytes != blob.entity_size_bytes
            or observation.mime_type != version.mime_type
            or observation.version_id != version.version_id
            or observation.manifest_version != version.manifest_version
            or observation.source_run_id != version.source_run_id
            or observation.normalized_source_identity
            != version.normalized_source_identity
        ):
            raise ArtifactStoreError("reference.identity_mismatch")
        self._validate_blob_storage_path(
            blob.sha256,
            blob.storage_path,
            blob.storage_encoding,
        )
        entity_bytes = self.read_blob(blob.artifact_uri)
        self._validate_mime(
            response_content_type=observation.mime_type,
            entity_bytes=entity_bytes,
            filename_or_url=observation.final_url,
            wire_encoding=observation.wire_encoding,
        )
        if observation.filename:
            self._validate_mime(
                response_content_type=observation.mime_type,
                entity_bytes=entity_bytes,
                filename_or_url=observation.filename,
                wire_encoding=observation.wire_encoding,
            )
        expected_version = artifact_version_id(
            source_run_id=version.source_run_id,
            normalized_source_identity=version.normalized_source_identity,
            sha256=version.sha256,
            manifest_version=version.manifest_version,
        )
        expected_artifact = artifact_id_for_identity(
            source_run_id=observation.source_run_id,
            normalized_source_identity=observation.normalized_source_identity,
            sha256=observation.sha256,
            manifest_version=observation.manifest_version,
        )
        if (
            not _VERSION_ID_RE.fullmatch(version.version_id)
            or version.version_id != expected_version
            or observation.artifact_id != expected_artifact
        ):
            raise ArtifactStoreError("reference.identity_mismatch")
        for lineage in stored.lineage:
            expected_lineage = artifact_lineage_id(
                artifact_id=lineage.artifact_id,
                relation=lineage.relation,
                related_artifact_id=lineage.related_artifact_id,
                ordinal=lineage.ordinal,
            )
            if (
                not _LINEAGE_ID_RE.fullmatch(lineage.lineage_id)
                or lineage.lineage_id != expected_lineage
                or lineage.artifact_id != observation.artifact_id
            ):
                raise ArtifactStoreError("lineage.identity_mismatch")
        self._validate_loaded_lineage(stored)
        self._validate_global_lineage_dag()
        if (
            observation.artifact_role not in {"source", "derived"}
            or observation.artifact_status != "completed"
            or not _ACCESS_DECISION_RE.fullmatch(observation.access_decision_id)
            or not _ADAPTER_ID_RE.fullmatch(observation.adapter_id)
            or not isinstance(observation.adapter_version, str)
            or len(observation.adapter_version) > 64
            or not _SEMVER_RE.fullmatch(observation.adapter_version)
        ):
            raise ArtifactStoreError("observation.provenance_invalid")
        self._validate_identity_and_urls(
            artifact_role=observation.artifact_role,
            normalized_source_identity=observation.normalized_source_identity,
            requested_url=observation.requested_url,
            source_url=observation.source_url,
            final_url=observation.final_url,
        )
        try:
            redirects = json.loads(observation.redirect_chain_json)
            discovered = json.loads(observation.discovered_from_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ArtifactStoreError("observation.provenance_invalid") from exc
        canonical_redirects = self._validate_redirect_provenance(
            redirects,
            requested_url=observation.requested_url,
            final_url=observation.final_url,
            access_decision_id=observation.access_decision_id,
            artifact_status=observation.artifact_status,
        )
        parent_ids = [
            edge.related_artifact_id
            for edge in stored.lineage
            if edge.relation == "parent"
        ]
        source_ids = [
            edge.related_artifact_id
            for edge in stored.lineage
            if edge.relation == "source"
        ]
        canonical_discovery = self._validate_discovery_provenance(
            discovered,
            artifact_role=observation.artifact_role,
            parent_artifact_id=parent_ids[0] if parent_ids else None,
            source_artifact_id=source_ids[0] if source_ids else None,
        )
        if (
            canonical_redirects != observation.redirect_chain_json
            or canonical_discovery != observation.discovered_from_json
        ):
            raise ArtifactStoreError("observation.provenance_invalid")

    @staticmethod
    def _validate_loaded_text_types(stored: StoredArtifact) -> None:
        text_fields = (
            (
                stored.blob,
                (
                    "sha256",
                    "artifact_uri",
                    "storage_path",
                    "storage_encoding",
                    "created_at",
                ),
            ),
            (
                stored.version,
                tuple(ArtifactVersion.__dataclass_fields__),
            ),
            (
                stored.observation,
                tuple(
                    field
                    for field in ArtifactObservation.__dataclass_fields__
                    if field not in {"http_status", "size_bytes"}
                ),
            ),
        )
        for value, fields in text_fields:
            if any(type(getattr(value, field)) is not str for field in fields):
                raise ArtifactStoreError("reference.type_invalid")
        for edge in stored.lineage:
            if any(
                type(getattr(edge, field)) is not str
                for field in ArtifactLineage.__dataclass_fields__
                if field != "ordinal"
            ):
                raise ArtifactStoreError("reference.type_invalid")

    def _validate_loaded_lineage(self, stored: StoredArtifact) -> None:
        observation = stored.observation
        grouped: dict[str, list[ArtifactLineage]] = {
            "parent": [],
            "source": [],
            "derived_from": [],
        }
        for edge in stored.lineage:
            if edge.relation not in grouped or type(edge.ordinal) is not int:
                raise ArtifactStoreError("lineage.invalid")
            if edge.related_artifact_id == observation.artifact_id:
                raise ArtifactStoreError("lineage.invalid")
            related = self.storage.conn.execute(
                "SELECT 1 FROM artifact_observations WHERE artifact_id = ?",
                (edge.related_artifact_id,),
            ).fetchone()
            if related is None:
                raise ArtifactStoreError("lineage.missing_reference")
            grouped[edge.relation].append(edge)
        if len(grouped["parent"]) > 1 or [
            edge.ordinal for edge in grouped["parent"]
        ] not in ([], [0]):
            raise ArtifactStoreError("lineage.invalid")
        if observation.artifact_role == "source":
            if grouped["source"] or grouped["derived_from"]:
                raise ArtifactStoreError("lineage.invalid")
            return
        source_edges = grouped["source"]
        derived_edges = sorted(grouped["derived_from"], key=lambda edge: edge.ordinal)
        if (
            len(source_edges) != 1
            or source_edges[0].ordinal != 0
            or not derived_edges
            or len(derived_edges) > 1000
            or [edge.ordinal for edge in derived_edges]
            != list(range(len(derived_edges)))
            or len({edge.related_artifact_id for edge in derived_edges})
            != len(derived_edges)
            or source_edges[0].related_artifact_id
            not in {edge.related_artifact_id for edge in derived_edges}
            or len(grouped["parent"]) != 1
            or grouped["parent"][0].related_artifact_id
            != source_edges[0].related_artifact_id
            or not observation.normalized_source_identity.startswith(
                f"urn:web-listening:derived:{source_edges[0].related_artifact_id}:"
            )
        ):
            raise ArtifactStoreError("lineage.invalid")

    def _validate_global_lineage_dag(self) -> None:
        artifact_ids: set[str] = set()
        for row in self.storage.conn.execute(
            "SELECT artifact_id FROM artifact_observations"
        ):
            artifact_id = row["artifact_id"]
            if type(artifact_id) is not str:
                raise ArtifactStoreError("reference.type_invalid")
            artifact_ids.add(artifact_id)
        dependencies = {artifact_id: set() for artifact_id in artifact_ids}
        for row in self.storage.conn.execute(
            "SELECT artifact_id, related_artifact_id FROM artifact_lineage"
        ):
            artifact_id = row["artifact_id"]
            related_id = row["related_artifact_id"]
            if type(artifact_id) is not str or type(related_id) is not str:
                raise ArtifactStoreError("reference.type_invalid")
            if artifact_id not in artifact_ids or related_id not in artifact_ids:
                raise ArtifactStoreError("lineage.missing_reference")
            dependencies[artifact_id].add(related_id)
        resolved: set[str] = set()
        pending = dict(dependencies)
        while pending:
            ready = {
                artifact_id
                for artifact_id, related_ids in pending.items()
                if related_ids <= resolved
            }
            if not ready:
                raise ArtifactStoreError("lineage.invalid")
            resolved.update(ready)
            for artifact_id in ready:
                del pending[artifact_id]

    def _prepare_lineage(
        self,
        *,
        artifact_id: str,
        artifact_role: str,
        parent_artifact_id: str | None,
        source_artifact_id: str | None,
        derived_from_artifact_ids: Sequence[str],
        created_at: str,
    ) -> tuple[ArtifactLineage, ...]:
        if not isinstance(derived_from_artifact_ids, Sequence) or isinstance(
            derived_from_artifact_ids, (str, bytes)
        ):
            raise ArtifactStoreError("lineage.invalid")
        derived = tuple(derived_from_artifact_ids)
        if len(derived) > 1000 or len(set(derived)) != len(derived):
            raise ArtifactStoreError("lineage.invalid")
        if artifact_role == "source" and (source_artifact_id is not None or derived):
            raise ArtifactStoreError("lineage.invalid")
        if artifact_role == "derived" and (
            source_artifact_id is None
            or source_artifact_id not in derived
            or parent_artifact_id != source_artifact_id
        ):
            raise ArtifactStoreError("lineage.invalid")
        values: list[tuple[str, str, int]] = []
        if parent_artifact_id is not None:
            values.append(("parent", parent_artifact_id, 0))
        if source_artifact_id is not None:
            values.append(("source", source_artifact_id, 0))
        values.extend(
            ("derived_from", related_artifact_id, ordinal)
            for ordinal, related_artifact_id in enumerate(derived)
        )
        lineage: list[ArtifactLineage] = []
        for relation, related_artifact_id, ordinal in values:
            if not isinstance(related_artifact_id, str) or not re.fullmatch(
                r"artifact-[0-9a-f]{24}", related_artifact_id
            ):
                raise ArtifactStoreError("lineage.invalid")
            lineage.append(
                ArtifactLineage(
                    lineage_id=artifact_lineage_id(
                        artifact_id=artifact_id,
                        relation=relation,
                        related_artifact_id=related_artifact_id,
                        ordinal=ordinal,
                    ),
                    artifact_id=artifact_id,
                    relation=relation,
                    related_artifact_id=related_artifact_id,
                    ordinal=ordinal,
                    created_at=created_at,
                )
            )
        return tuple(lineage)

    def _validate_identity_and_urls(
        self,
        *,
        artifact_role: str,
        normalized_source_identity: str,
        requested_url: str,
        source_url: str,
        final_url: str,
    ) -> None:
        for value in (requested_url, source_url, final_url):
            try:
                if not self._url_is_canonical_and_bounded(value):
                    raise ValueError("URL is not canonical")
            except (TypeError, ValueError) as exc:
                raise ArtifactStoreError("identity.url_invalid") from exc
        if artifact_role == "source":
            if normalized_source_identity != source_url:
                raise ArtifactStoreError("identity.source_mismatch")
        elif not isinstance(
            normalized_source_identity, str
        ) or not _DERIVED_IDENTITY_RE.fullmatch(normalized_source_identity):
            raise ArtifactStoreError("identity.derived_invalid")

    @staticmethod
    def _url_is_canonical_and_bounded(value: Any) -> bool:
        return (
            isinstance(value, str)
            and len(value) <= 2048
            and _canonical_url(value) == value
        )

    def _validate_mime(
        self,
        *,
        response_content_type: str,
        entity_bytes: bytes,
        filename_or_url: str,
        wire_encoding: str,
    ) -> str:
        if (
            not isinstance(response_content_type, str)
            or not response_content_type.strip()
        ):
            raise ArtifactStoreError("mime.header_missing")
        mime_type = response_content_type.split(";", 1)[0].strip().casefold()
        if mime_type not in self.allowed_mime_types:
            raise ArtifactStoreError("mime.unsupported")
        extension = self._extension(filename_or_url, wire_encoding=wire_encoding)
        if extension and extension not in _MIME_EXTENSIONS[mime_type]:
            raise ArtifactStoreError("mime.extension_mismatch")
        if not self._magic_matches(mime_type, entity_bytes):
            raise ArtifactStoreError("mime.magic_mismatch")
        return mime_type

    @staticmethod
    def _magic_matches(mime_type: str, data: bytes) -> bool:
        if mime_type == "application/pdf":
            return data.startswith(b"%PDF-")
        if mime_type in {"text/html", "application/xhtml+xml"}:
            prefix = data[:4096].lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0c\x0d\x20").lower()
            return bool(
                re.search(rb"<(?:!doctype\s+html\b|html\b|head\b|body\b)", prefix)
            )
        if mime_type in _ZIP_MIME_PREFIXES:
            if not data.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")):
                return False
            required = _ZIP_MIME_PREFIXES[mime_type]
            return required is None or required in data
        if mime_type in _OLE_MIMES:
            return data.startswith(_OLE_MAGIC)
        if mime_type == "image/png":
            return data.startswith(b"\x89PNG\r\n\x1a\n")
        if mime_type == "image/jpeg":
            return data.startswith(b"\xff\xd8\xff")
        if mime_type == "image/gif":
            return data.startswith((b"GIF87a", b"GIF89a"))
        if mime_type == "application/json":
            try:
                json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return False
            return True
        if mime_type in {"application/xml", "text/xml"}:
            prefix = data[:4096].lstrip(b"\xef\xbb\xbf\x00\x09\x0a\x0c\x0d\x20")
            return prefix.startswith(b"<")
        if mime_type in {"text/plain", "text/markdown"}:
            if b"\x00" in data:
                return False
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                return False
            return True
        return False

    @staticmethod
    def _extension(filename_or_url: str, *, wire_encoding: str) -> str:
        path = (
            unquote(urlsplit(filename_or_url).path)
            if "://" in filename_or_url
            else filename_or_url
        )
        suffixes = [suffix.casefold() for suffix in Path(path).suffixes]
        if suffixes and suffixes[-1] == ".gz" and wire_encoding == "gzip":
            suffixes.pop()
        return suffixes[-1] if suffixes else ""

    @staticmethod
    def _validate_encoding(value: str, kind: str) -> str:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 64
            or any(
                ord(character) < 0x21 or ord(character) > 0x7E for character in value
            )
        ):
            raise ArtifactStoreError(f"encoding.{kind}_invalid")
        return value.casefold()

    @staticmethod
    def _normalize_datetime(value: str | datetime) -> str:
        try:
            parsed = (
                value
                if isinstance(value, datetime)
                else datetime.fromisoformat(value.replace("Z", "+00:00"))
            )
        except (AttributeError, TypeError, ValueError) as exc:
            raise ArtifactStoreError("observation.retrieved_at_invalid") from exc
        if parsed.tzinfo is None:
            raise ArtifactStoreError("observation.retrieved_at_invalid")
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _validate_redirect_provenance(
        self,
        redirect_chain: Sequence[Mapping[str, Any]],
        *,
        requested_url: str,
        final_url: str,
        access_decision_id: str,
        artifact_status: str,
    ) -> str:
        try:
            if not isinstance(redirect_chain, Sequence) or isinstance(
                redirect_chain, (str, bytes)
            ):
                raise ValueError("redirect chain must be a sequence")
            loaded = json.loads(canonical_json(list(redirect_chain)))
            if not isinstance(loaded, list) or len(loaded) > 100:
                raise ValueError("redirect chain is invalid")
            expected_from = requested_url
            for ordinal, hop in enumerate(loaded):
                if not isinstance(hop, dict) or set(hop) != _REDIRECT_KEYS:
                    raise ValueError("redirect hop is not closed")
                if (
                    type(hop["ordinal"]) is not int
                    or hop["ordinal"] != ordinal
                    or hop["from_url"] != expected_from
                    or hop["http_status"] not in _REDIRECT_STATUSES
                    or not _ACCESS_DECISION_RE.fullmatch(str(hop["access_decision_id"]))
                    or hop["decision"] not in {"allow", "reject"}
                ):
                    raise ValueError("redirect hop is invalid")
                for key in ("from_url", "to_url"):
                    if not self._url_is_canonical_and_bounded(hop[key]):
                        raise ValueError("redirect URL is not canonical")
                if (
                    urlsplit(hop["from_url"]).scheme.casefold() == "https"
                    and urlsplit(hop["to_url"]).scheme.casefold() == "http"
                ):
                    raise ValueError("redirect transport was downgraded")
                expected_from = hop["to_url"]
            reject_ordinals = [
                ordinal
                for ordinal, hop in enumerate(loaded)
                if hop["decision"] == "reject"
            ]
            if reject_ordinals and (
                reject_ordinals != [len(loaded) - 1] or artifact_status != "rejected"
            ):
                raise ValueError("redirect rejection is inconsistent")
            if expected_from != final_url or (
                loaded and loaded[-1]["access_decision_id"] != access_decision_id
            ):
                raise ValueError("redirect chain endpoint is inconsistent")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactStoreError("observation.provenance_invalid") from exc
        return canonical_json(loaded)

    def _validate_discovery_provenance(
        self,
        discovered_from: Mapping[str, Any] | None,
        *,
        artifact_role: str,
        parent_artifact_id: str | None,
        source_artifact_id: str | None,
    ) -> str:
        if discovered_from is None:
            if artifact_role == "derived":
                discovered: dict[str, Any] = {
                    "kind": "derived",
                    "artifact_id": source_artifact_id,
                    "source_url": self._referenced_observation_url(source_artifact_id),
                }
            elif parent_artifact_id is not None:
                discovered = {
                    "kind": "link",
                    "artifact_id": parent_artifact_id,
                    "source_url": self._referenced_observation_url(parent_artifact_id),
                }
            else:
                discovered = {
                    "kind": "seed",
                    "artifact_id": None,
                    "source_url": None,
                }
        else:
            try:
                discovered = json.loads(canonical_json(dict(discovered_from)))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise ArtifactStoreError("observation.provenance_invalid") from exc

        try:
            if not isinstance(discovered, dict) or set(discovered) != _DISCOVERY_KEYS:
                raise ValueError("discovery evidence is not closed")
            kind = discovered["kind"]
            related = discovered["artifact_id"]
            discovered_url = discovered["source_url"]
            if kind not in _DISCOVERY_KINDS:
                raise ValueError("discovery kind is invalid")
            if kind == "seed":
                valid = (
                    artifact_role == "source"
                    and related is None
                    and discovered_url is None
                    and parent_artifact_id is None
                )
            elif kind == "search":
                valid = (
                    artifact_role == "source"
                    and related is None
                    and isinstance(discovered_url, str)
                    and parent_artifact_id is None
                )
            elif kind == "link":
                valid = (
                    artifact_role == "source"
                    and isinstance(related, str)
                    and isinstance(discovered_url, str)
                    and related == parent_artifact_id
                )
            elif kind == "crawler":
                valid = (
                    artifact_role == "source"
                    and isinstance(discovered_url, str)
                    and (related is None) == (parent_artifact_id is None)
                    and (related is None or related == parent_artifact_id)
                )
            else:
                valid = (
                    artifact_role == "derived"
                    and isinstance(related, str)
                    and isinstance(discovered_url, str)
                    and related == parent_artifact_id == source_artifact_id
                )
            if not valid:
                raise ValueError("discovery evidence contradicts lineage")
            if discovered_url is not None and not self._url_is_canonical_and_bounded(
                discovered_url
            ):
                raise ValueError("discovery URL is not canonical")
            if (
                related is not None
                and discovered_url != self._referenced_observation_url(related)
            ):
                raise ValueError("discovery URL does not match the referenced artifact")
        except ArtifactStoreError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactStoreError("observation.provenance_invalid") from exc
        return canonical_json(discovered)

    def _referenced_observation_url(self, artifact_id: str | None) -> str:
        if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(
            artifact_id
        ):
            raise ArtifactStoreError("lineage.invalid")
        row = self.storage.conn.execute(
            "SELECT final_url FROM artifact_observations WHERE artifact_id = ?",
            (artifact_id,),
        ).fetchone()
        if row is None:
            raise ArtifactStoreError("lineage.missing_reference")
        final_url = row["final_url"]
        if type(final_url) is not str:
            raise ArtifactStoreError("reference.type_invalid")
        return final_url

    @staticmethod
    def _utc_now() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _validate_portable_size(value: Any) -> int:
        if type(value) is not int or value < 0 or value > MAX_PORTABLE_JSON_INTEGER:
            raise ArtifactStoreError("size.invalid")
        return value

    @staticmethod
    def _decode_stored_bytes(
        data: bytes,
        storage_encoding: str,
        *,
        expected_entity_size: int,
        expected_stored_size: int,
    ) -> bytes:
        ArtifactStore._validate_portable_size(expected_entity_size)
        ArtifactStore._validate_portable_size(expected_stored_size)
        if len(data) != expected_stored_size:
            raise ArtifactStoreError("blob.corrupt")
        try:
            if storage_encoding == "identity":
                if len(data) != expected_entity_size:
                    raise ArtifactStoreError("blob.corrupt")
                return data
            if storage_encoding == "gzip":
                decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
                entity = bytearray()
                for offset in range(0, len(data), _GZIP_INPUT_CHUNK_SIZE):
                    remaining_input = data[offset : offset + _GZIP_INPUT_CHUNK_SIZE]
                    while remaining_input:
                        output_limit = expected_entity_size + 1 - len(entity)
                        if output_limit <= 0:
                            raise ArtifactStoreError("blob.corrupt")
                        before = len(remaining_input)
                        entity.extend(decoder.decompress(remaining_input, output_limit))
                        remaining_input = decoder.unconsumed_tail
                        if len(entity) > expected_entity_size:
                            raise ArtifactStoreError("blob.corrupt")
                        if remaining_input and len(remaining_input) == before:
                            raise ArtifactStoreError("blob.corrupt")
                    if decoder.unused_data:
                        raise ArtifactStoreError("blob.corrupt")
                output_limit = expected_entity_size + 1 - len(entity)
                if output_limit <= 0:
                    raise ArtifactStoreError("blob.corrupt")
                entity.extend(decoder.flush(output_limit))
                if (
                    len(entity) != expected_entity_size
                    or not decoder.eof
                    or decoder.unused_data
                    or decoder.unconsumed_tail
                ):
                    raise ArtifactStoreError("blob.corrupt")
                return bytes(entity)
        except (EOFError, OSError, zlib.error) as exc:
            raise ArtifactStoreError("blob.corrupt") from exc
        raise ArtifactStoreError("encoding.storage_invalid")

    @staticmethod
    def _validate_blob_storage_path(
        digest: str,
        storage_path: str,
        storage_encoding: str,
    ) -> None:
        suffix = ".gz" if storage_encoding == "gzip" else ""
        expected = f"_blobs/{digest[:2]}/{digest}{suffix}"
        try:
            validate_portable_relative_path(
                storage_path, field_name="artifact blob storage_path"
            )
        except (TypeError, ValueError) as exc:
            raise ArtifactStoreError("blob.path_invalid") from exc
        if (
            storage_encoding not in _ALLOWED_STORAGE_ENCODINGS
            or storage_path != expected
        ):
            raise ArtifactStoreError("blob.path_invalid")

    def _verify_blob_file(self, path: Path, blob: ArtifactBlob) -> None:
        stored_bytes = self._read_pinned_blob_file(
            path,
            expected_size=self._validate_portable_size(blob.stored_size_bytes),
        )
        entity = self._decode_stored_bytes(
            stored_bytes,
            blob.storage_encoding,
            expected_entity_size=blob.entity_size_bytes,
            expected_stored_size=blob.stored_size_bytes,
        )
        if (
            len(stored_bytes) != blob.stored_size_bytes
            or len(entity) != blob.entity_size_bytes
            or hashlib.sha256(entity).hexdigest() != blob.sha256
        ):
            raise ArtifactStoreError("blob.conflict")

    def _pin_blob_parent(self, target: Path) -> _PinnedBlobParent:
        try:
            anchored_target, anchored_root = self.storage._anchored_execution_path(
                target, self.root
            )
            relative = anchored_target.relative_to(anchored_root)
            root_descriptor = self.storage._open_execution_directory_descriptor(
                anchored_root
            )
        except (OSError, ValueError) as exc:
            raise ArtifactStoreError("blob.path_invalid") from exc
        parent_descriptor: int | None = None
        owned_descriptors = {root_descriptor}
        directory_pins: list[tuple[Path, int, tuple[int, int]]] = []
        try:
            root_info = os.fstat(root_descriptor)
            if not stat.S_ISDIR(root_info.st_mode):
                raise ArtifactStoreError("blob.path_invalid")
            secure_dir_fd = all(
                operation in os.supports_dir_fd
                for operation in (os.open, os.stat, os.link, os.unlink)
            )
            if secure_dir_fd:
                parent_descriptor = os.dup(root_descriptor)
                owned_descriptors.add(parent_descriptor)
                directory_flags = (
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                )
                parent_parts = relative.parts[:-1]
                current_path = anchored_root
                for index, component in enumerate(parent_parts):
                    named = os.stat(
                        component,
                        dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                    next_descriptor = os.open(
                        component,
                        directory_flags,
                        dir_fd=parent_descriptor,
                    )
                    owned_descriptors.add(next_descriptor)
                    opened = os.fstat(next_descriptor)
                    if (
                        not stat.S_ISDIR(named.st_mode)
                        or not stat.S_ISDIR(opened.st_mode)
                        or (named.st_dev, named.st_ino)
                        != (opened.st_dev, opened.st_ino)
                    ):
                        raise ArtifactStoreError("blob.path_changed")
                    current_path /= component
                    if index < len(parent_parts) - 1:
                        pin_descriptor = os.dup(next_descriptor)
                        owned_descriptors.add(pin_descriptor)
                        directory_pins.append(
                            (
                                current_path,
                                pin_descriptor,
                                (opened.st_dev, opened.st_ino),
                            )
                        )
                    previous_descriptor = parent_descriptor
                    parent_descriptor = next_descriptor
                    os.close(previous_descriptor)
                    owned_descriptors.discard(previous_descriptor)
            else:
                parent_descriptor = self.storage._open_execution_directory_descriptor(
                    anchored_target.parent
                )
                owned_descriptors.add(parent_descriptor)
                current_path = anchored_root
                for component in relative.parts[:-2]:
                    current_path /= component
                    pin_descriptor = self.storage._open_execution_directory_descriptor(
                        current_path
                    )
                    owned_descriptors.add(pin_descriptor)
                    opened = os.fstat(pin_descriptor)
                    named = current_path.lstat()
                    if (
                        current_path.is_symlink()
                        or not stat.S_ISDIR(opened.st_mode)
                        or not stat.S_ISDIR(named.st_mode)
                        or (opened.st_dev, opened.st_ino)
                        != (named.st_dev, named.st_ino)
                    ):
                        raise ArtifactStoreError("blob.path_changed")
                    directory_pins.append(
                        (
                            current_path,
                            pin_descriptor,
                            (opened.st_dev, opened.st_ino),
                        )
                    )
            parent_info = os.fstat(parent_descriptor)
            pinned = _PinnedBlobParent(
                target=anchored_target,
                root=anchored_root,
                root_descriptor=root_descriptor,
                parent_descriptor=parent_descriptor,
                root_identity=(root_info.st_dev, root_info.st_ino),
                parent_identity=(parent_info.st_dev, parent_info.st_ino),
                secure_dir_fd=secure_dir_fd,
                directory_pins=tuple(directory_pins),
            )
            self._verify_pinned_parent(pinned)
            owned_descriptors.discard(root_descriptor)
            owned_descriptors.discard(parent_descriptor)
            for _, descriptor, _ in directory_pins:
                owned_descriptors.discard(descriptor)
            return pinned
        except BaseException as exc:
            for descriptor in owned_descriptors:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            if isinstance(exc, ArtifactStoreError):
                raise
            if isinstance(exc, OSError):
                raise ArtifactStoreError("blob.path_changed") from exc
            raise

    @staticmethod
    def _close_pinned_blob_parent(pinned: _PinnedBlobParent) -> None:
        descriptors = [
            pinned.parent_descriptor,
            *(descriptor for _, descriptor, _ in reversed(pinned.directory_pins)),
            pinned.root_descriptor,
        ]
        for descriptor in descriptors:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _verify_pinned_parent(self, pinned: _PinnedBlobParent) -> None:
        try:
            root_named = pinned.root.lstat()
            parent_named = pinned.target.parent.lstat()
            root_opened = os.fstat(pinned.root_descriptor)
            parent_opened = os.fstat(pinned.parent_descriptor)
            if (
                pinned.root.is_symlink()
                or pinned.target.parent.is_symlink()
                or not stat.S_ISDIR(root_named.st_mode)
                or not stat.S_ISDIR(parent_named.st_mode)
                or (root_named.st_dev, root_named.st_ino) != pinned.root_identity
                or (root_opened.st_dev, root_opened.st_ino) != pinned.root_identity
                or (parent_named.st_dev, parent_named.st_ino) != pinned.parent_identity
                or (parent_opened.st_dev, parent_opened.st_ino)
                != pinned.parent_identity
            ):
                raise ArtifactStoreError("blob.path_changed")
            for path, descriptor, expected_identity in pinned.directory_pins:
                named = path.lstat()
                opened = os.fstat(descriptor)
                if (
                    path.is_symlink()
                    or not stat.S_ISDIR(named.st_mode)
                    or not stat.S_ISDIR(opened.st_mode)
                    or (named.st_dev, named.st_ino) != expected_identity
                    or (opened.st_dev, opened.st_ino) != expected_identity
                ):
                    raise ArtifactStoreError("blob.path_changed")
            current = pinned.root
            for component in pinned.target.parent.relative_to(pinned.root).parts:
                current /= component
                named = current.lstat()
                if current.is_symlink() or not stat.S_ISDIR(named.st_mode):
                    raise ArtifactStoreError("blob.path_changed")
        except ArtifactStoreError:
            raise
        except (OSError, ValueError) as exc:
            raise ArtifactStoreError("blob.path_changed") from exc

    def _open_pinned_leaf(
        self,
        pinned: _PinnedBlobParent,
        leaf_name: str,
        flags: int,
        *,
        mode: int = 0o600,
    ) -> int:
        descriptor: int | None = None
        opened_identity: tuple[int, int] | None = None
        created_exclusive = bool(flags & os.O_CREAT and flags & os.O_EXCL)
        read_only = not flags & (
            os.O_WRONLY
            | os.O_RDWR
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_APPEND", 0)
        )
        flags |= getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        try:
            self._verify_pinned_parent(pinned)
            if pinned.secure_dir_fd:
                descriptor = os.open(
                    leaf_name,
                    flags,
                    mode,
                    dir_fd=pinned.parent_descriptor,
                )
            elif os.name == "nt" and read_only:
                descriptor = self.storage._open_windows_shared_execution_leaf(
                    pinned.target.parent / leaf_name
                )
            else:
                descriptor = os.open(pinned.target.parent / leaf_name, flags, mode)
            opened = os.fstat(descriptor)
            opened_identity = (opened.st_dev, opened.st_ino)
            if not stat.S_ISREG(opened.st_mode):
                raise ArtifactStoreError("blob.path_invalid")
            self._verify_pinned_parent(pinned)
        except BaseException as exc:
            actual_path = None
            if descriptor is not None:
                if opened_identity is None:
                    try:
                        opened = os.fstat(descriptor)
                        if stat.S_ISREG(opened.st_mode):
                            opened_identity = (opened.st_dev, opened.st_ino)
                    except BaseException:
                        pass
                if not pinned.secure_dir_fd:
                    try:
                        actual_path = self._path_for_open_descriptor(
                            descriptor,
                            pinned.target.parent / leaf_name,
                        )
                    except BaseException:
                        pass
            if descriptor is not None and os.name == "nt" and not pinned.secure_dir_fd:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
                descriptor = None
            if created_exclusive and opened_identity is not None:
                try:
                    if pinned.secure_dir_fd:
                        self._unlink_pinned_leaf_if_identity(
                            pinned,
                            leaf_name,
                            opened_identity,
                        )
                    elif actual_path is not None:
                        self.storage.remove_execution_created_path_if_identity(
                            actual_path,
                            cleanup_root=actual_path.parent,
                            expected_identity=opened_identity,
                        )
                except BaseException:
                    pass
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
                descriptor = None
            if isinstance(exc, ArtifactStoreError):
                raise
            if isinstance(exc, OSError):
                raise ArtifactStoreError("blob.missing") from exc
            raise
        return descriptor

    @staticmethod
    def _path_for_open_descriptor(descriptor: int, fallback: Path) -> Path:
        if os.name != "nt":
            try:
                return Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            except OSError:
                return fallback

        try:
            import ctypes
            import msvcrt
            from ctypes import wintypes

            handle = msvcrt.get_osfhandle(descriptor)
            get_final_path = ctypes.WinDLL(
                "kernel32", use_last_error=True
            ).GetFinalPathNameByHandleW
            get_final_path.argtypes = (
                wintypes.HANDLE,
                wintypes.LPWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
            )
            get_final_path.restype = wintypes.DWORD
            buffer = ctypes.create_unicode_buffer(32768)
            length = get_final_path(handle, buffer, len(buffer), 0)
            if not length or length >= len(buffer):
                return fallback
            value = buffer.value
            if value.startswith("\\\\?\\"):
                value = value[4:]
            return Path(value)
        except (OSError, ValueError):
            return fallback

    def _pinned_leaf_identity(
        self, pinned: _PinnedBlobParent, leaf_name: str
    ) -> tuple[int, int] | None:
        self._verify_pinned_parent(pinned)
        try:
            if pinned.secure_dir_fd:
                named = os.stat(
                    leaf_name,
                    dir_fd=pinned.parent_descriptor,
                    follow_symlinks=False,
                )
            else:
                named = (pinned.target.parent / leaf_name).stat(follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ArtifactStoreError("blob.path_changed") from exc
        if not stat.S_ISREG(named.st_mode):
            raise ArtifactStoreError("blob.path_invalid")
        return named.st_dev, named.st_ino

    def _verify_pinned_leaf_identity(
        self,
        pinned: _PinnedBlobParent,
        leaf_name: str,
        expected_identity: tuple[int, int],
    ) -> None:
        if self._pinned_leaf_identity(pinned, leaf_name) != expected_identity:
            raise ArtifactStoreError("blob.path_changed")

    def _link_pinned_leaf(
        self,
        pinned: _PinnedBlobParent,
        source_name: str,
        target_name: str,
    ) -> None:
        self._verify_pinned_parent(pinned)
        if pinned.secure_dir_fd:
            os.link(
                source_name,
                target_name,
                src_dir_fd=pinned.parent_descriptor,
                dst_dir_fd=pinned.parent_descriptor,
                follow_symlinks=False,
            )
        else:
            os.link(
                pinned.target.parent / source_name,
                pinned.target.parent / target_name,
                follow_symlinks=False,
            )
        self._verify_pinned_parent(pinned)

    def _unlink_pinned_leaf_if_identity(
        self,
        pinned: _PinnedBlobParent,
        leaf_name: str,
        expected_identity: tuple[int, int],
    ) -> None:
        if pinned.secure_dir_fd:
            try:
                named = os.stat(
                    leaf_name,
                    dir_fd=pinned.parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            actual = (named.st_dev, named.st_ino)
            if not stat.S_ISREG(named.st_mode) or actual != expected_identity:
                raise ArtifactStoreError("blob.path_changed")
            os.unlink(leaf_name, dir_fd=pinned.parent_descriptor)
        else:
            actual = self._pinned_leaf_identity(pinned, leaf_name)
            if actual is None:
                return
            if actual != expected_identity:
                raise ArtifactStoreError("blob.path_changed")
            os.unlink(pinned.target.parent / leaf_name)
            self._verify_pinned_parent(pinned)

    def _verify_blob_descriptor(self, descriptor: int, blob: ArtifactBlob) -> bytes:
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise ArtifactStoreError("blob.path_invalid")
            self._validate_portable_size(blob.entity_size_bytes)
            stored_size = self._validate_portable_size(blob.stored_size_bytes)
            os.lseek(descriptor, 0, os.SEEK_SET)
            stored_bytes = self._read_descriptor_exact(descriptor, stored_size)
            entity = self._decode_stored_bytes(
                stored_bytes,
                blob.storage_encoding,
                expected_entity_size=blob.entity_size_bytes,
                expected_stored_size=blob.stored_size_bytes,
            )
            if (
                len(stored_bytes) != blob.stored_size_bytes
                or len(entity) != blob.entity_size_bytes
                or hashlib.sha256(entity).hexdigest() != blob.sha256
            ):
                raise ArtifactStoreError("blob.conflict")
            return stored_bytes
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError("blob.corrupt") from exc

    def _verify_pinned_blob(
        self,
        pinned: _PinnedBlobParent,
        leaf_name: str,
        blob: ArtifactBlob,
    ) -> bytes:
        descriptor: int | None = None
        try:
            expected_identity = self._pinned_leaf_identity(pinned, leaf_name)
            if expected_identity is None:
                raise ArtifactStoreError("blob.missing")
            descriptor = self._open_pinned_leaf(pinned, leaf_name, os.O_RDONLY)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise ArtifactStoreError("blob.path_changed")
            stored_bytes = self._verify_blob_descriptor(descriptor, blob)
            self._verify_pinned_leaf_identity(pinned, leaf_name, expected_identity)
            self._verify_pinned_parent(pinned)
            return stored_bytes
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _pin_canonical_blob(
        self,
        path: Path,
        blob: ArtifactBlob,
    ) -> _PinnedCanonicalBlob:
        pinned = self._pin_blob_parent(path)
        descriptor: int | None = None
        try:
            expected_identity = self._pinned_leaf_identity(pinned, path.name)
            if expected_identity is None:
                raise ArtifactStoreError("blob.missing")
            descriptor = self._open_pinned_leaf(pinned, path.name, os.O_RDONLY)
            canonical = _PinnedCanonicalBlob(
                parent=pinned,
                descriptor=descriptor,
                expected_identity=expected_identity,
            )
            self._verify_pinned_canonical_blob(canonical, blob)
            return canonical
        except BaseException:
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except BaseException:
                    pass
            self._close_pinned_blob_parent(pinned)
            raise

    def _verify_pinned_canonical_blob(
        self,
        canonical: _PinnedCanonicalBlob,
        blob: ArtifactBlob,
    ) -> None:
        try:
            self._verify_pinned_parent(canonical.parent)
            opened = os.fstat(canonical.descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != canonical.expected_identity
            ):
                raise ArtifactStoreError("blob.path_changed")
            self._verify_blob_descriptor(canonical.descriptor, blob)
            self._verify_pinned_leaf_identity(
                canonical.parent,
                canonical.parent.target.name,
                canonical.expected_identity,
            )
            self._verify_pinned_parent(canonical.parent)
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError("blob.path_changed") from exc

    @staticmethod
    def _close_pinned_canonical_blob(canonical: _PinnedCanonicalBlob) -> None:
        try:
            os.close(canonical.descriptor)
        except BaseException:
            pass
        try:
            ArtifactStore._close_pinned_blob_parent(canonical.parent)
        except BaseException:
            pass

    def _read_pinned_blob_file(self, path: Path, *, expected_size: int) -> bytes:
        expected_size = self._validate_portable_size(expected_size)
        pinned = self._pin_blob_parent(path)
        descriptor: int | None = None
        try:
            expected_identity = self._pinned_leaf_identity(pinned, path.name)
            if expected_identity is None:
                raise ArtifactStoreError("blob.missing")
            descriptor = self._open_pinned_leaf(pinned, path.name, os.O_RDONLY)
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != expected_identity:
                raise ArtifactStoreError("blob.path_changed")
            stored_bytes = self._read_descriptor_exact(descriptor, expected_size)
            self._verify_pinned_leaf_identity(pinned, path.name, expected_identity)
            self._verify_pinned_parent(pinned)
            return stored_bytes
        finally:
            if descriptor is not None:
                os.close(descriptor)
            self._close_pinned_blob_parent(pinned)

    @staticmethod
    def _read_descriptor_exact(descriptor: int, expected_size: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            remaining = expected_size + 1 - total
            if remaining <= 0:
                raise ArtifactStoreError("blob.corrupt")
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > expected_size:
                raise ArtifactStoreError("blob.corrupt")
        if total != expected_size:
            raise ArtifactStoreError("blob.corrupt")
        return b"".join(chunks)

    @staticmethod
    def _read_regular_file(path: Path) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = None
        try:
            before = path.stat(follow_symlinks=False)
            if not stat.S_ISREG(before.st_mode):
                raise ArtifactStoreError("blob.path_invalid")
            descriptor = os.open(path, flags)
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise ArtifactStoreError("blob.path_changed")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(descriptor, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = path.stat(follow_symlinks=False)
            if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
                raise ArtifactStoreError("blob.path_changed")
            return b"".join(chunks)
        except ArtifactStoreError:
            raise
        except OSError as exc:
            raise ArtifactStoreError("blob.missing") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _digest_from_reference(value: str) -> str:
        prefix = "artifact:sha256:"
        if type(value) is not str:
            raise ArtifactStoreError("blob.reference_invalid")
        digest = value[len(prefix) :] if value.startswith(prefix) else value
        if not _SHA256_RE.fullmatch(digest):
            raise ArtifactStoreError("blob.reference_invalid")
        return digest

    def _blob_is_referenced(self, digest: str) -> bool:
        queries = (
            "SELECT 1 FROM artifact_versions WHERE sha256 = ? LIMIT 1",
            "SELECT 1 FROM artifact_observations WHERE sha256 = ? LIMIT 1",
            "SELECT 1 FROM documents WHERE sha256 = ? LIMIT 1",
            "SELECT 1 FROM document_blobs WHERE sha256 = ? LIMIT 1",
            "SELECT 1 FROM tracked_files WHERE latest_sha256 = ? LIMIT 1",
            "SELECT 1 FROM acquisition_artifacts WHERE sha256 = ? LIMIT 1",
        )
        return any(
            self.storage.conn.execute(query, (digest,)).fetchone() is not None
            for query in queries
        )

    def _insert_blob_row(self, row) -> None:
        columns = list(self._blob_columns())
        if "mime_type" in row.keys():
            columns.insert(columns.index("storage_encoding"), "mime_type")
        placeholders = ", ".join("?" for _ in columns)
        self.storage.conn.execute(
            f"INSERT INTO artifact_blobs ({', '.join(columns)}) "
            f"VALUES ({placeholders})",
            tuple(row[key] for key in columns),
        )

    def _blob_table_has_legacy_mime_column(self) -> bool:
        return any(
            row["name"] == "mime_type"
            for row in self.storage.conn.execute("PRAGMA table_info(artifact_blobs)")
        )

    @staticmethod
    def _blob_columns() -> tuple[str, ...]:
        return (
            "sha256",
            "artifact_uri",
            "storage_path",
            "entity_size_bytes",
            "stored_size_bytes",
            "storage_encoding",
            "created_at",
        )

    @classmethod
    def _blob_values(cls, value: ArtifactBlob) -> tuple[Any, ...]:
        return tuple(getattr(value, field) for field in cls._blob_columns())

    @staticmethod
    def _version_values(value: ArtifactVersion) -> tuple[Any, ...]:
        return (
            value.version_id,
            value.manifest_version,
            value.source_run_id,
            value.normalized_source_identity,
            value.sha256,
            value.artifact_uri,
            value.mime_type,
            value.created_at,
        )

    @staticmethod
    def _observation_values(value: ArtifactObservation) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field) for field in ArtifactObservation.__dataclass_fields__
        )

    @staticmethod
    def _lineage_values(value: ArtifactLineage) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field) for field in ArtifactLineage.__dataclass_fields__
        )

    @staticmethod
    def _blob_from_row(row) -> ArtifactBlob:
        return ArtifactBlob(
            **{field: row[field] for field in ArtifactBlob.__dataclass_fields__}
        )

    @staticmethod
    def _version_from_row(row) -> ArtifactVersion:
        return ArtifactVersion(
            **{field: row[field] for field in ArtifactVersion.__dataclass_fields__}
        )

    @staticmethod
    def _observation_from_row(row) -> ArtifactObservation:
        return ArtifactObservation(
            **{field: row[field] for field in ArtifactObservation.__dataclass_fields__}
        )

    @staticmethod
    def _lineage_from_row(row) -> ArtifactLineage:
        return ArtifactLineage(
            **{field: row[field] for field in ArtifactLineage.__dataclass_fields__}
        )

    @staticmethod
    def _blob_semantics(value: ArtifactBlob) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field)
            for field in ArtifactBlob.__dataclass_fields__
            if field != "created_at"
        )

    @staticmethod
    def _blob_semantics_without_stored_size(value: ArtifactBlob) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field)
            for field in ArtifactBlob.__dataclass_fields__
            if field not in {"created_at", "stored_size_bytes"}
        )

    @staticmethod
    def _version_semantics(value: ArtifactVersion) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field)
            for field in ArtifactVersion.__dataclass_fields__
            if field != "created_at"
        )

    @staticmethod
    def _observation_semantics(value: ArtifactObservation) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field)
            for field in ArtifactObservation.__dataclass_fields__
            if field != "created_at"
        )

    @staticmethod
    def _lineage_semantics(value: ArtifactLineage) -> tuple[Any, ...]:
        return tuple(
            getattr(value, field)
            for field in ArtifactLineage.__dataclass_fields__
            if field != "created_at"
        )


ImmutableArtifactStore = ArtifactStore


__all__ = [
    "ArtifactBlob",
    "ArtifactLineage",
    "ArtifactObservation",
    "ArtifactStore",
    "ArtifactStoreError",
    "ArtifactVersion",
    "ImmutableArtifactStore",
    "IMMUTABLE_ARTIFACT_STORE_VERSION",
    "MAX_FILENAME_LENGTH",
    "ReplayedArtifact",
    "StoredArtifact",
    "artifact_lineage_id",
    "artifact_version_id",
]
