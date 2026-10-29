from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from io import BytesIO
import json
import mimetypes
import os
import random
import re
import tempfile
import time
from datetime import datetime, timezone
from email.message import Message
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import typer
from bs4 import BeautifulSoup
from bs4.dammit import UnicodeDammit

from web_listening import __version__
from web_listening.blocks.acquisition_terminal import classify_html_capture
from web_listening.contracts.tool_result import (
    ToolResult,
    ToolResultDataQuality,
    ToolResultError,
)

MAX_DOWNLOAD_BYTES = 128 * 1024 * 1024
MAX_PAGE_BYTES = 10 * 1024 * 1024
MAX_BROWSER_REQUESTS = 200
MAX_ACTIONS = 20
MAX_ACTIONS_BYTES = 64 * 1024
MAX_ACTION_SECONDS = 180
MAX_HTTP_TIMEOUT_SECONDS = 180
MAX_INTERACTION_CANDIDATES = 100
EXPLORATION_OUTPUT_DIR = Path("web-listening-exploration")
DEFAULT_HTTP_USER_AGENT = f"web-listening/{__version__}"


class ExplorationError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        attempts: list[dict[str, Any]] | None = None,
        data: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.attempts = attempts or []
        self.data = data or {}


def _resolve_http_user_agent(user_agent: str | None) -> str:
    value = DEFAULT_HTTP_USER_AGENT if user_agent is None else user_agent
    if (
        not isinstance(value, str)
        or not value.strip()
        or not value.isascii()
        or any(not character.isprintable() for character in value)
    ):
        raise ExplorationError(
            "user_agent.invalid",
            "User-Agent must be nonempty visible ASCII on one line",
        )
    return value


def _validate_browser_user_agent(user_agent: str | None) -> None:
    if user_agent is not None:
        _resolve_http_user_agent(user_agent)
        raise ExplorationError(
            "user_agent.unsupported",
            "Custom User-Agent is supported for HTTPX requests; browser mode keeps its normal browser identity",
        )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )


def _result(
    *,
    tool: str,
    data: dict[str, Any] | None = None,
    artifacts: dict[str, Any] | None = None,
    attempts: list[dict[str, Any]] | None = None,
    data_status: str = "present",
    data_count: int | None = 1,
    error: ExplorationError | None = None,
    next_action: str | None = None,
    stop_reason: str = "completed",
    data_quality: ToolResultDataQuality | None = None,
) -> ToolResult:
    return ToolResult(
        ok=error is None,
        has_data=error is None and data_status in {"present", "partial", "artifact_only"},
        data_status="error" if error else data_status,
        data_count=0 if error else data_count,
        tool=tool,
        data=data or {},
        artifacts=artifacts or {},
        attempts=attempts or [],
        data_quality=data_quality
        or ToolResultDataQuality(
            passed=error is None and data_status in {"present", "artifact_only"},
            status_code=(data or {}).get("status"),
        ),
        next_action=next_action,
        stop_reason=stop_reason if error is None else error.code,
        error=(
            ToolResultError(
                code=error.code,
                message=error.message,
                retryable=error.retryable,
            )
            if error
            else None
        ),
    )


