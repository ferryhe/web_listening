from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from web_listening.contracts import CaptureRequest
from web_listening.executors import browseract as browseract_executor
from web_listening.executors import browseract_wrapper
from web_listening.executors.browseract import (
    BROWSERACT_VERSION,
    BrowserActExecutor,
    discover_browseract,
    inspect_browseract,
)
from web_listening.executors.browseract_wrapper import execute
from web_listening.executors.browseract_wrapper import run_bounded_browseract_command
from web_listening.executors.subprocess_runner import SubprocessAcquisitionExecutor


def _tool(tmp_path: Path, *, runtime_payload: str | None = None) -> tuple[Path, Path]:
    prefix = tmp_path / "browseract-tool"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "browser-act"
    fixture = (Path(__file__).parent / "fixtures" / "fake_browseract_cli.py").read_text(
        encoding="utf-8"
    )
    executable.write_text(
        fixture.replace("__TOOL_PYTHON__", str(bin_dir / "python")), encoding="utf-8"
    )
    executable.chmod(0o755)
    python = bin_dir / "python"
    runtime_payload = (
        runtime_payload
        or f'{{"python_version":"3.12","sys_prefix":"{prefix}","package_version":"1.0.6"}}'
    )
    python.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "-I" ]; then\n'
        f"  printf '%s\\n' '{runtime_payload}'\n"
        "  exit 0\n"
        "fi\n"
        f'exec {sys.executable} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    return executable, prefix


def _request(
    config: dict | None = None, *, url: str = "https://example.com/read"
) -> CaptureRequest:
    return CaptureRequest(
        request_id="request-1",
        executor_id="browseract",
        url=url,
        requested_at=datetime.now(timezone.utc),
        config=config or {"recipe": "stealth_extract"},
        site_key="example",
        site_skill_id="example.read",
        site_skill_version="1.0.0",
        site_skill_digest="a" * 64,
        recipe_id="read",
        run_id="run-1",
        scope_id="scope-1",
    )


def test_discovery_requires_explicit_absolute_or_controlled_path(tmp_path: Path):
    executable, _ = _tool(tmp_path)
    assert discover_browseract(executable) == executable.resolve()
    assert (
        discover_browseract(search_path=str(executable.parent)) == executable.resolve()
    )
    assert discover_browseract() is None
    with pytest.raises(ValueError, match="absolute"):
        discover_browseract("browser-act")


def test_discovery_ignores_exe_only_search_path(tmp_path: Path):
    executable = tmp_path / "browser-act.exe"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)

    assert discover_browseract(search_path=str(tmp_path)) is None


def test_version_pattern_is_derived_from_pinned_version():
    assert browseract_executor._VERSION_RE.fullmatch(
        f"browser-act {BROWSERACT_VERSION}"
    )
    assert (
        browseract_executor._VERSION_RE.fullmatch(f"browser-act {BROWSERACT_VERSION}0")
        is None
    )


def test_read_shebang_closes_executable_promptly(tmp_path: Path, monkeypatch):
    executable, _ = _tool(tmp_path)
    real_open = Path.open
    closed = False

    class TrackedFile:
        def __init__(self, source):
            self.source = source

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            nonlocal closed
            self.source.close()
            closed = True

        def readline(self, size: int):
            return self.source.readline(size)

    def tracked_open(path: Path, *args, **kwargs):
        return TrackedFile(real_open(path, *args, **kwargs))

    monkeypatch.setattr(Path, "open", tracked_open)

    browseract_executor._read_shebang(executable)

    assert closed is True


def test_inspection_accepts_isolated_fake_runtime(tmp_path: Path):
    executable, prefix = _tool(tmp_path)
    payload = inspect_browseract(executable, project_prefix=tmp_path / "project-venv")
    assert payload["available"] is True
    assert payload["browseract_version"] == "1.0.6"
    assert payload["sys_prefix"] == str(prefix)
    assert payload["capabilities"] == ["interactive_read", "stealth_extract"]


