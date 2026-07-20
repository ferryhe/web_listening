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
5. [方向 D：MCP 工具规范与实现](#5-方向-dmcp-工具规范与实现)
6. [测试策略总览](#6-测试策略总览)
7. [审核检查清单](#7-审核检查清单)
8. [依赖关系图](#8-依赖关系图)

---

## 1. 总体路线图

```
迭代 1（2-3 周）
  ├── 方向 E：搜索抽象层（基础设施，5 个 provider）
  └── 方向 C：结构化 Change 元数据（独立，无外部依赖）

迭代 2（2-3 周）
  └── 方向 B：变化后自动搜索背景（依赖 E）

迭代 3（2-3 周）
  ├── 方向 D 阶段一：MCP 工具规范文档（依赖 E + C + B 全部就绪）
  └── 方向 D 阶段二：MCP Server 实现（fastmcp，依赖阶段一）
```

交付顺序的理由：
- **E 先行**：其他三个方向都需要"搜索能力"这个基础设施。E 提供 5 个 provider（Desearch、Tavily、Brave、SerpAPI、DuckDuckGo），agent 框架（OpenClaw 等）可按需选择。
- **C 可并行**：结构化 Change 元数据只改 `models.py` + `storage.py`，不引入新依赖，可与 E 同步进行。
- **B 依赖 E**：必须先有 `SearchProvider` 抽象才能在分析流水线中调用搜索。
- **D 分两阶段**：阶段一（规范文档）等 E/C/B 都落地后再写工具定义最准确；阶段二（MCP Server 实现）紧跟阶段一，在同一迭代中交付。

---

## 2. 方向 E：搜索 API 抽象层

### 2.1 目标

引入 `SearchProvider` 抽象，让其他模块可以调用"搜索"而不耦合具体搜索服务商。  
默认 provider 为 `NullSearchProvider`（不做任何事），避免破坏现有功能。  
同时提供 **5 个 provider** 供 OpenClaw 等 agent 框架按需选择。

> **关于 AI API 依赖**：当前项目的 OpenAI 分析功能是**完全可选的**。`Analyzer._generate_summary()` 在 `WL_OPENAI_API_KEY` 为空时自动降级为本地规则摘要（`_local_summary`），不调用任何外部服务。搜索 provider 同理，`NullSearchProvider` 为默认值，零外部依赖。

### 2.2 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `web_listening/blocks/searcher.py` | **新建** | Provider 抽象 + 6 个实现（含 Null） |
| `web_listening/config.py` | **修改** | 增加 `search_provider` + 各 provider 的 API key 配置项 |
| `pyproject.toml` | **修改** | 每个 provider 独立可选依赖组 |
| `tests/test_searcher.py` | **新建** | 单元测试（全部 provider） |
| `README.md` | **修改** | 新增配置表行、安装说明 |

### 2.3 详细实现规格

#### 支持的 Provider 一览

| Provider | 类名 | `WL_SEARCH_PROVIDER` 值 | 需要 API Key | 安装命令 | 特点 |
|---|---|---|---|---|---|
| 无操作（默认） | `NullSearchProvider` | `none` | ❌ | 无需安装 | 零依赖，安全默认值 |
| Desearch | `DesearchProvider` | `desearch` | ✅ `WL_DESEARCH_API_KEY` | `pip install 'web-listening[search-desearch]'` | Bittensor 去中心化，含 `quality_score` |
| Tavily | `TavilyProvider` | `tavily` | ✅ `WL_TAVILY_API_KEY` | `pip install 'web-listening[search-tavily]'` | AI 优化搜索，支持深度搜索，有免费额度 |
| Brave Search | `BraveSearchProvider` | `brave` | ✅ `WL_BRAVE_API_KEY` | `pip install 'web-listening[search-brave]'` | 隐私优先，有免费/付费计划，无需第三方 SDK |
| SerpAPI | `SerpAPIProvider` | `serpapi` | ✅ `WL_SERPAPI_API_KEY` | `pip install 'web-listening[search-serpapi]'` | Google/Bing/Yahoo 结果，稳定可靠 |
| DuckDuckGo | `DuckDuckGoProvider` | `duckduckgo` | ❌ | `pip install 'web-listening[search-duckduckgo]'` | 完全免费，无需注册，隐私友好 |

---

#### `web_listening/blocks/searcher.py`

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
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

    async def search(self, query: str, count: int = 10, start: int = 0) -> List[SearchResult]:
        return []


class DesearchProvider(SearchProvider):
    """基于 desearch-py SDK 的网页搜索实现（Bittensor Subnet 22）。"""

    def __init__(self, api_key: str, max_results: int = 10):
        self._api_key = api_key
        self._max_results = max_results

    async def search(self, query: str, count: int = 10, start: int = 0) -> List[SearchResult]:
        try:
            from desearch_py import Desearch
        except ImportError as exc:
            raise ImportError(
                "Install with: pip install 'web-listening[search-desearch]'"
            ) from exc

        limit = min(count, self._max_results)
        async with Desearch(api_key=self._api_key) as client:
            resp = await client.basic_web_search(query=query, count=limit, start=start)

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link") or item.get("url", ""),
                snippet=item.get("snippet") or item.get("description", ""),
                domain=item.get("domain", ""),
                quality_score=item.get("quality_score"),
                published_date=item.get("published_date"),
                relevance=item.get("relevance"),
            )
            for item in (resp.get("data") or [])
        ]


class TavilyProvider(SearchProvider):
    """基于 tavily-python SDK 的 AI 增强网页搜索实现。"""

    def __init__(self, api_key: str, search_depth: str = "basic", max_results: int = 10):
        self._api_key = api_key
        self._search_depth = search_depth   # "basic" 或 "advanced"
        self._max_results = max_results

    async def search(self, query: str, count: int = 10, start: int = 0) -> List[SearchResult]:
        try:
            from tavily import AsyncTavilyClient
        except ImportError as exc:
            raise ImportError(
                "Install with: pip install 'web-listening[search-tavily]'"
            ) from exc

        client = AsyncTavilyClient(api_key=self._api_key)
        limit = min(count, self._max_results)
        resp = await client.search(query=query, search_depth=self._search_depth, max_results=limit)

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("content", ""),
                domain=item.get("url", "").split("/")[2] if item.get("url") else "",
                relevance=item.get("score"),
                published_date=item.get("published_date"),
            )
            for item in (resp.get("results") or [])
        ]


