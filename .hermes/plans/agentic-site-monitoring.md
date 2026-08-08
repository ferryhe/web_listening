# Agentic Site Monitoring 实施计划

- 状态：待实施
- 目标仓库：`web_listening`
- 产品权威：根目录 `README.md`、稳定 schema 和代码；本文只是执行工单，不定义第二套合同。

## 1. 目标

对每个网站形成可复现的监听配置：监听哪些页面或文件、用什么采集工具、提取什么内容、使用哪个版本的 Site Skill。系统同时提供：

1. 诊断前置链路：先探测并评估 `robots.txt`，从中取得声明的 sitemap，再以安全、有界方式检查 sitemap 证据，最后才制定采集/发现方案。
2. Skill 维护链路：发现网站变化，探索并生成新 Skill 候选，审核后激活。
3. 工作链路：工作 Agent 固定使用已审核的 scope、profile 和 Skill 获取内容。
4. 最小操作界面：探索制定 Skill、按 Skill 运行、查看数据库内容。

## 2. 必须保持的边界

- 正式执行继续使用 `monitor_scope.yaml`、`acquisition-profile.v1`、`site-skill.v1` 和编译后的 `acquisition-execution-plan.v1`。
- `monitor_scope.yaml` 必须完整绑定六个字段：`acquisition_profile_id`、`site_skill_version`、`site_skill_package_sha256`、`site_skill_recipe_id`、`site_skill_script_sha256`、`executor_version`。
- Probe 只是证据，不授予执行权限；部分绑定必须 fail closed。
- `robots.txt`、sitemap 和网站诊断产物也只是规划证据：它们既不授予执行权限，也不构成网站所有者同意、法律授权或绕过访问控制的理由。正式执行仍必须经过操作员审核，并使用有界 scope、完整 profile、精确 Site Skill 绑定和编译后的非空 execution plan。
- Agentic Site Monitoring 的新规划路径必须先取得可验证的 `site-diagnostic.v1`，并在 PR1 起取得与其 digest 精确绑定的独立、不可变 `site-diagnostic-review.v1`；PR1、PR2、PR3 不得在诊断/审核缺失、过期、origin/digest 不匹配、disposition 未批准或状态/建议组合不允许继续时自动制定采集方案。当前 1.0 CLI 的既有行为不受该新路径影响，仍以根 `README.md` 为准。
- 已激活并被 scope 引用的 Skill 包不可原地修改。网站变化时创建新版本，审核后再显式重绑 scope。
- Skill 健康任务和内容采集任务独立调度。健康失败不能自动修改或激活 Skill。
- 保持范围有界，继续执行 allowed domains、`max_depth`、`max_pages`、`max_files` 和 executor 安全策略。
- PR0—PR3 不改变现有十个 MCP 工具；UI 和维护流程先使用共享服务及 REST API。新增 MCP 合同必须另行决策。
- 首版 UI 仅作为本地操作台，不做公网部署、账号系统或复杂前端框架。
- Skill 状态权限固定为：探测服务只能推进到 `probed`；`reviewed` 必须保存经过验证的操作员主体、时间和被审核 digest；只有 digest 未变化的 reviewed 包才能由激活服务推进到 `active`。维护 Agent 和工作 Agent 均不得自审或自激活。
- PR1 即提供独立 operator/maintenance capability：诊断 review、Skill review/activate 必须持有 operator capability；maintenance capability 只能执行 diagnosis、消费已批准诊断后的 discover/classify、probe、生成候选和处理 maintenance request，不能写 selection、scope、profile，也不能创建任何 review/activate。Capability 从运行时 secret 注入，不写文件、不回显、不记录原值，审核记录只保存验证后的主体 ID。
- PR1 新增的 planning/Site Skill 端点默认服务于 loopback 本地操作台；显式绑定非 loopback 时必须配置认证，否则服务启动 fail closed，请求鉴权层也必须拒绝漏配或无效凭据。该要求只约束新增端点，不强制迁移现有 1.0 路由。
- 内置 registry 保持只读；data-root registry 只包含已激活的用户包。服务端 resolver 同时读取两者，同一 `site_key/version` 冲突时 fail closed；CLI 调试用户包必须显式传 `--root` 或 `--site-skill-root`。
- Acquisition profile 持久化后可按 `acquisition_profile_id` 解析；新增 ID 字段/端点是向后兼容能力，UI 默认使用 ID。现有 1.0 `scope_path/profile_path` 请求继续支持并保留回归 fixture，但仍只能解析受控输入目录，不能用任意路径绕过控制面。
- 所有 `site_key/version` 先规范化并拒绝路径穿越、绝对路径和 symlink 越界；候选列表、生成和静态验证不得导入或执行候选脚本；候选写入、激活复制和最终 digest 固化必须原子化，active 包不可覆盖。

## 3. 两条独立工作链路

### A. Skill 维护链路

`health check -> maintenance request -> robots/sitemap diagnosis -> operator diagnostic review -> discover/classify/probe -> candidate package -> validate -> Skill review -> activate -> explicit scope rebind`

- 健康检查区分：网站不可达、页面结构漂移、内容质量失败、recipe/executor 失效、范围配置过期。
- 单次或短暂失败只记录证据；连续达到策略阈值后创建 maintenance request。
- 维护 Agent 根据 request 先生成新的 robots/sitemap 诊断；只有可继续的诊断通过 artifact 校验和操作员定义的边界后才重新探索，生成下一个语义版本的候选包。
- 候选包在激活前可修订；激活后固定版本和 SHA-256。
- 激活新版本不自动改变正在工作的 scope。

### B. 工作 Agent 链路

`resolve pinned authority -> preview plan -> bootstrap/run -> report/export manifest`

- Agent 只能使用 scope 中固定的 profile、Skill 版本、digest、recipe 和 executor。
- 运行失败写入 job、attempt 和 evidence，并可创建 maintenance request；工作 Agent 不修改 Skill。
- 下游应用只保存 `site_key`、完整 `origin_authority_bindings` 集合、`scope_id`、Skill/profile 绑定、`job_id` 和 manifest/artifact 标识，不再维护另一套 crawler/search YAML 权威；即使单 origin 也保存单元素集合，不能压平成一个 diagnosis/review/identity 字段。

## 4. 分批提交

每个 PR 都从最新干净的 `main` 开分支，完成本地验证、两个只读 reviewer-agent gate、推送、约 10 分钟 CI/评论复查后才合并。

PR0 是 PR1—PR3 的共同前置依赖；PR1—PR3 保留原编号，但不得在 PR0 合并前开始实现依赖诊断产物的新路径。

### PR0：robots.txt 与 sitemap 前置诊断

建议分支：`feat/robots-sitemap-diagnostic`

做什么：

