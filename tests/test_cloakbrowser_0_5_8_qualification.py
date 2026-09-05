from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

import tools.qualify_cloakbrowser_0_5_8 as qualification
from tools.qualify_cloakbrowser_0_5_8 import (
    PREVIEW_VERSION,
    QUALIFICATION_VERSION,
    STABLE_VERSION,
    canonical_json,
    validate_qualification_result,
)
from web_listening.blocks.crawler import BrowserCrawler
from web_listening.executors.cloakbrowser_wrapper import (
    CloakBrowserAcquisitionAdapter,
)

FIXTURE = (
    Path(__file__).parents[1]
    / "docs/testing/fixtures/cloakbrowser-0.5.8-qualification.win32-x86_64.json"
)
README = Path(__file__).parents[1] / "README.md"


def _fixture() -> dict[str, object]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _rehashed(mutator) -> dict[str, object]:
    payload = copy.deepcopy(_fixture())
    mutator(payload)
    body = {key: value for key, value in payload.items() if key != "fixture_sha256"}
    payload["fixture_sha256"] = hashlib.sha256(
        canonical_json(body).encode("utf-8")
    ).hexdigest()
    return payload


def test_committed_fixture_is_canonical_closed_and_deferred() -> None:
    raw = FIXTURE.read_bytes()
    payload = json.loads(raw)

    assert raw == (canonical_json(payload) + "\n").encode()
    assert validate_qualification_result(payload) == []
    assert payload["qualification_version"] == QUALIFICATION_VERSION
    assert payload["conclusion"] == "defer"
    assert payload["license"] == {
        "environment_key_present": False,
        "isolated_cache_key_file_present": False,
        "key_value_recorded": False,
        "keyed_channel_access": False,
        "state": "missing",
    }
    assert payload["qualification_scope"] == {
        "authorized_target": "loopback_only",
        "production_reader_enabled": False,
        "public_canary_run": False,
    }


def test_stable_and_preview_are_separate_exact_pins() -> None:
    channels = _fixture()["channels"]

    assert channels["stable"]["fixed_browser_version"] == STABLE_VERSION
    assert channels["preview"]["fixed_browser_version"] == PREVIEW_VERSION
    assert channels["stable"]["resolution_observation"] == {
        "fallback": False,
        "observed_on": "2026-09-05",
        "requested_channel": "stable",
        "resolved_channel": "stable",
        "source": "https://cloakbrowser.dev/api/download/version",
    }
    assert channels["preview"]["resolution_observation"] == {
        "fallback": False,
        "observed_on": "2026-09-05",
        "requested_channel": "preview",
        "resolved_channel": "preview",
        "source": "https://cloakbrowser.dev/api/download/version?channel=preview",
    }
    for name, version_pin in (("stable", STABLE_VERSION), ("preview", PREVIEW_VERSION)):
        binary = channels[name]["binary"]
        assert binary["executable_cache_path"] == (
            f"chromium-{version_pin}-pro/chrome.exe"
        )
        assert len(binary["archive_sha256"]) == 64
        assert binary["signature_verified"] is True
        assert binary["present"] is False
        assert binary["executable_sha256"] is None
        assert channels[name]["runtime"] == {
            "code": "license_unavailable",
            "passed": False,
        }


def test_platform_tag_is_derived_from_detected_os_and_architecture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(qualification, "PLATFORM_TAG", "tampered-constant")

    assert qualification._detected_platform_tag("Windows", "AMD64") == "windows-x64"
    assert qualification._detected_platform_tag("Windows", "x86_64") == "windows-x64"
    assert qualification._detected_platform_tag("Linux", "x86_64") == "unsupported"
    assert (
        qualification._detected_platform_tag("Windows", "AMD64")
        != qualification.PLATFORM_TAG
    )


