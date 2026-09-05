"""Produce the isolated CloakBrowser 0.5.8 Windows qualification evidence.

The committed evidence is intentionally a ``defer`` result.  This command never
downloads a browser, reads a license value, or navigates a public URL.  It checks
the exact wheel/dependency identity, replays the signed Stable/Preview manifest
snapshots, exercises local failure and cleanup behavior, and records why a real
keyed lifecycle could not be run.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import platform
import secrets
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any
from unittest.mock import patch

QUALIFICATION_VERSION = "cloakbrowser-0.5.8-qualification.v1"
OBSERVED_ON = "2026-09-05"
PLATFORM_TAG = "windows-x64"
FREE_BINARY_VERSION = "146.0.7680.177.5"
STABLE_VERSION = "151.0.7922.108.3"
PREVIEW_VERSION = "151.0.7922.108.6"
CONCLUSION_REASON = (
    "deferred on Windows AMD64/Python 3.12.14: no task-scoped paid license or "
    "keyed Stable/Preview executables were available for the required real lifecycle"
)

EXPECTED_PLATFORM = {
    "architecture": "AMD64",
    "os": "Windows",
    "platform_tag": PLATFORM_TAG,
    "python_version": "3.12.14",
}
EXPECTED_PACKAGE = {
    "resolved_dependencies": [
        {"name": "anyio", "version": "4.15.0"},
        {"name": "certifi", "version": "2026.7.22"},
        {"name": "cffi", "version": "2.1.1"},
        {"name": "cryptography", "version": "50.0.1"},
        {"name": "greenlet", "version": "3.5.5"},
        {"name": "h11", "version": "0.16.0"},
        {"name": "httpcore", "version": "1.0.9"},
        {"name": "httpx", "version": "0.28.1"},
        {"name": "idna", "version": "3.19"},
        {"name": "playwright", "version": "1.62.0"},
        {"name": "pycparser", "version": "3.0"},
        {"name": "pyee", "version": "13.0.1"},
        {"name": "typing-extensions", "version": "4.16.0"},
    ],
    "version": "0.5.8",
    "wheel_filename": "cloakbrowser-0.5.8-py3-none-any.whl",
    "wheel_sha256": "408e360962298757ef5cce4b0dcda91cee0da8387c9e2b92aab33504eaa4dce6",
}

# These exact bytes were fetched from the vendor's version-specific Pro release
# paths on OBSERVED_ON.  CloakBrowser 0.5.8 verifies the detached signatures
# against its embedded Ed25519 key before trusting the archive hashes.
_SIGNED_MANIFESTS = {
    "stable": {
        "archive_sha256": "5e03b7abab14d44f2f55368a888378ddca9eefadb08c08b6c28610aec580ab3a",
        "browser_version": STABLE_VERSION,
        "manifest": (
            "version=151.0.7922.108.3\n"
            "9ae73bd99ffc8428b9d6119ef1da413b598f6d875fa059ad989bd7a04cdd4ffe  cloakbrowser-linux-x64.tar.gz\n"
            "b1693d4407450beae178ac41366243ee235e9342a550282950f6cb4f767dca28  cloakbrowser-linux-arm64.tar.gz\n"
            "5e03b7abab14d44f2f55368a888378ddca9eefadb08c08b6c28610aec580ab3a  cloakbrowser-windows-x64.zip\n"
            "1b7f4e3926b7bcbae2469b98019cd4c0127f29ffe0291acf6467610e7933e83e  cloakbrowser-darwin-arm64.tar.gz\n"
            "987ddc2a5dc7fb8973544eae0e60a54bf1ab3a26c0ff3fadb02729cf0aa88155  cloakbrowser-darwin-x64.tar.gz\n"
        ),
        "manifest_sha256": "90c510c21b11b3fd9062b2430932931fc6eadd7292e2b8f34b69da1e11432a74",
        "signature": "oPAVwJBSsuM2dqrmM/DG4zecnfDkb/1repGyD8XcoOzu3P7CrOiYM8eBoJC+FY5bztNJpvsMrZLW7wdzgXh4Cw==",
        "signature_sha256": "b91286af836f586f5216949e88de71c24d4053474c9f174b9b0a179d71922a13",
    },
    "preview": {
        "archive_sha256": "5bd3951f470fff79998a437a139e3e8d68478bc2663402aca1666faf4eadac37",
        "browser_version": PREVIEW_VERSION,
        "manifest": (
            "version=151.0.7922.108.6\n"
            "2ce0a4feb8d4bda752e1e916d62d731205a462a6209a20405e280e4297b3a024  cloakbrowser-linux-x64.tar.gz\n"
            "8cfc19bce6a26b35454897deee04140a57917b681c29d1a07a798625d3bbbbcf  cloakbrowser-linux-arm64.tar.gz\n"
            "5bd3951f470fff79998a437a139e3e8d68478bc2663402aca1666faf4eadac37  cloakbrowser-windows-x64.zip\n"
        ),
        "manifest_sha256": "7e2f7c4a7703b7ead52ce66b9f2d79e9af4f35f05afd1cf4f7e668281b8f09ac",
        "signature": "sxW0EINn7547Ow2a1PN28igxeB2d09pzGGeoXmehY40XwbXbgdw3XSKN7g4ouf4XdF1U/SErEglFw1U1V5d0AQ==",
        "signature_sha256": "c4b3e518e2a178d30f413840a8646ccabf5e379f9fd4dcde8acff467fc763a84",
    },
}


def canonical_json(value: object) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _detected_platform_tag(os_name: str, architecture: str) -> str:
    if os_name == "Windows" and architecture in {"AMD64", "x86_64"}:
        return "windows-x64"
    return "unsupported"


def _passed(code: str, **details: object) -> dict[str, object]:
    return {"code": code, "passed": True, **details}


def _not_run(code: str = "license_unavailable") -> dict[str, object]:
    return {"code": code, "passed": False}


def _channel_body(channel: str, version_pin: str) -> dict[str, object]:
    snapshot = _SIGNED_MANIFESTS[channel]
    source = "https://cloakbrowser.dev/api/download/version"
    if channel == "preview":
        source += "?channel=preview"
    return {
        "binary": {
            "archive_filename": "cloakbrowser-windows-x64.zip",
            "archive_sha256": snapshot["archive_sha256"],
            "executable_cache_path": f"chromium-{version_pin}-pro/chrome.exe",
            "executable_sha256": None,
            "present": False,
            "signed_manifest_sha256": snapshot["manifest_sha256"],
            "signature_sha256": snapshot["signature_sha256"],
            "signature_verified": True,
        },
        "fixed_browser_version": version_pin,
        "resolution_observation": {
            "fallback": False,
            "observed_on": OBSERVED_ON,
            "requested_channel": channel,
            "resolved_channel": channel,
            "source": source,
        },
        "runtime": _not_run(),
    }


def _expected_body() -> dict[str, object]:
    return {
        "blockers": [
            "no task-scoped paid license; CloakBrowser 0.5.8 free-plan routing drops an exact browser_version pin",
            "keyed Stable and Preview executables unavailable; executable SHA-256 and real lifecycle evidence are not obtainable",
        ],
        "channels": {
            "preview": _channel_body("preview", PREVIEW_VERSION),
            "stable": _channel_body("stable", STABLE_VERSION),
        },
        "checks": {
            "base_exception_teardown": _passed(
                "base_exception_preserved", teardown_completed=True
            ),
            "cancellation": _not_run(),
            "challenge_page": _passed(
                "challenge_page", classified_as_content_success=False
            ),
            "close_failure": _passed("close_failure", teardown_completed=True),
            "corrupt_binary": _passed("corrupt_binary"),
            "first_download_boundary": _passed(
                "preinstalled_binary_required", auto_download_attempted=False
            ),
            "free_plan_version_pin": _passed(
                "free_plan_drops_version_pin", requested_pin_forwarded=False
            ),
            "launch_failure": _passed("launch_failure"),
            "lifecycle": {
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
            },
            "missing_binary": _passed("missing_binary"),
            "missing_license": _passed(
                "missing_license_uses_unlicensed_free_path",
                free_binary_version=FREE_BINARY_VERSION,
                keyed_channels_qualified=False,
            ),
            "navigation_timeout": _not_run(),
            "synthetic_invalid_license": _passed(
                "synthetic_invalid_uses_unlicensed_path",
                keyed_channels_qualified=False,
                unlicensed_executable_cache_path=(
                    f"chromium-{STABLE_VERSION}/chrome.exe"
                ),
            ),
        },
        "cleanup": {
            "browser_process_ids": [],
            "failure_probe_temp_removed": True,
            "runtime_temp_removed": True,
        },
        "conclusion": "defer",
        "conclusion_reason": CONCLUSION_REASON,
        "license": {
            "environment_key_present": False,
            "isolated_cache_key_file_present": False,
            "key_value_recorded": False,
            "keyed_channel_access": False,
            "state": "missing",
        },
        "package": EXPECTED_PACKAGE,
        "parameters": {
            "boundary_only": [
                "captcha_bypass",
                "extension_paths",
                "frame_access",
                "geoip",
                "login",
                "proxy",
                "session_persistence",
            ],
            "minimum_launch": {
                "headless": True,
                "humanize": True,
                "locale": "en-US",
                "timezone": "America/New_York",
            },
            "real_lifecycle_exercised": False,
        },
        "platform": EXPECTED_PLATFORM,
        "qualification_scope": {
            "authorized_target": "loopback_only",
            "production_reader_enabled": False,
            "public_canary_run": False,
        },
        "qualification_version": QUALIFICATION_VERSION,
    }


def _path(parent: str, key: object) -> str:
    return f"{parent}.{key}" if parent else str(key)


def _closed_errors(value: object, expected: object, parent: str = "") -> list[str]:
    if isinstance(expected, dict):
        if not isinstance(value, dict):
            return [parent or "result"]
        errors: list[str] = []
        expected_keys = set(expected)
        actual_keys = set(value)
        for key in sorted(expected_keys ^ actual_keys, key=str):
            errors.append(_path(parent, key))
        for key in sorted(expected_keys & actual_keys, key=str):
            errors.extend(_closed_errors(value[key], expected[key], _path(parent, key)))
        return errors
    if isinstance(expected, list):
        if not isinstance(value, list) or len(value) != len(expected):
            return [parent]
        errors: list[str] = []
        for index, (actual, wanted) in enumerate(zip(value, expected)):
            errors.extend(_closed_errors(actual, wanted, _path(parent, index)))
        return errors
    if type(value) is not type(expected) or value != expected:
        return [parent]
    return []


def validate_qualification_result(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return ["result"]
    expected = _expected_body()
    expected_keys = set(expected) | {"fixture_sha256"}
    actual_keys = set(payload)
    errors = [str(key) for key in sorted(expected_keys ^ actual_keys, key=str)]
    for key in sorted(set(expected) & actual_keys):
        errors.extend(_closed_errors(payload[key], expected[key], key))
    declared = payload.get("fixture_sha256")
    if "fixture_sha256" in payload and not isinstance(declared, str):
        errors.append("fixture_sha256")
    if errors:
        return sorted(set(errors))
    body = {key: value for key, value in payload.items() if key != "fixture_sha256"}
    try:
        actual_digest = _digest_bytes(canonical_json(body).encode("utf-8"))
    except (TypeError, ValueError):
        return ["fixture_sha256"]
    if declared != actual_digest:
        errors.append("fixture_sha256")
    return errors


def _minimum_launch_kwargs(channel: str, browser_version: str) -> dict[str, object]:
    return {
        "browser_version": browser_version,
        "headless": True,
        "humanize": True,
        "locale": "en-US",
        "release_channel": channel,
        "timezone": "America/New_York",
    }


def _classify_page(*, status: int, final_url: str, content: str) -> str:
    lowered = content.casefold()
    path = final_url.casefold()
    challenge_markers = (
        "verify you are human",
        "captcha",
        "cf-chl-",
        "sorry, you have been blocked",
    )
    if status in {401, 403, 429} or any(
        marker in lowered for marker in challenge_markers
    ):
        return "challenge_page"
    if "/captcha" in path or "/cdn-cgi/challenge-platform/" in path:
        return "challenge_page"
    return (
        "content_success"
        if 200 <= status < 300 and content.strip()
        else "content_failure"
    )


async def _teardown_resources(
    *,
    page: Any | None,
    context: Any | None,
    browser: Any | None,
    driver: Any | None,
    primary: BaseException | None = None,
) -> tuple[dict[str, object] | None, bool | None]:
    """Finish teardown, then preserve a primary cancellation/BaseException."""
    preserved = primary
    ordinary_close_failure = False
    browser_connected: bool | None = None
    for resource, method in (
        (page, "close"),
        (context, "close"),
        (browser, "close"),
        (driver, "stop"),
    ):
        if resource is None:
            continue
        try:
            await getattr(resource, method)()
        except BaseException as exc:  # noqa: BLE001 - teardown must preserve cancellation.
            if isinstance(exc, Exception):
                ordinary_close_failure = True
            elif preserved is None:
                preserved = exc
        if resource is browser:
            try:
                browser_connected = browser.is_connected()
            except Exception:  # noqa: BLE001 - an inspection error cannot stop teardown.
                ordinary_close_failure = True
    if preserved is not None:
        raise preserved
    outcome = (
        _passed("close_failure", teardown_completed=True)
        if ordinary_close_failure
        else None
    )
    return outcome, browser_connected


async def _verify_teardown_probes() -> tuple[dict[str, object], dict[str, object]]:
    class Resource:
        def __init__(self, name: str, failure: BaseException | None = None) -> None:
            self.name = name
            self.failure = failure

        async def close(self) -> None:
            events.append(self.name)
            if self.failure is not None:
                raise self.failure

    class Browser(Resource):
        def is_connected(self) -> bool:
            return False

    class Driver:
        async def stop(self) -> None:
            events.append("driver")

    events: list[str] = []
    close_outcome, connected = await _teardown_resources(
        page=Resource("page", RuntimeError("intentional close failure")),
        context=Resource("context"),
        browser=Browser("browser"),
        driver=Driver(),
    )
    if (
        close_outcome != _passed("close_failure", teardown_completed=True)
        or connected is not False
        or events != ["page", "context", "browser", "driver"]
    ):
        raise RuntimeError("ordinary close failure did not finish teardown")

    for failure in (asyncio.CancelledError(), KeyboardInterrupt()):
        events.clear()
        try:
            await _teardown_resources(
                page=Resource("page", failure),
                context=Resource("context"),
                browser=Browser("browser"),
                driver=Driver(),
            )
        except BaseException as caught:
            if caught is not failure:
                raise RuntimeError(
                    "teardown replaced the original BaseException"
                ) from caught
        else:
            raise RuntimeError("teardown swallowed a BaseException")
        if events != ["page", "context", "browser", "driver"]:
            raise RuntimeError("BaseException teardown stopped early")

    primary = asyncio.CancelledError()
    events.clear()
    try:
        await _teardown_resources(
            page=Resource("page", RuntimeError("secondary close failure")),
            context=Resource("context"),
            browser=Browser("browser"),
            driver=Driver(),
            primary=primary,
        )
    except BaseException as caught:
        if caught is not primary:
            raise RuntimeError("teardown replaced primary cancellation") from caught
    else:
        raise RuntimeError("teardown swallowed primary cancellation")
    if events != ["page", "context", "browser", "driver"]:
        raise RuntimeError("primary cancellation teardown stopped early")
    return close_outcome, _passed("base_exception_preserved", teardown_completed=True)


@contextmanager
def _temporary_environment(
    *, overrides: dict[str, str], remove: tuple[str, ...] = ()
) -> Iterator[None]:
    keys = set(overrides) | set(remove)
    if "CLOAKBROWSER_LICENSE_KEY" in keys and "CLOAKBROWSER_LICENSE_KEY" in os.environ:
        raise RuntimeError("qualification refuses to read or replace a license key")
    previous = {key: os.environ[key] for key in keys if key in os.environ}
    try:
        for key in remove:
            os.environ.pop(key, None)
        os.environ.update(overrides)
        yield
    finally:
        for key in keys:
            if key in previous:
                os.environ[key] = previous[key]
            else:
                os.environ.pop(key, None)


class _ProbeStop(BaseException):
    pass


def _probe_license_routing(browser_cache: Path) -> dict[str, dict[str, object]]:
    from cloakbrowser import download
    from cloakbrowser import license as license_module
    from cloakbrowser.license import LicenseInfo

    captures: dict[str, object] = {}

    def stop_missing(version_pin: str | None = None) -> None:
        captures["missing_version"] = version_pin
        raise _ProbeStop

    def stop_invalid(version_pin: str | None = None) -> None:
        captures["invalid_version"] = version_pin
        raise _ProbeStop

    def stop_free(
        _key: str,
        requested_version: str | None = None,
        plan: str | None = None,
        release_channel: str | None = None,
    ) -> None:
        captures["free_requested_version"] = requested_version
        captures["free_plan"] = plan
        captures["free_channel"] = release_channel
        raise _ProbeStop

    env = {"CLOAKBROWSER_CACHE_DIR": str(browser_cache.resolve())}
    removed = (
        "CLOAKBROWSER_AUTO_UPDATE",
        "CLOAKBROWSER_BINARY_PATH",
        "CLOAKBROWSER_DOWNLOAD_URL",
        "CLOAKBROWSER_LICENSE_KEY",
        "CLOAKBROWSER_RELEASE_CHANNEL",
        "CLOAKBROWSER_VERSION",
    )
    with _temporary_environment(overrides=env, remove=removed):
        captures["free_binary_version"] = download.get_chromium_version()
        with patch.object(download, "_download_and_extract", stop_missing):
            try:
                download.ensure_binary()
            except _ProbeStop:
                pass
        synthetic = "cb_" + secrets.token_hex(24)
        with (
            patch.object(
                license_module,
                "validate_license",
                return_value=LicenseInfo(False, "unknown", None),
            ),
            patch.object(download, "_download_and_extract", stop_invalid),
            patch.object(download.logger, "warning"),
        ):
            try:
                download.ensure_binary(
                    license_key=synthetic,
                    browser_version=STABLE_VERSION,
                    release_channel="stable",
                )
            except _ProbeStop:
                pass
        synthetic = "cb_" + secrets.token_hex(24)
        with (
            patch.object(
                license_module,
                "validate_license",
                return_value=LicenseInfo(True, "free", None),
            ),
            patch.object(download, "_ensure_pro_binary", stop_free),
        ):
            try:
                download.ensure_binary(
                    license_key=synthetic,
                    browser_version=STABLE_VERSION,
                    release_channel="stable",
                )
            except _ProbeStop:
                pass

    if captures != {
        "free_channel": "stable",
        "free_binary_version": FREE_BINARY_VERSION,
        "free_plan": "free",
        "free_requested_version": None,
        "invalid_version": STABLE_VERSION,
        "missing_version": None,
    }:
        raise RuntimeError(
            "CloakBrowser 0.5.8 license routing no longer matches evidence"
        )
    return {
        "free_plan_version_pin": _passed(
            "free_plan_drops_version_pin", requested_pin_forwarded=False
        ),
        "missing_license": _passed(
            "missing_license_uses_unlicensed_free_path",
            free_binary_version=FREE_BINARY_VERSION,
            keyed_channels_qualified=False,
        ),
        "synthetic_invalid_license": _passed(
            "synthetic_invalid_uses_unlicensed_path",
            keyed_channels_qualified=False,
            unlicensed_executable_cache_path=f"chromium-{STABLE_VERSION}/chrome.exe",
        ),
    }


async def _expected_launch_failure(path: Path) -> bool:
    try:
        from cloakbrowser import launch_async
        from playwright.async_api import Error as PlaywrightError
    except ImportError as exc:
        raise RuntimeError("cloakbrowser==0.5.8 is not installed") from exc
    with _temporary_environment(
        overrides={"CLOAKBROWSER_BINARY_PATH": str(path)},
        remove=("CLOAKBROWSER_LICENSE_KEY",),
    ):
        try:
            browser = await launch_async(
                **_minimum_launch_kwargs("stable", STABLE_VERSION)
            )
        except (FileNotFoundError, OSError, RuntimeError, PlaywrightError):
            return True
    await browser.close()
    return False


async def _verify_failure_probes(
    browser_cache: Path,
) -> tuple[dict[str, dict[str, object]], dict[str, object]]:
    parent = browser_cache.resolve().parent
    runtime_root = Path(
        tempfile.mkdtemp(prefix="web-listening-cb058-runtime-", dir=parent)
    )
    previous_temp = {key: os.environ.get(key) for key in ("TEMP", "TMP", "TMPDIR")}
    previous_set = {key: key in os.environ for key in previous_temp}
    try:
        for key in previous_temp:
            os.environ[key] = str(runtime_root)
        corrupt = runtime_root / "corrupt-browser.exe"
        corrupt.write_bytes(b"not a Chromium executable")
        directory = runtime_root / "browser-directory"
        directory.mkdir()
        outcomes = {
            "corrupt_binary": await _expected_launch_failure(corrupt),
            "launch_failure": await _expected_launch_failure(directory),
            "missing_binary": await _expected_launch_failure(
                runtime_root / "missing-browser.exe"
            ),
        }
        if not all(outcomes.values()):
            raise RuntimeError(
                "an expected local wrapper launch failure unexpectedly launched"
            )
    finally:
        for key, value in previous_temp.items():
            if previous_set[key]:
                os.environ[key] = value or ""
            else:
                os.environ.pop(key, None)
        shutil.rmtree(runtime_root)
    checks = {name: _passed(name) for name in sorted(outcomes)}
    return checks, {
        "failure_probe_temp_removed": not runtime_root.exists(),
        "runtime_temp_removed": not runtime_root.exists(),
    }


def _browser_process_ids(browser_cache: Path) -> list[int]:
    if os.name != "nt":
        return []
    escaped = str(browser_cache.resolve()).replace("'", "''")
    command = (
        "$path='" + escaped + "';@(Get-CimInstance Win32_Process | Where-Object { "
        "$_.ExecutablePath -like ($path + '*') -and $_.Name -like 'chrome*' } | "
        "ForEach-Object { $_.ProcessId }) -join ','"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        check=True,
        capture_output=True,
        text=True,
    )
    return [int(value) for value in completed.stdout.strip().split(",") if value]


def _verify_signed_manifests() -> None:
    try:
        from cloakbrowser.download import (
            _parse_checksums,
            _parse_manifest_version,
            _verify_signature,
        )
    except ImportError as exc:
        raise RuntimeError("cloakbrowser==0.5.8 is not installed") from exc
    for snapshot in _SIGNED_MANIFESTS.values():
        manifest = str(snapshot["manifest"]).encode("utf-8")
        signature = str(snapshot["signature"]).encode("ascii")
        if _digest_bytes(manifest) != snapshot["manifest_sha256"]:
            raise RuntimeError("signed manifest snapshot digest mismatch")
        if _digest_bytes(signature) != snapshot["signature_sha256"]:
            raise RuntimeError("signed manifest signature digest mismatch")
        _verify_signature(manifest, signature)
        if (
            _parse_manifest_version(manifest.decode("utf-8"))
            != snapshot["browser_version"]
        ):
            raise RuntimeError("signed manifest version mismatch")
        checksums = _parse_checksums(manifest.decode("utf-8"))
        if checksums.get("cloakbrowser-windows-x64.zip") != snapshot["archive_sha256"]:
            raise RuntimeError("signed Windows archive digest mismatch")


def _installed_package() -> dict[str, object]:
    try:
        dependencies = [
            {"name": item["name"], "version": version(str(item["name"]))}
            for item in EXPECTED_PACKAGE["resolved_dependencies"]
        ]
        installed = version("cloakbrowser")
    except PackageNotFoundError as exc:
        raise RuntimeError(
            "cloakbrowser==0.5.8 and its resolved dependencies are required"
        ) from exc
    return {
        "resolved_dependencies": dependencies,
        "version": installed,
    }


def qualify(*, wheel: Path, browser_cache: Path) -> dict[str, object]:
    if not wheel.is_file():
        raise RuntimeError("wheel path does not exist")
    if not browser_cache.is_dir():
        raise RuntimeError("isolated browser cache path does not exist")
    default_cache = (Path.home() / ".cloakbrowser").resolve()
    if browser_cache.resolve() == default_cache:
        raise RuntimeError("production CloakBrowser cache is forbidden")
    if "CLOAKBROWSER_LICENSE_KEY" in os.environ:
        raise RuntimeError(
            "committed defer evidence must run without a license environment key"
        )
    if (browser_cache / "license.key").exists():
        raise RuntimeError(
            "committed defer evidence must use a cache without a license file"
        )
    if (
        wheel.name != EXPECTED_PACKAGE["wheel_filename"]
        or _sha256(wheel) != EXPECTED_PACKAGE["wheel_sha256"]
    ):
        raise RuntimeError("wheel identity does not match cloakbrowser==0.5.8")
    installed = _installed_package()
    if installed != {
        "resolved_dependencies": EXPECTED_PACKAGE["resolved_dependencies"],
        "version": EXPECTED_PACKAGE["version"],
    }:
        raise RuntimeError(
            "resolved qualification environment does not match the pinned dependency set"
        )
    os_name = platform.system()
    architecture = platform.machine()
    actual_platform = {
        "architecture": architecture,
        "os": os_name,
        "platform_tag": _detected_platform_tag(os_name, architecture),
        "python_version": platform.python_version(),
    }
    if actual_platform != EXPECTED_PLATFORM:
        raise RuntimeError(
            "qualification must run on Windows AMD64 with Python 3.12.14"
        )
    for channel, pin in (("stable", STABLE_VERSION), ("preview", PREVIEW_VERSION)):
        expected = browser_cache / f"chromium-{pin}-pro" / "chrome.exe"
        if expected.exists():
            raise RuntimeError(
                f"clean defer cache unexpectedly contains the keyed {channel} binary"
            )

    _verify_signed_manifests()
    routing = _probe_license_routing(browser_cache)

    async def run_probes() -> tuple[
        dict[str, dict[str, object]],
        dict[str, object],
        dict[str, object],
        dict[str, object],
    ]:
        failures, cleanup = await _verify_failure_probes(browser_cache)
        close_failure, base_exception = await _verify_teardown_probes()
        return failures, cleanup, close_failure, base_exception

    failures, cleanup, close_failure, base_exception = asyncio.run(run_probes())
    if (
        _classify_page(
            status=200,
            final_url="http://127.0.0.1/challenge",
            content="Please verify you are human to continue",
        )
        != "challenge_page"
        or _classify_page(
            status=200,
            final_url="http://127.0.0.1/content",
            content="rendered local qualification content",
        )
        != "content_success"
    ):
        raise RuntimeError("challenge-page classification is not fail closed")

    result = _expected_body()
    result["checks"].update(routing)
    result["checks"].update(failures)
    result["checks"]["close_failure"] = close_failure
    result["checks"]["base_exception_teardown"] = base_exception
    cleanup["browser_process_ids"] = _browser_process_ids(browser_cache)
    if cleanup["browser_process_ids"]:
        raise RuntimeError("isolated CloakBrowser process remained after probes")
    result["cleanup"] = cleanup
    result["fixture_sha256"] = _digest_bytes(canonical_json(result).encode("utf-8"))
    return result


def _write_idempotent(path: Path, rendered: str) -> None:
    try:
        if path.exists():
            if path.read_text(encoding="utf-8") != rendered:
                raise RuntimeError(
                    "refusing to replace a different qualification result"
                )
            return
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(rendered)
    except OSError as exc:
        raise RuntimeError("unable to write qualification result") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--browser-cache", type=Path)
    parser.add_argument("--write", type=Path)
    parser.add_argument("--validate-result", type=Path)
    args = parser.parse_args(argv)
    if args.validate_result is not None:
        try:
            payload = json.loads(args.validate_result.read_text(encoding="utf-8"))
        except OSError:
            parser.error("unable to read qualification result")
        except (json.JSONDecodeError, UnicodeDecodeError):
            parser.error("qualification result is not valid JSON")
        errors = validate_qualification_result(payload)
        if errors:
            parser.error("invalid qualification result: " + ", ".join(errors))
        print(canonical_json(payload))
        return 0
    if args.wheel is None or args.browser_cache is None:
        parser.error("--wheel and --browser-cache are required to run qualification")
    try:
        result = qualify(wheel=args.wheel, browser_cache=args.browser_cache)
        errors = validate_qualification_result(result)
        if errors:
            raise RuntimeError(
                "generated result failed validation: " + ", ".join(errors)
            )
        rendered = canonical_json(result) + "\n"
        if args.write is not None:
            _write_idempotent(args.write, rendered)
    except RuntimeError as exc:
        parser.error(f"qualification failed: {exc}")
    print(rendered, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