- 抽取一个只读、无浏览器、无 stealth 的网站诊断服务。对输入 URL 先规范化：`requested_origin` 保留输入来源，`canonical_origin` 固定为规范后的 scheme、IDNA host 和有效端口，且本次诊断不可被任何重定向改写。第一项 HTTP 请求必须是 `${canonical_origin}/robots.txt`；在此之前不得抓取首页、页面链接或猜测 sitemap。
- 产生稳定的 `site-diagnostic.v1` 规划证据。至少记录 `diagnostic_id`、`site_key`、请求 URL、两个 origin、非空 allowed domains、operator-reviewed exact `allowed_document_origins`、开始/完成/过期时间、工具/schema 版本、预算、正交的 `diagnostic_status`/`recommendation`、per-origin robots policy、sitemap 证据、被接受/拒绝/去重的资源与 URL、截断原因和确定性的下一步。Normalized origin 唯一定义为 `{scheme, IDNA-lowercase host, effective_port}`：省略端口时 HTTP 固定为 80、HTTPS 固定为 443，非默认端口必须作为完整 origin 显式批准；host allowlist 从不授权任意端口。Canonical origin 必须精确出现在 `allowed_document_origins` 且 host 仍落在 allowed domains 内，否则在 HTTP 请求前失败。默认 freshness 为完成后 24 小时，只能配置得更短。
- Diagnosis 必须固定 canonical identity：实际 HTTP `User-Agent`、符合 RFC 9309 `identifier` 语法的规范 product token、identity ID，以及该 identity object 的 SHA-256。Product token 必须是非空 `[-A-Za-z_]+`，同时是实际 User-Agent 的大小写不敏感子串；非法 token、仅 prefix 匹配 robots token、或 token 不在 User-Agent 中都拒绝。**所有** robots/sitemap initial、retry、redirect 请求均使用同一 identity，并逐请求记录 actual User-Agent/token/identity digest、验证一致性。后续 PR1 的 discovery/profile/candidate Site Skill recipe/executor plan 必须精确绑定该 digest；身份不兼容需以实际身份重做 diagnosis。
- 为每次 robots/sitemap 文档请求保存请求 URL、重定向链、最终 URL、HTTP 状态、抓取时间、媒体/内容编码、wire/decoded 字节数、内容 SHA-256、父级来源和解析结果；每个 `Sitemap:` 指令保留 robots 行号，每个 sitemap 子项保留父 sitemap digest 与条目序号。Artifact 自身使用排除 `artifact_sha256` 字段后的 canonical JSON 计算 SHA-256。
- robots 访问规则必须按 RFC 9309 兼容语义解释；`Sitemap:` 作为独立扩展指令严格解析。Group selection 只做 product-token **exact、case-insensitive** 匹配，不能 prefix match；合并所有 exact-matching groups，只有没有 exact product-token group 时才合并 `User-agent: *` groups。Fixture 还必须覆盖 token identifier 非法、prefix-only 和 token absent from actual User-Agent，以及 path case、最长 path、`Allow` tie、空指令、百分号编码、`*`/`$`/`#`、group boundary/empty group、首 group 前规则忽略、`Sitemap:` 不终止 group和局部 malformed rule warning。没有规则或规则允许也不代表获得访问授权。
- robots `2xx` 只有在规范化媒体类型为 `text/plain` 且 decoded body 是严格 UTF-8 时才解析：media type/token 大小写与参数空白可规范化，`charset` 只能缺省或为 UTF-8，其他无害参数记录后忽略；HTML、缺失/unsupported MIME、非 UTF-8 charset、invalid UTF-8 都确定性归为 `blocked + operator_review`，不能当作无 robots。`404/410` 或有效 robots 中没有 `Sitemap:` 时，只检查一个确定性的同源 `${canonical_origin}/sitemap.xml`。空值、含凭据、非绝对 HTTP(S)、语法错误或无法规范化的 `Sitemap:` 是显式 robots 错误。`401/403` 不认证、不猜 sitemap。
- robots **和 sitemap** 文档只有 `408/425/429/5xx`、真正瞬时的 DNS/connect/timeout，以及可明确判定为瞬时且不涉及 certificate/SNI/hostname/peer/policy 的 TLS transport failure 才最多 retry 2 次且无 jitter；retry 在处理下一队列项前完成，每次尝试照常计 request/wire/decoded budget。Certificate、SNI、hostname、actual peer 或 TLS policy 校验失败不可重试，按最高优先级 authority/safety error 处理。Sitemap 队列确定为 FIFO：先按 canonical robots 中 `Sitemap:` 的行序入队；每个 sitemap index 按 XML 文档中的 `<loc>` 顺序追加队尾；同一输入不得因并发改变顺序，所有 queue/attempt ordinal 写入 artifact。
- 每个 HTTP/document attempt 在写 artifact 前必须唯一落入穷尽分类：有效 `2xx` 进入受控 robots/XML 解析；`401/403` 属于不可重试 priority-1 authority/safety；`404/410` 属于 deterministic completed-empty；除前述瞬时状态外的其他 `4xx`（如 `400/415`）属于不可重试 deterministic non-safety terminal document failure；合法 redirect 继续逐跳校验，缺失/malformed `Location` 或最终 `1xx` 属于 deterministic non-safety terminal protocol/document failure，redirect authority/policy/TLS 违规属于 priority 1，redirect hop budget 用尽属于预算截断。Sitemap XML 的 DTD/entity/XInclude、不允许根类型、HTML/通用 XML/伪装内容等 unsafe parser/root failure 属于 priority 1；普通、无 entity 的 XML 语法错误属于不可重试 deterministic non-safety terminal document failure；安全的空 `urlset`/index 属于 completed-empty。任何无法归类的 transport/protocol/parser outcome 均 fail closed 为 priority 1，不能暗中降级或无限重试。
- robots/sitemap 文档每个 redirect hop 都必须精确匹配 operator-reviewed `allowed_document_origins` 的 scheme/host/effective port，并禁止 HTTPS -> HTTP；同 host HTTP -> HTTPS 也只有 HTTPS exact origin 已预先批准时才允许。跨 host 或非默认端口同样必须 exact-approved；host allowlist、默认端口或重定向本身不能扩权。`canonical_origin` 始终不变。
- 跨 origin sitemap **文档**在抓取前必须先在同一 diagnosis 内请求该 exact origin 的 `/robots.txt`，以 canonical identity 建立 `origin_policy_evidence`（origin、policy ID/digest、robots status/digest/freshness、identity ID/digest）；整次 diagnosis 的第一项总 HTTP 请求仍必须是 canonical robots。只有 document origin/port 已 exact-approved、该 origin robots 结果可用且同 identity policy 明确允许 sitemap URL，才可抓取文档；否则记录并按优先级阻断。辅助 origin robots 的 `Sitemap:` 仅记录，不自动扩张队列。页面 seed 仍只接受 canonical origin，实际跨 origin 页面必须有独立 approved diagnosis/review。
- canonical/辅助 origin robots、声明/index/fallback sitemap 的每个 initial、retry、redirect hop 都必须通过同一 SSRF-safe transport：每次请求前重新解析该 hostname 的全部 A/AAAA，任一结果非公网（包括 mixed public/private）即拒绝；连接只能固定到本次已验证地址集合中的地址。HTTP `Host` 必须是该次 original normalized request authority 的 canonical 表示：IDNA-lowercase hostname，IPv6 literal 使用方括号，非默认 effective port 必须带 `:port`（默认端口省略）；HTTPS SNI 与证书 hostname 校验仍只使用未带方括号/端口的 normalized hostname。Authority/policy/DNS/validated-set 失败在建立目标连接前拒绝；TCP/TLS 建立期间或之后核对 SNI/certificate/actual peer，且必须在发送任何 HTTP 应用请求字节或调用应用层 handler 前确认 peer IP 仍属于本次验证集合且为公网。每一跳独立重做解析、pinning 和 peer check，不能复用上一次 DNS 信任或交给透明 redirect/proxy 绕过；DNS rebinding/mixed-address fixture 断言未建立目标连接，SNI/certificate/peer mismatch fixture 断言未发送 HTTP 请求字节且应用层 handler 未调用。
- `<urlset><url><loc>` 页面候选与 sitemap 文档 location 分开处理：一次 diagnosis 只接受 `origin(loc) == canonical_origin` 的页面 seed。其他 origin 即使 host 已 allowlisted，也只记录为 `cross_origin_requires_diagnosis` 并拒绝，必须针对那个 origin 重新执行 robots-first diagnosis 并单独审核；这次诊断绝不请求或跟随任何页面 `<loc>`。上述 redirect-hop 规则只适用于 robots/sitemap 文档获取，页面请求只有 PR1 审核通过后的 discovery 才能发生。
- `cross_origin_requires_diagnosis` 是有预算计数的记录/拒绝原因，本身不属于 authority/safety 错误：若同一 diagnosis 另有有效 canonical-origin seed，则按其余文档终止情况唯一归入 `complete + sitemap_seeded` 或 `partial + sitemap_seeded`；只有 cross-origin page loc 且 canonical seed 为 0 时才归入 `blocked + operator_review`。Fixture 必须同时覆盖 mixed canonical+cross-origin 和 cross-origin-only 两种结果，防止跨域拒绝被错误提升或被当作 seed。
- sitemap 解析必须禁用 DTD、外部实体、参数实体、网络实体解析和 XInclude，使用流式、namespace-aware 解析，只接受 sitemap index/urlset 根类型；HTML、通用 XML、嵌套压缩或伪装内容均是显式安全/解析错误，不能成为 seed。
- HTTP 客户端必须禁用透明 content decoding，以 raw streaming 方式先计 `wire_bytes`（HTTP transfer framing 之后、`Content-Encoding` 解码之前的 transmitted entity-body bytes），再做最多一次增量 gzip 解压并以解压后的 bytes 计 `decoded_bytes`，最后才做字符/XML 解析；任何 chunk 在追加到内存、字符串或 XML buffer **之前**先检查预算。robots 与 sitemap 同时识别受控的 gzip header/suffix/magic；多重/嵌套压缩或互相矛盾的编码信号归为安全错误。plain robots/XML 的同一 chunk 同时增加 wire 和 decoded 计数，不能因没有 gzip 而绕过 decoded limit；超限立即停止读取、关闭响应并保留已计数证据。
- Gzip 解压器的每次调用必须把最大输出限定为 `remaining_decoded_budget + 1`，若返回超过 remaining 立即拒绝且不得先追加；持续处理 `unconsumed_tail` 时也使用重新计算后的相同上限，不能先读取下一 wire chunk 或调用可能无界输出的 flush。输入结束必须确认恰好一个完整 gzip member、正常 EOF、没有 `unused_data`/trailing compressed stream/第二 member；任何 trailing bytes、额外 member 或未结束流均为安全错误。Bomb fixture 必须证明单个极小 compressed chunk 不能让 decompressor 在预算检查前分配无界输出。
- 默认硬预算固定为：每文档最多 5 个 redirect hop；本次诊断 HTTP request/redirect/retry 总计最多 64 个；每次 robots attempt 的 wire/decoded 各 1 MiB，全部 robots attempts 的 wire/decoded 各 3 MiB；每个 sitemap 文档 wire 10 MiB、decoded 50 MiB；全部 sitemap 文档 wire 32 MiB、decoded 128 MiB；sitemap index 深度 3；sitemap 文档 occurrence 32 个；robots `Sitemap:` URI、sitemap-document `<loc>` 和 page `<loc>` occurrence 合计 50,000 个。配置只能收紧。
- 所有 scheduled/attempted/rejected/duplicate/failed robots/sitemap 请求及 redirect/retry 都消耗 request budget；每个 robots `Sitemap:`、sitemap-index child document，以及无声明时的唯一 `${canonical_origin}/sitemap.xml` 根 fallback occurrence，都在校验或去重前消耗 file budget；每个 robots `Sitemap:` URI、fallback URI、sitemap-document `<loc>` 和 page `<loc>` occurrence 在校验或去重前消耗 URL budget；已从任何成功、失败、重复或随后拒绝的响应读取/解码的字节仍消耗相应 per-resource 和 aggregate byte budget。达到预算后不再调度或解析下一项，fallback 也不能成为第 33 个 sitemap 文档，不能通过失败、重复或拒绝项绕过上限。
- URL 规范化至少覆盖 scheme/host 大小写、IDNA、默认端口、点路径和 fragment；不得未经策略删除有意义的 query，也不得把 HTTP/HTTPS 混为同一资源。sitemap 文档按规范 final URL 和内容 digest 双重去重，页面 URL 按规范 URL 去重；去重只影响结果集，不退还预算，所有计数和拒绝原因进入 artifact。
- 结果合同使用两个正交、穷尽枚举。`diagnostic_status` 只能是 `complete`、`partial`、`retryable`、`blocked`；`recommendation` 只能是 `sitemap_seeded`、`bounded_homepage_fallback`、`retry_diagnosis`、`operator_review`。Schema 只允许下表五种组合；其他组合在写入和读取时都 fail closed。`recommendation` 只是可读的规划建议，不是审核或执行授权。

