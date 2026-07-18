from __future__ import annotations

from datetime import datetime, timezone
from importlib import import_module
from typing import Any, Callable

from web_listening.blocks.crawler import FetchResult, resolve_user_agent
from web_listening.blocks.normalizer import normalize_html
from web_listening.config import settings
from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors.wrapper_protocol import result_from_fetch, run_stdio_wrapper


class CloakBrowserAcquisitionAdapter:
    adapter_id = "cloakbrowser"

    def __init__(self, importer: Callable[[str], Any] = import_module):
        self._importer = importer

    def capture(self, url: str, *, config: dict[str, Any] | None = None) -> FetchResult:
        try:
            launch = self._importer("cloakbrowser").launch
        except ImportError as exc:
            raise RuntimeError(
                "CloakBrowser acquisition probing requires the optional cloakbrowser runtime. "
                "Install it with `pip install -e .[cloakbrowser]`. First launch may download a browser binary."
            ) from exc
        config = config or {}
        timeout_ms = int(config.get("timeout_ms", settings.request_timeout * 1000))
        wait_until = config.get("wait_until", "load")
        wait_for_selector = config.get("wait_for")
        extra_wait_ms = int(config.get("extra_wait_ms", 0))
        request_user_agent = resolve_user_agent(config)
        browser = launch(**_launch_kwargs(config))
        try:
            page = browser.new_page(user_agent=request_user_agent)
            response = page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            if wait_for_selector:
                page.wait_for_selector(wait_for_selector, timeout=timeout_ms)
            if extra_wait_ms > 0:
                page.wait_for_timeout(extra_wait_ms)
            html, final_url = page.content(), page.url
            status_code = response.status if response else None
        finally:
            browser.close()
        normalized = normalize_html(html, base_url=final_url or url)
        metadata = dict(normalized.metadata)
        metadata.update(driver="cloakbrowser", request_user_agent=request_user_agent, wait_until=wait_until, wait_for=wait_for_selector or "", humanize=bool(config.get("humanize", False)))
        return FetchResult(
            raw_html=normalized.raw_html,
            cleaned_html=normalized.cleaned_html,
            content_text=normalized.content_text,
            markdown=normalized.markdown,
            fit_markdown=normalized.fit_markdown,
            metadata_json=metadata,
            final_url=final_url or url,
            status_code=status_code,
        )


def _launch_kwargs(config: dict[str, Any]) -> dict[str, Any]:
    allowed = ("headless", "proxy", "timezone", "locale", "geoip", "humanize", "human_preset")
    return {key: config[key] for key in allowed if key in config}


def execute(request: CaptureRequest) -> CaptureResult:
    started = datetime.now(timezone.utc)
    result = CloakBrowserAcquisitionAdapter().capture(str(request.url), config=dict(request.config))
    return result_from_fetch(request, result, started)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run_stdio_wrapper(execute))


__all__ = ["CloakBrowserAcquisitionAdapter", "execute"]
