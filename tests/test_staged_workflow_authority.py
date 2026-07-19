from __future__ import annotations

import ast
import inspect
from datetime import datetime
from types import SimpleNamespace

import pytest

import tools.bootstrap_site_tree as bootstrap_site_tree_tool
import tools.classify_site_sections as classify_site_sections_tool
import tools.discover_site_sections as discover_site_sections_tool
import tools.export_scope_document_manifest as export_scope_document_manifest_tool
import tools.plan_monitor_scope as plan_monitor_scope_tool
import tools.run_site_tree as run_site_tree_tool
from web_listening.config import settings
from web_listening.models import CrawlScope
from web_listening.blocks.monitor_scope_planner import MonitorScopePlan
import web_listening.blocks.staged_workflow as staged_workflow
import web_listening.blocks.tree_bootstrap_workflow as tree_bootstrap_workflow
import web_listening.blocks.tree_run_workflow as tree_run_workflow


def _monitor_plan():
    return MonitorScopePlan(
        "scope", "demo", "Demo", "dev", "2026-01-01Z", "approved", "manual",
        "Track", "https://example.com/", "https://example.com/", "http", {},
        "selected_scope", "selected_scope_default", "site_root", ["/research"], ["/"],
        max_depth=3, max_pages=8, max_files=1,
    )


def test_staged_workflow_does_not_import_legacy_tools_modules():
    source = inspect.getsource(staged_workflow)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "tools"
                assert not alias.name.startswith("tools.")
        if isinstance(node, ast.ImportFrom):
            assert node.module != "tools"
            assert not (node.module or "").startswith("tools.")


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

    plan = _monitor_plan()
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


def test_build_section_inventory_treats_zero_max_pages_as_bounded(monkeypatch):
    fake_target = SimpleNamespace(
        site_key="demo",
        display_name="Demo",
        seed_url="https://example.com/",
        homepage_url="https://example.com/",
        fetch_mode="http",
        fetch_config_json={},
        allowed_page_prefixes=["/research"],
        allowed_file_prefixes=["/"],
        notes="",
    )

    class FakeDiscoverer:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def discover_target(self, **kwargs):
            return SimpleNamespace(site_key=kwargs["site_key"])

    monkeypatch.setattr(staged_workflow, "load_tree_targets", lambda catalog: [fake_target])
    monkeypatch.setattr(staged_workflow, "filter_tree_targets", lambda targets, site_keys: targets)
    monkeypatch.setattr(staged_workflow, "SectionDiscoverer", FakeDiscoverer)

    inventory = staged_workflow.build_section_inventory(catalog="dev", max_pages=0)

    assert inventory.max_pages == 0
    assert inventory.page_limit_mode == "bounded"


def test_staged_bootstrap_scope_preserves_zero_limit_overrides(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    plan = _monitor_plan()
    fake_target = SimpleNamespace(site_key="demo")

    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", lambda _: plan)
    monkeypatch.setattr(staged_workflow, "monitor_scope_to_tree_target", lambda plan: fake_target)
    monkeypatch.setattr(
        staged_workflow,
        "run_bootstrap",
        lambda **kwargs: captured.setdefault("bootstrap_kwargs", kwargs) or [],
    )
    monkeypatch.setattr(staged_workflow, "render_bootstrap_run_markdown", lambda *args, **kwargs: "report")

    artifacts = staged_workflow.bootstrap_scope(
        scope_path="dummy",
        max_depth=0,
        max_pages=0,
        max_files=0,
        report_path=tmp_path / "bootstrap.md",
    )

    assert artifacts.report_path == tmp_path / "bootstrap.md"
    assert captured["bootstrap_kwargs"]["max_depth"] == 0
    assert captured["bootstrap_kwargs"]["max_pages"] == 0
    assert captured["bootstrap_kwargs"]["max_files"] == 0


def test_governed_bootstrap_compiles_overrides_before_bootstrap_mutation(monkeypatch, tmp_path):
    captured = {}
    plan = MonitorScopePlan(
        "scope", "demo", "Demo", "dev", "2026-01-01Z", "approved", "manual",
        "Track", "https://example.com/research", "https://example.com/", "http", {},
        "selected_scope", "selected_scope_default", "site_root", ["/research"], ["/"],
        max_depth=3, max_pages=8, max_files=2,
        based_on={"acquisition_profile_id": "governed"},
    )

    def compile_gateway(effective_plan, **kwargs):
        captured["budgets"] = (effective_plan.max_depth, effective_plan.max_pages, effective_plan.max_files)
        raise ValueError("authority rejected")

    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", lambda _: plan)
    monkeypatch.setattr(staged_workflow, "_compile_acquisition_gateway", compile_gateway)
    monkeypatch.setattr(
        staged_workflow, "run_bootstrap",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("mutation path reached")),
    )

    with pytest.raises(ValueError, match="authority rejected"):
        staged_workflow.bootstrap_scope(
            scope_path="dummy", max_depth=1, max_pages=4, max_files=1,
            report_path=tmp_path / "unused.md",
        )

    assert captured["budgets"] == (1, 4, 1)


