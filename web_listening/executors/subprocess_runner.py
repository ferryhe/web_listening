from __future__ import annotations

import os
import json
import math
import numbers
import re
import signal
import subprocess
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import BinaryIO
from typing import get_args
from collections.abc import Mapping, Sequence

from pydantic import ValidationError

from web_listening.contracts import CaptureError, CaptureRequest, CaptureResult
from web_listening.contracts._protocol import ExecutorId


_SAFE_INHERITED_ENVIRONMENT = frozenset({"LANG", "LC_ALL", "LC_CTYPE", "TZ"})
_GOVERNED_PATH_AUTHORITIES = {
    "artifact", "blob", "data", "database", "db", "downloads", "manifest",
    "output", "report", "storage",
}
_LOCATION_SUFFIXES = {"path", "dir", "directory", "file", "root", "location", "url", "uri"}
_WORKING_DIRECTORY_ALIASES = {"cwd", "workdir", "workingdir", "workingdirectory"}
_URL_TOKEN = re.compile(
    r"(?i)\b(?:https?:[\\/]{0,2}|[a-z][a-z0-9+.-]*:(?://|\\\\))[^\s<>\"']*"
)
_SAFE_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CREDENTIAL_KEY = re.compile(
    r"(?ix)([\"']?(?:"
    r"authorization|cookie|password|token|credentials?|"
    r"api[\s_-]*key|client[\s_-]*secret|private[\s_-]*key|"
    r"(?:aws[\s_-]*)?access[\s_-]*key[\s_-]*id|"
    r"(?:aws[\s_-]*)?secret[\s_-]*access[\s_-]*key"
    r")[\"']?\s*[:=]\s*)"
    r"(?:\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^\s,;]+)"
)
_STRUCTURED_CREDENTIAL_VALUE = re.compile(
    r"(?ix)[\"']?(?:"
    r"authorization|cookie|password|token|credentials?|"
    r"api[\s_-]*key|client[\s_-]*secret|private[\s_-]*key|"
    r"(?:aws[\s_-]*)?access[\s_-]*key[\s_-]*id|"
    r"(?:aws[\s_-]*)?secret[\s_-]*access[\s_-]*key"
    r")[\"']?\s*[:=]\s*(?!\[REDACTED\])[\{\[]"
)
_AUTHORIZATION_VALUE = re.compile(
    r"(?i)((?:authorization\s*[:=]\s*)?)(?:bearer|basic)\s+[^\s,;]+"
)
_COOKIE_HEADER_VALUE = re.compile(
    r"(?im)^([ \t]*(?:cookie|set-cookie)[ \t]*:[ \t]*)[^\r\n]*"
)
_PEM_PRIVATE_KEY = re.compile(
    r"-----BEGIN (?P<label>[^-\r\n]*PRIVATE KEY)-----.*?"
    r"(?:-----END (?P=label)-----|\Z)",
    re.DOTALL,
)
_TRUSTED_EXECUTOR_IDS = frozenset(get_args(ExecutorId))
_DIAGNOSTIC_EXCEEDED = "[diagnostic exceeded configured byte limit]"
_INVALID_STRUCTURED_DIAGNOSTIC = "[invalid structured diagnostic redacted]"
_STDERR_EXCEEDED = "[stderr exceeded configured byte limit]"
_JSON_STRING_ESCAPE = re.compile(r'\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4})')
_MAX_STRUCTURED_DIAGNOSTIC_DEPTH = 128
_STRUCTURED_CREDENTIAL_PHRASES = (
    ("authorization",), ("cookie",), ("password",), ("token",),
    ("credential",), ("credentials",), ("api", "key"), ("client", "secret"),
    ("private", "key"), ("access", "key", "id"),
    ("secret", "access", "key"),
)
_STRUCTURED_CREDENTIAL_COMPACT_PHRASES = frozenset({
    "authorization", "cookie", "password", "token", "credential", "credentials",
    "apikey", "clientsecret", "privatekey", "accesskeyid", "secretaccesskey",
    "awsaccesskeyid", "awssecretaccesskey",
})
_STRUCTURED_CREDENTIAL_COMPACT_EXCLUSIONS = frozenset({"noncredential", "noncredentials"})


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class SubprocessLimits:
    timeout_seconds: float = 30.0
    stdout_bytes: int = 4 * 1024 * 1024
    stderr_bytes: int = 64 * 1024
    terminate_grace_seconds: float = 1.0
    kill_grace_seconds: float = 1.0

    def __post_init__(self) -> None:
        time_limits = (
            self.timeout_seconds,
            self.terminate_grace_seconds,
            self.kill_grace_seconds,
        )
        if any(
            isinstance(value, bool)
            or not isinstance(value, numbers.Real)
            or not math.isfinite(value)
            or value <= 0
            for value in time_limits
        ):
            raise ValueError("time limits must be positive")
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.stdout_bytes, self.stderr_bytes)
        ):
            raise ValueError("output limits must be positive")


