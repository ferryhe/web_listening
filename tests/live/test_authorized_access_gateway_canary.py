"""Explicitly authorized live AccessGateway canary; never enabled by default."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from urllib.parse import urlsplit, urlunsplit

import pytest


def _authorization() -> tuple[str, str]:
    if os.environ.get("WEB_LISTENING_RUN_AUTHORIZED_CANARY") != "1":
        pytest.skip("authorized live canary is offline by default")
    target = os.environ.get("WEB_LISTENING_AUTHORIZED_CANARY_URL", "").strip()
    window = os.environ.get("WEB_LISTENING_AUTHORIZED_CANARY_WINDOW", "").strip()
    if not target or not window:
        pytest.skip("an explicit authorized target and time window are required")
    parsed = urlsplit(target)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        pytest.fail("authorized canary target must be an absolute HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        pytest.fail("authorized canary target must not contain credentials")
    return target, window


@pytest.mark.live
def test_authorized_access_gateway_canary() -> None:
    from web_listening.blocks.governed_read import (
        AccessRejectedError,
        access_rejection_payload,
        build_runtime_read_gateway,
    )

    target, window = _authorization()
    parsed = urlsplit(target)
    recorded_target = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
    observed_at = datetime.now(timezone.utc).isoformat()
    authority_sha256 = hashlib.sha256(
        f"{recorded_target}\n{window}".encode("utf-8")
    ).hexdigest()
    gateway = build_runtime_read_gateway(
        authority_sha256=authority_sha256,
        seed_urls=(recorded_target,),
        allowed_domains=(parsed.hostname or "",),
        user_agent="web-listening-bot/2.0",
        max_body_bytes=2 * 1024 * 1024,
        timeout_seconds=30.0,
        budget_limit=1,
    )
    record = {
        "target": recorded_target,
        "authorized_window": window,
        "observed_at": observed_at,
        "result": {},
    }
    try:
        response = gateway.read(target)
        record["result"] = {
            "outcome": "allow",
            "status_code": response.status_code,
            "reason_code": response.access_decision.reason_code,
            "content_sha256": response.sha256,
        }
    except AccessRejectedError as exc:
        record["result"] = access_rejection_payload(exc)
        raise
    finally:
        gateway.close()
        print(json.dumps(record, sort_keys=True))
