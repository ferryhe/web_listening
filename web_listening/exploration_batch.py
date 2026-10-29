from __future__ import annotations

import hashlib
import json
import os
import random
import time
from pathlib import Path
from uuid import uuid4
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from web_listening.contracts.tool_result import ToolResult, ToolResultDataQuality
from web_listening.exploration import (
    DEFAULT_HTTP_USER_AGENT,
    EXPLORATION_OUTPUT_DIR,
    MAX_BROWSER_REQUESTS,
    ExplorationError,
    _allowed_domains,
    _emit,
    _json_bytes,
    _load_actions,
    _result,
    _resolve_http_user_agent,
    _utc_now,
    _validate_browser_user_agent,
    _validate_url,
    _write_new,
    download_file,
    extract_pdf,
    read_page,
)


EXPLORATION_TOOLS_VERSION = "exploration-tools.v1"
_console = Console(width=200, soft_wrap=True)


def exploration_tools_catalog() -> dict[str, Any]:
    return {
        "contract_version": EXPLORATION_TOOLS_VERSION,
        "purpose": "Explicit one-off evidence collection; does not grant formal scope authority.",
        "formal_scope_catalog": "list-acquisition-tools (acquisition-tools.v1) remains the separate formal planning/probe picker.",
        "http_request_identity": {
            "default": DEFAULT_HTTP_USER_AGENT,
            "override": "--user-agent",
            "applies_to": ["read-page --method http", "download-file", "collect HTTP requests"],
            "browser_mode": "Uses the selected browser's normal identity; custom HTTP User-Agent overrides are rejected.",
        },
        "success_rule": "Check data_quality.passed and data_status; collect exits nonzero when any item is incomplete.",
        "tools": [
            {
                "name": "read-page",
                "modes": ["http", "browser"],
                "runtime_requirements": {
                    "http": [],
                    "browser": ["web-listening[browser]", "bundled Chromium or an installed chromium/chrome/msedge channel"],
                },
                "recommended_when": ["one public HTML page", "FAQ expansion or AJAX pagination after explicit browser actions"],
                "result": "final DOM or static HTML, text, links, interaction candidates, attempts, HTTP User-Agent identity, and local page artifacts",
            },
            {
                "name": "download-file",
                "modes": ["http"],
                "runtime_requirements": {"http": []},
                "recommended_when": ["one original public document or attachment"],
                "result": "complete content-addressed bytes, SHA-256, source sidecar with HTTP User-Agent identity, and local artifact",
            },
            {
                "name": "extract-pdf",
                "modes": ["local-file"],
                "runtime_requirements": {"local-file": ["web-listening[pdf]"]},
                "recommended_when": ["page-indexed text and metadata from a downloaded PDF"],
                "result": "page-indexed text and metadata; partial extraction remains explicitly partial; no OCR",
            },
            {
                "name": "collect",
                "modes": ["page:http", "page:browser", "file:http", "file:http+parse-pdf"],
                "runtime_requirements": {
                    "page:http": [],
                    "page:browser": ["web-listening[browser]", "selected browser runtime"],
                    "file:http": [],
                    "file:http+parse-pdf": ["web-listening[pdf]"],
                },
                "recommended_when": ["a known URL list should be read serially with per-URL results and an append-only manifest"],
                "result": "per-item ToolResult files, append-only JSONL rows, counts, and failed_urls.txt for incomplete items",
            },
        ],
    }


def _read_urls_file(path: Path) -> tuple[Path, list[str]]:
    path = path.expanduser().resolve()
    if not path.is_file():
        raise ExplorationError("collect.urls_file_missing", "The URL list file does not exist")
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError) as exc:
        raise ExplorationError("collect.urls_file_invalid", "The URL list must be a readable UTF-8 text file") from exc
    urls = [line.strip() for line in lines if line.strip()]
    if not urls:
        raise ExplorationError("collect.urls_empty", "The URL list contains no nonblank URLs")
    return path, urls


