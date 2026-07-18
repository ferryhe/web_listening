from __future__ import annotations

import json
import os
import re
import signal
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from web_listening.contracts import CaptureContent, CaptureError, CaptureRequest, CaptureResult
from web_listening.executors.wrapper_protocol import run_stdio_wrapper


_RECIPES: dict[str, frozenset[str]] = {
    "stealth_extract": frozenset({"recipe", "content_type", "timeout_ms", "render_wait_ms"}),
    "interactive_read": frozenset({"recipe", "browser_id", "content_type", "timeout_ms", "read_actions"}),
}
_FORBIDDEN_PARTS = frozenset({
    "argv", "auth", "captcha", "click", "cookie", "credential", "eval", "file", "form", "input",
    "keys", "lifecycle", "login", "navigate", "output", "password", "path", "proxy", "secret",
    "select", "token", "upload", "write",
})
_BROWSER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DIRECTIONS = frozenset({"up", "down", "left", "right"})
_FINAL_URL_KEYS = frozenset({"final_url", "current_url"})

# Match the outer executor protocol ceiling so a nested BrowserAct process can
# never allocate more output than the wrapper itself is allowed to return.
BROWSERACT_STDOUT_LIMIT = 4 * 1024 * 1024
BROWSERACT_STDERR_LIMIT = 64 * 1024
_READ_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class BoundedCommandResult:
    returncode: int
    stdout: str


class BrowserActCommandError(RuntimeError):
    pass


class _CleanupError(RuntimeError):
    pass


def execute(request: CaptureRequest, executable: str) -> CaptureResult:
    started = datetime.now(timezone.utc)
    try:
        recipe, arguments = _validated_arguments(request)
    except ValueError as exc:
        return _failure(request, started, "browseract_request_rejected", str(exc))
    try:
        if recipe == "stealth_extract":
            content, final_url = _stealth_extract(executable, str(request.url), arguments)
        else:
            content, final_url = _interactive_read(executable, str(request.url), arguments)
        return _success(request, started, content, final_url, recipe, arguments["content_type"])
    except _CleanupError:
        return _failure(request, started, "browseract_cleanup_failed", "BrowserAct session cleanup failed", True)
    except (OSError, subprocess.TimeoutExpired):
        return _failure(request, started, "browseract_execution_failed", "BrowserAct command failed", True)
    except RuntimeError as exc:
        return _failure(request, started, "browseract_execution_failed", str(exc), True)
    except (json.JSONDecodeError, TypeError, ValueError):
        return _failure(request, started, "browseract_protocol_error", "BrowserAct returned an invalid result")


def _stealth_extract(executable: str, url: str, arguments: dict[str, Any]) -> tuple[str, str]:
    argv = [executable, "--format", "json", "stealth-extract", url, "--content-type", arguments["content_type"],
            "--timeout", _seconds(arguments["timeout_ms"]), "--render-wait", _seconds(arguments["render_wait_ms"])]
    payload = _command(argv, arguments["timeout_ms"] + arguments["render_wait_ms"] + 2_000)
    return _content_from(payload), _final_url_from(payload)


def _interactive_read(executable: str, url: str, arguments: dict[str, Any]) -> tuple[str, str]:
    session = f"wl-{secrets.token_hex(8)}"
    base = [executable, "--format", "json", "--session", session]
    timeout = arguments["timeout_ms"]
    cleanup_required = False
    try:
        cleanup_required = True
        _command([*base, "browser", "open", arguments["browser_id"], url], timeout)
        _command([*base, "wait", "stable", "--timeout", str(timeout)], timeout + 2_000)
        for action in arguments["read_actions"]:
            if action["action"] == "scroll":
                _command([*base, "scroll", action["direction"], "--amount", str(action["amount"])], timeout)
            else:
                _command([*base, "wait", "stable", "--timeout", str(action["timeout_ms"])], action["timeout_ms"] + 2_000)
        content_payload = _command([*base, "get", arguments["content_type"]], timeout)
        state_payload = _command([*base, "state"], timeout)
        return _content_from(content_payload), _final_url_from(state_payload, allow_state_url=True)
    finally:
        if cleanup_required:
            try:
                _command([*base, "session", "close", session], min(timeout, 5_000))
            except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as exc:
                raise _CleanupError("BrowserAct session cleanup failed") from exc


