from __future__ import annotations

import inspect

import tools.bootstrap_site_tree as bootstrap_site_tree_tool
import tools.export_scope_document_manifest as export_scope_document_manifest_tool
import tools.plan_monitor_scope as plan_monitor_scope_tool
import tools.run_site_tree as run_site_tree_tool
import web_listening.blocks.staged_workflow as staged_workflow


def test_staged_workflow_does_not_import_legacy_tools_modules():
    source = inspect.getsource(staged_workflow)

    assert "from tools." not in source


def test_plan_monitor_scope_tool_delegates_to_staged_workflow_authority():
    source = inspect.getsource(plan_monitor_scope_tool)

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
