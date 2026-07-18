from __future__ import annotations

import json
import ctypes
import io
import os
import re
import sys
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from web_listening.contracts import CaptureRequest, CaptureResult
from web_listening.executors import ExecutorRegistry, SubprocessAcquisitionExecutor, SubprocessLimits
from web_listening.executors import subprocess_runner
from web_listening.executors.wrapper_protocol import run_stdio_wrapper


FAKE = Path(__file__).parent / "fixtures" / "fake_executor.py"


def request() -> CaptureRequest:
    return CaptureRequest(
        site_key="example", site_skill_id="example-skill", site_skill_version="1.0.0",
        site_skill_digest="a" * 64, recipe_id="capture", run_id="run-1", scope_id="scope-1",
        request_id="request-1", executor_id="web_http", url="https://example.com/",
        requested_at=datetime.now(timezone.utc), config={}, metadata={},
    )


def run(mode: str, *, timeout=.5, stdout=8192, stderr=8192, environment=()):
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, str(FAKE), mode),
        limits=SubprocessLimits(timeout_seconds=timeout, stdout_bytes=stdout, stderr_bytes=stderr,
                                terminate_grace_seconds=.2, kill_grace_seconds=.2),
        allowed_environment=environment,
    )
    return executor.execute(request())


@pytest.fixture(autouse=True)
def delegated_cgroup_for_executor_tests(monkeypatch, tmp_path, request):
    if request.node.name == "test_writable_cgroup_escalation_reaps_ignore_term_helper_and_target":
        return
    probe = subprocess_runner._create_owned_cgroup()
    if probe is not None:
        assert probe.kill_and_remove(.2)
        return
    sequence = 0

    class TestOwnedCgroup(subprocess_runner._OwnedCgroup):
        def kill_and_remove(self, timeout):
            if not self.removed:
                procs = Path(self.path) / "cgroup.procs"
                procs.unlink(missing_ok=True)
                Path(self.path).rmdir()
                self.removed = True
            return True

    def create_test_cgroup():
        nonlocal sequence
        sequence += 1
        path = tmp_path / f"delegated-cgroup-{sequence}"
        path.mkdir()
        (path / "cgroup.procs").write_text("")
        return TestOwnedCgroup(str(path))

    monkeypatch.setattr(subprocess_runner, "_create_owned_cgroup", create_test_cgroup)


@pytest.mark.parametrize("field", [
    "timeout_seconds", "terminate_grace_seconds", "kill_grace_seconds",
])
@pytest.mark.parametrize("value", [
    float("nan"), float("inf"), float("-inf"), True, False, 0, -1, "1",
])
def test_subprocess_limits_reject_invalid_time_limits(field, value):
    with pytest.raises(ValueError, match="time limits must be positive"):
        SubprocessLimits(**{field: value})


@pytest.mark.parametrize("field", ["stdout_bytes", "stderr_bytes"])
@pytest.mark.parametrize("value", [
    True, False, 0, -1, 1.0, float("nan"), float("inf"), float("-inf"), "1",
])
def test_subprocess_limits_reject_invalid_output_limits(field, value):
    with pytest.raises(ValueError, match="output limits must be positive"):
        SubprocessLimits(**{field: value})


@pytest.mark.parametrize("value", [1, 0.5])
def test_subprocess_limits_accept_finite_positive_time_limits(value):
    limits = SubprocessLimits(
        timeout_seconds=value,
        terminate_grace_seconds=value,
        kill_grace_seconds=value,
    )
    assert limits.timeout_seconds == value


def test_subprocess_limits_accept_positive_integer_output_limits():
    limits = SubprocessLimits(stdout_bytes=1, stderr_bytes=2)
    assert (limits.stdout_bytes, limits.stderr_bytes) == (1, 2)


def test_success_and_explicit_registry():
    executor = SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success"))
    result = ExecutorRegistry({"web_http": executor}).execute(request())
    assert result.state == "succeeded"
    assert result.content.text == "captured"


@pytest.mark.parametrize(("mode", "code"), [
    ("timeout", "executor_timeout"), ("nonzero", "executor_nonzero_exit"),
    ("exception", "executor_nonzero_exit"),
    ("malformed", "executor_protocol_error"), ("mixed", "executor_protocol_error"),
    ("multiple", "executor_protocol_error"), ("invalid_utf8", "executor_invalid_utf8"),
    ("empty", "executor_empty_stdout"), ("mismatch", "executor_identity_mismatch"),
    ("duplicate_top", "executor_protocol_error"),
    ("duplicate_nested", "executor_protocol_error"),
])
def test_stable_failures(mode, code):
    result = run(mode)
    assert result.state == "failed"
    assert result.error.code == code
    assert result.request_id == "request-1"


def test_excessively_nested_result_is_structured_secret_free_protocol_failure():
    result = run("excessively_nested")
    assert result.state == "failed"
    assert result.error.code == "executor_protocol_error"
    serialized = result.model_dump_json()
    assert "nested-secret" not in serialized
    assert "Traceback" not in serialized


def test_startup_error_is_structured():
    secret = "trusted-secret-path"
    result = SubprocessAcquisitionExecutor("web_http", (f"/definitely/missing/{secret}",)).execute(request())
    assert result.error.code == "executor_startup_error"
    assert result.error.message == "executor failed to start"
    assert secret not in result.model_dump_json()


def test_temporary_workspace_creation_error_is_structured(monkeypatch):
    monkeypatch.setattr(
        subprocess_runner.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: (_ for _ in ()).throw(OSError("workspace-secret")),
    )
    result = run("success")
    assert result.error.code == "executor_startup_error"
    assert result.error.message == "executor failed to start"
    assert "workspace-secret" not in result.model_dump_json()


def test_temporary_workspace_setup_error_is_structured_and_cleans_up(monkeypatch):
    workspace_path = None

    def fail_chmod(path, mode):
        nonlocal workspace_path
        workspace_path = Path(path)
        raise OSError("chmod-secret")

    monkeypatch.setattr(subprocess_runner.os, "chmod", fail_chmod)
    result = run("success")
    assert result.error.code == "executor_startup_error"
    assert result.error.message == "executor failed to start"
    assert "chmod-secret" not in result.model_dump_json()
    assert workspace_path is not None
    assert not workspace_path.exists()


