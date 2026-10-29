---
name: web-listening-agent
description: Explore one-off public pages/files and operate recurring website monitoring through the `web_listening` CLI.
---

# Web Listening Agent

## Read first

Read the repository root `README.md`. It is the sole active human-facing source for product scope, authority, interfaces, schemas, storage, safety, versions, and validation. Read `AGENTS.md` separately for repository workflow and boundary rules. Documents under `docs/archive/` are provenance only, not current authority.

## Choose the route

- For a one-time public HTML question, dynamic page, document download, or PDF extraction, use the exploration route below. Start with representative data, then collect only the additional URLs the task needs.
- For recurring monitoring, persisted change history, or an explicitly requested governed scope, use the canonical workflow. Exploration results are evidence only and do not authorize a scope, run, or bootstrap.
- Read the relevant README sections progressively: start with **One-off exploration CLI** for one-off work; read the robots/sitemap diagnosis and formal access sections only when planning governed discovery or scope execution.

## Canonical workflow

Use the packaged CLI for recurring monitoring, persisted change history, or when the user asks for a governed scope:

1. `web-listening discover --catalog ...`
2. `web-listening classify --catalog ...`
3. review `section_selection.yaml`
4. `web-listening select --selection-path ...`
5. `web-listening plan-scope --selection-path ...`
6. compile/review the governed profile + Site Skill execution plan
7. `web-listening bootstrap-scope --scope-path ... --acquisition-profile-path ...`
8. `web-listening run-scope --scope-path ... --acquisition-profile-path ...`
9. `web-listening report-scope --scope-path ...`
10. `web-listening export-manifest --scope-path ...`

Do not bootstrap a new catalog before broad validation, draft-scope generation, and human scope confirmation. Lower-level `tools/*.py` programs are compatibility/developer wrappers.

## One-off data exploration

For a one-time question about a site's content, collect one representative page or file first, then expand only to the requested evidence. Use `web-listening list-exploration-tools --json` to select the versioned one-off tools. `list-acquisition-tools --json` is the separate formal planning/probe picker. Use the packaged exploration CLI rather than writing site-specific HTTP/browser/PDF glue:

1. `web-listening read-page --url URL --method http --json` for readable server HTML.
2. If HTTP fails (including 403), the page is empty, or it needs client rendering, preserve that result and explicitly try `--method browser` for URLs within the task's authorized target. A 403 applies to that request; do not infer that the whole site is inaccessible. Pass an ordered `--actions-json` or `--actions-file` array of `click`, `scroll`, `wait-selector`, `wait-text`, and bounded `wait` actions. For public URLs, a malformed sitemap or failed formal diagnosis blocks that planning input but does not by itself decide whether an explicitly authorized URL can be explored; preserve the diagnostic result and keep the one-off read within the user's task boundary.
3. `web-listening download-file --url URL --json` for the original document bytes.
4. `web-listening extract-pdf --input PATH --json` for page-indexed text and PDF metadata. The downloader's source sidecar supplies the source URL where possible.

Use the returned `ToolResult` as-is: check `ok`, `data_status`, `data_quality.passed`, `artifacts`, `attempts`, `next_action`, and `stop_reason`. Treat `error`, `blocked`, `empty`, and `partial` results distinctly; a 200 challenge page must not be accepted just because the HTTP request succeeded. A first browser DOM at `domcontentloaded` may precede client rendering or a challenge script; use an explicit bounded wait condition before treating that snapshot as final. Waiting alone does not show that a challenge passed. A plain mention of Cloudflare in regular article text is not sufficient evidence of a challenge. If a required public iframe/navigation is policy-blocked, inspect its request-ledger reason and explicitly add its host with `--allowed-domain` only when it belongs to the authorized public task. If the browser request budget is exhausted, follow `next_action`: for one page, adjust `read-page --max-requests`; for a URL list, reuse `artifacts.failed_urls` with `collect --max-requests`. Both accept 1-200 route-observed requests per page (default 100), and neither raises the budget automatically. Reduce page load when appropriate. Browser-mode errors retain requested channel and headless state, plus actual browser version when available. A partial PDF parse is not complete evidence; scanned pages may be empty because no OCR is promised. A 403 describes that particular request or route and does not prove that every URL on the site is inaccessible. Distinguish a command/runtime failure from a valid response with no text or a partial parse.