class BraveSearchProvider(SearchProvider):
    """基于 Brave Search REST API 的实现（使用 httpx，已是基础依赖，无需额外安装）。"""

    _BASE_URL = "https://api.search.brave.com/res/v1/web/search"

    def __init__(self, api_key: str, max_results: int = 10):
        self._api_key = api_key
        self._max_results = max_results

    async def search(self, query: str, count: int = 10, start: int = 0) -> List[SearchResult]:
        import httpx

        limit = min(count, self._max_results)
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                self._BASE_URL,
                headers=headers,
                params={"q": query, "count": limit, "offset": start},
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("url", ""),
                snippet=item.get("description", ""),
                domain=item.get("meta_url", {}).get("hostname", ""),
                published_date=item.get("age"),
            )
            for item in (data.get("web", {}).get("results") or [])
        ]


class SerpAPIProvider(SearchProvider):
    """基于 SerpAPI 的搜索实现，支持 Google/Bing/Yahoo 结果。"""

    def __init__(self, api_key: str, engine: str = "google", max_results: int = 10):
        self._api_key = api_key
        self._engine = engine           # "google" | "bing" | "yahoo"
        self._max_results = max_results

    async def search(self, query: str, count: int = 10, start: int = 0) -> List[SearchResult]:
        try:
            from serpapi import GoogleSearch
        except ImportError as exc:
            raise ImportError(
                "Install with: pip install 'web-listening[search-serpapi]'"
            ) from exc

        import asyncio
        limit = min(count, self._max_results)
        params = {
            "q": query, "api_key": self._api_key,
            "engine": self._engine, "num": limit, "start": start,
        }
        loop = asyncio.get_event_loop()
        search_obj = GoogleSearch(params)
        raw = await loop.run_in_executor(None, search_obj.get_dict)

        return [
            SearchResult(
                title=item.get("title", ""),
                url=item.get("link", ""),
                snippet=item.get("snippet", ""),
                domain=item.get("displayed_link", ""),
                published_date=item.get("date"),
            )
            for item in (raw.get("organic_results") or [])
        ]