@pytest.mark.skipif(os.name != "posix", reason="process state assertion uses /proc")
def test_reader_thread_start_failure_cleans_owned_child(monkeypatch):
    child_pid = None
    real_popen = subprocess_runner.subprocess.Popen

    def recording_popen(*args, **kwargs):
        nonlocal child_pid
        process = real_popen(*args, **kwargs)
        child_pid = process.pid
        return process

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        subprocess_runner._BoundedReader,
        "start",
        lambda self: (_ for _ in ()).throw(RuntimeError("thread-start-secret")),
    )
    result = run("timeout", timeout=2)
    assert result.error.code == "executor_startup_error"
    assert result.error.message == "executor failed to start"
    assert "thread-start-secret" not in result.model_dump_json()
    assert child_pid is not None
    _assert_dead_or_zombie(child_pid)


def test_registry_rejects_executor_identity_mismatch():
    executor = SubprocessAcquisitionExecutor("browser_rendered", (sys.executable, str(FAKE), "success"))
    with pytest.raises(ValueError, match="does not match executor identity"):
        ExecutorRegistry({"web_http": executor})


def test_registry_rejects_untrusted_mapping():
    executor = SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success"))
    with pytest.raises(ValueError, match="untrusted executor mapping key"):
        ExecutorRegistry({"attacker_command": executor})


def test_executor_rejects_request_identity_mismatch_before_start():
    executor = SubprocessAcquisitionExecutor("browser_rendered", ("/definitely/missing/executor",))
    result = executor.execute(request())
    assert result.error.code == "executor_identity_mismatch"


def test_registry_rejects_duplicate_mapping_deterministically():
    executor = SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success"))

    class DuplicateMapping(dict):
        def items(self):
            return [("web_http", executor), ("web_http", executor)]

    with pytest.raises(ValueError, match="duplicate trusted executor mapping"):
        ExecutorRegistry(DuplicateMapping())


def test_stdout_and_stderr_limits_are_distinct_and_bounded():
    assert run("stdout_large", stdout=64).error.code == "executor_stdout_limit"
    result = run("stderr_large", stderr=64)
    assert result.error.code == "executor_stderr_limit"
    assert result.metadata["stderr"] == "[stderr exceeded configured byte limit]"


def test_stderr_limit_marker_respects_one_byte_limit_without_secret_leak():
    result = run("stderr_limit_credential", stderr=1)

    assert result.error.code == "executor_stderr_limit"
    assert len(result.metadata["stderr"].encode("utf-8")) <= 1
    assert "cut-user" not in result.model_dump_json()
    assert "cut-password" not in result.model_dump_json()


def test_environment_is_empty_except_explicit_allowlist(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    monkeypatch.setenv("DATABASE_URL", "secret")
    result = run("environment", environment=("TZ",))
    assert result.metadata["environment_names"] == ("TZ",)


@pytest.mark.parametrize("name", [
    "PATH", "PYTHONPATH", "PYTHONSTARTUP", "LD_PRELOAD", "LD_LIBRARY_PATH",
    "DYLD_INSERT_LIBRARIES", "SSL_CERT_FILE", "TMPDIR", "HOME", "HTTPS_PROXY",
    "AWS_ACCESS_KEY_ID", "GOOGLE_CLOUD_PROJECT", "DATABASE_URL", "WL_DB_PATH",
    "APPLICATION_CONFIG",
])
def test_sensitive_environment_names_cannot_be_allowlisted(name):
    with pytest.raises(ValueError, match="unsafe environment"):
        SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success"), allowed_environment=(name,))


def test_timeout_starts_before_large_stdin_write_can_block():
    large = request().model_copy(update={"metadata": {"padding": "x" * (2 * 1024 * 1024)}})
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, str(FAKE), "stdin_block"),
        limits=SubprocessLimits(timeout_seconds=.2, terminate_grace_seconds=.2, kill_grace_seconds=.2),
    )
    started = time.monotonic()
    result = executor.execute(large)
    assert result.error.code == "executor_timeout"
    assert time.monotonic() - started < 2


def test_timeout_kills_process_tree():
    result = run("tree", timeout=.3)
    pid = int(re.search(r"child_pid=(\d+)", result.metadata["stderr"]).group(1))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        # A killed child may remain briefly as a zombie until adopted/reaped.
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split()[2] == "Z":
            break
        time.sleep(.02)
    else:
        pytest.fail("descendant survived executor timeout")


def _assert_dead_or_zombie(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists() and stat.read_text().split()[2] == "Z":
            return
        time.sleep(.02)
    pytest.fail("descendant survived executor cleanup")


def _assert_gone(pid: int) -> None:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if not Path(f"/proc/{pid}").exists():
            return
        time.sleep(.02)
    pytest.fail("descendant was not reaped")


def _direct_children(pid: int) -> set[int]:
    children = set()
    for stat_path in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat_path.read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == pid:
                children.add(int(stat_path.parent.name))
        except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
            pass
    return children


def test_leader_exit_with_inherited_pipes_remains_deadline_supervised():
    started = time.monotonic()
    result = run("leader_exit_pipes", timeout=.25)
    assert result.error.code == "executor_timeout"
    assert time.monotonic() - started < 1.5
    _assert_dead_or_zombie(int(re.search(r"child_pid=(\d+)", result.metadata["stderr"]).group(1)))


@pytest.mark.skipif(os.name != "posix", reason="process-group cleanup is POSIX-specific")
def test_successful_leader_exit_cleans_devnull_descendant_without_corrupting_result():
    result = run("success_with_devnull_descendant", timeout=2)
    assert result.state == "succeeded"
    assert result.content.text == "captured"
    _assert_dead_or_zombie(result.metadata["child_pid"])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lineage cleanup")
@pytest.mark.parametrize(("mode", "code"), [
    ("detached_success", None),
    ("fast_detach", None),
    ("detached_stopped", None),
    ("detached_timeout", "executor_timeout"),
    ("detached_nonzero", "executor_nonzero_exit"),
    ("detached_protocol", "executor_protocol_error"),
    ("detached_output_limit", "executor_stdout_limit"),
])
def test_detached_setsid_descendant_is_cleaned_on_success_and_timeout(mode, code):
    result = run(mode, timeout=.3 if mode == "detached_timeout" else 2, stdout=64 if mode == "detached_output_limit" else 8192)
    if code:
        assert result.error.code == code
        pid = int(re.search(r"child_pid=(\d+)", result.metadata["stderr"]).group(1))
    else:
        assert result.state == "succeeded"
        pid = result.metadata["child_pid"]
    _assert_gone(pid)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lineage cleanup")
@pytest.mark.parametrize(("outcome", "code"), [
    ("success", None), ("timeout", "executor_timeout"),
    ("nonzero", "executor_nonzero_exit"), ("protocol", "executor_protocol_error"),
    ("output_limit", "executor_stdout_limit"),
])
def test_nested_detached_descendant_is_cleaned_and_reaped(outcome, code):
    result = run(
        f"nested_detached_{outcome}",
        timeout=.3 if outcome == "timeout" else 2,
        stdout=64 if outcome == "output_limit" else 8192,
    )
    if code is None:
        assert result.state == "succeeded"
        pids = (result.metadata["child_pid"], result.metadata["grandchild_pid"])
    else:
        assert result.error.code == code
        pids = tuple(map(int, re.findall(r"(?m)^(?:child|grandchild)_pid=(\d+)", result.metadata["stderr"])))
    for pid in pids:
        _assert_gone(pid)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lineage cleanup")
def test_repeated_detached_runs_leave_no_survivors_or_zombies():
    pids = []
    for _ in range(5):
        result = run("fast_detach", timeout=2)
        assert result.state == "succeeded"
        pids.append(result.metadata["child_pid"])
    for pid in pids:
        _assert_gone(pid)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lineage cleanup")
def test_concurrent_executors_overlap_and_neither_kills_the_other():
    barrier = threading.Barrier(3)
    results = []

    def invoke():
        barrier.wait()
        results.append(run("detached_success", timeout=2))

    workers = [threading.Thread(target=invoke) for _ in range(2)]
    started = time.monotonic()
    for worker in workers:
        worker.start()
    barrier.wait()
    for worker in workers:
        worker.join(5)

    assert len(results) == 2
    assert time.monotonic() - started < 1
    assert all(result.state == "succeeded" for result in results)
    for result in results:
        _assert_gone(result.metadata["child_pid"])


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lineage cleanup")
@pytest.mark.parametrize("start_unrelated_after_executor", [False, True])
@pytest.mark.parametrize("new_session", [False, True])
def test_unrelated_application_child_is_never_signalled(start_unrelated_after_executor, new_session):
    unrelated = None
    result_holder = []
    worker = None
    try:
        if not start_unrelated_after_executor:
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=new_session)
        else:
            worker = threading.Thread(
                target=lambda: result_holder.append(run("detached_timeout", timeout=.3))
            )
            worker.start()
            time.sleep(.05)
            unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=new_session)

        result = run("detached_success", timeout=2) if worker is None else None
        if worker is not None:
            worker.join(5)
            assert len(result_holder) == 1
            result = result_holder[0]
        assert result is not None
        assert unrelated.poll() is None
    finally:
        if unrelated is not None and unrelated.poll() is None:
            unrelated.terminate()
            unrelated.wait(timeout=2)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc lineage cleanup")
