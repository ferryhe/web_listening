from __future__ import annotations

import inspect
from datetime import datetime
from types import SimpleNamespace

import tools.bootstrap_site_tree as bootstrap_site_tree_tool
import tools.classify_site_sections as classify_site_sections_tool
import tools.discover_site_sections as discover_site_sections_tool
import tools.export_scope_document_manifest as export_scope_document_manifest_tool
import tools.plan_monitor_scope as plan_monitor_scope_tool
import tools.run_site_tree as run_site_tree_tool
from web_listening.config import settings
from web_listening.models import CrawlScope
import web_listening.blocks.staged_workflow as staged_workflow
import web_listening.blocks.tree_bootstrap_workflow as tree_bootstrap_workflow
import web_listening.blocks.tree_run_workflow as tree_run_workflow


def test_staged_workflow_does_not_import_legacy_tools_modules():
    source = inspect.getsource(staged_workflow)

    assert "from tools." not in source
    assert "from tools import" not in source
    assert "import tools" not in source
    assert "import tools." not in source


def test_plan_monitor_scope_tool_delegates_to_staged_workflow_authority():
    source = inspect.getsource(plan_monitor_scope_tool)

    assert "web_listening.blocks.staged_workflow" in source


def test_discover_tool_delegates_to_staged_workflow_authority():
    source = inspect.getsource(discover_site_sections_tool)

    assert "web_listening.blocks.staged_workflow" in source


def test_classify_tool_delegates_to_staged_workflow_authority():
    source = inspect.getsource(classify_site_sections_tool)

    assert "web_listening.blocks.staged_workflow" in source


def test_export_manifest_tool_delegates_to_staged_workflow_authority():
    source = inspect.getsource(export_scope_document_manifest_tool)

    assert "web_listening.blocks.staged_workflow" in source


def test_bootstrap_tool_delegates_to_package_bootstrap_workflow():
    source = inspect.getsource(bootstrap_site_tree_tool)

    assert "web_listening.blocks.tree_bootstrap_workflow" in source


def test_run_tool_delegates_to_package_run_workflow():
    source = inspect.getsource(run_site_tree_tool)

    assert "web_listening.blocks.tree_run_workflow" in source


def test_default_output_path_builders_sanitize_catalog_and_site_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    report_path = staged_workflow.build_default_scope_report_path("../Bad Site/Name")
    inventory_path = staged_workflow.build_default_inventory_path("../Bad Catalog/Name")
    tree_run_path = tree_run_workflow.build_default_report_path("../Bad Catalog/Name")
    tree_bootstrap_path = tree_bootstrap_workflow.build_default_report_path("../Bad Catalog/Name")

    assert report_path.parent == tmp_path / "reports"
    assert inventory_path.parent == tmp_path / "plans"
    assert tree_run_path.parent == tmp_path / "reports"
    assert tree_bootstrap_path.parent == tmp_path / "reports"
    assert ".." not in report_path.name
    assert ".." not in inventory_path.name
    assert ".." not in tree_run_path.name
    assert ".." not in tree_bootstrap_path.name
    assert "/" not in report_path.name
    assert "/" not in inventory_path.name
    assert "/" not in tree_run_path.name
    assert "/" not in tree_bootstrap_path.name


def test_dated_output_path_accepts_naive_datetime(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", tmp_path)

    path = staged_workflow.build_default_scope_report_path("demo", now=datetime(2026, 4, 20, 12, 0, 0))

    assert path.parent == tmp_path / "reports"
    assert path.name.startswith("monitor_scope_demo_2026-04-20")


def test_staged_run_scope_provides_document_processor_when_downloading(monkeypatch):
    captured: dict[str, object] = {}

    plan = SimpleNamespace(
        catalog="dev",
        site_key="demo",
        display_name="Demo",
        seed_url="https://example.com/",
        max_depth=3,
        max_pages=8,
        max_files=1,
    )
    stored_scope = CrawlScope(
        id=7,
        site_id=1,
        seed_url="https://example.com/",
        allowed_origin="https://example.com",
        allowed_page_prefixes=["/research"],
        allowed_file_prefixes=["/"],
        max_depth=3,
        max_pages=8,
        max_files=1,
        fetch_mode="http",
    )

    class FakeStorage:
        def __init__(self, db_path):
            captured["db_path"] = db_path

        def close(self):
            captured["storage_closed"] = True

    class FakeDocumentProcessor:
        def __init__(self, storage):
            captured["document_processor_storage"] = storage

        def close(self):
            captured["document_processor_closed"] = True

    class FakeTreeCrawler:
        def __init__(self, *, storage, document_processor=None):
            captured["tree_storage"] = storage
            captured["document_processor"] = document_processor

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def run_scope(self, scoped_run, institution, download_files):
            captured["download_files"] = download_files
            return SimpleNamespace(
                scope=SimpleNamespace(id=stored_scope.id),
                run=SimpleNamespace(id=11, status="completed"),
                pages=[1],
                files=[1],
                new_pages=[],
                changed_pages=[],
                missing_pages=[],
                new_files=[],
                changed_files=[],
                missing_files=[],
                page_failures=[],
                file_failures=[],
            )

    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", lambda _: plan)
    monkeypatch.setattr(staged_workflow, "Storage", FakeStorage)
    monkeypatch.setattr(staged_workflow, "DocumentProcessor", FakeDocumentProcessor)
    monkeypatch.setattr(staged_workflow, "TreeCrawler", FakeTreeCrawler)
    monkeypatch.setattr(staged_workflow, "find_scope_for_plan", lambda storage, loaded_plan: (None, stored_scope))

    artifacts = staged_workflow.run_scope(scope_path="dummy", download_files=True)

    assert artifacts.result.run_id == 11
    assert captured["download_files"] is True
    assert captured["document_processor"] is not None
