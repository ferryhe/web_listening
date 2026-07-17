from __future__ import annotations

from datetime import datetime, timezone

import httpx

from web_listening.blocks.crawler import FetchResult, HttpCrawler, resolve_request_headers
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.wrapper_protocol import result_from_fetch, run_stdio_wrapper
from web_listening.config import settings


class HttpAcquisitionAdapter:
    adapter_id = "web_http"

    def __init__(self, crawler: HttpCrawler | None = None):
        self.crawler = crawler or HttpCrawler()

    def capture(self, url: str, *, config: dict | None = None) -> FetchResult:
        try:
            return self.crawler.fetch_page(url, fetch_config_json=config)
        except httpx.HTTPStatusError as exc:
            response = exc.response
            try:
                response_request = response.request
            except RuntimeError:
                response_request = None
            request = response_request or exc.request
            final_url = str(response_request.url if response_request is not None else request.url)
            from web_listening.blocks.normalizer import normalize_html
            page = normalize_html(response.text, base_url=final_url)
            metadata = dict(page.metadata)
            metadata["driver"] = "http"
            metadata["request_user_agent"] = request.headers.get(
                "User-Agent", resolve_request_headers(config).get("User-Agent", settings.user_agent)
            )
            metadata["http_status_error"] = True
            return FetchResult(page.raw_html, page.cleaned_html, page.content_text, page.markdown, page.fit_markdown, metadata, final_url, response.status_code)


def execute(request: CaptureRequest) -> CaptureResult:
    started = datetime.now(timezone.utc)
    result = HttpAcquisitionAdapter().capture(str(request.url), config=dict(request.config))
    return result_from_fetch(request, result, started)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_stdio_wrapper(execute))


__all__ = ["HttpAcquisitionAdapter", "execute"]
