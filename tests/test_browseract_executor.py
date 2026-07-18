from __future__ import annotations

import json
import os
import shutil
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from web_listening.contracts import CaptureRequest
from web_listening.executors import browseract_wrapper
from web_listening.executors.browseract import discover_browseract, inspect_browseract
from web_listening.executors.browseract_wrapper import execute
from web_listening.executors.browseract_wrapper import run_bounded_browseract_command


def _tool(tmp_path: Path, *, runtime_payload: str | None = None) -> tuple[Path, Path]:
    prefix = tmp_path / "browseract-tool"
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True)
    executable = bin_dir / "browser-act"
    fixture = (Path(__file__).parent / "fixtures" / "fake_browseract_cli.py").read_text(encoding="utf-8")
    executable.write_text(fixture.replace("__TOOL_PYTHON__", str(bin_dir / "python")), encoding="utf-8")
    executable.chmod(0o755)
    python = bin_dir / "python"
    runtime_payload = runtime_payload or f'{{"python_version":"3.12","sys_prefix":"{prefix}","package_version":"1.0.6"}}'
    python.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"-I\" ]; then\n"
        f"  printf '%s\\n' '{runtime_payload}'\n"
        "  exit 0\n"
        "fi\n"
        f"exec {sys.executable} \"$@\"\n",
        encoding="utf-8",
    )
    python.chmod(0o755)
    return executable, prefix


def _request(config: dict | None = None, *, url: str = "https://example.com/read") -> CaptureRequest:
    return CaptureRequest(
        request_id="request-1", executor_id="browseract", url=url,
        requested_at=datetime.now(timezone.utc), config=config or {"recipe": "stealth_extract"},
        site_key="example", site_skill_id="example.read", site_skill_version="1.0.0",
        site_skill_digest="a" * 64, recipe_id="read", run_id="run-1", scope_id="scope-1",
    )


def test_discovery_requires_explicit_absolute_or_controlled_path(tmp_path: Path):
    executable, _ = _tool(tmp_path)
    assert discover_browseract(executable) == executable.resolve()
    assert discover_browseract(search_path=str(executable.parent)) == executable.resolve()
    assert discover_browseract() is None
    with pytest.raises(ValueError, match="absolute"):
        discover_browseract("browser-act")


def test_inspection_accepts_isolated_fake_runtime(tmp_path: Path):
    executable, prefix = _tool(tmp_path)
    payload = inspect_browseract(executable, project_prefix=tmp_path / "project-venv")
    assert payload["available"] is True
    assert payload["browseract_version"] == "1.0.6"
    assert payload["sys_prefix"] == str(prefix)
    assert payload["capabilities"] == ["interactive_read", "stealth_extract"]


@pytest.mark.parametrize("duplicate_key", ["python_version", "sys_prefix", "package_version"])
def test_inspection_rejects_duplicate_runtime_identity_keys(tmp_path: Path, duplicate_key: str):
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
    assert payload["errors"] == [{"code": "invalid_runtime_probe", "message": "tool runtime identity is invalid"}]
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


@pytest.mark.parametrize("recipe", ["stealth_extract", "interactive_read"])
def test_wrapper_runs_only_fixed_read_only_recipes(tmp_path: Path, recipe: str):
    executable, _ = _tool(tmp_path)
    config = {"recipe": recipe, "timeout_ms": 1000}
    if recipe == "interactive_read":
        config["browser_id"] = "browser-123"
        config["read_actions"] = [{"action": "scroll", "direction": "down", "amount": 500}, {"action": "wait", "timeout_ms": 10}]
    result = execute(_request(config), str(executable))
    assert result.state == "succeeded"
    assert result.executor_id == "browseract"
    assert result.content is not None
    assert recipe in (result.content.text or "")
    assert result.metadata == {"driver": "browseract", "recipe": recipe}
    expected = "https://redirect.example/final" if recipe == "stealth_extract" else "https://redirect.example/interactive"
    assert str(result.final_url) == expected


def test_wrapper_records_only_real_safe_browseract_argv(tmp_path: Path):
    executable, prefix = _tool(tmp_path)
    record = prefix / "argv.jsonl"
    result = execute(_request({"recipe": "interactive_read", "browser_id": "public-browser", "timeout_ms": 1000,
                               "read_actions": [{"action": "scroll", "direction": "down", "amount": 200}]}), str(executable))
    assert result.state == "succeeded"
    commands = [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]
    assert [command[4:6] for command in commands] == [["browser", "open"], ["wait", "stable"],
                                                       ["scroll", "down"], ["get", "html"], ["state"], ["session", "close"]]
    flattened = " ".join(part for command in commands for part in command)
    for forbidden in ("click", "navigate", "input", "keys", "select", "eval", "upload", "cookies", "auth", "proxy", "captcha"):
        assert forbidden not in flattened


