# web_listening Agent-Facing Tracking Tool 评估

## 评估范围

- 仓库：`https://github.com/ferryhe/web_listening`（本地 checkout）
- 分支：`main`
- 评估时间：`2026-05-05`（America/New_York）
- 约束：只基于仓库内真实文件与本地命令；不修改既有代码逻辑，不做 commit/push

本报告重点回答 7 个问题：

1. 项目定位是否已经是完全面向 agent 的网站跟踪、分析、报告工具
2. 输入契约是否清楚稳定
3. 输出契约是否清楚稳定，且适合 agent handoff
4. README/docs 叙事是否清楚
5. Hermes skill / AGENTS / CLI 是否足够 agent-friendly
6. CLI 主路径、legacy 路径、可发现性和机器可消费性如何
7. 当前缺口、风险和优先级建议

---

## 执行证据

本次判断主要基于以下文件和命令：

- `README.md`
- `docs/README.md`
- `docs/contracts/web-listening-manifest-v1.md`
- `docs/testing/web-listening-contract-smoke.md`
- `.codex/skills/web-listening-agent/SKILL.md`
- `.codex/skills/web-listening-agent/references/current-api.md`
- `.codex/skills/web-listening-agent/references/interface-strategy.md`
- `docs/skills/OPENCLAW_SKILL_USAGE.md`
- `web_listening/cli.py`
- `web_listening/models.py`
- `web_listening/blocks/monitor_scope_planner.py`
- `web_listening/blocks/document_manifest.py`
- `web_listening/blocks/tracking_report.py`
- `web_listening/blocks/staged_workflow.py`
- `tests/test_cli.py`
- `tests/test_document_manifest.py`
- `tests/test_manifest_contract_fixture.py`
- `tests/test_tracking_report.py`

执行过的本地命令：

```bash
git status --short --branch
python -m web_listening.cli --help
python -m pytest tests/test_cli.py tests/test_document_manifest.py tests/test_manifest_contract_fixture.py tests/test_tracking_report.py -q
git diff --check
```

---

## 一、总体结论

### 结论摘要

`web_listening` 现在已经明显不是“普通人手工用的网页监控脚本”，而是一个**强烈朝向 agent 工作流设计**的 staged 网站监听平台。它已经具备：

- 文件化 control plane：`section_selection.yaml`、`monitor_scope.yaml`、`monitor_task.yaml`
- 证据化 evidence plane：SQLite、`_blobs`、`_tracked`
- handoff contract：`job_delivery.v1`、`artifact_contract.v1`、`web-listening-manifest.v1`
- 面向 agent 的解释层：tracking report、bootstrap summary、document manifest
- 对稳定 artifact 的显式测试覆盖

但它**还不是“完全面向 agent”**。更准确地说，它是：

- **定位上：是 agent-first 倾向的 staged monitoring 工具**
- **接口上：是 agent-friendly，但尚未完全统一**
- **叙事上：PR1 前存在明显文档滞后和双轨表述冲突，本 PR 已开始收敛 authority map**

### 最终评级

- 结论：**不是“完全面向 agent”**
- 当前等级：**A- / 7.8 分（接近 agent-first production candidate，但尚未 fully agent-native）**

### 为什么不是“完全”

核心缺口不是爬虫能力，而是**接口与叙事的一致性**：

