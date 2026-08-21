from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from web_listening.contracts import CaptureError, CaptureRequest, CaptureResult
from web_listening.executors.wrapper_protocol import run_stdio_wrapper


# Match the outer executor protocol ceiling so a nested BrowserAct process can
# never allocate more inspection output than the caller is allowed to retain.
BROWSERACT_STDOUT_LIMIT = 4 * 1024 * 1024
BROWSERACT_STDERR_LIMIT = 64 * 1024
_READ_CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True)
class BoundedCommandResult:
    returncode: int
    stdout: str


class BrowserActCommandError(RuntimeError):
    pass


def execute(request: CaptureRequest, executable: str) -> CaptureResult:
    """Fail closed: BrowserAct is inspection-only and cannot read target URLs."""
    del executable
    started = datetime.now(timezone.utc)
    return _failure(
        request,
        started,
        "browseract_target_execution_disabled",
        "BrowserAct target execution is disabled; only runtime inspection is supported",
    )


def run_bounded_browseract_command(
    argv: Sequence[str], timeout_seconds: float, env: Mapping[str, str] | None = None
) -> BoundedCommandResult:
    """Drain both child streams concurrently while retaining only bounded stdout."""
    process = subprocess.Popen(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=dict(env or {}),
        close_fds=True,
        start_new_session=True,
    )
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
            threading.Thread(
                target=drain,
                args=(process.stdout, stdout, BROWSERACT_STDOUT_LIMIT),
                daemon=True,
            ),
            threading.Thread(
                target=drain,
                args=(process.stderr, None, BROWSERACT_STDERR_LIMIT),
                daemon=True,
            ),
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
            raise BrowserActCommandError(
                "BrowserAct command output exceeded the safe limit"
            )
        remaining = deadline - time.monotonic()
        for reader in readers:
            reader.join(max(0.0, remaining))
        if overflow.is_set():
            raise BrowserActCommandError(
                "BrowserAct command output exceeded the safe limit"
            )
        if any(reader.is_alive() for reader in readers):
            raise subprocess.TimeoutExpired(list(argv), timeout_seconds)
        try:
            decoded = bytes(stdout).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise BrowserActCommandError(
                "BrowserAct command returned invalid UTF-8"
            ) from exc
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


def _failure(
    request: CaptureRequest,
    started: datetime,
    code: str,
    message: str,
    retryable: bool = False,
) -> CaptureResult:
    return CaptureResult(
        **_lineage(request),
        state="failed",
        started_at=started,
        finished_at=datetime.now(timezone.utc),
        error=CaptureError(code=code, message=message, retryable=retryable),
    )


def _lineage(request: CaptureRequest) -> dict[str, Any]:
    return request.model_dump(
        include={
            "site_key",
            "site_skill_id",
            "site_skill_version",
            "site_skill_digest",
            "recipe_id",
            "run_id",
            "scope_id",
            "request_id",
            "executor_id",
        }
    )


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    return run_stdio_wrapper(lambda request: execute(request, sys.argv[1]))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["execute", "main"]