| 优先级 | 穷尽条件 | `diagnostic_status` | `recommendation` | 解释/后续 |
|---:|---|---|---|---|
| 1 | robots identity/header/token 不一致或 token 非法；robots wire/decoded 超限；robots `2xx` 为 HTML、缺失/unsupported MIME、非 UTF-8 charset、invalid UTF-8 或整体无法安全解码/确定解释；空值或 malformed `Sitemap:`；`401/403`；certificate/SNI/hostname/peer/TLS-policy failure；不安全 XML/根类型/entity/XInclude；文档请求的凭据、私网、allowlist/origin/exact port、redirect、scheme 或其他 authority/safety 错误；无法归类的 outcome；robots 过滤全部 canonical-origin 候选；或准备 homepage fallback 时 robots 排除首页/根路径 | `blocked` | `operator_review` | 最高优先级且不可重试；局部 malformed rule 仅 warning，`cross_origin_requires_diagnosis` 也不属于本行错误 |
| 2 | 至少已有 1 个有效 canonical-origin seed，随后出现 request/file/URL/wire/decoded/depth/redirect-hop budget 截断、重试后仍失败的瞬时错误，或其他 `4xx`/普通 XML 语法错误/missing Location/final `1xx` 等 deterministic non-safety terminal document/protocol failure | `partial` | `sitemap_seeded` | 保留已验证 seed 和明确失败/截断点，仍需独立 operator review |
| 3 | 至少已有 1 个有效 canonical-origin seed，且全部队列项在预算内确定性完成；其他 sitemap 为 `404/410`、安全解析为空或仅含已记录的 cross-origin page loc 均算完成，没有其他错误或截断 | `complete` | `sitemap_seeded` | 完整的 sitemap 规划证据，仍需独立 operator review |
| 4 | 0 个有效 seed、未发生优先级 1 错误、至少一个重试后仍失败的瞬时状态/transport error，且未触发预算、也不存在 deterministic non-safety terminal failure | `retryable` | `retry_diagnosis` | 只能重做 diagnosis，不能进入 discovery |
| 5 | 0 个有效 seed、未发生优先级 1/4 条件、预算未耗尽，且所有声明 sitemap（或无声明时唯一 `/sitemap.xml`）均确定为 `404/410` 或安全解析为空，同时首页/根路径未被 robots 排除 | `complete` | `bounded_homepage_fallback` | 可提交最小同源首页发现建议，仍需独立 operator review |
| 6 | 0 个有效 seed 且在取得 seed 前耗尽任一预算；出现其他 `4xx`、普通 XML 语法错误、missing/malformed redirect `Location`、final `1xx` 等 deterministic non-safety terminal document/protocol failure；只有跨 origin 页面 loc；或不满足以上任何合法情况 | `blocked` | `operator_review` | 不得猜测、不降级到 homepage；需要改变输入/边界后重新 diagnosis |

