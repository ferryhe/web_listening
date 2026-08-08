# Agentic Site Monitoring 实施计划

- 状态：待实施
- 目标仓库：`web_listening`
- 产品权威：根目录 `README.md`、稳定 schema 和代码；本文只是执行工单，不定义第二套合同。

## 1. 目标

对每个网站形成可复现的监听配置：监听哪些页面或文件、用什么采集工具、提取什么内容、使用哪个版本的 Site Skill。系统同时提供：

1. Skill 维护链路：发现网站变化，探索并生成新 Skill 候选，审核后激活。
2. 工作链路：工作 Agent 固定使用已审核的 scope、profile 和 Skill 获取内容。
3. 最小操作界面：探索制定 Skill、按 Skill 运行、查看数据库内容。

## 2. 必须保持的边界

- 正式执行继续使用 `monitor_scope.yaml`、`acquisition-profile.v1`、`site-skill.v1` 和编译后的 `acquisition-execution-plan.v1`。
- `monitor_scope.yaml` 必须完整绑定六个字段：`acquisition_profile_id`、`site_skill_version`、`site_skill_package_sha256`、`site_skill_recipe_id`、`site_skill_script_sha256`、`executor_version`。
- Probe 只是证据，不授予执行权限；部分绑定必须 fail closed。
- 已激活并被 scope 引用的 Skill 包不可原地修改。网站变化时创建新版本，审核后再显式重绑 scope。
- Skill 健康任务和内容采集任务独立调度。健康失败不能自动修改或激活 Skill。
- 保持范围有界，继续执行 allowed domains、`max_depth`、`max_pages`、`max_files` 和 executor 安全策略。
- 前三批工作不改变现有十个 MCP 工具；UI 和维护流程先使用共享服务及 REST API。新增 MCP 合同必须另行决策。
- 首版 UI 仅作为本地操作台，不做公网部署、账号系统或复杂前端框架。
- Skill 状态权限固定为：探测服务只能推进到 `probed`；`reviewed` 必须保存经过验证的操作员主体、时间和被审核 digest；只有 digest 未变化的 reviewed 包才能由激活服务推进到 `active`。维护 Agent 和工作 Agent 均不得自审或自激活。
- PR1 即提供独立 operator/maintenance capability：review/activate 必须持有 operator capability；maintenance capability 只能执行 discover/classify、probe、生成候选和处理 maintenance request，不能写 selection、scope、profile，也不能 review/activate。Capability 从运行时 secret 注入，不写文件、不回显、不记录原值，审核记录只保存验证后的主体 ID。
- PR1 新增的 planning/Site Skill 端点默认服务于 loopback 本地操作台；显式绑定非 loopback 时必须配置认证，否则服务启动 fail closed，请求鉴权层也必须拒绝漏配或无效凭据。该要求只约束新增端点，不强制迁移现有 1.0 路由。
- 内置 registry 保持只读；data-root registry 只包含已激活的用户包。服务端 resolver 同时读取两者，同一 `site_key/version` 冲突时 fail closed；CLI 调试用户包必须显式传 `--root` 或 `--site-skill-root`。
- Acquisition profile 持久化后可按 `acquisition_profile_id` 解析；新增 ID 字段/端点是向后兼容能力，UI 默认使用 ID。现有 1.0 `scope_path/profile_path` 请求继续支持并保留回归 fixture，但仍只能解析受控输入目录，不能用任意路径绕过控制面。
- 所有 `site_key/version` 先规范化并拒绝路径穿越、绝对路径和 symlink 越界；候选列表、生成和静态验证不得导入或执行候选脚本；候选写入、激活复制和最终 digest 固化必须原子化，active 包不可覆盖。

## 3. 两条独立工作链路

### A. Skill 维护链路

`health check -> maintenance request -> discover/classify/probe -> candidate package -> validate -> review -> activate -> explicit scope rebind`

- 健康检查区分：网站不可达、页面结构漂移、内容质量失败、recipe/executor 失效、范围配置过期。
- 单次或短暂失败只记录证据；连续达到策略阈值后创建 maintenance request。
- 维护 Agent 根据 request 重新探索，生成下一个语义版本的候选包。
- 候选包在激活前可修订；激活后固定版本和 SHA-256。
- 激活新版本不自动改变正在工作的 scope。

