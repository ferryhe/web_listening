"""Run the offline Playwright 1.62.0 Chromium qualification in an isolated cache.

This is qualification evidence only.  It does not call a public URL and it does
not enable the production browser-rendered reader.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import shutil
import tempfile
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
from pathlib import Path
from threading import Event, Thread
from typing import Any, Self

QUALIFICATION_VERSION = "playwright-1.62.0-qualification.v1"
CANONICAL_CONCLUSION_VALUES = frozenset({"adopt", "defer", "reject"})
EXPECTED_BROWSER = {
    "build": "151.0.7922.34",
    "chrome_for_testing": {
        "executable_cache_path": "chromium-1234/chrome-win64/chrome.exe",
        "executable_sha256": "409805a16d6416087e6b2f778df1cf8f7bbb267d6b99f6b5bb0a618eace234f2",
    },
    "engine": "chromium",
    "playwright_revision": "1234",
    "headless_shell": {
        "executable_cache_path": "chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe",
        "executable_sha256": "ce4635cd0e5dc0e21494542a701f347e91c1f1d821970578d97ed8df4ced50ef",
    },
}
EXPECTED_PACKAGE = {
    "resolved_dependencies": [
        {"name": "greenlet", "version": "3.5.5"},
        {"name": "pyee", "version": "13.0.1"},
        {"name": "typing-extensions", "version": "4.16.0"},
    ],
    "version": "1.62.0",
    "wheel_filename": "playwright-1.62.0-py3-none-win_amd64.whl",
    "wheel_sha256": "92c0d98ed04eb35af557b709875edba415b1f548bdb22ddb5bb3e1e6c835c2f1",
}
EXPECTED_PLATFORM = {
    "architecture": "AMD64",
    "os": "Windows",
    "python_version": "3.12.14",
}
EXPECTED_CONCLUSION_REASON = "qualified on Windows AMD64 with Python 3.12.14; reader adoption remains separately authorized as #69"


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _failure(code: str) -> dict[str, object]:
    return {"code": code, "ok": False}


def _success(**details: object) -> dict[str, object]:
    return {"code": "passed", "ok": True, **details}


class _BrowserRuntimeTemp:
    """Keep browser and driver temporary files inside one qualification root."""

    _environment_keys = ("TEMP", "TMP", "TMPDIR")

    def __init__(self) -> None:
        self.root: Path | None = None
        self._previous: dict[str, str | None] = {}
        self._was_set: dict[str, bool] = {}

    def __enter__(self) -> Self:
        self.root = Path(tempfile.mkdtemp(prefix="web-listening-pw162-runtime-"))
        self._was_set = {key: key in os.environ for key in self._environment_keys}
        self._previous = {key: os.environ.get(key) for key in self._environment_keys}
        for key in self._environment_keys:
            os.environ[key] = str(self.root)
        return self

    def remove_after_teardown(self) -> bool:
        if self.root is None:
            raise RuntimeError("browser runtime temporary root was not created")
        shutil.rmtree(self.root)
        return not self.root.exists()

    def __exit__(self, *_: object) -> None:
        try:
            if self.root is not None and self.root.exists():
                shutil.rmtree(self.root)
        finally:
            for key in self._environment_keys:
                if self._was_set[key]:
                    os.environ[key] = self._previous[key] or ""
                else:
                    os.environ.pop(key, None)


def _teardown_error_types() -> tuple[type[Exception], ...]:
    try:
        from playwright.async_api import Error as PlaywrightError
    except ImportError:
        return (OSError, RuntimeError)
    return (OSError, RuntimeError, PlaywrightError)


async def _teardown_resources(
    *,
    page: Any | None,
    context: Any | None,
    browser: Any | None,
    playwright: Any | None,
) -> tuple[dict[str, object] | None, bool | None]:
    """Close every real Playwright resource even when an earlier close fails."""
    close_failed = False
    browser_connected_after_close: bool | None = None
    for resource, method_name in (
        (page, "close"),
        (context, "close"),
        (browser, "close"),
        (playwright, "stop"),
    ):
        if resource is None:
            continue
        try:
            await getattr(resource, method_name)()
        except _teardown_error_types():
            close_failed = True
        if resource is browser:
            try:
                browser_connected_after_close = browser.is_connected()
            except _teardown_error_types():
                close_failed = True
    return (
        _failure("close_failure") if close_failed else None,
        browser_connected_after_close,
    )


class _QualificationServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        self.slow_started = Event()
        self.release_slow = Event()
        super().__init__(("127.0.0.1", 0), _QualificationHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_port}"


class _QualificationHandler(BaseHTTPRequestHandler):
    server: _QualificationServer

    def do_GET(self) -> None:
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/rendered")
            self.end_headers()
            return
        if self.path == "/rendered":
            body = (
                b"<!doctype html><html><body><main id='content'>local fixture</main>"
                b"<script>document.body.dataset.qualified='rendered-1.62';</script></body></html>"
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/slow":
            self.server.slow_started.set()
            self.server.release_slow.wait(5)
            body = b"slow fixture"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            with suppress(
                BrokenPipeError, ConnectionResetError, ConnectionAbortedError
            ):
                self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        del format, args


async def _expected_launch_failure(chromium: Any, **kwargs: object) -> bool:
    from playwright.async_api import Error as PlaywrightError

    try:
        browser = await chromium.launch(**kwargs)
    except (OSError, PlaywrightError):
        return True
    await browser.close()
    return False


async def _verify_lifecycle(
    browser_cache: Path,
) -> tuple[
    Path,
    Path,
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object],
    dict[str, object] | None,
]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover - exercised by the command preflight
        raise RuntimeError(
            "Playwright 1.62.0 is not installed in this interpreter"
        ) from exc

    server = _QualificationServer()
    server_thread = Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    browser = None
    context = None
    page = None
    playwright = None
    temporary_probe_removed = False
    browser_runtime_temp_state_removed = False
    browser_connected_after_close: bool | None = None
    teardown_outcome: dict[str, object] | None = None
    try:
        with _BrowserRuntimeTemp() as runtime_temp:
            try:
                playwright = await async_playwright().start()
                chrome_for_testing = Path(playwright.chromium.executable_path)
                if not chrome_for_testing.is_file():
                    raise RuntimeError(
                        "isolated Chrome for Testing executable is missing"
                    )
                chrome_for_testing, headless_shell = _browser_artifact_paths(
                    browser_cache=browser_cache, chrome_for_testing=chrome_for_testing
                )
                browser = await playwright.chromium.launch(
                    headless=True, executable_path=str(headless_shell)
                )
                context = await browser.new_context()
                page = await context.new_page()
                response = await page.goto(
                    f"{server.base_url}/redirect", wait_until="load", timeout=5_000
                )
                rendered = await page.evaluate("document.body.dataset.qualified")
                if (
                    response is None
                    or response.status != 200
                    or not page.url.endswith("/rendered")
                    or rendered != "rendered-1.62"
                ):
                    raise RuntimeError(
                        "local redirect/rendered-page lifecycle did not produce the expected result"
                    )
                lifecycle = _success(
                    javascript_rendered=True,
                    redirected=True,
                    status_code=200,
                )

                try:
                    await page.goto(f"{server.base_url}/slow", timeout=100)
                except PlaywrightTimeoutError:
                    navigation_timeout = _failure("navigation_timeout")
                    server.release_slow.set()
                else:
                    raise RuntimeError("local slow navigation did not time out")

                cancellation = asyncio.create_task(page.wait_for_timeout(5_000))
                await asyncio.sleep(0)
                cancellation.cancel()
                try:
                    await cancellation
                except asyncio.CancelledError:
                    cancellation_result = _failure("cancellation")
                else:
                    raise RuntimeError(
                        "local browser operation cancellation did not raise CancelledError"
                    )

                with tempfile.TemporaryDirectory(
                    prefix="web-listening-pw162-"
                ) as temporary:
                    corrupt = Path(temporary) / "corrupt-browser.exe"
                    corrupt.write_bytes(b"not a Chromium executable")
                    missing_binary = await _expected_launch_failure(
                        playwright.chromium,
                        executable_path=str(Path(temporary) / "missing-browser.exe"),
                    )
                    corrupt_binary = await _expected_launch_failure(
                        playwright.chromium,
                        executable_path=str(corrupt),
                    )
                    launch_failure = await _expected_launch_failure(
                        playwright.chromium,
                        executable_path=str(headless_shell.parent),
                    )
                temporary_probe_removed = not Path(temporary).exists()
                if not all((missing_binary, corrupt_binary, launch_failure)):
                    raise RuntimeError(
                        "expected local launch-failure probe unexpectedly launched"
                    )
            finally:
                server.release_slow.set()
                (
                    teardown_outcome,
                    browser_connected_after_close,
                ) = await _teardown_resources(
                    page=page,
                    context=context,
                    browser=browser,
                    playwright=playwright,
                )
                browser_runtime_temp_state_removed = (
                    runtime_temp.remove_after_teardown()
                )
    finally:
        server.release_slow.set()
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)

    cleanup = {
        "browser_connected_after_close": browser_connected_after_close,
        "browser_process_ids": _browser_process_ids(browser_cache),
        "browser_runtime_temp_state_removed": browser_runtime_temp_state_removed,
        "temporary_probe_removed": temporary_probe_removed,
    }
    if cleanup["browser_process_ids"]:
        raise RuntimeError("isolated Chromium process remained after close")
    if not cleanup["browser_runtime_temp_state_removed"]:
        raise RuntimeError(
            "isolated browser runtime temporary state remained after close"
        )
    return (
        chrome_for_testing,
        headless_shell,
        lifecycle,
        navigation_timeout,
        cancellation_result,
        cleanup,
        teardown_outcome,
    )


def _browser_process_ids(browser_cache: Path) -> list[int]:
    """Return remaining Chromium processes from this exact isolated cache on Windows."""
    if os.name != "nt":
        return []
    import subprocess

    escaped = str(browser_cache).replace("'", "''")
    command = (
        "$path='" + escaped + "';"
        "@(Get-CimInstance Win32_Process |"
        " Where-Object { $_.ExecutablePath -like ($path + '*') -and $_.Name -like 'chrome*' } |"
        " ForEach-Object { $_.ProcessId }) -join ','"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return [int(value) for value in completed.stdout.strip().split(",") if value]


async def _close_failure_probe() -> dict[str, object]:
    events: list[str] = []

    class Resource:
        def __init__(self, name: str, *, fails: bool = False) -> None:
            self.name = name
            self.fails = fails

        async def close(self) -> None:
            events.append(self.name)
            if self.fails:
                raise RuntimeError("intentional qualification close failure")

    class Browser(Resource):
        def is_connected(self) -> bool:
            return False

    class Driver:
        async def stop(self) -> None:
            events.append("playwright")

    outcome, browser_connected = await _teardown_resources(
        page=Resource("page", fails=True),
        context=Resource("context"),
        browser=Browser("browser"),
        playwright=Driver(),
    )
    if (
        outcome != _failure("close_failure")
        or browser_connected is not False
        or events != ["page", "context", "browser", "playwright"]
    ):
        raise RuntimeError("close failure probe did not finish shared teardown")
    return outcome


def _cache_relative_path(executable: Path, browser_cache: Path) -> str:
    try:
        return executable.resolve().relative_to(browser_cache.resolve()).as_posix()
    except ValueError as exc:
        raise RuntimeError(
            "Playwright executable is outside the isolated browser cache"
        ) from exc


def _configure_isolated_browser_cache(browser_cache: Path) -> None:
    """Bind Playwright's driver to the cache selected by this qualification."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_cache.resolve())


