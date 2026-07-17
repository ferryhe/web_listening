from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from web_listening.contracts import CaptureRequest
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


def test_governed_words_in_ordinary_values_are_not_rejected():
    safe = request().model_copy(update={"metadata": {"description": "download report data"}})
    assert SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success")).execute(safe).state == "succeeded"


@pytest.mark.parametrize("key", ["report", "data", "output", "description", "source_url"])
def test_ordinary_keys_are_not_rejected(key):
    safe = request().model_copy(update={"config": {"nested": {key: "ordinary value"}}})
    assert SubprocessAcquisitionExecutor("web_http", (sys.executable, str(FAKE), "success")).execute(safe).state == "succeeded"


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

    assert redacted == f'{{"{key}": [REDACTED]}}'
    assert secret not in redacted


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


def test_wrapper_exception_does_not_echo_secret(monkeypatch, capsys):
    secret = "handler-secret-value"
    monkeypatch.setattr(sys, "stdin", type("Input", (), {"buffer": __import__("io").BytesIO(request().model_dump_json().encode())})())

    def fail(_request):
        raise RuntimeError(secret)

    run_stdio_wrapper(fail)
    output = capsys.readouterr().out
    assert secret not in output
    assert "executor handler failed" in output