归并严格按表中优先级执行：优先级 1 的任何 safety/authority/robots/unsafe-XML 错误始终可见；有 seed 后，预算截断、最终瞬时失败或 deterministic non-safety terminal document/protocol failure 都是 `partial + sitemap_seeded`，只有其他文档均为 `404/410`/安全空/已记录 cross-origin page loc 才保持 `complete + sitemap_seeded`。无 seed 时，纯最终瞬时失败进入 `retryable`，全部 `404/410`/安全空可进入 bounded homepage fallback，而预算耗尽或任一 deterministic non-safety terminal failure 都是 `blocked + operator_review`；普通 XML syntax failure 与 unsafe parser/root/entity failure 分别落入 priority 6 与 priority 1，绝不合并。Artifact 同时保存所有触发原因和首个决定性优先级，便于 UI/日志可读展示。

- PR0 拟新增加法式 CLI `web-listening diagnose-site` 和 `site-diagnostic.v1` schema/fixture；它们都是 **PR0 交付物，并非当前已实现命令或合同**。PR0 不改造或调用 `discover`，也不创建审核记录；当前 1.0 planning path 完全不变。诊断消费和独立审核记录由具备 operator capability 的 PR1 实现。
- 新 command 和 `site-diagnostic.v1` 属于向后兼容的加法式能力，按根 `README.md` 的规则采用 Minor SemVer；不得在本计划中预先宣布具体已发布版本。PR0 实现时必须同步更新根 `README.md`、CLI inventory、稳定 schema/fixture 和版本决策；在此之前根 `README.md` 仍是当前产品权威，`sitemap` acquisition adapter 仍保持 reserved，诊断服务不得冒充执行 adapter。

提交什么：

- robots/sitemap 诊断共享服务、`site-diagnostic.v1` model/schema/fixture、拟议 `diagnose-site` CLI 及受控 artifact 写入；不包含 discovery consumer 或 operator review API。
- RFC 9309 product-token/groups/wildcard/anchor/comment/percent-encoding/malformed-line fixtures（含非法 token、prefix-only group、token 不在 actual User-Agent）、实际 robots identity、`text/plain`/MIME/charset/strict UTF-8、上述状态/建议合法组合与优先级、全 HTTP/TLS/XML terminal outcome 分类、canonical-first 请求先后、cross-origin sitemap exact-origin robots preflight、robots/sitemap redirect 与 exact-port 区别、robots/sitemap 最多两次无 jitter retry、确定性 FIFO、mixed canonical/cross-origin page loc、逐请求 DNS/IP pinning/peer SSRF、canonical Host authority（含 IPv6 brackets/nondefault port）、XML syntax 与 unsafe entity/root/DTD/XInclude 分离、plain/gzip bounded-decompress/bomb/extra-member、fallback/file budget、所有失败/重复 budget 计数、规范化/去重、digest/lineage 和确定性 fallback 的离线测试。
- 当前 1.0 `discover`/classification/scope fixture 原样回归，证明 PR0 尚未改变既有 planning authority 或要求未实现的审核输入。
- 根 `README.md` 的实际接口、artifact、边界和 SemVer 说明；实现完成前不改写现有权威描述。

完成标准：