def _browser_artifact_paths(
    *, browser_cache: Path, chrome_for_testing: Path
) -> tuple[Path, Path]:
    """Resolve the two Chromium artifacts declared by Playwright 1.62.0 itself."""
    from importlib.resources import files

    manifest = json.loads(
        files("playwright")
        .joinpath("driver", "package", "browsers.json")
        .read_text(encoding="utf-8")
    )
    entries = {entry["name"]: entry for entry in manifest["browsers"]}
    chromium = entries.get("chromium")
    headless = entries.get("chromium-headless-shell")
    expected_revision = EXPECTED_BROWSER["playwright_revision"]
    expected_build = EXPECTED_BROWSER["build"]
    if (
        chromium is None
        or headless is None
        or chromium.get("revision") != expected_revision
        or headless.get("revision") != expected_revision
        or chromium.get("browserVersion") != expected_build
        or headless.get("browserVersion") != expected_build
    ):
        raise RuntimeError(
            "Playwright browser manifest does not match the qualified pair"
        )
    expected_chrome = (
        browser_cache / EXPECTED_BROWSER["chrome_for_testing"]["executable_cache_path"]
    )
    expected_headless = (
        browser_cache / EXPECTED_BROWSER["headless_shell"]["executable_cache_path"]
    )
    if chrome_for_testing.resolve() != expected_chrome.resolve():
        raise RuntimeError(
            "Playwright Chrome for Testing executable is not the qualified cache artifact"
        )
    if not expected_headless.is_file():
        raise RuntimeError("Playwright Headless Shell executable is missing")
    return expected_chrome, expected_headless


