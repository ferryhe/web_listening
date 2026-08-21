from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Callable

from web_listening.blocks.crawler import FetchResult
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.wrapper_protocol import (
    result_from_fetch,
    run_stdio_wrapper,
)


class CloakBrowserAcquisitionAdapter:
    adapter_id = "cloakbrowser"

    def __init__(self, importer: Callable[[str], Any] = import_module):
        self._importer = importer

    def capture(self, url: str, *, config: dict[str, Any] | None = None) -> FetchResult:
        del url, config
        raise RuntimeError(
            "CloakBrowser target reads are disabled until the runtime can consume gateway bytes"
        )


def _launch_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "headless",
        "proxy",
        "timezone",
        "locale",
        "geoip",
        "humanize",
        "human_preset",
    )
    return {key: config[key] for key in allowed if key in config}


def execute(request: CaptureRequest) -> CaptureResult:
    started = datetime.now(timezone.utc)
    result = CloakBrowserAcquisitionAdapter().capture(
        str(request.url), config=dict(request.config)
    )
    return result_from_fetch(request, result, started)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_stdio_wrapper(execute))


__all__ = ["CloakBrowserAcquisitionAdapter", "execute"]