def test_staged_bootstrap_target_conversion_failure_precedes_gateway_construction(
    monkeypatch, tmp_path,
):
    plan = _monitor_plan()
    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", lambda _: plan)
    monkeypatch.setattr(
        staged_workflow, "monitor_scope_to_tree_target",
        lambda _: (_ for _ in ()).throw(ValueError("target conversion failed")),
    )
    monkeypatch.setattr(
        staged_workflow, "_compile_acquisition_gateway",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("gateway constructed before target conversion")
        ),
    )

    with pytest.raises(ValueError, match="target conversion failed"):
        staged_workflow.bootstrap_scope(
            scope_path="dummy", report_path=tmp_path / "unused.md",
        )


def test_governed_run_authority_failure_happens_before_storage(monkeypatch):
    plan = MonitorScopePlan(
        "scope", "demo", "Demo", "dev", "2026-01-01Z", "approved", "manual",
        "Track", "https://example.com/research", "https://example.com/", "http", {},
        "selected_scope", "selected_scope_default", "site_root", ["/research"], ["/"],
        max_depth=3, max_pages=8, max_files=2,
        based_on={"acquisition_profile_id": "governed"},
    )
    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", lambda _: plan)
    monkeypatch.setattr(
        staged_workflow, "_compile_acquisition_gateway",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("authority rejected")),
    )
    monkeypatch.setattr(
        staged_workflow, "Storage",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("storage constructed")),
    )

    with pytest.raises(ValueError, match="authority rejected"):
        staged_workflow.run_scope(scope_path="dummy")


def test_staged_run_scope_preserves_zero_limit_overrides(monkeypatch, tmp_path):
    captured: dict[str, object] = {}

    plan = _monitor_plan()
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

    class FakeTreeCrawler:
        def __init__(self, *, storage, document_processor=None):
            captured["document_processor"] = document_processor

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def run_scope(self, scoped_run, institution, download_files):
            captured["scoped_run"] = scoped_run
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
    monkeypatch.setattr(staged_workflow, "TreeCrawler", FakeTreeCrawler)
    monkeypatch.setattr(staged_workflow, "find_scope_for_plan", lambda storage, loaded_plan: (None, stored_scope))
    monkeypatch.setattr(staged_workflow, "render_run_markdown", lambda *args, **kwargs: "report")

    artifacts = staged_workflow.run_scope(
        scope_path="dummy",
        max_depth=0,
        max_pages=0,
        max_files=0,
        report_path=tmp_path / "run.md",
    )

    assert artifacts.report_path == tmp_path / "run.md"
    assert captured["scoped_run"].max_depth == 0
    assert captured["scoped_run"].max_pages == 0
    assert captured["scoped_run"].max_files == 0


def test_tree_bootstrap_main_preserves_explicit_zero_cli_limits(monkeypatch, tmp_path):
    captured: dict[str, object] = {}
    args = SimpleNamespace(
        catalog="dev",
        scope_path=None,
        max_depth=0,
        max_pages=0,
        max_files=0,
        download_files=False,
        refresh_existing=False,
        site_key=None,
        report_path=tmp_path / "bootstrap-cli.md",
    )

    monkeypatch.setattr(tree_bootstrap_workflow.argparse.ArgumentParser, "parse_args", lambda self: args)
    monkeypatch.setattr(
        tree_bootstrap_workflow,
        "run_bootstrap",
        lambda **kwargs: captured.setdefault("bootstrap_kwargs", kwargs) or [],
    )
    monkeypatch.setattr(tree_bootstrap_workflow, "render_markdown", lambda *args, **kwargs: "report")

    tree_bootstrap_workflow.main()

    assert captured["bootstrap_kwargs"]["max_depth"] == 0
    assert captured["bootstrap_kwargs"]["max_pages"] == 0
    assert captured["bootstrap_kwargs"]["max_files"] == 0


