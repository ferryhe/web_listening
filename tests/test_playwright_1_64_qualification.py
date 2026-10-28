from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import subprocess
import os

import pytest

import tools.qualify_playwright_1_64 as q

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def baseline_root(tmp_path, monkeypatch):
    source = tmp_path / "baseline"
    for name in (q.READER, q.READER_TEST):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        # The candidate full-suite checkout retains the recorded baseline Git
        # objects; its patched working tree must not become its own baseline.
        text = subprocess.run(
            ["git", "show", "HEAD:" + name],
            cwd=os.environ.get("PLAYWRIGHT_QUALIFICATION_BASELINE_SOURCE", ROOT),
            check=True,
            capture_output=True,
        ).stdout
        path.write_bytes(text)
    (source / q.PATCH).parent.mkdir(parents=True, exist_ok=True)
    (source / q.PATCH).write_text(q.candidate_patch(source), encoding="utf-8")
    listing = "\0".join((q.READER, q.READER_TEST, q.PATCH)).encode()
    monkeypatch.setattr(
        q.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=listing)
    )
    return source


def test_snapshot_patch_is_minimal_and_source_stays_on_baseline(
    tmp_path, baseline_root
):
    snapshot = tmp_path / "candidate"
    evidence = q.create_snapshot(baseline_root, snapshot)
    assert q.verify_snapshot(baseline_root, snapshot, evidence) == []
    assert 'PLAYWRIGHT_RUNTIME_VERSION = "1.64.1"' in (snapshot / q.READER).read_text()
    assert (
        'PLAYWRIGHT_RUNTIME_VERSION = "1.62.0"'
        in (baseline_root / q.READER).read_text()
    )
    assert (ROOT / q.PATCH).read_text() == q.candidate_patch(baseline_root)
    (snapshot / q.READER).write_text("drift")
    assert q.READER in q.verify_snapshot(baseline_root, snapshot, evidence)


def test_patch_digest_drift_is_rejected(tmp_path, baseline_root):
    snapshot = tmp_path / "candidate"
    evidence = q.create_snapshot(baseline_root, snapshot)
    evidence["patch_sha256"] = "0" * 64
    assert "patch_sha256" in q.verify_snapshot(baseline_root, snapshot, evidence)


@pytest.mark.parametrize("version", [None, "1.62.0", "1.64.0"])
def test_missing_or_wrong_package_never_passes(tmp_path, version):
    wheel = tmp_path / q.WHEEL_NAME
    wheel.write_bytes(b"invalid")
    with pytest.raises(ValueError):
        q.verify_package(wheel, SimpleNamespace(version=version))


@pytest.mark.parametrize("wheel", ["missing.whl", "wrong.whl"])
def test_missing_or_wrong_wheel_never_passes(tmp_path, wheel):
    path = tmp_path / wheel
    if wheel.startswith("wrong"):
        path.write_bytes(b"wrong")
    with pytest.raises(ValueError):
        q.verify_package(path, SimpleNamespace(version=q.VERSION))


@pytest.mark.parametrize("archive", [None, "0" * 64, "invalid"])
def test_installed_archive_hash_is_mandatory(archive):
    distribution = SimpleNamespace(
        read_text=lambda _: json.dumps(
            {"archive_info": {"hashes": {"sha256": archive}}}
        )
    )
    with pytest.raises(ValueError):
        q.verify_archive(distribution, q.WHEEL_SHA256)


def test_conflicting_legacy_installed_archive_hash_rejected():
    distribution = SimpleNamespace(
        read_text=lambda _: json.dumps(
            {
                "archive_info": {
                    "hashes": {"sha256": q.WHEEL_SHA256},
                    "hash": "sha256=" + "0" * 64,
                }
            }
        )
    )
    with pytest.raises(ValueError):
        q.verify_archive(distribution, q.WHEEL_SHA256)


@pytest.mark.parametrize("field", ["revision", "browserVersion"])
def test_forged_browser_manifest_is_rejected(field):
    manifest = {
        "browsers": [
            {"name": name, "revision": q.REVISION, "browserVersion": q.BUILD}
            for name in ("chromium", "chromium-headless-shell")
        ]
    }
    manifest["browsers"][0][field] = "forged"
    with pytest.raises(ValueError):
        q.verify_browser_manifest(manifest)


@pytest.mark.parametrize("name", ["headed", "headless"])
def test_missing_browser_binary_is_rejected(tmp_path, name):
    with pytest.raises(ValueError):
        q.verify_binary(tmp_path, name)


def test_invalid_result_cannot_claim_adopt():
    with pytest.raises(ValueError):
        q.validate_result({"conclusion": "adopt"})


def test_rehashed_snapshot_reader_drift_is_rejected(tmp_path, baseline_root):
    snapshot = tmp_path / "candidate"
    evidence = q.create_snapshot(baseline_root, snapshot)
    (snapshot / q.READER).write_text("forged reader")
    evidence["candidate_files"][q.READER] = q.sha256(snapshot / q.READER)
    assert q.READER in q.verify_snapshot(baseline_root, snapshot, evidence)