### B. 工作 Agent 链路

`resolve pinned authority -> preview plan -> bootstrap/run -> report/export manifest`

- Agent 只能使用 scope 中固定的 profile、Skill 版本、digest、recipe 和 executor。
- 运行失败写入 job、attempt 和 evidence，并可创建 maintenance request；工作 Agent 不修改 Skill。
- 下游应用只保存 `site_key`、`scope_id`、Skill/profile 绑定、`job_id` 和 manifest/artifact 标识，不再维护另一套 crawler/search YAML 权威。

## 4. 分批提交

每个 PR 都从最新干净的 `main` 开分支，完成本地验证、两个只读 reviewer-agent gate、推送、约 10 分钟 CI/评论复查后才合并。

### PR1：规划与 Site Skill 生命周期 API

建议分支：`feat/planning-site-skill-api`

做什么：

- 从 CLI 流程抽取共享服务，REST 直接调用服务，不通过 subprocess 调 CLI。
- 为 `discover`、`classify`、`select`、`plan-scope` 增加 job 化 REST 接口，并返回 artifact/job 标识。
- 增加 Site Skill 列表、详情、候选生成、静态验证、状态推进和激活接口。
- 内置 Skill 根目录保持只读；用户生成的候选放入 `${WL_DATA_DIR}/site-skills/<site_key>/<version>/`。
- 激活时将审核通过的候选原子复制到 `${WL_DATA_DIR}/site-skills-active/<site_key>/<version>/`，并固定最终 package/script digest；候选根目录永不参与正式执行。
- 实现统一 resolver：内置 root 加 data-root active registry；冲突 fail closed，CLI 的显式 root 只用于开发/调试。
- Acquisition profile 写入控制目录并按 ID 解析；新增 ID 请求与现有受控 path 请求共存并走同一 resolver，不能改变已有 1.0 成功/失败 envelope。
- 激活前完成静态校验、verification、profile/domain 一致性、安全路径检查和操作员审核记录校验。
- 在 PR1 增加独立 operator/maintenance capability 校验；review/activate 从 capability 取得主体，不能信任请求体自报身份。
- 新增端点默认仅供 loopback 本地操作台使用；非 loopback 绑定必须同时配置认证，缺失配置时启动失败，且请求层对缺失/错误 capability fail closed。现有 1.0 路由保持原认证兼容性，不在本批强制迁移。
- 状态转换使用独立 probe/review/activate 操作，不提供可任意跳转的通用 promote 接口。
- CLI、REST 复用同一业务服务；现有 CLI 和 MCP 行为保持兼容。

建议接口：

- `POST /api/v1/planning/discover`
- `POST /api/v1/planning/classify`
- `POST /api/v1/planning/selections`
- `POST /api/v1/planning/monitor-scopes`
- `GET /api/v1/site-skills`
- `GET /api/v1/site-skills/{site_key}/{version}`
- `POST /api/v1/site-skills/candidates`
- `POST /api/v1/site-skills/{site_key}/{version}/validate`
- `POST /api/v1/site-skills/{site_key}/{version}/record-probe`
- `POST /api/v1/site-skills/{site_key}/{version}/review`
- `POST /api/v1/site-skills/{site_key}/{version}/activate`

PR1 新增端点的角色/capability 矩阵如下；operator 和 maintenance 是独立运行时 capability，矩阵之外不隐式扩大权限：

| 端点/操作 | loopback 本地调用 | 非 loopback 调用 |
|---|---|---|
| `GET /api/v1/site-skills`、`GET /api/v1/site-skills/{site_key}/{version}`、无副作用的静态 `validate` | 可无 capability | 必须通过 maintenance 或 operator capability 认证 |
| `POST /api/v1/planning/discover`、`POST /api/v1/planning/classify` | maintenance 或 operator capability | maintenance 或 operator capability，并通过非 loopback 认证配置 |
| `record-probe`、`candidates`；以及 PR3 增加的 request claim/heartbeat/complete | maintenance 或 operator capability | maintenance 或 operator capability，并通过非 loopback 认证配置 |
| `POST /api/v1/planning/selections`、`POST /api/v1/planning/monitor-scopes`、acquisition profile 创建/更新 | 仅 operator capability | 仅 operator capability，并通过非 loopback 认证配置 |
| `review`、`activate` | 仅 operator capability | 仅 operator capability，并通过非 loopback 认证配置 |