def test_supervisor_control_corruption_is_structured_and_bounded(monkeypatch):
    monkeypatch.setattr(
        subprocess_runner, "_SUPERVISOR_CODE",
        "import os,sys; os.write(int(sys.argv[1]), b'corrupt')",
    )
    result = run("success")
    assert result.state == "failed"
    assert result.error.code == "executor_cleanup_error"
    assert result.error.message == "executor process-lineage cleanup could not be completed"
    assert len(result.error.message.encode()) < 128


def test_supervisor_startup_failure_is_cleanup_error(monkeypatch):
    monkeypatch.setattr(
        subprocess_runner.subprocess, "Popen",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("helper-start-secret")),
    )
    result = run("success")
    assert result.error.code == "executor_cleanup_error"
    assert "helper-start-secret" not in result.model_dump_json()


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc cleanup proof")
def test_control_fdopen_failure_reaps_helper_and_target(monkeypatch):
    helper_pid = None
    target_pids: set[int] = set()
    real_popen = subprocess_runner.subprocess.Popen
    real_fdopen = subprocess_runner.os.fdopen

    def recording_popen(*args, **kwargs):
        nonlocal helper_pid
        process = real_popen(*args, **kwargs)
        helper_pid = process.pid
        return process

    def failing_fdopen(fd, *args, **kwargs):
        deadline = time.monotonic() + 1
        while helper_pid is not None and time.monotonic() < deadline:
            target_pids.update(_direct_children(helper_pid))
            if target_pids:
                break
            time.sleep(.005)
        raise OSError("fdopen-secret")

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(subprocess_runner.os, "fdopen", failing_fdopen)
    result = run("timeout", timeout=2)
    monkeypatch.setattr(subprocess_runner.os, "fdopen", real_fdopen)
    assert result.error.code == "executor_startup_error"
    assert helper_pid is not None and target_pids
    _assert_gone(helper_pid)
    for pid in target_pids:
        _assert_gone(pid)


def test_control_stream_keeps_raw_fd_owned_until_finally(monkeypatch):
    real_fdopen = subprocess_runner.os.fdopen
    adopted_fd = None
    replacement_fd = None

    class StreamProxy:
        def __init__(self, stream):
            self.stream = stream

        def read(self, *args, **kwargs):
            return self.stream.read(*args, **kwargs)

        def close(self):
            nonlocal replacement_fd
            self.stream.close()
            replacement_fd = os.open("/dev/null", os.O_RDONLY)

    def recording_fdopen(fd, *args, **kwargs):
        nonlocal adopted_fd
        adopted_fd = fd
        assert kwargs["closefd"] is False
        return StreamProxy(real_fdopen(fd, *args, **kwargs))

    monkeypatch.setattr(subprocess_runner.os, "fdopen", recording_fdopen)
    try:
        assert run("success").state == "succeeded"
        assert adopted_fd is not None and replacement_fd is not None
        assert replacement_fd != adopted_fd
        os.fstat(replacement_fd)
    finally:
        if replacement_fd is not None:
            os.close(replacement_fd)


@pytest.mark.skipif(sys.platform != "linux", reason="Linux /proc cleanup proof")
def test_keyboard_interrupt_reaps_helper_and_target_before_reraising(monkeypatch):
    helper_pid = None
    target_pids: set[int] = set()
    raised = False
    real_popen = subprocess_runner.subprocess.Popen
    real_sleep = time.sleep

    def recording_popen(*args, **kwargs):
        nonlocal helper_pid
        process = real_popen(*args, **kwargs)
        helper_pid = process.pid
        return process

    def interrupt_once(seconds):
        nonlocal raised
        if threading.current_thread() is threading.main_thread() and helper_pid is not None and not raised:
            target_pids.update(_direct_children(helper_pid))
            if target_pids:
                raised = True
                raise KeyboardInterrupt
        real_sleep(seconds)

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(subprocess_runner.time, "sleep", interrupt_once)
    with pytest.raises(KeyboardInterrupt):
        run("timeout", timeout=2)
    assert helper_pid is not None and target_pids
    _assert_gone(helper_pid)
    for pid in target_pids:
        _assert_gone(pid)


