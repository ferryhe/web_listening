# MCP Fallback Chain Design

`web_listening` exposes the acquisition fallback chain to Hermes and other agents through a thin MCP server. The MCP layer is intentionally an adapter: it validates and normalizes agent input, calls shared core functions in `web_listening.blocks`, and returns the shared `ToolResult` envelope.

## Server entrypoint

Install the MCP extra or the development extra, then run the stdio server:

```bash
pip install -e '.[mcp]'
web-listening-mcp
```

Development installs can use:

```bash
pip install -e '.[dev]'
web-listening-mcp
```

Hermes config example:

```yaml
mcp_servers:
  web_listening:
    command: "web-listening-mcp"
    args: []
    timeout: 300
    connect_timeout: 60
```

## MCP tools

The server exposes four acquisition tools:

| Tool | Purpose |
|---|---|
| `web_listening_list_acquisition_tools` | Return the acquisition tool catalog in a `ToolResult` envelope. |
| `web_listening_probe_tool_once` | Probe one adapter such as `web_http` or `browser_rendered`. |
| `web_listening_recommend_next_tool` | Pure decision helper that recommends the next adapter from prior attempts. |
| `web_listening_acquire_with_fallback` | Run the shared fallback engine and return all attempt history. |

The default agent-facing path is:

1. call `web_listening_list_acquisition_tools` to inspect capabilities;
2. call `web_listening_acquire_with_fallback` for a bounded acquisition attempt;
3. inspect `ok`, `has_data`, `data_status`, `stop_reason`, `attempts`, `next_tool`, and `warnings` before deciding whether to continue.

## Example fallback call

```json
{
  "url": "https://example.com/reports",
  "site_key": "example",
  "goal": "find public report/document links",
  "strategy": "document_discovery",
  "quality_gates": {
    "min_words": 120,
    "min_links": 3,
    "min_document_links": 1
  },
  "safety": {
    "allowed_domains": ["example.com"],
    "allow_stealth_browser": false,
    "require_authorized_access": false
  },
  "max_attempts": 4
}
```

A successful result uses the shared contract shape:

```json
{
  "ok": true,
  "has_data": true,
  "data_status": "present",
  "tool": "browser_rendered",
  "data_count": 1,
  "stop_reason": "usable_data_found",
  "attempts": [
    {"tool": "web_http", "data_status": "failed_quality_gate"},
    {"tool": "browser_rendered", "data_status": "present"}
  ],
  "data": {
    "final_url": "https://example.com/reports",
    "content_text_preview": "..."
  }
}
```

Large page bodies, downloaded files, manifests, and report artifacts should be returned as paths or metadata by future workflow tools rather than inlined through MCP responses.

## Safety policy

MCP callers are untrusted transport clients. The server applies these boundary rules before delegating to core acquisition code:

- URLs must be HTTP(S), must not include embedded credentials, and must not point at localhost, private, loopback, link-local, or reserved IP ranges.
- If no reviewed acquisition profile is supplied, `allowed_domains` defaults to the input URL host so final URL redirects cannot silently produce usable cross-domain data.
- If `profile_path` is supplied, it is treated as the complete reviewed safety policy. Inline `safety` or `allowed_domains` overrides are rejected, including falsy override values such as an empty allowlist.
- `cloakbrowser` is only available when the active profile allows stealth browser access and requires authorized access.
- MCP responses must not reflect secrets. Profile payloads are sanitized before return; adapter `config` and adapter `safety` dictionaries are not exposed.
- Validation errors must not echo caller-supplied secrets, tokens, cookies, authorization headers, credentials, or local paths.

## ToolResult interpretation

Agents should treat `ToolResult` as the only stable response contract:

- `ok=false` means the tool failed or stopped for a terminal reason.
- `has_data=false` means the returned data is not sufficient for the requested goal, even when the tool call itself was valid.
- `data_status` explains whether data is `present`, `not_found`, `blocked`, `permission_denied`, `auth_required`, `not_applicable`, or `error`.
- `quality_gates.requested` records caller thresholds; `quality_gates.effective` records the thresholds actually applied.
- `attempts` is the audit trail for fallback decisions.
- `next_tool` and `next_action` are suggestions, not commands; the agent must still respect safety policy.

## Non-goals for this MCP layer

- It does not change the staged `bootstrap-scope` or `run-scope` workflow semantics.
- It does not expose every internal crawler function as an MCP tool.
- It does not automatically use stealth or authorized acquisition tools for arbitrary public URLs.
- It does not inline large artifacts by default.

Future PRs may add workflow-oriented MCP tools for bootstrapping scopes, reading safe artifacts, and exporting manifests.