`validate` 在此矩阵中仅指不写状态、不执行候选脚本的静态校验；任何会创建 job、artifact、候选或状态记录的操作都不属于本地只读豁免。

提交什么：

- 共享 planning/Site Skill 服务、API models/routes、存储支持。
- 离线 API/CLI parity、生命周期、上述角色矩阵、loopback/非 loopback fail-closed、capability 权限拒绝、1.0 path 回归、路径/symlink/冲突、原子写入和 contract fixture 测试。
- 根 `README.md` 的新增接口、候选目录和 SemVer 决策说明：本批采用向后兼容的 Minor 能力，不删除或强制迁移现有 path 请求。

完成标准：

- 同一 fixture 经 CLI 和 REST 产生语义一致的 inventory、classification、selection 和 scope。
- 可从候选生成走到 `draft -> probed -> reviewed -> active`；缺少/错误 capability、maintenance token 调用 review/activate、缺少操作员审核、digest 已变化或 Agent 直接激活都被稳定拒绝且不改文件。
- loopback 无 capability 只能调用矩阵中的只读操作；maintenance capability 可完成 discover/classify/probe/candidate/request 操作，但不能创建 selection、scope、profile 或执行 review/activate；新增端点在非 loopback 未配置认证时不能启动或处理请求。
- data-root active Skill 和持久化 profile 可通过新增 ID 请求完成 REST preview + bootstrap/run；现有 1.0 受控 path fixture 继续通过；内置/data-root 冲突、部分或错误绑定仍 fail closed。
- 静态操作不执行候选脚本，路径穿越和 symlink 越界被拒绝，失败写入不会留下半激活包。
- 现有 CLI/MCP 回归测试和完整测试通过，`git diff --check` 通过。

### PR2：三个页面的本地 Web 操作台

建议分支：`feat/local-operator-ui`

做什么：

- 由现有 FastAPI 服务静态托管轻量 HTML/CSS/JavaScript，不引入 SPA 构建链。
- 页面一“Explore & Build Skill”：输入网站和目标，执行 discovery/classification/probe，选择页面、文件、工具和范围，生成并验证候选 Skill，展示状态推进。
- 页面二“Run by Skill”：选择精确 scope/profile/Skill 版本，预览 execution plan，执行 bootstrap 或 run，查看 job、attempt、artifact 和错误。
- 页面三“Evidence & Content”：按 site/scope/run/type/time 查询页面、文件、变化、下载、报告和 manifest；正文只按需读取。
- 所有高风险动作需要明确确认；界面不显示 secret 值，不允许越过 scope/profile/Skill 权威。
- `serve` 默认仅绑定 `127.0.0.1`，禁止 wildcard CORS；UI 新增写接口校验同源请求并复用 PR1 operator/maintenance capability。显式绑定非 loopback 时若未配置认证则 fail closed；现有 1.0 路由不在本 PR 被强制迁移。
- 网站正文、错误和数据库内容始终按文本转义展示，禁止把远程 HTML 直接插入 DOM。

提交什么：

- `/ui/explore`、`/ui/run`、`/ui/evidence` 及共享静态资源。
- 必要的只读列表/筛选 API，不复制 planning 或执行逻辑。
- 页面/API 测试、浏览器 smoke 测试和 README 使用说明。

完成标准：

- 新用户可仅通过三个页面完成“候选 -> 审核激活 -> preview -> bootstrap/run -> 查看证据”。
- 浏览器 smoke 覆盖三页主路径、错误状态和刷新后的 job 恢复。
- 页面不触发无界 crawl，不泄漏本地路径或 secrets；测试覆盖 loopback、CORS、同源/令牌、非 loopback 拒绝和存储型 XSS；API 测试和完整测试通过。

### PR3：独立的 Site Skill 健康与维护队列