def run_bounded_browseract_command(argv: Sequence[str], timeout_seconds: float,
                                   env: Mapping[str, str] | None = None) -> BoundedCommandResult:
    """Drain both child streams concurrently while retaining only bounded stdout."""
    process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, env=dict(env or {}), close_fds=True,
                               start_new_session=True)
    assert process.stdout is not None and process.stderr is not None
    stdout = bytearray()
    overflow = threading.Event()

    def drain(stream: Any, target: bytearray | None, limit: int) -> None:
        retained = 0
        try:
            while True:
                chunk = stream.read(_READ_CHUNK_SIZE)
                if not chunk:
                    return
                if retained + len(chunk) > limit:
                    overflow.set()
                    return
                retained += len(chunk)
                if target is not None:
                    target.extend(chunk)
        finally:
            stream.close()

    readers: list[threading.Thread] = []
    started_readers: list[threading.Thread] = []
    try:
        readers = [
            threading.Thread(target=drain, args=(process.stdout, stdout, BROWSERACT_STDOUT_LIMIT), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, None, BROWSERACT_STDERR_LIMIT), daemon=True),
        ]
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        deadline = time.monotonic() + max(timeout_seconds, 1.0)
        while not overflow.is_set():
            leader_terminal = process.poll() is not None
            readers_terminal = all(not reader.is_alive() for reader in readers)
            if leader_terminal and readers_terminal:
                break
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
            overflow.wait(min(0.02, max(0.0, deadline - time.monotonic())))
        if overflow.is_set():
            raise BrowserActCommandError("BrowserAct command output exceeded the safe limit")
        remaining = deadline - time.monotonic()
        for reader in readers:
            reader.join(max(0.0, remaining))
        if overflow.is_set():
            raise BrowserActCommandError("BrowserAct command output exceeded the safe limit")
        if any(reader.is_alive() for reader in readers):
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        try:
            decoded = bytes(stdout).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BrowserActCommandError("BrowserAct command returned invalid UTF-8") from exc
        return BoundedCommandResult(process.returncode, decoded)
    except BaseException:
        _terminate_process_group(process)
        for reader in started_readers:
            reader.join(1.0)
        if all(not reader.is_alive() for reader in started_readers):
            for stream in (process.stdout, process.stderr):
                if not stream.closed:
                    stream.close()
        raise


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    for group_signal, grace in ((signal.SIGTERM, 0.2), (signal.SIGKILL, 0.5)):
        try:
            os.killpg(process.pid, group_signal)
        except ProcessLookupError:
            break
        end = time.monotonic() + grace
        while time.monotonic() < end:
            process.poll()
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.01)
        else:
            continue
        break
    if process.poll() is None:
        process.kill()
    process.wait()


def _command(argv: list[str], timeout_ms: int) -> object:
    completed = run_bounded_browseract_command(argv, max(timeout_ms / 1000, 1), {})
    if completed.returncode:
        raise RuntimeError("BrowserAct command failed")
    payload = json.loads(completed.stdout, object_pairs_hook=_unique_json_object)
    if not isinstance(payload, Mapping):
        raise ValueError
    return payload