def test_supervisor_root_pid_reuse_replacement_is_not_tracked_or_signalled():
    code = subprocess_runner._SUPERVISOR_CODE
    function_source = "def update_tracked" + code.split("def update_tracked", 1)[1].split(
        "def descendants_hold_output", 1
    )[0]
    replacement = (4100, 999, 1)
    adopted = (4200, 300, 77)
    namespace = {
        "snapshot": lambda: {replacement[0]: replacement, adopted[0]: adopted},
        "os": type("FakeOS", (), {"getpid": staticmethod(lambda: 77)}),
    }
    exec(function_source, namespace)
    tracked = {(4100, 100)}
    namespace["update_tracked"](tracked, False)
    assert (4100, 999) not in tracked
    assert (4200, 300) in tracked


def test_unenrolled_supervisor_timeout_is_cleanup_failure(monkeypatch):
    monkeypatch.setattr(
        subprocess_runner, "_SUPERVISOR_CODE",
        "import signal,time; signal.signal(signal.SIGTERM, lambda *_: None); time.sleep(.5)",
    )
    result = run("success", timeout=.05)
    assert result.error.code == "executor_cleanup_error"


@pytest.mark.parametrize(
    "supervisor_code",
    [subprocess_runner._SUPERVISOR_CODE, "import os; os._exit(9)"],
    ids=["normal-supervisor", "abrupt-supervisor-death"],
)
def test_no_owned_cgroup_refuses_execution_before_pipe_or_popen(
    monkeypatch, supervisor_code
):
    pipe_calls = []
    popen_calls = []
    monkeypatch.setattr(subprocess_runner, "_create_owned_cgroup", lambda: None)
    monkeypatch.setattr(subprocess_runner, "_SUPERVISOR_CODE", supervisor_code)
    monkeypatch.setattr(
        subprocess_runner.os, "pipe", lambda: pipe_calls.append(True) or (10, 11)
    )
    monkeypatch.setattr(
        subprocess_runner.subprocess,
        "Popen",
        lambda *args, **kwargs: popen_calls.append((args, kwargs)),
    )

    result = run("success")

    assert result.error.code == "executor_cleanup_error"
    assert result.error.message == "executor process-lineage cleanup could not be completed"
    assert pipe_calls == []
    assert popen_calls == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux pidfd /proc enrollment cleanup")
def test_unenrolled_supervisor_forged_cleanup_is_rejected_and_child_reaped(
    monkeypatch, tmp_path
):
    cgroup_path = tmp_path / "owned-cgroup"
    cgroup_path.mkdir()
    (cgroup_path / "cgroup.procs").write_text("")
    ready = tmp_path / "child-pid"

    class FakeOwnedCgroup(subprocess_runner._OwnedCgroup):
        def kill_and_remove(self, timeout):
            if not self.removed:
                (Path(self.path) / "cgroup.procs").unlink()
                Path(self.path).rmdir()
                self.removed = True
            return True

    owned = FakeOwnedCgroup(str(cgroup_path))
    monkeypatch.setattr(subprocess_runner, "_create_owned_cgroup", lambda: owned)
    monkeypatch.setattr(
        subprocess_runner,
        "_SUPERVISOR_CODE",
        "import json,os,signal,subprocess,sys,time; "
        "signal.signal(signal.SIGTERM,lambda *_:None); "
        "child=subprocess.Popen([sys.executable,'-c','import signal,time; "
        "signal.signal(signal.SIGTERM,lambda *_:None); time.sleep(30)']); "
        f"fd=os.open({str(ready)!r},os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); "
        "os.write(fd,str(child.pid).encode()); os.fsync(fd); os.close(fd); "
        "os.write(int(sys.argv[1]),json.dumps({'version':1,'startup':True,"
        "'returncode':0,'cleanup':True}).encode()); time.sleep(30)",
    )

    result = run("success", timeout=2)

    assert ready.exists()
    child_pid = int(ready.read_text())
    assert result.error.code == "executor_cleanup_error"
    assert result.error.message == "executor process-lineage cleanup could not be completed"
    _assert_gone(child_pid)
    assert owned.removed
    assert not cgroup_path.exists()


def test_parent_fallback_rejects_reused_helper_identity_without_signalling(monkeypatch):
    helper = subprocess_runner._ProcessIdentity(4100, 100, 9)
    monkeypatch.setattr(
        subprocess_runner, "_proc_identity", lambda pid: (4100, 999, 1, "S")
    )
    signalled = []
    monkeypatch.setattr(
        subprocess_runner.signal, "pidfd_send_signal",
        lambda *args: signalled.append(args), raising=False,
    )
    process = type("Process", (), {})()
    assert not subprocess_runner._kill_exact_helper_tree(process, helper, .1)
    assert signalled == []