- 请求顺序测试证明每次新网站诊断的第一项总 HTTP 请求是 canonical origin 的 `/robots.txt`，在 canonical robots 响应完成分类并按行序选出声明/唯一 fallback sitemap 前不会请求 sitemap；cross-origin sitemap 文档在入队后也必须先抓取其 exact origin 的 `/robots.txt` 建立 policy evidence，并仅在该 identity policy 允许且 exact scheme/host/effective port 已批准时请求。辅助 robots 的 `Sitemap:` 不扩队列；PR0 从不 dereference 页面 loc 或执行 discovery/采集。
- robots identity/token/MIME/UTF-8/over-limit、malformed `Sitemap:`、`401/403`、transient status/DNS/connect/timeout/TLS、certificate/SNI/hostname/peer/policy TLS failure、其他 `4xx`、`404/410`、missing/malformed redirect Location、final `1xx`、0 seed 前预算耗尽、有 seed 后预算/最终瞬时/deterministic non-safety failure、有效 seed 加其他 sitemap `404/410`/安全空、全部声明 `404/410`/安全空、完整成功、robots root/全部候选排除、mixed/only cross-origin 页面 loc、普通 XML syntax、unsafe XML/root/entity、plain/gzip bomb、trailing/第二 gzip member 和 fallback 第 33 文档均唯一映射到表中合法组合；特别证明“有效 seed + 其他 404/410/安全空”是 `complete + sitemap_seeded`、“有效 seed + 其他 4xx/普通 XML syntax”是 `partial + sitemap_seeded`、后两者在 0 seed 时是 `blocked + operator_review`。Artifact 保留所有 attempt/queue ordinal、identity、时间、wire/decoded 计数、digest 和父子 lineage。
- canonical/辅助 origin robots 以及声明、index child、唯一 fallback sitemap 的初始请求、最多两次无 jitter retry 及每个 redirect hop 都使用并记录同一 actual User-Agent/product token/identity digest，重新完成 exact origin/port policy gate、all-address DNS 校验、validated-IP connection pinning、canonical Host authority 与 HTTPS SNI/certificate/actual peer check；Host fixture 覆盖默认端口省略、nondefault `:port` 和 bracketed IPv6，SNI/certificate 始终只使用 hostname。未批准 nondefault port、redirect 到未批准 port、mixed public/private、DNS rebinding 在连接前 fail closed；SNI/certificate/peer mismatch 在 TCP/TLS 阶段 fail closed，且 fixture 必须证明没有发送任何 HTTP 应用请求字节、没有调用应用层 handler。诊断从不扩大域、不绕过 robots、不启动浏览器、不写入正式 scope/profile/Skill 或 Storage evidence。
- 同一输入和同一模拟响应产生除运行 ID/时间外语义相同的 artifact；同一 artifact 的 canonical digest 稳定，篡改后消费端拒绝。
- PR0 聚焦测试、现有 CLI/采集/发现回归、完整离线测试、CLI help/schema fixture 和 `git diff --check` 全部通过。

### PR1：规划与 Site Skill 生命周期 API

建议分支：`feat/planning-site-skill-api`

做什么：

- 从 CLI 流程抽取共享服务，REST 直接调用服务，不通过 subprocess 调 CLI。
- 复用 PR0 诊断服务并增加 `POST /api/v1/planning/site-diagnostics`。Operator 可为新站点或显式扩域创建 authority boundary。Maintenance request **不新增独立 operator-review lifecycle**：PR3 阈值触发时由服务端从当时 pinned、operator-reviewed scope/profile 自动固化 immutable `maintenance_boundary_snapshot`，至少含 canonical origins、allowed domains、exact allowed origins/ports、robots identity ID/digest、ancestor policy/authority IDs+digests、captured/expiry 时间和 snapshot digest，request 直接引用该 snapshot。
- Maintenance 创建 diagnosis 必须提供 `maintenance_request_id`/snapshot digest；服务端把 snapshot 与当前仍有效、digest 匹配的 ancestor scope/profile/authority bindings 取交集，canonical/document origins、ports、domains 只能继承或收紧，但 actual `User-Agent`、product token、identity ID 与 identity digest **不得收紧或替换，必须与 snapshot 和每个当前 ancestor binding 精确相等**。任一 identity 字段变化都在首个网络请求前 fail closed 并转 operator boundary/rediagnosis；snapshot/ancestor 缺失、过期或其他 digest 已变化同样 fail closed。新站点/扩域转 operator boundary 流程，但 request 本身不进入 review 状态。
- 只有 operator 可创建独立、不可变的 `site-diagnostic-review.v1`，review 至少固定 `review_id`、`diagnostic_id`、diagnostic artifact SHA-256、从 capability 验证出的 operator subject、`reviewed_at`、`expires_at` 和 disposition（`approved_for_planning`、`rejected`、`needs_rediagnosis`），并以排除自身 digest 字段后的 canonical JSON 固化 review SHA-256。该 hash 只做完整性校验，**不证明 operator authority**。Review 不得内嵌或改写 diagnosis，过期时间不得晚于 diagnosis 过期时间；重新审核只能追加新 record，不能覆盖旧 record。
- Review authority 只来自服务端受信 append-only store：review 写入路径必须先验证 operator capability，store 文件/DB 由服务账户 ACL 限制写入，maintenance/请求体/任意本地路径均不能注入记录。Consumer 只接受 `review_id`，通过配置的 trusted resolver 从该 store 读取，核验 store provenance、record digest、diagnosis binding、subject/disposition/expiry；请求携带的 review object、digest 或 path 均不是 authority。若做 CLI parity，只接受 `--diagnostic-id` + `--diagnostic-review-id` 并调用同一 trusted store/service lookup，不提供任意 `--diagnostic-review-path` 或以文件自哈希代替 provenance。
- 所有**新增 Agentic discovery consumer** 必须同时解析 diagnosis 和 trusted-store matching review，校验 schema/digest、immutable canonical origin、allowed document origins/ports、合法状态/建议组合、`approved_for_planning`、review subject、双方 freshness/expiry 后才可使用 sitemap/fallback 证据。可继续组合只限 `complete + sitemap_seeded`、`partial + sitemap_seeded`、`complete + bounded_homepage_fallback`；`retryable`/`blocked` diagnosis 不得创建 `approved_for_planning` review，只能拒绝或要求重新诊断。REST/CLI consumer 都要求 diagnosis/review ID；都不提供时保留当前 1.0 CLI 行为，不能声称既有路径已迁移。
- PR1 引入 `origin_authority_bindings`：按 normalized origin `{scheme, IDNA host, effective_port}` 索引的非空集合，每项固定 diagnostic/review/robots identity/robots policy 的 ID+digest 和 expiry。单 origin 也必须用单元素集合，不退化成单数字段；重复 key、缺项、过期或 digest 不匹配 fail closed。首版不支持按 origin 切换 runtime identity：同一 planning/execution authority set 内所有 binding 的 actual `User-Agent`、product token、identity ID 与 identity digest 必须完全相同，任一混合 identity 集合在首个请求前 fail closed 并要求用统一实际身份重新 diagnosis。Classification、selection、profile、scope、candidate Site Skill、compiled execution plan、job、attempt/evidence、report、manifest 及 downstream lineage 必须完整透传该集合；每个 page/file/sitemap target origin 都必须找到自身 matching approved binding。任何非 canonical 页面/文件 origin 必须有其独立 robots-first diagnosis + trusted-store approved review，不能只靠另一个 diagnosis 的 sitemap-document policy evidence。
- Profile allowed domains、scope domains/target origins、候选 Site Skill allowed domains 仍必须分别是所有适用 reviewed diagnosis allowed domains 的形式化子集，现有 profile domains ⊆ Site Skill domains 规则继续成立；exact origin/port 还必须逐项落入 `origin_authority_bindings`。
- PR1 为 profile、candidate Site Skill recipe/executor binding 和 compiled execution plan 增加精确 robots identity ID/digest 绑定；discovery 和 runtime capture 在发请求前后校验实际 `User-Agent`/product token 与 diagnosis identity。Identity、token 或匹配 robots group 不同即 fail closed 并要求重新 diagnosis，不能由 operator review 静默改写。
- PR1 discovery/runtime 在调度每个 initial sitemap（适用时）、初始 seed、后续发现的 page/file 请求**之前**，都按请求 normalized origin 查找 `origin_authority_bindings`，用其 approved robots policy 与 exact identity/product token 对完整目标 URL 重新计算 `Allow`/`Disallow`；无 binding、过期、identity/policy 不匹配或 disallowed 时不创建网络请求。任何 redirect hop 的新 URL 也在 follow 前重新做 origin binding、policy 和 exact port 检查。
- 上述每个 initial sitemap/seed/page/file 请求和每个 redirect hop 还必须复用 PR0 **同一 SSRF-safe transport**：逐次重新解析全部 A/AAAA 并拒绝 mixed/non-public，连接 pin 到本次 validated IP set，以 original normalized request authority 生成 canonical HTTP `Host`（非默认端口含 `:port`、IPv6 literal 加 brackets），HTTPS SNI/证书仍校验 normalized hostname，连接后核验 actual peer。Authority/policy/DNS/validated-set 失败必须在建立目标连接前拒绝；SNI/certificate/actual-peer 只能在 TCP/TLS 建立期间或之后验证，但必须在发送任何 HTTP 应用请求字节或调用应用层 target handler 前拒绝。Fixture 对 disallowed/mixed-address/DNS-rebinding/validated-set 失败断言未建立目标连接，对 peer mismatch/bad SNI/certificate 断言未发送 HTTP 请求字节且应用层 handler 未调用。
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