def _append_manifest_row(path: Path, row: dict[str, Any]) -> None:
    with path.open("ab+") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.seek(0, os.SEEK_END)
                stream.write(b"\n")
        stream.seek(0, os.SEEK_END)
        stream.write((json.dumps(row, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())


def _stage_record(result: ToolResult, result_path: Path) -> dict[str, Any]:
    return {
        "tool": result.tool,
        "ok": result.ok,
        "data_status": result.data_status,
        "quality_passed": result.data_quality.passed,
        "error_code": result.error.code if result.error else None,
        "next_action": result.next_action,
        "browser_channel": result.data.get("browser_channel"),
        "browser_version": result.data.get("browser_version"),
        "headless": result.data.get("headless"),
        "user_agent": result.data.get("user_agent"),
        "artifacts": result.artifacts,
        "result_ref": str(result_path.resolve()),
    }


def _save_stage(result: ToolResult, stage_dir: Path, name: str) -> tuple[Path, dict[str, Any]]:
    path = stage_dir / f"{name}.json"
    _write_new(path, _json_bytes(result.model_dump(mode="json")))
    return path, _stage_record(result, path)


def _with_collection(result: ToolResult, *, collection: dict[str, Any], stages: list[dict[str, Any]]) -> ToolResult:
    payload = result.model_dump(mode="python")
    payload["tool"] = "collect"
    payload["data"] = {**result.data, "collection": collection, "stages": stages}
    return ToolResult.model_validate(payload)


def _file_collection_result(
    download: ToolResult,
    parse: ToolResult | None,
    *,
    collection: dict[str, Any],
    stages: list[dict[str, Any]],
) -> ToolResult:
    if not download.ok:
        return _with_collection(download, collection=collection, stages=stages)
    artifacts = dict(download.artifacts)
    if parse is None:
        result = download
        data = {**download.data, "collection": collection, "stages": stages}
    else:
        artifacts.update(parse.artifacts)
        data = {
            "download": download.data,
            "pdf": parse.data,
            "collection": collection,
            "stages": stages,
        }
        if parse.ok and parse.data_quality.passed:
            result = parse
        else:
            error = parse.error
            return ToolResult(
                ok=True,
                has_data=True,
                data_status="partial",
                data_count=parse.data_count,
                tool="collect",
                data_quality=ToolResultDataQuality(
                    passed=False,
                    word_count=parse.data_quality.word_count,
                    failure_reasons=[error.code if error else "pdf_extraction_incomplete"],
                ),
                data=data,
                artifacts=artifacts,
                attempts=[*download.attempts, *parse.attempts],
                next_action=parse.next_action or "inspect_pdf_extraction_result",
                warnings=[error.message] if error else [],
                stop_reason="pdf_extraction_incomplete",
            )
    payload = result.model_dump(mode="python")
    payload["tool"] = "collect"
    payload["data"] = data
    payload["artifacts"] = artifacts
    payload["attempts"] = [*download.attempts, *(parse.attempts if parse else [])]
    return ToolResult.model_validate(payload)


def _item_data(result: ToolResult) -> dict[str, Any]:
    collection = result.data.get("collection", {})
    return {
        "url": collection.get("url"),
        "outcome": collection.get("outcome"),
        "max_requests": collection.get("max_requests"),
        "user_agent": collection.get("user_agent"),
        "data_status": result.data_status,
        "quality_passed": result.data_quality.passed,
        "error_code": result.error.code if result.error else None,
        "next_action": result.next_action,
        "artifacts": result.artifacts,
        "result_ref": collection.get("result_ref"),
        "stages": collection.get("stages", []),
    }


def collect_urls(
    *,
    urls_file: Path,
    kind: str,
    method: str,
    output_dir: Path,
    manifest_path: Path | None,
    allowed_domains: list[str] | None,
    parse_pdf: bool,
    pacing_seconds: float,
    jitter_seconds: float,
    timeout_seconds: float,
    browser_channel: str,
    headed: bool,
    max_requests: int,
    user_agent: str | None,
    actions_json: str,
    actions_file: Path | None,
) -> ToolResult:
    actual_user_agent: str | None = None
    try:
        if kind not in {"page", "file"}:
            raise ExplorationError("collect.kind_invalid", "Kind must be page or file")
        if method not in {"http", "browser"}:
            raise ExplorationError("collect.method_invalid", "Method must be http or browser")
        if kind == "file" and method != "http":
            raise ExplorationError("collect.method_invalid", "File collection always uses HTTP; pass --method http")
        if method == "browser":
            _validate_browser_user_agent(user_agent)
        else:
            actual_user_agent = _resolve_http_user_agent(user_agent)
        if parse_pdf and kind != "file":
            raise ExplorationError("collect.parse_pdf_invalid", "--parse-pdf is available only with --kind file")
        if browser_channel not in {"bundled", "chromium", "chrome", "msedge"}:
            raise ExplorationError("browser_channel.invalid", "Browser channel must be bundled, chromium, chrome, or msedge")
        if kind == "file" and (browser_channel != "bundled" or headed):
            raise ExplorationError("browser_options.unsupported", "Browser options apply only to page collection")
        if kind == "page" and method == "http" and (browser_channel != "bundled" or headed):
            raise ExplorationError("browser_options.unsupported", "Browser channel and --headed require --method browser")
        if kind == "file" and (actions_json or actions_file is not None):
            raise ExplorationError("collect.actions_unsupported", "Page actions apply only to page collection")
        actions = _load_actions(actions_json, actions_file)
        if actions and method != "browser":
            raise ExplorationError("actions.unsupported", "Page actions require --method browser")
        normalized_actions_json = json.dumps(actions, ensure_ascii=True) if actions else ""
        if not 0 <= pacing_seconds <= 60 or not 0 <= jitter_seconds <= 30:
            raise ExplorationError("pacing.invalid", "Pacing must be 0-60 seconds and jitter 0-30 seconds")
        if not 1 <= timeout_seconds <= 180:
            raise ExplorationError("timeout.invalid", "Timeout must be between 1 and 180 seconds")
        if not 1 <= max_requests <= MAX_BROWSER_REQUESTS:
            raise ExplorationError(
                "budget.invalid",
                f"Max requests must be between 1 and {MAX_BROWSER_REQUESTS}",
            )
        if (kind == "file" or method == "http") and max_requests != 100:
            raise ExplorationError(
                "browser_options.unsupported",
                "--max-requests applies only to browser page collection",
            )
        input_path, raw_urls = _read_urls_file(urls_file)
        urls = []
        for index, raw_url in enumerate(raw_urls, start=1):
            try:
                url = _validate_url(raw_url)
                _allowed_domains(url, allowed_domains)
            except ExplorationError as exc:
                raise ExplorationError(
                    "collect.url_invalid",
                    f"Invalid URL on line {index}: {exc.message}",
                ) from exc
            urls.append(url)
        output_dir = output_dir.expanduser().resolve()
        manifest = (manifest_path or output_dir / "manifest.jsonl").expanduser().resolve()
        if manifest == input_path:
            raise ExplorationError("collect.manifest_conflicts_with_input", "The manifest path cannot overwrite the URL list")
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            manifest.parent.mkdir(parents=True, exist_ok=True)
            manifest.touch(exist_ok=True)
        except OSError as exc:
            raise ExplorationError("collect.output_unavailable", "The output directory or manifest path is not writable") from exc
    except ExplorationError as exc:
        next_action = {
            "collect.urls_file_missing": "correct_urls_file_path",
            "collect.urls_file_invalid": "save_a_utf8_urls_file",
            "collect.urls_empty": "add_one_url_per_nonblank_line",
            "collect.url_invalid": "correct_invalid_urls_before_retrying",
            "collect.manifest_conflicts_with_input": "choose_a_separate_manifest_path",
            "collect.output_unavailable": "choose_a_writable_output_path",
            "collect.parse_pdf_invalid": "use_kind_file_or_remove_parse_pdf",
            "collect.actions_unsupported": "remove_page_actions_for_file_collection",
            "actions.ambiguous": "choose_actions_json_or_file",
            "actions.invalid": "correct_actions",
            "actions.unsupported": "retry_with_browser_for_page_actions",
            "collect.method_invalid": "correct_collect_kind_and_method",
            "collect.kind_invalid": "correct_collect_kind",
            "browser_channel.invalid": "select_a_supported_browser_channel",
            "browser_options.unsupported": "use_browser_options_only_with_browser_page_reads",
            "pacing.invalid": "correct_pacing_arguments",
            "timeout.invalid": "correct_timeout_argument",
            "budget.invalid": "set_max_requests_between_1_and_200",
            "user_agent.invalid": "correct_user_agent",
            "user_agent.unsupported": "remove_user_agent_for_browser_mode",
        }.get(exc.code, "correct_collect_arguments")
        return _result(tool="collect", error=exc, next_action=next_action)

    batch_id = uuid4().hex
    invocation_id = uuid4().hex
    collection_dir = output_dir / "collections" / invocation_id
    stages_dir = collection_dir / "stages"
    items_summary: list[dict[str, Any]] = []
    failed_urls: list[str] = []
    attempts: list[dict[str, Any]] = []
    success_count = 0
    partial_count = 0
    for index, url in enumerate(urls, start=1):
        item_started_at = _utc_now()
        item_stage_records: list[dict[str, Any]] = []
        item_stage_results: list[ToolResult] = []
        item_dir = collection_dir / "items" / f"{index:06d}-{hashlib.sha256(url.encode('utf-8')).hexdigest()[:12]}"
        try:
            if kind == "page":
                wait_seconds = pacing_seconds + random.uniform(0, jitter_seconds)
                time.sleep(wait_seconds)
                paced_at = _utc_now()
                result = read_page(
                    url=url,
                    method=method,
                    output_dir=item_dir,
                    allowed_domains=allowed_domains,
                    actions_json=normalized_actions_json,
                    actions_file=None,
                    wait_until="domcontentloaded",
                    timeout_seconds=timeout_seconds,
                    max_requests=max_requests,
                    max_dom_bytes=10 * 1024 * 1024,
                    max_http_bytes=10 * 1024 * 1024,
                    browser_channel=browser_channel,
                    headed=headed,
                    user_agent=actual_user_agent,
                )
                _, stage_record = _save_stage(result, stages_dir, f"{index:06d}-read-page")
                stage_record["pacing_wait_seconds"] = round(wait_seconds, 3)
                stage_record["paced_at"] = paced_at
                if method == "browser":
                    stage_record["max_requests"] = max_requests
                item_stage_records.append(stage_record)
                item_stage_results.append(result)
            else:
                downloaded = download_file(
                    url=url,
                    output_dir=output_dir,
                    allowed_domains=allowed_domains,
                    timeout_seconds=timeout_seconds,
                    max_bytes=128 * 1024 * 1024,
                    pacing_seconds=pacing_seconds,
                    jitter_seconds=jitter_seconds,
                    user_agent=actual_user_agent,
                )
                _, stage_record = _save_stage(downloaded, stages_dir, f"{index:06d}-download-file")
                item_stage_records.append(stage_record)
                item_stage_results.append(downloaded)
                parsed: ToolResult | None = None
                if downloaded.ok and parse_pdf:
                    parsed = extract_pdf(
                        input_path=Path(downloaded.data["artifact_path"]),
                        output_dir=output_dir,
                        source_url=downloaded.data.get("source_url"),
                        max_bytes=128 * 1024 * 1024,
                    )
                    _, parse_record = _save_stage(parsed, stages_dir, f"{index:06d}-extract-pdf")
                    item_stage_records.append(parse_record)
                    item_stage_results.append(parsed)
                result = _file_collection_result(
                    downloaded,
                    parsed,
                    collection={"url": url, "kind": kind, "method": method},
                    stages=item_stage_records,
                )
            success = bool(result.ok and result.data_quality.passed)
            partial = result.data_status == "partial"
            outcome = "success" if success else "partial" if partial else "failed"
        except KeyboardInterrupt:
            raise
        except Exception:
            result = _result(
                tool="collect",
                error=ExplorationError("collect.item_failed", "An item failed before its result could be produced", retryable=True),
                next_action="inspect_item_result_and_retry_if_appropriate",
            )
            success = False
            partial = False
            outcome = "failed"

        result_path = item_dir / "item-result.json"
        result = _with_collection(
            result,
            collection={
                "batch_id": batch_id,
                "invocation_id": invocation_id,
                "index": index,
                "url": url,
                "kind": kind,
                "method": method,
                "browser_channel": browser_channel if kind == "page" and method == "browser" else None,
                "browser_version": result.data.get("browser_version") if kind == "page" and method == "browser" else None,
                "headless": not headed if kind == "page" and method == "browser" else None,
                "max_requests": max_requests if kind == "page" and method == "browser" else None,
                "user_agent": actual_user_agent,
                "outcome": outcome,
                "started_at": item_started_at,
                "finished_at": _utc_now(),
                "result_ref": str(result_path.resolve()),
            },
            stages=item_stage_records,
        )
        if method == "browser" and result.error and result.error.code == "browser.request_limit":
            result = ToolResult.model_validate(
                {
                    **result.model_dump(mode="python"),
                    "next_action": "retry_collect_failed_urls_with_higher_max_requests_or_reduce_page_load",
                }
            )
        _write_new(result_path, _json_bytes(result.model_dump(mode="json")))
        if not success:
            failed_urls.append(url)
        else:
            success_count += 1
        if partial:
            partial_count += 1
        for stage_result in item_stage_results:
            attempts.extend(
                {**attempt, "item_index": index, "url": url, "stage": stage_result.tool}
                for attempt in stage_result.attempts
            )
        item_ref = str(result_path.resolve())
        row = {
            "schema_version": "exploration-manifest-row.v1",
            "batch_id": batch_id,
            "invocation_id": invocation_id,
            "index": index,
            "url": url,
            "kind": kind,
            "method": method,
            "browser_channel": browser_channel if kind == "page" and method == "browser" else None,
            "browser_version": result.data.get("browser_version") if kind == "page" and method == "browser" else None,
            "headless": not headed if kind == "page" and method == "browser" else None,
            "max_requests": max_requests if kind == "page" and method == "browser" else None,
            "user_agent": actual_user_agent,
            "stages": item_stage_records,
            "outcome": outcome,
            "artifacts": result.artifacts,
            "result_ref": item_ref,
        }
        _append_manifest_row(manifest, row)
        items_summary.append(_item_data(result))

    failed_path: Path | None = None
    if failed_urls:
        failed_path = collection_dir / "failed_urls.txt"
        _write_new(failed_path, ("\n".join(failed_urls) + "\n").encode("utf-8"))
    failed_count = len(urls) - success_count
    all_passed = failed_count == 0
    status = "present" if all_passed else "partial" if success_count else "failed_quality_gate"
    summary = {
        "schema_version": "exploration-collect.v1",
        "batch_id": batch_id,
        "invocation_id": invocation_id,
        "kind": kind,
        "method": method,
        "user_agent": actual_user_agent,
        "requested_count": len(urls),
        "success_count": success_count,
        "failed_count": failed_count,
        "partial_count": partial_count,
        "failed_urls": failed_urls,
        "manifest_path": str(manifest.resolve()),
        "items": items_summary,
        "failed_items": [item for item in items_summary if item["outcome"] != "success"],
        "recommended_next_actions": sorted(
            {item["next_action"] for item in items_summary if item["outcome"] != "success" and item["next_action"]}
        ),
    }
    artifact_refs = {"manifest": str(manifest.resolve())}
    if failed_path:
        artifact_refs["failed_urls"] = str(failed_path.resolve())
    return _result(
        tool="collect",
        data=summary,
        artifacts=artifact_refs,
        attempts=attempts,
        data_status=status,
        data_count=success_count,
        next_action=(
            None
            if all_passed
            else (
                summary["recommended_next_actions"][0]
                if len(summary["recommended_next_actions"]) == 1
                else "inspect_failed_item_results"
            )
        ),
        stop_reason="completed" if all_passed else "partial" if success_count else "no_successful_items",
        data_quality=ToolResultDataQuality(
            passed=all_passed,
            failure_reasons=[] if all_passed else [f"{failed_count}_items_incomplete"],
        ),
    )


def register_exploration_batch_commands(app: typer.Typer) -> None:
    @app.command("list-exploration-tools")
    def list_exploration_tools(
        json_output: bool = typer.Option(False, "--json", help="Emit the versioned exploration-tool catalog."),
    ) -> None:
        payload = exploration_tools_catalog()
        if json_output:
            typer.echo(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True))
            return
        table = Table(title="One-off Exploration Tools")
        table.add_column("Tool", style="cyan")
        table.add_column("Modes")
        table.add_column("Runtime")
        table.add_column("Recommended when")
        for item in payload["tools"]:
            table.add_row(
                item["name"],
                ", ".join(item["modes"]),
                "; ".join(", ".join(value) for value in item["runtime_requirements"].values() if value) or "built in",
                "; ".join(item["recommended_when"]),
            )
        _console.print(table)

    @app.command("collect", help="Collect a URL list serially into local per-item evidence and an append-only manifest.")
    def collect_command(
        urls_file: Path = typer.Option(..., "--urls-file", exists=False, help="UTF-8 file with one URL per nonblank line."),
        kind: str = typer.Option(..., "--kind", help="Collect page or file evidence."),
        method: str = typer.Option("http", "--method", help="Page read method: http or browser; files always use HTTP."),
        output_dir: Path = typer.Option(EXPLORATION_OUTPUT_DIR, "--output-dir", help="Directory for collection evidence."),
        manifest_path: Path | None = typer.Option(None, "--manifest", help="Append-only JSONL manifest path; defaults to <output-dir>/manifest.jsonl."),
        allowed_domain: list[str] = typer.Option([], "--allowed-domain", help="Allowed navigation or redirect host; may be repeated."),
        parse_pdf: bool = typer.Option(False, "--parse-pdf", help="After each file download, also attempt PDF text extraction."),
        pacing_seconds: float = typer.Option(0.5, "--pacing-seconds", min=0, max=60),
        jitter_seconds: float = typer.Option(0.5, "--jitter-seconds", min=0, max=30),
        timeout_seconds: float = typer.Option(60, "--timeout-seconds", min=1, max=180),
        browser_channel: str = typer.Option("bundled", "--browser-channel", help="Normal Playwright channel: bundled, chromium, chrome, or msedge."),
        headed: bool = typer.Option(False, "--headed", help="Show the selected browser window for page collection."),
        max_requests: int = typer.Option(
            100,
            "--max-requests",
            min=1,
            max=MAX_BROWSER_REQUESTS,
            help="Route-observed browser requests per page (1-200).",
        ),
        user_agent: str | None = typer.Option(
            None,
            "--user-agent",
            help=f"HTTPX User-Agent (default {DEFAULT_HTTP_USER_AGENT}); browser mode keeps its normal identity.",
        ),
        actions_json: str = typer.Option("", "--actions-json", help="Ordered browser actions applied to each page URL."),
        actions_file: Path | None = typer.Option(None, "--actions-file", help="File containing the ordered browser action array applied to each page URL."),
        json_output: bool = typer.Option(False, "--json", help="Emit the shared ToolResult JSON envelope."),
    ) -> None:
        result = collect_urls(
            urls_file=urls_file,
            kind=kind,
            method=method,
            output_dir=output_dir,
            manifest_path=manifest_path,
            allowed_domains=allowed_domain,
            parse_pdf=parse_pdf,
            pacing_seconds=pacing_seconds,
            jitter_seconds=jitter_seconds,
            timeout_seconds=timeout_seconds,
            browser_channel=browser_channel,
            headed=headed,
            max_requests=max_requests,
            user_agent=user_agent,
            actions_json=actions_json,
            actions_file=actions_file,
        )
        _emit(result, json_output=json_output)
        if not result.ok or result.data_status in {"partial", "failed_quality_gate"}:
            raise typer.Exit(1)


__all__ = ["collect_urls", "exploration_tools_catalog", "register_exploration_batch_commands"]
