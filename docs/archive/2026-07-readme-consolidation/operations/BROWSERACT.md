# BrowserAct runtime policy

BrowserAct is an optional, disabled-by-default acquisition runtime. It is not part of the project package, is not an automatic fallback, and is not used by `bootstrap-scope` or `run-scope`.

## Supported runtime

- `browser-act-cli==1.0.6`
- Python 3.12.x
- a dedicated tool virtual environment whose `sys.prefix` differs from the web_listening project environment
- an executable and Python interpreter contained by that dedicated prefix

Never install BrowserAct in the project `.venv`. A Linux operator may create a disposable tool environment outside the repository, for example under `/tmp`, and install the exact pin there. A real install or live smoke is optional and must not require an account, authentication, CAPTCHA service, proxy, or browser interaction.

## Discovery and inspection

Discovery is deterministic:

1. an explicit absolute `--executable` path;
2. an executable found in the caller-supplied controlled `--path`;
3. structured unavailable status.

The ambient `PATH` is never searched. Inspection is read-only:

```bash
web-listening inspect-browseract --executable /tmp/browseract-tool/bin/browser-act --json
web-listening inspect-browseract --path /tmp/browseract-tool/bin --json
```

The handshake reads the executable's absolute shebang interpreter without importing BrowserAct into the project process. It runs `browser-act --version`, asks that interpreter for Python major/minor, `sys.prefix`, and the installed `browser-act-cli` metadata version, then uses bounded `--help` probes to verify the real read-only command surface. It must report BrowserAct 1.0.6, Python 3.12, an isolated tool prefix, `read_only: true`, and both `stealth_extract` and `interactive_read` capabilities. Executable and shebang paths are checked lexically under the tool prefix so a standard virtual-environment interpreter symlink is not resolved outside that prefix.

## Safety boundary

Only the fixed `stealth_extract` and `interactive_read` recipes are accepted. `stealth_extract` invokes global `--format json`, `stealth-extract`, a request URL, `--content-type html|markdown`, and bounded `--timeout`/`--render-wait` values; proxy and output flags are never passed. Interactive reads require a validated non-secret `browser_id`, generate a bounded session name, and invoke only `browser open` with the initial request URL, `wait stable`, optional bounded `scroll` and stable-wait operations, `get html|markdown`, `state` to read the actual current URL from the pinned CLI's state-only `url` field, and `session close` in cleanup.

The gateway does not expose click, later navigation, input, keys, select, eval, upload, cookies/auth, proxy, CAPTCHA, browser create/update/delete/renew/import, arbitrary argv, or output paths. Every nested BrowserAct command uses the same concurrent bounded reader: stdout is capped at the outer protocol-compatible 4 MiB maximum and stderr at a 64 KiB diagnostic ceiling before allocation can grow beyond those limits. Timeout or overflow kills and reaps the immediate child; outer cgroup cleanup remains authoritative for descendants. Failures are stable and sanitized, and stderr, excess output, and request secrets are never returned.

BrowserAct 1.0.6 `stealth-extract` reports `url` as the input URL, so that field is not accepted as redirect lineage on the stealth response. A successful stealth capture requires exactly one unambiguous, independently reported string named `final_url` or `current_url` (including in a single `data` object). For `interactive_read` only, the separately queried BrowserAct 1.0.6 `state` response's `url` field is the trusted current URL. Therefore pinned 1.0.6 stealth extraction is fail-closed and **No-Go** unless the runtime can independently report a final/current URL. This optional-runtime No-Go does not block later roadmap work.

The fake CLI contract tests are the required implementation verification. The opt-in live test runs only when `WEB_LISTENING_BROWSERACT_LIVE` and an absolute `WEB_LISTENING_BROWSERACT_EXECUTABLE` are supplied.
