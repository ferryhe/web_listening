from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from web_listening.blocks.acquisition_profile import CaptureAttempt, load_acquisition_profile
from web_listening.blocks.storage import Storage
from web_listening.blocks.acquisition_gateway import redact_persisted_value
from web_listening.contracts._protocol import validate_portable_relative_path


SCHEMA_VERSION = "acquisition-evidence.v1"


def persisted_acquisition_evidence(storage: Storage, *, scope_id: int, run_id: int) -> dict | None:
    attempts = storage.list_acquisition_attempts(scope_id, run_id)
    if not attempts:
        return None
    rows = []
    for attempt in attempts:
        row = attempt.model_dump(mode="json", exclude={"canonical_json"})
        row["canonical_attempt"] = json.loads(attempt.canonical_json)
        rows.append(redact_persisted_value(row))
    accepted = [item for item in attempts if item.accepted]
    latest = attempts[-1]
    rendered_latest = {
        **rows[attempts.index(latest)],
        "adapter": latest.executor_id,
        "status": "passed" if latest.accepted else latest.classification,
    }
    payload = {
        "schema_version": "acquisition-evidence.v2",
        "source": "persisted_exact_run",
        "scope_id": scope_id,
        "run_id": run_id,
        "attempts": rows,
        "executor_chain": [item.executor_id for item in attempts],
        "fallback_used": any(item.position > 0 for item in attempts),
        "quality_results": [item.validation for item in attempts],
        "failure_classifications": [item.classification for item in attempts if not item.accepted],
        "artifacts": [artifact.model_dump(mode="json") for item in attempts for artifact in item.artifacts],
        "accepted_attempt_ids": [item.attempt_id for item in accepted],
        "latest_attempt": rendered_latest,
        "profile": {"site_key": attempts[0].profile_id or ""},
        "recommended_next_adapter": "",
        "next_action": "use_adapter_output" if latest.accepted else "review_probe_failure",
    }
    return redact_persisted_value(payload)
MAX_EVIDENCE_BYTES = 512 * 1024


class AcquisitionEvidenceError(ValueError):
    """Raised when an acquisition evidence artifact cannot be loaded safely."""


def load_acquisition_evidence(
    *,
    profile_path: str | Path | None = None,
    capture_attempt_path: str | Path | None = None,
    probe_path: str | Path | None = None,
) -> dict[str, Any] | None:
    """Load acquisition profile/probe artifacts without executing acquisition."""
    if profile_path is None and capture_attempt_path is None and probe_path is None:
        return None

    input_paths = {
        "profile_path": _portable_source_path("acquisition_profile", profile_path),
        "capture_attempt_path": _portable_source_path("capture_attempt", capture_attempt_path),
        "probe_path": _portable_source_path("acquisition_probe", probe_path),
    }
    profile_payload: dict[str, Any] | None = None
    attempts: list[dict[str, Any]] = []
    source_next_actions: list[str] = []

    if profile_path is not None:
        _ensure_artifact_size(profile_path)
        profile_payload = load_acquisition_profile(profile_path).model_dump(mode="json")

    for path in (capture_attempt_path, probe_path):
        if path is None:
            continue
        payload = _load_structured_file(path)
        extracted_profile, extracted_attempts, next_action = _extract_payload_parts(payload)
        if profile_payload is None and extracted_profile is not None:
            profile_payload = extracted_profile
        attempts.extend(extracted_attempts)
        if next_action:
            source_next_actions.append(next_action)

    latest_attempt = attempts[-1] if attempts else None
    recommended_next_adapter = _recommended_next_adapter(latest_attempt, profile_payload)
    next_action = source_next_actions[-1] if source_next_actions else _derive_next_action(latest_attempt, recommended_next_adapter)

    return redact_persisted_value({
        "schema_version": SCHEMA_VERSION,
        "input_paths": input_paths,
        "profile": profile_payload,
        "attempts": attempts,
        "latest_attempt": latest_attempt,
        "recommended_next_adapter": recommended_next_adapter,
        "next_action": next_action,
    })


