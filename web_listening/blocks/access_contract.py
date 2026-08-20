from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from web_listening.contracts.access_decision import (
    AccessDecision,
    AccessPolicy,
    AccessRejectionErrorEnvelope,
)


AccessContract = AccessPolicy | AccessDecision | AccessRejectionErrorEnvelope


class AccessContractError(ValueError):
    pass


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def load_access_contract(path: str | Path) -> AccessContract:
    """Load and verify one access contract without network or runtime side effects."""
    try:
        text = Path(path).read_text(encoding="utf-8")
        payload = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(payload, dict):
            raise ValueError("access contract must be a JSON object")
        schema_version = payload.get("schema_version")
        model = {
            "access-policy.v1": AccessPolicy,
            "access-decision.v1": AccessDecision,
            "access-rejection-error.v1": AccessRejectionErrorEnvelope,
        }.get(schema_version)
        if model is None:
            raise ValueError("unknown access contract schema_version")
        return model.model_validate_json(text)
    except RecursionError as exc:
        raise AccessContractError(
            "invalid access contract: governed JSON nesting limit exceeded"
        ) from exc
    except (
        OSError,
        UnicodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        TypeError,
        OverflowError,
    ) as exc:
        raise AccessContractError(f"invalid access contract: {exc}") from exc


__all__ = ["AccessContract", "AccessContractError", "load_access_contract"]
