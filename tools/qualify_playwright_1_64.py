"""Isolated Playwright 1.64.1 candidate wiring and artifact verification.

The production reader stays on 1.62.0. A candidate snapshot changes its strict
version check and adapts the existing reader tests; it grants no new authority.
Runtime probes alone never authorize adoption: the full suite and rollback are
separate gates recorded in the reviewed qualification result.
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import hashlib
import importlib.metadata as metadata
import json
import platform
import os
import shutil
import subprocess
import runpy
import zipfile
import tempfile
from pathlib import Path
from threading import Thread

RESULT_SHA256 = "4f9a08cc6b9230e375ac39746320c2b75a9f56c28a0a9da50384b0c160559335"
VERSION = "1.64.1"
WHEEL_NAME = "playwright-1.64.1-py3-none-win_amd64.whl"
WHEEL_SHA256 = "5e240a79464434c9260957b9ef59a5808e02e96e3fcabdc717fd7485260b9730"
REVISION = "1248"
BUILD = "156.0.8078.4"
DEPENDENCIES = {"greenlet": "3.5.6", "pyee": "14.0.0", "typing-extensions": "4.16.0"}
READER = "web_listening/executors/playwright_wrapper.py"
READER_TEST = "tests/test_governed_browser_reader.py"
PATCH = "docs/testing/fixtures/playwright-1.64.1-candidate.patch"
RESULT = "docs/testing/fixtures/playwright-1.64.1-qualification.win32-x86_64.json"
# Populated from the isolated official-wheel browser installation, never from
# user-supplied result metadata.
BINARIES = {
    "headed": (
        "chromium-1248/chrome-win64/chrome.exe",
        "7f08161bbeb2853ffe1780a9ded4985087f52efb9725d7d8207aabdd42f7cfb9",
    ),
    "headless": (
        "chromium_headless_shell-1248/chrome-headless-shell-win64/chrome-headless-shell.exe",
        "f77d8e27c8db1e4ac0399459a00e0c97bb9417adc393474cabaffebe2314ecb0",
    ),
}


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _candidate_files(source: Path) -> dict[str, str]:
    reader = (source / READER).read_text(encoding="utf-8")
    pin = 'PLAYWRIGHT_RUNTIME_VERSION = "1.62.0"'
    if reader.count(pin) != 1:
        raise ValueError("source reader is not the unchanged 1.62.0 baseline")
    tests = (source / READER_TEST).read_text(encoding="utf-8")
    start = tests.index("@pytest.mark.live\ndef test_qualified_playwright_")
    end = tests.index("\n\nclass _TemporaryDirectory:", start)
    real = tests[start:end].replace("pytest.skip(", "pytest.fail(")
    # The extra cases exercise the same real governed entrypoint at every
    # supported wait boundary; synchronous JS is observable at all three.
    real = real.replace(
        "@pytest.mark.live\ndef test_qualified_playwright_renders_governed_offline_fixture()",
        '@pytest.mark.live\n@pytest.mark.parametrize("candidate_wait_until", '
        '["domcontentloaded", "load", "networkidle"])\n'
        "def test_qualified_playwright_renders_governed_offline_fixture(candidate_wait_until)",
    ).replace('{"wait_until": "networkidle"}', '{"wait_until": candidate_wait_until}')
    real = real.replace("setTimeout(()=>{", "(()=>{").replace("},100)", "})()")
    tests = (tests[:start] + real + tests[end:]).replace("1.62.0", VERSION)
    return {
        READER: reader.replace(pin, f'PLAYWRIGHT_RUNTIME_VERSION = "{VERSION}"'),
        READER_TEST: tests,
    }


def candidate_patch(source: Path) -> str:
    return "".join(
        "".join(
            difflib.unified_diff(
                (source / name).read_text(encoding="utf-8").splitlines(keepends=True),
                text.splitlines(keepends=True),
                fromfile="a/" + name,
                tofile="b/" + name,
            )
        )
        for name, text in _candidate_files(source).items()
    )


def create_snapshot(source: Path, destination: Path) -> dict[str, object]:
    """Copy repository inputs into a new owned root, then apply only this patch."""
    if destination.exists() or destination.resolve().is_relative_to(source.resolve()):
        raise ValueError("candidate destination must be a new root outside the source")
    patch = candidate_patch(source)
    if (source / PATCH).read_text(encoding="utf-8") != patch:
        raise ValueError("committed candidate patch differs from generated patch")
    listing = (
        subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=source,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .split("\0")
    )
    files = [name for name in listing if name and (source / name).is_file()]
    destination.mkdir(parents=True)
    before = {}
    for name in files:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / name, target)
        before[name] = sha256(source / name)
    for name, text in _candidate_files(source).items():
        (destination / name).write_text(text, encoding="utf-8", newline="\n")
    return {
        "patch": PATCH,
        "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
        "source_files": before,
        "candidate_files": {name: sha256(destination / name) for name in files},
    }


def verify_snapshot(source: Path, snapshot: Path, evidence: dict) -> list[str]:
    errors = []
    patch = candidate_patch(source)
    if (
        evidence.get("patch") != PATCH
        or evidence.get("patch_sha256") != hashlib.sha256(patch.encode()).hexdigest()
    ):
        errors.append("patch_sha256")
    if (source / PATCH).read_text(encoding="utf-8") != patch:
        errors.append(PATCH)
    expected = _candidate_files(source)
    for section, root in (("source_files", source), ("candidate_files", snapshot)):
        files = evidence.get(section)
        if (
            not isinstance(files, dict)
            or not files
            or READER not in files
            or READER_TEST not in files
        ):
            errors.append(section)
            continue
        for name, digest in files.items():
            relative = Path(name)
            if relative.is_absolute() or ".." in relative.parts:
                errors.append(section)
                continue
            path = root / name
            if not path.is_file() or sha256(path) != digest:
                errors.append(name)
    for name, text in expected.items():
        if (
            not (snapshot / name).is_file()
            or (snapshot / name).read_text(encoding="utf-8") != text
        ):
            errors.append(name)
    return sorted(set(errors))


def verify_archive(distribution, expected_sha256: str) -> str:
    try:
        direct = json.loads(distribution.read_text("direct_url.json") or "null")
        info = direct["archive_info"]
        actual = info["hashes"]["sha256"]
        if (
            actual != expected_sha256
            or info.get("hash", "sha256=" + actual) != "sha256=" + actual
        ):
            raise ValueError("installed archive SHA-256 mismatch")
    except (KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("missing installed PEP 610 archive SHA-256") from exc
    return actual


def verify_browser_manifest(manifest: dict) -> None:
    entries = {entry["name"]: entry for entry in manifest.get("browsers", [])}
    for name in ("chromium", "chromium-headless-shell"):
        entry = entries.get(name, {})
        if entry.get("revision") != REVISION or entry.get("browserVersion") != BUILD:
            raise ValueError("browser revision/build mismatch")


def verify_package(wheel: Path, distribution=None) -> dict:
    distribution = distribution or metadata.distribution("playwright")
    if distribution.version != VERSION:
        raise ValueError("installed package is not playwright==1.64.1")
    if wheel.name != WHEEL_NAME or not wheel.is_file() or sha256(wheel) != WHEEL_SHA256:
        raise ValueError("selected official Windows wheel identity mismatch")
    archive = verify_archive(distribution, WHEEL_SHA256)
    with zipfile.ZipFile(wheel) as package:
        name = "playwright/driver/package/browsers.json"
        manifest = package.read(name)
        installed = Path(distribution.locate_file(name))
        if installed.read_bytes() != manifest:
            raise ValueError("installed browser manifest differs from pinned wheel")
        verify_browser_manifest(json.loads(manifest))
        for name in package.namelist():
            if name.startswith("playwright/") and not name.endswith("/"):
                if Path(distribution.locate_file(name)).read_bytes() != package.read(
                    name
                ):
                    raise ValueError(
                        "installed Playwright bytes differ from pinned wheel"
                    )
    dependencies = {name: metadata.version(name) for name in DEPENDENCIES}
    if dependencies != DEPENDENCIES:
        raise ValueError("resolved Playwright dependency identity mismatch")
    return {
        "version": distribution.version,
        "wheel_filename": wheel.name,
        "wheel_sha256": WHEEL_SHA256,
        "installed_archive_sha256": archive,
        "dependencies": dependencies,
    }


def verify_binary(cache: Path, kind: str, *, launched_path: Path | None = None) -> dict:
    relative, digest = BINARIES[kind]
    executable = cache / relative
    if not executable.resolve().is_relative_to(cache.resolve()):
        raise ValueError("browser executable escaped owned cache")
    if not executable.is_file() or sha256(executable) != digest:
        raise ValueError("missing/corrupt/wrong browser executable")
    if launched_path is not None and launched_path.resolve() != executable.resolve():
        raise ValueError("launched headed/headless identity mismatch")
    return {"path": relative, "sha256": digest}


def validate_result(payload: object) -> None:
    # This round is deliberately DEFER-only. A self-hash is an integrity check,
    # never an independent proof that an unexecuted case passed.
    if not isinstance(payload, dict) or payload.get("conclusion") != "defer":
        raise ValueError("this qualification does not authorize adoption")
    result_path = Path(__file__).resolve().parents[1] / RESULT
    if sha256(result_path) != RESULT_SHA256:
        raise ValueError("reviewed qualification fixture bytes changed")
    expected = json.loads(result_path.read_text(encoding="utf-8"))
    if payload != expected:
        raise ValueError("result differs from the reviewed DEFER evidence")


def websocket_probe() -> None:
    """A child-process probe: a deadlocked route must not hang the whole suite."""
    import faulthandler
    import httpx
    from web_listening.executors.playwright_wrapper import (
        _default_session_factory,
        prepare_browser_acquisition_adapter,
    )
    from web_listening.blocks.governed_read import MockClientReadGateway

    faulthandler.dump_traceback_later(10)
    root = Path(__file__).resolve().parents[1]
    plan_factory = runpy.run_path(str(root / READER_TEST))["_plan"]
    seen = []
    websocket_url = os.environ.get(
        "PLAYWRIGHT_QUALIFICATION_WEBSOCKET_URL", "wss://example.net/blocked-ws"
    )
    if not (
        websocket_url.startswith("ws://127.0.0.1:")
        or websocket_url == "wss://example.net/blocked-ws"
    ):
        raise ValueError(
            "WebSocket probe endpoint must be the task-owned loopback fixture"
        )

    def handler(request):
        seen.append(str(request.url))
        print(canonical_json({"gateway_read": str(request.url)}), flush=True)
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body>Governed notice<script>new WebSocket("
            + json.dumps(websocket_url)
            + ")</script></body></html>",
            request=request,
        )

    gateway = MockClientReadGateway(
        httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="web-listening-bot/qualification-offline",
        max_body_bytes=4096,
    )
    plan = plan_factory(config={"wait_until": "networkidle"})
    adapter = prepare_browser_acquisition_adapter(
        plan, plan.steps[0], gateway, session_factory=_default_session_factory
    )
    result = adapter.capture(
        "https://example.com/start", config={"wait_until": "networkidle"}
    )
    if (
        seen != ["https://example.com/start"]
        or result.content_text != "Governed notice"
    ):
        raise ValueError("WebSocket probe caused a target read or lost content")
    faulthandler.cancel_dump_traceback_later()
    print(
        canonical_json(
            {
                "seen": seen,
                "runtime_version": result.metadata_json["runtime_version"],
                "completed": True,
            }
        )
    )


def classify_probe(*, exit_code: int, timed_out: bool) -> dict:
    """Keep a harness assertion distinct from a failed real browser case."""
    return {
        "browser_case": "failed" if timed_out or exit_code else "passed",
        "code": (
            "external_deadline"
            if timed_out
            else ("process_failure" if exit_code else "completed")
        ),
        "reader_cleanup": "not_completed" if timed_out else "inspect_process_evidence",
        "conclusion": "defer",
        "exit_code": exit_code,
    }


def owned_processes(cache: Path) -> list[dict]:
    """Observe this cache and this installed driver only; never kill processes."""
    import playwright

    roots = [
        str(cache.absolute()),
        str(cache.resolve()),
        str(Path(playwright.__file__).parent / "driver"),
        str(Path(playwright.__file__).parent.resolve() / "driver"),
    ]
    quoted = ",".join("'" + root.replace("'", "''") + "'" for root in roots)
    command = (
        f"$roots=@({quoted}); @(Get-CimInstance Win32_Process | Where-Object {{ "
        "$exe=$_.ExecutablePath; $exe -and @($roots | Where-Object { "
        "$exe.StartsWith($_ + '\\',[StringComparison]::OrdinalIgnoreCase) }).Count "
        "} | Select-Object ProcessId,ExecutablePath) | ConvertTo-Json -Compress"
    )
    output = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    value = json.loads(output or "[]")
    return value if isinstance(value, list) else [value]


async def lifecycle(cache: Path) -> dict:
    """Real local-only binary, timeout, cancellation, failure and cleanup probes."""
    from playwright.async_api import Error, TimeoutError, async_playwright

    if __package__:
        from tools.qualify_playwright_1_62 import (
            _BrowserRuntimeTemp,
            _QualificationServer,
            _close_failure_probe,
            _expected_launch_failure,
            _teardown_resources,
        )
    else:
        from qualify_playwright_1_62 import (
            _BrowserRuntimeTemp,
            _QualificationServer,
            _close_failure_probe,
            _expected_launch_failure,
            _teardown_resources,
        )

    server = _QualificationServer()
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    checks = {}
    launched = {}
    cleanup = {}
    try:
        for kind in BINARIES:
            driver = browser = context = page = None
            closed = []
            with _BrowserRuntimeTemp() as runtime:
                try:
                    driver = await async_playwright().start()
                    identity = verify_binary(cache, kind)
                    executable = cache / identity["path"]
                    browser = await driver.chromium.launch(
                        executable_path=str(executable),
                        headless=kind == "headless",
                        timeout=5000,
                    )
                    observed = owned_processes(cache)
                    if not any(
                        Path(row["ExecutablePath"]).resolve() == executable.resolve()
                        for row in observed
                    ):
                        raise ValueError(
                            "actually launched binary identity was not observed"
                        )
                    verify_binary(cache, kind, launched_path=executable)
                    if browser.version != BUILD:
                        raise ValueError("launched browser build mismatch")
                    launched[kind] = {
                        **identity,
                        "headless": kind == "headless",
                        "processes": observed,
                        "build": browser.version,
                    }
                    context = await browser.new_context()
                    page = await context.new_page()
                    page.on("close", lambda: closed.append("page"))
                    context.on("close", lambda: closed.append("context"))
                    browser.on("disconnected", lambda: closed.append("browser"))
                    response = await page.goto(
                        server.base_url + "/redirect", wait_until="load", timeout=5000
                    )
                    if (
                        response.status != 200
                        or not page.url.endswith("/rendered")
                        or await page.evaluate("document.body.dataset.qualified")
                        != "rendered-1.62"
                    ):
                        raise ValueError("local lifecycle render failed")
                    checks[kind + "_lifecycle"] = "passed"
                    try:
                        await page.goto(server.base_url + "/slow", timeout=1)
                    except TimeoutError:
                        checks[kind + "_navigation_timeout"] = "passed"
                    else:
                        raise ValueError("navigation timeout did not occur")
                    finally:
                        server.release_slow.set()
                    pending = asyncio.create_task(page.wait_for_timeout(5000))
                    await asyncio.sleep(0)
                    pending.cancel()
                    try:
                        await pending
                    except asyncio.CancelledError:
                        checks[kind + "_cancellation"] = "passed"
                    else:
                        raise ValueError("cancellation did not occur")
                    with tempfile.TemporaryDirectory(
                        prefix="pw164-binary-probe-"
                    ) as temporary:
                        root = Path(temporary)
                        corrupt = root / "corrupt.exe"
                        corrupt.write_bytes(b"not a browser")
                        for name, path in (
                            ("missing_binary", root / "missing.exe"),
                            ("corrupt_binary", corrupt),
                            ("launch_failure", root),
                        ):
                            if not await _expected_launch_failure(
                                driver.chromium, executable_path=str(path), timeout=5000
                            ):
                                raise ValueError(name + " unexpectedly launched")
                            checks[kind + "_" + name] = "passed"
                    # Exercise the real driver's bounded startup path. A one-ms
                    # deadline must fail, otherwise this case is not evidence.
                    try:
                        unexpected = await driver.chromium.launch(
                            executable_path=str(executable),
                            headless=kind == "headless",
                            timeout=1,
                        )
                    except (TimeoutError, Error):
                        checks[kind + "_startup_timeout"] = "passed"
                    else:
                        await unexpected.close()
                        raise ValueError("startup timeout did not occur")
                finally:
                    outcome, connected = await _teardown_resources(
                        page=page, context=context, browser=browser, playwright=driver
                    )
                    removed = runtime.remove_after_teardown()
                    if outcome is not None or connected is True or not removed:
                        raise ValueError("lifecycle cleanup failed")
                    cleanup[kind] = {
                        "page_closed": page.is_closed() if page is not None else None,
                        "context_closed": "context" in closed,
                        "browser_connected": connected,
                        "driver_stop_returned": driver is not None and outcome is None,
                        "temporary_root_removed": removed,
                        "close_events": closed,
                    }
                    checks[kind + "_cleanup"] = "passed"
            server.release_slow.clear()
        checks["injected_close_failure_independent_cleanup"] = (
            await _close_failure_probe()
        )
    finally:
        server.release_slow.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    remaining = owned_processes(cache)
    if remaining:
        raise ValueError("owned browser/driver process survived teardown")
    return {
        "checks": checks,
        "launched": launched,
        "cleanup": cleanup,
        "remaining_processes": remaining,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--validate-result", type=Path)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--browser-cache", type=Path)
    parser.add_argument("--probe-output", type=Path)
    parser.add_argument(
        "--websocket-probe", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(argv)
    source = Path(__file__).resolve().parents[1]
    try:
        if args.websocket_probe:
            websocket_probe()
        elif args.validate_result:
            validate_result(
                json.loads(args.validate_result.read_text(encoding="utf-8"))
            )
            print("validated defer")
        elif args.snapshot and args.manifest:
            evidence = create_snapshot(source, args.snapshot)
            args.manifest.write_text(canonical_json(evidence) + "\n", encoding="utf-8")
            print(evidence["patch_sha256"])
        elif args.wheel and args.browser_cache:
            if (platform.system(), platform.machine(), platform.python_version()) != (
                "Windows",
                "AMD64",
                "3.12.14",
            ):
                raise ValueError("qualification requires Windows AMD64/Python 3.12.14")
            package = verify_package(args.wheel)
            binaries = {
                kind: verify_binary(args.browser_cache, kind) for kind in BINARIES
            }
            # Preserve an operator-owned short junction on Windows: resolving it
            # can exceed Chromium's profile/executable path limit.
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(args.browser_cache.absolute())
            evidence = {
                "package": package,
                "binaries": binaries,
                "conclusion": "defer",
                "reason": "identity/lifecycle evidence only; governed-reader, full-suite and rollback gates remain separate",
            }
            if args.probe_output:
                evidence["lifecycle"] = asyncio.run(lifecycle(args.browser_cache))
                args.probe_output.write_text(
                    canonical_json(evidence) + "\n", encoding="utf-8"
                )
            print(canonical_json(evidence))
        else:
            parser.error(
                "use --snapshot/--manifest, --validate-result, or --wheel/--browser-cache"
            )
    except (ValueError, OSError, metadata.PackageNotFoundError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