def _seconds(milliseconds: int) -> str:
    return str(milliseconds // 1000) if milliseconds % 1000 == 0 else str(milliseconds / 1000)


def _content_from(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError
    candidates: list[object] = [payload.get("content"), payload.get("html"), payload.get("markdown")]
    data = payload.get("data")
    if isinstance(data, str):
        candidates.append(data)
    elif isinstance(data, Mapping):
        candidates.extend((data.get("content"), data.get("html"), data.get("markdown")))
    values = [value for value in candidates if isinstance(value, str)]
    if len(values) != 1:
        raise ValueError
    return values[0]


def _final_url_from(payload: object, *, allow_state_url: bool = False) -> str:
    if not isinstance(payload, Mapping):
        raise ValueError
    containers = [payload]
    data = payload.get("data")
    if isinstance(data, Mapping):
        containers.append(data)
    keys = _FINAL_URL_KEYS | ({"url"} if allow_state_url else set())
    candidates = [container[key] for container in containers for key in keys if key in container]
    if len(candidates) != 1 or not isinstance(candidates[0], str) or not candidates[0]:
        raise ValueError
    return candidates[0]


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _validated_arguments(request: CaptureRequest) -> tuple[str, dict[str, Any]]:
    config = dict(request.config)
    recipe = config.get("recipe")
    if recipe not in _RECIPES:
        raise ValueError("recipe must be stealth_extract or interactive_read")
    unexpected = set(config).difference(_RECIPES[recipe])
    if unexpected:
        raise ValueError("BrowserAct configuration contains unsupported fields")
    _reject_forbidden(config)
    timeout = config.get("timeout_ms", 30_000)
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1_000 <= timeout <= 120_000:
        raise ValueError("timeout_ms must be an integer from 1000 through 120000")
    content_type = config.get("content_type", "html")
    if content_type not in {"html", "markdown"}:
        raise ValueError("content_type must be html or markdown")
    result = dict(config, timeout_ms=timeout, content_type=content_type)
    if recipe == "stealth_extract":
        render_wait = config.get("render_wait_ms", 1_000)
        if not isinstance(render_wait, int) or isinstance(render_wait, bool) or not 0 <= render_wait <= 30_000:
            raise ValueError("render_wait_ms must be an integer from 0 through 30000")
        result["render_wait_ms"] = render_wait
        return recipe, result
    browser_id = config.get("browser_id")
    if (not isinstance(browser_id, str) or not _BROWSER_ID.fullmatch(browser_id)
            or browser_id.casefold().startswith(("sk-", "api_", "bearer", "secret", "token"))):
        raise ValueError("browser_id must be a non-secret identifier of 1 through 128 safe characters")
    actions = config.get("read_actions", [])
    if not isinstance(actions, Sequence) or isinstance(actions, (str, bytes, bytearray)) or len(actions) > 20:
        raise ValueError("read_actions must be a list of at most 20 actions")
    for action in actions:
        if not isinstance(action, Mapping) or action.get("action") not in {"scroll", "wait"}:
            raise ValueError("read_actions permit only scroll and wait")
        if action["action"] == "scroll":
            if set(action) != {"action", "direction", "amount"} or action["direction"] not in _DIRECTIONS:
                raise ValueError("scroll requires only a supported direction and amount")
            if not isinstance(action["amount"], int) or isinstance(action["amount"], bool) or not 1 <= action["amount"] <= 10_000:
                raise ValueError("scroll amount must be an integer from 1 through 10000")
        elif set(action) != {"action", "timeout_ms"} or not isinstance(action["timeout_ms"], int) or isinstance(action["timeout_ms"], bool) or not 1 <= action["timeout_ms"] <= 30_000:
            raise ValueError("wait timeout_ms must be an integer from 1 through 30000")
    result["read_actions"] = list(actions)
    return recipe, result


def _reject_forbidden(value: Any, key: str = "") -> None:
    normalized = key.casefold().replace("-", "_")
    if any(part in normalized.split("_") for part in _FORBIDDEN_PARTS):
        raise ValueError("BrowserAct configuration contains a forbidden field")
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            _reject_forbidden(child, str(child_key))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            _reject_forbidden(child)


def _success(request: CaptureRequest, started: datetime, text: str, final_url: str, recipe: str, content_type: str) -> CaptureResult:
    media_type = "text/html" if content_type == "html" else "text/markdown"
    metadata = {"driver": "browseract", "recipe": recipe}
    return CaptureResult(**_lineage(request), state="succeeded", started_at=started, finished_at=datetime.now(timezone.utc),
                         final_url=final_url, status_code=200,
                         content=CaptureContent(media_type=media_type, text=text, metadata=metadata), metadata=metadata)


def _failure(request: CaptureRequest, started: datetime, code: str, message: str, retryable: bool = False) -> CaptureResult:
    return CaptureResult(**_lineage(request), state="failed", started_at=started, finished_at=datetime.now(timezone.utc), error=CaptureError(code=code, message=message, retryable=retryable))


def _lineage(request: CaptureRequest) -> dict[str, Any]:
    return request.model_dump(include={"site_key", "site_skill_id", "site_skill_version", "site_skill_digest", "recipe_id", "run_id", "scope_id", "request_id", "executor_id"})


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    return run_stdio_wrapper(lambda request: execute(request, sys.argv[1]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["execute", "main"]