def test_run_scope_primary_gateway_failure_still_closes_processor_and_storage(monkeypatch):
    closed = []
    plan = _monitor_plan()
    scope = CrawlScope(
        id=7, site_id=1, seed_url="https://example.com/", allowed_origin="https://example.com",
        allowed_page_prefixes=["/"], allowed_file_prefixes=["/"], max_depth=1,
        max_pages=1, max_files=1, is_initialized=True,
    )

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            closed.append(self.name)

    class FakeStorage(Resource):
        def __init__(self, *args):
            super().__init__("storage")

    class FakeProcessor(Resource):
        def __init__(self, *, storage):
            super().__init__("processor")

    class FakeTree:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def run_scope(self, *args, **kwargs):
            raise RuntimeError("gateway failed")

    gateway = Resource("gateway")
    monkeypatch.setattr(staged_workflow, "load_monitor_scope_plan", lambda _: plan)
    monkeypatch.setattr(staged_workflow, "_compile_acquisition_gateway", lambda *a, **k: gateway)
    monkeypatch.setattr(staged_workflow, "Storage", FakeStorage)
    monkeypatch.setattr(staged_workflow, "DocumentProcessor", FakeProcessor)
    monkeypatch.setattr(staged_workflow, "find_scope_for_plan", lambda *a: (None, scope))
    monkeypatch.setattr(staged_workflow, "TreeCrawler", FakeTree)

    with pytest.raises(RuntimeError, match="gateway failed"):
        staged_workflow.run_scope(scope_path="dummy", download_files=True)

    assert closed == ["gateway", "processor", "storage"]


@pytest.mark.parametrize("failure_point", ["load", "filter"])
def test_run_bootstrap_target_resolution_failure_closes_gateway_without_masking_primary(
    monkeypatch, failure_point,
):
    attempted = []

    class Gateway:
        def close(self):
            attempted.append("gateway")
            raise RuntimeError("gateway cleanup failed")

    def fail_load(catalog):
        if failure_point == "load":
            raise ValueError("target load failed")
        return []

    def fail_filter(targets, site_keys):
        raise ValueError("target filter failed")

    monkeypatch.setattr(tree_bootstrap_workflow, "load_tree_targets", fail_load)
    monkeypatch.setattr(tree_bootstrap_workflow, "filter_tree_targets", fail_filter)
    monkeypatch.setattr(
        tree_bootstrap_workflow, "Storage",
        lambda *args: (_ for _ in ()).throw(AssertionError("storage constructed")),
    )

    with pytest.raises(ValueError, match=f"target {failure_point} failed"):
        tree_bootstrap_workflow.run_bootstrap(
            catalog="scope", max_depth=1, max_pages=1, max_files=1,
            acquisition_gateway=Gateway(),
        )

    assert attempted == ["gateway"]


@pytest.mark.parametrize("failure_point", ["construction", "entry"])
def test_run_bootstrap_primary_tree_failure_survives_failing_all_resource_cleanup(
    monkeypatch, failure_point,
):
    attempted = []

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            attempted.append(self.name)
            raise RuntimeError(f"{self.name} cleanup failed")

    storage = Resource("storage")
    processor = Resource("processor")
    gateway = Resource("gateway")

    class FakeStorage:
        def __new__(cls, *args):
            return storage

    class FakeProcessor:
        def __new__(cls, *, storage):
            return processor

    class FakeTree:
        def __init__(self, **kwargs):
            if failure_point == "construction":
                raise ValueError("tree construction failed")
            self.acquisition_gateway = kwargs["acquisition_gateway"]
            self.document_processor = kwargs["document_processor"]

        def __enter__(self):
            raise ValueError("tree entry failed")

        def close(self):
            attempted.append("tree")
            for resource in (self.acquisition_gateway, self.document_processor):
                if resource is not None:
                    resource.close()
            raise RuntimeError("tree cleanup failed")

    monkeypatch.setattr(tree_bootstrap_workflow, "Storage", FakeStorage)
    monkeypatch.setattr(tree_bootstrap_workflow, "DocumentProcessor", FakeProcessor)
    monkeypatch.setattr(tree_bootstrap_workflow, "TreeCrawler", FakeTree)

    with pytest.raises(ValueError, match=f"tree {failure_point} failed"):
        tree_bootstrap_workflow.run_bootstrap(
            catalog="scope", max_depth=1, max_pages=1, max_files=1,
            targets=[], download_files=True, acquisition_gateway=gateway,
        )

    expected = ["gateway", "processor", "storage"]
    if failure_point == "entry":
        expected.insert(0, "tree")
    assert attempted == expected


