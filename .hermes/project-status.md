# Project Status

- Date: 2026-06-10
- Project: web_listening
- Repo path: `/root/.hermes/projects/web_listening`
- Branch: `feat/acquisition-fallback-core`
- Run type: PR3 implementation for core acquisition fallback engine only.
- Scope: added shared core fallback execution in `web_listening.blocks`; no MCP server, CLI, or API in this PR.
- Workspace boundary: only `/root/.hermes/projects/web_listening` is in scope; sibling repositories are off-limits unless explicitly named by a task.
- Starting state: `git status --short --branch` showed clean branch `feat/acquisition-fallback-core`.
- Changes: added `web_listening/blocks/acquisition_fallback.py` with `acquire_with_fallback_result`, default strategy chains, continuation logic, quality gate/profile resolution, allowed-domain/input/final-url safety checks, reserved-adapter skipped attempts, status-code terminal mapping that clears usable data for terminal HTTP statuses, and structured-error escalation requiring both `retryable` and `safe_to_escalate`; updated capture/profile helpers so successful attempts expose safe inline previews and `allowed_domains` accepts sequence inputs; added `tests/test_acquisition_fallback.py` with fake-adapter/no-network coverage for fallback success, quality-gate override behavior, reserved adapter skips, all-fail history, max attempts, domain safety, not-found/auth/permission terminal behavior, preview availability, and structured-error escalation.
- Verification: `git diff --check` passed. `python -m pytest tests/test_acquisition_fallback.py tests/test_tool_result.py tests/test_acquisition_capture.py tests/test_acquisition_profile.py -q` passed (51 passed, 0.52s). `python -m pytest -q` passed (272 passed, 22.05s).
- Reviewer gate: Hermes reviewer-agent gate completed after two fix loops; final spec/scope reviewer PASS and final code quality/security/maintainability reviewer PASS.
- Next recommended action: commit, push, create PR, then wait for CI/Copilot/review feedback before merge.
