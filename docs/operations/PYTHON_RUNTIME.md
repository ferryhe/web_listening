# Python Runtime Compatibility

> Status: Active operations record
> Official runtime: Python 3.12.x (`>=3.12,<3.13`)

## Runtime contract

Development, CI, and production deployments use Python 3.12.x. The package
metadata is the installation authority and rejects Python 3.11 and Python 3.13.
Create a fresh project virtual environment after selecting an approved Python
3.12 installation; do not reuse an environment created by another Python minor
version.

## Dependency installation matrix

All commands below require Python 3.12.x and may use `-e` before `.` for an
editable development checkout. **Core has no extra**: use `pip install .`, not
an invented `[core]` extra.

| Installation | Command | Adds to core |
|---|---|---|
| Core | `python -m pip install .` | Nothing; this is the base dependency set and has no extra |
| Development/test | `python -m pip install ".[dev]"` | pytest, pytest-asyncio, and MCP development support |
| Rendered browser | `python -m pip install ".[browser]"` | Playwright; then run `python -m playwright install chromium` |
| CloakBrowser probe | `python -m pip install ".[cloakbrowser]"` | CloakBrowser for explicitly authorized acquisition probing |
| Browser + CloakBrowser | `python -m pip install ".[browser,cloakbrowser]"` | Both optional acquisition runtimes |
| Development + all browser runtimes | `python -m pip install ".[dev,browser,cloakbrowser]"` | Test tooling plus both optional acquisition runtimes |

Extras are additive: every row installs the core dependencies as well as the
listed optional dependencies. Use only the extras needed by that environment.

Windows remains a supported operator path. Use `py -3.12 --version` and
`py -3.12 -m venv .venv`, then run commands through `.venv\Scripts\python` or
the activated environment. On Linux/macOS, use the platform's approved Python
3.12 executable and `.venv/bin/python`.

## Unsupported-runtime installation check

CI builds the wheel with Python 3.12, switches to Python 3.11, and runs this
exact negative check against that same artifact (substitute the single wheel
filename produced in `dist/`):

```bash
python -m pip install --no-deps dist/web_listening-0.1.0-py3-none-any.whl
```

Expected result: the command exits nonzero and pip reports that
`web-listening` requires a different Python because Python 3.11 does not satisfy
`Requires-Python: >=3.12,<3.13`. Any successful installation, or a failure
without that compatibility diagnostic, fails CI. `--no-deps` keeps the check
focused on this project's wheel metadata rather than dependency resolution.

## Deployment and reproducibility inventory

| Item | Repository state | Applicability |
|---|---|---|
| Package/CLI entrypoints | `web-listening` and `web-listening-mcp` from `pyproject.toml`; `tools/*.py` are compatibility/developer wrappers | Development, CI, production |
| API/service entrypoint | `web-listening serve`; MCP stdio via `web-listening-mcp` | Development and production when enabled |
| CI | `.github/workflows/ci.yml` builds wheel/sdist, installs the wheel, and runs tests on Python 3.12.x | CI |
| Containers / Dockerfiles | N/A — none are tracked | N/A |
| Service-unit or supervisor scripts | N/A — none are tracked | N/A |
| Deployment manifests | N/A — none are tracked | N/A |
| Dependency lock files | N/A — no `uv.lock`, `poetry.lock`, Pipfile lock, or frozen requirements file is tracked | N/A |
| Windows launch path | Python launcher plus project `.venv\Scripts`; packaged CLI entrypoints | Development and operator-run production |

No `uv.lock` is added in this baseline PR. The project currently publishes
bounded Python compatibility metadata but uses minimum dependency constraints
and has no established lock/update workflow. Adding a lock without defining its
target platforms, update cadence, and CI consumption would not improve the
current deployment path demonstrably. The repository maintainer owns the
follow-up decision when a reproducible deployment artifact or release workflow
is introduced. Until then, CI's clean wheel build/install is the compatibility
check; deployed environments should retain their resolved dependency record as
part of their build artifact.

## Environment boundaries

The project virtual environment and the BrowserAct tool virtual environment
may share the same approved host Python 3.12 installation. They
must have independent site-packages, console entrypoints, upgrades, and
lifecycle management. A shared `uv` download/build cache is only an efficiency
mechanism; it is not a runtime isolation or security boundary. BrowserAct
installation remains optional and the inspected runtime stays isolated from the project.

## Rollback and change boundary

Rollback means redeploying the previous application build together with its
previous Python 3.11 environment. Do not downgrade Python in place or reuse the
Python 3.12 virtual environment for rollback.

This runtime-baseline change does not alter database schema, stored data
formats, collection behavior, or business/API protocols.
## Optional BrowserAct tool runtime

BrowserAct must use a separate Python 3.12 tool environment and the exact runtime pin `browser-act-cli==1.0.6`. It must never be added to project dependencies or installed into the project `.venv`. See [BROWSERACT.md](BROWSERACT.md) for isolation and handshake requirements.