- `POST /api/v1/planning/site-diagnostics`
- `POST /api/v1/planning/site-diagnostics/{diagnostic_id}/reviews`
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
| `POST /api/v1/planning/site-diagnostics`、消费已有 approved review 的 `POST /api/v1/planning/discover`、`POST /api/v1/planning/classify` | maintenance 或 operator capability；maintenance diagnosis 必须绑定 server-generated boundary snapshot，domain/origin/port 只可按当前有效祖先 bindings 收紧，identity 四元组必须精确相等，request 本身不 review | maintenance 或 operator capability，并通过非 loopback 认证配置；maintenance 同样不能扩域、替换 identity 或创建 review |
| `record-probe`、`candidates`；以及 PR3 增加的 request claim/heartbeat/complete | maintenance 或 operator capability | maintenance 或 operator capability，并通过非 loopback 认证配置 |
| `POST /api/v1/planning/selections`、`POST /api/v1/planning/monitor-scopes`、acquisition profile 创建/更新 | 仅 operator capability | 仅 operator capability，并通过非 loopback 认证配置 |
| diagnosis review、Site Skill `review`/`activate` | 仅 operator capability | 仅 operator capability，并通过非 loopback 认证配置 |

`validate` 在此矩阵中仅指不写状态、不执行候选脚本的静态校验；任何会创建 job、artifact、候选或状态记录的操作都不属于本地只读豁免。

提交什么：

- 共享 planning/Site Skill 服务、API models/routes、`site-diagnostic-review.v1`、`maintenance_boundary_snapshot`、`origin_authority_bindings` schema/fixture、trusted resolver 与 ACL 限制的 append-only store 支持。
- 离线 API/CLI parity、diagnosis/review trusted-store provenance 与 freshness、robots identity/policy、server-generated maintenance snapshot/domain+exact-origin subset/exact-identity、per-request policy 与 PR0 SSRF transport enforcement（含 no-handler-call、mixed address、DNS rebinding、IP pinning、canonical Host authority、SNI/certificate/peer）、生命周期、上述角色矩阵、loopback/非 loopback fail-closed、capability 权限拒绝、1.0 path 回归、路径/symlink/冲突、原子写入和 contract fixture 测试。
- 根 `README.md` 的新增接口、候选目录和 SemVer 决策说明：本批采用向后兼容的 Minor 能力，不删除或强制迁移现有 path 请求。

完成标准：

- 同一 fixture 经 CLI 和 REST 产生语义一致的 inventory、classification、selection 和 scope。
- 新增 Agentic planning 请求在 diagnosis/review 任一缺失、过期、篡改、digest/origin/domain/port/identity/policy 不匹配、review disposition 未批准、非法状态/建议组合、review 未从 trusted store 解析或不是 operator capability 写入时 fail closed。有效 authority 从 discover/classify/selection/profile/scope/candidate Skill/compiled plan 一直透传到 job/attempt/evidence/report/manifest；单 origin 为单元素 `origin_authority_bindings`，多 origin 必须完整集合，不能退化为一个 diagnosis/review 字段。
- 测试证明 profile allowed domains、scope domains/target origins 和 candidate Site Skill domains 都是适用 reviewed diagnosis allowed domains 的子集并继续满足 profile ⊆ Site Skill；每个 exact target origin/port 都有 matching approved binding，跨 origin 页面/文件目标缺少自身 binding 时被拒绝。
- 测试证明 maintenance request 在阈值时自动取得 immutable snapshot、无需独立 review；maintenance diagnosis 的 origin/domain/port 只能是 snapshot 与仍有效 ancestor scope/profile/authority bindings 交集的子集，而 actual `User-Agent`、product token、identity ID/digest 必须逐项精确相等。任意扩域、新站点、unapproved/nondefault port、identity 变化、snapshot/ancestor 过期或 digest 变化均在首个请求前被拒绝，fixture 断言网络 handler 未调用；只有 operator 可建立新 boundary/identity 并重新 diagnosis。
- 不同 actual `User-Agent`、product token、匹配 group 或 identity digest 的 discovery/profile/Site Skill/executor 在请求前被拒绝并要求重做 diagnosis；多 origin `origin_authority_bindings` 中任一 identity 四元组不完全同质也在首个请求前被拒绝。Disallowed 或 SSRF/DNS rebinding/Host/SNI/certificate/peer 校验失败的 sitemap/seed/discovered page/file/redirect fixture 分别按校验阶段断言未建立目标连接，或虽完成 TCP/TLS 验证阶段但未发送 HTTP 应用请求字节且未调用应用层 handler。
- 测试证明伪造 review 文件/object/path、仅提供自哈希或 maintenance 直接写 store 均不能取得 authority；只有按 `review_id` 经 trusted resolver 取回、provenance/digest/binding 全部有效的 append-only record 可以通过。
- 可从候选生成走到 `draft -> probed -> reviewed -> active`；缺少/错误 capability、maintenance token 调用 review/activate、缺少操作员审核、digest 已变化或 Agent 直接激活都被稳定拒绝且不改文件。
- loopback 无 capability 只能调用矩阵中的只读操作；maintenance capability 只能在 server-generated snapshot 与当前有效 ancestor bindings 的交集内创建收紧 domain/origin/port、但 identity 四元组精确相等的 diagnosis，并在已有 trusted-store operator-approved review 后完成 discover/classify/probe/candidate/request 操作；它不能创建 diagnosis review、selection、scope、profile 或执行 Site Skill review/activate，identity 变化必须转 operator boundary/rediagnosis。新增端点在非 loopback 未配置认证时不能启动或处理请求。
- data-root active Skill 和持久化 profile 可通过新增 ID 请求完成 REST preview + bootstrap/run；现有 1.0 受控 path fixture 继续通过；内置/data-root 冲突、部分或错误绑定仍 fail closed。
- 静态操作不执行候选脚本，路径穿越和 symlink 越界被拒绝，失败写入不会留下半激活包。
- 现有 CLI/MCP 回归测试和完整测试通过，`git diff --check` 通过。