建议分支：`feat/site-skill-health-loop`

做什么：

- 新增独立的 `skill_health` 调度，不复用内容监听的 job id 或状态。
- 新增 `site_skill_health_checks` 和 `skill_maintenance_requests` 存储，保留探测 URL、Skill 版本、adapter、结果、分类、连续失败数和 evidence/job 标识。
- 连续序列键固定为 `{site_key, skill_version, probe_target, adapter, check_kind}`；默认阈值为 3，可按站点配置，成功只清零同一序列。
- 开放 request 以序列键加 failure epoch 建数据库唯一约束；重复/并发调度不得生成第二个开放 request。
- 领取使用原子 `open -> claimed` 转换、owner、lease expiry 和 attempt；进程崩溃后过期 lease 可重领，完成操作幂等。
- 维护 Agent 是 REST 的外部消费者：持有 maintenance capability，claim request 后只调用 `discover`、`classify`、probe 和 candidate API，把 source request、job、artifact 和 candidate digest lineage 写回，并完成为 `awaiting_review`；它没有 operator capability，不能创建/重绑 selection、scope、profile，也不能 review/activate。
- 阈值达到后只创建 request，不自动改包、不自动激活、不自动重绑 scope。
- 提供手工触发、结果查询、request claim/heartbeat/complete 接口，供维护 Agent 和 UI 使用。

提交什么：

- 健康策略、独立 scheduler orchestration、SQLite schema/storage、REST API 和状态模型。
- 维护 Agent REST contract/lineage，以及瞬时失败、序列隔离、恢复、去重、lease、并发和“工作 Agent 保持 pinned”测试。
- README 的健康任务配置和故障处理说明。

完成标准：

- 同一序列一次失败不创建 request，第三次连续失败创建一个；其他 adapter 成功不清零该序列，当前序列恢复后归零。
- 重复调度和并发执行不会重复创建开放 request；过期 lease 可重领，重复完成不产生第二份结果。
- 端到端测试覆盖 `claim -> discover/classify/probe -> candidate + lineage -> awaiting_review`，并证明维护 Agent 无法自审或激活。
- 健康任务不能修改 active package 或现有 scope，内容采集任务不受维护流程阻塞。
- 重启服务后健康历史、计数和开放 request 可恢复；完整测试通过。

### 后续应用集成 PR（在下游应用仓库执行）

做什么：

- 将应用层自定义 crawler/search/YAML 执行路径替换成 `web_listening` MCP/API 薄适配器。
- 应用只管理用户意图、业务 schedule、scope/job 状态和 manifest 导入。
- 首次运行调用 preview + bootstrap，后续运行调用 pinned scope run；失败转成 maintenance request。

提交什么：

- MCP/API adapter 与稳定 envelope 映射。
- 旧 crawler/search/YAML 执行路径的删除或默认禁用及迁移开关。
- manifest 导入、scope/Skill/profile/job/attempt/artifact lineage 持久化。
- 合同测试、失败/重试测试和应用 README/迁移说明。

完成标准：

- 应用不再直接决定 executor、绕过 profile 或自行抓取相同站点。
- 同一次运行可从应用记录追溯到 scope、Skill digest、job、attempt、artifact 和 manifest。
- 合同测试证明应用可以兼容当前稳定 envelope，并能正确处理失败和重试。

## 5. 调试顺序

先离线验证合同，再做单站 probe，最后才运行正式 scope：