HTTPX exploration requests identify as `web-listening/<installed package version>` by default. Use `--user-agent` to provide another nonempty, printable one-line ASCII identity for HTTP `read-page`, `download-file`, or HTTP `collect`, then verify the actual sent value in request attempts and result/manifest evidence. Browser mode keeps the selected browser's normal identity and rejects a custom HTTPX User-Agent. The formal `AccessGateway` identity and catalog are unchanged.

For a validated URL list, use `collect --kind page|file --method http|browser`; `--actions-json` or `--actions-file` applies the same bounded action list to each page URL. Browser page collection accepts `--max-requests` (default 100, maximum 200) as its Playwright-route-observed per-page budget. File mode may add `--parse-pdf`. Each invocation writes one result per item and appends one JSON object per line to its manifest. Use the returned `artifacts.failed_urls` path as the next `--urls-file` to explicitly retry incomplete URLs with a chosen method or a higher browser request budget. The manifest retains the first method result and later retry rows.

These commands read only explicit URLs/files supplied for the invocation and save local exploration evidence. They do not create or authorize a formal scope/run, persist into `ArtifactStore`, or inherit the formal gateway's robots checks, DNS pinning, peer validation, or request budgets. Browser mode uses a fresh one-command context, real Playwright Chromium, and normal public subresource requests; Playwright applies allowed-host checks to document navigations observed by its route for the starting host plus explicit `--allowed-domain` values. The request count and cap describe routed observations. On the current build, direct HTTP 302 chains expose only their first hop to that route ledger, so this is not per-hop transport enforcement or the formal `AccessGateway`. The default is Playwright's bundled headless shell; `--browser-channel chromium|chrome|msedge` selects a normal installed channel, and `--headed` opens a visible window. Results record the requested channel, actual browser version when available, and headless state. These options do not use a saved browser profile, copied cookies, stealth patches, or challenge solving. There is no arbitrary JavaScript, login, form submission, or persistent session interface. Browser requests are direct target network traffic; state this boundary when recording evidence. Keep the bundled browser under a separate `PLAYWRIGHT_BROWSERS_PATH` cache.

Only after representative data confirms the target and relevant URLs should you broaden collection. If the user needs recurring monitoring or change detection, convert the confirmed intent into the canonical discovery/selection/scope workflow and obtain the required scope review before bootstrap/run.

## Authority and guardrails

- Formal bootstrap/run authority is the complete governed acquisition profile + Site Skill binding compiled into a validated non-empty execution plan before Storage opens.
- Picker and probe results are planning evidence, not formal authority.
- Keep `_blobs` as canonical SHA-256 storage and `_tracked` as the source-oriented view.
- Preserve scope/run/source/executor/Site Skill lineage, timestamps, and hashes in outputs.
- Keep traversal bounded and domain-constrained; require explicit authorization for stealth-capable runtimes.
- Site Skill registry inspection must remain static and side-effect free.

## Interface choice

Use `web-listening --help` and the root `README.md` for full CLI usage and exploration output fields. Use `web-listening list-exploration-tools --json` to choose one-off tools; use `web-listening list-acquisition-tools --json` only for its stable formal planning/probe catalog. Use the documented eleven-tool MCP server when an MCP caller needs shared acquisition, scoped execution, job, or artifact operations. Do not claim full REST planning parity: discover/classify/select/plan-scope remain CLI-authoritative.

## Validation

Run focused tests for the touched surface, then the full offline suite when appropriate. At minimum verify `git diff --check` and relevant CLI `--help`; use the validation commands listed in the root `README.md` for live or catalog work.
