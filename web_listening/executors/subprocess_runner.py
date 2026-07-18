from __future__ import annotations

import ctypes
import os
import json
import math
import numbers
import re
import signal
import secrets
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
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
_CLEANUP_DIAGNOSTIC = "executor process-lineage cleanup could not be completed"
_CONTROL_BYTES = 4096
_CGROUP_ROOT = "/sys/fs/cgroup"
_CGROUP_ENROLLMENT_SECONDS = 1.0
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


@dataclass(slots=True)
class _ProcessIdentity:
    pid: int
    start_time: int
    pidfd: int

    def close(self) -> None:
        if self.pidfd >= 0:
            os.close(self.pidfd)
            self.pidfd = -1


def _proc_identity(pid: int) -> tuple[int, int, int, str] | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        return pid, int(fields[19]), int(fields[1]), fields[0]
    except (FileNotFoundError, ProcessLookupError, PermissionError, OSError, ValueError, IndexError):
        return None


def _bind_process(pid: int, expected_start: int | None = None) -> _ProcessIdentity | None:
    before = _proc_identity(pid)
    if before is None or (expected_start is not None and before[1] != expected_start):
        return None
    try:
        try:
            pidfd = os.pidfd_open(pid)
        except AttributeError:
            libc = ctypes.CDLL(None, use_errno=True)
            pidfd = libc.pidfd_open(pid, 0)
            if pidfd < 0:
                return None
    except OSError:
        return None
    after = _proc_identity(pid)
    if after is None or after[:2] != before[:2]:
        os.close(pidfd)
        return None
    return _ProcessIdentity(pid, before[1], pidfd)


def _pidfd_signal(identity: _ProcessIdentity, sig: signal.Signals) -> bool:
    current = _proc_identity(identity.pid)
    if current is None:
        return True
    if current[:2] != (identity.pid, identity.start_time):
        return False
    try:
        signal.pidfd_send_signal(identity.pidfd, sig)
        return True
    except AttributeError:
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            return libc.pidfd_send_signal(identity.pidfd, int(sig), None, 0) == 0
        except (AttributeError, OSError):
            return False
    except ProcessLookupError:
        return True
    except OSError:
        return False


