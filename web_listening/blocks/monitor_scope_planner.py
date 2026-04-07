from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from web_listening.blocks.section_discovery import render_yaml
from web_listening.tree_defaults import PRODUCTION_TREE_LIMITS
from web_listening.tree_targets import TreeTarget, load_tree_targets


@dataclass(slots=True)
class SelectionEntry:
    path: str
    selection_reason: str = ""


@dataclass(slots=True)
class SectionSelection:
    site_key: str
    generated_at: str
    selection_mode: str
    review_status: str
    business_goal: str
    based_on: dict[str, str] = field(default_factory=dict)
    selection_summary: dict[str, int] = field(default_factory=dict)
    selected_sections: list[SelectionEntry] = field(default_factory=list)
    rejected_sections: list[SelectionEntry] = field(default_factory=list)
    deferred_sections: list[SelectionEntry] = field(default_factory=list)
    excluded_categories: list[str] = field(default_factory=list)
    excluded_prefixes: list[str] = field(default_factory=list)
    selection_notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MonitorScopePlan:
    site_key: str
    display_name: str
    catalog: str
    generated_at: str
    selection_review_status: str
    selection_mode: str
    business_goal: str
    seed_url: str
    homepage_url: str
    fetch_mode: str
    fetch_config_json: dict[str, Any]
    tree_strategy: str
    tree_budget_profile: str
    file_scope_mode: str
    allowed_page_prefixes: list[str]
    allowed_file_prefixes: list[str]
    selected_focus_prefixes: list[str] = field(default_factory=list)
    excluded_page_prefixes: list[str] = field(default_factory=list)
    deferred_page_prefixes: list[str] = field(default_factory=list)
    excluded_categories: list[str] = field(default_factory=list)
    max_depth: int = PRODUCTION_TREE_LIMITS.max_depth
    max_pages: int = PRODUCTION_TREE_LIMITS.max_pages
    max_files: int = PRODUCTION_TREE_LIMITS.max_files
    based_on: dict[str, str] = field(default_factory=dict)
    selection_summary: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalize_prefix(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    while normalized != "/" and normalized.endswith("/"):
        normalized = normalized[:-1]
    return normalized or "/"


def _path_level(path: str) -> int:
    normalized = _normalize_prefix(path)
    if normalized in {"", "/"}:
        return 0
    return len([part for part in normalized.strip("/").split("/") if part])


def _covers(prefix: str, candidate: str) -> bool:
    normalized_prefix = _normalize_prefix(prefix)
    normalized_candidate = _normalize_prefix(candidate)
    if not normalized_prefix or not normalized_candidate:
        return False
    if normalized_prefix == "/":
        return True
    return normalized_candidate == normalized_prefix or normalized_candidate.startswith(normalized_prefix + "/")


def _dedupe_prefixes(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = _normalize_prefix(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def minimize_prefixes(values: list[str]) -> list[str]:
    normalized = _dedupe_prefixes(values)
    ordered = sorted(
        enumerate(normalized),
        key=lambda item: (_path_level(item[1]), len(item[1]), item[0]),
    )
    kept: list[str] = []
    for _, candidate in ordered:
        if any(_covers(prefix, candidate) for prefix in kept):
            continue
        kept.append(candidate)
    return kept


def _as_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        normalized = str(item or "").strip()
        if normalized:
            result.append(normalized)
    return result


def _parse_entries(value: Any) -> list[SelectionEntry]:
    if not isinstance(value, list):
        return []
    entries: list[SelectionEntry] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        path = _normalize_prefix(item.get("path", ""))
        if not path:
            continue
        entries.append(
            SelectionEntry(
                path=path,
                selection_reason=str(item.get("selection_reason", "")).strip(),
            )
        )
    return entries


def load_section_selection(path: str | Path) -> SectionSelection:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    return SectionSelection(
        site_key=str(payload.get("site_key", "")).strip().lower(),
        generated_at=str(payload.get("generated_at", "")).strip(),
        selection_mode=str(payload.get("selection_mode", "")).strip(),
        review_status=str(payload.get("review_status", "")).strip(),
        business_goal=str(payload.get("business_goal", "")).strip(),
        based_on={str(key): str(value) for key, value in (payload.get("based_on", {}) or {}).items()},
        selection_summary={
            str(key): int(value)
            for key, value in (payload.get("selection_summary", {}) or {}).items()
            if str(key).strip()
        },
        selected_sections=_parse_entries(payload.get("selected_sections")),
        rejected_sections=_parse_entries(payload.get("rejected_sections")),
        deferred_sections=_parse_entries(payload.get("deferred_sections")),
        excluded_categories=_as_string_list(payload.get("excluded_categories")),
        excluded_prefixes=[_normalize_prefix(value) for value in _as_string_list(payload.get("excluded_prefixes"))],
        selection_notes=_as_string_list(payload.get("selection_notes")),
    )


def _load_classification_site(path: str | Path, *, site_key: str) -> tuple[str, dict[str, Any]]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    catalog = str(payload.get("catalog", "")).strip().lower()
    for site in payload.get("sites", []) or []:
        if isinstance(site, dict) and str(site.get("site_key", "")).strip().lower() == site_key:
            return catalog, site
    raise ValueError(f"Could not find site `{site_key}` in classification file `{path}`.")


def _find_tree_target(catalog: str, site_key: str) -> TreeTarget | None:
    normalized_catalog = (catalog or "").strip().lower()
    if normalized_catalog not in {"dev", "smoke", "all"}:
        return None
    try:
        targets = load_tree_targets(normalized_catalog)
    except Exception:  # pragma: no cover - defensive loader fallback
        return None
    for target in targets:
        if target.site_key == site_key:
            return target
    return None


def _selected_focus_prefixes(selected: list[str], allowed: list[str]) -> list[str]:
    focus: list[str] = []
    for item in _dedupe_prefixes(selected):
        if item in allowed:
            continue
        if any(_covers(prefix, item) for prefix in allowed):
            focus.append(item)
    return focus


def compile_monitor_scope(
    selection: SectionSelection,
    *,
    classification_path: str | Path,
    file_scope_mode: str = "site_root",
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
) -> MonitorScopePlan:
    if file_scope_mode not in {"site_root", "selected_pages"}:
        raise ValueError("file_scope_mode must be one of: site_root, selected_pages")

    catalog, site_payload = _load_classification_site(classification_path, site_key=selection.site_key)
    target = _find_tree_target(catalog, selection.site_key)

    requested_selected = [entry.path for entry in selection.selected_sections]
    allowed_page_prefixes = minimize_prefixes(requested_selected)
    selected_focus_prefixes = _selected_focus_prefixes(requested_selected, allowed_page_prefixes)

    explicit_excluded = [entry.path for entry in selection.rejected_sections] + selection.excluded_prefixes
    excluded_page_prefixes = [
        prefix
        for prefix in minimize_prefixes(explicit_excluded)
        if not any(_covers(prefix, allowed) or _covers(allowed, prefix) for allowed in allowed_page_prefixes)
    ]
    deferred_page_prefixes = [
        prefix
        for prefix in minimize_prefixes([entry.path for entry in selection.deferred_sections])
        if not any(_covers(allowed, prefix) for allowed in allowed_page_prefixes)
    ]

    if file_scope_mode == "site_root":
        allowed_file_prefixes = ["/"]
    else:
        allowed_file_prefixes = allowed_page_prefixes.copy()

    fetch_config_json: dict[str, Any] = {}
    if target is not None:
        fetch_config_json = target.fetch_config_json

    effective_max_depth = max_depth or (target.tree_max_depth if target and target.tree_max_depth else None) or PRODUCTION_TREE_LIMITS.max_depth
    effective_max_pages = max_pages or (target.tree_max_pages if target and target.tree_max_pages else None) or PRODUCTION_TREE_LIMITS.max_pages
    effective_max_files = max_files or (target.tree_max_files if target and target.tree_max_files else None) or PRODUCTION_TREE_LIMITS.max_files

    notes = list(selection.selection_notes)
    notes.append("Allowed page prefixes are minimized from the selected sections; redundant child prefixes move into `selected_focus_prefixes`.")
    if file_scope_mode == "site_root":
        notes.append("File scope is intentionally broader than page scope so selected pages can still download same-origin files hosted under shared asset paths.")
    else:
        notes.append("File scope matches page scope prefixes; this is stricter and may exclude files hosted outside the selected page branches.")
    if any(prefix.lower() != prefix for prefix in allowed_page_prefixes):
        notes.append("Case-variant page prefixes were preserved because the site emitted both canonical and mixed-case paths.")
    if excluded_page_prefixes:
        notes.append("Excluded page prefixes are recorded for traceability, but current tree bootstrap enforcement is allow-list based.")

    return MonitorScopePlan(
        site_key=selection.site_key,
        display_name=str(site_payload.get("display_name", "")).strip() or (target.display_name if target else selection.site_key.upper()),
        catalog=catalog,
        generated_at=datetime.now(timezone.utc).isoformat(),
        selection_review_status=selection.review_status,
        selection_mode=selection.selection_mode,
        business_goal=selection.business_goal,
        seed_url=str(site_payload.get("seed_url", "")).strip() or (target.seed_url if target else ""),
        homepage_url=str(site_payload.get("homepage_url", "")).strip() or (target.homepage_url if target else ""),
        fetch_mode=(target.fetch_mode if target else str(site_payload.get("fetch_mode", "http")).strip() or "http"),
        fetch_config_json=fetch_config_json,
        tree_strategy="selected_scope",
        tree_budget_profile=(target.tree_budget_profile if target and target.tree_budget_profile else "selected_scope_default"),
        file_scope_mode=file_scope_mode,
        allowed_page_prefixes=allowed_page_prefixes,
        allowed_file_prefixes=allowed_file_prefixes,
        selected_focus_prefixes=selected_focus_prefixes,
        excluded_page_prefixes=excluded_page_prefixes,
        deferred_page_prefixes=deferred_page_prefixes,
        excluded_categories=selection.excluded_categories,
        max_depth=effective_max_depth,
        max_pages=effective_max_pages,
        max_files=effective_max_files,
        based_on=dict(selection.based_on),
        selection_summary=selection.selection_summary,
        notes=notes,
    )


def build_monitor_scope(
    selection_path: str | Path,
    *,
    classification_path: str | Path | None = None,
    file_scope_mode: str = "site_root",
    max_depth: int | None = None,
    max_pages: int | None = None,
    max_files: int | None = None,
) -> MonitorScopePlan:
    selection = load_section_selection(selection_path)
    resolved_classification_path = classification_path or selection.based_on.get("section_classification")
    if not resolved_classification_path:
        raise ValueError("A classification path is required either via --classification-path or selection.based_on.section_classification.")
    plan = compile_monitor_scope(
        selection,
        classification_path=resolved_classification_path,
        file_scope_mode=file_scope_mode,
        max_depth=max_depth,
        max_pages=max_pages,
        max_files=max_files,
    )
    plan.based_on = {
        **selection.based_on,
        "section_selection": str(selection_path).replace("\\", "/"),
        "section_classification": str(resolved_classification_path).replace("\\", "/"),
    }
    return plan


def render_markdown(plan: MonitorScopePlan) -> str:
    lines = [
        "# Plan Monitor Scope",
        "",
        "## Final Conclusion",
        "",
        f"- Conclusion time: `{plan.generated_at}`",
        f"- Site: `{plan.display_name}` (`{plan.site_key}`)",
        f"- Catalog: `{plan.catalog}`",
        f"- Selection status: `{plan.selection_review_status}`",
        f"- Scope result: allowed_page_prefixes=`{len(plan.allowed_page_prefixes)}`, selected_focus_prefixes=`{len(plan.selected_focus_prefixes)}`, excluded_page_prefixes=`{len(plan.excluded_page_prefixes)}`, deferred_page_prefixes=`{len(plan.deferred_page_prefixes)}`",
        f"- Runtime defaults: max_depth=`{plan.max_depth}`, max_pages=`{plan.max_pages}`, max_files=`{plan.max_files}`, file_scope_mode=`{plan.file_scope_mode}`",
        "- Operational note: this artifact is the direct bridge between section selection and a later targeted tree bootstrap.",
        "",
        "## Compiled Scope",
        "",
        f"- Seed URL: `{plan.seed_url}`",
        f"- Homepage URL: `{plan.homepage_url}`",
        f"- Fetch mode: `{plan.fetch_mode}`",
        f"- Tree strategy: `{plan.tree_strategy}`",
        f"- Tree budget profile: `{plan.tree_budget_profile}`",
        f"- Business goal: {plan.business_goal}",
        "",
        "### Allowed Page Prefixes",
        "",
    ]
    for prefix in plan.allowed_page_prefixes:
        lines.append(f"- `{prefix}`")
    lines.extend(["", "### Allowed File Prefixes", ""])
    for prefix in plan.allowed_file_prefixes:
        lines.append(f"- `{prefix}`")
    if plan.selected_focus_prefixes:
        lines.extend(["", "### Selected Focus Prefixes", ""])
        for prefix in plan.selected_focus_prefixes:
            lines.append(f"- `{prefix}`")
    if plan.excluded_page_prefixes:
        lines.extend(["", "### Excluded Page Prefixes", ""])
        for prefix in plan.excluded_page_prefixes:
            lines.append(f"- `{prefix}`")
    if plan.deferred_page_prefixes:
        lines.extend(["", "### Deferred Page Prefixes", ""])
        for prefix in plan.deferred_page_prefixes:
            lines.append(f"- `{prefix}`")
    if plan.notes:
        lines.extend(["", "## Notes", ""])
        for note in plan.notes:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def render_yaml_text(plan: MonitorScopePlan) -> str:
    return render_yaml(plan.to_dict())