def _emit(result: ToolResult, *, json_output: bool) -> None:
    payload = result.model_dump(mode="json")
    if json_output:
        typer.echo(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
    elif result.ok:
        typer.echo(f"{result.tool}: {result.data_status}; artifacts: {', '.join(result.artifacts.values()) or 'none'}")
    else:
        assert result.error is not None
        typer.echo(f"{result.tool}: {result.error.code}: {result.error.message}", err=True)


def parser_error_result(tool: str, message: str | None = None) -> ToolResult:
    return _result(
        tool=tool,
        error=ExplorationError("parser.invalid", message or "Invalid command arguments"),
        next_action="correct_arguments",
    )


def _validate_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
        host = parsed.hostname
    except ValueError as exc:
        raise ExplorationError("url.invalid", "URL must be a valid HTTP or HTTPS URL") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ExplorationError("url.invalid", "URL must be a valid HTTP or HTTPS URL without embedded credentials")
    return url.strip()


def _allowed_domains(url: str, values: list[str] | None) -> list[str]:
    initial_host = urlparse(url).hostname
    domains: list[str] = []
    for raw in values or []:
        value = raw.strip().lower().lstrip(".")
        if not value or any(char in value for char in "/:@?# \t"):
            raise ExplorationError("domain.invalid", "Allowed domains must be host names without a scheme or path")
        try:
            parsed_host = urlparse(f"//{value}").hostname
            parsed_port = urlparse(f"//{value}").port
        except ValueError as exc:
            raise ExplorationError("domain.invalid", "Allowed domains must be host names without a scheme or path") from exc
        if parsed_host != value or parsed_port is not None:
            raise ExplorationError("domain.invalid", "Allowed domains must be host names without a scheme or path")
        domains.append(value)
    return list(dict.fromkeys([initial_host or "", *domains]))


def _host_allowed(url: str, domains: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _normalized_dom(html: str, base_url: str) -> tuple[str, list[dict[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text_content = " ".join(soup.stripped_strings)
    links = []
    for item in BeautifulSoup(html, "html.parser").select("a[href]"):
        href = urljoin(base_url, str(item.get("href", "")).strip())
        if urlparse(href).scheme not in {"http", "https"}:
            continue
        links.append({"text": " ".join(item.stripped_strings), "url": href})
    return re.sub(r"\s+", " ", text_content).strip(), links


def _decode_http_html(body: bytes, content_type: str) -> str:
    content_headers = Message()
    content_headers["content-type"] = content_type
    explicit_charset = content_headers.get_content_charset()
    decoder = UnicodeDammit(
        body,
        known_definite_encodings=[explicit_charset] if explicit_charset else [],
        is_html=True,
    )
    return decoder.unicode_markup or body.decode(explicit_charset or "utf-8", errors="replace")


def _css_quoted(value: str) -> str:
    escaped = []
    for char in value:
        codepoint = ord(char)
        if char in {'"', "\\"}:
            escaped.append("\\" + char)
        elif codepoint == 0:
            escaped.append("\\fffd ")
        elif codepoint < 0x20 or codepoint == 0x7F:
            escaped.append(f"\\{codepoint:x} ")
        else:
            escaped.append(char)
    return "".join(escaped)


def _interaction_selector(element: Any, id_counts: Counter[str]) -> str:
    element_id = element.get("id")
    if isinstance(element_id, str) and id_counts[element_id] == 1:
        return f'{element.name}[id="{_css_quoted(element_id)}"]'
    parts = []
    node = element
    while getattr(node, "name", None) and not str(node.name).startswith("["):
        node_name = str(node.name)
        node_id = node.get("id")
        if isinstance(node_id, str) and id_counts[node_id] == 1:
            parts.insert(0, f'{node_name}[id="{_css_quoted(node_id)}"]')
            break
        parent = node.parent
        siblings = (
            [child for child in parent.find_all(node_name, recursive=False)]
            if getattr(parent, "find_all", None)
            else [node]
        )
        sibling_index = next((index for index, sibling in enumerate(siblings, start=1) if sibling is node), 1)
        parts.insert(0, f"{node_name}:nth-of-type({sibling_index})")
        if node_name == "html":
            break
        node = parent
    return " > ".join(parts)


def _interaction_name(element: Any, soup: BeautifulSoup) -> str:
    for attribute in ("aria-label", "title", "placeholder"):
        value = element.get(attribute)
        if isinstance(value, str) and value.strip():
            return re.sub(r"\s+", " ", value).strip()[:160]
    labelled_by = element.get("aria-labelledby")
    if isinstance(labelled_by, str):
        names = [" ".join(soup.find(id=part).stripped_strings) for part in labelled_by.split() if soup.find(id=part)]
        if names:
            return re.sub(r"\s+", " ", " ".join(names)).strip()[:160]
    element_id = element.get("id")
    if isinstance(element_id, str):
        labels = [
            " ".join(label.stripped_strings)
            for label in soup.find_all("label")
            if label.get("for") == element_id
        ]
        if labels:
            return re.sub(r"\s+", " ", " ".join(labels)).strip()[:160]
    wrapping_label = element.find_parent("label")
    if wrapping_label:
        return re.sub(r"\s+", " ", " ".join(wrapping_label.stripped_strings)).strip()[:160]
    text_value = " ".join(element.stripped_strings)
    if text_value.strip():
        return re.sub(r"\s+", " ", text_value).strip()[:160]
    input_type = str(element.get("type", "text")).lower()
    if element.name == "input" and input_type in {"button", "submit", "reset"}:
        value = element.get("value")
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    name = element.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()[:160]
    if element.name == "input":
        return f"input ({input_type})"
    return str(element.name)


def _interaction_candidates(html: str, base_url: str, *, source: str) -> dict[str, Any]:
    soup = BeautifulSoup(html, "html.parser")
    id_counts: Counter[str] = Counter(
        element.get("id") for element in soup.find_all(attrs={"id": True})
    )
    candidates: list[tuple[int, dict[str, Any]]] = []
    pagination_identifier = re.compile(
        r"(?:^|[\s_-])(?:pagination|pager|page[-_]numbers?|page[-_]links?|page[-_]items?)(?:$|[\s_-])"
    )
    navigation_identifier = re.compile(
        r"(?:next|prev|previous|older|newer|load[-_]more)(?:[-_](?:page|posts?|results?|articles?|items?))?"
    )
    navigation_label = re.compile(
        r"(?:go to )?(?:next|prev|previous|older|newer|load more)"
        r"(?:\s+(?:page|posts?|results?|articles?|items?))?\s*[»›→]*"
    )
    navigation_tokens = {"next", "prev", "previous", "older", "newer", "load-more", "load_more"}
    for dom_index, element in enumerate(soup.find_all(True), start=1):
        tag = str(element.name).lower()
        role = str(element.get("role", "")).lower()
        priority: int | None = None
        kind = ""
        if tag == "button":
            priority, kind = 0, "button"
        elif tag == "input" and str(element.get("type", "text")).lower() not in {"hidden", "password", "file"}:
            priority, kind = 0, "input"
        elif tag in {"select", "textarea"}:
            priority, kind = 0, tag
        elif tag == "summary" or element.has_attr("aria-expanded"):
            priority, kind = 0, "expander"
        elif role in {"button", "tab", "switch"}:
            priority, kind = 0, "button_role"
        elif tag == "a" and role == "button":
            priority, kind = 0, "link_control"
        elif tag == "a" and element.has_attr("href"):
            rel_values = {str(value).casefold() for value in element.get("rel", [])}
            direct_identifiers = [
                str(element.get("id", "")),
                *(str(value) for value in element.get("class", [])),
            ]
            direct_navigation = any(
                value.casefold() in navigation_tokens
                or navigation_identifier.fullmatch(value.casefold())
                for value in direct_identifiers
            )
            pagination_hint = any(
                pagination_identifier.search(value.casefold())
                for value in direct_identifiers
            )
            parent = element.parent
            for _ in range(3):
                if not parent or not getattr(parent, "name", None):
                    break
                parent_values = [
                    str(parent.get("aria-label", "")),
                    str(parent.get("id", "")),
                    *(str(value) for value in parent.get("class", [])),
                ]
                pagination_hint = pagination_hint or any(
                    pagination_identifier.search(value.casefold())
                    for value in parent_values
                )
                parent = parent.parent
            labels = [
                str(element.get("aria-label", "")),
                str(element.get("title", "")),
                " ".join(element.stripped_strings),
            ]
            meaningful_navigation_label = any(
                navigation_label.fullmatch(re.sub(r"\s+", " ", label).strip().casefold())
                for label in labels
            )
            if (
                rel_values & {"next", "prev", "previous"}
                or direct_navigation
                or pagination_hint
                or meaningful_navigation_label
            ):
                priority, kind = 1, "pagination_link"
        if priority is None:
            continue
        href = element.get("href")
        candidate = {
            "dom_index": dom_index,
            "kind": kind,
            "name": _interaction_name(element, soup),
            "selector": _interaction_selector(element, id_counts),
            "source": source,
            "visibility": "not_computed",
            "hidden_attribute": element.has_attr("hidden") or element.get("aria-hidden") == "true",
            "disabled": element.has_attr("disabled") or element.get("aria-disabled") == "true",
        }
        if isinstance(href, str):
            candidate["url"] = urljoin(base_url, href)
        candidates.append((priority, candidate))
    candidates.sort(key=lambda item: (item[0], item[1]["dom_index"]))
    selected = [candidate for _, candidate in candidates[:MAX_INTERACTION_CANDIDATES]]
    return {
        "interaction_candidates": selected,
        "interaction_candidate_count": len(candidates),
        "interaction_candidates_truncated": len(candidates) > len(selected),
        "interaction_candidates_source": source,
        "interaction_visibility_note": "Candidates come from DOM attributes and text; computed CSS visibility, viewport position, and interactability are not verified.",
    }


def _write_new(path: Path, content: bytes) -> bool:
    """Write atomically, refusing to replace different immutable evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != content:
            raise ExplorationError("artifact.conflict", "An artifact already exists at the selected output path with different content")
        return False
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".part", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp_path, path)
        except FileExistsError:
            if path.read_bytes() != content:
                raise ExplorationError("artifact.conflict", "An artifact already exists at the selected output path with different content")
        return True
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _write_observation(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=f".{path.name}.", suffix=".part", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _load_actions(actions_json: str, actions_file: Path | None) -> list[dict[str, Any]]:
    if actions_json and actions_file is not None:
        raise ExplorationError("actions.ambiguous", "Use either --actions-json or --actions-file")
    if actions_file is not None:
        try:
            if actions_file.stat().st_size > MAX_ACTIONS_BYTES:
                raise ExplorationError("actions.invalid", f"Actions file must be at most {MAX_ACTIONS_BYTES} bytes")
            value = json.loads(actions_file.read_text(encoding="utf-8-sig"))
        except ExplorationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ExplorationError("actions.invalid", "Actions file must contain a JSON array") from exc
    elif actions_json:
        if len(actions_json.encode("utf-8")) > MAX_ACTIONS_BYTES:
            raise ExplorationError("actions.invalid", f"Actions JSON must be at most {MAX_ACTIONS_BYTES} bytes")
        try:
            value = json.loads(actions_json)
        except json.JSONDecodeError as exc:
            raise ExplorationError("actions.invalid", "Actions must be a JSON array") from exc
    else:
        value = []
    if not isinstance(value, list) or len(value) > MAX_ACTIONS:
        raise ExplorationError("actions.invalid", f"Actions must be a JSON array with at most {MAX_ACTIONS} steps")
    allowed = {
        "click": {"type", "selector"},
        "scroll": {"type", "x", "y", "selector"},
        "wait-selector": {"type", "selector", "state"},
        "wait-text": {"type", "text"},
        "wait": {"type", "seconds"},
    }
    for index, item in enumerate(value):
        action_type = item.get("type") if isinstance(item, dict) else None
        if (
            not isinstance(action_type, str)
            or action_type not in allowed
            or set(item) - allowed[action_type]
        ):
            raise ExplorationError("actions.invalid", f"Action {index + 1} has an unsupported shape or type")
        kind = action_type
        if kind in {"click", "wait-selector"} and (
            not isinstance(item.get("selector"), str) or not item["selector"].strip()
        ):
            raise ExplorationError("actions.invalid", f"Action {index + 1} requires a selector")
        if kind in {"click", "wait-selector"} and len(item["selector"]) > 2048:
            raise ExplorationError("actions.invalid", f"Action {index + 1} selector exceeds 2048 characters")
        if kind == "wait-text" and (
            not isinstance(item.get("text"), str) or not item["text"].strip()
        ):
            raise ExplorationError("actions.invalid", f"Action {index + 1} requires text")
        if kind == "wait-text" and len(item["text"]) > 2048:
            raise ExplorationError("actions.invalid", f"Action {index + 1} text exceeds 2048 characters")
        state = item.get("state", "visible")
        if kind == "wait-selector" and (
            not isinstance(state, str)
            or state not in {"visible", "hidden", "attached", "detached"}
        ):
            raise ExplorationError("actions.invalid", f"Action {index + 1} has an unsupported wait state")
        if kind == "wait" and (not isinstance(item.get("seconds"), (int, float)) or not 0 <= item["seconds"] <= 10):
            raise ExplorationError("actions.invalid", f"Action {index + 1} wait must be between 0 and 10 seconds")
        if kind == "scroll":
            if "selector" in item and not isinstance(item["selector"], str):
                raise ExplorationError("actions.invalid", f"Action {index + 1} selector must be text")
            if "selector" in item and not item["selector"].strip():
                raise ExplorationError("actions.invalid", f"Action {index + 1} selector must not be blank")
            if "selector" in item and len(item["selector"]) > 2048:
                raise ExplorationError("actions.invalid", f"Action {index + 1} selector exceeds 2048 characters")
            if "selector" not in item and not any(key in item for key in ("x", "y")):
                raise ExplorationError("actions.invalid", f"Action {index + 1} requires a selector or scroll offset")
            if any(not isinstance(item.get(key, 0), int) or abs(item.get(key, 0)) > 100000 for key in ("x", "y")):
                raise ExplorationError("actions.invalid", f"Action {index + 1} scroll offsets are outside the limit")
    return value


def _browser_capture(
    *,
    url: str,
    domains: list[str],
    actions: list[dict[str, Any]],
    wait_until: str,
    timeout_seconds: float,
    max_requests: int,
    max_dom_bytes: int,
    browser_channel: str = "bundled",
    headed: bool = False,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise ExplorationError("browser.runtime_missing", "Install the optional browser extra and its Chromium binary") from exc

    started = time.monotonic()
    deadline = started + timeout_seconds

    def remaining_timeout_ms() -> int:
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            raise ExplorationError("browser.timeout", "The browser time budget expired", retryable=True)
        return min(page_timeout_ms, remaining_ms)

    request_records: list[dict[str, Any]] = []
    request_by_id: dict[int, dict[str, Any]] = {}
    action_records: list[dict[str, Any]] = []
    main_document_response: Any = None
    request_limit_hit = False
    page_timeout_ms = int(timeout_seconds * 1000)
    browser = playwright = context = page = None
    browser_version: str | None = None
    primary_error: BaseException | None = None
    try:
        playwright = sync_playwright().start()
        launch_options: dict[str, Any] = {"headless": not headed, "timeout": remaining_timeout_ms()}
        if browser_channel != "bundled":
            launch_options["channel"] = browser_channel
        try:
            browser = playwright.chromium.launch(**launch_options)
        except PlaywrightTimeoutError as exc:
            raise ExplorationError(
                "browser.timeout", "Browser launch exceeded its time limit", retryable=True
            ) from exc
        except Exception as exc:
            if browser_channel == "bundled":
                raise ExplorationError(
                    "browser.binary_missing",
                    "Bundled Playwright Chromium could not start; install the matching browser binary",
                ) from exc
            raise ExplorationError(
                "browser.channel_unavailable",
                f"Browser channel {browser_channel!r} could not start; install it or choose another channel",
            ) from exc
        try:
            browser_version = getattr(browser, "version", None)
        except Exception:
            browser_version = None
        context = browser.new_context()

        def intercept(route: Any) -> None:
            nonlocal request_limit_hit
            request = route.request
            is_document_navigation = (
                request.is_navigation_request() and request.resource_type == "document"
            )
            try:
                is_main_frame_navigation = is_document_navigation and request.frame == page.main_frame
            except Exception:
                is_main_frame_navigation = False
            record = {
                "requested_url": request.url,
                "method": request.method,
                "resource_type": request.resource_type,
                "main_frame_navigation": is_main_frame_navigation,
                "requested_at": _utc_now(),
                "status": None,
                "final_url": request.url,
            }
            request_records.append(record)
            request_by_id[id(request)] = record
            if len(request_records) > max_requests:
                request_limit_hit = True
                record["policy_block"] = "request_limit"
                route.abort("blockedbyclient")
                return
            if is_document_navigation and not _host_allowed(request.url, domains):
                record["policy_block"] = "navigation_domain_not_allowed"
                route.abort("blockedbyclient")
                return
            try:
                route.continue_()
            except Exception:
                record["failure"] = "request_failed"
                raise

        context.route("**/*", intercept)
        page = context.new_page()
        page.set_default_timeout(remaining_timeout_ms())

        def on_response(response: Any) -> None:
            nonlocal main_document_response
            record = request_by_id.get(id(response.request))
            if record is not None:
                record["status"] = response.status
                record["final_url"] = response.url
                record["received_at"] = _utc_now()
                record["content_type"] = response.headers.get("content-type", "")
                record["content_range"] = response.headers.get("content-range")
            request = response.request
            if request.is_navigation_request() and request.resource_type == "document":
                try:
                    if request.frame == page.main_frame:
                        main_document_response = response
                except Exception:
                    pass

        def on_request_failed(request: Any) -> None:
            record = request_by_id.get(id(request))
            if record is not None:
                record["transport_failure"] = request.failure or "request_failed"
                record["failed_at"] = _utc_now()

        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)
        response = page.goto(url, wait_until=wait_until, timeout=remaining_timeout_ms())
        if response is None:
            raise ExplorationError("browser.navigation_failed", "The browser did not receive a main document response", retryable=True)
        if main_document_response is None:
            main_document_response = response
        if not 200 <= response.status < 400 and response.status != 403:
            hint = "; this does not establish that every URL on the site is inaccessible" if response.status == 403 else ""
            raise ExplorationError("http.status", f"The main document returned HTTP {response.status}{hint}", retryable=response.status >= 500)
        for index, action in enumerate(actions, start=1):
            action_timeout_ms = remaining_timeout_ms()
            kind = action["type"]
            try:
                if kind == "click":
                    page.locator(action["selector"]).first.click(timeout=action_timeout_ms)
                elif kind == "wait-selector":
                    page.locator(action["selector"]).first.wait_for(
                        state=action.get("state", "visible"), timeout=action_timeout_ms
                    )
                elif kind == "wait-text":
                    page.get_by_text(action["text"], exact=False).first.wait_for(
                        state="visible", timeout=action_timeout_ms
                    )
                elif kind == "scroll":
                    if "selector" in action:
                        page.locator(action["selector"]).first.scroll_into_view_if_needed(
                            timeout=action_timeout_ms
                        )
                    else:
                        page.evaluate(
                            "([x, y]) => window.scrollBy(x, y)",
                            [action.get("x", 0), action.get("y", 0)],
                        )
                else:
                    requested_wait_ms = int(action["seconds"] * 1000)
                    if requested_wait_ms > action_timeout_ms:
                        wait_started = time.monotonic()
                        page.wait_for_timeout(action_timeout_ms)
                        action_records.append(
                            {
                                "index": index,
                                "type": kind,
                                "action": action,
                                "status": "timeout",
                                "timeout_reason": "overall_deadline",
                                "wait_requested_ms": requested_wait_ms,
                                "waited_ms": min(
                                    requested_wait_ms,
                                    max(0, int((time.monotonic() - wait_started) * 1000)),
                                ),
                                "completed_at": _utc_now(),
                                "page_url": page.url,
                                "title": page.title(),
                            }
                        )
                        raise ExplorationError(
                            "browser.timeout",
                            f"Browser time budget expired before wait action {index} completed",
                            retryable=True,
                        )
                    page.wait_for_timeout(requested_wait_ms)
            except PlaywrightTimeoutError as exc:
                action_records.append(
                    {
                        "index": index,
                        "type": kind,
                        "action": action,
                        "status": "timeout",
                        "completed_at": _utc_now(),
                        "page_url": page.url,
                        "title": page.title(),
                    }
                )
                raise ExplorationError("browser.action_timeout", f"Browser action {index} timed out", retryable=True) from exc
            action_records.append(
                {
                    "index": index,
                    "type": kind,
                    "action": action,
                    "status": "completed",
                    "completed_at": _utc_now(),
                    "page_url": page.url,
                    "title": page.title(),
                }
            )
        remaining_timeout_ms()
        html = page.content()
        html_bytes = html.encode("utf-8")
        if len(html_bytes) > max_dom_bytes:
            raise ExplorationError("browser.dom_too_large", f"Rendered HTML exceeded the {max_dom_bytes}-byte limit")
        text_content, links = _normalized_dom(html, page.url)
        final_response = main_document_response or response
        final_status = final_response.status
        final_content_type = final_response.headers.get("content-type", "")
        if not 200 <= final_status < 400 and final_status != 403:
            hint = "; this does not establish that every URL on the site is inaccessible" if final_status == 403 else ""
            raise ExplorationError(
                "http.status",
                f"The final main document returned HTTP {final_status}{hint}",
                retryable=final_status >= 500,
            )
        return {
            "requested_url": url,
            "final_url": page.url,
            "title": page.title(),
            "status": final_status,
            "content_type": final_content_type,
            "content_range": final_response.headers.get("content-range"),
            "browser_channel": browser_channel,
            "browser_version": browser_version,
            "headless": not headed,
            "started_at": request_records[0]["requested_at"] if request_records else _utc_now(),
            "finished_at": _utc_now(),
            "html": html,
            "html_sha256": hashlib.sha256(html_bytes).hexdigest(),
            "html_bytes": len(html_bytes),
            "text": text_content,
            "links": links,
            "actions": action_records,
            "requests": request_records,
            "request_count": len(request_records),
            "request_limit_hit": request_limit_hit,
        }
    except BaseException as exc:
        primary_error = exc
    finally:
        cleanup_errors: list[str] = []
        for resource, close in ((page, "close"), (context, "close"), (browser, "close"), (playwright, "stop")):
            if resource is None:
                continue
            try:
                getattr(resource, close)()
            except BaseException as exc:
                cleanup_errors.append(type(exc).__name__)
        if primary_error is None and cleanup_errors:
            primary_error = ExplorationError("browser.cleanup_failed", "The browser could not close all resources")
    if primary_error is not None:
        error_data = {
            "actions": action_records,
            "requests": request_records,
            "request_count": len(request_records),
            "request_limit_hit": request_limit_hit,
            "browser_channel": browser_channel,
            "browser_version": browser_version,
            "headless": not headed,
        }
        if request_limit_hit and not isinstance(primary_error, KeyboardInterrupt):
            raise ExplorationError(
                "browser.request_limit",
                f"Browser request budget exceeded ({max_requests})",
                attempts=request_records,
                data=error_data,
            ) from primary_error
        blocked_main_navigation = next(
            (
                record
                for record in request_records
                if record.get("main_frame_navigation")
                and record.get("policy_block") == "navigation_domain_not_allowed"
            ),
            None,
        )
        if blocked_main_navigation is not None and not isinstance(primary_error, KeyboardInterrupt):
            raise ExplorationError(
                "navigation.domain_not_allowed",
                "A main-frame navigation was blocked; inspect the request ledger and explicitly allow its public host if authorized",
                attempts=request_records,
                data=error_data,
            ) from primary_error
        if isinstance(primary_error, ExplorationError):
            primary_error.attempts = request_records
            primary_error.data = error_data
            raise primary_error
        if isinstance(primary_error, PlaywrightTimeoutError):
            raise ExplorationError(
                "browser.timeout",
                "Browser navigation exceeded its time limit",
                retryable=True,
                attempts=request_records,
                data=error_data,
            ) from primary_error
        if isinstance(primary_error, KeyboardInterrupt):
            raise primary_error
        raise ExplorationError(
            "browser.failed",
            "Browser reading failed; retry with --method http or inspect the target manually",
            retryable=True,
            attempts=request_records,
            data=error_data,
        ) from primary_error
    raise ExplorationError("browser.failed", "Browser reading failed")


def _browser_error_data(
    data: dict[str, Any] | None,
    *,
    browser_channel: str,
    headed: bool,
    capture: Any,
) -> dict[str, Any]:
    result = dict(data or {})
    if result.get("browser_channel") is None:
        result["browser_channel"] = browser_channel
    result.setdefault("browser_version", None)
    if result.get("headless") is None:
        result["headless"] = not headed
    if isinstance(capture, dict):
        for key in ("browser_channel", "browser_version", "headless"):
            if key in capture:
                result[key] = capture[key]
    return result


async def _read_page_http(
    *,
    url: str,
    domains: list[str],
    max_http_bytes: int,
    user_agent: str,
    deadline: float,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    redirects: list[dict[str, Any]] = []
    current_url = url
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExplorationError("http.timeout", "HTTP page read exceeded its overall time limit", retryable=True, attempts=attempts)
    try:
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(
                timeout=None,
                headers={"User-Agent": user_agent},
                trust_env=False,
            ) as client:
                while True:
                    record = {
                        "requested_url": current_url,
                        "method": "GET",
                        "resource_type": "document",
                        "requested_at": _utc_now(),
                        "status": None,
                        "user_agent": user_agent,
                    }
                    attempts.append(record)
                    try:
                        async with client.stream(
                            "GET", current_url, follow_redirects=False
                        ) as response:
                            record.update(
                                status=response.status_code,
                                final_url=str(response.url),
                                content_type=response.headers.get("content-type", ""),
                                content_range=response.headers.get("content-range"),
                            )
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location or len(redirects) >= 10:
                                    raise ExplorationError(
                                        "http.redirect_invalid",
                                        "HTTP redirect chain was invalid or exceeded 10 hops",
                                        attempts=attempts,
                                    )
                                next_url = urljoin(current_url, location)
                                record["redirect_target_url"] = next_url
                                if not _host_allowed(next_url, domains):
                                    raise ExplorationError(
                                        "navigation.domain_not_allowed",
                                        "Redirect target is outside the allowed domains",
                                        attempts=attempts,
                                    )
                                redirects.append(
                                    {
                                        "from_url": current_url,
                                        "to_url": next_url,
                                        "status": response.status_code,
                                    }
                                )
                                current_url = next_url
                                continue
                            if not 200 <= response.status_code < 300:
                                hint = (
                                    "; this does not establish that every URL on the site is inaccessible"
                                    if response.status_code == 403
                                    else ""
                                )
                                raise ExplorationError(
                                    "http.status",
                                    f"HTTP page request returned status {response.status_code}{hint}",
                                    retryable=response.status_code >= 500,
                                    attempts=attempts,
                                )
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > max_http_bytes:
                                    raise ExplorationError(
                                        "page.too_large",
                                        f"HTTP page exceeded the {max_http_bytes}-byte limit",
                                        attempts=attempts,
                                    )
                            return {
                                "status": response.status_code,
                                "final_url": str(response.url),
                                "content_type": response.headers.get("content-type", ""),
                                "content_range": response.headers.get("content-range"),
                                "body": bytes(body),
                                "redirects": redirects,
                            }
                    except httpx.TimeoutException as exc:
                        raise ExplorationError(
                            "http.timeout",
                            "HTTP page read exceeded its overall time limit",
                            retryable=True,
                            attempts=attempts,
                        ) from exc
                    except httpx.HTTPError as exc:
                        raise ExplorationError(
                            "http.request_failed",
                            "HTTP page request failed",
                            retryable=True,
                            attempts=attempts,
                        ) from exc
    except TimeoutError as exc:
        raise ExplorationError(
            "http.timeout",
            "HTTP page read exceeded its overall time limit",
            retryable=True,
            attempts=attempts,
        ) from exc


async def _download_http_to_part(
    *,
    url: str,
    domains: list[str],
    output_dir: Path,
    max_bytes: int,
    pacing_seconds: float,
    jitter_seconds: float,
    user_agent: str,
    deadline: float,
    attempts: list[dict[str, Any]],
) -> dict[str, Any]:
    redirects: list[dict[str, Any]] = []
    current_url = url
    part_path: Path | None = None
    keep_part = False
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise ExplorationError("download.timeout", "File download exceeded its overall time limit", retryable=True, attempts=attempts)
    try:
        async with asyncio.timeout(remaining):
            async with httpx.AsyncClient(
                timeout=None,
                headers={"User-Agent": user_agent},
                trust_env=False,
            ) as client:
                while True:
                    wait_seconds = pacing_seconds + random.uniform(0, jitter_seconds)
                    await asyncio.sleep(wait_seconds)
                    requested_at = _utc_now()  # Recorded after pacing, immediately before this request.
                    record = {
                        "requested_url": current_url,
                        "method": "GET",
                        "resource_type": "document",
                        "requested_at": requested_at,
                        "pacing_wait_seconds": round(wait_seconds, 3),
                        "status": None,
                        "user_agent": user_agent,
                    }
                    attempts.append(record)
                    try:
                        async with client.stream(
                            "GET", current_url, follow_redirects=False
                        ) as response:
                            record.update(
                                status=response.status_code,
                                final_url=str(response.url),
                                content_type=_response_content_type(response),
                            )
                            if response.status_code in {301, 302, 303, 307, 308}:
                                location = response.headers.get("location")
                                if not location or len(redirects) >= 10:
                                    raise ExplorationError(
                                        "download.redirect_invalid",
                                        "File redirect chain was invalid or exceeded 10 hops",
                                        attempts=attempts,
                                    )
                                next_url = urljoin(current_url, location)
                                record["redirect_target_url"] = next_url
                                if not _host_allowed(next_url, domains):
                                    raise ExplorationError(
                                        "navigation.domain_not_allowed",
                                        "Redirect target is outside the allowed domains",
                                        attempts=attempts,
                                    )
                                redirects.append(
                                    {
                                        "from_url": current_url,
                                        "to_url": next_url,
                                        "status": response.status_code,
                                    }
                                )
                                current_url = next_url
                                continue
                            if response.status_code == 206:
                                raise ExplorationError(
                                    "download.partial_response",
                                    "The server returned partial content; this command does not request or assemble byte ranges",
                                    attempts=attempts,
                                )
                            if not 200 <= response.status_code < 300:
                                hint = (
                                    "; a 403 response does not establish that the whole site is inaccessible"
                                    if response.status_code == 403
                                    else ""
                                )
                                raise ExplorationError(
                                    "http.status",
                                    f"File request returned status {response.status_code}{hint}",
                                    retryable=response.status_code >= 500,
                                    attempts=attempts,
                                )
                            content_type = _response_content_type(response)
                            content_length = response.headers.get("content-length")
                            if content_length and content_length.isdigit() and int(content_length) > max_bytes:
                                raise ExplorationError(
                                    "download.too_large",
                                    f"Content-Length exceeds the {max_bytes}-byte limit",
                                    attempts=attempts,
                                )
                            download_dir = output_dir / "downloads"
                            download_dir.mkdir(parents=True, exist_ok=True)
                            with tempfile.NamedTemporaryFile(
                                mode="wb", dir=download_dir, prefix=".download-", suffix=".part", delete=False
                            ) as stream:
                                part_path = Path(stream.name)
                                digest = hashlib.sha256()
                                size = 0
                                prefix = bytearray()
                                async for chunk in response.aiter_bytes():
                                    if not chunk:
                                        continue
                                    if len(prefix) < 512:
                                        prefix.extend(chunk[: 512 - len(prefix)])
                                    size += len(chunk)
                                    if size > max_bytes:
                                        raise ExplorationError(
                                            "download.too_large",
                                            f"File exceeded the {max_bytes}-byte limit",
                                            attempts=attempts,
                                        )
                                    digest.update(chunk)
                                    stream.write(chunk)
                                stream.flush()
                                os.fsync(stream.fileno())
                            transfer = {
                                "part_path": part_path,
                                "size": size,
                                "sha256": digest.hexdigest(),
                                "prefix": bytes(prefix),
                                "content_type": content_type,
                                "final_url": str(response.url),
                                "etag": response.headers.get("etag"),
                                "last_modified": response.headers.get("last-modified"),
                                "request_started_at": requested_at,
                                "redirects": redirects,
                            }
                            break
                    except httpx.TimeoutException as exc:
                        raise ExplorationError(
                            "download.timeout",
                            "File download exceeded its overall time limit",
                            retryable=True,
                            attempts=attempts,
                        ) from exc
                    except httpx.HTTPError as exc:
                        raise ExplorationError(
                            "download.request_failed",
                            "File request failed",
                            retryable=True,
                            attempts=attempts,
                        ) from exc
        keep_part = True
        return transfer
    except TimeoutError as exc:
        raise ExplorationError(
            "download.timeout",
            "File download exceeded its overall time limit",
            retryable=True,
            attempts=attempts,
        ) from exc
    finally:
        if part_path is not None and not keep_part:
            part_path.unlink(missing_ok=True)


def read_page(
    *,
    url: str,
    method: str,
    output_dir: Path,
    allowed_domains: list[str] | None,
    actions_json: str,
    actions_file: Path | None,
    wait_until: str,
    timeout_seconds: float,
    max_requests: int,
    max_dom_bytes: int,
    max_http_bytes: int,
    browser_channel: str = "bundled",
    headed: bool = False,
    user_agent: str | None = None,
) -> ToolResult:
    tool = "read-page"
    actual_user_agent: str | None = None
    pdf_signature = False
    attempts: list[dict[str, Any]] = []
    try:
        url = _validate_url(url)
        domains = _allowed_domains(url, allowed_domains)
        if method not in {"http", "browser"}:
            raise ExplorationError("method.invalid", "Method must be http or browser")
        if method == "http":
            actual_user_agent = _resolve_http_user_agent(user_agent)
        else:
            _validate_browser_user_agent(user_agent)
        if browser_channel not in {"bundled", "chromium", "chrome", "msedge"}:
            raise ExplorationError("browser_channel.invalid", "Browser channel must be bundled, chromium, chrome, or msedge")
        if method == "http" and (browser_channel != "bundled" or headed):
            raise ExplorationError("browser_options.unsupported", "Browser channel and --headed require --method browser")
        if wait_until not in {"domcontentloaded", "load", "networkidle"}:
            raise ExplorationError("wait.invalid", "wait-until must be domcontentloaded, load, or networkidle")
        if not 1 <= timeout_seconds <= MAX_ACTION_SECONDS:
            raise ExplorationError("timeout.invalid", f"Timeout must be between 1 and {MAX_ACTION_SECONDS} seconds")
        if not 1 <= max_requests <= MAX_BROWSER_REQUESTS:
            raise ExplorationError("budget.invalid", f"Max requests must be between 1 and {MAX_BROWSER_REQUESTS}")
        if not 1024 <= max_dom_bytes <= MAX_PAGE_BYTES or not 1024 <= max_http_bytes <= MAX_PAGE_BYTES:
            raise ExplorationError("budget.invalid", f"Page byte limits must be between 1024 and {MAX_PAGE_BYTES}")
        actions = _load_actions(actions_json, actions_file)
        if method == "http" and actions:
            raise ExplorationError("actions.unsupported", "Page actions require --method browser")
        if method == "browser":
            capture = _browser_capture(
                url=url,
                domains=domains,
                actions=actions,
                wait_until=wait_until,
                timeout_seconds=timeout_seconds,
                max_requests=max_requests,
                max_dom_bytes=max_dom_bytes,
                browser_channel=browser_channel,
                headed=headed,
            )
            html = capture.pop("html")
            attempts = capture.pop("requests")
            capture["requests"] = attempts
            text_content = capture["text"]
            links = capture["links"]
            status = capture["status"]
            started_at = capture["started_at"]
        else:
            deadline = time.monotonic() + timeout_seconds
            started_at = _utc_now()
            transfer = asyncio.run(
                _read_page_http(
                    url=url,
                    domains=domains,
                    max_http_bytes=max_http_bytes,
                    user_agent=actual_user_agent,
                    deadline=deadline,
                    attempts=attempts,
                )
            )
            redirects = transfer["redirects"]
            status = transfer["status"]
            final_url = transfer["final_url"]
            content_type = transfer["content_type"]
            content_range = transfer["content_range"]
            raw_body = transfer["body"]
            pdf_signature = raw_body.startswith(b"%PDF-")
            html = _decode_http_html(raw_body, content_type)
            text_content, links = _normalized_dom(html, final_url)
            finished_at = _utc_now()
            capture = {
                "requested_url": url,
                "final_url": final_url,
                "title": BeautifulSoup(html, "html.parser").title.get_text(" ", strip=True) if BeautifulSoup(html, "html.parser").title else "",
                "status": status,
                "content_type": content_type,
                "content_range": content_range,
                "started_at": started_at,
                "finished_at": finished_at,
                "html_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
                "html_bytes": len(html.encode("utf-8")),
                "text": text_content,
                "links": links,
                "actions": [],
                "request_count": len(attempts),
                "request_limit_hit": False,
                "redirects": redirects,
                "user_agent": actual_user_agent,
            }
        content_type = capture.get("content_type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if capture.get("status") == 206 or capture.get("content_range"):
            raise ExplorationError(
                "page.partial_response",
                "The main document response is partial; retry after verifying a complete page response is available",
                attempts=attempts,
                data=dict(capture),
            )
        if (media_type and media_type not in {"text/html", "application/xhtml+xml"}) or pdf_signature:
            non_html_data = dict(capture)
            non_html_data["pdf_signature"] = pdf_signature
            if method == "http":
                non_html_data["body_bytes"] = len(raw_body)
            raise ExplorationError(
                "page.non_html_response",
                "The response is not an HTML page; use download-file",
                attempts=attempts,
                data=non_html_data,
            )
        capture.update(
            _interaction_candidates(
                html,
                capture["final_url"],
                source="rendered_dom" if method == "browser" else "static_dom",
            )
        )
        if capture.get("request_limit_hit"):
            raise ExplorationError(
                "browser.request_limit",
                f"Browser request budget exceeded ({max_requests})",
                attempts=attempts,
                data=capture,
            )
        classification = classify_html_capture(
            requested_url=url,
            final_url=capture["final_url"],
            status_code=status,
            extracted_text=text_content,
            raw_text=html,
        )
        capture["classification"] = classification
        started_safe = re.sub(r"[^0-9TZ]", "", started_at)[:17]
        url_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        stem = f"{url_digest}-{started_safe}"
        pages_dir = output_dir / "pages"
        html_path = pages_dir / f"{stem}.html"
        result_path = pages_dir / f"{stem}.json"
        _write_new(html_path, html.encode("utf-8"))
        capture["output_html"] = str(html_path.resolve())
        capture["output_json"] = str(result_path.resolve())
        result_document = {"schema_version": "exploration-page.v1", **capture}
        _write_new(result_path, _json_bytes(result_document))
        data_status = (
            "blocked"
            if classification in {"blocked", "blocked_redirect", "http_403", "http_status_rejected"}
            else "empty" if classification == "empty_content" else "present"
        )
        content_type = capture.get("content_type", "").lower()
        if "application/pdf" in content_type or "application/octet-stream" in content_type:
            next_action = "use_download_file"
        elif method == "http" and data_status in {"blocked", "empty"}:
            next_action = "retry_with_browser"
        elif data_status == "blocked":
            next_action = "inspect_blocked_evidence_or_choose_another_authorized_url"
        elif data_status == "empty":
            next_action = "inspect_empty_page"
        else:
            next_action = None
        if method == "browser" and any(
            attempt.get("policy_block") == "navigation_domain_not_allowed"
            for attempt in attempts
        ):
            next_action = "inspect_request_ledger_and_allow_required_public_navigation_domain"
        return _result(
            tool=tool,
            data=capture,
            artifacts={"page_html": str(html_path.resolve()), "page_result": str(result_path.resolve())},
            attempts=attempts,
            data_status=data_status,
            data_count=len(text_content.split()) if classification == "accepted" else 0,
            next_action=next_action,
            stop_reason=classification if classification != "accepted" else "completed",
            data_quality=ToolResultDataQuality(
                passed=classification == "accepted",
                status_code=status,
                word_count=len(text_content.split()),
                link_count=len(links),
                document_link_count=sum(
                    1
                    for link in links
                    if Path(urlparse(link["url"]).path).suffix.lower()
                    in {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}
                ),
                blocked=data_status == "blocked",
                failure_reasons=[] if classification == "accepted" else [classification],
            ),
        )
    except ExplorationError as exc:
        if exc.code == "user_agent.invalid":
            next_action = "correct_user_agent"
        elif exc.code == "user_agent.unsupported":
            next_action = "remove_user_agent_for_browser_mode"
        elif exc.code.startswith("actions."):
            next_action = "correct_actions"
        elif exc.code in {"browser.runtime_missing", "browser.binary_missing"}:
            next_action = "install_browser_runtime"
        elif exc.code == "browser.channel_unavailable":
            next_action = "install_or_select_browser_channel"
        elif exc.code == "http.status" and locals().get("attempts") and locals()["attempts"][-1].get("status") == 403:
            next_action = "retry_with_browser" if locals().get("method") == "http" else "inspect_access_or_choose_authorized_url"
        elif exc.code in {"browser.timeout", "browser.action_timeout", "http.timeout"}:
            next_action = "retry_within_timeout_limit"
        elif exc.code == "browser.request_limit":
            next_action = "retry_single_read_page_with_max_requests_or_reduce_page_load"
        elif exc.code == "navigation.domain_not_allowed":
            next_action = "inspect_request_ledger_and_allow_required_public_navigation_domain"
        elif exc.code == "page.non_html_response":
            next_action = "use_download_file"
        elif exc.code == "page.partial_response":
            next_action = "retry_for_complete_page_response"
        elif exc.code in {"url.invalid", "domain.invalid"}:
            next_action = "correct_url_or_allowed_domains"
        elif exc.code in {"method.invalid", "wait.invalid", "timeout.invalid", "budget.invalid", "browser_channel.invalid", "browser_options.unsupported"}:
            next_action = "correct_arguments"
        elif exc.code in {"http.status", "http.request_failed", "browser.failed", "browser.navigation_failed"}:
            next_action = "inspect_url_or_choose_another_authorized_method"
        else:
            next_action = "inspect_error_and_retry_if_appropriate"
        error_data = dict(exc.data)
        if method == "browser":
            error_data = _browser_error_data(
                error_data,
                browser_channel=browser_channel,
                headed=headed,
                capture=locals().get("capture"),
            )
        elif actual_user_agent is not None:
            error_data["user_agent"] = actual_user_agent
        return _result(
            tool=tool,
            error=exc,
            data=error_data,
            attempts=exc.attempts or locals().get("attempts", []),
            next_action=next_action,
        )
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        error_data = {}
        if method == "browser":
            error_data = _browser_error_data(
                None,
                browser_channel=browser_channel,
                headed=headed,
                capture=locals().get("capture"),
            )
        elif actual_user_agent is not None:
            error_data["user_agent"] = actual_user_agent
        return _result(
            tool=tool,
            error=ExplorationError("read.failed", "Page reading failed; inspect the URL, runtime, and available time budget", retryable=True),
            data=error_data,
            attempts=locals().get("attempts", []),
            next_action="inspect_runtime_or_retry",
        )


def _response_content_type(response: httpx.Response) -> str:
    return response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()


def download_file(
    *,
    url: str,
    output_dir: Path,
    allowed_domains: list[str] | None,
    timeout_seconds: float,
    max_bytes: int,
    pacing_seconds: float,
    jitter_seconds: float,
    user_agent: str | None = None,
) -> ToolResult:
    tool = "download-file"
    attempts: list[dict[str, Any]] = []
    part_path: Path | None = None
    actual_user_agent: str | None = None
    try:
        url = _validate_url(url)
        domains = _allowed_domains(url, allowed_domains)
        actual_user_agent = _resolve_http_user_agent(user_agent)
        if not 1 <= timeout_seconds <= MAX_HTTP_TIMEOUT_SECONDS:
            raise ExplorationError("timeout.invalid", f"Timeout must be between 1 and {MAX_HTTP_TIMEOUT_SECONDS} seconds")
        if not 1 <= max_bytes <= MAX_DOWNLOAD_BYTES:
            raise ExplorationError("budget.invalid", f"Max bytes must be between 1 and {MAX_DOWNLOAD_BYTES}")
        if not 0 <= pacing_seconds <= 60 or not 0 <= jitter_seconds <= 30:
            raise ExplorationError("pacing.invalid", "Pacing must be 0-60 seconds and jitter 0-30 seconds")
        deadline = time.monotonic() + timeout_seconds
        transfer = asyncio.run(
            _download_http_to_part(
                url=url,
                domains=domains,
                output_dir=output_dir,
                max_bytes=max_bytes,
                pacing_seconds=pacing_seconds,
                jitter_seconds=jitter_seconds,
                user_agent=actual_user_agent,
                deadline=deadline,
                attempts=attempts,
            )
        )
        part_path = transfer["part_path"]
        size = transfer["size"]
        prefix = transfer["prefix"]
        content_type = transfer["content_type"]
        current_url = transfer["final_url"]
        requested_at = transfer["request_started_at"]
        redirects = transfer["redirects"]
        download_dir = output_dir / "downloads"
        raw_prefix = bytes(prefix).lstrip(b"\xef\xbb\xbf \t\r\n").lower()
        is_pdf_expected = content_type == "application/pdf" or urlparse(url).path.lower().endswith(".pdf")
        has_pdf_magic = bytes(prefix).startswith(b"%PDF-")
        is_html = not has_pdf_magic and (
            content_type in {"text/html", "application/xhtml+xml"}
            or raw_prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))
        )
        if is_pdf_expected and is_html:
            raise ExplorationError("download.html_response", "The server returned an HTML page for the requested PDF; no file artifact was saved")
        if is_pdf_expected and not has_pdf_magic:
            raise ExplorationError("download.not_pdf", "The response did not contain a PDF signature")
        if has_pdf_magic:
            with part_path.open("rb") as stream:
                stream.seek(max(0, size - 2048))
                pdf_tail = stream.read()
            if b"%%EOF" not in pdf_tail:
                raise ExplorationError("download.pdf_truncated", "Downloaded PDF has no end marker; retry the file download")
        hexdigest = transfer["sha256"]
        suffix = ".pdf" if has_pdf_magic else Path(urlparse(current_url).path).suffix.lower()
        if not suffix or len(suffix) > 12 or not re.fullmatch(r"\.[a-z0-9]{1,10}", suffix):
            suffix = mimetypes.guess_extension(content_type) or ".bin"
        artifact_path = download_dir / f"{hexdigest}{suffix}"
        artifact_reused = artifact_path.exists()
        if artifact_reused:
            if artifact_path.stat().st_size != size or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != hexdigest:
                raise ExplorationError("artifact.conflict", "Existing content-addressed file does not match its filename hash")
            part_path.unlink(missing_ok=True)
            part_path = None
        else:
            try:
                os.link(part_path, artifact_path)
            except FileExistsError:
                if artifact_path.stat().st_size != size or hashlib.sha256(artifact_path.read_bytes()).hexdigest() != hexdigest:
                    raise ExplorationError("artifact.conflict", "Existing content-addressed file does not match its filename hash")
            part_path.unlink(missing_ok=True)
            part_path = None
        finished_at = _utc_now()
        source_digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
        metadata_path = download_dir / f"{artifact_path.name}.{source_digest}.source.json"
        metadata = {
            "schema_version": "exploration-download-source.v1",
            "source_url": url,
            "requested_url": url,
            "final_url": current_url,
            "sha256": hexdigest,
            "size_bytes": size,
            "mime_type": content_type,
            "etag": transfer["etag"],
            "last_modified": transfer["last_modified"],
            "request_started_at": requested_at,
            "downloaded_at": finished_at,
            "redirects": redirects,
            "user_agent": actual_user_agent,
        }
        _write_observation(metadata_path, _json_bytes(metadata))
        return _result(
            tool=tool,
            data={**metadata, "artifact_path": str(artifact_path.resolve()), "source_metadata_path": str(metadata_path.resolve()), "artifact_reused": artifact_reused},
            artifacts={"file": str(artifact_path.resolve()), "source_metadata": str(metadata_path.resolve())},
            attempts=attempts,
            data_status="artifact_only",
            data_count=size,
            data_quality=ToolResultDataQuality(
                passed=True,
                status_code=attempts[-1]["status"],
                document_link_count=1,
            ),
        )
    except ExplorationError as exc:
        if exc.code == "user_agent.invalid":
            next_action = "correct_user_agent"
        elif exc.code in {"http.status"} and attempts and attempts[-1].get("status") == 403:
            next_action = "read_source_page_for_an_alternative_file_url"
        elif exc.code in {"download.partial_response", "download.pdf_truncated", "download.html_response", "download.not_pdf"}:
            next_action = "verify_source_or_redownload_complete_file"
        elif exc.code == "download.too_large":
            next_action = "choose_a_supported_file_under_the_size_limit"
        elif exc.code in {"download.timeout", "download.request_failed"}:
            next_action = "retry_download_if_appropriate"
        elif exc.code in {"url.invalid", "domain.invalid", "navigation.domain_not_allowed"}:
            next_action = "correct_url_or_allowed_domains"
        elif exc.code in {"timeout.invalid", "budget.invalid", "pacing.invalid"}:
            next_action = "correct_arguments"
        else:
            next_action = "inspect_file_url_or_download_error"
        return _result(
            tool=tool,
            error=exc,
            data={"user_agent": actual_user_agent} if actual_user_agent is not None else {},
            attempts=attempts,
            next_action=next_action,
        )
    except KeyboardInterrupt:
        raise
    except Exception:
        return _result(
            tool=tool,
            error=ExplorationError("download.failed", "File download failed; inspect the URL, network response, and available time budget", retryable=True),
            data={"user_agent": actual_user_agent} if actual_user_agent is not None else {},
            attempts=attempts,
            next_action="inspect_error_or_adjust_limit",
        )
    finally:
        if part_path is not None:
            part_path.unlink(missing_ok=True)


def _pdf_source_url(path: Path, source_url: str | None, expected_sha256: str) -> str | None:
    candidates = list(path.parent.glob(f"{path.name}.*.source.json"))
    matching: list[dict[str, Any]] = []
    for candidate in candidates:
        try:
            value = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if value.get("sha256") != expected_sha256:
            continue
        if source_url is None or value.get("source_url") == source_url:
            matching.append(value)
    if len(matching) > 1:
        raise ExplorationError("source.ambiguous", "Several download sources match this PDF; pass --source-url to select one")
    if matching:
        return str(matching[0].get("source_url") or "") or None
    return source_url


def extract_pdf(
    *,
    input_path: Path,
    output_dir: Path,
    source_url: str | None,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> ToolResult:
    tool = "extract-pdf"
    try:
        input_path = input_path.expanduser().resolve()
        if not input_path.is_file():
            raise ExplorationError("input.not_found", "Input PDF file does not exist")
        if not 1 <= max_bytes <= MAX_DOWNLOAD_BYTES:
            raise ExplorationError("budget.invalid", f"PDF byte limit must be between 1 and {MAX_DOWNLOAD_BYTES}")
        if input_path.stat().st_size > max_bytes:
            raise ExplorationError("pdf.too_large", f"PDF exceeds the {max_bytes}-byte limit")
        try:
            parser_version = version("pypdf")
        except PackageNotFoundError as exc:
            raise ExplorationError("pdf.runtime_missing", "Install the optional PDF extra: pip install 'web-listening[pdf]'") from exc
        if not (parser_version == "6.21.3" or parser_version.startswith("6.")):
            raise ExplorationError("pdf.parser_version", f"pypdf {parser_version} is outside the supported major version 6")
        content = input_path.read_bytes()
        source_sha256 = hashlib.sha256(content).hexdigest()
        if not content.startswith(b"%PDF-"):
            raise ExplorationError("pdf.invalid", "Input does not begin with a PDF signature")
        if b"%%EOF" not in content[-2048:]:
            raise ExplorationError("pdf.truncated", "PDF has no end marker near the end of the file; download it again")
        if source_url is not None:
            source_url = _validate_url(source_url)
        resolved_source_url = _pdf_source_url(input_path, source_url, source_sha256)
        try:
            from pypdf import PdfReader

            reader = PdfReader(BytesIO(content), strict=True)
            if reader.is_encrypted:
                raise ExplorationError("pdf.encrypted", "Encrypted PDFs are not supported by this extractor")
            page_count = len(reader.pages)
            pages = []
            page_errors = 0
            empty_pages = 0
            for number, page in enumerate(reader.pages, start=1):
                try:
                    text_content = page.extract_text() or ""
                except Exception:
                    text_content = ""
                    page_errors += 1
                if not text_content.strip():
                    empty_pages += 1
                pages.append({"page_number": number, "text": text_content})
            metadata = {
                str(key): str(value) if value is not None else ""
                for key, value in (reader.metadata or {}).items()
            }
        except ExplorationError:
            raise
        except Exception as exc:
            raise ExplorationError("pdf.parse_failed", "PDF could not be parsed; check that it is complete and readable") from exc
        output_dir.mkdir(parents=True, exist_ok=True)
        source_binding = (
            hashlib.sha256(resolved_source_url.encode("utf-8")).hexdigest()[:12]
            if resolved_source_url
            else "no-source"
        )
        output_path = (
            output_dir
            / "extracted"
            / f"{source_sha256}-pypdf-{parser_version}-source-{source_binding}.json"
        )
        words = sum(len(page["text"].split()) for page in pages)
        output_document = {
            "schema_version": "exploration-pdf-text.v1",
            "source_url": resolved_source_url,
            "source_sha256": source_sha256,
            "size_bytes": len(content),
            "parser": {"name": "pypdf", "version": parser_version},
            "metadata": metadata,
            "page_count": page_count,
            "empty_text_page_count": empty_pages,
            "page_error_count": page_errors,
            "status": "partial" if page_errors else "present" if words else "empty",
            "pages": pages,
        }
        output_created = _write_new(output_path, _json_bytes(output_document))
        if not output_created:
            output_document = json.loads(output_path.read_text(encoding="utf-8"))
        return _result(
            tool=tool,
            data={key: value for key, value in output_document.items() if key != "pages"} | {"output_json": str(output_path.resolve()), "artifact_reused": not output_created},
            artifacts={"page_text": str(output_path.resolve())},
            data_status=output_document["status"],
            data_count=words,
            next_action=(
                "review_failed_page_text"
                if page_errors
                else "inspect_scanned_or_empty_pages"
                if words == 0
                else None
            ),
            stop_reason=(
                output_document["status"]
                if output_document["status"] != "present"
                else "completed"
            ),
            data_quality=ToolResultDataQuality(
                passed=output_document["status"] == "present" and words > 0,
                word_count=words,
                failure_reasons=(
                    ["one_or_more_pages_failed_text_extraction"]
                    if page_errors
                    else ["no_text_extracted"] if words == 0 else []
                ),
            ),
        )
    except ExplorationError as exc:
        next_action = {
            "input.not_found": "correct_input_path",
            "pdf.runtime_missing": "install_pdf_runtime",
            "pdf.parser_version": "install_supported_pdf_runtime",
            "pdf.truncated": "redownload_complete_pdf",
            "pdf.invalid": "provide_a_valid_pdf",
            "pdf.encrypted": "provide_an_unencrypted_pdf",
            "pdf.too_large": "choose_a_supported_pdf_under_the_size_limit",
            "pdf.parse_failed": "redownload_or_repair_complete_pdf",
            "source.ambiguous": "pass_source_url",
            "url.invalid": "correct_source_url",
        }.get(exc.code, "inspect_input_format_and_pdf_runtime")
        return _result(tool=tool, error=exc, next_action=next_action)
    except Exception:
        return _result(
            tool=tool,
            error=ExplorationError("pdf.failed", "PDF extraction failed; check the input file and parser installation"),
            next_action="inspect_pdf_input_and_runtime",
        )


def register_exploration_commands(app: typer.Typer) -> None:
    @app.command("read-page", help="Read one page over HTTP or with a real JavaScript-enabled browser.")
    def read_page_command(
        url: str = typer.Option(..., "--url", help="The explicit page URL to read."),
        method: str = typer.Option("http", "--method", help="Read mode: http or browser."),
        output_dir: Path = typer.Option(EXPLORATION_OUTPUT_DIR, "--output-dir", help="Directory for exploration evidence."),
        allowed_domain: list[str] = typer.Option([], "--allowed-domain", help="Additional allowed document-navigation host; may be repeated."),
        actions_json: str = typer.Option("", "--actions-json", help="Ordered JSON actions: click, scroll, wait-selector, wait-text, wait."),
        actions_file: Path | None = typer.Option(None, "--actions-file", help="File containing the ordered JSON action array."),
        wait_until: str = typer.Option("domcontentloaded", "--wait-until", help="Browser navigation wait: domcontentloaded, load, or networkidle."),
        timeout_seconds: float = typer.Option(30, "--timeout-seconds", min=1, max=MAX_ACTION_SECONDS),
        browser_channel: str = typer.Option("bundled", "--browser-channel", help="Browser for normal Playwright launch: bundled, chromium, chrome, or msedge."),
        headed: bool = typer.Option(False, "--headed", help="Show the selected browser window for this invocation."),
        max_requests: int = typer.Option(100, "--max-requests", min=1, max=MAX_BROWSER_REQUESTS),
        user_agent: str | None = typer.Option(
            None,
            "--user-agent",
            help=f"HTTPX User-Agent (default {DEFAULT_HTTP_USER_AGENT}); browser mode keeps its normal identity.",
        ),
        max_dom_bytes: int = typer.Option(MAX_PAGE_BYTES, "--max-dom-bytes", min=1024, max=MAX_PAGE_BYTES),
        max_http_bytes: int = typer.Option(MAX_PAGE_BYTES, "--max-http-bytes", min=1024, max=MAX_PAGE_BYTES),
        json_output: bool = typer.Option(False, "--json", help="Emit the stable ToolResult JSON envelope."),
    ) -> None:
        result = read_page(
            url=url,
            method=method,
            output_dir=output_dir,
            allowed_domains=allowed_domain,
            actions_json=actions_json,
            actions_file=actions_file,
            wait_until=wait_until,
            timeout_seconds=timeout_seconds,
            browser_channel=browser_channel,
            headed=headed,
            max_requests=max_requests,
            max_dom_bytes=max_dom_bytes,
            max_http_bytes=max_http_bytes,
            user_agent=user_agent,
        )
        _emit(result, json_output=json_output)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("download-file", help="Stream one complete file to a SHA-256-addressed local artifact.")
    def download_file_command(
        url: str = typer.Option(..., "--url", help="The file URL to download."),
        output_dir: Path = typer.Option(EXPLORATION_OUTPUT_DIR, "--output-dir", help="Directory for downloaded evidence."),
        allowed_domain: list[str] = typer.Option([], "--allowed-domain", help="Allowed redirect host; may be repeated."),
        timeout_seconds: float = typer.Option(60, "--timeout-seconds", min=1, max=MAX_HTTP_TIMEOUT_SECONDS),
        max_bytes: int = typer.Option(MAX_DOWNLOAD_BYTES, "--max-bytes", min=1, max=MAX_DOWNLOAD_BYTES),
        pacing_seconds: float = typer.Option(0.5, "--pacing-seconds", min=0, max=60),
        jitter_seconds: float = typer.Option(0.5, "--jitter-seconds", min=0, max=30),
        user_agent: str | None = typer.Option(
            None,
            "--user-agent",
            help=f"HTTPX User-Agent (default {DEFAULT_HTTP_USER_AGENT}).",
        ),
        json_output: bool = typer.Option(False, "--json", help="Emit the stable ToolResult JSON envelope."),
    ) -> None:
        result = download_file(
            url=url,
            output_dir=output_dir,
            allowed_domains=allowed_domain,
            timeout_seconds=timeout_seconds,
            max_bytes=max_bytes,
            pacing_seconds=pacing_seconds,
            jitter_seconds=jitter_seconds,
            user_agent=user_agent,
        )
        _emit(result, json_output=json_output)
        if not result.ok:
            raise typer.Exit(1)

    @app.command("extract-pdf", help="Extract page-indexed text and metadata from a local PDF.")
    def extract_pdf_command(
        input_path: Path = typer.Option(..., "--input", exists=False, help="Input PDF path."),
        output_dir: Path = typer.Option(EXPLORATION_OUTPUT_DIR, "--output-dir", help="Directory for parsed text evidence."),
        source_url: str | None = typer.Option(None, "--source-url", help="Override or select the source URL recorded by download-file."),
        max_bytes: int = typer.Option(MAX_DOWNLOAD_BYTES, "--max-bytes", min=1, max=MAX_DOWNLOAD_BYTES),
        json_output: bool = typer.Option(False, "--json", help="Emit the stable ToolResult JSON envelope."),
    ) -> None:
        result = extract_pdf(input_path=input_path, output_dir=output_dir, source_url=source_url, max_bytes=max_bytes)
        _emit(result, json_output=json_output)
        if not result.ok:
            raise typer.Exit(1)


__all__ = [
    "DEFAULT_HTTP_USER_AGENT",
    "MAX_DOWNLOAD_BYTES",
    "MAX_PAGE_BYTES",
    "extract_pdf",
    "parser_error_result",
    "read_page",
    "register_exploration_commands",
    "download_file",
]