_SUPERVISOR_CODE = r'''
import ctypes,json,os,signal,subprocess,sys,time
control_fd=int(sys.argv[1]); command=json.loads(sys.argv[2]); target_env=json.loads(sys.argv[3])
term_grace=float(sys.argv[4]); kill_grace=float(sys.argv[5])
owned_cgroup_fd=int(sys.argv[6])
cancelled=False
def cancel(_signum,_frame):
    global cancelled
    cancelled=True
signal.signal(signal.SIGTERM,cancel)
signal.signal(signal.SIGINT,cancel)
def report(value):
    data=(json.dumps(value,separators=(",",":"))+"\n").encode()
    os.write(control_fd,data)
    os.close(control_fd)
def stat(pid):
    try:
        fields=open("/proc/%d/stat"%pid).read().rsplit(")",1)[1].split()
        return (pid,int(fields[19]),int(fields[1]))
    except (FileNotFoundError,ProcessLookupError,PermissionError,OSError,ValueError,IndexError):
        return None
def snapshot():
    values={}
    try: names=os.listdir("/proc")
    except OSError: return None
    for name in names:
        if name.isdigit():
            value=stat(int(name))
            if value is not None: values[value[0]]=value
    return values
def live(identity):
    value=stat(identity[0])
    return value is not None and value[:2]==identity
def send(identity,sig):
    if not live(identity): return True
    try:
        fd=libc.pidfd_open(identity[0],0)
        if fd<0: return not live(identity)
    except (AttributeError,OSError): return False
    try:
        if not live(identity): return True
        return libc.pidfd_send_signal(fd,int(sig),None,0)==0 or not live(identity)
    except (AttributeError,ProcessLookupError): return not live(identity)
    except OSError: return False
    finally: os.close(fd)
def reap():
    while True:
        try:
            pid,_=os.waitpid(-1,os.WNOHANG)
            if pid==0: return
        except (ChildProcessError,ProcessLookupError): return
def update_tracked(tracked,root_alive):
    values=snapshot()
    if values is None: return None
    changed=True
    while changed:
        changed=False
        parents={pid for pid,start in tracked if values.get(pid,(None,None,None))[:2]==(pid,start)}
        if not root_alive: parents.add(os.getpid())
        for value in values.values():
            identity=value[:2]
            if identity not in tracked and value[2] in parents:
                tracked.add(identity); changed=True
    return values
def descendants_hold_output(tracked,root_alive):
    if update_tracked(tracked,root_alive) is None: return True
    owned={identity[0] for identity in tracked if live(identity)}
    expected={(os.fstat(1).st_dev,os.fstat(1).st_ino),(os.fstat(2).st_dev,os.fstat(2).st_ino)}
    for pid in owned:
        for fd in (1,2):
            try:
                value=os.stat("/proc/%d/fd/%d"%(pid,fd))
                if (value.st_dev,value.st_ino) in expected: return True
            except (FileNotFoundError,ProcessLookupError,PermissionError,OSError): pass
    return False
def cleanup(tracked,root_identity):
    for sig,grace in ((signal.SIGTERM,term_grace),(signal.SIGKILL,kill_grace)):
        deadline=time.monotonic()+grace
        while time.monotonic()<deadline:
            root_alive=live(root_identity)
            if update_tracked(tracked,root_alive) is None: return False
            ok=True
            for identity in tuple(tracked):
                if live(identity) and not send(identity,sig): ok=False
            reap()
            if not any(live(identity) for identity in tracked):
                if update_tracked(tracked,False) is None: return False
                reap()
                if not any(live(identity) for identity in tracked): return ok
            time.sleep(.005)
    reap()
    return not any(live(identity) for identity in tracked)
try:
    libc=ctypes.CDLL(None,use_errno=True)
    if libc.prctl(36,1,0,0,0)!=0: raise OSError("subreaper")
    if libc.prctl(1,int(signal.SIGTERM),0,0,0)!=0: raise OSError("pdeathsig")
    if os.getppid()==1: cancelled=True
    os.set_inheritable(control_fd,False)
    if owned_cgroup_fd>=0:
        fd=os.open("cgroup.procs",os.O_WRONLY,dir_fd=owned_cgroup_fd)
        os.write(fd,str(os.getpid()).encode()); os.close(fd); os.close(owned_cgroup_fd)
    try:
        target=subprocess.Popen(command,close_fds=True,env=target_env)
    except (OSError,ValueError):
        report({"version":1,"startup":False,"cleanup":True}); raise SystemExit(0)
    root_identity=stat(target.pid)
    if root_identity is None: raise OSError("target identity")
    root_identity=root_identity[:2]
    tracked={root_identity}
    while target.poll() is None and not cancelled:
        if update_tracked(tracked,True) is None: cancelled=True
        time.sleep(.005)
    target_rc=target.poll()
    while target_rc is not None and descendants_hold_output(tracked,False) and not cancelled:
        time.sleep(.005)
    cleaned=cleanup(tracked,root_identity)
    if target_rc is None:
        target_rc=target.poll()
    report({"version":1,"startup":True,"returncode":target_rc,"cleanup":cleaned})
except BaseException:
    try: report({"version":1,"startup":False,"cleanup":False})
    except BaseException: pass
    raise SystemExit(125)
'''


@dataclass(slots=True)
class _OwnedCgroup:
    path: str
    removed: bool = False

    def contains(self, pid: int) -> bool:
        if self.removed:
            return False
        try:
            with open(os.path.join(self.path, "cgroup.procs"), encoding="ascii") as stream:
                return any(int(line.strip()) == pid for line in stream if line.strip())
        except (OSError, ValueError):
            return False

    def wait_contains_exact(self, pid: int, start_time: int, timeout: float) -> bool:
        deadline = time.monotonic() + min(timeout, _CGROUP_ENROLLMENT_SECONDS)
        while time.monotonic() < deadline:
            identity = _proc_identity(pid)
            if identity is None or identity[:2] != (pid, start_time):
                return False
            if self.contains(pid):
                identity = _proc_identity(pid)
                return identity is not None and identity[:2] == (pid, start_time)
            time.sleep(.005)
        return False

    def kill_and_remove(self, timeout: float) -> bool:
        if self.removed:
            return True
        deadline = time.monotonic() + timeout
        try:
            populated = _cgroup_populated(self.path)
            if populated is None:
                return False
            if populated:
                with open(os.path.join(self.path, "cgroup.kill"), "w", encoding="ascii") as stream:
                    stream.write("1")
            while time.monotonic() < deadline:
                populated = _cgroup_populated(self.path)
                if populated is False:
                    os.rmdir(self.path)
                    self.removed = True
                    return True
                if populated is None:
                    return False
                time.sleep(.005)
            return False
        except OSError:
            return False