class _BoundedReader(threading.Thread):
    def __init__(self, pipe: BinaryIO, limit: int) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.limit = limit
        self.data = bytearray()
        self.exceeded = threading.Event()

    def run(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(min(65536, self.limit + 1 - len(self.data)))
                if not chunk:
                    return
                self.data.extend(chunk)
                if len(self.data) > self.limit:
                    self.exceeded.set()
                    return
        finally:
            self.pipe.close()


class _StdinWriter(threading.Thread):
    def __init__(self, pipe: BinaryIO, data: bytes) -> None:
        super().__init__(daemon=True)
        self.pipe = pipe
        self.data = data

    def run(self) -> None:
        try:
            self.pipe.write(self.data)
            self.pipe.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            try:
                self.pipe.close()
            except OSError:
                pass


class SubprocessAcquisitionExecutor:
    """Run one trusted command using the one-request/one-result JSON protocol.

    This controls protocol and process exposure; it is not an OS sandbox.
    """

    def __init__(
        self,
        executor_id: ExecutorId,
        command: tuple[str, ...],
        *,
        limits: SubprocessLimits | None = None,
        allowed_environment: tuple[str, ...] = (),
    ) -> None:
        if executor_id not in _TRUSTED_EXECUTOR_IDS:
            raise ValueError(f"untrusted executor identity {executor_id!r}")
        if not command or any(not isinstance(item, str) or not item for item in command):
            raise ValueError("trusted command must contain non-empty strings")
        unsafe_names = [name for name in allowed_environment if name not in _SAFE_INHERITED_ENVIRONMENT]
        if unsafe_names:
            raise ValueError(f"unsafe environment variable names are forbidden: {', '.join(unsafe_names)}")
        self.executor_id = executor_id
        self._command = command
        self._limits = limits or SubprocessLimits()
        self._allowed_environment = tuple(allowed_environment)

    @property
    def command(self) -> tuple[str, ...]:
        return self._command

    def execute(self, request: CaptureRequest) -> CaptureResult:
        started = datetime.now(timezone.utc)
        if request.executor_id != self.executor_id:
            return _failure(
                request, started, "executor_identity_mismatch",
                "capture request executor identity does not match trusted executor",
            )
        if _contains_write_path_key(request.config) or _contains_write_path_key(request.metadata):
            return _failure(
                request, started, "executor_request_rejected",
                "capture request must not convey governed storage or output paths",
            )
        wire = request.model_dump_json().encode("utf-8")
        env = {name: os.environ[name] for name in self._allowed_environment if name in os.environ}
        workspace: tempfile.TemporaryDirectory[str] | None = None
        try:
            workspace = tempfile.TemporaryDirectory(prefix="web-listening-executor-")
            os.chmod(workspace.name, 0o700)
        except OSError:
            if workspace is not None:
                workspace.cleanup()
            return _failure(request, started, "executor_startup_error", "executor failed to start")
        with workspace as cwd:
            try:
                process = subprocess.Popen(
                    self._command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=(os.name == "posix"),
                )
            except (OSError, ValueError):
                return _failure(request, started, "executor_startup_error", "executor failed to start")

            assert process.stdin and process.stdout and process.stderr
            stdout = _BoundedReader(process.stdout, self._limits.stdout_bytes)
            stderr = _BoundedReader(process.stderr, self._limits.stderr_bytes)
            stdin = _StdinWriter(process.stdin, wire)
            started_workers: list[threading.Thread] = []
            try:
                stdout.start()
                started_workers.append(stdout)
                stderr.start()
                started_workers.append(stderr)
                stdin.start()
                started_workers.append(stdin)
            except Exception:
                _terminate_tree(process, self._limits)
                for pipe in (process.stdin, process.stdout, process.stderr):
                    try:
                        pipe.close()
                    except OSError:
                        pass
                for worker in started_workers:
                    worker.join(self._limits.kill_grace_seconds)
                return _failure(
                    request, started, "executor_startup_error", "executor failed to start"
                )
            deadline = time.monotonic() + self._limits.timeout_seconds
            failure_code = ""
            while process.poll() is None or stdin.is_alive() or stdout.is_alive() or stderr.is_alive():
                if stdout.exceeded.is_set():
                    failure_code = "executor_stdout_limit"
                    break
                if stderr.exceeded.is_set():
                    failure_code = "executor_stderr_limit"
                    break
                if time.monotonic() >= deadline:
                    failure_code = "executor_timeout"
                    break
                time.sleep(0.01)
            if failure_code:
                _terminate_tree(process, self._limits)
            elif process.poll() is not None:
                # The session belongs to this invocation even after its leader exits.
                # Retire descendants that deliberately detached from our stdio pipes.
                _terminate_tree(process, self._limits)

            stdin.join(self._limits.kill_grace_seconds)
            for reader in (stdout, stderr):
                reader.join(self._limits.kill_grace_seconds)

            if stderr.exceeded.is_set() or failure_code == "executor_stderr_limit":
                diagnostic = _bounded_marker(_STDERR_EXCEEDED, self._limits.stderr_bytes)
            else:
                diagnostic = _sanitize_diagnostic(bytes(stderr.data), self._limits.stderr_bytes)
            if failure_code:
                return _failure(request, started, failure_code, _message(failure_code), diagnostic)
            if stdout.exceeded.is_set():
                _terminate_tree(process, self._limits)
                return _failure(request, started, "executor_stdout_limit", _message("executor_stdout_limit"), diagnostic)
            if stderr.exceeded.is_set():
                _terminate_tree(process, self._limits)
                return _failure(request, started, "executor_stderr_limit", _message("executor_stderr_limit"), diagnostic)
            if process.returncode:
                return _failure(request, started, "executor_nonzero_exit", f"executor exited with status {process.returncode}", diagnostic)
            return _parse_result(request, started, bytes(stdout.data), diagnostic)


def _terminate_tree(process: subprocess.Popen[bytes], limits: SubprocessLimits) -> None:
    posix_group = os.name == "posix"
    try:
        if posix_group:
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:  # pragma: no cover - Windows runner
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=limits.terminate_grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    if posix_group:
        deadline = time.monotonic() + limits.terminate_grace_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(process.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.01)
    elif process.poll() is not None:  # pragma: no cover - Windows runner
        return
    try:
        if posix_group:
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - Windows runner
            process.kill()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=limits.kill_grace_seconds)
    except subprocess.TimeoutExpired:
        pass


