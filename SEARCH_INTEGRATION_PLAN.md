# web_listening × desearch 集成开发与审核计划

> 计划日期：2026-04-06  
> 范围：按优先级顺序实施 E、C、B、D 四个改进方向  
> 原则：最小化变更、向后兼容、有测试才上线

---

## 目录

1. [总体路线图](#1-总体路线图)
2. [方向 E：搜索 API 抽象层](#2-方向-e搜索-api-抽象层)
3. [方向 C：结构化变化元数据](#3-方向-c结构化变化元数据)
4. [方向 B：变化后自动搜索背景](#4-方向-b变化后自动搜索背景)
5. [方向 D：MCP 工具规范](#5-方向-dmcp-工具规范)
6. [测试策略总览](#6-测试策略总览)
7. [审核检查清单](#7-审核检查清单)
8. [依赖关系图](#8-依赖关系图)

---

## 1. 总体路线图

```
迭代 1（2-3 周）
  ├── 方向 E：搜索抽象层（基础设施）
  └── 方向 C：结构化 Change 元数据（独立，无外部依赖）

迭代 2（2-3 周）
  └── 方向 B：变化后自动搜索背景（依赖 E）

迭代 3（2-3 周）
  └── 方向 D：MCP 工具规范（依赖 E + C + B 全部就绪）
```

交付顺序的理由：
- **E 先行**：其他三个方向都需要"搜索能力"这个基础设施。
- **C 可并行**：结构化 Change 元数据只改 `models.py` + `storage.py`，不引入新依赖，可与 E 同步进行。
- **B 依赖 E**：必须先有 `SearchProvider` 抽象才能在分析流水线中调用搜索。
- **D 最后**：MCP 工具描述需要稳定的后端契约，等 E/C/B 都落地后再写工具定义最准确。

---

## 2. 方向 E：搜索 API 抽象层

### 2.1 目标

引入 `SearchProvider` 抽象，让其他模块可以调用"搜索"而不耦合具体搜索服务商。  
默认 provider 为 `NullSearchProvider`（不做任何事），避免破坏现有功能。

### 2.2 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `web_listening/blocks/searcher.py` | **新建** | Provider 抽象 + 三个实现 |
| `web_listening/config.py` | **修改** | 增加 `search_provider`、`desearch_api_key`、`search_max_results` 配置项 |
| `pyproject.toml` | **修改** | 新增 `[project.optional-dependencies]` `search` 组 |
| `tests/test_searcher.py` | **新建** | 单元测试 |
| `README.md` | **修改** | 新增配置表行、安装说明 |

### 2.3 详细实现规格

#### `web_listening/blocks/searcher.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str
    domain: str = ""
    quality_score: Optional[float] = None
    published_date: Optional[str] = None
    relevance: Optional[float] = None


class SearchProvider(ABC):
    """网页搜索能力的统一抽象接口。"""

    @abstractmethod
    async def search(
        self,
        query: str,
        count: int = 10,
        start: int = 0,
    ) -> List[SearchResult]:
        """执行搜索，返回结构化结果列表。"""


class NullSearchProvider(SearchProvider):
    """无操作实现，默认值；搜索未配置时不报错。"""

    async def search(
        self,
        query: str,
        count: int = 10,
        start: int = 0,
    ) -> List[SearchResult]:
        return []


class DesearchProvider(SearchProvider):
    """基于 desearch-py SDK 的网页搜索实现。"""

    def __init__(self, api_key: str, max_results: int = 10):
        self._api_key = api_key
        self._max_results = max_results

    async def search(
        self,
        query: str,
        count: int = 10,
        start: int = 0,
    ) -> List[SearchResult]:
        try:
            from desearch_py import Desearch  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "desearch-py is required for DesearchProvider. "
                "Install it with: pip install 'web-listening[search]'"
            ) from exc

        limit = min(count, self._max_results)
        async with Desearch(api_key=self._api_key) as client:
            resp = await client.basic_web_search(
                query=query,
                count=limit,
                start=start,
            )

        results = []
        for item in (resp.get("data") or []):
            results.append(SearchResult(
                title=item.get("title", ""),
                url=item.get("link") or item.get("url", ""),
                snippet=item.get("snippet") or item.get("description", ""),
                domain=item.get("domain", ""),
                quality_score=item.get("quality_score"),
                published_date=item.get("published_date"),
                relevance=item.get("relevance"),
            ))
        return results


def build_search_provider() -> SearchProvider:
    """根据 Settings 构建并返回合适的 SearchProvider 实例。"""
    from web_listening.config import settings  # noqa: PLC0415

    provider = getattr(settings, "search_provider", "none").lower()
    if provider == "desearch":
        api_key = getattr(settings, "desearch_api_key", "")
        if not api_key:
            raise ValueError(
                "WL_DESEARCH_API_KEY must be set when WL_SEARCH_PROVIDER=desearch"
            )
        max_results = getattr(settings, "search_max_results", 10)
        return DesearchProvider(api_key=api_key, max_results=max_results)
    # 默认：none / 任何未知值
    return NullSearchProvider()
```

#### `web_listening/config.py` 新增字段

```python
search_provider: str = "none"       # none | desearch
desearch_api_key: str = ""
search_max_results: int = 10
```

#### `pyproject.toml` 新增可选依赖

```toml
[project.optional-dependencies]
search = [
    "desearch-py>=0.1.0",
]
```

#### `.env.example` 新增行

```dotenv
WL_SEARCH_PROVIDER=none             # none | desearch
WL_DESEARCH_API_KEY=                # 仅当 SEARCH_PROVIDER=desearch 时需要
WL_SEARCH_MAX_RESULTS=10
```

### 2.4 测试规格（`tests/test_searcher.py`）

| 测试用例 | 方法 | 断言 |
|---|---|---|
| `NullSearchProvider` 返回空列表 | `asyncio.run(NullSearchProvider().search("test"))` | `== []` |
| `build_search_provider` 默认返回 `NullSearchProvider` | 不设置任何环境变量 | `isinstance(result, NullSearchProvider)` |
| `build_search_provider` 配置 desearch 无 key 时抛出 `ValueError` | `settings.search_provider = "desearch"`，不设 key | `raises(ValueError)` |
| `DesearchProvider.search` 正确解析 API 响应 | mock `desearch_py.Desearch`，返回固定 dict | `len(results) == 2`，字段映射正确 |
| `SearchResult` 字段可选，缺失不报错 | 传入空 dict 构造 `SearchResult` | 无异常，可选字段为 `None` |

### 2.5 审核检查点

- [ ] `NullSearchProvider` 是默认值，现有流程无任何改变
- [ ] `desearch-py` 只在 `[search]` 可选依赖中，不污染基础安装
- [ ] `DesearchProvider` 在 `desearch-py` 未安装时给出清晰 `ImportError` 消息
- [ ] `Settings` 新字段全部有默认值，`WL_` 前缀，`.env.example` 同步更新
- [ ] `build_search_provider` 是纯工厂函数，不持有模块级状态
- [ ] 所有测试覆盖 happy path + error path

---

## 3. 方向 C：结构化变化元数据

### 3.1 目标

在现有 `Change` 模型中增加结构化字段，让每条变化记录除了自由文本外，还携带机器可读的元数据：变化类型语义、重要性分、关键词、受影响区域、证据链接。

### 3.2 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `web_listening/models.py` | **修改** | `Change` 模型新增 5 个可选字段 |
| `web_listening/blocks/storage.py` | **修改** | `changes` 表 DDL + `add_change` + `list_changes` |
| `web_listening/api/routes.py` | **修改** | `_do_check` 写入新字段（初始值）；`Change` response schema 自动更新 |
| `tests/test_storage.py` | **修改** | 新增字段读写测试 |
| `tests/test_api.py` | **修改** | 验证 API 响应中包含新字段 |

### 3.3 详细实现规格

#### `models.py` — `Change` 新增字段

```python
class Change(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: Optional[int] = None
    site_id: int
    detected_at: Optional[datetime] = None
    change_type: str                     # new_content | new_links | new_document
    summary: str = ""
    diff_snippet: str = ""

    # ── 新增结构化元数据 ──────────────────────────────────────
    importance_score: Optional[float] = None   # 0.0–1.0，None = 未评分
    semantic_type: Optional[str] = None        # regulation | publication | update | other
    keywords: List[str] = Field(default_factory=list)
    affected_sections: List[str] = Field(default_factory=list)
    evidence_urls: List[str] = Field(default_factory=list)

    @field_validator("keywords", "affected_sections", "evidence_urls", mode="before")
    @classmethod
    def parse_json_list(cls, v):
        if isinstance(v, str):
            try:
                return json.loads(v)
            except json.JSONDecodeError:
                return []
        return v or []
```

字段说明：

| 字段 | 类型 | 说明 |
|---|---|---|
| `importance_score` | `float \| None` | 0.0（无关紧要）～1.0（极重要），`None` 表示未评分 |
| `semantic_type` | `str \| None` | 变化的语义分类，枚举值：`regulation`、`publication`、`update`、`other`；`None` = 未分类 |
| `keywords` | `List[str]` | 从变化摘要或 diff 中提取的关键词，默认空列表 |
| `affected_sections` | `List[str]` | 发生变化的页面区域或章节（如 `"announcements"`, `"policy-updates"`） |
| `evidence_urls` | `List[str]` | 支撑该变化的证据链接（如新增文档 URL、变化页面截图 URL 等） |

#### `storage.py` — DDL 变更

`changes` 表新增列（向后兼容，全部有 DEFAULT）：

```sql
ALTER TABLE changes ADD COLUMN importance_score REAL;
ALTER TABLE changes ADD COLUMN semantic_type TEXT DEFAULT '';
ALTER TABLE changes ADD COLUMN keywords TEXT DEFAULT '[]';
ALTER TABLE changes ADD COLUMN affected_sections TEXT DEFAULT '[]';
ALTER TABLE changes ADD COLUMN evidence_urls TEXT DEFAULT '[]';
```

> 由于 SQLite 的 `ALTER TABLE ... ADD COLUMN` 不支持非常量默认值，将在 `create_tables()` 中通过 `IF NOT EXISTS` 方式实现迁移兼容性（详见下方迁移策略）。

#### `storage.py` — 迁移策略

在 `create_tables()` 末尾追加：

```python
_CHANGE_EXTRA_COLS = [
    ("importance_score", "REAL"),
    ("semantic_type",    "TEXT DEFAULT ''"),
    ("keywords",         "TEXT DEFAULT '[]'"),
    ("affected_sections","TEXT DEFAULT '[]'"),
    ("evidence_urls",    "TEXT DEFAULT '[]'"),
]

for col_name, col_def in _CHANGE_EXTRA_COLS:
    try:
        cur.execute(f"ALTER TABLE changes ADD COLUMN {col_name} {col_def}")
    except sqlite3.OperationalError:
        pass  # 列已存在，忽略
```

这样现有数据库在首次启动新版本时会自动迁移，无需额外脚本。

#### `storage.py` — `add_change` 更新

```python
def add_change(self, change: Change) -> Change:
    cur = self.conn.cursor()
    cur.execute(
        """INSERT INTO changes
           (site_id, detected_at, change_type, summary, diff_snippet,
            importance_score, semantic_type, keywords, affected_sections, evidence_urls)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            change.site_id,
            _now_iso(),
            change.change_type,
            change.summary,
            change.diff_snippet,
            change.importance_score,
            change.semantic_type or "",
            json.dumps(change.keywords),
            json.dumps(change.affected_sections),
            json.dumps(change.evidence_urls),
        ),
    )
    self.conn.commit()
    change.id = cur.lastrowid
    return change
```

#### `api/routes.py` — `_do_check` 初始赋值示例

在 `_do_check` 中写入 `Change` 时，为 `evidence_urls` 填入新发现的文档链接：

```python
if doc_links:
    storage.add_change(Change(
        site_id=site.id,
        detected_at=datetime.now(timezone.utc),
        change_type="new_document",
        summary=f"{len(doc_links)} new document links",
        diff_snippet="\n".join(doc_links[:10]),
        evidence_urls=doc_links[:20],       # ← 新字段
        importance_score=0.8,               # ← 文档变化默认高重要性
        semantic_type="publication",        # ← 语义分类
    ))
```

### 3.4 测试规格

| 测试文件 | 测试用例 | 断言 |
|---|---|---|
| `tests/test_storage.py` | 写入带新字段的 Change，再读回 | 字段值完全匹配 |
| `tests/test_storage.py` | 写入不含新字段的 Change（旧接口兼容） | 无异常，新字段为 None / [] |
| `tests/test_storage.py` | 旧数据库（无新列）启动新版本后 `list_changes` 正常返回 | 旧记录新字段为 None / [] |
| `tests/test_api.py` | `GET /api/v1/changes` 响应包含 `importance_score` 字段 | JSON 中有该 key |
| `tests/test_api.py` | `POST /api/v1/sites/{id}/check` 后新增的 Change 含 `evidence_urls` | 文档链接出现在 evidence_urls |

### 3.5 审核检查点

- [ ] 所有新字段均为可选（`Optional` 或带 `default_factory`），不破坏现有 `Change` 构造
- [ ] `storage.py` 的 DDL 迁移逻辑有 `try/except OperationalError` 保护
- [ ] `importance_score` 范围未在代码中强制（留给业务层决定），但文档说明 0–1
- [ ] `semantic_type` 枚举值在注释中有说明，但类型为 `str` 以保持可扩展性
- [ ] API Response schema（`Change`）自动包含新字段，无需额外改 routes.py 响应类
- [ ] 测试覆盖迁移兼容场景（旧表 → 新版本启动）

---

## 4. 方向 B：变化后自动搜索背景

### 4.1 目标

当 `web_listening` 检测到重要变化时（如新文档、重要内容更新），自动通过 `SearchProvider` 搜索相关背景信息，将搜索结果注入 AI 分析摘要，使报告更具上下文。

### 4.2 依赖

**必须先完成方向 E**（`SearchProvider` 抽象层）。

### 4.3 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `web_listening/blocks/analyzer.py` | **修改** | 新增 `analyze_with_search_context` 方法，可选传入 `SearchProvider` |
| `web_listening/api/routes.py` | **修改** | `run_analysis` 端点传入 `SearchProvider` |
| `web_listening/cli.py` | **修改** | `analyze` 命令传入 `SearchProvider` |
| `tests/test_analyzer_search.py` | **新建** | 单元测试 |

### 4.4 详细实现规格

#### `blocks/analyzer.py` — 新增方法

```python
from web_listening.blocks.searcher import SearchProvider, NullSearchProvider, SearchResult

class Analyzer:
    # 现有方法保持不变…

    async def analyze_with_search_context(
        self,
        changes: List[Change],
        period_start: datetime,
        period_end: datetime,
        search_provider: Optional[SearchProvider] = None,
        search_count: int = 5,
    ) -> AnalysisReport:
        """
        与 analyze_changes() 功能相同，但在生成 AI 摘要前，
        先针对重要变化触发搜索，并将搜索结果作为上下文附加到 prompt 中。
        """
        provider = search_provider or NullSearchProvider()

        # 只对重要变化（文档更新、高 importance_score）触发搜索
        search_context = ""
        if changes:
            query = self._build_search_query(changes)
            if query:
                results = await provider.search(query, count=search_count)
                search_context = self._format_search_context(results)

        if not changes:
            summary = "No changes detected during this period."
        else:
            summary = self._generate_summary_with_context(changes, search_context)

        site_ids = list({c.site_id for c in changes})
        return AnalysisReport(
            period_start=period_start,
            period_end=period_end,
            generated_at=datetime.now(timezone.utc),
            site_ids=site_ids,
            summary_md=summary,
            change_count=len(changes),
        )

    def _build_search_query(self, changes: List[Change]) -> str:
        """
        从变化列表中提取搜索关键词。
        优先使用 keywords 字段，降级到 summary 文本。
        """
        all_keywords = []
        for c in changes[:5]:  # 只取前 5 条变化
            if c.keywords:
                all_keywords.extend(c.keywords[:3])
            elif c.summary:
                # 简单截取 summary 中的关键词
                words = [w for w in c.summary.split() if len(w) > 3]
                all_keywords.extend(words[:3])
        if not all_keywords:
            return ""
        # 去重，最多 8 个词
        seen = set()
        unique = [w for w in all_keywords if not (w in seen or seen.add(w))]
        return " ".join(unique[:8])

    def _format_search_context(self, results: List[SearchResult]) -> str:
        if not results:
            return ""
        lines = ["### Related Background (from web search)", ""]
        for r in results:
            lines.append(f"- **{r.title}** — {r.snippet}")
            lines.append(f"  Source: {r.url}")
        return "\n".join(lines)

    def _generate_summary_with_context(
        self,
        changes: List[Change],
        search_context: str,
    ) -> str:
        if not settings.openai_api_key:
            base = self._local_summary(changes)
            if search_context:
                base += f"\n\n{search_context}"
            return base

        changes_text = "\n".join(
            f"- [{c.change_type}] Site {c.site_id} at {c.detected_at}: {c.summary}"
            for c in changes[:100]
        )
        context_section = f"\n\nRelated background from web search:\n{search_context}" if search_context else ""
        prompt = (
            "You are a research analyst. Below are website monitoring changes detected over the past week.\n"
            "Please write a concise markdown summary of the key changes, grouped by type and significance.\n"
            "Use the related background section to enrich your summary with context where relevant.\n\n"
            f"Changes:\n{changes_text}"
            f"{context_section}"
        )

        try:
            resp = self.client.chat.completions.create(
                model=settings.openai_model,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2000,
            )
            return resp.choices[0].message.content
        except Exception as e:
            base = self._local_summary(changes)
            if search_context:
                base += f"\n\n{search_context}"
            return base + f"\n\n[AI analysis unavailable: {e}]"
```

#### `api/routes.py` — `run_analysis` 端点更新

```python
@router.post("/analyze", response_model=AnalysisReport)
async def run_analysis(body: AnalyzeRequest):
    from web_listening.blocks.analyzer import Analyzer
    from web_listening.blocks.searcher import build_search_provider
    from dateutil import parser as dtparser

    storage = get_storage()
    try:
        period_end = datetime.now(timezone.utc)
        period_start = dtparser.parse(body.since_date) if body.since_date else period_end - timedelta(days=7)

        changes = storage.list_changes(since=period_start)
        analyzer = Analyzer()
        search_provider = build_search_provider()
        report = await analyzer.analyze_with_search_context(
            changes, period_start, period_end,
            search_provider=search_provider,
        )
        return storage.add_analysis(report)
    finally:
        storage.close()
```

> 注意：`run_analysis` 需从 `def` 改为 `async def`，FastAPI 支持异步端点。

#### `cli.py` — `analyze` 命令更新

```python
@app.command()
def analyze(since: str = typer.Option(None, "--since")):
    """Run AI analysis of recent changes."""
    import asyncio
    from web_listening.blocks.searcher import build_search_provider

    # …现有逻辑…
    search_provider = build_search_provider()
    report = asyncio.run(
        analyzer.analyze_with_search_context(
            changes, period_start, period_end,
            search_provider=search_provider,
        )
    )
```

### 4.5 测试规格（`tests/test_analyzer_search.py`）

| 测试用例 | 方法 | 断言 |
|---|---|---|
| `NullSearchProvider` 时摘要正常生成（不受搜索影响） | 传入 `NullSearchProvider()` | 摘要非空，不含 "Related Background" |
| `_build_search_query` 从 `keywords` 字段提取正确 | 构造含 keywords 的 Change 列表 | 查询字符串包含 keywords |
| `_build_search_query` 降级到 summary 文本 | keywords 为空的 Change 列表 | 查询字符串非空 |
| `_format_search_context` 空结果返回空字符串 | `results=[]` | `== ""` |
| `analyze_with_search_context` mock provider 返回2条结果 | mock `search()` 返回 2 个 SearchResult | 摘要中含 "Related Background"（本地摘要路径） |
| `analyze_with_search_context` 无变化时不触发搜索 | `changes=[]` | `provider.search` 未被调用 |

### 4.6 审核检查点

- [ ] `analyze_changes()` 同步接口保持不变（向后兼容），新增 `analyze_with_search_context()` 为异步方法
- [ ] 搜索失败（`provider.search` 抛出异常）不影响摘要生成（有 `try/except` 保护）
- [ ] `NullSearchProvider` 时行为与现有 `analyze_changes()` 完全相同
- [ ] `run_analysis` API 端点改为 `async def`，确认 FastAPI + uvicorn 配置不需要额外改动
- [ ] CLI `analyze` 命令使用 `asyncio.run()`，非 async 上下文下可正常调用
- [ ] 搜索结果不写入数据库（仅用于当次摘要生成），避免数据膨胀

---

## 5. 方向 D：MCP 工具规范

### 5.1 目标

参考 desearch-web-search 的 OpenClaw Skill `SKILL.md` 规范格式，为 `web_listening` 的未来 MCP server 制定"工具说明书"：每个工具有清晰的名称、用途、参数、返回值和错误说明。本阶段交付的是**规范文档和工具接口契约**，而非 MCP server 的实现代码。

### 5.2 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `mcp/TOOLS.md` | **新建** | MCP 工具完整规范文档 |
| `mcp/RESOURCES.md` | **新建** | MCP 资源（Resource）完整规范文档 |
| `mcp/README.md` | **新建** | MCP server 概述与快速启动指南 |

> 实际 MCP server 代码（`mcp/server.py` 等）将在后续迭代中实现；本阶段只交付规范。

### 5.3 MCP 工具列表（`mcp/TOOLS.md` 内容）

以下为本阶段需要规范化的 9 个工具：

---

#### 工具 1：`add_site`

```
名称：add_site
用途：向监控列表添加一个新网站
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `url` | string | ✅ | 监控目标 URL（http/https） |
| `name` | string | ❌ | 站点显示名称，默认为 URL |
| `tags` | string[] | ❌ | 分类标签，默认空列表 |
| `fetch_mode` | string | ❌ | `http`（默认）、`browser`、`auto` |
| `fetch_config_json` | object | ❌ | 抓取配置，如 `{"wait_for": "main"}` |

**返回**：`Site` 对象（含 `id`、`url`、`name`、`tags`、`created_at`）

**错误**：`INVALID_URL`（非 http/https scheme）

---

#### 工具 2：`list_sites`

```
名称：list_sites
用途：列出所有当前被监控的网站
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `include_inactive` | bool | ❌ | 是否包含已停用站点，默认 false |

**返回**：`Site[]`

---

#### 工具 3：`check_site`

```
名称：check_site
用途：立即触发对指定站点的抓取与变化检测（后台异步执行）
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site_id` | integer | ✅ | 站点 ID |

**返回**：`{"status": "check queued", "site_id": <id>}`

**注意**：任务在后台执行，调用方应随后调用 `list_changes` 查询结果

---

#### 工具 4：`list_changes`

```
名称：list_changes
用途：查询指定时间范围内的页面变化记录
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site_id` | integer | ❌ | 过滤特定站点 |
| `since` | string | ❌ | ISO 8601 时间戳，如 `2026-04-01T00:00:00Z` |

**返回**：`Change[]`（含 `importance_score`、`semantic_type`、`keywords`、`evidence_urls` 等新字段）

---

#### 工具 5：`get_latest_snapshot`

```
名称：get_latest_snapshot
用途：获取指定站点的最新内容快照（含 Markdown、fit_markdown 等 agent 可读格式）
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site_id` | integer | ✅ | 站点 ID |

**返回**：`SiteSnapshot`（含 `markdown`、`fit_markdown`、`metadata_json`、`links`）

---

#### 工具 6：`download_documents`

```
名称：download_documents
用途：下载指定站点最新快照中发现的文档链接
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site_id` | integer | ✅ | 站点 ID |
| `institution` | string | ✅ | 机构标识符（用于文档分组） |
| `url` | string | ❌ | 指定下载单个 URL，不填则下载快照中所有文档链接 |

**返回**：`{"status": "download queued", "site_id": <id>}`

---

#### 工具 7：`list_documents`

```
名称：list_documents
用途：查询已下载的文档记录
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `institution` | string | ❌ | 过滤特定机构 |
| `site_id` | integer | ❌ | 过滤特定站点 |

**返回**：`Document[]`（含 `local_path`、`sha256`、`content_md_status`）

---

#### 工具 8：`run_analysis`

```
名称：run_analysis
用途：对指定时间段内的变化运行 AI 分析，生成 Markdown 报告
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `since_date` | string | ❌ | ISO 8601 日期，默认为 7 天前 |

**返回**：`AnalysisReport`（含 `summary_md`、`change_count`、`period_start`、`period_end`）

**说明**：若配置了 `WL_SEARCH_PROVIDER`，摘要中将包含来自 web 搜索的背景信息

---

#### 工具 9：`rescue_check`

```
名称：rescue_check
用途：对指定站点运行 rescue ladder（梯级容错抓取）并返回尝试日志，不写入数据库
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `site_id` | integer | ✅ | 站点 ID |
| `allow_browser` | bool | ❌ | 是否允许浏览器模式，默认 true |
| `allow_official_feeds` | bool | ❌ | 是否尝试 sitemap/rss，默认 true |

**返回**：`RescueCheckResponse`（含 `resolved`、`attempts[]`、`winning_snapshot`）

---

### 5.4 MCP 资源列表（`mcp/RESOURCES.md` 内容节选）

| Resource URI | 说明 | 对应 REST API |
|---|---|---|
| `site://{id}/latest` | 站点最新快照 | `GET /api/v1/sites/{id}/snapshots/latest` |
| `site://{id}/state` | 站点基本信息 + 上次检查时间 | `GET /api/v1/sites/{id}` |
| `changes://recent` | 最近 7 天变化列表 | `GET /api/v1/changes` |
| `documents://site/{id}` | 站点文档列表 | `GET /api/v1/documents?site_id={id}` |
| `analysis://latest` | 最新分析报告 | `GET /api/v1/analyses`（取第一条） |

### 5.5 审核检查点

- [ ] 所有工具名使用 `snake_case`
- [ ] 所有工具的参数、返回值、错误类型均有清晰说明
- [ ] 必填参数和可选参数明确区分
- [ ] 工具描述不超过 2 句话（agent 的 tool description 要简短）
- [ ] Resource URI scheme 格式一致
- [ ] 工具列表与现有 REST API 端点完全对应，无凭空发明的能力
- [ ] `mcp/README.md` 说明未来实现时的依赖（`fastmcp` 或 `mcp` SDK）

---

## 6. 测试策略总览

### 6.1 单元测试

每个方向单独建立测试文件，互不干扰：

| 测试文件 | 覆盖方向 | 说明 |
|---|---|---|
| `tests/test_searcher.py` | E | Provider 抽象、NullProvider、mock DesearchProvider |
| `tests/test_storage.py`（扩展） | C | Change 新字段读写、迁移兼容 |
| `tests/test_api.py`（扩展） | C | Change API 响应新字段 |
| `tests/test_analyzer_search.py` | B | 搜索上下文注入 Analyzer |

### 6.2 集成测试策略

- 所有搜索相关测试使用 `unittest.mock.AsyncMock` mock `SearchProvider.search`，**不发出真实网络请求**。
- `DesearchProvider` 的集成测试（真实 API 调用）放在 `tests/test_searcher_live.py`，用环境变量 `WL_DESEARCH_API_KEY` 跳过（`pytest.mark.skipif`）。
- 数据库迁移兼容测试用 `tmp_path` 创建旧版 schema 的临时 SQLite 文件，验证新版 `Storage` 启动后能正常读写旧数据。

### 6.3 运行命令

```bash
# 单元测试（无网络需求）
pytest tests/test_searcher.py tests/test_storage.py tests/test_analyzer_search.py tests/test_api.py -v

# 完整测试套件（不含 live 测试）
pytest tests/ -v --ignore=tests/test_searcher_live.py

# 实时搜索测试（需要 API Key）
WL_DESEARCH_API_KEY=<your-key> pytest tests/test_searcher_live.py -v
```

---

## 7. 审核检查清单

### 迭代 1 上线前

#### 方向 E

- [ ] `NullSearchProvider` 存在且为默认值
- [ ] `desearch-py` 只在 `[search]` optional dependencies 中
- [ ] `Settings` 新字段全部有合理默认值
- [ ] `build_search_provider` 对未知 `search_provider` 值降级到 `NullSearchProvider`
- [ ] `.env.example` 同步更新
- [ ] `README.md` 配置表新增 3 行

#### 方向 C

- [ ] `Change` 新字段全部可选（不破坏现有代码）
- [ ] SQLite 迁移有 `try/except OperationalError` 保护
- [ ] `list_changes` 正确返回旧记录（新字段为 None/[]）
- [ ] API 响应 schema 自动包含新字段（Pydantic response_model）
- [ ] `_do_check` 中 `new_document` 类型的 Change 填入 `evidence_urls`

### 迭代 2 上线前

#### 方向 B

- [ ] `analyze_changes()` 同步接口不变
- [ ] 搜索失败时摘要仍然正常生成
- [ ] `run_analysis` 端点改为 `async def`
- [ ] CLI `analyze` 使用 `asyncio.run()` 包裹异步调用
- [ ] 搜索结果不持久化到数据库

### 迭代 3 上线前

#### 方向 D

- [ ] 工具文档与实际 REST API 端点 100% 对应
- [ ] 工具名、参数名全部为 `snake_case`
- [ ] `mcp/TOOLS.md` 中每个工具有完整的参数表和返回说明
- [ ] `mcp/README.md` 列明实现 MCP server 的推荐依赖和启动步骤
- [ ] 规范中未出现现有代码不支持的能力

---

## 8. 依赖关系图

```
方向 E（SearchProvider 抽象层）
    │
    ├──────────────────────────┐
    ▼                          ▼
方向 B（变化后自动搜索）   方向 C（结构化 Change 元数据）
    │                          │
    └──────────┬───────────────┘
               ▼
         方向 D（MCP 工具规范）
```

**原则**：
- E 是基础设施，B 依赖它，其余不依赖
- C 独立于 E 和 B，但 D 的工具规范需要 C 的字段定义才能准确
- D 是最终交付，需要 E/C/B 都稳定后才能准确描述工具契约