```powershell
$ErrorActionPreference = "Stop"
$WebListening = ".\.venv\Scripts\web-listening.exe"
$Python = ".\.venv\Scripts\python.exe"

function Invoke-WebListening {
    & $WebListening @args
    $ExitCode = $LASTEXITCODE
    if ($ExitCode -ne 0) {
        throw "web-listening exited with code ${ExitCode}: $($args -join ' ')"
    }
}

$SiteKey = "replace-with-catalog-site-key" # 必须替换为所选 catalog 中已存在的 site_key
$SkillVersion = "replace-with-version-from-selected-active-entry"
$SkillPackageDigest = "replace-with-package_sha256-from-same-selected-active-entry"
$SkillRoot = ".\data\site-skills-active"
$Domain = "www.example.org"
$RootUrl = "https://www.example.org/"
$SelectionPath = ".\data\plans\section_selection_example.yaml"
$ScopePath = ".\data\plans\monitor_scope_example.yaml"
$ProfilePath = ".\data\plans\acquisition_profile_example.yaml"

Invoke-WebListening discover --catalog dev --site-key $SiteKey
Invoke-WebListening classify --catalog dev --site-key $SiteKey
Invoke-WebListening list-site-skills --root $SkillRoot --json

# 继续前，用上面同一条 active registry entry 的 version 和 package_sha256 替换两个 Skill 占位值。
Invoke-WebListening validate-site-skill --root $SkillRoot --site-key $SiteKey --version $SkillVersion --package-digest $SkillPackageDigest --json
Invoke-WebListening build-acquisition-profile --site-key $SiteKey --allowed-domain $Domain --output $ProfilePath --json
Invoke-WebListening probe-acquisition --url $RootUrl --site-key $SiteKey --json

# 在 UI/API 中选择 exact active Skill/profile，审核并生成 $SelectionPath。
Invoke-WebListening select --selection-path $SelectionPath
Invoke-WebListening plan-scope --selection-path $SelectionPath --yaml-path $ScopePath

# plan-scope 生成的 monitor_scope.based_on 必须含 acquisition_profile_id 和五个 Skill/executor 绑定字段。
$ScopeBindingCheck = @'
import sys

from web_listening.blocks.monitor_scope_planner import load_monitor_scope_plan

required = (
    "acquisition_profile_id",
    "site_skill_version",
    "site_skill_package_sha256",
    "site_skill_recipe_id",
    "site_skill_script_sha256",
    "executor_version",
)
plan = load_monitor_scope_plan(sys.argv[1], strict_limits=True)
invalid = [
    field
    for field in required
    if type(plan.based_on.get(field)) is not str or not plan.based_on[field].strip()
]
if invalid:
    raise SystemExit(
        "monitor_scope.based_on requires exact non-empty string values for: "
        + ", ".join(invalid)
    )
'@
$ScopeBindingCheck | & $Python -c 'import sys; exec(sys.stdin.buffer.read().decode().lstrip(chr(0xfeff)))' $ScopePath
$PythonExitCode = $LASTEXITCODE
if ($PythonExitCode -ne 0) {
    throw "monitor scope binding validation exited with code $PythonExitCode"
}
Invoke-WebListening preview-execution-plan --scope-path $ScopePath --profile-path $ProfilePath --site-skill-root $SkillRoot --json
Invoke-WebListening bootstrap-scope --scope-path $ScopePath --acquisition-profile-path $ProfilePath --site-skill-root $SkillRoot --json
Invoke-WebListening run-scope --scope-path $ScopePath --acquisition-profile-path $ProfilePath --site-skill-root $SkillRoot --json
```

排错时按此顺序定位：

1. Skill package 是否能静态解析、digest 是否匹配。
2. Profile domain、quality gate 和 runtime approval 是否满足。
3. Probe 是网站不可达，还是当前 adapter/recipe 不满足质量门。
4. Execution plan 是否完整绑定六个字段并找到可用 executor。
5. Job、attempt、evidence、report 和 manifest 的 lineage 是否一致。
6. 仅对 bootstrap/run/report/job/artifact 等 MCP 与 REST 都支持的操作，检查同一 shared service/fixture 的结果是否语义一致。

## 6. 整体完成定义

- 操作员能在本地 UI 为一个新网站建立经审核的有界 Skill、profile 和 scope。
- 工作 Agent 能固定使用该版本完成 bootstrap、增量 run、report 和 manifest 导出。
- 模拟网站结构漂移能触发维护 request，但不会改变 active Skill 或正在运行的 scope。
- 外部维护 Agent 能领取 request、生成带完整 lineage 的候选并停在 awaiting_review，且无法自审或激活。
- 页面、API、CLI、MCP 和 SQLite evidence 可通过同一组标识完整追溯。
- 离线合同测试、完整测试、浏览器 smoke、reviewer gate、CI 和有效 review 评论全部通过。