@pytest.mark.parametrize("config", [
    {"recipe": "shell", "argv": ["--version"]},
    {"recipe": "stealth_extract", "output_path": "/tmp/result"},
    {"recipe": "stealth_extract", "proxy": "managed"},
    {"recipe": "stealth_extract", "captcha": "cloud"},
    {"recipe": "interactive_read", "read_actions": [{"action": "submit", "selector": "form"}]},
    {"recipe": "interactive_read", "read_actions": [{"action": "click", "lifecycle": "close"}]},
    {"recipe": "interactive_read", "browser_id": "secret token", "read_actions": []},
    {"recipe": "interactive_read", "browser_id": "safe", "navigate": "https://evil.example"},
])
def test_wrapper_rejects_mutation_and_arbitrary_arguments(tmp_path: Path, config: dict):
    executable, _ = _tool(tmp_path)
    result = execute(_request(config), str(executable))
    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_request_rejected"


def test_wrapper_does_not_echo_secret_field_names_in_rejections(tmp_path: Path):
    executable, _ = _tool(tmp_path)
    secret = "sk-private-value-that-must-not-leak"
    result = execute(_request({"recipe": "stealth_extract", secret: secret}), str(executable))
    assert result.state == "failed"
    assert result.error is not None
    assert secret not in result.error.message


@pytest.mark.parametrize("path", ["duplicate-content", "duplicate-final-url", "ambiguous-url"])
def test_stealth_extract_fails_closed_on_untrustworthy_json_or_url(tmp_path: Path, path: str):
    executable, _ = _tool(tmp_path)
    request = _request(url=f"https://example.com/{path}")
    result = execute(request, str(executable))
    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_protocol_error"


def test_real_106_stealth_shape_does_not_fabricate_redirect_lineage(tmp_path: Path):
    executable, _ = _tool(tmp_path)
    result = execute(_request(url="https://example.com/real-106-url-only"), str(executable))
    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_protocol_error"


@pytest.mark.parametrize("mode", ["oversized-stdout", "oversized-stderr"])
def test_nested_browseract_output_is_bounded_and_sanitized(tmp_path: Path, mode: str):
    executable, _ = _tool(tmp_path)
    baseline_threads = {thread.ident for thread in threading.enumerate()}
    fd_directory = Path("/proc/self/fd")
    baseline_fds = len(list(fd_directory.iterdir())) if fd_directory.is_dir() else None
    for _ in range(3):
        result = execute(_request(url=f"https://example.com/{mode}"), str(executable))
        assert result.state == "failed"
        assert result.error is not None
        assert result.error.code == "browseract_execution_failed"
        assert result.error.message == "BrowserAct command output exceeded the safe limit"
        assert "secret-diagnostic" not in result.model_dump_json()
    assert {thread.ident for thread in threading.enumerate()} == baseline_threads
    if baseline_fds is not None:
        assert len(list(fd_directory.iterdir())) == baseline_fds


def test_bounded_command_reaps_child_when_reader_thread_start_fails(tmp_path: Path, monkeypatch):
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
    payload = inspect_browseract(executable, project_prefix=tmp_path / "project-venv", timeout_seconds=0.2)
    elapsed = time.monotonic() - started

    assert elapsed < 2
    assert payload["available"] is False
    assert payload["errors"] == [{"code": "handshake_failed", "message": "BrowserAct identity probe failed"}]
    pid = int(descendant_pid.read_text(encoding="utf-8"))
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_interactive_read_rejects_duplicate_state_url(tmp_path: Path):
    executable, _ = _tool(tmp_path)
    result = execute(_request({"recipe": "interactive_read", "browser_id": "duplicate-state", "timeout_ms": 1000}), str(executable))
    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_protocol_error"


@pytest.mark.parametrize("browser_id", ["open-timeout", "open-malformed"])
def test_interactive_read_closes_after_failed_open_attempt(tmp_path: Path, browser_id: str):
    executable, prefix = _tool(tmp_path)
    result = execute(_request({"recipe": "interactive_read", "browser_id": browser_id, "timeout_ms": 1000}), str(executable))
    assert result.state == "failed"
    commands = [json.loads(line) for line in (prefix / "argv.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [command[4:6] for command in commands][-1] == ["session", "close"]


def test_cleanup_failure_withholds_success_and_is_sanitized(tmp_path: Path):
    executable, prefix = _tool(tmp_path)
    result = execute(_request({"recipe": "interactive_read", "browser_id": "close-failure", "timeout_ms": 1000}), str(executable))
    assert result.state == "failed"
    assert result.error is not None
    assert result.error.code == "browseract_cleanup_failed"
    assert result.error.message == "BrowserAct session cleanup failed"
    commands = [json.loads(line) for line in (prefix / "argv.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [command[4:6] for command in commands][-1] == ["session", "close"]