def _cgroup_populated(path: str) -> bool | None:
    try:
        with open(os.path.join(path, "cgroup.events"), encoding="ascii") as stream:
            for line in stream:
                key, value = line.split()
                if key == "populated":
                    return value == "1"
    except (OSError, ValueError):
        return None
    return None


def _create_owned_cgroup() -> _OwnedCgroup | None:
    try:
        with open("/proc/self/cgroup", encoding="ascii") as stream:
            unified = next(
                line.split("::", 1)[1].strip()
                for line in stream
                if line.startswith("0::")
            )
        parent = os.path.realpath(os.path.join(_CGROUP_ROOT, unified.lstrip("/")))
        root = os.path.realpath(_CGROUP_ROOT)
        if os.path.commonpath((root, parent)) != root:
            return None
        path = os.path.join(parent, f"web-listening-{os.getpid()}-{secrets.token_hex(12)}")
        os.mkdir(path, 0o700)
        return _OwnedCgroup(path)
    except (OSError, StopIteration):
        return None


def _snapshot_helper_descendants(
    helper: _ProcessIdentity,
) -> dict[tuple[int, int], tuple[int, int, int, str]] | None:
    root = _proc_identity(helper.pid)
    if root is None or root[:2] != (helper.pid, helper.start_time):
        return None
    values: dict[int, tuple[int, int, int, str]] = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if name.isdigit():
            value = _proc_identity(int(name))
            if value is not None:
                values[value[0]] = value
    owned_pids = {helper.pid}
    descendants: dict[tuple[int, int], tuple[int, int, int, str]] = {}
    changed = True
    while changed:
        changed = False
        for value in values.values():
            identity = value[:2]
            if value[0] != helper.pid and identity not in descendants and value[2] in owned_pids:
                descendants[identity] = value
                owned_pids.add(value[0])
                changed = True
    return descendants