class DuckDuckGoProvider(SearchProvider):
    """基于 duckduckgo_search SDK 的免费搜索实现（无需 API Key）。"""

    def __init__(self, max_results: int = 10, region: str = "wt-wt"):
        self._max_results = max_results
        self._region = region           # "wt-wt" = 全球，"cn-zh" = 中文

    async def search(self, query: str, count: int = 10, start: int = 0) -> List[SearchResult]:
        try:
            from duckduckgo_search import AsyncDDGS
        except ImportError as exc:
            raise ImportError(
                "Install with: pip install 'web-listening[search-duckduckgo]'"
            ) from exc

        limit = min(count, self._max_results)
        results = []
        async with AsyncDDGS() as ddgs:
            async for item in ddgs.text(keywords=query, region=self._region, max_results=limit):
                results.append(SearchResult(
                    title=item.get("title", ""),
                    url=item.get("href", ""),
                    snippet=item.get("body", ""),
                    domain=item.get("href", "").split("/")[2] if item.get("href") else "",
                ))
        return results


def build_search_provider() -> SearchProvider:
    """根据 Settings 构建并返回合适的 SearchProvider 实例。"""
    from web_listening.config import settings

    provider = getattr(settings, "search_provider", "none").lower()

    if provider == "desearch":
        api_key = getattr(settings, "desearch_api_key", "")
        if not api_key:
            raise ValueError("WL_DESEARCH_API_KEY must be set when WL_SEARCH_PROVIDER=desearch")
        return DesearchProvider(api_key=api_key, max_results=settings.search_max_results)

    if provider == "tavily":
        api_key = getattr(settings, "tavily_api_key", "")
        if not api_key:
            raise ValueError("WL_TAVILY_API_KEY must be set when WL_SEARCH_PROVIDER=tavily")
        depth = getattr(settings, "tavily_search_depth", "basic")
        return TavilyProvider(api_key=api_key, search_depth=depth, max_results=settings.search_max_results)

    if provider == "brave":
        api_key = getattr(settings, "brave_api_key", "")
        if not api_key:
            raise ValueError("WL_BRAVE_API_KEY must be set when WL_SEARCH_PROVIDER=brave")
        return BraveSearchProvider(api_key=api_key, max_results=settings.search_max_results)

    if provider == "serpapi":
        api_key = getattr(settings, "serpapi_api_key", "")
        if not api_key:
            raise ValueError("WL_SERPAPI_API_KEY must be set when WL_SEARCH_PROVIDER=serpapi")
        engine = getattr(settings, "serpapi_engine", "google")
        return SerpAPIProvider(api_key=api_key, engine=engine, max_results=settings.search_max_results)

    if provider == "duckduckgo":
        region = getattr(settings, "duckduckgo_region", "wt-wt")
        return DuckDuckGoProvider(max_results=settings.search_max_results, region=region)

    return NullSearchProvider()
```

#### `web_listening/config.py` 新增字段

```python
# 搜索 provider 选择
search_provider: str = "none"           # none | desearch | tavily | brave | serpapi | duckduckgo
search_max_results: int = 10

# Desearch（Bittensor Subnet 22）
desearch_api_key: str = ""

# Tavily
tavily_api_key: str = ""
tavily_search_depth: str = "basic"      # basic | advanced

# Brave Search
brave_api_key: str = ""

# SerpAPI
serpapi_api_key: str = ""
serpapi_engine: str = "google"          # google | bing | yahoo

# DuckDuckGo（无需 API Key）
duckduckgo_region: str = "wt-wt"       # wt-wt（全球）| cn-zh（中文）等
```

#### `pyproject.toml` 新增可选依赖（每 provider 独立组）

```toml
[project.optional-dependencies]
search-desearch    = ["desearch-py>=0.1.0"]
search-tavily      = ["tavily-python>=0.3.0"]
search-brave       = []                          # 仅用 httpx（已是基础依赖）
search-serpapi     = ["google-search-results>=2.4.0"]
search-duckduckgo  = ["duckduckgo-search>=6.2.0"]
search             = [                           # 一键安装全部 provider
    "desearch-py>=0.1.0",
    "tavily-python>=0.3.0",
    "google-search-results>=2.4.0",
    "duckduckgo-search>=6.2.0",
]
```

#### `.env.example` 新增行

```dotenv
# 搜索 Provider（选一个）
WL_SEARCH_PROVIDER=none       # none | desearch | tavily | brave | serpapi | duckduckgo
WL_SEARCH_MAX_RESULTS=10