### PR2：三个页面的本地 Web 操作台

建议分支：`feat/local-operator-ui`

做什么：

- 由现有 FastAPI 服务静态托管轻量 HTML/CSS/JavaScript，不引入 SPA 构建链。
- 页面一“Explore & Build Skill”：输入网站和目标后，第一步分别展示 PR0 的 `diagnostic_status`、`recommendation`、canonical/辅助 origin 的 exact scheme/host/effective port、逐 origin robots identity/policy、全部触发原因、预算截断和 fallback；明确显示被 policy/authority 拒绝且“未发出网络请求”的 URL。只有 diagnosis 可继续且 operator capability 经服务端写入、再按 `review_id` 从 trusted store 解析出匹配 digest、未过期、`approved_for_planning` 的不可变 review，才执行 discovery/classification/probe。UI 不接受 review 文件/path 上传。
- 页面二“Run by Skill”：选择精确 scope/profile/Skill 版本，预览 execution plan 及其完整 `origin_authority_bindings`，执行 bootstrap 或 run，查看 job、attempt、artifact、逐 origin policy 决策和错误；单 origin 也按单元素集合展示。
- 页面三“Evidence & Content”：按 site/scope/run/type/time 查询页面、文件、变化、下载、报告和 manifest；正文只按需读取。
- 所有高风险动作需要明确确认；界面不显示 secret 值，不允许越过 scope/profile/Skill 权威。
- `serve` 默认仅绑定 `127.0.0.1`，禁止 wildcard CORS；UI 新增写接口校验同源请求并复用 PR1 operator/maintenance capability。显式绑定非 loopback 时若未配置认证则 fail closed；现有 1.0 路由不在本 PR 被强制迁移。
- 网站正文、错误和数据库内容始终按文本转义展示，禁止把远程 HTML 直接插入 DOM。

提交什么：

- `/ui/explore`、`/ui/run`、`/ui/evidence` 及共享静态资源。
- 必要的只读列表/筛选 API，不复制 planning 或执行逻辑。
- 页面/API 测试、浏览器 smoke 测试和 README 使用说明。

完成标准：

- 新用户可仅通过三个页面完成“canonical robots-first、跨 origin sitemap robots preflight 的诊断 -> 逐 origin 不可变 operator review/binding -> 候选 -> Skill 审核激活 -> preview -> bootstrap/run -> 查看证据”，并能核对 exact ports、完整 `origin_authority_bindings` 与每个 policy-blocked request 的未发送状态。
- 浏览器 smoke 覆盖三页主路径、错误状态和刷新后的 job 恢复。
- 页面不触发无界 crawl，不泄漏本地路径或 secrets；测试覆盖 loopback、CORS、同源/令牌、非 loopback 拒绝和存储型 XSS；API 测试和完整测试通过。

### PR3：独立的 Site Skill 健康与维护队列

建议分支：`feat/site-skill-health-loop`

做什么：

- 新增独立的 `skill_health` 调度，不复用内容监听的 job id 或状态。
- 新增 `site_skill_health_checks` 和 `skill_maintenance_requests` 存储，保留探测 URL、Skill 版本、adapter、结果、分类、连续失败数和 evidence/job 标识。
- 连续序列键固定为 `{site_key, skill_version, probe_target, adapter, check_kind}`；默认阈值为 3，可按站点配置，成功只清零同一序列。
- 开放 request 以序列键加 failure epoch 建数据库唯一约束；重复/并发调度不得生成第二个开放 request。达到阈值的同一事务中，服务端从当时 pinned、operator-reviewed scope/profile 及完整 `origin_authority_bindings` 生成 immutable boundary snapshot 并绑定 request；snapshot 保留 normalized origin key 及每项 diagnosis/review/identity/policy ID+digest+expiry，request 自动开放，不增加 operator-review 状态或 capability。
- 领取使用原子 `open -> claimed` 转换、owner、lease expiry 和 attempt；进程崩溃后过期 lease 可重领，完成操作幂等。
- 维护 Agent 是 REST 的外部消费者，持有 maintenance capability；其显式允许调用序列仅为 `claim/heartbeat -> site-diagnostics(snapshot ∩ current valid ancestor bindings 的 domain/origin/port 收紧子集 + exact identity) -> 等待 trusted-store operator diagnosis review -> discover -> classify -> probe -> candidates -> complete(awaiting_skill_review)`。它可在 snapshot 内创建 diagnosis，但 actual User-Agent/product token/identity ID+digest 必须与 snapshot 和当前 ancestor bindings 精确相等；request/snapshot 本身不 review。它不能新建站点、扩 origin/port/domain、替换 identity、调用 diagnosis review 或写 trusted store。只有服务端按 `review_id` 解析到 matching approved diagnosis review 后才可继续 discover。
- 每次 maintenance request 的重新探索都必须生成新的 diagnosis，并保存 `diagnostic_id`/digest 和后续 `review_id`/digest。发现新域时先转 `awaiting_operator_boundary`，不得自行把域加入 diagnosis；`retryable + retry_diagnosis` 保持 request 可重试；`blocked + operator_review` 或等待 review 时转为 `awaiting_diagnostic_review`；不得沿用旧 sitemap seed、旧 review 或直接跳到 discover。
- 阈值达到后只创建 request + immutable boundary snapshot，不自动改包、不自动激活、不自动重绑 scope。
- 提供手工触发、结果查询、request claim/heartbeat/complete 接口，供维护 Agent 和 UI 使用。

提交什么：

- 健康策略、独立 scheduler orchestration、含 immutable boundary snapshot ID/digest 的 SQLite schema/storage、REST API 和状态模型。
- 维护 Agent REST contract/lineage、request+snapshot fixture、完整 `origin_authority_bindings` 在 diagnosis/discovery/candidate/job/evidence/manifest 的透传，以及瞬时失败、序列隔离、恢复、去重、lease、并发和“工作 Agent 保持 pinned”测试。
- README 的健康任务配置和故障处理说明。