def _result_without_digest(
    *,
    wheel: Path,
    browser_cache: Path,
    chrome_for_testing: Path,
    headless_shell: Path,
    checks: dict[str, object],
) -> dict[str, object]:
    return {
        "browser": {
            "build": EXPECTED_BROWSER["build"],
            "chrome_for_testing": {
                "executable_cache_path": _cache_relative_path(
                    chrome_for_testing, browser_cache
                ),
                "executable_sha256": _sha256(chrome_for_testing),
            },
            "engine": EXPECTED_BROWSER["engine"],
            "headless_shell": {
                "executable_cache_path": _cache_relative_path(
                    headless_shell, browser_cache
                ),
                "executable_sha256": _sha256(headless_shell),
            },
            "playwright_revision": EXPECTED_BROWSER["playwright_revision"],
        },
        "checks": checks,
        "conclusion": "adopt",
        "conclusion_reason": EXPECTED_CONCLUSION_REASON,
        "package": {
            "resolved_dependencies": [
                {"name": name, "version": version(name)}
                for name in ("greenlet", "pyee", "typing-extensions")
            ],
            "version": version("playwright"),
            "wheel_filename": wheel.name,
            "wheel_sha256": _sha256(wheel),
        },
        "platform": {
            "architecture": platform.machine(),
            "os": platform.system(),
            "python_version": platform.python_version(),
        },
        "qualification_version": QUALIFICATION_VERSION,
    }