def _contains_write_path_key(value: object, governed_container: bool = False) -> bool:
    if isinstance(value, Mapping):
        for key, child in value.items():
            text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(key))
            tokens = tuple(re.findall(r"[a-z0-9]+", text.lower()))
            compact = "".join(tokens)
            authority_indexes = (
                index for index, token in enumerate(tokens)
                if token in _GOVERNED_PATH_AUTHORITIES
            )
            governed = (
                compact in _WORKING_DIRECTORY_ALIASES
                or any(
                    any(token in _LOCATION_SUFFIXES for token in tokens[index + 1:])
                    for index in authority_indexes
                )
                or any(
                compact == authority + suffix
                for authority in _GOVERNED_PATH_AUTHORITIES
                for suffix in _LOCATION_SUFFIXES
                )
                or governed_container and len(tokens) == 1 and tokens[0] in _LOCATION_SUFFIXES
            )
            child_governed_container = governed_container or (
                len(tokens) == 1 and tokens[0] in _GOVERNED_PATH_AUTHORITIES
            )
            if governed or _contains_write_path_key(child, child_governed_container):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_write_path_key(child, governed_container) for child in value)
    return False


def _parse_result(request: CaptureRequest, started: datetime, stdout: bytes, diagnostic: str) -> CaptureResult:
    if not stdout:
        return _failure(request, started, "executor_empty_stdout", "executor produced no result", diagnostic)
    try:
        text = stdout.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return _failure(request, started, "executor_invalid_utf8", "executor stdout is not valid UTF-8", diagnostic)
    try:
        payload = json.loads(text, object_pairs_hook=_unique_json_object)
        if (
            isinstance(payload, dict)
            and payload.get("state") == "failed"
            and isinstance(payload.get("error"), dict)
        ):
            error = payload["error"]
            if isinstance(error.get("message"), str):
                error["message"] = _sanitize_diagnostic(
                    error["message"].encode("utf-8"), 64 * 1024
                )
            if isinstance(error.get("code"), str):
                child_code = error["code"]
                if (
                    not _SAFE_ERROR_CODE.fullmatch(child_code)
                    or _is_structured_credential_key(child_code)
                    or _sanitize_diagnostic(child_code.encode("utf-8"), 64 * 1024) != child_code
                ):
                    error["code"] = "executor_child_failed"
            if "metadata" in error:
                error["metadata"] = _sanitize_diagnostic_values(error["metadata"])
            if "metadata" in payload:
                payload["metadata"] = _sanitize_diagnostic_values(payload["metadata"])
            text = json.dumps(payload)
        result = CaptureResult.model_validate_json(text)
    except (RecursionError, ValidationError, ValueError):
        return _failure(request, started, "executor_protocol_error", "executor stdout is not exactly one valid CaptureResult JSON value", diagnostic)
    if any(getattr(result, field) != getattr(request, field) for field in (
        "site_key", "site_skill_id", "site_skill_version", "site_skill_digest",
        "recipe_id", "run_id", "scope_id", "request_id", "executor_id",
    )):
        return _failure(request, started, "executor_identity_mismatch", "executor result identity or lineage does not match request", diagnostic)
    return result


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonKey(f"duplicate JSON object key: {key!r}")
        result[key] = value
    return result


