from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tools.qualify_mcp_1_28_1_to_1_30_0 import (
    BUILD_MANIFEST,
    EXPECTED_TOOL_NAMES,
    MCP_VERSIONS,
    QUALIFICATION_VERSION,
    compare,
    render_hash_lock,
    verify_rebuild,
    _child_import_provenance_matches,
    _valid_cancellation_evidence,
)

FIXTURES = Path(__file__).parents[1] / "docs/testing/fixtures"
MATRIX_NAMES = {"baseline": "1.28.1", "candidate": "1.30.0"}
COMBINATIONS = ("base", "dev", "mcp")


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _locked_packages(path: Path) -> dict[str, str]:
    packages = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.fullmatch(
            r"([a-z0-9][a-z0-9_.-]*)==([^ ]+) --hash=sha256:([0-9a-f]{64})", line
        )
        assert match is not None, line
        name, version, _ = match.groups()
        key = re.sub(r"[-_.]+", "-", name.lower())
        assert key not in packages
        packages[key] = version
    return packages


def test_report_lock_renderer_requires_each_selected_wheel_hash() -> None:
    report = {
        "install": [
            {
                "metadata": {"name": "Example_Package", "version": "1.2.3"},
                "download_info": {"archive_info": {"hashes": {"sha256": "a" * 64}}},
            },
            {
                "metadata": {"name": "web-listening", "version": "3.2.0"},
                "download_info": {"archive_info": {}},
            },
        ]
    }
    assert render_hash_lock(report) == f"example-package==1.2.3 --hash=sha256:{'a' * 64}\n"

    report["install"][0]["download_info"]["archive_info"] = {}
    with pytest.raises(ValueError, match="no selected wheel SHA-256"):
        render_hash_lock(report)


def test_mcp_qualification_fixtures_are_closed_hash_pinned_and_offline() -> None:
    assert MCP_VERSIONS == MATRIX_NAMES
    for matrix, mcp_version in MATRIX_NAMES.items():
        for combination in COMBINATIONS:
            stem = f"mcp-{mcp_version}-{combination}-linux-amd64-py3.12.14"
            snapshot = _load(f"{stem}.json")
            lock_path = FIXTURES / f"{stem}.lock"
            locked = _locked_packages(lock_path)
            installed = {
                item["name"]: item["version"]
                for item in snapshot["environment"]["packages"]
                if item["name"] not in {"pip", "setuptools", "wheel", "web-listening"}
            }

            assert snapshot["schema_version"] == QUALIFICATION_VERSION
            assert snapshot["matrix"] == matrix
            assert snapshot["combination"] == combination
            assert snapshot["build"] == BUILD_MANIFEST
            assert locked == installed
            assert snapshot["installation"]["installed_set_matches_lock"] is True
            assert snapshot["installation"]["pip_check_returncode"] == 0
            assert snapshot["project_wheel"]["sha256"] == snapshot["project_wheel"]["installed_archive_sha256"]
            assert snapshot["project_wheel"]["version"] == "3.2.0"
            assert snapshot["environment"]["fastapi"] == "0.139.2"
            assert snapshot["constraints"]["dev"].lower().startswith("mcp")
            assert snapshot["constraints"]["mcp"].lower().startswith("mcp")
            assert snapshot["constraints"] == {"dev": "mcp<2.0.0,>=1.28.1", "mcp": "mcp<2.0.0,>=1.28.1"}

            if combination == "base":
                assert snapshot["environment"]["mcp"] is None
                assert "mcp" not in locked
                assert snapshot["mcp_stdio"] is None
            else:
                assert snapshot["environment"]["mcp"] == mcp_version
                assert locked["mcp"] == mcp_version
                stdio = snapshot["mcp_stdio"]
                assert [tool["name"] for tool in stdio["tools"]] == EXPECTED_TOOL_NAMES
                assert set(stdio["valid_calls"]) == set(EXPECTED_TOOL_NAMES)
                assert set(stdio["invalid_calls"]) == set(EXPECTED_TOOL_NAMES)
                for name in EXPECTED_TOOL_NAMES:
                    assert stdio["valid_calls"][name]["isError"] is False
                    assert stdio["valid_calls"][name]["structuredContent"] is not None
                    if name == "web_listening_list_acquisition_tools":
                        assert stdio["invalid_calls"][name]["protocol_error"]["code"] == -32602
                    else:
                        assert stdio["invalid_calls"][name]["isError"] is True
                assert stdio["stdio_client_cleanup"]["all_children_reaped"] is True
                cancellation = stdio["cancellation"]
                assert _valid_cancellation_evidence(cancellation)
                assert "server_responded_after_cancel" not in cancellation
                assert cancellation["local_client_task_cancelled"] is True
                assert cancellation["cancellation_notification_observed"] is False
                assert cancellation["client_outgoing_notification_methods"] == ["notifications/initialized"]
                assert cancellation["original_request"]["handler_started_before_cancel"] is True
                assert cancellation["original_request"]["handler_state_before_transport_eof"] == "running"
                assert cancellation["original_request"]["handler_cancelled_before_transport_eof"] is False
                assert cancellation["original_request"]["events_before_transport_eof"] == ["started"]
                assert cancellation["original_request"]["handler_state_after_transport_eof"] == "finished"
                assert cancellation["original_request"]["handler_cancelled_after_transport_eof"] is True
                assert cancellation["original_request"]["events_after_transport_eof"] == [
                    "started", "cancelled", "finished"
                ]
                assert cancellation["followup_request"]["response_received_before_transport_eof"] is True
                assert cancellation["child_reaped_after_transport_eof"] is True
                assert "notifications/cancelled" not in cancellation["client_outgoing_notification_methods"]
                assert stdio["eof_probe"]["exited_on_eof"] is True
                assert stdio["eof_probe"]["eof_exit_code"] == 0
                assert stdio["server_info"]["version"] == mcp_version
                provenance = stdio["child_import_provenance"]
                assert provenance["distribution"] == "web-listening"
                assert provenance["wheel_archive_sha256"] == snapshot["project_wheel"]["sha256"]
                assert Path(provenance["site_packages_root"]).name == "site-packages"
                assert all(
                    Path(path).is_relative_to(Path(provenance["site_packages_root"]))
                    for path in provenance["module_paths"].values()
                )
                assert "no public target request" in stdio["target_read_policy"]


