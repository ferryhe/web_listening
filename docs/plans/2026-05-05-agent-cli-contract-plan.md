# Web Listening 输出规范化与 Agent CLI Contract Implementation Plan

> **For Hermes/Codex:** Use the project-isolated Codex worker pattern. Read `AGENTS.md` and `.hermes/project-status.md` before each run. Do not commit, push, or open PRs without explicit approval.

**Goal:** 把 web_listening 的采集结果收口为稳定、可追溯、可增量消费的 machine-readable artifact。

**Architecture:** 保持现有 staged workflow，不重写采集逻辑；新增/收敛统一 manifest schema、status JSON、artifact layout 和跨模块 handoff contract。

**Tech Stack:** Project-native stack plus CLI-first JSON/JSONL manifests. Python projects should use Typer/Pydantic where already present; TypeScript projects should preserve pnpm/OpenAPI workflow.

---

## Context

This repository is one module in the broader agent-operated knowledge pipeline:

```text
web_listening -> doc_to_md -> md_to_rag -> rag_to_agent/domain adapters -> ai_interface
```

Current project role: 数据采集/网站监听 CLI，负责 discover -> classify -> scope -> bootstrap/run -> report/export manifest。

Current planning scope: 规范化采集输出 manifest，让 downstream doc_to_md 和 agent console 可以稳定消费。

## Non-Negotiable Contracts

1. CLI outputs must be machine-readable and stable (`--json` where applicable).
2. Artifacts must be path-portable and manifest-driven.
3. Reruns must be idempotent.
4. Every derived artifact must preserve provenance back to its input.
5. Secrets/API keys must never be written into manifests or committed files.
6. Cross-repo integration happens through files/manifests/tool specs, not hidden imports.

## Proposed Tasks

### Task 1: 盘点现有输出入口

**Objective:** 阅读 web_listening/cli.py、docs/README.md、现有 artifacts/report/export 逻辑，列出现有 YAML/Markdown/JSON 输出。

**Files:**
- Modify/Create project-specific files identified during the task.
- Update tests or fixtures for the changed contract.

**Steps:**
1. Inspect the current implementation and write down exact files touched.
2. Add or update the smallest contract/test fixture first.
3. Implement the minimal change.
4. Run the focused verification command.
5. Update `.hermes/project-status.md` with result and next action.

**Verification:** 只读检查：`python -m pytest -q` 或现有 smoke；输出一张 current-output inventory。

### Task 2: 定义采集 manifest v1

**Objective:** 新增 docs/contracts/web-listening-manifest-v1.md，定义 run/job/source/discovered_item/downloaded_asset/checksum/status 字段。

**Files:**
- Modify/Create project-specific files identified during the task.
- Update tests or fixtures for the changed contract.

**Steps:**
1. Inspect the current implementation and write down exact files touched.
2. Add or update the smallest contract/test fixture first.
3. Implement the minimal change.
4. Run the focused verification command.
5. Update `.hermes/project-status.md` with result and next action.

**Verification:** 文档包含 JSON 示例、必填字段、兼容策略。

### Task 3: 实现统一 export 命令适配

**Objective:** 收敛 `web-listening export-manifest` 输出为 contract v1；保留旧字段但标 deprecated。

**Files:**
- Modify/Create project-specific files identified during the task.
- Update tests or fixtures for the changed contract.

**Steps:**
1. Inspect the current implementation and write down exact files touched.
2. Add or update the smallest contract/test fixture first.
3. Implement the minimal change.
4. Run the focused verification command.
5. Update `.hermes/project-status.md` with result and next action.

**Verification:** 新增 focused tests 验证 schema keys、idempotent rerun、相对路径可移植。

### Task 4: 补 status/inspect JSON

**Objective:** 让关键命令支持 `--json`，返回 status/artifact path/counts/errors，而不是只靠自然语言日志。

**Files:**
- Modify/Create project-specific files identified during the task.
- Update tests or fixtures for the changed contract.

**Steps:**
1. Inspect the current implementation and write down exact files touched.
2. Add or update the smallest contract/test fixture first.
3. Implement the minimal change.
4. Run the focused verification command.
5. Update `.hermes/project-status.md` with result and next action.

**Verification:** 测试 CLI runner 输出可 json.loads。

### Task 5: 跨模块 handoff smoke

**Objective:** 准备一个小型 sample job，验证 manifest 能被 doc_to_md 测试 fixture 消费。

**Files:**
- Modify/Create project-specific files identified during the task.
- Update tests or fixtures for the changed contract.

**Steps:**
1. Inspect the current implementation and write down exact files touched.
2. Add or update the smallest contract/test fixture first.
3. Implement the minimal change.
4. Run the focused verification command.
5. Update `.hermes/project-status.md` with result and next action.

**Verification:** 生成 docs/testing/web-listening-contract-smoke.md。


---

## Acceptance Criteria

- A Codex worker can understand this repo's boundary from `AGENTS.md`.
- A future implementation branch can start from this plan without needing cross-chat context.
- The module's input/output contract is explicit enough for the next module in the chain.
- All new behavior is testable through CLI commands and fixture manifests.

## Recommended First PR

Start with documentation/contracts and fixture-only changes. Do not implement all runtime behavior in the first PR. The first PR should make the intended contract reviewable before code follows.