def _sanitize_diagnostic_values(value: object) -> object:
    if isinstance(value, str):
        return _sanitize_diagnostic(value.encode("utf-8"), 64 * 1024)
    if isinstance(value, Mapping):
        sanitized: dict[object, object] = {}
        for key, child in value.items():
            credential_key = _is_structured_credential_key(key)
            safe_key = "[REDACTED]" if credential_key else _sanitize_diagnostic_values(key)
            if safe_key in sanitized:
                raise ValueError("diagnostic metadata keys collide after sanitization")
            sanitized[safe_key] = (
                "[REDACTED]" if credential_key
                else _sanitize_diagnostic_values(child)
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_diagnostic_values(child) for child in value]
    return value


def _sanitize_diagnostic(data: bytes, byte_limit: int) -> str:
    """Return bounded, credential-free text safe for governed metadata."""
    if len(data) > byte_limit:
        return _bounded_marker(_DIAGNOSTIC_EXCEEDED, byte_limit)
    text = data.decode("utf-8", errors="replace")
    stripped = text.strip()
    starts_structured = stripped.startswith("{") or stripped.startswith("[")
    if starts_structured:
        try:
            structured = json.loads(text, object_pairs_hook=_unique_json_object)
        except (json.JSONDecodeError, UnicodeDecodeError):
            if _JSON_STRING_ESCAPE.search(text):
                return _bounded_marker(_INVALID_STRUCTURED_DIAGNOSTIC, byte_limit)
            if _CREDENTIAL_KEY.search(text):
                sanitized_fallback = _sanitize_unstructured_diagnostic(text)
                if sanitized_fallback != text:
                    return _bounded_marker(_INVALID_STRUCTURED_DIAGNOSTIC, byte_limit)
                text = sanitized_fallback
        except (_DuplicateJsonKey, RecursionError, ValueError):
            return _bounded_marker(_INVALID_STRUCTURED_DIAGNOSTIC, byte_limit)
        else:
            try:
                sanitized = _sanitize_structured_diagnostic(structured)
                text = json.dumps(
                    sanitized,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                encoded = text.encode("utf-8")
            except (RecursionError, TypeError, UnicodeEncodeError, ValueError):
                return _bounded_marker(_INVALID_STRUCTURED_DIAGNOSTIC, byte_limit)
            return encoded[:byte_limit].decode("utf-8", errors="ignore")
    if _JSON_STRING_ESCAPE.search(text) and ("{" in text or "[" in text):
        return _bounded_marker(_INVALID_STRUCTURED_DIAGNOSTIC, byte_limit)
    if _STRUCTURED_CREDENTIAL_VALUE.search(text):
        return _bounded_marker(_INVALID_STRUCTURED_DIAGNOSTIC, byte_limit)
    text = _sanitize_unstructured_diagnostic(text)
    # Redaction placeholders may expand the input, so enforce the bound again.
    return text.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")


def _bounded_marker(marker: str, byte_limit: int) -> str:
    return marker.encode("utf-8")[:byte_limit].decode("utf-8", errors="ignore")


def _sanitize_unstructured_diagnostic(text: str) -> str:
    text = _PEM_PRIVATE_KEY.sub("[PRIVATE KEY REDACTED]", text)
    text = _URL_TOKEN.sub("[URL REDACTED]", text)
    text = _COOKIE_HEADER_VALUE.sub(r"\1[REDACTED]", text)
    text = _AUTHORIZATION_VALUE.sub(r"\1[REDACTED]", text)
    text = _CREDENTIAL_KEY.sub(r"\1[REDACTED]", text)
    text = "".join(
        character if character in "\n\r\t" or unicodedata.category(character) != "Cc"
        else f"\\x{ord(character):02x}"
        for character in text
    )
    return text


def _sanitize_structured_diagnostic(value: object, depth: int = 0) -> object:
    if depth > _MAX_STRUCTURED_DIAGNOSTIC_DEPTH:
        raise ValueError("structured diagnostic exceeds maximum depth")
    if isinstance(value, Mapping):
        sanitized: dict[str, object] = {}
        for key, child in value.items():
            safe_key = _sanitize_unstructured_diagnostic(str(key))
            if safe_key in sanitized:
                raise ValueError("structured diagnostic keys collide after sanitization")
            sanitized[safe_key] = (
                "[REDACTED]" if _is_structured_credential_key(key)
                else _sanitize_structured_diagnostic(child, depth + 1)
            )
        return sanitized
    if isinstance(value, list):
        return [_sanitize_structured_diagnostic(child, depth + 1) for child in value]
    if isinstance(value, str):
        return _sanitize_unstructured_diagnostic(value)
    return value


def _is_structured_credential_key(key: object) -> bool:
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", str(key))
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    words = tuple(re.findall(r"[a-z0-9]+", text.lower()))
    expanded_words = tuple(
        part
        for word in words
        for part in next(
            (
                phrase for phrase in _STRUCTURED_CREDENTIAL_PHRASES
                if len(phrase) > 1 and word == "".join(phrase)
            ),
            (word,),
        )
    )
    compact = "".join(words)
    return (
        any(
            expanded_words[index:index + len(phrase)] == phrase
            for phrase in _STRUCTURED_CREDENTIAL_PHRASES
            for index in range(len(expanded_words) - len(phrase) + 1)
        )
        or (
            len(words) == 1
            and compact not in _STRUCTURED_CREDENTIAL_COMPACT_EXCLUSIONS
            and any(compact.endswith(phrase) for phrase in _STRUCTURED_CREDENTIAL_COMPACT_PHRASES)
        )
    )


def _message(code: str) -> str:
    return {
        "executor_timeout": "executor timed out",
        "executor_stdout_limit": "executor stdout exceeded the configured byte limit",
        "executor_stderr_limit": "executor stderr exceeded the configured byte limit",
    }[code]


def _failure(request: CaptureRequest, started: datetime, code: str, message: str, diagnostic: str = "") -> CaptureResult:
    safe_message = _sanitize_diagnostic(message.encode("utf-8"), 64 * 1024)
    safe_diagnostic = _sanitize_diagnostic(diagnostic.encode("utf-8"), 64 * 1024)
    metadata = {"stderr": safe_diagnostic} if safe_diagnostic else {}
    return CaptureResult(
        **request.model_dump(include={
            "site_key", "site_skill_id", "site_skill_version", "site_skill_digest",
            "recipe_id", "run_id", "scope_id", "request_id", "executor_id",
        }),
        state="failed",
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        error=CaptureError(code=code, message=safe_message, retryable=code in {"executor_timeout", "executor_startup_error"}),
        metadata=metadata,
    )


__all__ = ["SubprocessAcquisitionExecutor", "SubprocessLimits"]