@pytest.mark.skipif(sys.platform != "linux", reason="Linux cgroup-v2 escalation")
def test_writable_cgroup_escalation_reaps_ignore_term_helper_and_target(monkeypatch):
    probe = subprocess_runner._create_owned_cgroup()
    if probe is None:
        pytest.skip("current delegated cgroup is not writable")
    assert probe.kill_and_remove(.2)
    helper_pid = None
    real_popen = subprocess_runner.subprocess.Popen

    def recording_popen(*args, **kwargs):
        nonlocal helper_pid
        process = real_popen(*args, **kwargs)
        helper_pid = process.pid
        return process

    monkeypatch.setattr(subprocess_runner.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(
        subprocess_runner,
        "_SUPERVISOR_CODE",
        "import os,signal,subprocess,sys,time; "
        "fd=int(sys.argv[6]); p=os.open('cgroup.procs',os.O_WRONLY,dir_fd=fd); "
        "os.write(p,str(os.getpid()).encode()); os.close(p); os.close(fd); "
        "signal.signal(signal.SIGTERM,lambda *_:None); "
        "child=subprocess.Popen([sys.executable,'-c','import signal,time; "
        "signal.signal(signal.SIGTERM,lambda *_:None); time.sleep(30)']); "
        "sys.stderr.write(f'child_pid={child.pid}\\n'); sys.stderr.flush(); time.sleep(30)",
    )
    result = run("success", timeout=.1)
    target_pid = int(re.search(r"child_pid=(\d+)", result.metadata["stderr"]).group(1))
    assert result.error.code == "executor_timeout"
    assert helper_pid is not None
    _assert_gone(helper_pid)
    _assert_gone(target_pid)


def test_supervisor_reports_cleanup_failure_as_cleanup_error(monkeypatch):
    monkeypatch.setattr(
        subprocess_runner, "_SUPERVISOR_CODE",
        "import json,os,sys; os.write(int(sys.argv[1]), json.dumps({\"version\":1,\"startup\":True,\"returncode\":0,\"cleanup\":False}).encode())",
    )
    result = run("success")
    assert result.error.code == "executor_cleanup_error"


@pytest.mark.skipif(sys.platform != "linux", reason="Linux subreaper state")
def test_gateway_subreaper_state_is_unchanged():
    libc = ctypes.CDLL(None)
    def state():
        value = ctypes.c_int()
        assert libc.prctl(37, ctypes.byref(value), 0, 0, 0) == 0
        return value.value
    before = state()
    assert run("detached_success", timeout=2).state == "succeeded"
    assert state() == before


def test_no_gateway_tracker_threads_or_fd_leak_after_repeated_runs():
    before_threads = {thread.ident for thread in threading.enumerate()}
    before_fds = len(tuple(Path("/proc/self/fd").iterdir()))
    for _ in range(10):
        assert run("fast_detach", timeout=2).state == "succeeded"
    assert {thread.ident for thread in threading.enumerate()} == before_threads
    assert len(tuple(Path("/proc/self/fd").iterdir())) == before_fds


@pytest.mark.parametrize(("mode", "code"), [
    ("late_stdout_large", "executor_stdout_limit"),
    ("late_stderr_large", "executor_stderr_limit"),
])
def test_limit_after_leader_exit_kills_residual_process_group(mode, code):
    result = run(mode, timeout=2, stdout=64, stderr=64)
    assert result.error.code == code
    if mode == "late_stdout_large":
        _assert_dead_or_zombie(int(re.search(r"child_pid=(\d+)", result.metadata["stderr"]).group(1)))
    else:
        assert result.metadata["stderr"] == "[stderr exceeded configured byte limit]"


def test_output_limit_kills_process_tree():
    result = run("tree_stdout_large", timeout=2, stdout=64)
    assert result.error.code == "executor_stdout_limit"
    pid = int(re.search(r"child_pid=(\d+)", result.metadata["stderr"]).group(1))
    stat = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + 2
    while stat.exists() and time.monotonic() < deadline:
        if stat.read_text().split()[2] == "Z":
            break
        time.sleep(.02)
    else:
        if stat.exists():
            pytest.fail("descendant survived stdout limit breach")


def test_transport_does_not_convey_official_write_paths():
    executor = SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success"))
    exposed = " ".join(executor.command).lower()
    for forbidden in ("database", "blob", "report", "manifest", "wl_data_dir", "wl_db_path"):
        assert forbidden not in exposed


def test_request_storage_path_is_rejected_before_child_start():
    unsafe = request().model_copy(update={"config": {"manifest_path": "/official/manifest.json"}})
    result = SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success")).execute(unsafe)
    assert result.error.code == "executor_request_rejected"


@pytest.mark.parametrize("container", ["config", "metadata"])
@pytest.mark.parametrize("key", [
    "report_dir", "blob-dir", "ManifestPath", "outputPath", "databasePath",
    "downloadsDir", "manifestpath", "storage_directory", "artifactPath", "cwd",
    "manifest_file", "report_root", "blob_location", "database_url", "artifact_file",
    "output_file", "db_file", "storage_root", "workdir", "working_dir",
])
def test_nested_governed_path_key_variants_are_rejected(container, key):
    unsafe = request().model_copy(update={container: {"ordinary": {"nested": {key: "/governed"}}}})
    result = SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success")).execute(unsafe)
    assert result.error.code == "executor_request_rejected"


@pytest.mark.parametrize("payload", [
    {"artifact_output_path": "/governed"},
    {"artifact": {"path": "/governed"}},
    {"database": {"url": "sqlite:///governed.db"}},
    {"manifest": {"file": "/governed/manifest.json"}},
])
def test_governed_compound_keys_and_nested_authority_locations_are_rejected(payload):
    unsafe = request().model_copy(update={"config": payload})
    result = SubprocessAcquisitionExecutor(
        "web_http", (sys.executable, str(FAKE), "success")
    ).execute(unsafe)
    assert result.error.code == "executor_request_rejected"


@pytest.mark.parametrize("key", [
    "workingDirectory", "working_directory", "working-directory",
])
def test_nested_working_directory_key_variants_are_rejected(key):
    unsafe = request().model_copy(update={
        "config": {"ordinary": {"nested": {key: "/governed"}}},
    })
    result = SubprocessAcquisitionExecutor(
        "web_http", (sys.executable, str(FAKE), "success")
    ).execute(unsafe)
    assert result.error.code == "executor_request_rejected"


def test_governed_words_in_ordinary_values_are_not_rejected():
    safe = request().model_copy(update={"metadata": {"description": "download report data"}})
    assert SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success")).execute(safe).state == "succeeded"


@pytest.mark.parametrize("key", ["report", "data", "output", "description", "source_url"])
def test_ordinary_keys_are_not_rejected(key):
    safe = request().model_copy(update={"config": {"nested": {key: "ordinary value"}}})
    assert SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success")).execute(safe).state == "succeeded"


@pytest.mark.parametrize("payload", [
    {"artifact": {"description": "ordinary value"}},
    {"category": {"path": "ordinary value"}},
    {"database_notes": {"source_url": "https://example.com/source"}},
    {"manifestation": {"file": "ordinary value"}},
])
def test_non_location_authority_content_and_non_authority_containers_are_accepted(payload):
    safe = request().model_copy(update={"config": payload})
    assert SubprocessAcquisitionExecutor(
        "web_http", (sys.executable, str(FAKE), "success")
    ).execute(safe).state == "succeeded"


def test_stderr_redaction_never_echoes_exact_secrets_and_stays_bounded():
    result = run("stderr_credentials", stderr=512)
    assert result.error.code == "executor_nonzero_exit"
    redacted = result.metadata["stderr"]
    for secret in (
        "supersecret", "dXNlcjpwYXNz", "standalonesecret", "uri-user", "uri-pass",
        "top-secret", "cookie-secret", "pass-secret", "api-secret", "token-secret",
        "cookie-assignment", "pass-assignment", "api-assignment",
    ):
        assert secret not in redacted
    assert "[REDACTED]" in redacted
    assert len(redacted.encode()) <= 512


def test_proxy_credential_assignment_is_redacted_from_subprocess_stderr():
    secret = "proxy-credential-VALUE-9f83"
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write('proxy-credential={secret}'); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "proxy-credential=[REDACTED]"
    assert secret not in result.model_dump_json()


@pytest.mark.parametrize("key", [
    "credential", "credentials", "proxy_credential", "proxy-credentials",
    "proxyCredential", "proxyCredentials", "proxycredential", "proxycredentials",
])
def test_json_credential_labelled_diagnostics_are_redacted(key):
    secret = "json-credential-VALUE-9f83"
    diagnostic = f'{{"{key}": "{secret}"}}'

    redacted = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert redacted == f'{{"{key}":"[REDACTED]"}}'
    assert secret not in redacted


@pytest.mark.parametrize(("diagnostic", "byte_limit", "marker"), [
    (b"xx", 1, "[diagnostic exceeded configured byte limit]"),
    (b'{"client\\u0053":', 19, "[invalid structured diagnostic redacted]"),
])
def test_fixed_diagnostic_markers_respect_configured_byte_limit(diagnostic, byte_limit, marker):
    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic, byte_limit)

    assert sanitized == marker.encode("utf-8")[:byte_limit].decode("utf-8")
    assert len(sanitized.encode("utf-8")) <= byte_limit


