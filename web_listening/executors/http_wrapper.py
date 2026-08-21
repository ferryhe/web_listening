from __future__ import annotations

from datetime import datetime, timezone

from web_listening.blocks.crawler import FetchResult, HttpCrawler
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.wrapper_protocol import (
    result_from_fetch,
    run_stdio_wrapper,
)


class HttpAcquisitionAdapter:
    adapter_id = "web_http"

    def __init__(self, crawler: HttpCrawler | None = None):
        self.crawler = crawler or HttpCrawler()

    def capture(self, url: str, *, config: dict | None = None) -> FetchResult:
        return self.crawler.fetch_page(url, fetch_config_json=config)


def execute(request: CaptureRequest) -> CaptureResult:
    started = datetime.now(timezone.utc)
    result = HttpAcquisitionAdapter().capture(
        str(request.url), config=dict(request.config)
    )
    return result_from_fetch(request, result, started)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_stdio_wrapper(execute))


__all__ = ["HttpAcquisitionAdapter", "execute"]
