from __future__ import annotations

import sys
from collections.abc import Callable
from datetime import datetime, timezone

from web_listening.blocks.crawler import FetchResult
from web_listening.contracts import CaptureContent, CaptureError, CaptureRequest, CaptureResult


def result_from_fetch(request: CaptureRequest, result: FetchResult, started_at: datetime) -> CaptureResult:
    return CaptureResult(
        **request.model_dump(include={
            "site_key", "site_skill_id", "site_skill_version", "site_skill_digest",
            "recipe_id", "run_id", "scope_id", "request_id", "executor_id",
        }),
        state="succeeded",
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        final_url=result.final_url,
        status_code=result.status_code,
        content=CaptureContent(media_type="text/html", text=result.raw_html, metadata=result.metadata_json),
    )


def run_stdio_wrapper(handler: Callable[[CaptureRequest], CaptureResult]) -> int:
    """Consume exactly one request value and emit exactly one result value."""
    raw = sys.stdin.buffer.read()
    request = CaptureRequest.model_validate_json(raw)
    try:
        result = handler(request)
    except Exception:
        now = datetime.now(timezone.utc)
        result = CaptureResult(
            **request.model_dump(include={
                "site_key", "site_skill_id", "site_skill_version", "site_skill_digest",
                "recipe_id", "run_id", "scope_id", "request_id", "executor_id",
            }),
            state="failed",
            started_at=now,
            finished_at=now,
            error=CaptureError(code="executor_exception", message="executor handler failed"),
        )
    sys.stdout.write(result.model_dump_json())
    sys.stdout.flush()
    return 0


__all__ = ["result_from_fetch", "run_stdio_wrapper"]