def test_credential_labelled_multi_member_object_is_redacted_end_to_end():
    secret = "OBJECT-CREDENTIAL-SECRET-9f83"
    diagnostic = '{"credentials":{"username":"alice","value":"' + secret + '"}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == '{"credentials":"[REDACTED]"}'
    assert secret not in result.model_dump_json()


def test_prefixed_credential_labelled_object_fails_closed_directly():
    secret = "SYNTHETIC-SECRET"
    diagnostic = f'prefix {{"credentials": {{"username": "alice", "value": "{secret}"}}}}'

    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert sanitized == "[invalid structured diagnostic redacted]"
    assert secret not in sanitized


def test_prefixed_credential_labelled_object_fails_closed_end_to_end():
    secret = "SYNTHETIC-SECRET"
    diagnostic = f'prefix {{"credentials": {{"username": "alice", "value": "{secret}"}}}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert secret not in result.model_dump_json()


def test_prefixed_escaped_credential_label_fails_closed_directly():
    secret = "ESCAPED-PREFIX-SECRET"
    diagnostic = f'prefix {{"client\\u0053ecret":{{"value":"{secret}"}}}}'

    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert sanitized == "[invalid structured diagnostic redacted]"
    assert secret not in sanitized


def test_prefixed_escaped_credential_label_fails_closed_end_to_end():
    secret = "ESCAPED-PREFIX-SECRET"
    diagnostic = f'prefix {{"client\\u0053ecret":{{"value":"{secret}"}}}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert secret not in result.model_dump_json()


def test_deep_structured_stderr_fails_closed_end_to_end():
    diagnostic = "{\"safe\":" * 600 + "null" + "}" * 600
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert result.state == "failed"


def test_oversized_integer_structured_diagnostic_fails_closed_directly():
    diagnostic = b"[" + b"1" * 5000 + b"]"

    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic, 6000)

    assert sanitized == "[invalid structured diagnostic redacted]"
    assert len(sanitized.encode("utf-8")) <= 6000


def test_oversized_integer_structured_stderr_fails_closed_end_to_end():
    diagnostic = "[" + "1" * 5000 + "]"
    capture_request = request()
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
        limits=SubprocessLimits(stderr_bytes=6000),
    )

    result = executor.execute(capture_request)

    assert result.state == "failed"
    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert len(result.metadata["stderr"].encode("utf-8")) <= 6000
    for field in (
        "site_key", "site_skill_id", "site_skill_version", "site_skill_digest",
        "recipe_id", "run_id", "scope_id", "request_id", "executor_id",
    ):
        assert getattr(result, field) == getattr(capture_request, field)


def test_lone_surrogate_structured_diagnostic_fails_closed_directly():
    diagnostic = bytes.fromhex("7b2273616665223a225c7564383030227d")

    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic, 512)

    assert sanitized == "[invalid structured diagnostic redacted]"
    assert len(sanitized.encode("utf-8")) <= 512


def test_lone_surrogate_structured_stderr_fails_closed_end_to_end():
    diagnostic = bytes.fromhex("7b2273616665223a225c7564383030227d")
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (
            sys.executable,
            "-c",
            f"import sys; sys.stderr.buffer.write(bytes.fromhex({diagnostic.hex()!r})); sys.exit(1)",
        ),
    )

    result = executor.execute(request())

    assert result.state == "failed"
    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"


@pytest.mark.parametrize(("diagnostic", "expected"), [
    ('{"outer":{"client_secret":{"value":"nested-secret"}}}', '{"outer":{"client_secret":"[REDACTED]"}}'),
    ('{"outer":[{"credentials":["array-secret",{"visible":"also-secret"}]}]}', '{"outer":[{"credentials":"[REDACTED]"}]}'),
    ('{"refresh_token":{"value":"refresh-secret"}}', '{"refresh_token":"[REDACTED]"}'),
    ('{"items":[{"proxy_password":"proxy-secret"}]}', '{"items":[{"proxy_password":"[REDACTED]"}]}'),
    ('{"client_api_key":["api-secret",{"visible":"also-secret"}]}', '{"client_api_key":"[REDACTED]"}'),
    ('{"session_cookie":{"value":"cookie-secret"}}', '{"session_cookie":"[REDACTED]"}'),
    ('{"outer":{"aws_secret_access_key":"aws-secret"}}', '{"outer":{"aws_secret_access_key":"[REDACTED]"}}'),
])
def test_nested_structured_credential_values_are_redacted_completely(diagnostic, expected):
    redacted = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert redacted == expected


@pytest.mark.parametrize("key", [
    "apikey", "APIKey", "clientsecret", "clientSecret", "privatekey", "privateKey",
    "accesskeyid", "accessKeyID", "secretaccesskey", "AWSSecretAccessKey",
    "awsaccesskeyid", "AWSAccessKeyID", "awssecretaccesskey", "awsSecretAccessKey",
    "clientAPIKey", "AUTHToken", "authtoken", "userpassword", "sessioncookie",
    "backupAWSSecretAccessKey",
])
def test_compact_and_acronym_credential_keys_redact_the_whole_subtree(key):
    diagnostic = f'{{"outer":{{"{key}":{{"visible":"compact-secret"}}}},"safe":"ordinary"}}'

    assert subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512) == (
        f'{{"outer":{{"{key}":"[REDACTED]"}},"safe":"ordinary"}}'
    )