def _kill_exact_helper_tree(
    process: subprocess.Popen[bytes],
    helper: _ProcessIdentity,
    timeout: float,
) -> bool:
    bound: dict[tuple[int, int], _ProcessIdentity] = {}
    success = True
    deadline = time.monotonic() + timeout
    try:
        if not _pidfd_signal(helper, signal.SIGSTOP):
            return False
        stable = 0
        previous: frozenset[tuple[int, int]] | None = None
        while time.monotonic() < deadline and stable < 2:
            snapshot = _snapshot_helper_descendants(helper)
            if snapshot is None:
                success = False
                break
            for identity in snapshot:
                if identity not in bound:
                    item = _bind_process(*identity)
                    if item is None:
                        success = False
                        break
                    bound[identity] = item
                if not _pidfd_signal(bound[identity], signal.SIGSTOP):
                    success = False
                    break
            if not success:
                break
            current = frozenset(snapshot)
            stable = stable + 1 if current == previous else 0
            previous = current
            time.sleep(.005)
        if stable < 2:
            success = False
        for identity in bound.values():
            success = _pidfd_signal(identity, signal.SIGKILL) and success
        success = _pidfd_signal(helper, signal.SIGKILL) and success
        remaining = max(0.001, deadline - time.monotonic())
        try:
            process.wait(timeout=remaining)
        except subprocess.TimeoutExpired:
            success = False
        while time.monotonic() < deadline:
            if all(
                (_proc_identity(item.pid) or (None, None))[:2]
                != (item.pid, item.start_time)
                for item in (*bound.values(), helper)
            ):
                return success
            time.sleep(.005)
        return False
    finally:
        for identity in bound.values():
            identity.close()


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
        if sys.platform != "linux":
            return _failure(request, started, "executor_cleanup_error", _CLEANUP_DIAGNOSTIC)
        workspace: tempfile.TemporaryDirectory[str] | None = None
        try:
            workspace = tempfile.TemporaryDirectory(prefix="web-listening-executor-")
            os.chmod(workspace.name, 0o700)
        except OSError:
            if workspace is not None:
                workspace.cleanup()
            return _failure(request, started, "executor_startup_error", "executor failed to start")
        with workspace as cwd:
            control_read = control_write = -1
            cgroup_fd = -1
            process: subprocess.Popen[bytes] | None = None
            helper_identity: _ProcessIdentity | None = None
            cgroup = _create_owned_cgroup()
            # A writable delegated cgroup v2 is a runtime safety prerequisite.
            if cgroup is None:
                return _failure(
                    request, started, "executor_cleanup_error", _CLEANUP_DIAGNOSTIC
                )
            streams: list[BinaryIO] = []
            workers: list[threading.Thread] = []
            started_workers: list[threading.Thread] = []
            failure_code = ""
            setup_error = False
            interrupted: BaseException | None = None
            cancel_ok = True
            cancel_attempted = False
            enrollment_proved = False
            enrollment_failed = False
            try:
                control_read, control_write = os.pipe()
                cgroup_fd = os.open(cgroup.path, os.O_RDONLY | os.O_DIRECTORY)
                process = subprocess.Popen(
                    (
                        sys.executable, "-c", _SUPERVISOR_CODE, str(control_write),
                        json.dumps(self._command),
                        json.dumps(env),
                        str(self._limits.terminate_grace_seconds),
                        str(self._limits.kill_grace_seconds),
                        str(cgroup_fd),
                    ),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    start_new_session=(os.name == "posix"),
                    pass_fds=(control_write, cgroup_fd),
                )
                helper_identity = _bind_process(process.pid)
                if helper_identity is None:
                    raise OSError("supervisor identity could not be bound")
                enrollment_proved = cgroup.wait_contains_exact(
                    helper_identity.pid,
                    helper_identity.start_time,
                    _CGROUP_ENROLLMENT_SECONDS,
                )
                if not enrollment_proved:
                    enrollment_failed = True
                    cancel_attempted = True
                    cancel_ok = _cancel_supervisor(
                        process, self._limits, cgroup, helper_identity
                    )
                    raise OSError("supervisor cgroup enrollment was not proved")
                os.close(control_write)
                control_write = -1
                if cgroup_fd >= 0:
                    os.close(cgroup_fd)
                    cgroup_fd = -1
                assert process.stdin and process.stdout and process.stderr
                streams.extend((process.stdin, process.stdout, process.stderr))
                control_stream = os.fdopen(control_read, "rb", buffering=0, closefd=False)
                streams.append(control_stream)
                stdout = _BoundedReader(process.stdout, self._limits.stdout_bytes)
                stderr = _BoundedReader(process.stderr, self._limits.stderr_bytes)
                control = _BoundedReader(control_stream, _CONTROL_BYTES)
                stdin = _StdinWriter(process.stdin, wire)
                workers.extend((stdout, stderr, control, stdin))
                for worker in workers:
                    stream = worker.pipe
                    streams.remove(stream)
                    try:
                        worker.start()
                    except BaseException:
                        if worker.ident is None:
                            streams.append(stream)
                        else:
                            started_workers.append(worker)
                        raise
                    started_workers.append(worker)
                deadline = time.monotonic() + self._limits.timeout_seconds
                while process.poll() is None or any(worker.is_alive() for worker in workers):
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
                    cancel_attempted = True
                    cancel_ok = _cancel_supervisor(process, self._limits, cgroup, helper_identity)
                else:
                    try:
                        process.wait(timeout=self._limits.kill_grace_seconds)
                    except subprocess.TimeoutExpired:
                        cancel_attempted = True
                        cancel_ok = _cancel_supervisor(process, self._limits, cgroup, helper_identity)
            except Exception:
                setup_error = True
            except BaseException as exc:
                interrupted = exc
            finally:
                if process is not None and process.poll() is None:
                    cancel_attempted = True
                    cancel_ok = _cancel_supervisor(
                        process, self._limits, cgroup, helper_identity
                    ) and cancel_ok
                for stream in streams:
                    try:
                        stream.close()
                    except OSError:
                        pass
                for fd in (control_write, cgroup_fd):
                    if fd >= 0:
                        try:
                            os.close(fd)
                        except OSError:
                            pass
                for worker in started_workers:
                    worker.join(self._limits.kill_grace_seconds)
                if control_read >= 0:
                    try:
                        os.close(control_read)
                    except OSError:
                        pass
                    control_read = -1
                if helper_identity is not None:
                    helper_identity.close()
                cgroup_ok = cgroup.kill_and_remove(self._limits.kill_grace_seconds)

            stdout_data = bytes(stdout.data) if "stdout" in locals() else b""
            stderr_data = bytes(stderr.data) if "stderr" in locals() else b""
            control_data = bytes(control.data) if "control" in locals() else b""
            control_exceeded = control.exceeded.is_set() if "control" in locals() else False
            status = None if enrollment_failed else _parse_supervisor_status(
                control_data, control_exceeded
            )
            supervisor_cleanup_ok = (
                enrollment_proved
                and status is not None
                and status.get("cleanup") is True
            ) or (cancel_attempted and process is not None and cancel_ok)
            cleanup_proved = (
                cancel_ok
                and cgroup_ok
                and enrollment_proved
                and supervisor_cleanup_ok
            )
            if enrollment_failed:
                return _failure(
                    request, started, "executor_cleanup_error", _CLEANUP_DIAGNOSTIC
                )
            if interrupted is not None:
                if cleanup_proved:
                    raise interrupted
                return _failure(request, started, "executor_cleanup_error", _CLEANUP_DIAGNOSTIC)
            if setup_error:
                code = "executor_startup_error" if cleanup_proved else "executor_cleanup_error"
                message = "executor failed to start" if cleanup_proved else _CLEANUP_DIAGNOSTIC
                return _failure(request, started, code, message)

            if stderr.exceeded.is_set() or failure_code == "executor_stderr_limit":
                diagnostic = _bounded_marker(_STDERR_EXCEEDED, self._limits.stderr_bytes)
            else:
                diagnostic = _sanitize_diagnostic(stderr_data, self._limits.stderr_bytes)
            if not cleanup_proved:
                return _failure(request, started, "executor_cleanup_error", _CLEANUP_DIAGNOSTIC, diagnostic)
            if failure_code:
                return _failure(request, started, failure_code, _message(failure_code), diagnostic)
            assert status is not None
            if status.get("startup") is not True:
                return _failure(request, started, "executor_startup_error", "executor failed to start")
            if stdout.exceeded.is_set():
                return _failure(request, started, "executor_stdout_limit", _message("executor_stdout_limit"), diagnostic)
            if stderr.exceeded.is_set():
                return _failure(request, started, "executor_stderr_limit", _message("executor_stderr_limit"), diagnostic)
            target_returncode = status.get("returncode")
            if not isinstance(target_returncode, int):
                return _failure(request, started, "executor_cleanup_error", _CLEANUP_DIAGNOSTIC, diagnostic)
            if target_returncode:
                return _failure(request, started, "executor_nonzero_exit", f"executor exited with status {target_returncode}", diagnostic)
            return _parse_result(request, started, stdout_data, diagnostic)


