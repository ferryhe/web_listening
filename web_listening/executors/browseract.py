from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from web_listening.executors.subprocess_runner import SubprocessAcquisitionExecutor, SubprocessLimits
from web_listening.executors.browseract_wrapper import BrowserActCommandError, run_bounded_browseract_command


BROWSERACT_VERSION = "1.0.6"
PYTHON_VERSION = "3.12"
REQUIRED_CAPABILITIES = frozenset({"stealth_extract", "interactive_read"})
INSPECTION_CONTRACT = "browseract-inspection.v1"
_VERSION_RE = re.compile(rf"^browser-act {re.escape(BROWSERACT_VERSION)}$")
_RUNTIME_PROBE = (
    "import importlib.metadata,json,sys;"
    "print(json.dumps({'python_version':f'{sys.version_info.major}.{sys.version_info.minor}',"
    "'sys_prefix':sys.prefix,'package_version':importlib.metadata.version('browser-act-cli')}))"
)
_HELP_PROBES = (
    (("--help",), ("--format {json,text}", "stealth-extract <url>", "browser open <id>",
                    "wait stable", "get html", "get markdown", "state", "scroll <direction>", "session close")),
    (("stealth-extract", "--help"), ("--content-type", "--timeout", "--render-wait")),
    (("browser", "--help"), ("open",)),
    (("wait", "--help"), ("stable", "--timeout")),
    (("get", "--help"), ("html", "markdown")),
    (("state", "--help"), ("browser-act state",)),
    (("scroll", "--help"), ("direction", "--amount")),
    (("session", "--help"), ("close",)),
)


class BrowserActExecutor(SubprocessAcquisitionExecutor):
    """Trusted CaptureRequest gateway for one validated BrowserAct installation."""

    def __init__(self, executable: str | Path, *, project_prefix: str | Path | None = None,
                 limits: SubprocessLimits | None = None) -> None:
        inspection = inspect_browseract(executable, project_prefix=project_prefix or sys.prefix)
        if not inspection["available"]:
            codes = ", ".join(item["code"] for item in inspection["errors"])
            raise RuntimeError(f"BrowserAct handshake rejected: {codes}")
        resolved = inspection["resolved_executable"]
        super().__init__("browseract", (sys.executable, "-m", "web_listening.executors.browseract_wrapper", resolved), limits=limits)
        self.inspection = inspection


def discover_browseract(executable: str | Path | None = None, *, search_path: str | None = None) -> Path | None:
    """Resolve BrowserAct without consulting the ambient process PATH."""
    if executable is not None:
        candidate = Path(executable)
        if not candidate.is_absolute():
            raise ValueError("browseract executable must be an absolute path")
        return candidate.resolve(strict=True) if _is_executable(candidate) else None
    if search_path is None:
        return None
    for entry in search_path.split(os.pathsep):
        directory = Path(entry)
        if not entry or not directory.is_absolute():
            continue
        candidate = directory / "browser-act"
        if _is_executable(candidate):
            return candidate.resolve(strict=True)
    return None


def inspect_browseract(executable: str | Path | None = None, *, search_path: str | None = None,
                       project_prefix: str | Path | None = None, timeout_seconds: float = 5.0) -> dict[str, Any]:
    requested = str(executable or "")
    try:
        resolved = discover_browseract(executable, search_path=search_path)
    except (OSError, ValueError):
        return _unavailable(requested, "invalid_executable", "BrowserAct executable path is invalid")
    if resolved is None:
        return _unavailable(requested, "executable_not_found", "BrowserAct executable was not found")
    try:
        interpreter = _read_shebang(resolved)
    except (OSError, ValueError):
        return _unavailable(requested, "invalid_shebang", "BrowserAct executable has an invalid shebang", resolved)
    env = {"PATH": search_path or "", "LANG": "C", "LC_ALL": "C"}
    try:
        version_result = _run((str(resolved), "--version"), timeout_seconds, env)
        runtime_result = _run((str(interpreter), "-I", "-c", _RUNTIME_PROBE), timeout_seconds, env)
    except (OSError, subprocess.TimeoutExpired, BrowserActCommandError):
        return _unavailable(requested, "handshake_failed", "BrowserAct identity probe failed", resolved)
    if version_result.returncode or not _VERSION_RE.fullmatch(version_result.stdout.strip()):
        return _unavailable(requested, "browseract_version_mismatch", f"BrowserAct {BROWSERACT_VERSION} is required", resolved)
    try:
        runtime = json.loads(runtime_result.stdout, object_pairs_hook=_unique_json_object)
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        runtime = None
    errors = _validate_runtime(runtime, resolved, interpreter, project_prefix, runtime_result.returncode)
    if not errors:
        try:
            for argv, required in _HELP_PROBES:
                probe = _run((str(resolved), *argv), timeout_seconds, env)
                if probe.returncode or any(token not in probe.stdout for token in required):
                    errors.append({"code": "missing_capabilities", "message": "required read-only BrowserAct command surface is missing"})
                    break
        except (OSError, subprocess.TimeoutExpired, BrowserActCommandError):
            errors.append({"code": "capability_probe_failed", "message": "BrowserAct help probe failed"})
    available = not errors
    return {
        "schema_version": INSPECTION_CONTRACT, "available": available,
        "requested_executable": "", "resolved_executable": str(resolved) if available else "",
        "browseract_version": BROWSERACT_VERSION if _VERSION_RE.fullmatch(version_result.stdout.strip()) else "",
        "python_version": runtime.get("python_version", "") if isinstance(runtime, dict) else "",
        "python_executable": str(interpreter) if available else "",
        "sys_prefix": runtime.get("sys_prefix", "") if available and isinstance(runtime, dict) else "",
        "capabilities": sorted(REQUIRED_CAPABILITIES) if not errors else [],
        "read_only": available, "errors": errors,
    }