def test_cancellation_evidence_rejects_unrelated_followup_as_server_response() -> None:
    misleading = {
        "client_request_cancelled": True,
        "server_responded_after_cancel": True,
    }
    assert _valid_cancellation_evidence(misleading) is False


def test_seed_and_independent_rebuild_locks_reproduce_package_sets() -> None:
    for mcp_version in MATRIX_NAMES.values():
        for combination in COMBINATIONS:
            stem = f"mcp-{mcp_version}-{combination}-linux-amd64-py3.12.14"
            seed = _load(f"{stem}.json")
            rebuild = _load(f"mcp-{mcp_version}-{combination}-rebuild.json")
            result = verify_rebuild(seed, rebuild)
            expected = _load(f"mcp-{mcp_version}-{combination}-rebuild-comparison.json")
            assert result == expected
            assert result["compatible"] is True
            assert result["seed_project_wheel_valid"] is True
            assert result["rebuild_project_wheel_valid"] is True
            assert result["same_project_wheel"] is True


@pytest.mark.parametrize(
    "mutation", ["missing", "different_wheel", "mismatched_archive", "malformed_hash", "wrong_version"]
)
def test_rebuild_rejects_missing_or_changed_project_wheel_provenance(mutation: str) -> None:
    seed = _load("mcp-1.28.1-dev-linux-amd64-py3.12.14.json")
    rebuild = _load("mcp-1.28.1-dev-rebuild.json")
    if mutation == "missing":
        del rebuild["project_wheel"]
    elif mutation == "different_wheel":
        rebuild["project_wheel"]["sha256"] = "a" * 64
        rebuild["project_wheel"]["installed_archive_sha256"] = "a" * 64
    elif mutation == "mismatched_archive":
        rebuild["project_wheel"]["installed_archive_sha256"] = "a" * 64
    elif mutation == "malformed_hash":
        rebuild["project_wheel"]["sha256"] = "not-a-sha256"
    else:
        rebuild["project_wheel"]["version"] = "9.9.9"

    result = verify_rebuild(seed, rebuild)
    assert result["compatible"] is False
    assert result["same_project_wheel"] is False