def test_fixture_records_all_required_outcomes_without_inventing_runtime() -> None:
    payload = _fixture()
    checks = payload["checks"]

    for name in (
        "base_exception_teardown",
        "challenge_page",
        "close_failure",
        "corrupt_binary",
        "first_download_boundary",
        "free_plan_version_pin",
        "launch_failure",
        "missing_binary",
        "missing_license",
        "synthetic_invalid_license",
    ):
        assert checks[name]["passed"] is True
    assert checks["challenge_page"]["classified_as_content_success"] is False
    assert checks["missing_license"]["keyed_channels_qualified"] is False
    assert checks["synthetic_invalid_license"]["keyed_channels_qualified"] is False
    assert checks["free_plan_version_pin"]["requested_pin_forwarded"] is False
    assert checks["first_download_boundary"]["auto_download_attempted"] is False
    assert checks["navigation_timeout"]["passed"] is False
    assert checks["cancellation"]["passed"] is False
    assert checks["lifecycle"] == {
        "close": False,
        "code": "license_unavailable",
        "content_access": False,
        "final_url": False,
        "goto": False,
        "http_status": False,
        "launch": False,
        "new_page": False,
        "passed": False,
        "rendered_content": False,
    }
    assert payload["cleanup"] == {
        "browser_process_ids": [],
        "failure_probe_temp_removed": True,
        "runtime_temp_removed": True,
    }


@pytest.mark.parametrize(
    ("mutator", "expected_path"),
    [
        (lambda value: value.update({"extra": True}), "extra"),
        (lambda value: value["channels"].pop("preview"), "channels.preview"),
        (
            lambda value: value["channels"]["stable"].update(
                {"fixed_browser_version": PREVIEW_VERSION}
            ),
            "channels.stable.fixed_browser_version",
        ),
        (
            lambda value: value["channels"]["preview"]["binary"].update(
                {"executable_sha256": "0" * 64}
            ),
            "channels.preview.binary.executable_sha256",
        ),
        (lambda value: value.update({"conclusion": "adopt"}), "conclusion"),
        (
            lambda value: value["parameters"]["minimum_launch"].update(
                {"humanize": False}
            ),
            "parameters.minimum_launch.humanize",
        ),
    ],
)
def test_validator_rejects_rehashed_identity_or_schema_tampering(
    mutator, expected_path: str
) -> None:
    assert expected_path in validate_qualification_result(_rehashed(mutator))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"fixture_sha256": None},
        {"fixture_sha256": float("nan")},
    ],
)
def test_validator_is_total_for_malformed_json_values(payload: object) -> None:
    assert validate_qualification_result(payload)


def test_minimum_launch_kwargs_do_not_enable_boundary_only_capabilities() -> None:
    kwargs = qualification._minimum_launch_kwargs("stable", STABLE_VERSION)

    assert kwargs == {
        "browser_version": STABLE_VERSION,
        "headless": True,
        "humanize": True,
        "locale": "en-US",
        "release_channel": "stable",
        "timezone": "America/New_York",
    }
    assert not (
        set(kwargs)
        & {
            "extension_paths",
            "geoip",
            "proxy",
            "storage_state",
            "user_data_dir",
        }
    )


@pytest.mark.parametrize(
    ("status", "url", "content", "expected"),
    [
        (200, "http://127.0.0.1/content", "rendered content", "content_success"),
        (
            200,
            "http://127.0.0.1/challenge",
            "Please verify you are human to continue",
            "challenge_page",
        ),
        (403, "http://127.0.0.1/content", "substantive text", "challenge_page"),
        (
            200,
            "http://127.0.0.1/cdn-cgi/challenge-platform/h/g",
            "",
            "challenge_page",
        ),
    ],
)
def test_challenge_classification_precedes_content_success(
    status: int, url: str, content: str, expected: str
) -> None:
    assert (
        qualification._classify_page(status=status, final_url=url, content=content)
        == expected
    )


