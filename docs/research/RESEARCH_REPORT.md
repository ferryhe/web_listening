# Web Listening / 网站监控开源项目研究报告

> 报告日期：2026-03-14  
> 作者：自动生成（基于 GitHub 公开数据及社区资料）

---

## 目录

1. [研究背景](#1-研究背景)
2. [主要开源项目概览](#2-主要开源项目概览)
   - 2.1 changedetection.io
   - 2.2 urlwatch
   - 2.3 Huginn
   - 2.4 Scrapy
   - 2.5 ScrapeGraphAI
   - 2.6 AI Page Watcher Extension
3. [横向对比](#3-横向对比)
4. [关键技术点分析](#4-关键技术点分析)
5. [本项目（web_listening）方案分析](#5-本项目-web_listening-方案分析)
6. [借鉴与创新点总结](#6-借鉴与创新点总结)
7. [2026-04 补充观察与下一步开发计划](#7-2026-04-补充观察与下一步开发计划)
8. [参考资料](#8-参考资料)

---

## 1. 研究背景

随着信息爆炸的加剧，研究人员、政策分析师和从业者需要持续追踪特定网站（政府机构、监管机构、学术机构、行业协会等）的内容变动：

- 是否发布了新的政策文件、研究报告、PDF 白皮书？
- 网页正文是否有实质性内容更新？
- 新出现的链接是否指向可下载文档？

GitHub 上已有多个成熟的开源项目覆盖这一需求的不同层面，本报告对其进行系统梳理，并结合 `web_listening` 项目的需求给出对比与借鉴建议。

---

## 2. 主要开源项目概览

### 2.1 changedetection.io

| 属性 | 详情 |
|---|---|
| **仓库** | [dgtlmoon/changedetection.io](https://github.com/dgtlmoon/changedetection.io) |
| **Stars** | 30,000+ ⭐ |
| **语言** | Python |
| **协议** | Apache 2.0 |
| **部署方式** | Docker / pip 自托管 + SaaS（$8.99/月） |

#### 核心功能

- **全页面 / 局部监控**：支持 CSS 选择器、XPath，可精确监控页面中某个 `<div>` 的变化。
- **动态页面支持**：集成 Playwright（含 Chrome headless），可监控 JavaScript 渲染的内容。
- **Browser Steps**：模拟登录、填写表单、点击按钮后再检测变化，覆盖鉴权页面。
- **差异查看**：逐词、逐行、逐字符三种 diff 可视化。
- **85+ 通知渠道**：Discord、Slack、Telegram、Email、Webhook、Pushover 等。
- **文档/PDF 变动追踪**：通过自定义配置可间接追踪 PDF 链接变化。
- **价格监控**：内置产品页面价格提取（meta-data 解析）。
- **历史版本记录**：可回溯任意时间点的快照。

#### 架构亮点

```
Flask Web UI
  └── Scheduled Checker (每隔 N 分钟)
       ├── Fetcher (requests / Playwright)
       ├── Filter/Processor (CSS, XPath, Regex, JSON, Text)
       ├── Diff Engine (unified diff)
       └── Notifier (Apprise library → 85+ channels)
Data: JSON 文件存储 (datastore/)
```

#### 优点

- 开箱即用，Web UI 友好，零代码配置
- Docker 一键部署，社区活跃
- 通知渠道极其丰富（基于 Apprise 库）

#### 缺点

- 无原生 API（REST）接口，扩展性受限
- 无 AI 分析模块
- 文档下载与内容转 Markdown 需自行扩展
- 存储为 JSON 文件，不适合大规模或程序化查询

---

### 2.2 urlwatch

| 属性 | 详情 |
|---|---|
| **仓库** | [thp/urlwatch](https://github.com/thp/urlwatch) |
| **Stars** | 3,000+ ⭐ |
| **语言** | Python |
| **协议** | MIT |
| **部署方式** | pip + cron |

#### 核心功能

- **CLI 工具**：纯命令行，适合技术用户和 CI 流水线集成
- **YAML 配置**：每个监控任务（job）定义 URL + 过滤器链
- **过滤器链**：CSS、XPath、Regex、HTML-to-text、sha1sum、json 格式化等可链式组合
- **Shell Command 监控**：可监控命令输出而非 URL
- **HTTP 条件请求**：ETag / Last-Modified 优化，减少带宽消耗
- **通知**：Email、Slack、Telegram、Pushover、自定义 hook

#### 架构亮点

```
urlwatch CLI
  └── Job Runner (串行 / 并发)
       ├── URL Fetcher (requests / browser)
       ├── Filter Chain (YAML 配置)
       ├── Cache (SQLite / minidb)
       └── Reporter (email / shell / 第三方)
```

#### 优点

- 极其轻量，无 Web UI 依赖
- 过滤器链设计优雅，可组合性强
- 长期维护（2008 年起），文档完善

#### 缺点

- 无 Web UI、无 REST API
- 无 AI 分析
- 文档下载功能需自行实现
- 社区规模较小

---

### 2.3 Huginn

| 属性 | 详情 |
|---|---|
| **仓库** | [huginn/huginn](https://github.com/huginn/huginn) |
| **Stars** | 48,800+ ⭐ |
| **语言** | Ruby |
| **协议** | MIT |
| **部署方式** | Docker / Heroku / Railway |

#### 核心功能

- **Agent 系统**：每个 Agent 是一个独立的数据处理单元（爬取、过滤、转换、发通知）
- **可视化工作流编排**：在 Web UI 中拖拽连接 Agent，构建复杂 Pipeline
- **内置 Agent 类型**：WebsiteAgent、RssAgent、TriggerAgent、EventFormattingAgent、EmailAgent 等 50+ 种
- **Scenario（场景）**：可导出/导入完整工作流，便于复用和分享
- **Webhook 支持**：可作为触发器接收外部事件
- **完整 Web UI + REST API**

#### 架构亮点

```
Rails Web App
  └── Agent Graph (有向无环图)
       ├── Source Agents (WebsiteAgent, RssAgent, ...)
       ├── Transform Agents (EventFormattingAgent, ...)
       ├── Trigger Agents (条件判断)
       └── Sink Agents (EmailAgent, SlackAgent, WebhookAgent, ...)
Data: PostgreSQL / MySQL
Background Jobs: Delayed Job / Sidekiq
```

#### 优点

- 可视化 Pipeline 构建，极强的可组合性
- 内置 REST API，易于程序化集成
- 社区最大（~5 万 star），生态丰富

#### 缺点

- Ruby 技术栈，Python 生态用户学习成本高
- 重量级，部署依赖 Rails + PostgreSQL
- 无原生 AI/LLM 分析模块

---

### 2.4 Scrapy

| 属性 | 详情 |
|---|---|
| **仓库** | [scrapy/scrapy](https://github.com/scrapy/scrapy) |
| **Stars** | 60,000+ ⭐ |
| **语言** | Python |
| **协议** | BSD |
| **部署方式** | pip / Docker / Scrapy Cloud |

#### 核心功能

- **Spider 框架**：定义爬虫逻辑，自动广度优先/深度优先爬取
- **异步引擎**：基于 Twisted，高并发，不阻塞 I/O
- **FilesPipeline / MediaPipeline**：内置文件/图片下载管道，支持去重和断点续传
- **Item Pipeline**：提取的数据经 Pipeline 清洗、验证、写入 DB/文件
- **中间件（Middleware）**：可自定义请求/响应处理（代理、UA 轮换、重试）
- **Scrapy-Playwright 插件**：动态页面支持

#### 架构亮点

```
Scrapy Engine
  ├── Scheduler (请求队列)
  ├── Downloader (异步 HTTP, Playwright 可选)
  │    └── Downloader Middleware
  ├── Spider (解析逻辑 + 生成新请求)
  │    └── Spider Middleware
  └── Item Pipeline (清洗 → 存储)
       ├── FilesPipeline (PDF 下载)
       └── Custom Pipelines
```

#### PDF 下载示例

```python
class PDFSpider(scrapy.Spider):
    name = "pdf_spider"
    start_urls = ["https://example.com/publications"]

    def parse(self, response):
        for href in response.css("a::attr(href)").getall():
            if href.lower().endswith(".pdf"):
                yield {"file_urls": [response.urljoin(href)]}
```

#### 优点

- Python 生态最成熟的爬虫框架，60k+ star
- 原生支持文件下载管道
- 高度可扩展，插件丰富
- 完善文档，企业级应用广泛

#### 缺点

- 无内置变化检测（需自行维护状态）
- 无 AI 分析
- 无 Web UI / REST API（需配合 Scrapyd 或自建）

---

### 2.5 ScrapeGraphAI

| 属性 | 详情 |
|---|---|
| **仓库** | [ScrapeGraphAI/Scrapegraph-ai](https://github.com/ScrapeGraphAI/Scrapegraph-ai) |
| **Stars** | 25,000+ ⭐ |
| **语言** | Python |
| **协议** | MIT |
| **部署方式** | pip |

#### 核心功能

- **AI 驱动爬取**：用自然语言描述想要的数据，LLM 自动生成爬取图（Graph Pipeline）
- **多 LLM 支持**：OpenAI、Ollama（本地）、Hugging Face 等
- **SmartScraperGraph**：单 URL 智能提取
- **SearchGraph**：多 URL 搜索聚合
- **SpeechGraph**：将结果转为语音
- **结构化输出**：返回 JSON 格式的结构化数据

#### 优点

- 零代码描述爬取逻辑，大幅降低门槛
- 支持本地 LLM，数据不出境
- 对非结构化内容的语义理解能力强

#### 缺点

- 依赖 LLM API（成本），准确性受模型影响
- 不适合大规模定期监控（Token 消耗大）
- 无持久化存储和变化检测

---

### 2.6 AI Page Watcher Extension

| 属性 | 详情 |
|---|---|
| **仓库** | [dineshpotla/AI-page-watcher-extension](https://github.com/dineshpotla/AI-page-watcher-extension) |
| **Stars** | ~200 ⭐ |
| **语言** | JavaScript (Chrome 扩展) |
| **协议** | MIT |

#### 核心功能

- **自然语言监控条件**：用户用自然语言描述"当满足 X 条件时通知我"
- **LLM 判断**：通过 OpenRouter 调用任意 LLM 评估页面是否满足条件
- **通知**：短信、邮件、桌面弹窗

#### 优点

- 创新性地将 LLM 引入变化检测的"语义层"
- 浏览器扩展，零服务器部署

#### 缺点

- 仅为 Chrome 扩展，无法自动化/定时运行
- 无文档下载、无历史记录、无 API

---

## 3. 横向对比

| 特性 | changedetection.io | urlwatch | Huginn | Scrapy | ScrapeGraphAI | **web_listening** |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **GitHub Stars** | 30k+ | 3k+ | 48k+ | 60k+ | 25k+ | — |
| **语言** | Python | Python | Ruby | Python | Python | **Python** |
| **变化检测** | ✅ 核心 | ✅ 核心 | ✅ 可配置 | ❌ 需自建 | ⚠️ 间接 | **✅** |
| **差异存储（历史）** | ✅ | ✅ | ✅ | ❌ | ❌ | **✅ SQLite** |
| **PDF/文档下载** | ⚠️ 间接 | ❌ | ⚠️ 可配置 | ✅ Pipeline | ⚠️ 部分 | **✅** |
| **文档转 Markdown** | ❌ | ❌ | ❌ | ❌ | ⚠️ 部分 | ⚠️ 单独模块 |
| **定时调度** | ✅ | ✅ cron | ✅ | ❌ | ❌ | **✅ APScheduler** |
| **机构分类存储** | ❌ | ❌ | ❌ | ❌ | ❌ | **✅** |
| **AI 分析摘要** | ❌ | ❌ | ❌ | ❌ | ✅ (驱动层) | **✅ (weekly)** |
| **CLI 接口** | ❌ | ✅ | ❌ | ✅ | ✅ | **✅ (Typer)** |
| **REST API** | ❌ | ❌ | ✅ | ❌ | ❌ | **✅ (FastAPI)** |
| **Web UI** | ✅ | ❌ | ✅ | ❌ | ❌ | ❌ (可扩展) |
| **通知推送** | ✅ 85+ | ✅ 多渠道 | ✅ 多渠道 | ❌ | ❌ | ⚠️ 单独模块 |
| **动态页面(JS)** | ✅ Playwright | ⚠️ 有限 | ⚠️ 有限 | ✅ (插件) | ✅ | ❌ (可扩展) |
| **自托管** | ✅ | ✅ | ✅ | ✅ | ✅ | **✅** |
| **模块化/Building Block** | ❌ | ⚠️ | ⚠️ | ✅ | ✅ | **✅ 核心设计** |

---

## 4. 关键技术点分析

### 4.1 变化检测策略

各项目采用的变化检测策略对比：

| 策略 | 描述 | 代表项目 |
|---|---|---|
| **全文哈希比较** | SHA-256/MD5 对全文内容哈希，若不同则认为有变化 | urlwatch, web_listening |
| **Unified Diff** | 类 Git diff，可定位到具体行变化 | changedetection.io, urlwatch, web_listening |
| **DOM 选择器比较** | 仅比较特定 CSS/XPath 节点的内容 | changedetection.io |
| **语义相似度** | LLM 判断语义层面的变化 | AI Page Watcher, ScrapeGraphAI |
| **条件触发** | 检测是否满足某个条件（价格低于 X，股票上涨等）| changedetection.io, Huginn |

### 4.2 文档处理管道

本项目只负责文档**发现与下载**，内容转换由独立的 `doc_to_md` 模块负责：

```
web_listening（本项目）
  URL 发现 (diff.find_document_links)
    └── 下载 (DocumentProcessor.download)
         └── 存储元数据 (Storage.add_document, content_md="")

doc_to_md（单独模块，不在本项目）
  PDF    → pymupdf (fitz)     → 文本页面
  DOCX   → python-docx        → 段落文本
  HTML   → markdownify        → Markdown
  XLSX   → openpyxl           → 表格文本
    └── 写回 Document.content_md
```

### 4.3 AI 分析架构

changedetection.io 等主流项目均**无 AI 分析层**，这是 `web_listening` 的核心差异化竞争力。  
本项目生成分析内容（`AnalysisReport.summary_md`），通知推送由独立模块消费：

```
变化记录列表
  └── Analyzer.analyze_changes()
       └── LLM API (OpenAI / 本地降级摘要)
            └── AnalysisReport 存储 (Storage.add_analysis)

独立通知模块（不在本项目）
  └── Storage.list_analyses() + list_changes()
       └── Apprise / Email / Webhook 推送
```

### 4.4 Building Block 模块化设计

Scrapy 的 Pipeline 模式和 Huginn 的 Agent 模式都体现了良好的模块化：

```
web_listening 模块依赖关系：

CLI ──────────┐
              ├──► crawler.py   (爬取)
FastAPI ──────┤
              ├──► diff.py      (比较)
              ├──► storage.py   (持久化)
              ├──► document.py  (下载)
              ├──► analyzer.py  (AI 内容生成)
              └──► scheduler.py (APScheduler 定时)
```

各模块可独立导入，例如：

```python
from web_listening.blocks.crawler import Crawler
from web_listening.blocks.diff import compute_diff
from web_listening.blocks.storage import Storage
```

---

## 5. 本项目（web_listening）方案分析

### 5.1 核心定位

`web_listening` 的定位不同于上述所有项目：

> **面向研究/政策追踪场景的、以文档为中心的智能监控工具**

与 changedetection.io 相比，它：
- 更关注**文档发现与下载**（文档链接自动识别、按机构目录存储）
- 增加了**机构分类维度**（按发布机构组织下载目录）
- 内置**AI 周报生成**，产出可读的分析摘要
- 提供**REST API**，支持与外部系统集成

### 5.2 当前实现状态

```
web_listening/
├── blocks/
│   ├── crawler.py      ✅ httpx + BeautifulSoup, 快照+哈希
│   ├── diff.py         ✅ SHA-256, unified diff, 链接/文档链接过滤
│   ├── storage.py      ✅ SQLite, 5 张表, 完整 CRUD
│   ├── document.py     ✅ 文档链接发现 + 下载（转换由外部 doc_to_md 模块负责）
│   └── analyzer.py     ✅ OpenAI + 本地降级摘要
├── cli.py              ✅ 8 个命令 (Typer + Rich)
├── api/routes.py       ✅ 10 个 REST 端点 (FastAPI)
└── tests/              ✅ 32 个单元测试
```

### 5.3 与主流项目的对比优势

| 能力 | 主流项目最强者 | web_listening |
|---|---|---|
| 通知渠道丰富度 | changedetection.io (85+) | ⚠️ 独立通知模块（消费本项目数据） |
| JS 动态页面 | changedetection.io + Playwright | ⚠️ 待扩展（可加 Playwright 驱动） |
| 工作流可视化 | Huginn | ❌ 不在范围内 |
| **PDF / 文档下载** | **web_listening** | ✅ 原生支持（不含转换） |
| **机构分类存储** | **web_listening** | ✅ 原生支持 |
| **AI 周报摘要生成** | **web_listening** | ✅ 原生支持（内容生成，不推送） |
| **REST API** | **web_listening + Huginn** | ✅ FastAPI |
| **定时调度** | **web_listening** | ✅ APScheduler 3.x |
| **Building Block 可组合** | **web_listening + Scrapy** | ✅ 核心设计原则 |

---

## 6. 借鉴与创新点总结

### 6.1 已借鉴的设计

| 来源项目 | 借鉴点 |
|---|---|
| **urlwatch** | CLI 优先设计；过滤器链思想；基于哈希的变化检测 |
| **Scrapy** | Pipeline 模块化；FilesPipeline 文档下载思路 |
| **changedetection.io** | 快照历史存储；unified diff 可视化 |
| **Huginn** | REST API 接口；任务队列思想（FastAPI BackgroundTasks） |

### 6.2 创新点（主流项目均未具备）

1. **文档中心化追踪**：自动发现 PDF/DOCX/XLSX 链接，下载并转为 Markdown，按机构分类存储，记录发布机构、来源页面、发布时间、下载链接等元数据。

2. **AI 周报生成**：将一周内的变化记录汇总，通过 OpenAI（或本地 LLM）生成可读的 Markdown 分析报告，支持无 API Key 时的本地降级摘要。

3. **CLI + FastAPI 双模式**：同一套 Building Block，既支持命令行批处理，也支持 REST API 程序化调用，易于与 CI/CD、定时任务、其他微服务集成。

4. **SQLite 结构化存储**：与 changedetection.io 的 JSON 文件存储不同，使用 SQLite 支持丰富的查询（按机构、按时间、按站点过滤），方便后续分析。

### 6.3 架构决策与模块边界

经过评审，本项目的定位调整如下：

> **web_listening 只负责「跟踪 + 下载」，不做文档转换，不做通知推送。**  
> 所有新功能均以独立、可调用的 Building Block 模块方式提供，方便外部集成。

| 功能领域 | 归属 | 说明 |
|---|---|---|
| 网页爬取 + 变化检测 | ✅ **本项目** (`blocks/crawler`, `blocks/diff`) | 核心职责 |
| 文档链接发现 + 下载 | ✅ **本项目** (`blocks/document`) | 只下载，不转换 |
| 结构化存储 | ✅ **本项目** (`blocks/storage`) | SQLite，按机构/站点/时间查询 |
| AI 变化摘要（周报内容生成） | ✅ **本项目** (`blocks/analyzer`) | 生成 Markdown 内容，不发送 |
| 定时调度 | ✅ **本项目** (`blocks/scheduler`) | APScheduler 3.x，生产级调度库 |
| PDF/DOCX/HTML → Markdown 转换 | ⚠️ **单独模块** `doc_to_md` | 写入 `Document.content_md` 字段 |
| 多渠道通知推送（Email/Telegram/Slack） | ⚠️ **单独模块**（可基于 Apprise） | 消费 `AnalysisReport` + 变化记录 |
| Web UI 管理界面 | ❌ **不在当前范围** | 可后续独立集成 |

### 6.4 模块间调用关系

```
外部 doc_to_md 模块
  └── DocumentProcessor.download() → 写 Document.content_md

外部通知模块（Apprise 等）
  └── Storage.list_changes() + Storage.list_analyses() → 发送通知

本项目内部数据流：
  Scheduler
    └── check_callback()
         ├── Crawler.snapshot()
         ├── diff.compute_diff() / find_new_links() / find_document_links()
         ├── Storage.add_change() / add_snapshot()
         └── DocumentProcessor.download()
```

### 6.5 路线图（剩余增强方向）

| 优先级 | 功能 | 参考项目 | 备注 |
|---|---|---|---|
| 🟡 中 | Playwright 支持（监控 JS 渲染页面） | changedetection.io | 可扩展 Crawler |
| 🟡 中 | 文档元数据结构化提取（发布日期、机构） | ScrapeGraphAI | 结构化块存储 |
| 🟢 低 | 分布式爬取支持（多节点） | Scrapy + Scrapy-Redis | 结构化块存储 |

---

## 7. 2026-04 补充观察与下一步开发计划

> 补充日期：2026-04-03  
> 说明：以下判断结合截至 2026-04-03 的官方文档与当前项目代码现状，重点关注“更适合 AI agent 使用”的 Web Listening 形态。

### 7.1 当前更流行的 Web Listening 方式

过去一个月里，主流方案已经明显从“抓网页 + 算 hash”演进到“给 LLM / Agent 提供可直接消费的数据层”：

| 趋势 | 当前主流做法 | 对 `web_listening` 的启发 |
|---|---|---|
| **浏览器级抓取** | Playwright MCP 将浏览器操作能力通过 MCP 暴露给 AI 助手，支持结构化 accessibility snapshot、表单交互、登录态复用 | 不能只保留 `httpx` 抓取，必须补一层 browser driver |
| **LLM-ready 内容归一化** | Firecrawl 直接输出 `markdown` / `html` / `json`，强调“Turn any url into clean data”；Crawl4AI 强调 `fit_markdown` 这类更适合模型消费的正文表示 | 快照主存储不应只有 `content_text`，还应增加 Markdown / 主体正文版本 |
| **结构化变更检测** | Firecrawl `changeTracking` 在 Markdown 上比较，并支持 `git-diff` 与 `json` 两种 diff 模式 | 变化检测应从“全文 diff”升级到“字段级 / 规则级 / 结构化 diff” |
| **事件驱动异步编排** | Firecrawl 通过 webhook 推送 `started` / `page` / `completed` / `failed` 事件，不再要求调用方持续轮询 | 现有 `BackgroundTasks` 适合 demo，但不足以成为 agent 基础设施 |
| **MCP / Tool 化暴露** | Firecrawl、Playwright 都已把网页能力包装成 MCP tool；AI 助手按“工具调用”而不是“拼 REST 请求”来使用 | `web_listening` 应补 MCP server，而不止 REST API |

### 7.2 当前项目与主流形态的核心差距

对照现有实现，`web_listening` 已具备不错的基础，但与“agent-first”形态还差四层：

1. **采集层偏静态**：`blocks/crawler.py` 当前是 `httpx + BeautifulSoup`，无法处理需要 JS 执行、点击展开、登录后访问的页面。
2. **内容层偏文本**：`site_snapshots` 只存 `content_text`，没有 `markdown`、`fit_markdown`、页面元数据、渲染方式等 agent 更易消费的产物。
3. **变化层偏全文**：`blocks/diff.py` 只有 hash + unified diff，没有 selector 规则、schema 抽取、字段级 diff、语义级变更判定。
4. **接口层偏“给人写客户端”**：API 可以被程序调用，但还没有持久化 job、webhook、MCP tools、agent skill 这些“给 agent 用”的能力。

### 7.3 目标形态：面向 AI Agent 的 Web Listening 2.0

建议将后续架构收敛为五层：

```text
Acquisition Layer
  ├── HTTP Fetch
  └── Browser Fetch (Playwright)

Normalization Layer
  ├── raw_html
  ├── cleaned_html
  ├── markdown
  └── fit_markdown / main_content

Change Intelligence Layer
  ├── text diff
  ├── selector diff
  ├── schema diff
  └── semantic summary

Orchestration Layer
  ├── persistent jobs
  ├── schedules
  ├── retries
  └── webhooks

Agent Interface Layer
  ├── REST API
  ├── MCP Server
  └── repo skill
```

其中最关键的设计原则有三条：

- **先把内容变成 agent 易消费的格式，再做 AI 分析**。换句话说，先补 Markdown / 主体正文层，再继续堆摘要能力。
- **所有长任务都要变成 job**。agent 需要稳定查询状态、拿到 artifact，而不是只拿到一条“queued”字符串。
- **所有结论都要保留证据指针**。每条变化都应能回溯到 snapshot、document、URL、时间戳与 diff 片段。

### 7.4 分阶段开发计划

#### Phase 0：先把 agent 使用契约定下来（本轮最优先）

目标：让未来 AI agent 有统一调用方法，而不是每次临时拼接口。

具体工作：

- 在仓库内维护一个 `web-listening-agent` skill，固定 agent 的操作顺序、当前能力边界和扩展优先级。
- 明确 API 的标准返回形态，后续所有写操作统一返回 `job_id`、`status`、`accepted_at`。
- 明确后续 artifact 命名：`snapshot_id`、`change_id`、`document_id`、`analysis_id`。
- 为后续 MCP server 提前约束“最小可用工具集”。

本轮已经建议并创建的产物：

- 仓库内 skill：`.codex/skills/web-listening-agent`
- 本节路线图：作为项目级执行参考

#### Phase 1：补齐 LLM-ready 内容归一化层

目标：让 `web_listening` 不只知道“页面变了”，还知道“该把什么内容交给 agent”。

建议改动：

- 新增 `blocks/normalizer.py`
- 为 `SiteSnapshot` 增加以下字段：
  - `raw_html`
  - `cleaned_html`
  - `markdown`
  - `fit_markdown`
  - `metadata_json`
  - `fetch_mode`
  - `final_url`
  - `status_code`
- 将默认 diff 基础从 `content_text` 迁移到 `markdown` 或 `fit_markdown`

模块落点：

- `web_listening/models.py`
- `web_listening/blocks/storage.py`
- `web_listening/blocks/crawler.py`
- `tests/test_crawler.py`
- `tests/test_storage.py`

为什么这一阶段最重要：

- 这是后续“结构化抽取”“语义 diff”“agent 阅读快照”的共同底座。
- 也是与 Firecrawl / Crawl4AI 这一代产品差距最大的部分。

#### Phase 2：新增 Browser / Playwright 抓取能力

目标：覆盖 JS 渲染站点、折叠内容、点击后出现的文档链接，以及需要登录态的场景。

建议改动：

- 将 `Crawler` 改造成 driver 抽象：
  - `HttpCrawler`
  - `BrowserCrawler`
- 新增可选 `playwright` 依赖，不强制所有用户安装
- 为站点级配置增加：
  - `fetch_mode=http|browser|auto`
  - `wait_for`
  - `browser_steps`
  - `storage_state_path`
- 支持抓取后的截图或 DOM 调试 artifact，便于 agent 回溯失败原因

说明：

- 这一层建议先做“无登录 + JS 渲染”版本，再逐步加入登录态与复杂步骤。
- 不建议一开始就做浏览器集群或远程浏览器池，先把本地单机路径走通。

#### Phase 3：从全文 diff 升级到规则化 / 结构化 diff

目标：让系统回答的不只是“页面有变化”，而是“哪些字段有变化，变化是否重要”。

建议改动：

- 新增 `watch_rules` 概念，支持：
  - `full_page`
  - `css_selector`
  - `xpath`
  - `document_links`
  - `json_schema_extract`
  - `prompt_extract`
- 新增 `blocks/extractor.py`
- 新增结构化存储：
  - `extraction_results`
  - `change_payload_json`
  - `severity`
  - `evidence_snapshot_id`
- 扩展 `change_type`：
  - `content_changed`
  - `field_changed`
  - `document_added`
  - `document_updated`
  - `page_removed`
  - `fetch_failed`

这一步完成后，agent 才真正能把 `web_listening` 当成“外部感知器”，而不是“网页 diff 记录器”。

#### Phase 4：把异步能力做成真正可编排的任务系统

目标：让 API 成为可靠的 agent backend，而不是 FastAPI 的轻量后台任务包装。

建议改动：

- 新增 `jobs` 表，记录：
  - `job_type`
  - `status`
  - `input_json`
  - `result_json`
  - `error_message`
  - `created_at` / `started_at` / `finished_at`
- 写接口返回 job，而不是只返回 `"check queued"`
- 增加新接口：
  - `GET /api/v1/jobs/{id}`
  - `POST /api/v1/webhooks/subscriptions`
  - `GET /api/v1/sites/{id}/latest-state`
- 将调度器从“直接执行检查”改成“投递 job”

为什么这一步对 AI agent 很关键：

- agent 可以安全轮询
- 也可以用 webhook / 事件流接收结果
- 可以做失败重试、幂等控制、并行编排

#### Phase 5：在稳定 REST 之上补 MCP Server

目标：让 `web_listening` 被 Cursor、Claude、Codex 等支持 MCP 的 agent 直接当成工具使用。

建议的第一版 MCP tools：

- `add_site`
- `list_sites`
- `check_site`
- `list_changes`
- `download_documents`
- `list_documents`
- `run_analysis`
- `get_latest_snapshot`
- `get_site_state`

建议的 MCP resources：

- `site://{id}/latest`
- `changes://recent`
- `documents://site/{id}`
- `analysis://latest`

实施建议：

- 不要绕过现有 building blocks 直接重写一套逻辑
- MCP server 只做 tool/resource 暴露
- 真正的执行仍复用 `storage.py`、`crawler.py`、`document.py`、`analyzer.py`

### 7.5 建议的首个迭代 Backlog

如果只排一个最现实、最容易成功的三周版本，建议顺序如下：

1. 先扩展 `SiteSnapshot` 存储结构，补 `markdown` / `fit_markdown` / `metadata_json`
2. 再把 `Crawler` 拆成 HTTP 与 Browser 两个 driver
3. 给 API 增加 `jobs` 与 `GET /jobs/{id}`
4. 引入 `watch_rules` 与 `extraction_results`
5. 最后再补 MCP server

不建议当前阶段优先做的事情：

- Web UI
- 分布式多节点爬取
- 复杂多租户权限模型
- 过早把 SQLite 替换成更重的数据库

### 7.6 对“让 API 更适合以后 AI agent 使用”的直接结论

如果只保留一句最核心的策略，那就是：

> **把 `web_listening` 从“网页变化检测 API”升级成“可回溯、可编排、可工具化调用的内容感知层”。**

这意味着后续每个功能都要优先回答四个问题：

1. agent 能不能直接消费这个输出，而不用二次清洗？
2. 这个任务是不是有稳定的 `job_id` 和状态流转？
3. 这个结果能不能回溯到原始证据？
4. 这项能力将来能不能自然映射成 MCP tool 或 resource？

---

## 8. 参考资料

| 项目 | 链接 |
|---|---|
| changedetection.io | https://github.com/dgtlmoon/changedetection.io |
| urlwatch | https://github.com/thp/urlwatch |
| Huginn | https://github.com/huginn/huginn |
| Scrapy | https://github.com/scrapy/scrapy |
| ScrapeGraphAI | https://github.com/ScrapeGraphAI/Scrapegraph-ai |
| AI Page Watcher | https://github.com/dineshpotla/AI-page-watcher-extension |
| Apprise（通知库） | https://github.com/caronc/apprise |
| Scrapy 架构文档 | https://docs.scrapy.org/en/latest/topics/architecture.html |
| urlwatch 文档 | https://urlwatch.readthedocs.io/ |
| markdownify | https://github.com/matthewwithanm/python-markdownify |
| pymupdf | https://pymupdf.readthedocs.io/ |
| FastAPI | https://fastapi.tiangolo.com/ |
| Typer | https://typer.tiangolo.com/ |
| Firecrawl Scrape | https://docs.firecrawl.dev/features/scrape |
| Firecrawl Change Tracking | https://docs.firecrawl.dev/features/change-tracking |
| Firecrawl Webhooks | https://docs.firecrawl.dev/webhooks/overview |
| Firecrawl Developers & MCP | https://docs.firecrawl.dev/use-cases/developers-mcp |
| Playwright MCP | https://playwright.dev/docs/next/getting-started-mcp |
| Crawl4AI Markdown Generation | https://docs.crawl4ai.com/core/markdown-generation/ |
| Model Context Protocol | https://modelcontextprotocol.io/ |

---

*本报告由 `web_listening` 项目自动生成，基于 2026 年 3 月公开数据整理。*