@pytest.mark.parametrize(
    "duplicate_key", ["python_version", "sys_prefix", "package_version"]
)
def test_inspection_rejects_duplicate_runtime_identity_keys(
    tmp_path: Path, duplicate_key: str
):
    canary = "path-canary-must-not-leak"
    prefix = tmp_path / canary / "browseract-tool"
    values = {
        "python_version": '"3.12"',
        "sys_prefix": f'"{prefix}"',
        "package_version": '"1.0.6"',
    }
    fields = [f'"{key}":{value}' for key, value in values.items()]
    fields.append(f'"{duplicate_key}":{values[duplicate_key]}')
    executable, _ = _tool(tmp_path, runtime_payload="{" + ",".join(fields) + "}")

    payload = inspect_browseract(executable, project_prefix=tmp_path / "project-venv")

    assert payload["available"] is False
    assert payload["errors"] == [
        {"code": "invalid_runtime_probe", "message": "tool runtime identity is invalid"}
    ]
    assert payload["python_version"] == ""
    assert payload["sys_prefix"] == ""
    assert payload["resolved_executable"] == ""
    assert payload["python_executable"] == ""
    assert canary not in json.dumps(payload, sort_keys=True)


def test_inspection_is_structured_when_unavailable(tmp_path: Path):
    payload = inspect_browseract(tmp_path / "missing")
    assert payload["schema_version"] == "browseract-inspection.v1"
    assert payload["available"] is False
    assert payload["errors"][0]["code"] == "executable_not_found"


def test_executor_disables_target_execution_before_wrapper_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    executable, _ = _tool(tmp_path)
    monkeypatch.setattr(
        browseract_executor,
        "inspect_browseract",
        lambda *args, **kwargs: {
            "available": True,
            "errors": [],
            "resolved_executable": str(executable),
        },
    )
    executor = BrowserActExecutor(executable, project_prefix=tmp_path / "project-venv")
    monkeypatch.setattr(
        SubprocessAcquisitionExecutor,
        "execute",
        lambda self, request: pytest.fail("target wrapper subprocess was reached"),
    )

    result = executor.execute(_request())

    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_target_execution_disabled"


@pytest.mark.parametrize("recipe", ["stealth_extract", "interactive_read"])
def test_wrapper_disables_target_execution_before_browseract_spawn(
    monkeypatch: pytest.MonkeyPatch, recipe: str
):
    monkeypatch.setattr(
        browseract_wrapper,
        "run_bounded_browseract_command",
        lambda *args, **kwargs: pytest.fail("target BrowserAct subprocess was reached"),
    )

    config = {"recipe": recipe, "timeout_ms": 1_000}
    if recipe == "interactive_read":
        config["browser_id"] = "public-browser"
    result = execute(
        _request(config),
        "/opt/browseract/bin/browser-act",
    )

    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_target_execution_disabled"


def test_bounded_command_reaps_child_when_reader_thread_start_fails(
    tmp_path: Path, monkeypatch
):
    real_start = threading.Thread.start
    real_popen = browseract_wrapper.subprocess.Popen
    processes = []
    calls = 0

    def record_popen(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    def fail_second_start(thread):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic thread start failure")
        return real_start(thread)

    monkeypatch.setattr(threading.Thread, "start", fail_second_start)
    monkeypatch.setattr(browseract_wrapper.subprocess, "Popen", record_popen)
    with pytest.raises(RuntimeError, match="synthetic thread start failure"):
        run_bounded_browseract_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            5,
        )
    assert len(processes) == 1
    assert processes[0].poll() is not None
    assert processes[0].stdout is not None and processes[0].stdout.closed
    assert processes[0].stderr is not None and processes[0].stderr.closed


def test_inspection_kills_pipe_holding_descendant_after_leader_exit(tmp_path: Path):
    executable, _ = _tool(tmp_path)
    interpreter = executable.parent / "python"
    descendant_pid = tmp_path / "descendant.pid"
    runtime_payload = (
        f'{{"python_version":"3.12","sys_prefix":"{executable.parent.parent}",'
        '"package_version":"1.0.6"}}'
    )
    interpreter.write_text(
        f"#!{sys.executable}\n"
        "import os, signal, sys, time\n"
        "if sys.argv[1:2] == ['-I']:\n"
        "    pid = os.fork()\n"
        "    if pid == 0:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "        signal.signal(signal.SIGHUP, signal.SIG_IGN)\n"
        "        time.sleep(30)\n"
        "        os._exit(0)\n"
        f"    open({str(descendant_pid)!r}, 'w').write(str(pid))\n"
        f"    print({runtime_payload!r}, flush=True)\n"
        "    os._exit(0)\n",
        encoding="utf-8",
    )
    interpreter.chmod(0o755)

    started = time.monotonic()
    payload = inspect_browseract(
        executable, project_prefix=tmp_path / "project-venv", timeout_seconds=0.2
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert payload["available"] is False
    assert payload["errors"] == [
        {"code": "handshake_failed", "message": "BrowserAct identity probe failed"}
    ]
    pid = int(descendant_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