def test_run_bootstrap_surfaces_first_cleanup_only_failure_and_attempts_all(monkeypatch):
    attempted = []

    class Resource:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def close(self):
            attempted.append(self.name)
            if self.fail:
                raise RuntimeError(f"{self.name} cleanup failed")

    storage = Resource("storage", fail=True)
    processor = Resource("processor", fail=True)
    gateway = Resource("gateway", fail=True)

    class FakeTree:
        def __init__(self, **kwargs):
            self.acquisition_gateway = kwargs["acquisition_gateway"]
            self.document_processor = kwargs["document_processor"]

        def __enter__(self):
            return self

        def close(self):
            attempted.append("tree")
            for resource in (self.acquisition_gateway, self.document_processor):
                if resource is not None:
                    resource.close()

    monkeypatch.setattr(tree_bootstrap_workflow, "Storage", lambda *a: storage)
    monkeypatch.setattr(tree_bootstrap_workflow, "DocumentProcessor", lambda **k: processor)
    monkeypatch.setattr(tree_bootstrap_workflow, "TreeCrawler", FakeTree)

    with pytest.raises(RuntimeError, match="gateway cleanup failed"):
        tree_bootstrap_workflow.run_bootstrap(
            catalog="scope", max_depth=1, max_pages=1, max_files=1,
            targets=[], download_files=True, acquisition_gateway=gateway,
        )

    assert attempted == ["tree", "gateway", "processor", "storage"]


def test_run_bootstrap_preserves_recorded_target_failure_and_attempts_all_cleanup(monkeypatch):
    attempted = []

    class Resource:
        def __init__(self, name):
            self.name = name

        def close(self):
            attempted.append(self.name)
            raise RuntimeError(f"{self.name} cleanup failed")

    storage = Resource("storage")
    processor = Resource("processor")
    gateway = Resource("gateway")

    class FakeTree:
        def __init__(self, **kwargs):
            self.acquisition_gateway = kwargs["acquisition_gateway"]
            self.document_processor = kwargs["document_processor"]

        def __enter__(self):
            return self

        def bootstrap_scope(self, *args, **kwargs):
            raise ValueError("target acquisition failed")

        def close(self):
            attempted.append("tree")
            raise RuntimeError("tree cleanup failed")

    target = SimpleNamespace(
        catalog="scope", site_key="demo", display_name="Demo",
        seed_url="https://example.com/", tree_max_depth=None,
        tree_max_pages=None, tree_max_files=None, notes="",
    )
    scope = SimpleNamespace(id=7, is_initialized=False)
    monkeypatch.setattr(tree_bootstrap_workflow, "Storage", lambda *a: storage)
    monkeypatch.setattr(tree_bootstrap_workflow, "DocumentProcessor", lambda **k: processor)
    monkeypatch.setattr(tree_bootstrap_workflow, "TreeCrawler", FakeTree)
    monkeypatch.setattr(tree_bootstrap_workflow, "ensure_tree_site", lambda *a: object())
    monkeypatch.setattr(tree_bootstrap_workflow, "ensure_tree_scope", lambda *a, **k: scope)

    results = tree_bootstrap_workflow.run_bootstrap(
        catalog="scope", max_depth=1, max_pages=1, max_files=1,
        targets=[target], download_files=True, acquisition_gateway=gateway,
    )

    assert len(results) == 1
    assert results[0].status == "failed"
    assert results[0].notes == "ValueError: target acquisition failed"
    assert attempted == ["tree", "gateway", "processor", "storage"]
