import json
from pathlib import Path

from web_listening.blocks.acquisition_tools import acquisition_tools_catalog


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_PATH = REPO_ROOT / "docs/testing/fixtures/acquisition-tools-v1.sample.json"

EXPECTED_ADAPTERS = [
    "web_http",
    "browser_rendered",
    "sitemap",
    "rss",
    "cloakbrowser",
    "batch_python",
]

REQUIRED_TOOL_FIELDS = {
    "adapter",
    "category",
    "purpose",
    "recommended_when",
    "not_for",
    "operator_inputs",
    "requires_profile_safety",
    "output_contract",
    "runtime_status",
    "frontend_control",
    "built_in_now",
    "implemented_for_pr3_probing",
    "probe_capable",
    "safety_notes",
}


def test_acquisition_tools_v1_sample_fixture_has_frontend_agent_contract_shape():
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    assert payload["contract_version"] == "acquisition-tools.v1"
    assert payload["catalog_version"]
    assert payload["tool_selection_rules"][0]["tool"] == "web_http"
    assert [tool["adapter"] for tool in payload["tools"]] == EXPECTED_ADAPTERS

    tools = {tool["adapter"]: tool for tool in payload["tools"]}
    for tool in tools.values():
        assert REQUIRED_TOOL_FIELDS <= set(tool)
        assert isinstance(tool["recommended_when"], list)
        assert isinstance(tool["not_for"], list)
        assert isinstance(tool["operator_inputs"], list)
        assert isinstance(tool["requires_profile_safety"], dict)
        assert isinstance(tool["output_contract"], dict)
        assert tool["runtime_status"] in {"available", "optional_runtime", "reserved"}
        assert {"label", "picker_group", "control_kind", "selectable"} <= set(tool["frontend_control"])

    assert tools["web_http"]["recommended_when"][0] == "ordinary public HTML"
    web_http_inputs = {item["name"]: item for item in tools["web_http"]["operator_inputs"]}
    assert web_http_inputs["site_key"]["required"] is False
    assert web_http_inputs["site_key"]["required_when"] == "profile_path is not provided"
    assert tools["browser_rendered"]["recommended_when"][0] == "dynamic JavaScript-rendered public pages"
    assert tools["browser_rendered"]["runtime_status"] == "optional_runtime"
    assert tools["browser_rendered"]["optional_runtime"]["extra"] == "browser"
    assert tools["cloakbrowser"]["recommended_when"][0] == "authorized stealth browser or CDP-like contexts"
    assert tools["cloakbrowser"]["requires_profile_safety"] == {
        "allow_stealth_browser": True,
        "require_authorized_access": True,
    }
    cloak_inputs = {item["name"]: item for item in tools["cloakbrowser"]["operator_inputs"]}
    assert cloak_inputs["site_key"]["required_when"] == "profile_path is not provided"
    assert cloak_inputs["allow_stealth_browser"]["required_when"] == "profile_path is not provided"
    assert tools["batch_python"]["recommended_when"][0] == "bulk structured or site-specific scrape jobs"
    assert tools["sitemap"]["runtime_status"] == "reserved"
    assert tools["rss"]["runtime_status"] == "reserved"


def test_acquisition_tools_v1_sample_fixture_matches_runtime_catalog_contract_surface():
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    runtime = acquisition_tools_catalog()

    assert fixture["contract_version"] == runtime["contract_version"]
    assert fixture["catalog_version"] == runtime["catalog_version"]
    assert fixture["tool_selection_rules"] == runtime["tool_selection_rules"]

    fixture_tools = {tool["adapter"]: tool for tool in fixture["tools"]}
    runtime_tools = {tool["adapter"]: tool for tool in runtime["tools"]}
    assert list(fixture_tools) == list(runtime_tools)

    compared_fields = REQUIRED_TOOL_FIELDS | {"optional_runtime"}
    for adapter, runtime_tool in runtime_tools.items():
        fixture_tool = fixture_tools[adapter]
        for field in compared_fields & set(runtime_tool):
            assert fixture_tool[field] == runtime_tool[field]
