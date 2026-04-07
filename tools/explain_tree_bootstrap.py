from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from web_listening.blocks.storage import Storage
from web_listening.blocks.tree_explainer import BootstrapSiteEvidence, TreeBootstrapExplainer
from web_listening.config import settings
from web_listening.models import CrawlScope, Site
from web_listening.tree_targets import filter_tree_targets, load_tree_targets


def build_default_report_path(catalog: str, now: datetime | None = None) -> Path:
    moment = now or datetime.now().astimezone()
    report_date = moment.date().isoformat()
    return settings.data_dir / "reports" / f"tree_bootstrap_explained_{catalog}_{report_date}.md"


@dataclass(slots=True)
class ExplainResult:
    evidence: BootstrapSiteEvidence
    status: str
    notes: str = ""


def find_scope(storage: Storage, target) -> tuple[Site | None, CrawlScope | None]:
    expected_name = f"{target.display_name} Tree"
    matched_site = None
    for site in storage.list_sites(active_only=False):
        if site.name == expected_name and site.url == target.seed_url:
            matched_site = site
            break
    if matched_site is None:
        return None, None

    for scope in storage.list_crawl_scopes(site_id=matched_site.id):
        if (
            scope.seed_url == target.seed_url
            and scope.allowed_page_prefixes == target.allowed_page_prefixes
            and scope.allowed_file_prefixes == target.allowed_file_prefixes
        ):
            return matched_site, scope
    return matched_site, None


def explain_bootstrap(
    *,
    catalog: str,
    site_keys: set[str] | None = None,
    use_ai: bool = True,
) -> tuple[list[ExplainResult], str]:
    targets = filter_tree_targets(load_tree_targets(catalog), site_keys)
    storage = Storage(settings.db_path)
    results: list[ExplainResult] = []
    ai_summary_md = ""

    try:
        explainer = TreeBootstrapExplainer(storage)
        evidences: list[BootstrapSiteEvidence] = []
        for target in targets:
            _, scope = find_scope(storage, target)
            if scope is None or scope.id is None or scope.baseline_run_id is None:
                results.append(
                    ExplainResult(
                        evidence=BootstrapSiteEvidence(
                            display_name=target.display_name,
                            site_key=target.site_key,
                            seed_url=target.seed_url,
                            scope_id=0,
                            run_id=0,
                            pages=0,
                            files=0,
                        ),
                        status="missing_scope",
                        notes="Run bootstrap_site_tree.py first.",
                    )
                )
                continue
            evidence = explainer.build_site_evidence(
                display_name=target.display_name,
                site_key=target.site_key,
                scope=scope,
            )
            evidence.source_page_classifications = explainer.classify_site_sources(evidence)
            evidences.append(evidence)
            results.append(ExplainResult(evidence=evidence, status="explained"))

        if use_ai and evidences:
            ai_summary_md = explainer.generate_ai_summary(evidences, catalog=catalog)
        markdown = explainer.render_markdown(
            [item.evidence for item in results if item.status == "explained"],
            catalog=catalog,
            ai_summary_md=ai_summary_md,
        )
    finally:
        storage.close()

    return results, markdown


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explain the first-run bootstrap tree baseline for initialized scopes."
    )
    parser.add_argument("--catalog", choices=("dev", "smoke", "all"), default="dev")
    parser.add_argument("--site-key", action="append", help="Limit the explanation to one or more site keys.")
    parser.add_argument("--no-ai", action="store_true", help="Skip the optional OpenAI explanation layer.")
    parser.add_argument(
        "--report-path",
        type=Path,
        help="Optional Markdown output path. Defaults to data/reports/tree_bootstrap_explained_<catalog>_YYYY-MM-DD.md",
    )
    args = parser.parse_args()

    site_keys = {value.strip().lower() for value in args.site_key or [] if value.strip()} or None
    _, markdown = explain_bootstrap(
        catalog=args.catalog,
        site_keys=site_keys,
        use_ai=not args.no_ai,
    )
    report_path = args.report_path or build_default_report_path(args.catalog)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(markdown, encoding="utf-8")
    print(markdown)


if __name__ == "__main__":
    main()