# Desearch（Bittensor 去中心化搜索）
WL_DESEARCH_API_KEY=

# Tavily（AI 增强搜索）
WL_TAVILY_API_KEY=
WL_TAVILY_SEARCH_DEPTH=basic  # basic | advanced

# Brave Search（隐私优先）
WL_BRAVE_API_KEY=

# SerpAPI（Google/Bing/Yahoo）
WL_SERPAPI_API_KEY=
WL_SERPAPI_ENGINE=google      # google | bing | yahoo

# DuckDuckGo（免费，无需 Key）
WL_DUCKDUCKGO_REGION=wt-wt   # wt-wt（全球）| cn-zh（中文）
```

### 2.4 测试规格（`tests/test_searcher.py`）

| 测试用例 | 方法 | 断言 |
|---|---|---|
| `NullSearchProvider` 返回空列表 | `asyncio.run(NullSearchProvider().search("test"))` | `== []` |
| `build_search_provider` 默认返回 `NullSearchProvider` | 不设置任何环境变量 | `isinstance(result, NullSearchProvider)` |
| `build_search_provider` 未知 provider 名降级为 `NullSearchProvider` | `search_provider = "unknown_engine"` | `isinstance(result, NullSearchProvider)` |
| `build_search_provider(desearch)` 无 key 抛出 `ValueError` | `search_provider="desearch"`，不设 key | `raises(ValueError)` |
| `build_search_provider(tavily)` 无 key 抛出 `ValueError` | `search_provider="tavily"`，不设 key | `raises(ValueError)` |
| `build_search_provider(brave)` 无 key 抛出 `ValueError` | `search_provider="brave"`，不设 key | `raises(ValueError)` |
| `build_search_provider(serpapi)` 无 key 抛出 `ValueError` | `search_provider="serpapi"`，不设 key | `raises(ValueError)` |
| `build_search_provider(duckduckgo)` 无需 key 正常返回 | `search_provider="duckduckgo"` | `isinstance(result, DuckDuckGoProvider)` |
| `DesearchProvider.search` 解析 API 响应 | mock `desearch_py.Desearch`，返回固定 dict | `len == 2`，`quality_score` 字段映射正确 |
| `TavilyProvider.search` 解析 API 响应 | mock `AsyncTavilyClient`，返回固定 dict | `relevance` 字段映射到 `score` |
| `BraveSearchProvider.search` 解析 API 响应 | mock `httpx.AsyncClient.get`，返回固定 JSON | `domain` 从 `meta_url.hostname` 提取 |
| `SerpAPIProvider.search` 解析 API 响应 | mock `GoogleSearch.get_dict`，返回固定 dict | `url` 从 `link` 字段映射 |
| `DuckDuckGoProvider.search` 解析 API 响应 | mock `AsyncDDGS.text`，yield 固定 dict | `snippet` 从 `body` 字段映射 |
| 任意 provider 的 SDK 未安装时给出清晰 `ImportError` | 对应 SDK 抛出 `ModuleNotFoundError` | `ImportError` 消息含 `pip install` 提示 |
| `SearchResult` 可选字段缺失不报错 | 最小参数构造 | `quality_score is None`，`published_date is None` |

### 2.5 审核检查点

- [ ] `NullSearchProvider` 是默认值，现有流程无任何改变
- [ ] 每个 provider 的 SDK 都只在对应的可选依赖组中，不污染基础安装
- [ ] `BraveSearchProvider` 使用 `httpx`（已是基础依赖），**不需要**额外 SDK
- [ ] `DuckDuckGoProvider` 不需要 API Key，`build_search_provider` 中不做 key 校验
- [ ] 所有需要 API Key 的 provider 在 key 为空时给出清晰的 `ValueError`（含 env var 名称）
- [ ] 所有 provider 的 SDK 未安装时给出清晰的 `ImportError`（含 `pip install` 命令）
- [ ] `Settings` 新字段全部有默认值，`WL_` 前缀，`.env.example` 同步更新
- [ ] `build_search_provider` 是纯工厂函数，不持有模块级状态
- [ ] 所有测试覆盖 happy path + error path（包括 SDK 未安装 mock）

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

## 5. 方向 D：MCP 工具规范与实现

### 5.1 目标

参考 desearch-web-search 的 OpenClaw Skill `SKILL.md` 规范格式，为 `web_listening` 的未来 MCP server 制定"工具说明书"：每个工具有清晰的名称、用途、参数、返回值和错误说明。本阶段交付的是**规范文档和工具接口契约**，而非 MCP server 的实现代码。

### 5.2 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `mcp/TOOLS.md` | **新建** | MCP 工具完整规范文档 |
| `mcp/RESOURCES.md` | **新建** | MCP 资源（Resource）完整规范文档 |
| `mcp/README.md` | **新建** | MCP server 概述与快速启动指南 |

> 实际 MCP server 代码（`mcp/server.py` 等）将在后续迭代中实现；本阶段只交付规范。

### 5.6 MCP Server 实现计划

本节将规范文档转化为可运行的 MCP server，作为方向 D 的第二阶段实施。

#### 5.6.1 涉及文件

| 文件 | 操作 | 说明 |
|---|---|---|
| `mcp/server.py` | **新建** | MCP server 主入口，使用 `fastmcp` 注册所有工具和资源 |
| `mcp/__init__.py` | **新建** | 空文件，使 `mcp/` 成为 Python 包 |
| `mcp/README.md` | **新建** | 快速启动指南 + 与 OpenClaw/LangChain/CrewAI 的集成说明 |
| `mcp/TOOLS.md` | **新建** | 每个工具的完整接口文档（从 5.3 节生成） |
| `mcp/RESOURCES.md` | **新建** | 每个资源的 URI 规范文档（从 5.4 节生成） |
| `pyproject.toml` | **修改** | 新增 `[mcp]` 可选依赖组；新增 `web-listening-mcp` 入口命令 |

#### 5.6.2 依赖选型

使用 [`fastmcp`](https://github.com/jlowin/fastmcp)（MCP 官方 Python SDK 的高层封装），原因：
- 装饰器语法与 FastAPI 高度一致，团队学习成本低
- 工具参数自动从函数签名生成 JSON Schema（与 Pydantic 集成）
- 支持 stdio、SSE、HTTP 三种传输层

```toml
# pyproject.toml 新增
[project.optional-dependencies]
mcp = [
    "fastmcp>=2.0.0",
]
```

```bash
# 启动命令
pip install 'web-listening[mcp]'
web-listening-mcp          # stdio 模式（供 Claude Desktop 等桌面客户端使用）
web-listening-mcp --transport sse --port 8001   # SSE 模式（供网页 agent 使用）
```

#### 5.6.3 `mcp/server.py` 详细实现规格

```python
"""web_listening MCP server — exposes monitoring capabilities as MCP tools."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import List, Optional

import fastmcp
from fastmcp import FastMCP

from web_listening.blocks.storage import Storage
from web_listening.config import settings

mcp = FastMCP(
    name="web-listening",
    instructions=(
        "A website monitoring assistant. "
        "Use add_site to register URLs, check_site to trigger a crawl, "
        "list_changes to see what changed, and run_analysis for an AI summary."
    ),
)

# ─── 工具实现 ────────────────────────────────────────────────────────────────

@mcp.tool()
def add_site(url: str, name: str = "", tags: List[str] = None, fetch_mode: str = "http") -> dict:
    """Add a URL to the monitoring watch list. Returns the created Site object."""
    from web_listening.models import Site
    tags = tags or []
    storage = Storage(settings.db_path)
    try:
        site = storage.add_site(Site(url=url, name=name or url, tags=tags, fetch_mode=fetch_mode))
        return site.model_dump()
    finally:
        storage.close()


@mcp.tool()
def list_sites(include_inactive: bool = False) -> List[dict]:
    """List all monitored websites."""
    storage = Storage(settings.db_path)
    try:
        sites = storage.list_sites()
        if not include_inactive:
            sites = [s for s in sites if s.is_active]
        return [s.model_dump() for s in sites]
    finally:
        storage.close()


@mcp.tool()
def check_site(site_id: int) -> dict:
    """Trigger an immediate crawl and change-detection for the given site (runs in background)."""
    import threading
    from web_listening.api.routes import _do_check
    threading.Thread(target=_do_check, args=(site_id,), daemon=True).start()
    return {"status": "check queued", "site_id": site_id}


@mcp.tool()
def list_changes(site_id: Optional[int] = None, since: Optional[str] = None) -> List[dict]:
    """List detected changes, optionally filtered by site or start date (ISO 8601)."""
    from dateutil import parser as dtparser
    storage = Storage(settings.db_path)
    try:
        since_dt = dtparser.parse(since) if since else None
        changes = storage.list_changes(site_id=site_id, since=since_dt)
        return [c.model_dump() for c in changes]
    finally:
        storage.close()


@mcp.tool()
def get_latest_snapshot(site_id: int) -> dict:
    """Return the most recent content snapshot for a site (includes markdown and links)."""
    storage = Storage(settings.db_path)
    try:
        snap = storage.get_latest_snapshot(site_id)
        if not snap:
            return {"error": f"No snapshot found for site {site_id}"}
        return snap.model_dump()
    finally:
        storage.close()


@mcp.tool()
def list_documents(institution: Optional[str] = None, site_id: Optional[int] = None) -> List[dict]:
    """List downloaded documents, optionally filtered by institution or site."""
    storage = Storage(settings.db_path)
    try:
        docs = storage.list_documents(site_id=site_id, institution=institution)
        return [d.model_dump() for d in docs]
    finally:
        storage.close()


@mcp.tool()
def run_analysis(since_date: Optional[str] = None) -> dict:
    """Run AI analysis on recent changes and return a Markdown report."""
    import asyncio
    from dateutil import parser as dtparser
    from web_listening.blocks.analyzer import Analyzer
    from web_listening.blocks.searcher import build_search_provider

    storage = Storage(settings.db_path)
    try:
        period_end = datetime.now(timezone.utc)
        period_start = dtparser.parse(since_date) if since_date else period_end - timedelta(days=7)
        changes = storage.list_changes(since=period_start)
        analyzer = Analyzer()
        search_provider = build_search_provider()
        report = asyncio.run(
            analyzer.analyze_with_search_context(
                changes, period_start, period_end,
                search_provider=search_provider,
            )
        )
        saved = storage.add_analysis(report)
        return saved.model_dump()
    finally:
        storage.close()


@mcp.tool()
def deactivate_site(site_id: int) -> dict:
    """Stop monitoring a site (soft delete)."""
    storage = Storage(settings.db_path)
    try:
        storage.deactivate_site(site_id)
        return {"status": "deactivated", "site_id": site_id}
    finally:
        storage.close()


# ─── 资源实现 ────────────────────────────────────────────────────────────────

@mcp.resource("site://{site_id}/latest")
def resource_latest_snapshot(site_id: int) -> str:
    """Latest snapshot markdown for a site."""
    storage = Storage(settings.db_path)
    try:
        snap = storage.get_latest_snapshot(site_id)
        return snap.fit_markdown or snap.markdown if snap else ""
    finally:
        storage.close()


@mcp.resource("changes://recent")
def resource_recent_changes() -> str:
    """Changes detected in the last 7 days as a formatted list."""
    from dateutil.relativedelta import relativedelta
    storage = Storage(settings.db_path)
    try:
        since = datetime.now(timezone.utc) - timedelta(days=7)
        changes = storage.list_changes(since=since)
        if not changes:
            return "No changes in the last 7 days."
        lines = [f"- [{c.change_type}] site={c.site_id}: {c.summary}" for c in changes]
        return "\n".join(lines)
    finally:
        storage.close()


@mcp.resource("analysis://latest")
def resource_latest_analysis() -> str:
    """Most recent AI analysis report in Markdown."""
    storage = Storage(settings.db_path)
    try:
        reports = storage.list_analyses()
        return reports[0].summary_md if reports else "No analysis reports yet."
    finally:
        storage.close()


# ─── 入口 ─────────────────────────────────────────────────────────────────────

def main():
    mcp.run()


if __name__ == "__main__":
    main()
```

#### 5.6.4 `pyproject.toml` 入口脚本

```toml
[project.scripts]
web-listening     = "web_listening.cli:app"
web-listening-mcp = "mcp.server:main"       # 新增 MCP server 入口
```

#### 5.6.5 `mcp/README.md` 快速启动（规格）

```markdown
# web-listening MCP Server

## 安装
pip install 'web-listening[mcp]'

## 启动（stdio 模式，供 Claude Desktop / OpenClaw 使用）
web-listening-mcp

## 启动（SSE 模式，供网页 agent 使用）
web-listening-mcp --transport sse --port 8001

## Claude Desktop 配置（~/.claude/config.json）
{
  "mcpServers": {
    "web-listening": {
      "command": "web-listening-mcp",
      "env": {
        "WL_DATA_DIR": "/path/to/data",
        "WL_OPENAI_API_KEY": "sk-...",
        "WL_SEARCH_PROVIDER": "duckduckgo"
      }
    }
  }
}

## 可用工具
- add_site / list_sites / deactivate_site
- check_site / get_latest_snapshot
- list_changes / list_documents
- run_analysis

## 可用资源
- site://{id}/latest — 站点最新 Markdown 快照
- changes://recent  — 最近 7 天变化
- analysis://latest — 最新分析报告
```

#### 5.6.6 测试规格（`tests/test_mcp_server.py`）

| 测试用例 | 方法 | 断言 |
|---|---|---|
| `add_site` 工具正确调用 `Storage.add_site` | mock Storage，调用工具 | 返回含 `id` 的 dict |
| `list_sites` 过滤 `include_inactive=False` | 存入 active + inactive 站点 | 只返回 active 站点 |
| `check_site` 在线程中异步执行 | mock `_do_check`，调用工具 | 立即返回 `{"status": "check queued"}` |
| `list_changes` 日期过滤正确 | mock Storage，`since="2026-04-01"` | 只返回该日期后的变化 |
| `get_latest_snapshot` 无快照时返回 error dict | mock Storage 返回 None | `result["error"]` 含 site_id |
| `run_analysis` 使用 `NullSearchProvider` 正常完成 | mock Analyzer，不设 API key | 返回含 `summary_md` 的 report |
| `resource_latest_snapshot` 返回 fit_markdown | mock Storage 返回 snap | 返回值为 `snap.fit_markdown` |
| MCP server 可以正常实例化（不报 ImportError） | `from mcp.server import mcp` | 无异常 |

#### 5.6.7 审核检查点（MCP Server 实现）

- [ ] `mcp/server.py` 中每个工具对应 5.3 节中的一个工具定义
- [ ] 工具返回值使用 Pydantic `.model_dump()` 序列化，类型稳定
- [ ] `check_site` 使用 `threading.Thread` 而非 asyncio（MCP stdio 环境不保证事件循环）
- [ ] `run_analysis` 使用 `asyncio.run()` 包裹异步 Analyzer 调用
- [ ] `resource_*` 函数返回字符串（MCP 资源内容必须为 str）
- [ ] MCP server 不自己管理数据库连接池，每次调用新建 Storage 并在 finally 中关闭
- [ ] `web-listening-mcp` CLI 入口在 `pyproject.toml` 中正确注册
- [ ] `fastmcp>=2.0.0` 只在 `[mcp]` 可选依赖组中，不影响基础安装

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
| `tests/test_searcher.py` | E | Provider 抽象、NullProvider、全部 5 个 provider 的 mock 测试 |
| `tests/test_storage.py`（扩展） | C | Change 新字段读写、迁移兼容 |
| `tests/test_api.py`（扩展） | C | Change API 响应新字段 |
| `tests/test_analyzer_search.py` | B | 搜索上下文注入 Analyzer |
| `tests/test_mcp_server.py` | D | MCP 工具函数、资源函数的单元测试 |

### 6.2 集成测试策略

- 所有搜索相关测试使用 `unittest.mock.AsyncMock` mock `SearchProvider.search`，**不发出真实网络请求**。
- 每个 provider 的实时集成测试（真实 API 调用）放在 `tests/test_searcher_live.py`，用各自的 `WL_*_API_KEY` 环境变量控制跳过（`pytest.mark.skipif`）。
- 数据库迁移兼容测试用 `tmp_path` 创建旧版 schema 的临时 SQLite 文件，验证新版 `Storage` 启动后能正常读写旧数据。
- MCP server 测试不启动真正的 MCP 进程，直接调用工具函数本身，用 `unittest.mock.patch` 替换 Storage 调用。

### 6.3 运行命令

```bash
# 单元测试（无网络需求）
pytest tests/test_searcher.py tests/test_storage.py tests/test_analyzer_search.py tests/test_api.py tests/test_mcp_server.py -v

# 完整测试套件（不含 live 测试）
pytest tests/ -v --ignore=tests/test_searcher_live.py

# 实时搜索测试（需要对应的 API Key）
WL_DESEARCH_API_KEY=<key>  pytest tests/test_searcher_live.py::TestDesearch -v
WL_TAVILY_API_KEY=<key>    pytest tests/test_searcher_live.py::TestTavily -v
WL_BRAVE_API_KEY=<key>     pytest tests/test_searcher_live.py::TestBrave -v
WL_SERPAPI_API_KEY=<key>   pytest tests/test_searcher_live.py::TestSerpAPI -v
pytest tests/test_searcher_live.py::TestDuckDuckGo -v  # 无需 Key
```

---

## 7. 审核检查清单

### 迭代 1 上线前

#### 方向 E

- [ ] `NullSearchProvider` 存在且为默认值
- [ ] 每个 provider 的 SDK 只在对应的可选依赖组中，不污染基础安装
- [ ] `BraveSearchProvider` 使用 `httpx`（已是基础依赖），不引入新依赖
- [ ] `DuckDuckGoProvider` 不需要 API Key，`build_search_provider` 不做 key 校验
- [ ] 所有需要 API Key 的 provider 在 key 为空时抛出清晰的 `ValueError`
- [ ] `Settings` 新字段全部有合理默认值，`WL_` 前缀，`.env.example` 同步更新
- [ ] `build_search_provider` 对未知 `search_provider` 值降级到 `NullSearchProvider`
- [ ] `README.md` 配置表新增对应行

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

#### 方向 D — 阶段一：规范文档

- [ ] 工具文档与实际 REST API 端点 100% 对应
- [ ] 工具名、参数名全部为 `snake_case`
- [ ] `mcp/TOOLS.md` 中每个工具有完整的参数表和返回说明
- [ ] `mcp/README.md` 列明实现 MCP server 的推荐依赖和启动步骤
- [ ] 规范中未出现现有代码不支持的能力

#### 方向 D — 阶段二：MCP Server 实现

- [ ] `mcp/server.py` 所有工具与 5.3 节规范一一对应
- [ ] `fastmcp>=2.0.0` 只在 `[mcp]` 可选依赖组中
- [ ] `web-listening-mcp` 入口命令在 `pyproject.toml` 中正确注册
- [ ] 工具函数使用 `.model_dump()` 序列化返回值，不返回原始 ORM 对象
- [ ] `check_site` 工具使用 `threading.Thread` 而非 asyncio（避免 MCP stdio 环境事件循环冲突）
- [ ] 资源函数（`resource_*`）返回类型为 `str`
- [ ] `tests/test_mcp_server.py` 覆盖全部工具的 happy path + error path
- [ ] `mcp/README.md` 包含 Claude Desktop 配置示例

---

## 8. 依赖关系图

```
方向 E（SearchProvider 抽象层：5 个 provider）
    │
    ├──────────────────────────┐
    ▼                          ▼
方向 B（变化后自动搜索）   方向 C（结构化 Change 元数据）
    │                          │
    └──────────┬───────────────┘
               ▼
         方向 D 阶段一（MCP 工具规范文档）
               │
               ▼
         方向 D 阶段二（MCP Server 实现 fastmcp）
```

**原则**：
- E 是基础设施，包含 5 个 provider（NullSearchProvider 为默认，零依赖），B 依赖它
- C 独立于 E 和 B，但 D 的工具规范需要 C 的字段定义才能准确
- D 分两阶段：先规范后实现，阶段二需要 E/C/B 都稳定
