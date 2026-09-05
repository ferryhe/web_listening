from __future__ import annotations

import hashlib
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

import tools.qualify_playwright_1_62 as qualification
from tools.qualify_playwright_1_62 import (
    CANONICAL_CONCLUSION_VALUES,
    _BrowserRuntimeTemp,
    _configure_isolated_browser_cache,
    _teardown_resources,
    canonical_json,
    main,
    validate_qualification_result,
)
from web_listening.blocks.crawler import BrowserCrawler
from web_listening.blocks.staged_workflow import _preflight_optional_browser_runtimes
from web_listening.executors.playwright_wrapper import BrowserAcquisitionAdapter

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = (
    REPO_ROOT
    / "docs/testing/fixtures/playwright-1.62.0-qualification.win32-x86_64.json"
)


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def _rehashed_fixture(mutator) -> dict[str, object]:
    payload = deepcopy(_fixture())
    mutator(payload)
    body = {key: value for key, value in payload.items() if key != "fixture_sha256"}
    payload["fixture_sha256"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()
    return payload


def test_committed_playwright_1_62_qualification_fixture_is_canonical_and_complete() -> (
    None
):
    raw = FIXTURE_PATH.read_text(encoding="utf-8")
    payload = _fixture()

    assert raw == canonical_json(payload) + "\n"
    assert payload["conclusion"] in CANONICAL_CONCLUSION_VALUES
    assert payload["conclusion"] == "adopt"
    assert payload["package"]["version"] == "1.62.0"
    assert payload["browser"] == {
        "build": "151.0.7922.34",
        "chrome_for_testing": {
            "executable_cache_path": "chromium-1234/chrome-win64/chrome.exe",
            "executable_sha256": "409805a16d6416087e6b2f778df1cf8f7bbb267d6b99f6b5bb0a618eace234f2",
        },
        "engine": "chromium",
        "headless_shell": {
            "executable_cache_path": "chromium_headless_shell-1234/chrome-headless-shell-win64/chrome-headless-shell.exe",
            "executable_sha256": "ce4635cd0e5dc0e21494542a701f347e91c1f1d821970578d97ed8df4ced50ef",
        },
        "playwright_revision": "1234",
    }
    assert payload["platform"] == {
        "architecture": "AMD64",
        "os": "Windows",
        "python_version": "3.12.14",
    }
    assert payload["cleanup"] == {
        "browser_connected_after_close": False,
        "browser_process_ids": [],
        "browser_runtime_temp_state_removed": True,
        "temporary_probe_removed": True,
    }
    assert (
        payload["fixture_sha256"]
        == hashlib.sha256(
            canonical_json(
                {
                    key: value
                    for key, value in payload.items()
                    if key != "fixture_sha256"
                }
            ).encode("utf-8")
        ).hexdigest()
    )
    assert validate_qualification_result(payload) == []


@pytest.mark.parametrize(
    "field",
    [
        "missing_binary",
        "corrupt_binary",
        "launch_failure",
        "navigation_timeout",
        "cancellation",
        "close_failure",
    ],
)
def test_committed_qualification_records_each_required_stable_failure_outcome(
    field: str,
) -> None:
    payload = _fixture()

    outcome = payload["checks"][field]
    assert outcome == {"code": field, "ok": False}


def test_qualification_validation_rejects_tampered_browser_identity() -> None:
    payload = _fixture()
    payload["browser"]["playwright_revision"] = "9999"

    assert "browser.playwright_revision" in validate_qualification_result(payload)


def test_qualification_validation_rejects_rehashed_headless_runtime_tampering() -> None:
    payload = _fixture()
    payload["browser"]["headless_shell"]["executable_sha256"] = "0" * 64
    body = {key: value for key, value in payload.items() if key != "fixture_sha256"}
    payload["fixture_sha256"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()

    assert "browser.headless_shell.executable_sha256" in validate_qualification_result(
        payload
    )


def test_qualification_validation_rejects_rehashed_wheel_identity_tampering() -> None:
    payload = _fixture()
    payload["package"]["wheel_sha256"] = "0" * 64
    body = {key: value for key, value in payload.items() if key != "fixture_sha256"}
    payload["fixture_sha256"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()

    assert "package.wheel_sha256" in validate_qualification_result(payload)


@pytest.mark.parametrize(
    ("mutator", "expected_error"),
    [
        (lambda payload: payload.__setitem__("latest", "1.62.1"), "latest"),
        (
            lambda payload: payload["package"].__setitem__("latest", "1.62.1"),
            "package.latest",
        ),
        (
            lambda payload: payload["browser"]["headless_shell"].__setitem__(
                "latest", "unexpected"
            ),
            "browser.headless_shell.latest",
        ),
        (
            lambda payload: payload["checks"].__setitem__("lifecycle", "passed"),
            "checks.lifecycle",
        ),
        (
            lambda payload: payload["checks"].__setitem__("cancellation", []),
            "checks.cancellation",
        ),
    ],
    ids=[
        "root-extra",
        "package-extra",
        "nested-extra",
        "lifecycle-string",
        "failure-list",
    ],
)
def test_qualification_validation_is_closed_and_total_for_rehashed_json_payloads(
    mutator, expected_error: str
) -> None:
    payload = _rehashed_fixture(mutator)

    first = validate_qualification_result(payload)

    assert expected_error in first
    assert first == validate_qualification_result(payload)


@pytest.mark.parametrize(
    ("payload", "expected_error"),
    [
        (None, "result"),
        ([], "result"),
        ({"checks": "not-an-object"}, "checks"),
        ({"fixture_sha256": []}, "fixture_sha256"),
    ],
    ids=["null", "list", "nested-wrong-type", "digest-wrong-type"],
)
def test_qualification_validation_returns_stable_errors_for_malformed_json_values(
    payload: object, expected_error: str
) -> None:
    first = validate_qualification_result(payload)

    assert expected_error in first
    assert first == validate_qualification_result(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failing_resource", ["page", "context", "browser", "playwright"]
)
async def test_teardown_continues_after_each_close_failure(
    failing_resource: str,
) -> None:
    events: list[str] = []

    class Resource:
        def __init__(self, name: str) -> None:
            self.name = name

        async def close(self) -> None:
            events.append(self.name)
            if self.name == failing_resource:
                raise RuntimeError(self.name)

    class Browser(Resource):
        def is_connected(self) -> bool:
            return False

    class Playwright:
        async def stop(self) -> None:
            events.append("playwright")
            if failing_resource == "playwright":
                raise RuntimeError("playwright")

    with _BrowserRuntimeTemp() as runtime_temp:
        assert runtime_temp.root is not None
        (runtime_temp.root / "playwright-profile").mkdir()
        outcome, browser_connected = await _teardown_resources(
            page=Resource("page"),
            context=Resource("context"),
            browser=Browser("browser"),
            playwright=Playwright(),
        )
        runtime_temp_state_removed = runtime_temp.remove_after_teardown()

    assert events == ["page", "context", "browser", "playwright"]
    assert outcome == {"code": "close_failure", "ok": False}
    assert browser_connected is False
    assert runtime_temp_state_removed is True


@pytest.mark.parametrize(
    "mutator",
    [
        lambda payload: payload["package"].__setitem__("wheel_sha256", "0" * 64),
        lambda payload: payload["package"]["resolved_dependencies"][0].__setitem__(
            "version", "0.0.0"
        ),
        lambda payload: payload["platform"].__setitem__("python_version", "3.12.13"),
        lambda payload: payload["package"].__setitem__("latest", "1.62.1"),
        lambda payload: payload["checks"].__setitem__("lifecycle", "passed"),
    ],
    ids=["wheel-digest", "dependency", "platform", "package-extra", "malformed-check"],
)
def test_generation_refuses_identity_drift_without_writing_adopt_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutator
) -> None:
    output = tmp_path / "result.json"
    monkeypatch.setattr(
        qualification, "qualify", lambda **_: _rehashed_fixture(mutator)
    )

    with pytest.raises(SystemExit) as exited:
        main(
            [
                "--wheel",
                "ignored.whl",
                "--browser-cache",
                str(tmp_path),
                "--write",
                str(output),
            ]
        )

    assert exited.value.code == 2
    assert not output.exists()


def test_generation_writes_exact_self_validated_adopt_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "result.json"
    payload = _fixture()
    monkeypatch.setattr(qualification, "qualify", lambda **_: payload)

    assert (
        main(
            [
                "--wheel",
                "ignored.whl",
                "--browser-cache",
                str(tmp_path),
                "--write",
                str(output),
            ]
        )
        == 0
    )
    assert output.read_text(encoding="utf-8") == canonical_json(payload) + "\n"


def test_validate_result_missing_file_is_a_traceback_free_argparse_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    output = tmp_path / "result.json"

    with pytest.raises(SystemExit) as exited:
        main(
            [
                "--validate-result",
                str(tmp_path / "missing.json"),
                "--write",
                str(output),
            ]
        )

    assert exited.value.code == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "error: unable to read qualification result" in captured.err
    assert "Traceback" not in captured.err


def test_validate_result_unreadable_file_is_a_traceback_free_argparse_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text("{}", encoding="utf-8")
    output = tmp_path / "output.json"

    def raise_os_error(*_: object, **__: object) -> str:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_text", raise_os_error)

    with pytest.raises(SystemExit) as exited:
        main(["--validate-result", str(result), "--write", str(output)])

    assert exited.value.code == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "error: unable to read qualification result" in captured.err
    assert "Traceback" not in captured.err


def test_validate_result_malformed_json_is_a_traceback_free_argparse_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text("{", encoding="utf-8")
    output = tmp_path / "output.json"

    with pytest.raises(SystemExit) as exited:
        main(["--validate-result", str(result), "--write", str(output)])

    assert exited.value.code == 2
    assert not output.exists()
    captured = capsys.readouterr()
    assert "error: qualification result is not valid JSON" in captured.err
    assert "Traceback" not in captured.err


def test_validate_result_invalid_utf8_is_a_traceback_free_argparse_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_bytes(b"\xff")
    output = tmp_path / "output.json"

    with pytest.raises(SystemExit) as exited:
        main(["--validate-result", str(result), "--write", str(output)])

    captured = capsys.readouterr()
    assert exited.value.code == 2
    assert not output.exists()
    assert "error: qualification result is not valid JSON" in captured.err
    assert "Traceback" not in captured.err


def test_validate_result_invalid_canonical_payload_is_an_argparse_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    result = tmp_path / "result.json"
    result.write_text(
        canonical_json(
            _rehashed_fixture(
                lambda payload: payload["package"].__setitem__("latest", "1.62.1")
            )
        ),
        encoding="utf-8",
    )
    output = tmp_path / "output.json"

    with pytest.raises(SystemExit) as exited:
        main(["--validate-result", str(result), "--write", str(output)])

    captured = capsys.readouterr()
    assert exited.value.code == 2
    assert not output.exists()
    assert "error: invalid qualification result: package.latest" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    "message",
    [
        "Playwright 1.62.0 is not installed in this interpreter",
        "wheel path does not exist",
        "isolated browser cache path does not exist",
        "explicit qualification preflight failed",
    ],
    ids=["missing-runtime", "missing-wheel", "missing-cache", "explicit-runtime-error"],
)
def test_generation_runtime_errors_are_traceback_free_argparse_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    message: str,
) -> None:
    output = tmp_path / "result.json"

    def raise_runtime_error(**_: object) -> dict[str, object]:
        raise RuntimeError(message)

    monkeypatch.setattr(qualification, "qualify", raise_runtime_error)

    with pytest.raises(SystemExit) as exited:
        main(
            [
                "--wheel",
                "ignored.whl",
                "--browser-cache",
                str(tmp_path),
                "--write",
                str(output),
            ]
        )

    captured = capsys.readouterr()
    assert exited.value.code == 2
    assert not output.exists()
    assert f"error: qualification failed: {message}" in captured.err
    assert "Traceback" not in captured.err


def test_qualification_binds_playwright_to_the_selected_isolated_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "C:/production-browser-cache")

    _configure_isolated_browser_cache(tmp_path)

    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]).resolve() == tmp_path.resolve()


def test_disabled_browser_entry_points_reject_before_any_target_navigation() -> None:
    with pytest.raises(RuntimeError, match="disabled"):
        BrowserCrawler().fetch_page("http://127.0.0.1:1/should-not-run")
    with pytest.raises(RuntimeError, match="disabled"):
        BrowserAcquisitionAdapter().capture("http://127.0.0.1:1/should-not-run")


def test_optional_browser_preflight_only_checks_the_installed_executable(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "chromium.exe"
    executable.write_bytes(b"qualification fixture")
    events: list[str] = []

    class FakeDriver:
        class chromium:
            executable_path = str(executable)

        def stop(self) -> None:
            events.append("stop")

    class FakeContext:
        def start(self) -> FakeDriver:
            events.append("start")
            return FakeDriver()

    class FakeSyncApi:
        @staticmethod
        def sync_playwright() -> FakeContext:
            return FakeContext()

    _preflight_optional_browser_runtimes(
        {"browser_rendered"}, importer=lambda _: FakeSyncApi()
    )

    assert events == ["start", "stop"]