@pytest.mark.asyncio
async def test_teardown_continues_after_close_failure() -> None:
    close_failure, base_exception = await qualification._verify_teardown_probes()

    assert close_failure == {
        "code": "close_failure",
        "passed": True,
        "teardown_completed": True,
    }
    assert base_exception == {
        "code": "base_exception_preserved",
        "passed": True,
        "teardown_completed": True,
    }


def test_validate_result_cli_is_idempotent(capsys: pytest.CaptureFixture[str]) -> None:
    assert qualification.main(["--validate-result", str(FIXTURE)]) == 0
    first = capsys.readouterr().out
    assert qualification.main(["--validate-result", str(FIXTURE)]) == 0
    second = capsys.readouterr().out

    assert first == second == canonical_json(_fixture()) + "\n"


def test_validate_result_cli_owns_malformed_json(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")

    with pytest.raises(SystemExit) as caught:
        qualification.main(["--validate-result", str(malformed)])

    streams = capsys.readouterr()
    assert caught.value.code == 2
    assert streams.out == ""
    assert "qualification result is not valid JSON" in streams.err
    assert "Traceback" not in streams.err


def test_write_is_idempotent_and_refuses_different_bytes(tmp_path: Path) -> None:
    target = tmp_path / "result.json"
    rendered = canonical_json(_fixture()) + "\n"

    qualification._write_idempotent(target, rendered)
    qualification._write_idempotent(target, rendered)
    with pytest.raises(RuntimeError, match="different qualification result"):
        qualification._write_idempotent(target, "{}\n")
    assert target.read_text(encoding="utf-8") == rendered


def test_present_license_variable_is_refused_without_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    synthetic = "synthetic-never-valid"
    monkeypatch.setenv("CLOAKBROWSER_LICENSE_KEY", synthetic)

    with (
        pytest.raises(RuntimeError, match="refuses to read or replace"),
        qualification._temporary_environment(
            overrides={}, remove=("CLOAKBROWSER_LICENSE_KEY",)
        ),
    ):
        pytest.fail("license-bearing environment must not be entered")

    assert qualification.os.environ["CLOAKBROWSER_LICENSE_KEY"] == synthetic


def test_readme_future_paid_key_rollback_restores_prior_process_state() -> None:
    section = README.read_text(encoding="utf-8").split(
        "### CloakBrowser 0.5.8 qualification", 1
    )[1]

    assert (
        'throw "Use a fresh shell without CLOAKBROWSER_LICENSE_KEY; '
        'the qualification will not read it."' in section
    )
    capture = section.index(
        "For any future authorized paid-key run, capture the caller's "
        "process-level license-variable state"
    )
    rollback = section.index("Rollback is paired to wrapper, channel, and binary")
    assert capture < rollback
    assert "$hadCloakLicense = Test-Path Env:CLOAKBROWSER_LICENSE_KEY" in section
    assert "$previousCloakLicense = if ($hadCloakLicense)" in section
    assert "$env:CLOAKBROWSER_LICENSE_KEY = $previousCloakLicense" in section
    assert "} else {\n    Remove-Item Env:CLOAKBROWSER_LICENSE_KEY" in section


@pytest.mark.skipif(
    importlib.util.find_spec("cloakbrowser") is None,
    reason="real wrapper routing probe requires isolated cloakbrowser==0.5.8",
)
def test_real_wrapper_license_routing_probe_uses_no_key_value(tmp_path: Path) -> None:
    result = qualification._probe_license_routing(tmp_path)

    assert result == {
        key: qualification._expected_body()["checks"][key]
        for key in (
            "free_plan_version_pin",
            "missing_license",
            "synthetic_invalid_license",
        )
    }


def test_disabled_production_entry_points_still_reject_before_navigation() -> None:
    with pytest.raises(RuntimeError, match="target reads are disabled"):
        CloakBrowserAcquisitionAdapter().capture("http://127.0.0.1:1/should-not-run")
    with pytest.raises(RuntimeError, match="target reads are disabled"):
        BrowserCrawler().fetch_page("http://127.0.0.1:1/should-not-run")
