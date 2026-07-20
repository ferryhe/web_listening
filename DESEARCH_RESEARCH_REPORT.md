# desearch-web-search 项目研究报告

> 报告日期：2026-04-06  
> 作者：自动生成（基于官方文档、GitHub 公开仓库及社区资料）

---

## 目录

1. [研究背景与目的](#1-研究背景与目的)
2. [项目概览](#2-项目概览)
   - 2.1 定位与起源
   - 2.2 核心组件
3. [核心功能详解](#3-核心功能详解)
   - 3.1 基础网页搜索（desearch-web-search skill）
   - 3.2 AI 综合搜索（ai_search）
   - 3.3 网页爬取（web_crawl）
   - 3.4 多源聚合搜索
4. [技术架构](#4-技术架构)
   - 4.1 去中心化底层（Bittensor Subnet 22）
   - 4.2 Python SDK（desearch-py）
   - 4.3 OpenClaw Skill（desearch-web-search）
5. [API 能力与结果结构](#5-api-能力与结果结构)
   - 5.1 接口清单
   - 5.2 结果字段示例
   - 5.3 分页机制
6. [集成生态](#6-集成生态)
7. [优缺点分析](#7-优缺点分析)
8. [对本项目（web_listening）的改进建议](#8-对本项目-web_listening-的改进建议)
   - 8.1 现状差距分析
   - 8.2 可借鉴的设计理念
   - 8.3 具体改进方向与落地建议
9. [参考资料](#9-参考资料)

---

## 1. 研究背景与目的

`web_listening` 项目当前的核心能力是：监控指定网站的内容变化、下载文档、生成 AI 摘要。  
其信息来源完全依赖"被动爬取"——只能监控项目预先配置好的若干固定 URL。

`desearch-web-search` 代表了另一种获取互联网信息的方式：**主动、实时地通过搜索引擎查询获得结构化结果**。  
通过研究这一工具，可以为 `web_listening` 的下一阶段演进提供如下参考：

- 如何在监控到变化后，自动搜索相关背景信息？
- 如何引入"主动发现"能力，而不仅靠固定 URL 监控？
- 如何获取更结构化、对 AI agent 更友好的搜索结果？

---

## 2. 项目概览

### 2.1 定位与起源

| 属性 | 详情 |
|---|---|
| **项目名称** | desearch-web-search |
| **所属组织** | [Desearch-ai](https://github.com/Desearch-ai) |
| **Python SDK** | [desearch-py](https://pypi.org/project/desearch-py/) |
| **OpenClaw Skill** | [desearch-openclaw-skills](https://github.com/Desearch-ai/desearch-openclaw-skills) |
| **官网** | [desearch.ai](https://desearch.ai) |
| **底层网络** | Bittensor 去中心化网络（Subnet 22） |
| **协议** | MIT |

`desearch-web-search` 本质上是 **Desearch AI** 平台提供的一项能力，有两种使用形态：

1. **OpenClaw Skill**：一个独立的命令行脚本（`desearch.py`），专门为 AI agent 框架（OpenClaw）设计，可以像工具一样被 agent 调用。
2. **Python SDK（desearch-py）**：更完整的异步 Python 客户端，暴露完整的 Desearch API 能力。

### 2.2 核心组件

```
Desearch 平台
  ├── desearch-web-search（OpenClaw Skill）
  │     └── desearch.py 脚本：CLI + agent 工具调用入口
  ├── desearch-py（Python SDK）
  │     ├── ai_search()
  │     ├── basic_web_search()
  │     ├── web_links_search()
  │     ├── web_crawl()
  │     ├── basic_twitter_search()
  │     └── twitter_links_search() / reddit / youtube / ...
  └── Desearch API（REST 后端）
        └── 由 Bittensor Subnet 22 上的去中心化 miners 提供数据
```

---

## 3. 核心功能详解

### 3.1 基础网页搜索（desearch-web-search skill）

这是 `desearch-web-search` 最基础的功能：接受一个自然语言查询，返回结构化的 SERP（搜索引擎结果页）结果。

**CLI 用法**：

```bash
# 设置 API Key
export DESEARCH_API_KEY='your-api-key'

# 基础搜索
desearch.py web "machine learning news"

# 翻页（从第 11 条开始）
desearch.py web "machine learning news" --start 10
```

**Python SDK 用法**：

```python
from desearch_py import Desearch
import asyncio

async def main():
    async with Desearch(api_key="your-api-key") as d:
        result = await d.basic_web_search(
            query="machine learning news",
            count=10,
            start=0,
        )
        print(result)

asyncio.run(main())
```

**返回结构**：

```json
{
  "data": [
    {
      "title": "页面标题",
      "link": "https://www.example.com/page",
      "snippet": "页面摘要或关键内容片段...",
      "domain": "example.com",
      "quality_score": 0.92,
      "published_date": "2026-04-01T10:00:00Z"
    }
  ]
}
```

### 3.2 AI 综合搜索（ai_search）

在基础网页搜索之上，增加了 AI 生成的摘要层，适合需要"先搜后总结"的场景：

```python
result = await d.ai_search(
    prompt="2026年最新AI法规动态",
    tools=["web", "twitter", "reddit"],
    date_filter="PAST_7_DAYS",
    result_type="LINKS_WITH_FINAL_SUMMARY",
    count=10,
)
```

返回内容除了链接列表外，还包含一段综合 AI 摘要，附有来源引用。

### 3.3 网页爬取（web_crawl）

给定 URL，抓取并返回清洗后的网页内容：

```python
result = await d.web_crawl(url="https://example.com/article")
# 返回：HTML、纯文本、结构化 JSON
```

支持 JavaScript 渲染的动态页面，返回干净的文本或 HTML，可直接用于 AI 处理流水线。

### 3.4 多源聚合搜索

除网页外，`desearch-py` 还支持：

| 数据源 | 方法 |
|---|---|
| 网页（Web） | `basic_web_search`, `web_links_search` |
| X / Twitter | `basic_twitter_search`, `twitter_links_search` |
| Reddit | 含于 `ai_search` tools 参数 |
| YouTube | 含于 `web_links_search` |
| HackerNews | 含于 `web_links_search` |
| Wikipedia | 含于 `web_links_search` |
| arXiv | 含于 `web_links_search` |

---

## 4. 技术架构

### 4.1 去中心化底层（Bittensor Subnet 22）

Desearch 的一大特点是其底层架构建立在 **Bittensor** 去中心化 AI 网络的 Subnet 22 上：

```
用户查询
    │
    ▼
Desearch API（Validator 层）
    │  按质量分发给最优 miner
    ▼
Bittensor Subnet 22（Miners 层）
    ├── Miner A：爬取并索引网页，提取结构化内容
    ├── Miner B：同上，独立爬取
    └── Miner C：同上，独立爬取
    │
    ▼
Validator 评分
    ├── 相关性（Relevance）
    ├── 新鲜度（Timeliness）
    ├── 内容完整性（Completeness）
    └── 结构化质量（Structured Quality）
    │
    ▼
返回最优结果
```

这种架构带来的优势：
- **无中心化单点故障**：分布式 miners 相互独立
- **激励驱动质量**：miners 通过 TAO（Bittensor 代币）获得奖励，差质量结果会被淘汰
- **透明可审计**：评分模型和验证逻辑开放，可被社区审查
- **防审查**：无单一实体能够控制搜索结果

### 4.2 Python SDK（desearch-py）

SDK 采用现代 Python 异步设计：

```
desearch-py
  ├── 异步接口（asyncio + aiohttp）
  ├── Pydantic 模型校验
  ├── 上下文管理器（async with Desearch(...) as d）
  └── 类型提示完整
```

### 4.3 OpenClaw Skill（desearch-web-search）

这是专为 AI agent 框架设计的"工具化封装"，遵循 OpenClaw skill 规范：

```
desearch-web-search/
  ├── SKILL.md          # 工具描述、参数定义、使用说明
  └── scripts/
        └── desearch.py  # 可执行脚本，接受命令行参数，输出 JSON
```

Agent 调用时，直接执行 `desearch.py web "<query>"` 并解析 stdout 中的 JSON 结果。这种设计让任何支持工具调用的 AI agent 都能无缝接入。

---

## 5. API 能力与结果结构

### 5.1 接口清单

| 接口 | 用途 | 主要参数 |
|---|---|---|
| `basic_web_search` | 标准网页搜索（SERP 风格） | `query`, `count`, `start` |
| `web_links_search` | 结构化链接聚合（含多源） | `query`, `tools`, `date_filter` |
| `ai_search` | AI 摘要 + 多源搜索 | `prompt`, `tools`, `result_type`, `date_filter` |
| `web_crawl` | 单 URL 网页内容提取 | `url` |
| `basic_twitter_search` | Twitter/X 帖子搜索 | `query`, `date_filter`, `lang` |
| `twitter_links_search` | Twitter/X 链接聚合 | `query`, `date_filter` |

### 5.2 结果字段示例

`basic_web_search` 的每条结果包含：

| 字段 | 类型 | 说明 |
|---|---|---|
| `title` | string | 页面标题 |
| `link` | string | 完整 URL |
| `snippet` | string | 内容摘要片段 |
| `domain` | string | 域名 |
| `quality_score` | float | 0-1 质量分（由 validator 给出） |
| `published_date` | string | 发布日期（ISO 8601） |
| `relevance` | float | 与查询的相关性分数 |

`ai_search` 额外返回：

| 字段 | 类型 | 说明 |
|---|---|---|
| `summary` | string | AI 生成的综合摘要 |
| `sources` | array | 摘要引用的来源列表 |

### 5.3 分页机制

```python
# 第 1 页（结果 1-10）
result = await d.basic_web_search(query="...", count=10, start=0)

# 第 2 页（结果 11-20）
result = await d.basic_web_search(query="...", count=10, start=10)

# 第 3 页（结果 21-30）
result = await d.basic_web_search(query="...", count=10, start=20)
```

通过 `start` 偏移量控制分页，与传统 SERP API 设计一致，便于循环抓取多页结果。

---

## 6. 集成生态

Desearch 已官方支持多种 AI agent 框架集成：

| 框架/平台 | 集成方式 |
|---|---|
| **LangChain** | 官方 Tool 封装，可作为 ReAct agent 的搜索工具 |
| **LlamaIndex** | 官方 Tool/Reader 集成 |
| **CrewAI** | 官方 Tool 集成，支持 Crew 任务中的搜索步骤 |
| **n8n** | 官方节点，支持可视化工作流搜索 |
| **OpenClaw** | Skill 封装（即本文研究的 `desearch-web-search`） |
| **MCP** | 通过 Desearch MCP server 支持 |
| **OpenAI** | 通过 Function Calling 集成 |

这种广泛的生态集成说明 Desearch 在设计上以"被 AI agent 调用"为第一优先。

---

## 7. 优缺点分析

### 优点

| 维度 | 评价 |
|---|---|
| **实时性** | 数据来自实时爬取，而非过期索引，适合追踪最新动态 |
| **结构化输出** | 返回 JSON，含质量分、日期、域名等元数据，AI agent 易消费 |
| **多源聚合** | 一次查询可聚合网页、Twitter、Reddit、YouTube 等多源 |
| **AI 摘要** | 内置高质量 AI 摘要，可直接用于 RAG 流水线 |
| **agent 友好** | 专为 AI agent 工具调用设计，集成成本低 |
| **去中心化** | Bittensor 架构带来透明度和抗审查性 |
| **分页支持** | 支持 `start` 偏移，可迭代获取大量结果 |

### 缺点

| 维度 | 评价 |
|---|---|
| **需要 API Key** | 须在 console.desearch.ai 注册并申请密钥 |
| **有费用** | 超出免费额度后需付费（定价以官网为准） |
| **依赖外部网络** | 实时搜索依赖 Desearch 网络可用性，本地不可离线使用 |
| **数据主权问题** | 查询内容会发送至外部服务，敏感行业需评估合规性 |
| **去中心化延迟** | 相较中心化搜索服务，去中心化架构可能带来额外延迟 |

---

## 8. 对本项目（web_listening）的改进建议

### 8.1 现状差距分析

| 能力维度 | web_listening 现状 | desearch-web-search 提供的参考 |
|---|---|---|
| **信息获取方式** | 被动：只能监控预配置的固定 URL | 主动：通过搜索查询实时发现相关页面 |
| **内容发现** | 靠链接提取（从已知页面爬取子链接） | 通过语义搜索发现相关内容，覆盖更广 |
| **变化后的上下文** | 只记录"某页面变了"，无背景信息 | 可自动搜索变化相关背景，理解变化意义 |
| **结果结构化程度** | 存储 HTML/Markdown，无质量分/相关性分 | 每条结果含质量分、相关性分、发布日期 |
| **多源数据** | 仅抓取网页 | 可聚合 Web、Twitter、Reddit 等多源 |
| **AI 摘要** | 基于本地 diff 生成摘要，无搜索背景 | AI 摘要内置引用来源，更可信 |

### 8.2 可借鉴的设计理念

#### 理念一：主动发现 + 被动监控并举

`web_listening` 目前只做"被动监控"（Passive Monitoring），而 Desearch 代表了"主动发现"（Active Discovery）。  
这两种能力是互补的：

```
被动监控（现有）：
  已知 URL → 定期爬取 → 发现变化

主动发现（新增）：
  关键词 / 主题 → 搜索 → 发现新相关 URL → 加入监控列表
```

#### 理念二：结构化结果优先

Desearch 的每条结果都是结构化对象（有 `quality_score`、`published_date`、`relevance` 等字段），而 `web_listening` 存储的快照是非结构化的 HTML/Markdown blob。

借鉴方向：在 `SiteSnapshot` 和 `SiteChange` 中增加更多结构化元数据字段，让 AI agent 可以基于字段而非全文做判断。

#### 理念三：agent-first 工具设计

Desearch 的 OpenClaw Skill 设计展示了"如何把一项能力包装成 AI agent 可调用的工具"：

- 清晰的工具描述（`SKILL.md`）
- 标准 CLI 接口（stdin/stdout JSON）
- 幂等、无副作用的查询操作

这与 `web_listening` 的 MCP 路线图完全对应，可直接参考其 Skill 规范格式。

#### 理念四：质量评分驱动

Desearch 为每条搜索结果提供质量分（`quality_score`）和相关性分（`relevance`）。  
`web_listening` 可在变化检测中引入类似的"变化重要性评分"，而不只是记录"有变化"。

### 8.3 具体改进方向与落地建议

#### 改进方向 A：新增"主动发现"模块

**目标**：监控某机构时，不只爬取其网站，还能主动搜索该机构的最新动态。

**实现思路**：

```python
# 新建 web_listening/blocks/searcher.py
class WebSearcher:
    """通过搜索 API 主动发现与监控目标相关的新内容"""
    
    async def search_for_site(self, site: MonitoredSite, query: str) -> list[SearchResult]:
        """给定站点上下文，搜索相关最新内容"""
        ...
    
    async def discover_new_pages(self, site: MonitoredSite) -> list[str]:
        """根据站点名称/标签，搜索发现未被监控的相关 URL"""
        ...
```

**配置接口**：

```json
{
  "site_id": 1,
  "name": "证监会",
  "search_enabled": true,
  "search_keywords": ["证监会 监管规定", "证监会 公告"],
  "search_provider": "desearch"
}
```

**API 端点**：

```
POST /api/v1/sites/{id}/search        # 触发主动搜索
GET  /api/v1/sites/{id}/search-results # 查看搜索发现的相关内容
```

#### 改进方向 B：变化后自动搜索相关背景

**目标**：当 `web_listening` 检测到某页面有重要变化时，自动搜索该变化的背景信息，丰富 AI 分析报告。

**实现思路**：

```python
# 在 blocks/analyzer.py 中扩展
async def analyze_with_context(self, change: SiteChange) -> AnalysisReport:
    # 1. 基于变化内容生成搜索关键词
    query = self._extract_search_query(change)
    
    # 2. 搜索相关背景（使用 Desearch 或其他搜索 API）
    background = await self.searcher.search(query)
    
    # 3. 将背景信息纳入 AI 摘要生成
    summary = await self._summarize_with_context(change, background)
    
    return summary
```

#### 改进方向 C：结构化变化元数据

借鉴 Desearch 的 `quality_score` + `relevance` 设计，在 `SiteChange` 中增加：

```python
# web_listening/models.py 扩展建议
class SiteChange(Base):
    # 现有字段...
    
    # 新增结构化元数据
    importance_score: float | None      # 变化重要性分（0-1）
    change_type: str | None             # "content_update" | "new_document" | "link_added"
    affected_sections: list[str] | None # 变化涉及的页面区域
    keywords: list[str] | None          # 变化涉及的关键词
    evidence_urls: list[str] | None     # 支撑该变化的证据链接
```

#### 改进方向 D：MCP 工具包装参考 OpenClaw Skill 规范

Desearch 的 OpenClaw Skill 结构是一个很好的参考模板，`web_listening` 的 MCP server 工具设计可以参考其 `SKILL.md` 格式：

**参考格式**：

```markdown
# web-listening-check-site

一个用于检查指定网站最新变化的工具。

## 参数

- `site_id`（必填）：要检查的站点 ID
- `force`（可选，默认 false）：是否强制重新抓取，忽略 TTL

## 返回

- `job_id`：后台任务 ID
- `status`：queued | running | completed | failed
- `change_summary`：变化摘要（completed 时返回）
```

这种"工具即文档"的设计让 AI agent 能准确理解每个工具的用途和边界。

#### 改进方向 E：引入搜索 API 抽象层

为了避免强依赖某一搜索服务商，建议引入搜索 provider 抽象层：

```python
# web_listening/blocks/searcher.py
from abc import ABC, abstractmethod

class SearchProvider(ABC):
    @abstractmethod
    async def search(self, query: str, count: int = 10) -> list[SearchResult]:
        ...

class DesearchProvider(SearchProvider):
    """使用 desearch-py SDK"""
    async def search(self, query: str, count: int = 10) -> list[SearchResult]:
        ...

class TavilyProvider(SearchProvider):
    """使用 Tavily API（另一选择）"""
    async def search(self, query: str, count: int = 10) -> list[SearchResult]:
        ...

class NullSearchProvider(SearchProvider):
    """降级：不启用搜索功能（默认值）"""
    async def search(self, query: str, count: int = 10) -> list[SearchResult]:
        return []
```

配置：

```dotenv
WL_SEARCH_PROVIDER=desearch          # desearch | tavily | none
WL_DESEARCH_API_KEY=your-api-key
WL_SEARCH_MAX_RESULTS=10
```

### 改进优先级建议

| 优先级 | 改进方向 | 理由 |
|---|---|---|
| ⭐⭐⭐ 高 | **方向 E**：搜索 API 抽象层 | 基础设施，其他方向依赖它 |
| ⭐⭐⭐ 高 | **方向 C**：结构化变化元数据 | 独立改进，收益大，不引入新依赖 |
| ⭐⭐ 中 | **方向 B**：变化后自动搜索背景 | 依赖方向 E，但用户价值明显 |
| ⭐⭐ 中 | **方向 D**：MCP 工具规范参考 | 与现有 MCP 路线图对齐 |
| ⭐ 低 | **方向 A**：主动发现模块 | 功能扩展，优先度较低，可后期迭代 |

---

## 9. 参考资料

| 资源 | 链接 |
|---|---|
| desearch-web-search OpenClaw Skill | https://github.com/Desearch-ai/desearch-openclaw-skills |
| desearch-py PyPI 页面 | https://pypi.org/project/desearch-py/ |
| Desearch 官方文档 | https://desearch.ai/docs/guide/ |
| Desearch Web Search API 文档 | https://desearch.ai/docs/guide/capabilities/basic-web-search |
| Desearch AI Search 文档 | https://desearch.ai/docs/guide/capabilities/ai-search |
| Desearch Web Crawl 文档 | https://desearch.ai/docs/guide/apis/desearch-api |
| Desearch 集成：OpenClaw | https://desearch.ai/docs/guide/integrations/openclaw-agents |
| Desearch SDK 文档 | https://desearch.ai/docs/guide/sdk/desearch-api-sdk |
| Bittensor Subnet 22（Desearch） | https://subnetalpha.ai/subnet/desearch/ |
| 关于去中心化搜索引擎 | https://desearch.ai/blog/decentralized-search-engine-desearch-bittensor |
| desearch-web-search on ClawHub | https://clawskills.sh/skills/okradze-desearch-web-search |
| web_listening 现有研究报告 | [RESEARCH_REPORT.md](./RESEARCH_REPORT.md) |
| web_listening AI Agent 计划 | [AI_AGENT_FUTURE_PLAN.md](./AI_AGENT_FUTURE_PLAN.md) |