def test_each_mcp_and_api_comparison_matches_committed_evidence() -> None:
    for combination in COMBINATIONS:
        baseline = _load(f"mcp-1.28.1-{combination}-linux-amd64-py3.12.14.json")
        candidate = _load(f"mcp-1.30.0-{combination}-linux-amd64-py3.12.14.json")
        result = compare(baseline, candidate)
        expected = _load(f"mcp-1.28.1-to-1.30.0-{combination}-comparison.json")
        assert result == expected
        assert result["compatible"] is True
        expected_deltas = [] if combination == "base" else [
            {"name": "mcp", "baseline": "1.28.1", "candidate": "1.30.0"}
        ]
        assert result["package_changes"] == expected_deltas

    baseline_dev = _load("mcp-1.28.1-dev-linux-amd64-py3.12.14.json")
    candidate_dev = _load("mcp-1.30.0-dev-linux-amd64-py3.12.14.json")
    assert _child_import_provenance_matches(baseline_dev) is True
    assert _child_import_provenance_matches(candidate_dev) is True
    baseline_packages = {item["name"]: item["version"] for item in baseline_dev["environment"]["packages"]}
    candidate_packages = {item["name"]: item["version"] for item in candidate_dev["environment"]["packages"]}
    assert baseline_packages["pytest"] == candidate_packages["pytest"] == "9.2.0"
    assert {name: version for name, version in baseline_packages.items() if name != "mcp"} == {
        name: version for name, version in candidate_packages.items() if name != "mcp"
    }

    candidate = _load("mcp-1.30.0-dev-linux-amd64-py3.12.14.json")
    candidate["mcp_stdio"]["tools"][0]["inputSchema"]["required"] = ["changed"]
    baseline = _load("mcp-1.28.1-dev-linux-amd64-py3.12.14.json")
    assert compare(baseline, candidate)["compatible"] is False

    candidate["project_wheel"]["sha256"] = "a" * 64
    candidate["project_wheel"]["installed_archive_sha256"] = "a" * 64
    assert compare(baseline, candidate)["compatible"] is False


def test_dev_and_mcp_extra_snapshots_have_same_mcp_contract_per_version() -> None:
    for mcp_version in MATRIX_NAMES.values():
        dev = _load(f"mcp-{mcp_version}-dev-linux-amd64-py3.12.14.json")
        mcp = _load(f"mcp-{mcp_version}-mcp-linux-amd64-py3.12.14.json")
        assert dev["mcp_stdio"]["tools"] == mcp["mcp_stdio"]["tools"]
        assert dev["mcp_stdio"]["valid_calls"] == mcp["mcp_stdio"]["valid_calls"]
        assert dev["mcp_stdio"]["invalid_calls"] == mcp["mcp_stdio"]["invalid_calls"]
        assert dev["api"] == mcp["api"]
        assert dev["cli"] == mcp["cli"]
        assert dev["mcp_stdio"]["server_info"]["version"] == mcp_version
        assert mcp["mcp_stdio"]["server_info"]["version"] == mcp_version


