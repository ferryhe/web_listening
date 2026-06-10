# Project Agent Boundary: web_listening

## Identity

- Project: `web_listening`
- Repo path: `/root/.hermes/projects/web_listening`
- Remote: `https://github.com/ferryhe/web_listening`
- Worker role: 数据采集/网站监听 CLI，负责 discover -> classify -> scope -> bootstrap/run -> report/export manifest。

## Project Boundary

1. `/root/.hermes/projects/web_listening` is the only writable workspace for this project.
2. Do not read, edit, or infer requirements from sibling repositories unless the current task explicitly names them.
3. Do not copy secrets, local `.env` files, generated credentials, generated data artifacts, or unreviewed artifacts between projects.
4. Keep changes narrow and project-scoped. For cross-project contracts, edit only this repo's side unless the task explicitly covers multiple repos.
5. Before editing, run `git status --short --branch` and identify unrelated local changes.

## Mandatory Multi-PR Gated Workflow

Boss requires the following workflow for every implementation PR unless the task explicitly says otherwise:

1. Start from a fresh, clean `main` branch tracking latest `origin/main`.
2. Create a focused task branch for the worker implementation.
3. Implement only the scoped worker changes for that PR.
4. Run the required local verification for the touched area.
5. Run the mandatory reviewer-agent gate before PR creation/update (see `Pre-PR Reviewer Agent Gate`).
6. Commit, push, and create/update the PR only after implementation, local verification, and reviewer gate are clean.
7. After PR creation/update, wait about **10 minutes** for CI, Copilot, and remote review comments.
8. Evaluate all comments on merit; fix only technically valid, in-scope comments.
9. Rerun focused/full validation as appropriate and push follow-up fixes.
10. Merge only when CI and valid review/Copilot comments are clean.
11. After merge, delete the completed task branch when branch deletion is in scope for the run.
12. Sync local `main` back to latest `origin/main` before starting the next PR.

Do not skip the reviewer gate or the 10-minute CI/Copilot/review wait. Destructive actions such as force-push, history rewrite, or broad cleanup require explicit approval from 北老师.

## Branch Policy

- `main` is the clean baseline tracking `origin/main`.
- New work starts from latest clean `main` on a task branch.
- Do not implement directly on `main`.
- Keep each PR focused; use multiple gated PRs for separable setup, core implementation, MCP adapter, and docs/example work.
- Commit/push/create PR automatically after implementation and required verification pass, unless the current task explicitly says not to commit/push/create a PR.
- Delete task branches only after the PR is merged and branch deletion is in scope.

## Required Startup Routine

Every Codex worker run must:

1. Read this `AGENTS.md`.
2. Read `.hermes/project-status.md` if present.
3. Run `git status --short --branch`.
4. Restate the active repo, branch, files in scope, and whether sibling repos are off-limits.
5. Only then edit files.

## Verification Policy

- Prefer focused tests for touched code.
- Required verification includes the focused/full checks identified by this Verification Policy and the Pre-PR Reviewer Agent Gate below; if required checks fail or cannot run, do not treat verification as passed, and record the exact blocker and next command.
- For documentation/setup-only PRs, run at least lightweight checks such as `git diff --check`.
- For CLI contract changes, verify `--help`, `--json` output, schema fixtures, and idempotent reruns.
- For frontend/API work, run local service/build checks and browser smoke when UI behavior changes.
- If checks cannot run, record the exact blocker and the next command to run.

## Pre-PR Reviewer Agent Gate

After development and local verification are complete, but before creating or updating a PR, run one or more fresh read-only Hermes reviewer agents against the current branch and the PR base. Treat this as the mandatory local review gate for this project; do **not** use Codex CLI review as the gate.

Reviewer-agent coverage must include:

1. Spec compliance: requested behavior, scope control, and product/architecture constraints.
2. Code/document quality: maintainability, tests, safety/security, and project conventions.

For PRs that target `main`, reviewers should compare against latest `origin/main`. If there is no open PR yet, use `main` unless the task explicitly names another base. Reviewer agents must not modify files.

Evaluate reviewer-agent findings the same way as remote review comments: accept only technically correct, in-scope findings; make the necessary fixes; rerun focused/full verification; then re-run reviewer agents until PASS/APPROVED. If reviewer agents cannot run because of tooling, quota, network, or any other blocker, the review gate is **not clean**: do not create or update the PR, commit/push a review-gated update, or otherwise proceed past the gate unless 北老师 explicitly approves bypassing that specific blocker. Record the blocker, the attempted review, and the next command/action to retry in the final report.

## Status Cadence

For long or multi-step runs, provide regular concise status updates after major phases such as startup/context gathering, edits, verification, reviewer-gate results, and final handoff. Keep updates brief and factual: current step, changed files or checks, blockers if any, and next action.

## Reporting Format

End every run with:

- Project and branch
- Files changed
- Checks run and results
- Uncommitted/untracked files noticed
- Blockers or decisions needed
- Recommended next action

For non-read-only runs that edit project files, also update `.hermes/project-status.md` with the latest state. Read-only inspection/review tasks should leave project status unchanged unless the task explicitly requests a status update.