def acquisition_artifact_rows(
    *,
    profile_path: str | Path | None = None,
    capture_attempt_path: str | Path | None = None,
    probe_path: str | Path | None = None,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    if profile_path is not None:
        rows.append(_artifact_row(kind="acquisition_profile", path=profile_path, reader="yaml"))
    if capture_attempt_path is not None:
        rows.append(_artifact_row(kind="capture_attempt", path=capture_attempt_path, reader=_reader_for_path(capture_attempt_path)))
    if probe_path is not None:
        rows.append(_artifact_row(kind="acquisition_probe", path=probe_path, reader=_reader_for_path(probe_path)))
    return rows


def _load_structured_file(path: str | Path) -> Any:
    candidate = Path(path)
    _ensure_artifact_size(candidate)
    raw = candidate.read_text(encoding="utf-8")
    if candidate.suffix.lower() == ".json":
        return json.loads(raw)
    return yaml.safe_load(raw)


def _ensure_artifact_size(path: str | Path) -> None:
    candidate = Path(path)
    size = candidate.stat().st_size
    if size > MAX_EVIDENCE_BYTES:
        raise AcquisitionEvidenceError(
            f"Acquisition evidence artifact `{candidate}` is too large to inline "
            f"({size} bytes > {MAX_EVIDENCE_BYTES} bytes)."
        )


def _extract_payload_parts(payload: Any) -> tuple[dict[str, Any] | None, list[dict[str, Any]], str]:
    if payload is None:
        return None, [], ""
    if isinstance(payload, list):
        return None, [_attempt_to_dict(item) for item in payload], ""
    if not isinstance(payload, Mapping):
        raise ValueError("acquisition evidence artifact root must be an object or list")

    profile = payload.get("profile") if isinstance(payload.get("profile"), Mapping) else None
    profile_payload = dict(profile) if profile is not None else None
    next_action = str(payload.get("next_action") or "")

    if payload.get("schema_version") == "capture-attempt.v1":
        return profile_payload, [_attempt_to_dict(payload)], next_action
    if isinstance(payload.get("attempt"), Mapping):
        return profile_payload, [_attempt_to_dict(payload["attempt"])], next_action
    if isinstance(payload.get("attempts"), list):
        return profile_payload, [_attempt_to_dict(item) for item in payload["attempts"]], next_action
    return profile_payload, [], next_action


def _attempt_to_dict(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("capture attempt entries must be objects")
    return CaptureAttempt(**dict(value)).model_dump(mode="json")


def _recommended_next_adapter(latest_attempt: dict[str, Any] | None, profile: dict[str, Any] | None) -> str:
    if latest_attempt is not None:
        return str(latest_attempt.get("recommended_next_adapter") or "")
    if profile is not None:
        return str(profile.get("default_adapter") or "")
    return ""


def _derive_next_action(latest_attempt: dict[str, Any] | None, recommended_next_adapter: str) -> str:
    if latest_attempt is None:
        return f"use_profile_default:{recommended_next_adapter}" if recommended_next_adapter else "review_acquisition_inputs"
    if latest_attempt.get("status") == "passed":
        return "use_adapter_output"
    if recommended_next_adapter:
        return f"try_adapter:{recommended_next_adapter}"
    return "review_probe_failure"


def _artifact_row(*, kind: str, path: str | Path, reader: str) -> dict[str, str]:
    return {
        "plane": "control_plane" if kind == "acquisition_profile" else "evidence_plane",
        "kind": kind,
        "label": Path(path).name,
        "path": _portable_source_path(kind, path),
        "url": "",
        "recommended_reader": reader,
    }


def _portable_source_path(kind: str, path: str | Path | None) -> str:
    if path is None:
        return ""
    basename = Path(path).name
    try:
        validate_portable_relative_path(basename, field_name="acquisition evidence basename")
    except ValueError:
        basename = f"source-{hashlib.sha256(basename.encode('utf-8')).hexdigest()[:16]}"
    return f"compatibility_inputs/{kind}/{basename}"


def _reader_for_path(path: str | Path) -> str:
    suffix = Path(path).suffix.lower()
    if suffix == ".json":
        return "json"
    if suffix in {".yaml", ".yml"}:
        return "yaml"
    return "text"