def _cancel_supervisor(
    process: subprocess.Popen[bytes],
    limits: SubprocessLimits,
    cgroup: _OwnedCgroup,
    helper_identity: _ProcessIdentity | None = None,
) -> bool:
    # Enrollment is the ownership proof for cgroup-wide cleanup. A malformed
    # helper may never join; freeze its exact tree before signalling it so an
    # unenrolled descendant cannot be orphaned by the helper's early exit.
    if not cgroup.contains(process.pid):
        if helper_identity is None:
            return False
        return _kill_exact_helper_tree(
            process, helper_identity, limits.kill_grace_seconds
        )
    try:
        if process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=limits.terminate_grace_seconds)
    except subprocess.TimeoutExpired:
        pass
    if cgroup.kill_and_remove(limits.kill_grace_seconds):
        try:
            process.wait(timeout=limits.kill_grace_seconds)
            return True
        except subprocess.TimeoutExpired:
            # A malformed or interrupted helper may fail to enroll itself in the
            # owned cgroup. The empty cgroup is then not proof that the exact
            # helper incarnation is gone, so use the identity-bound fallback.
            if helper_identity is None:
                return False
            return _kill_exact_helper_tree(
                process, helper_identity, limits.kill_grace_seconds
            )
    if helper_identity is None:
        return False
    return _kill_exact_helper_tree(
        process, helper_identity, limits.kill_grace_seconds
    )


def _parse_supervisor_status(data: bytes, exceeded: bool) -> dict[str, object] | None:
    if exceeded or len(data) > _CONTROL_BYTES:
        return None
    try:
        value = json.loads(data.decode("ascii"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict) or value.get("version") != 1:
        return None
    return value


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