@pytest.mark.parametrize("key", ["clientAPIKEY", "clientApiKEY", "serviceAPIKEY"])
def test_acronym_compound_credential_keys_redact_the_whole_subtree(key):
    diagnostic = f'{{"{key}":{{"visible":"acronym-secret"}},"safe":"ordinary"}}'

    assert subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512) == (
        f'{{"{key}":"[REDACTED]","safe":"ordinary"}}'
    )


def test_client_api_key_acronym_is_redacted_end_to_end():
    key = "clientAPIKEY"
    secret = "client-api-key-secret-9f83"
    diagnostic = f'{{"{key}":{{"visible":"{secret}"}}}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.metadata["stderr"] == f'{{"{key}":"[REDACTED]"}}'
    assert secret not in result.model_dump_json()


@pytest.mark.parametrize("key", [
    "clientAPIKey", "AUTHToken", "authtoken", "userpassword", "sessioncookie",
    "backupAWSSecretAccessKey",
])
def test_boundary_model_credential_keys_are_redacted_end_to_end(key):
    secret = f"{key}-secret-9f83"
    diagnostic = f'{{"{key}":{{"visible":"{secret}"}}}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.metadata["stderr"] == f'{{"{key}":"[REDACTED]"}}'
    assert secret not in result.model_dump_json()


def test_malformed_structured_credential_assignment_fails_closed_end_to_end():
    secret = "malformed-object-secret-9f83"
    diagnostic = f'{{"credentials":{{"username":"alice","value":"{secret}"'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert secret not in result.model_dump_json()


def test_redacted_prefix_malformed_structured_credential_fails_closed_directly():
    secret = "SYNTHETIC-LEAK-9f83"
    diagnostic = f'[REDACTED],{{"credentials":{{"username":"alice","value":"{secret}"}}}}'

    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert sanitized == "[invalid structured diagnostic redacted]"
    assert secret not in sanitized


def test_redacted_prefix_malformed_structured_credential_fails_closed_end_to_end():
    secret = "SYNTHETIC-LEAK-9f83"
    diagnostic = f'[REDACTED],{{"credentials":{{"username":"alice","value":"{secret}"}}}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert secret not in result.model_dump_json()


def test_exact_redacted_marker_keeps_safe_textual_fallback_directly():
    assert subprocess_runner._sanitize_diagnostic(b"[REDACTED]", 512) == "[REDACTED]"


def test_exact_redacted_marker_is_unchanged_end_to_end():
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", "import sys; sys.stderr.write('[REDACTED]'); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[REDACTED]"


def test_malformed_structured_escaped_credential_key_fails_closed():
    diagnostic = r'{"client\u0053ecret":{"visible":"SECRET"'

    assert subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512) == (
        "[invalid structured diagnostic redacted]"
    )


def test_malformed_structured_escaped_credential_key_fails_closed_end_to_end():
    secret = "escaped-client-secret-9f83"
    diagnostic = rf'{{"client\u0053ecret":{{"visible":"{secret}"'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.metadata["stderr"] == "[invalid structured diagnostic redacted]"
    assert secret not in result.model_dump_json()


def test_malformed_structured_text_without_credential_label_keeps_bounded_fallback():
    diagnostic = '{"ordinary":"visible"'

    assert subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512) == diagnostic


@pytest.mark.parametrize("key", [
    "refresh_interval", "client_label", "noncredential", "noncredentials", "tokenizer",
    "cookiecutter", "passwordless",
])
def test_noncredential_structured_values_are_preserved(key):
    diagnostic = f'{{"profile":{{"{key}":"ordinary-value"}},"items":[1,true]}}'

    assert subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512) == diagnostic


def test_structured_url_key_is_sanitized_end_to_end():
    secret = "url-key-secret-9f83"
    diagnostic = f'{{"https://key-user:{secret}@example.test/path":"safe"}}'
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.metadata["stderr"] == '{"[URL REDACTED]":"safe"}'
    assert secret not in result.model_dump_json()


def test_structured_sanitized_key_collision_fails_closed():
    diagnostic = '{"https://first:secret@example.test":"one","https://second:secret@example.test":"two"}'

    assert subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512) == (
        "[invalid structured diagnostic redacted]"
    )


@pytest.mark.parametrize(("header", "secrets"), [
    ("Cookie: session=first-secret; refresh=second-secret", ("first-secret", "second-secret")),
    ("Set-Cookie: session=first-secret; refresh=second-secret", ("first-secret", "second-secret")),
])
def test_cookie_header_values_are_redacted_through_end_of_line(header, secrets):
    diagnostic = f"before\n{header}\nafter"
    redacted = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert redacted == f"before\n{header.split(':', 1)[0]}: [REDACTED]\nafter"
    for secret in secrets:
        assert secret not in redacted


def test_failed_child_result_error_message_is_sanitized_and_revalidated():
    result = run("failed_secret")
    assert result.state == "failed"
    assert result.error.code == "child_failed"
    assert "child-top-secret" not in result.error.message
    assert "[REDACTED]" in result.error.message


@pytest.mark.parametrize(("mode", "secret"), [
    ("nonzero_zero_slash_url", "zero-secret"),
    ("nonzero_one_slash_url", "one-secret"),
])
def test_browser_special_url_credentials_are_redacted_from_nonzero_diagnostics(mode, secret):
    result = run(mode)
    assert result.state == "failed"
    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "[URL REDACTED]"
    assert secret not in result.model_dump_json()


def test_convergence_diagnostics_are_contract_safe_and_secret_free():
    result = run("stderr_convergence_credentials")
    assert result.state == "failed"
    assert result.error.code == "executor_nonzero_exit"
    serialized = result.model_dump_json()
    for secret in (
        "standalone-url-secret", "url-user", "url-password", "aws-id-secret",
        "aws-secret-value", "private-key-value", "client-secret-value",
        "authorization-secret",
    ):
        assert secret not in serialized
    assert "http://[" not in serialized
    assert "[URL REDACTED]" in result.metadata["stderr"]


def test_stderr_limit_returns_no_partial_credential_bytes():
    result = run("stderr_limit_credential", stderr=64)
    assert result.state == "failed"
    assert result.error.code == "executor_stderr_limit"
    assert result.metadata["stderr"] == "[stderr exceeded configured byte limit]"
    assert "cut-user" not in result.model_dump_json()
    assert "cut-password" not in result.model_dump_json()


def test_pem_private_key_stderr_is_redacted_from_serialized_result():
    result = run("stderr_pem_private_key")
    serialized = result.model_dump_json()
    assert result.error.code == "executor_nonzero_exit"
    assert "pem-private-secret" not in serialized
    assert "BEGIN RSA PRIVATE KEY" not in serialized
    assert "[PRIVATE KEY REDACTED]" in result.metadata["stderr"]