@pytest.mark.parametrize("name", ["headed", "headless"])
def test_wrong_binary_bytes_are_rejected(tmp_path, name):
    relative, _ = q.BINARIES[name]
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_bytes(b"forged executable")
    with pytest.raises(ValueError):
        q.verify_binary(tmp_path, name)


def test_false_headed_headless_path_is_rejected(tmp_path, monkeypatch):
    headed = tmp_path / "headed.exe"
    headed.write_bytes(b"test identity")
    monkeypatch.setitem(q.BINARIES, "headed", ("headed.exe", q.sha256(headed)))
    with pytest.raises(ValueError):
        q.verify_binary(tmp_path, "headed", launched_path=tmp_path / "headless.exe")


def test_missing_browser_revision_is_rejected():
    with pytest.raises(ValueError):
        q.verify_browser_manifest({"browsers": []})


def test_result_fixture_is_exact_and_tamper_fails_closed():
    raw = (ROOT / q.RESULT).read_text()
    payload = json.loads(raw)
    assert raw == q.canonical_json(payload) + "\n"
    q.validate_result(payload)
    for path in (
        ("package", "version"),
        ("package", "wheel_sha256"),
        ("binaries", "headed"),
        ("binaries", "headless"),
        ("snapshot_patch_sha256",),
    ):
        mutated = deepcopy(payload)
        target = mutated
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = "forged"
        with pytest.raises(ValueError):
            q.validate_result(mutated)


@pytest.fixture
def real_candidate():
    from web_listening.executors.playwright_wrapper import PLAYWRIGHT_RUNTIME_VERSION
    from importlib import metadata

    if PLAYWRIGHT_RUNTIME_VERSION != q.VERSION:
        pytest.skip("candidate snapshot only; production reader remains on 1.62.0")
    assert metadata.version("playwright") == q.VERSION


@pytest.mark.live
def test_real_candidate_websocket_evidence_fails_closed(real_candidate, tmp_path):
    import sys
    import signal

    command = [
        sys.executable,
        str(ROOT / "tools/qualify_playwright_1_64.py"),
        "--websocket-probe",
    ]
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_error(400)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    child_environment = os.environ.copy()
    child_environment["PLAYWRIGHT_QUALIFICATION_WEBSOCKET_URL"] = (
        f"ws://127.0.0.1:{server.server_port}/blocked-ws"
    )
    output = tmp_path / "websocket-child.log"
    with output.open("w") as stream:
        process = subprocess.Popen(
            command,
            stdout=stream,
            stderr=subprocess.STDOUT,
            start_new_session=os.name != "nt",
            env=child_environment,
        )
        timed_out = False
        try:
            code = process.wait(timeout=25)
        except subprocess.TimeoutExpired:
            timed_out = True
            if os.name == "nt":
                cleanup = subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    capture_output=True,
                    text=True,
                )
                stream.write(
                    "\nSCOPED TIMEOUT CLEANUP: " + cleanup.stdout + cleanup.stderr
                )
            else:
                os.killpg(process.pid, signal.SIGTERM)
            code = process.wait(timeout=10)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    log = output.read_text(errors="replace")
    evidence = q.classify_probe(exit_code=code, timed_out=timed_out)
    evidence["loopback_websocket_target_reads"] = len(hits)
    assert hits == []
    (tmp_path / "websocket-outcome.json").write_text(q.canonical_json(evidence) + "\n")
    print(q.canonical_json(evidence))
    assert evidence["conclusion"] == "defer"
    if timed_out:
        assert evidence["browser_case"] == "failed"
        assert evidence["reader_cleanup"] == "not_completed"
    elif code == 0:
        assert '"completed":true' in log
    else:
        pytest.fail("real probe setup/process error:\n" + log)


@pytest.mark.live
@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("<html><body></body></html>", "empty_content"),
        (
            "<html><body>Please verify you are human to continue</body></html>",
            "blocked",
        ),
    ],
)
def test_real_candidate_empty_and_blocked_dom(real_candidate, body, expected):
    import httpx
    import runpy
    from web_listening.executors.playwright_wrapper import (
        BrowserCaptureError,
        _default_session_factory,
        prepare_browser_acquisition_adapter,
    )
    from web_listening.blocks.governed_read import MockClientReadGateway

    plan = runpy.run_path(str(ROOT / q.READER_TEST))["_plan"]()
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "text/html"}, text=body, request=request
        )

    gateway = MockClientReadGateway(
        httpx.Client(transport=httpx.MockTransport(handler)),
        user_agent="web-listening-bot/qualification-offline",
        max_body_bytes=4096,
    )
    adapter = prepare_browser_acquisition_adapter(
        plan, plan.steps[0], gateway, session_factory=_default_session_factory
    )
    with pytest.raises(BrowserCaptureError) as captured:
        adapter.capture("https://example.com/start", config={})
    assert captured.value.code == expected
    assert seen == ["https://example.com/start"]


@pytest.mark.parametrize(("code", "timeout"), [(0, False), (1, False), (-15, True)])
def test_probe_classification_never_grants_adoption(code, timeout):
    evidence = q.classify_probe(exit_code=code, timed_out=timeout)
    assert evidence["conclusion"] == "defer"
    assert (evidence["browser_case"] == "passed") == (code == 0 and not timeout)