def test_full_suite_evidence_records_final_defer_and_shared_flaky_control() -> None:
    evidence = _load("mcp-1.30.0-dev-full-suite.json")
    first = evidence["candidate_full_suite"]["first_run"]
    repeated = evidence["candidate_full_suite"]["repeat_with_skip_reasons"]
    init_run = evidence["candidate_full_suite"]["init_enabled_run"]
    invalid_init_run = evidence["candidate_full_suite"]["invalid_init_attempt"]

    assert evidence["decision"] == "defer"
    assert first["returncode"] == repeated["returncode"] == 1
    assert (first["failed"], first["passed"], first["skipped"]) == (3, 3092, 7)
    assert (repeated["failed"], repeated["passed"], repeated["skipped"]) == (3, 3092, 7)
    assert invalid_init_run["accepted_as_final"] is False
    assert "/workspace/repo" in invalid_init_run["pythonpath"]
    assert (init_run["failed"], init_run["passed"], init_run["skipped"]) == (1, 3096, 7)
    assert init_run["returncode"] == 1
    assert init_run["log_sha256"] == "a692f2681572b395ea329a277da46a30a4498b6fc99afd7c3f90cca65d0361ff"
    assert init_run["source_commit"] == BUILD_MANIFEST["source_commit"]
    assert init_run["python_version"] == BUILD_MANIFEST["python"]
    assert init_run["pytest_version"] == "9.2.0"
    assert init_run["mcp_version"] == "1.30.0"
    assert init_run["fastapi_version"] == "0.139.2"
    assert init_run["project_wheel_sha256"] == "cd3bf730e3dbf0db5c565cf384e50012404d5805c87ccc192be8e15c3de6421e"
    assert "site-packages/web_listening/" in init_run["project_import_path"]
    assert "PYTHONPATH=/tmp/issue84-pytest-support" in init_run["command"]
    assert init_run["summary"] == "1 failed, 3096 passed, 7 skipped in 132.86s"
    assert init_run["failed_node"] == "tests/test_site_skill_registry.py::test_cache_source_same_inode_rewrite_after_package_hash_fails_closed"
    assert init_run["preflight_manifest_sha256"] == "523e7cf5fd023fbcc8e11cfb4a61310e8d271e1617c006fe1c4cff17cb8dad83"
    assert init_run["runner"]["hostconfig_init"] is True
    assert init_run["runner"]["id"] == "1bec665bfae412ba3d74a19eac21b9d1c0d1bca8b1581044964daf13c665e20b"
    assert init_run["runner"]["volume_filesystem"] == "ext4"
    assert len(evidence["candidate_full_suite"]["skip_reasons"]) == 7
    assert evidence["candidate_full_suite"]["original_runner"]["hostconfig_init_present"] is False
    prior = evidence["candidate_full_suite"]["superseded_pre_f1_run"]
    assert prior["accepted_as_final"] is False
    assert (prior["failed"], prior["passed"], prior["skipped"], prior["returncode"]) == (0, 3096, 7, 0)
    assert evidence["candidate_full_suite"]["final_status"] == "defer"
    registry_failure = next(
        failure
        for failure in evidence["candidate_full_suite"]["failures"]
        if failure["node"] == init_run["failed_node"]
    )
    assert "final post-capture candidate full suite also failed it" in registry_failure["classification"]
    assert registry_failure["result"] == (
        "Isolated baseline and candidate controls both failed; "
        "the final candidate full suite exited 1 with this node as its sole failure."
    )
    assert "final candidate full suite passed" not in registry_failure["result"]
    audit = evidence["candidate_full_suite"]["skip_reason_audit"]
    assert (audit["returncode"], audit["skipped"]) == (0, 7)
    assert audit["log_sha256"] == "6061aeb14dc2694bc6a3e51e507d9d573e92dc3274f99ea3c3619920939d705e"
    control = evidence["candidate_full_suite"]["same_inode_node_control"]
    assert control["same_failure_in_both"] is True
    assert control["candidate"]["returncode"] == control["baseline"]["returncode"] == 1
    assert control["candidate"]["log_sha256"] == "6c5beec3b1cdfe95afa4579ab373d8984fd376adf23280e7b9731b2381b515f0"
    assert control["baseline"]["log_sha256"] == "6058538c9a0e5c090b2177f829652d3312db69ac03498e274c6fd597b5f1e059"
    assert (evidence["focused_suite"]["passed"], evidence["focused_suite"]["returncode"]) == (282, 0)
    for result in (evidence["pid1_control"]["candidate_nodes"], evidence["pid1_control"]["baseline_nodes"]):
        assert result["passed"] == 2
        assert result["failed"] == 1
        assert result["failure"] == "tests/test_site_skill_registry.py::test_cache_source_same_inode_rewrite_after_package_hash_fails_closed"
    assert "same-inode" in evidence["decision_reason"]
    assert "not green" in evidence["decision_reason"]