def test_incomplete_pem_private_key_is_redacted_through_end_directly():
    secret = "incomplete-pem-private-secret"
    diagnostic = f"prefix\n-----BEGIN PRIVATE KEY-----\n{secret}"

    sanitized = subprocess_runner._sanitize_diagnostic(diagnostic.encode(), 512)

    assert sanitized == "prefix\n[PRIVATE KEY REDACTED]"
    assert secret not in sanitized


def test_incomplete_pem_private_key_is_redacted_through_end_end_to_end():
    secret = "incomplete-pem-private-secret"
    diagnostic = f"prefix\n-----BEGIN PRIVATE KEY-----\n{secret}"
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stderr.write({diagnostic!r}); sys.exit(1)"),
    )

    result = executor.execute(request())

    assert result.error.code == "executor_nonzero_exit"
    assert result.metadata["stderr"] == "prefix\n[PRIVATE KEY REDACTED]"
    assert secret not in result.model_dump_json()


def test_failed_child_arbitrary_diagnostic_is_sanitized_before_revalidation():
    result = run("failed_unsafe_diagnostic")
    assert result.state == "failed"
    assert result.error.code == "child_failed"
    assert "http://[" not in result.error.message
    assert "child-client-secret" not in result.error.message


def test_failed_child_diagnostic_metadata_and_error_code_are_sanitized_before_revalidation():
    result = run("failed_diagnostic_metadata")
    assert result.state == "failed"
    assert result.error.code == "executor_child_failed"
    serialized = result.model_dump_json()
    for secret in (
        "error-token-secret", "error-password-secret", "error-user", "error-url-secret",
        "top-user", "top-url-secret", "top-metadata-secret", "Unsafe Child Code!",
    ):
        assert secret not in serialized
    assert result.error.metadata["nested"] == (
        "token=[REDACTED]", {"detail": "password=[REDACTED]"},
    )
    assert result.error.metadata["url"] == "[URL REDACTED]"
    assert result.error.metadata["number"] == 17
    assert result.error.metadata["enabled"] is True
    assert result.metadata["nested"] == {"url": "[URL REDACTED]"}
    assert result.metadata["detail"] == "token=[REDACTED]"
    assert result.metadata["items"] == (None, 23, False)


def test_failed_child_credential_labelled_error_code_is_replaced():
    secret = "syntheticsecret9f83"
    payload = run("failed_diagnostic_metadata").model_dump(mode="json")
    payload["error"]["code"] = f"password_{secret}"
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stdout.write({json.dumps(payload)!r})"),
    )

    result = executor.execute(request())

    assert result.state == "failed"
    assert result.error.code == "executor_child_failed"
    assert secret not in result.model_dump_json()


def test_diagnostic_metadata_redacts_credential_labelled_subtree_directly():
    secret = "direct-client-api-key-secret"

    sanitized = subprocess_runner._sanitize_diagnostic_values({
        "clientAPIKey": {"visible": secret},
        "ordinary": {"detail": "safe"},
    })

    assert sanitized == {
        "[REDACTED]": "[REDACTED]",
        "ordinary": {"detail": "safe"},
    }


@pytest.mark.parametrize(("surface", "credential_key"), [
    ("error", "clientAPIKey"),
    ("top", "client_api_key"),
])
def test_failed_child_credential_metadata_subtree_is_redacted_end_to_end(surface, credential_key):
    secret = f"{surface}-client-api-key-secret"
    payload = run("failed_diagnostic_metadata").model_dump(mode="json")
    payload["error"]["code"] = "child_failed"
    payload["error"]["metadata"] = {"ordinary": "safe"}
    payload["metadata"] = {"ordinary": "safe"}
    target = payload["error"]["metadata"] if surface == "error" else payload["metadata"]
    target[credential_key] = {"visible": secret}
    executor = SubprocessAcquisitionExecutor(
        "web_http",
        (sys.executable, "-c", f"import sys; sys.stdout.write({json.dumps(payload)!r})"),
    )

    result = executor.execute(request())

    assert result.state == "failed"
    assert result.error.code == "child_failed"
    assert result.error.code != "executor_protocol_error"
    assert secret not in result.model_dump_json()
    sanitized_target = result.error.metadata if surface == "error" else result.metadata
    assert sanitized_target == {"ordinary": "safe", "[REDACTED]": "[REDACTED]"}


def test_failed_child_diagnostic_metadata_keys_are_sanitized():
    result = run("failed_diagnostic_metadata_key")
    serialized = result.model_dump_json()
    assert result.state == "failed"
    assert result.metadata == {"[URL REDACTED]": "safe"}
    assert "key-user" not in serialized
    assert "key-secret" not in serialized


def test_sanitized_diagnostic_metadata_key_collision_fails_closed():
    result = run("failed_diagnostic_metadata_key_collision")
    serialized = result.model_dump_json()
    assert result.error.code == "executor_protocol_error"
    for secret in ("first-user", "first-secret", "second-user", "second-secret"):
        assert secret not in serialized


def test_wrapper_exception_does_not_echo_secret_and_records_handler_duration(monkeypatch):
    secret = "handler-secret-value"
    output = io.BytesIO()
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(request().model_dump_json().encode())})())
    monkeypatch.setattr(sys, "stdout", type("Output", (), {"buffer": output})())

    def fail(_request):
        time.sleep(0.02)
        raise RuntimeError(secret)

    run_stdio_wrapper(fail)
    result = CaptureResult.model_validate_json(output.getvalue())
    assert secret not in output.getvalue().decode("utf-8")
    assert result.error.message == "executor handler failed"
    assert (result.finished_at - result.started_at).total_seconds() >= 0.02


def test_wrapper_emits_one_utf8_json_value_via_binary_stdout(monkeypatch):
    output = io.BytesIO()

    class NonUtf8TextStdout:
        buffer = output
        encoding = "ascii"

        def write(self, value):
            value.encode("ascii")
            raise AssertionError("wrapper must not write through text stdout")

        def flush(self):
            raise AssertionError("wrapper must flush binary stdout")

    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": io.BytesIO(request().model_dump_json().encode())})())
    monkeypatch.setattr(sys, "stdout", NonUtf8TextStdout())

    def fail(_request):
        raise RuntimeError("failure")

    assert run_stdio_wrapper(fail) == 0
    raw = output.getvalue()
    assert raw.decode("utf-8")
    decoder = json.JSONDecoder()
    value, end = decoder.raw_decode(raw.decode("utf-8"))
    assert end == len(raw.decode("utf-8"))
    assert value["error"]["message"] == "executor handler failed"