完成标准：

- 同一序列一次失败不创建 request，第三次连续失败创建一个；其他 adapter 成功不清零该序列，当前序列恢复后归零。
- 重复调度和并发执行不会重复创建开放 request；过期 lease 可重领，重复完成不产生第二份结果。
- 端到端测试覆盖 `threshold -> request+immutable snapshot(no request review) -> claim -> bounded diagnosis -> awaiting_diagnostic_review -> trusted-store operator-approved diagnosis review -> discover/classify/probe -> candidate + lineage -> awaiting_skill_review`，并证明 snapshot 精确复现触发时 pinned authority；扩域/新端口或 actual User-Agent/product token/identity ID/digest 任一变化都会在网络 handler 调用前停在 `awaiting_operator_boundary`/rediagnosis，ancestor 过期/变化会 fail closed，maintenance Agent 无法 review request、写 trusted store、创建 diagnosis review、自审或激活。
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
- manifest 导入及完整 `origin_authority_bindings` 持久化；集合中每个 normalized origin 的 diagnostic/review/robots identity/robots policy ID+digest+expiry 必须与 scope/profile/Skill/compiled plan/job/attempt/evidence/artifact/report/manifest lineage 一致，单 origin 也不得压平。
- 合同测试、失败/重试测试和应用 README/迁移说明。

完成标准：

- 应用不再直接决定 executor、绕过 profile 或自行抓取相同站点。
- 同一次运行可从应用记录的完整 `origin_authority_bindings` 逐 origin 追溯 diagnosis/review/robots identity/robots policy ID 与 digest，再追溯 scope、profile、Skill digest、compiled plan、job、attempt、evidence、artifact、report 和 manifest。
- 合同测试证明应用可以兼容当前稳定 envelope，并能正确处理失败和重试。

## 5. 调试顺序

目标顺序必须是：先离线验证 `site-diagnostic.v1`，再执行单站 robots/sitemap diagnosis；PR1 验证 operator capability、写入 trusted append-only store 并按 `review_id` 解析 matching `site-diagnostic-review.v1` 后，才做 discovery/probe，最后运行正式 scope。拟议的 `diagnose-site` 是 PR0 交付物；基于 `--diagnostic-id` + `--diagnostic-review-id` trusted lookup 的 CLI consumer 若保留则是 PR1 交付物；这些当前都不存在。各 PR 实现并更新根 `README.md` 前，不得把它们写成现行操作说明。

下列 PowerShell 仍是当前根 `README.md` 所描述的 1.0 回归基线，只用于验证既有命令，不能被视为已满足新的诊断前置要求，也不能作为 PR0—PR3 新路径的操作说明：

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

1. robots 是否为第一次 HTTP 请求，实际 User-Agent/product token/identity digest 是否一致，RFC 9309 group/rule 解释、状态、redirect、时间和 content digest 是否有效；maintenance identity 是否与 snapshot/current ancestors 精确相等而非“收紧”。
2. 声明或唯一 fallback sitemap 是否按确定性 FIFO/最多两次无 jitter retry 处理；跨 origin 文档是否先完成该 exact origin robots preflight；每个 HTTP/TLS/XML outcome 是否唯一归入 priority-1 safety、transient retry、completed-empty 或 deterministic non-safety terminal；所有请求是否通过 XML/gzip、wire/decoded/request/file/URL/depth、exact origin/port、policy 及逐跳 SSRF transport 检查，状态/建议是否为表中合法组合，是否完整保留所有高优先级原因。
3. Immutable operator review 是否按 `review_id` 从 trusted append-only store 解析，store provenance/ACL、record digest、diagnosis binding、subject/disposition/expiry 是否有效，是否仍在 diagnosis freshness 内。
4. Skill package 是否能静态解析、digest 是否匹配。
5. Maintenance diagnosis 的 domain/origin/port 是否严格等于 server-generated immutable snapshot 与当前仍有效 ancestor scope/profile/authority bindings 的交集或其收紧子集，actual User-Agent/product token/identity ID+digest 是否精确相等，request/snapshot 是否从未进入独立 review lifecycle；Profile/scope/candidate Skill domains 是否分别是适用 reviewed diagnosis allowed domains 的子集，profile 是否仍是 Site Skill domains 的非空子集，所有 target origin/port 是否有自身 approved binding，discovery/executor actual identity 是否精确匹配。
6. Probe 是网站不可达，还是当前 adapter/recipe 不满足质量门。
7. Execution plan 是否完整绑定六个字段并找到可用 executor。
8. Classification/inventory/selection、profile、scope、Skill、compiled plan、job、attempt、evidence、report、manifest 和 downstream 是否携带相同的完整 `origin_authority_bindings`，并能逐 normalized origin 核验 diagnosis/review/identity/policy ID+digest+expiry。
9. 仅对 bootstrap/run/report/job/artifact 等 MCP 与 REST 都支持的操作，检查同一 shared service/fixture 的结果是否语义一致。

## 6. 整体完成定义

- 操作员能在本地 UI 先完成 canonical robots-first、cross-origin sitemap robots preflight 的诊断，查看正交状态/建议、actual identity、exact origins/ports、逐 origin policy、全部原因和预算，再创建 digest-bound immutable diagnosis review，最后为一个新网站建立经审核的有界 Skill、profile 和 scope。
- 工作 Agent 能以完整 `origin_authority_bindings` 固定每个 target origin 的 diagnosis/review/identity/policy，逐请求重算 robots，并让每个 initial/redirect 复用 all-address validation、IP pinning、canonical Host authority、SNI/certificate 与 peer check 的 SSRF-safe transport；任何 disallowed/transport validation failure 都不调用 target handler，然后完成 bootstrap、增量 run、report 和 manifest 导出。
- 模拟网站结构漂移能触发维护 request，但不会改变 active Skill 或正在运行的 scope。
- 外部维护 Agent 能领取无独立 review lifecycle 的 request，严格依赖服务端阈值事务生成的 immutable boundary snapshot 与当前有效 ancestors 的 domain/origin/port 交集且保持 identity 四元组精确相等来创建 diagnosis，停在 `awaiting_diagnostic_review`，仅在 operator review 后生成带完整 lineage 的候选并停在 `awaiting_skill_review`，且无法创建 review、自审、扩 origin/port/domain、替换 identity 或激活。
- 页面、API、CLI、MCP、SQLite evidence 与下游应用可通过完整 `origin_authority_bindings` 逐 normalized origin 追溯 diagnosis/review/robots identity/robots policy ID+digest+expiry，并关联既有 scope/profile/Skill/plan/run/job/attempt/artifact/report/manifest 标识。
- 离线合同测试、完整测试、浏览器 smoke、reviewer gate、CI 和有效 review 评论全部通过。