- staged CLI 已经存在，但 PR1 前 skill/reference 文档仍把它描述成“主要依赖 tools/*.py，CLI 还是 legacy”
- 部分命令支持 `--json`，部分不支持，机器可消费性不一致
- PR1 前 README / docs 中仍保留 Windows 绝对链接；本 PR 已将其改为仓库相对链接
- REST 仍主要覆盖 legacy site-level，不是 staged flow 的统一对外面
- 还没有单一、权威、面向 agent 的“主入口契约说明”

---

## 二、项目定位：是否是完全面向 agent 的网站跟踪、分析、报告工具

### 支持“面向 agent”的证据

`README.md:3-18` 直接把项目定义为 “for human operators and AI agents”，并把产品方向明确成：

```text
discover -> classify -> select -> task -> bootstrap -> run -> report -> manifest
```

这不是传统“输入 URL 然后抓网页”的设计，而是更接近 agent workflow orchestration。`README.md:97-128` 还明确要求：

- 先做 smoke / tree validation
- 再产出 draft `section_selection.yaml` 与 `monitor_scope.yaml`
- 再交给人确认
- 然后才 bootstrap / rerun / report

这说明产品边界不是“自动监控所有东西”，而是“让 agent 在受控 scope 内生成可复核的持续监听工件”。

`.codex/skills/web-listening-agent/SKILL.md:12-19` 也把系统拆成三层：

- control plane: YAML planning artifacts
- evidence plane: SQLite plus downloaded files
- explanation plane: Markdown reports

这三层划分非常 agent-facing，因为它把“计划、证据、解释”从 prompt memory 中剥离到了磁盘 artifacts。

### 不支持“完全 agent-native”的证据

虽然 `web_listening/cli.py:359-862` 已经把 staged flow 暴露为 packaged CLI 命令，但 PR1 前 skill/reference 仍在输出旧叙事：

- `.codex/skills/web-listening-agent/SKILL.md:30-41` 曾优先推荐 `tools/*.py`
- `.codex/skills/web-listening-agent/references/current-api.md:50-61` 曾说 packaged CLI 只新增少量 staged artifact commands
- `.codex/skills/web-listening-agent/references/current-api.md:65-80` 和 `interface-strategy.md:5-18` 曾说 tree workflow 主要通过 `tools/*.py`
- `docs/skills/OPENCLAW_SKILL_USAGE.md:40-44` 曾明确写着 staged workflow “It is not yet exposed as a first-class REST or packaged CLI workflow.”

这和 `README.md:26-38`、`docs/README.md:35-46`、`tests/test_cli.py:18-36` 体现的现实不一致；本 PR 已将这些 repo-facing 说明更新为 packaged CLI 优先。

### 定位判断

定位上，这个项目已经是：

- **agent-oriented**
- **artifact-first**
- **scope-driven**
- **evidence-preserving**

但还不是：

- **single-surface**
- **single-contract**
- **single-story**

所以它现在是“强 agent 倾向的 staged tracking platform”，不是“完全面向 agent 的统一 tracking console / protocol endpoint”。

---

## 三、输入契约评估

### 1. 输入契约的主结构是清楚的

当前 staged 输入契约的核心链路很明确：

1. catalog / site target
2. `section_selection.yaml`
3. `monitor_scope.yaml`
4. 可选 `monitor_task.yaml`
5. CLI flags 覆盖局部运行参数

`web_listening/blocks/monitor_scope_planner.py:24-37` 定义了 `SectionSelection`，其关键字段包括：

- `site_key`
- `selection_mode`
- `review_status`
- `business_goal`
- `selected_sections`
- `rejected_sections`
- `deferred_sections`
- `excluded_categories`
- `excluded_prefixes`

`web_listening/blocks/monitor_scope_planner.py:41-69` 定义了 `MonitorScopePlan`，其关键字段包括：

- `scope_fingerprint`
- `site_key`
- `catalog`
- `seed_url`
- `homepage_url`
- `fetch_mode`
- `fetch_config_json`
- `file_scope_mode`
- `allowed_page_prefixes`
- `allowed_file_prefixes`
- `selected_focus_prefixes`
- `max_depth`
- `max_pages`
- `max_files`

`load_section_selection()` 与 `load_monitor_scope_plan()`（`web_listening/blocks/monitor_scope_planner.py:175-244`）把 YAML 读入为明确结构；`fetch_config_json` 不是对象时还会直接报错，这对 agent 很重要，因为失败边界清楚。

### 2. CLI 参数总体上是显式、文件导向的

staged CLI 的输入面基本围绕显式文件和显式参数：

- `discover`：`--catalog`、`--site-key`、`--max-depth`、`--section-depth`、`--max-pages`
- `classify`：`--catalog`、`--inventory-path`、`--site-key`、`--use-ai`
- `select`：`--selection-path`
- `plan-scope`：`--selection-path`、`--classification-path`、`--file-scope-mode`
- `bootstrap-scope` / `run-scope`：`--scope-path` 加预算覆盖
- `report-scope` / `export-manifest`：`--scope-path` 加可选 `--run-id`
- `create-monitor-task`：显式声明 task name、site url、goal、focus 等

对应位置见 `web_listening/cli.py:359-862`。

这类输入方式对 agent 很友好，因为：

- 不依赖交互式问答
- 不依赖隐式状态
- 可以从文件恢复上下文
- 可被上层编排器重放

### 3. 输入契约的不足

#### 3.1 缺少单一 intent artifact

roadmap 已经承认这点：`.codex/skills/web-listening-agent/references/agent-roadmap.md` 提到下一步要加 `monitor_intent.yaml`。当前 intent 被拆散在：

- `selection.business_goal`
- `monitor_scope.*`
- `monitor_task.goal`
- `monitor_task.focus_topics`

对于 agent 来说，这意味着“为什么监控”和“监控哪些边界”仍然分散在多个文件中。

#### 3.2 校验策略不完全一致

`create-monitor-task` 会对 `site_url` 做 `_validate_http_url()` 校验，见 `web_listening/cli.py:790-804`；但 legacy `add-site` 并没有同级别 URL 校验，`fetch_mode`、`fetch_config` 的严谨度也与 staged 契约不完全对齐。

这说明项目内部仍存在“legacy 宽松输入”和“staged 显式输入”两套风格。

#### 3.3 review 状态是契约的一部分，但 CLI 没有强制 gate

`README.md:127-128` 和 `docs/README.md:82-83` 强调 draft scope 不是自动批准的生产 scope；`SectionSelection.review_status` 也明确存在。但 `plan-scope` / `bootstrap-scope` CLI 本身没有强制“未 review 不允许继续”的执行闸门。

这意味着流程规范主要靠文档与操作纪律，而不是强制契约。

### 输入契约结论

输入契约已经具备**agent-first 文件化编排**的核心特征，优于大量“只吃 CLI flags”的工具。但还缺：

- 单一 intent artifact
- 更统一的参数校验
- 更强的 review gate

评级：**8.2 / 10**

---

## 四、输出契约评估

### 1. 输出契约是本项目最强的一部分

`docs/contracts/web-listening-manifest-v1.md:3-21` 明确把 `web-listening-manifest.v1` 定义为稳定、机器可读、可 handoff 的下游契约；并且明确区分：

- runtime JSON artifact：`web_listening_manifest_<site>_<date>.json`
- CLI/API wrapper：`job_delivery.v1`
- artifact pointer layer：`artifact_contract.v1`

这是很成熟的分层思路，不把“作业状态”和“业务 handoff payload”混在一起。

### 2. `Job` 输出层对 agent 很友好

`web_listening/models.py:172-245` 的 `Job` 模型提供：

- `next_recommended_action()`
- `artifact_contract()`
- `to_delivery_payload()`

这使 `--json` 输出不仅有 job status，还有：

- `primary_kind`
- `primary_path`
- `path_map`

对 agent 来说，这比“输出一段成功文本”强很多，因为它告诉下游“下一步应该读什么文件”。

### 3. manifest handoff 设计是稳定且 portable 的

`docs/contracts/web-listening-manifest-v1.md:43-68`、`181-250` 以及 `web_listening/blocks/document_manifest.py:37-78`、`289-340` 显示出几个关键设计：

- `artifact_root` + 相对路径，避免机器本地绝对路径泄漏
- `discovered_items[]` 和 `downloaded_assets[]` 分开
- 保留 `source_item_id`、`sha256`、`page_url`、`download_url`
- 明确 `_tracked` 是浏览优先路径，`_blobs` 是 canonical store
- `idempotency_key = source_id | scope_fingerprint | run_id`

这正是 agent handoff 最需要的稳定性与 provenance。

`tests/test_document_manifest.py:174-212` 还验证了：

- `artifact_root == "."`
- `discovered_items` 可包含 remote-only file link
- `downloaded_assets.local_path` 优先输出 `_tracked`
- `canonical_blob_path` 仍保留 `_blobs`
- `run.input_paths` 不是绝对路径

这些测试不是装饰性测试，而是真正绑定了 handoff contract 的设计意图。

### 4. tracking report 兼顾人读与 agent 读

`web_listening/blocks/tracking_report.py:19-59` 的 `TrackingReport` 包含：

- `new_pages` / `changed_pages` / `missing_pages`
- `new_files` / `changed_files` / `missing_files`
- `review_queue`
- `artifact_index`
- `documents`
- `recommended_next_actions`

再加上 `tracking_report.py:77-91` 的下一步动作建议，说明它不是单纯的 Markdown 总结，而是带有操作导向的结构化解释层。

### 5. 输出契约的不足

#### 5.1 JSON 支持不一致

- `bootstrap-scope`、`run-scope`、`report-scope`、`export-manifest`、`get-job`、`create-monitor-task` 有 `--json`，见 `web_listening/cli.py:469-823`
- `discover`、`classify`、`select`、`plan-scope`、`list-jobs`、`export-tracking-report` 没有 `--json`

这会造成一个问题：前半段规划工序和某些报告导出步骤仍需要 agent 解析人类文本或直接读文件系统，不是完整统一的 JSON-first surface。

#### 5.2 `report-scope` 与 `export-tracking-report` 语义重叠

`report-scope` 可以导出 `md|yaml`，还有 `--json` wrapper；`export-tracking-report` 也能导出 `md|yaml`，但没有 `--json`，见 `web_listening/cli.py:578-630` 与 `822-862`。

从 agent 视角看，这两个命令容易被误解为：

- 一个是 staged report
- 一个是 unified report

但边界在 CLI help 层面不够强，属于 discoverability 风险。

#### 5.3 Markdown 产物仍带有人类叙事偏置

例如 `document_manifest` 的 Markdown 仍然是“Final Conclusion + 表格”，见 `web_listening/blocks/document_manifest.py:250-286`。这本身没问题，但如果 agent 误把 Markdown 当一等输入，而不是 JSON/YAML，会受到格式漂移影响。

### 输出契约结论

输出契约是这个项目最接近“完全面向 agent”的地方，尤其是 manifest 与 job wrapper 的组合已经很成熟。

评级：**8.8 / 10**

---

## 五、README / docs 叙事是否清楚

### 清楚的部分

`README.md` 与 `docs/README.md` 在“当前主 workflow 是 staged tree monitoring”这件事上已经很清楚：

- `README.md:26-38` 列出 staged packaged commands
- `README.md:99-100` 明确 packaged CLI 是 primary entrypoint
- `docs/README.md:31-57` 把 packaged staged workflow 与 lower-level compatibility entrypoints 并列列出

如果只看这两份文档，读者会得出正确结论：**staged CLI 现在已经是主入口，tools 是兼容层**。

### 不清楚的部分

#### 5.1 skill/reference 文档明显滞后（PR1 已开始处理）

这是 PR1 前最大的叙事问题。

PR1 前，同一个仓库同时存在两套说法：

- 新说法：`README.md:99-100`，“packaged CLI is now the primary entrypoint”
- 旧说法：`.codex/skills/web-listening-agent/SKILL.md:30-41`，“prefer the staged tool flow”
- 旧说法：`current-api.md:78-80`，“not yet exposed as first-class packaged CLI commands”
- 旧说法：`OPENCLAW_SKILL_USAGE.md:40-44`，“not yet exposed as a first-class REST or packaged CLI workflow”

对于 agent 来说，skill/reference 往往比 README 更直接驱动行为，所以这类滞后会直接误导操作路径。本 PR 已把 skill/reference 的主入口叙事改为 packaged CLI 优先。

#### 5.2 Windows 绝对链接污染仓库文档（PR1 已处理）

PR1 前，`README.md:20` 和 `docs/README.md:5-23,97` 使用了 `C:/Project/...` 风格链接。这在当前 Linux 工作区中不是可移植链接，也不利于 agent 或人类在 GitHub / 本地仓库里直接跳转。

本 PR 已将这些链接改为仓库相对链接，因此该项在当前 PR 范围内已完成修复。

#### 5.3 legacy / staged 双轨叙事还不够收敛

`README.md:236-255` 试图同时说明：

- legacy 命令仍在
- staged packaged CLI 已经存在
- tools 仍是兼容入口

这方向没错，但仍缺少一段更硬的“authority map”：

- 对 agent：默认用什么
- 对 human operator：什么时候用哪个
- 对 automation：稳定 JSON 的首选命令是哪几个

### 文档叙事结论

主 README 方向基本正确；PR1 已收敛 skill/reference 的主入口叙事，但后续仍需要继续补齐 JSON-first 契约等接口层问题。

评级：**6.8 / 10**

---

## 六、Hermes skill / AGENTS / CLI 是否足够 agent-friendly

### 1. `AGENTS.md` 是 agent-friendly 的

仓库级 `AGENTS.md` 要求：

- 启动时读 `AGENTS.md`
- 读 `.hermes/project-status.md`
- 执行 `git status --short --branch`
- restate active repo / branch / scope
- 最后更新 `.hermes/project-status.md`

这对多 agent / 长会话协作是有帮助的，尤其 `.hermes/project-status.md` 记录了上一轮 PR、验证命令和 manifest 变更范围，能降低重复上下文成本。

### 2. Hermes skill 的理念是对的，但当前内容落后于 CLI 现实

`.codex/skills/web-listening-agent/SKILL.md:12-19` 的三层模型很好，说明它理解本项目不是“一个爬虫脚本”，而是 artifact system。

问题在于 `SKILL.md:30-41` 把推荐工作流仍放在 `tools/*.py`，并让 packaged CLI 退回到 legacy 层。这已经与 `cli.py` 实际情况不符。

所以 PR1 前 skill 的问题不是“没有 agent 思维”，而是“信息过期”。本 PR 已将推荐工作流更新为 packaged CLI 优先。

### 3. CLI 已经相当 agent-friendly，但还不均匀

优点：

- 命令名清晰，`python -m web_listening.cli --help` 可见 staged + legacy 全部命令
- 大部分关键 staged 命令使用显式 artifact path
- 多个核心命令支持 `--json`
- job wrapper 可指向产物路径

不足：

- 不是所有 staged 命令都支持 `--json`
- `list-jobs` 只有表格，没有 JSON
- `export-tracking-report` 没有 JSON wrapper
- `discover` / `classify` / `plan-scope` 只能靠文本确认输出路径

### 结论

- `AGENTS.md`：**强**
- `.hermes/project-status.md` 机制：**强**
- skill 思路：**对；PR1 已更新主入口叙事**
- CLI：**实用且大体 agent-friendly，但 contract 不齐**

综合评级：**7.6 / 10**

---

## 七、CLI 主路径、legacy 路径、可发现性与机器可消费性

### 1. 主路径已经存在

从实际代码和 help 看，主路径就是：

```text
discover -> classify -> select -> plan-scope -> bootstrap-scope -> run-scope -> report-scope -> export-manifest
```

证据：

- `web_listening/cli.py:359-680`
- `tests/test_cli.py:18-36`
- `python -m web_listening.cli --help`

这说明 staged path 已经不再只是“计划中的接口”，而是可执行主路径。

### 2. legacy 路径仍然清晰存在

legacy 命令仍是：

- `add-site`
- `list-sites`
- `check`
- `list-changes`
- `download-docs`
- `list-docs`
- `analyze`
- `serve`

见 `web_listening/cli.py:52-356`。

这套路径与 staged path 共存，对兼容性有好处，但也要求文档必须非常明确地区分“何时不要用 legacy”。

### 3. 可发现性总体不错，但存在重叠和噪音

优点：

- `--help` 一次能看到全部命令
- staged 命令命名按流程顺序排列，可理解性强
- README/docs 已把 staged 流程写成推荐路径

问题：

- `report-scope` 与 `export-tracking-report` 边界容易混淆
- PR1 前 skill/reference 仍将 tools 提升为首选，削弱 packaged CLI 的 authority；本 PR 已将 tools 降为 compatibility layer
- legacy 命令与 staged 命令并列出现，没有 “preferred / compatibility” 机器标签

### 4. 机器可消费性是“中上，但不完整”

强项：

- `job_delivery.v1`
- `artifact_contract.v1`
- `web-listening-manifest.v1`
- 相对路径与 provenance

弱项：

- 并非所有命令都能稳定输出 JSON
- 规划阶段 `discover/classify/select/plan-scope` 还偏向“写文件 + 打印路径”
- `list-jobs` 对自动化不够友好

### CLI 结论

CLI 已经具备“agent 可操作主路径”，但还缺“端到端一致的 JSON-first 机器接口”。

评级：**7.9 / 10**

---

## 八、主要缺口、风险与优先级建议

## P0：文档与 skill 叙事失真

### 问题

PR1 前 skill/reference 仍在告诉 agent “优先 tools，CLI 还是 legacy”，这与 `web_listening/cli.py` 和 `README.md` 现实不符。

### 风险

- agent 会走旧入口
- 同一仓库中出现两套 authority
- review / automation 难以统一最佳实践

### 建议

统一更新以下文件，使 staged packaged CLI 成为默认官方入口，tools 明确定义为 compatibility layer：

- `.codex/skills/web-listening-agent/SKILL.md`
- `.codex/skills/web-listening-agent/references/current-api.md`
- `.codex/skills/web-listening-agent/references/interface-strategy.md`
- `docs/skills/OPENCLAW_SKILL_USAGE.md`

## P1：JSON contract 不完整

### 问题

当前并非所有 staged 命令都支持 `--json`。

### 风险

- agent 需要解析人类文本
- 上层编排器难以统一消费 discover / classify / plan 输出
- 自动化链路会在前半段退化成“读 stdout + 猜路径”

### 建议

为以下命令补充统一 `--json`：

- `discover`
- `classify`
- `select`
- `plan-scope`
- `list-jobs`
- `export-tracking-report`

建议统一返回 `job_delivery.v1` 或一个更轻量但规范化的 `artifact_delivery.v1`。

## P1：report 命令边界重叠

### 问题

`report-scope` 与 `export-tracking-report` 都能产出 tracking report，机器视角下界限不够硬。

### 风险

- agent 不知道哪个是 canonical report surface
- 测试和文档容易重复或分裂

### 建议

收敛策略二选一：

- 要么将 `export-tracking-report` 明确标为 legacy alias
- 要么将 `report-scope` 明确定位为 staged canonical report export，并让 `export-tracking-report` 只保留兼容包装

## P1：文档链接不可移植（PR1 已处理）

### 问题

PR1 前，`README.md`、`docs/README.md` 仍含 `C:/Project/...` 链接。

### 风险

- 本地跳转失效
- GitHub 渲染不可用
- 降低仓库作为 agent 知识源的可执行性

### 建议

已在本 PR 中全部改成仓库相对链接；后续只需避免重新引入机器本地绝对链接。

## P2：缺少单一 intent 契约

### 问题

目前监控意图散落在 selection/scope/task 中。

### 风险

- agent 很难快速回答“为什么监控这个站点”
- scope 和 task 的职责边界不够硬

### 建议

按 roadmap 补 `monitor_intent.yaml`，并把：

- 业务目标
- 成功标准
- 关注主题
- 升级规则
- 下游 handoff 需要

集中到单一 artifact 中。

## P2：review gate 主要靠文档而非执行器

### 问题

draft scope 必须人审这件事主要写在文档里，CLI 没有硬 gate。

### 建议

可考虑在 `bootstrap-scope` 前增加可选严格模式，例如：

```bash
web-listening bootstrap-scope --scope-path ... --require-reviewed-scope
```

或约定 `selection_review_status` / `scope_approval_status` 的允许集合。

---

## 九、建议的下一步 PR

如果只做一轮高价值、低风险 PR，我建议按下面顺序：

1. **文档与 skill 收敛 PR**
   - 更新 README / docs / skill / references
   - 明确 packaged staged CLI 是首选入口
   - tools 标为 compatibility layer
   - 修正 Windows 绝对链接

2. **CLI JSON 一致性 PR**
   - 给 `discover/classify/select/plan-scope/list-jobs/export-tracking-report` 补 `--json`
   - 输出统一 contract

3. **report surface 收敛 PR**
   - 明确 `report-scope` 与 `export-tracking-report` 的 canonical/compatibility 关系

4. **intent artifact PR**
   - 引入 `monitor_intent.yaml`
   - 让 task/scope/report/manifest 全部引用它

---

## 十、最终判断

### 它现在是不是“完全面向 agent”？

**不是。**

### 更准确的表述

它现在是一个：

- **面向 agent 设计的 staged 网站跟踪与证据交付工具**
- **已经拥有较强 artifact contract**
- **但还没有把 CLI、skill、docs、REST、机器输出面完全统一起来**

### 当前程度

- 产品方向：**9/10**
- 输入契约：**8.2/10**
- 输出契约：**8.8/10**
- 文档叙事：**6.8/10**
- agent-friendly 运维与 skill：**7.6/10**
- CLI 与机器可消费性：**7.9/10**

综合：**A- / 7.8 分**

### 一句话结论

`web_listening` 已经具备“agent-facing tracking tool”的主体骨架，尤其在 artifact contract 上表现突出；它距离“完全面向 agent”只差**统一叙事、统一 JSON surface、统一 canonical entrypoint** 这最后一层产品化收敛。