def _read_shebang(executable: Path) -> Path:
    with executable.open("rb") as source:
        first = source.readline(4096)
    if not first.startswith(b"#!"):
        raise ValueError
    value = first[2:].decode("utf-8").strip()
    if not value or any(character.isspace() for character in value):
        raise ValueError
    interpreter = Path(value)
    if not interpreter.is_absolute() or not _is_executable(interpreter):
        raise ValueError
    return interpreter.absolute()


def _validate_runtime(payload: object, executable: Path, interpreter: Path,
                      project_prefix: str | Path | None, returncode: int) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    if returncode or not isinstance(payload, dict):
        return [{"code": "invalid_runtime_probe", "message": "tool runtime identity is invalid"}]
    prefix_value = payload.get("sys_prefix")
    prefix = Path(prefix_value) if isinstance(prefix_value, str) else Path()
    checks = (
        (payload.get("python_version") == PYTHON_VERSION, "python_version_mismatch", "Python 3.12 is required"),
        (payload.get("package_version") == BROWSERACT_VERSION, "package_version_mismatch", f"browser-act-cli {BROWSERACT_VERSION} is required"),
        (isinstance(prefix_value, str) and prefix.is_absolute(), "invalid_sys_prefix", "tool sys.prefix must be absolute"),
    )
    for condition, code, message in checks:
        if not condition:
            errors.append({"code": code, "message": message})
    if prefix.is_absolute():
        if not _lexically_within(interpreter, prefix):
            errors.append({"code": "interpreter_outside_prefix", "message": "shebang interpreter must be under tool sys.prefix"})
        if not _lexically_within(executable, prefix):
            errors.append({"code": "executable_outside_prefix", "message": "BrowserAct executable must be under tool sys.prefix"})
        if project_prefix and _same_lexical_path(prefix, Path(project_prefix)):
            errors.append({"code": "project_environment_reused", "message": "BrowserAct must use an isolated environment"})
    return errors


def _run(argv: tuple[str, ...], timeout: float, env: dict[str, str]):
    return run_bounded_browseract_command(argv, timeout, env)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _unavailable(requested: str, code: str, message: str, resolved: Path | None = None) -> dict[str, Any]:
    return {"schema_version": INSPECTION_CONTRACT, "available": False, "requested_executable": "",
            "resolved_executable": "", "browseract_version": "", "python_version": "",
            "python_executable": "", "sys_prefix": "", "capabilities": [], "read_only": False,
            "errors": [{"code": code, "message": message}]}


def _is_executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _same_lexical_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.abspath(left)) == os.path.normcase(os.path.abspath(right))


def _lexically_within(child: Path, parent: Path) -> bool:
    try:
        Path(os.path.abspath(child)).relative_to(Path(os.path.abspath(parent)))
        return True
    except ValueError:
        return False


__all__ = ["BROWSERACT_VERSION", "BrowserActExecutor", "INSPECTION_CONTRACT", "PYTHON_VERSION", "REQUIRED_CAPABILITIES", "discover_browseract", "inspect_browseract"]