def _expected_result_body() -> dict[str, object]:
    return {
        "browser": EXPECTED_BROWSER,
        "checks": {
            "cancellation": _failure("cancellation"),
            "close_failure": _failure("close_failure"),
            "corrupt_binary": _failure("corrupt_binary"),
            "launch_failure": _failure("launch_failure"),
            "lifecycle": _success(
                javascript_rendered=True,
                redirected=True,
                status_code=200,
            ),
            "missing_binary": _failure("missing_binary"),
            "navigation_timeout": _failure("navigation_timeout"),
        },
        "cleanup": {
            "browser_connected_after_close": False,
            "browser_process_ids": [],
            "browser_runtime_temp_state_removed": True,
            "temporary_probe_removed": True,
        },
        "conclusion": "adopt",
        "conclusion_reason": EXPECTED_CONCLUSION_REASON,
        "package": EXPECTED_PACKAGE,
        "platform": EXPECTED_PLATFORM,
        "qualification_version": QUALIFICATION_VERSION,
    }


def _path(parent: str, key: object) -> str:
    return f"{parent}.{key}" if parent else str(key)


def _closed_schema_errors(
    value: object, expected: object, parent: str = ""
) -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(value, dict):
            return [parent or "result"]
        errors: list[str] = []
        expected_keys = set(expected)
        actual_keys = set(value)
        for key in sorted(expected_keys - actual_keys, key=str):
            errors.append(_path(parent, key))
        for key in sorted(actual_keys - expected_keys, key=str):
            errors.append(_path(parent, key))
        for key in sorted(expected_keys & actual_keys, key=str):
            errors.extend(
                _closed_schema_errors(value[key], expected[key], _path(parent, key))
            )
        return errors
    if isinstance(expected, list):
        if not isinstance(value, list):
            return [parent]
        if len(value) != len(expected):
            return [parent]
        errors: list[str] = []
        for index, (actual_item, expected_item) in enumerate(zip(value, expected)):
            errors.extend(
                _closed_schema_errors(actual_item, expected_item, _path(parent, index))
            )
        return errors
    if type(value) is not type(expected) or value != expected:
        return [parent]
    return []


def validate_qualification_result(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return ["result"]
    expected_body = _expected_result_body()
    expected_keys = set(expected_body) | {"fixture_sha256"}
    actual_keys = set(payload)
    errors = [str(key) for key in sorted(expected_keys ^ actual_keys, key=str)]
    for key in sorted(expected_body.keys() & actual_keys):
        errors.extend(_closed_schema_errors(payload[key], expected_body[key], key))
    declared = payload.get("fixture_sha256")
    if "fixture_sha256" in payload and not isinstance(declared, str):
        errors.append("fixture_sha256")
    if errors:
        return sorted(set(errors))
    body = {key: value for key, value in payload.items() if key != "fixture_sha256"}
    try:
        digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    except (TypeError, ValueError):
        return ["fixture_sha256"]
    if declared != digest:
        errors.append("fixture_sha256")
    return errors


def qualify(*, wheel: Path, browser_cache: Path) -> dict[str, object]:
    if version("playwright") != "1.62.0":
        raise RuntimeError("qualification interpreter must resolve playwright==1.62.0")
    if not wheel.is_file():
        raise RuntimeError("wheel path does not exist")
    if not browser_cache.is_dir():
        raise RuntimeError("isolated browser cache path does not exist")
    _configure_isolated_browser_cache(browser_cache)

    async def run() -> tuple[
        Path,
        Path,
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        (
            chrome_for_testing,
            headless_shell,
            lifecycle,
            timeout,
            cancellation,
            cleanup,
            lifecycle_close_failure,
        ) = await _verify_lifecycle(browser_cache)
        close_failure = lifecycle_close_failure or await _close_failure_probe()
        return (
            chrome_for_testing,
            headless_shell,
            lifecycle,
            timeout,
            cancellation,
            cleanup,
            close_failure,
        )

    (
        chrome_for_testing,
        headless_shell,
        lifecycle,
        timeout,
        cancellation,
        cleanup,
        close_failure,
    ) = asyncio.run(run())
    checks = {
        "cancellation": cancellation,
        "close_failure": close_failure,
        "corrupt_binary": _failure("corrupt_binary"),
        "launch_failure": _failure("launch_failure"),
        "lifecycle": lifecycle,
        "missing_binary": _failure("missing_binary"),
        "navigation_timeout": timeout,
    }
    result = _result_without_digest(
        wheel=wheel,
        browser_cache=browser_cache,
        chrome_for_testing=chrome_for_testing,
        headless_shell=headless_shell,
        checks=checks,
    )
    result["cleanup"] = cleanup
    result["fixture_sha256"] = hashlib.sha256(
        canonical_json(result).encode("utf-8")
    ).hexdigest()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--browser-cache", type=Path)
    parser.add_argument("--write", type=Path)
    parser.add_argument("--validate-result", type=Path)
    args = parser.parse_args(argv)
    if args.validate_result is not None:
        try:
            payload = json.loads(args.validate_result.read_text(encoding="utf-8"))
        except OSError:
            parser.error("unable to read qualification result")
        except (json.JSONDecodeError, UnicodeDecodeError):
            parser.error("qualification result is not valid JSON")
        errors = validate_qualification_result(payload)
        if errors:
            parser.error("invalid qualification result: " + ", ".join(errors))
        print(canonical_json(payload))
        return 0
    if args.wheel is None or args.browser_cache is None:
        parser.error("--wheel and --browser-cache are required to run qualification")
    try:
        result = qualify(wheel=args.wheel, browser_cache=args.browser_cache)
    except RuntimeError as exc:
        parser.error(f"qualification failed: {exc}")
    errors = validate_qualification_result(result)
    if errors:
        parser.error("generated qualification result is invalid: " + ", ".join(errors))
    rendered = canonical_json(result) + "\n"
    if args.write is not None:
        args.write.write_text(rendered, encoding="utf-8", newline="\n")
    print(rendered, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
