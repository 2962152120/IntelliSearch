# IntelliSearch

[![CI](https://github.com/2962152120/IntelliSearch/actions/workflows/ci.yml/badge.svg)](https://github.com/2962152120/IntelliSearch/actions/workflows/ci.yml)
[![Integration](https://github.com/2962152120/IntelliSearch/actions/workflows/integration.yml/badge.svg)](https://github.com/2962152120/IntelliSearch/actions/workflows/integration.yml)

面向大模型的自研联网检索工具。输入关键词或自然语言问题，输出**可直接作为大模型上下文引用**的结构化检索结果。

```bash
git clone https://github.com/2962152120/IntelliSearch.git
cd IntelliSearch
pip install -r requirements.txt
PYTHONPATH=src python -m intellisearch.cli "铭凡 UM880 Pro 的 NPU 算力多少" --top-k 5
```

```python
from intellisearch import SearchEngine
engine = SearchEngine()
resp = engine.search("铭凡 UM880 Pro 的 NPU 算力多少", top_k=5)
print(resp.to_context())     # 带 [1][2] 引用编号，可直接进 prompt
```

---

## 1. 检索来源与策略：聚合第三方，不是自建索引

这一点必须先讲清楚，因为它决定了这个工具的能力边界：

| 方案 | 本项目的选择 | 理由 |
|---|---|---|
| 自建网页级索引（爬虫 + 倒排索引） | ❌ 未采用 | 需要持续抓取全网、维护千亿级索引，成本与时延都不适合单机/小团队；时效性也远不如商业引擎 |
| **聚合第三方公开检索接口（元搜索）** | ✅ **采用** | 复用各引擎的实时索引，零索引成本；多源并行天然消除单一引擎偏见 |
| 自建缓存/可信度/会话索引 | ✅ 采用（辅助层） | SQLite 结果缓存、域名可信度表、会话实体记忆，属于"结果级"自建索引 |

**当前内置检索源**（均为公开接口，无需 Key）：

| 源 | 类型 | 特点 |
|---|---|---|
| `bing_rss` | 结构化 RSS | 字段最干净（title/link/description/pubDate），**默认主源** |
| `bing_html` | 网页解析 | 补充覆盖，含站点归属 |
| `sogou` | 网页解析 | 中文覆盖好，结果带日期 |
| `so360` | 网页解析 | 独立索引，与搜狗互补 |

**可选商业源**（配置了 API Key 自动启用，返回 JSON，抗改版）：`tavily` / `serpapi` / `brave`。

实测排除的源：`baidu`（安全验证页）、`mojeek`（Captcha）、`toutiao`（class 名带 hash，结构不稳定）、`quark`（SPA，标题摘要错位）。

### 如果要接自建索引

Provider 是抽象接口，接入本地倒排索引 / 向量库只需实现一个类：

```python
from intellisearch.providers.base import SearchProvider
from intellisearch.models import SearchResult

class MyIndexProvider(SearchProvider):
    name = "my_index"
    def search(self, query, options, ctx):
        hits = my_vector_db.search(query, k=options.top_k)   # 你的索引
        return [SearchResult(title=h.title, url=h.url, snippet=h.text,
                             publish_time=h.date, source=self.name)
                for h in hits]

# 注册后即可参与多源并行与融合
from intellisearch.providers import REGISTRY
REGISTRY["my_index"] = MyIndexProvider
```

---

## 2. 整体架构

```
┌──────────────────────────────────────────────────────────────┐
│ 接入层   CLI(cli.py) │ HTTP REST │ MCP stdio │ Python API      │
│          server/http_api.py   server/mcp_server.py            │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ AI 适配层   formatter(结构化/压缩/引用)                        │
│             conflict(多源信息冲突检测)  context(会话记忆)      │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ 编排层     engine.py —— 统一入口，全链路状态收敛               │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ 检索核心   query(分词/布尔/时间/站点 + Query改写)              │
│            fusion(多源并行·去重合并)  rank(排序·过滤·截断)     │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Provider   bing_rss │ bing_html │ sogou │ so360 │ tavily …    │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ 抓取层     client(超时/退避重试/并发/代理/UA轮换/资源过滤)      │
│            ua(指纹池)          robots(robots.txt 判定)         │
│            fetcher(★ 三级降级编排)                             │
│            └ browser(内核发现/池) → renderer(Chromium 渲染)     │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ 抽取层     dom(零依赖DOM)  extractor(正文/元信息)  clean(降噪)  │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ 支撑层     cache(SQLite)  ratelimit(三层限流)  blacklist(安全) │
└──────────────────────────────────────────────────────────────┘
```

### 模块职责

| 模块 | 职责 | 关键设计 |
|---|---|---|
| `engine.py` | **统一入口**。编排全链路，把任何异常收敛为 Status | 永不抛业务异常，永远返回 `SearchResponse` |
| `retrieval/query.py` | 分词、布尔检索、时间/站点过滤、**面向 AI 的 Query 改写** | 意图词扩展表；可外接 LLM 改写器 |
| `retrieval/fusion.py` | 多源并行、去重合并、补查 | 单源失败不影响其它源；结果 <2 条才用备选 query 补查 |
| `retrieval/rank.py` | 排序、过滤、数量上限 | 相关性 > 时效性 > 可信度 > 长度 |
| `providers/*` | 各检索源的请求与解析 | 统一抽象，新增源只需实现一个类 |
| `http/client.py` | 超时、指数退避重试、并发控制、代理轮换、资源过滤、反爬识别 | 全局 + 单主机双层并发上限 |
| `http/fetcher.py` | **三级降级编排**：HTTP → 渲染 → 降级返回 | 渲染只在"内容不完整"时启用，静态页零开销 |
| `http/browser.py` | Chromium 内核发现、浏览器池、`RenderRequest/Result` | 以"可用性"而非版本号挑内核；事件循环内自动转工作线程 |
| `http/renderer.py` | Playwright 渲染、反检测、资源拦截、**统一判定 `needs_render`** | **单工作线程独占 Playwright**（同步 API 线程绑定） |
| `http/robots.py` | robots.txt 解析与访问判定 | 抓不到时放行（避免网络抖动拖垮服务） |
| `extract/dom.py` | 零依赖 HTML DOM | **文本片段也是节点**，保证高亮标签不打乱标题顺序 |
| `extract/extractor.py` | 正文抽取 + 元信息 | 容器打分（文本量 × 低链接密度 × 段落数 × 语义 class） |
| `extract/clean.py` | 降噪、截断、类型识别、抽取式摘要 | 剥离摘要中的日期残留 |
| `ai/formatter.py` | 结构化输出、压缩、引用文本 | 先裁正文→再裁摘要→最后才减条数 |
| `ai/conflict.py` | 多源信息冲突检测 | 抽取"关键词+数值+单位"事实做差异比对 |
| `ai/context.py` | 会话维度上下文记忆 | 仅在 query 含指代词时才关联上文 |
| `cache.py` | SQLite 结果/正文缓存 | TTL + LRU 淘汰，损坏记录自动丢弃 |
| `safety/ratelimit.py` | 全局/会话/主机三层限流 | Token Bucket，主机限速尊重 crawl-delay |
| `safety/blacklist.py` | 黑名单、钓鱼站、付费墙 | 付费墙不丢弃，标记 + 降权 |

---

## 3. 关键数据结构

```python
class Status(str, Enum):
    OK = "ok"                  # 至少一个源成功且有结果
    PARTIAL = "partial"        # 部分源失败，但仍有可用结果
    NO_RESULTS = "no_results"  # 源都正常，但确实没匹配（正常业务状态，非故障）
    ERROR = "error"            # 全部源失败 / 参数非法 / 被限流
    BLOCKED = "blocked"        # 命中安全策略或 robots 禁止

@dataclass
class SearchResult:            # 单条结果
    title: str                 # 标题
    url: str                   # 链接
    snippet: str               # 摘要
    publish_time: str|None     # 发布时间（ISO8601）
    source: str                # 来自哪个检索源
    source_type: SourceType    # 页面类型：doc/wiki/news/forum/product/blog/code/pdf…
    domain: str
    relevance_score: float     # 相关性 0~1
    credibility: float         # 来源可信度 0~1
    clean_text: str            # 清洗后正文（fetch_content=True 时有值）
    segments: list[dict]       # 段落级引用：[{"index":1,"text":"…"}]
    extra: dict                # sources / _score / paywall / fetch_status …

@dataclass
class SearchResponse:          # 统一返回（永远返回这个对象）
    query, status, results, parsed_query, providers,
    total_found, elapsed_ms, cached, message, conflicts

    to_dict() / to_json()      # 完整结构
    to_ai_dict()               # 精简结构（6 字段）
    to_context()               # 带 [n] 编号的 prompt 文本
```

给大模型的精简结构（字段固定、类型稳定，可直接解析）：

```json
{
  "title": "铭凡UM880Pro详细参数_太平洋产品报价",
  "url": "https://product.pconline.com.cn/pc/minisforum/2490479_detail.html",
  "publish_time": "2026-09-15T00:00:00+00:00",
  "source_type": "product",
  "snippet": "提供详尽的铭凡UM880Pro参数…",
  "relevance_score": 0.455,
  "credibility": 0.53
}
```

---

## 4. 核心能力

### 4.1 Query 改写（面向 AI 优化）

```
铭凡UM880 Pro的NPU算力多少
  → 铭凡 UM880 Pro NPU 算力 TOPS 参数 规格

Python 列表推导式怎么用   → Python 列表推导式 用 文档 教程
iPhone 16 多少钱          → iPhone 16 价格 报价
最近一周 AI 芯片 发布      → AI 芯片 发布 新闻 上市   （时间交给 freshness=1w）
rust async runtime -tokio → rust async runtime       （排除词生效）
```

流程：剥离疑问句式 → 按虚词切分（避免"算力多少"切出"力多"）→ 去停用词 → 按意图表追加检索词 → 保留原词序。

### 4.2 去重 / 排序 / 时效 / 数量上限

- **去重**：URL 归一化（去 www / 末尾斜杠 / fragment / 28 种跟踪参数）+ 标题相似度（0.88）二次判定；合并时补全缺失字段、保留更长摘要与更优标题
- **排序**：`相关性(1.0) > 时效性(0.35) > 可信度(0.3) > 长度(0.08)`，时效性权重在有明确时间需求时翻倍；多源共识额外加成
- **时效**：`exp(-days/180)` 衰减，无发布时间给中性分 0.45（不惩罚，多数网页本就没有）
- **过滤**：站点白/黑名单、时间区间、广告与采集站、搜索引擎中间页（如 `ai.so.com/search/...`）
- **数量上限**：`top_k` 截断（1~50）

### 4.3 可靠性

| 机制 | 实现 |
|---|---|
| 超时 | 连接 5s / 总 15s，可配 |
| 重试 | 指数退避 + 抖动；4xx 不重试（429 除外） |
| 并发控制 | 全局 8 + 单主机 2，防 IP 被封 |
| 代理池 | HTTP/SOCKS 轮换 |
| UA 轮换 | PC / 移动 / 低识别度 bot 三套指纹 |
| 限流 | 全局 5 QPS / 会话 1 QPS / 主机 crawl-delay |
| 缓存 | SQLite，检索 30min、正文 2h，TTL + LRU |
| 降级 | 单源失败 → PARTIAL；全源失败 → ERROR + 逐源原因 |
| 无结果 | NO_RESULTS + 可操作建议（缩短查询词/放宽时间） |
| **反爬软封** | 逐条过滤无关结果；整批全被过滤则整批丢弃，该源标 `irrelevant`（见 4.4） |

### 4.4 反爬软封防护：宁可无结果，不能给错引用

搜索引擎被限流后**不一定**返回 403 或验证码页。实测遇到的更隐蔽的一种：

```
查询: AMD Zen5 架构
源:   bing_rss / bing_html  →  200, 结构完整, 10 条结果
内容: 伊朗局势时间线 / 京东十月份活动汇总 / win10禁用安全模式的方法
```

页面能正常解析、计数是 10、状态码是 200 —— 但内容与查询毫无关系。
本工具的产出是**给大模型当引用来源**的，这种"看起来正常的错误链接"
比直接失败危害大得多：它们会被原样写进答案。

因此在 `parallel_search` 里加了相关性闸门（`fusion._drop_irrelevant`），
**逐条**判定而不是整批一刀切：

- 判定只用**实词** term：单字与疑问词（什么/怎么/为什么…）不算证据 ——
  否则搜"啥"字词典页也能命中（见 4.4.1）；
- 一条结果要命中 **≥2 个**实词才保留。只有 URL 域名命中（`amd.com` 之于
  "AMD Zen5 架构"）证据太弱，不算；标题/摘要命中且属权威站点
  （官方文档/官网）时作为例外保留；
- 过滤后还剩至少一条 → 保留这个子集（避免把唯一正确结果连同垃圾一起扔掉）；
- 过滤后**一条不剩** → 说明整个结果页被替换，整批丢弃，该源
  `ok=False`、`status="irrelevant"`，`error` 写明原因；
- 检索词全为虚词（实测 "什么是" → 无实词）→ 不做过滤，避免误杀。

效果对比（同一次查询）：

| | 修复前 | 修复后 |
|---|---|---|
| 返回内容 | 10 条伊朗局势/京东活动的无关链接 | 0 条 |
| 状态 | `ok`，"检索成功：5 条结果" | `no_results` + 指明哪些源被判为不可用 |
| 逐源上报 | 全部 `ok`/10 | `bing_html: irrelevant`、`bing_rss: irrelevant`，附原因 |

#### 4.4.1 上游伪成功的两个源头

闸门本身也曾被绕过，原因在**上游**——检索词被切坏，垃圾结果"合法"命中：

| 缺陷 | 现象 | 修法 |
|---|---|---|
| `query.py` 把停用词**单字**当切分点 | `为啥`→`啥`、`人工智能`→`人工智`、`最新`→`新`；搜"啥"字词典页整批通过闸门（实测 6 条垃圾当正常结果返回） | 停用词表是整词级的，不再拆成单字注入切分表 |
| 疑问词被逐字拆 | `为什么`→`为什`+`么选择`（2 字伪词被当实词，"为什的拼音"词典页又能通过闸门） | 多字疑问词整体跳过，不逐字拆 |

分词器的硬约束（`test_real_words_are_never_destroyed` 逐个锁定）：
**不得丢失用户输入里的实词**。中文虚词大量参与构词，任何"丢弃段首字"
的启发式都会整词吃掉 —— `并发`/`和平`/`在线`/`是非`/`给力`/`请求`
都以虚词开头。因此切分点只在"已积累 ≥2 字"时生效，且段首字一律保留。

对应的集成测试 `test_irrelevant_results_never_reach_caller` 会在软封发生时
真去检查"调用方拿到的每一条结果都含查询词"；
`test_all_providers_reachable`（≥2 源可用）标了 `xfail(strict=False)`——
上游软封时它会显示为 xfail 而不是让整个套件常红，但一旦是真实回归仍然会暴露。

### 4.5 网页渲染（零 API Key，内核随包安装）

搜索引擎给的是「索引快照」，正文常常抓不到。三条实测数据说明为什么必须用浏览器内核：

| 目标 | 轻量 HTTP | Headless Chromium |
|---|---|---|
| 知乎搜索页 | **403 / 584 B** | 200 / **43,502 B** |
| 东方财富行情页 | 200 / 20,963 B，但全文可见文字仅 **1,057** 字符（全是导航） | 200 / **102,028 B** |
| `quotes.toscrape.com/js/` | 200 / 骨架页，无 `.quote` 节点 | 200 / 完整 10 条数据 |

#### 4.5.0 内核从哪来：随包安装，不依赖目标机器

Chromium 内核**不随 pip 包一起下发**（PyPI 包体积限制），但可以由一条命令装进
包目录，从而随虚拟环境 / Docker 镜像一起分发：

```bash
pip install -r requirements.txt
pip install playwright
python scripts/install_browser.py          # 装 chromium（约 150MB，仅首次）
python scripts/install_browser.py --check  # 只检查，不下载
python scripts/install_browser.py --launch # 检查并真实启动一次内核
python scripts/install_browser.py --mirror # 官方 CDN 不通时改走 npmmirror
```

脚本会**依次尝试官方 CDN → npmmirror 镜像**（国内网络下官方
`cdn.playwright.dev` 常在长传输中被掐断，报 `Download failed: server closed
connection`，此时加 `--mirror` 直接走镜像即可）。

该脚本设置 `PLAYWRIGHT_BROWSERS_PATH=0`，把内核装进
`site-packages/playwright/driver/package/.local-browsers/`，因此：

- 目标机器**无需预装** Edge / Chrome，装完即可渲染；
- 内核版本与本环境 playwright 严格一致，不会出现"期望 chromium-1243 但只有
  chromium-1210"这类不匹配；
- Docker 镜像同理自包含（见 `Dockerfile`）。

**发现顺序**（`http/browser.py: find_chromium`）：

| 优先级 | 来源 | 说明 |
|---|---|---|
| 1 | 显式指定 `IS_BROWSER_PATH` / `IS_CHROME_PATH` | 用户明确指定时最优先 |
| 2 | **包内自带 chromium** | 随包安装，跨机器行为一致 |
| 3 | `ms-playwright` 缓存 | 任意版本的完整内核，版本号降序 |
| 4 | 系统浏览器 | Edge / Chrome / Chromium，作为兜底 |
| — | 都没有 | 返回 `None`，退回纯 HTTP 并如实上报原因 |

> **未装内核时不会静默假装正常**：`reason="渲染内核不可用"`、
> `unavailable_reason` 会出现在 `/render-info` 里，JS 动态页面只能拿到骨架页。
> `scripts/install_browser.py --check` 可随时确认状态。

#### 4.5.1 三级降级链

```
        fetch(url, mode="auto")
                │
    ┌───────────▼────────────┐
    │ Tier 1  轻量 HTTP       │  httpx，~100ms，覆盖约 70% 公开页面
    │   client.get()          │  资源过滤：图片/视频/字体/CSS 默认不下载
    └───────────┬────────────┘
                │ _looks_incomplete() 判定为 JS 骨架/空正文
                ▼
    ┌───────────▼────────────┐
    │ Tier 2  Headless Chromium│  Playwright 驱动包内 chromium
    │   browser → renderer     │  执行 JS、等待选择器、拦资源
    └───────────┬────────────┘
                │ 内核不可用 / 超时 / 被拦 / 返回空
                ▼
    ┌───────────▼────────────┐
    │ Tier 3  降级返回         │  返回已拿到的最好结果
    │   degraded = True       │  附 reason 与 attempts 轨迹，绝不抛异常
    └────────────────────────┘
```

**关键点：渲染默认开启但不默认付费。** 只有 Tier 1 判定内容不完整时才启动浏览器 —— 静态文档页走 HTTP 拿满正文，1.7 秒、零浏览器开销。

三种模式：

```python
f.fetch(url, mode="auto")    # 默认：HTTP 优先，必要时自动升级
f.fetch(url, mode="http")    # 强制纯 HTTP，永不启动浏览器
f.fetch(url, mode="render")  # 强制渲染，即使 HTTP 已拿到内容也走一遍浏览器执行 JS
```

#### 4.5.2 渲染必要性判定

四类信号命中任一即升级。判定实现集中在 `renderer.needs_render`（`fetcher._looks_incomplete` 只是它的兼容包装，两者共用一套阈值）：

1. **HTTP 层被挡** —— 403 / 429 / 5xx
2. **框架挂载点存在** —— `id="root"` / `id="app"` / `__NEXT_DATA__` / `__NUXT__` / `__INITIAL_STATE__` / `ng-version` / `v-cloak`（不受页面长度限制，短骨架页同样命中）
3. **文本密度过低** —— `可见文字 < 400 且 HTML > 3×文字`，或 `可见文字 < 1200 且 HTML > 8×文字`（覆盖东方财富这类"无框架标记但正文靠异步拉取"的页面）。**可见文字按全文统计**（封顶 400KB），不取前缀 —— 分子是全文长度，分母若只用前 20KB 就成了两个口径：长文档会被算成 67:1 而只能靠阈值兜底，导航骨架页则会因前缀里截断到未闭合标签、把属性文本当成正文而正好顶翻阈值（实测该页 `n_head=1200 > n_full=1057`，卡在 `1200 < 1200` 上漏判）。统一口径后真实文章 6.2:1、骨架页 19.8:1，分界清晰
4. **验证页 / 加载占位** —— `captcha` / `安全验证` / `人机验证` / `unusual traffic`，或整页只有"加载中 / Just a moment"

阈值是刻意放宽的：宁可多渲染一次（多花 ~2 秒），也不要给 AI 返回一个只有导航菜单的空正文。

**一个重要的例外：搜索结果页不走这套启发式。** 检索源（`providers/*`）经
`ProviderContext.fetch()` 时带 `rescue_only=True`，只有**硬信号**才升级渲染：

- HTTP 抛异常（`BlockedError` / `UpstreamError`，即 403/429/5xx 被 `HttpClient` 转换后的形态）
- 状态码 401/403/405/429 或 ≥500
- 正文为空

其余情况一律用 HTTP 原文解析。原因（实测）：

| 目标 | HTTP 直取 | 浏览器重渲染后 |
|---|---|---|
| Bing SERP | `b_algo` 10 条 | `b_algo` **0** 条（DOM 结构变了） |
| 360 SERP（被拦时） | 9.8 KB"访问异常页面" | 326 KB 真实页面，但 `res-list` **0** 条 |

搜索结果页脚本多、可见文字少，上面第 3 类"文本密度过低"信号**必然命中**，
于是每个源都要启动一次浏览器（5~7 秒），而重渲染后的 DOM 与原始 HTML 不是同一棵树，
选择器全部失效——**把一个能正常解析的源活活渲染成 0 条**。
修复后同一查询的端到端耗时从 ~11s 降到 **0.3~1.1s**，`bing_html` 从 0 条恢复到 10 条。

**渲染"无效主机"记忆**：某主机渲染确认无产出（4xx/5xx 或空内容）后，
10 分钟内不再为它启动浏览器——同一个被封的源每轮检索都会被访问一次，
不记这笔账就要反复白等 5~10 秒。超时类抖动不记（那是瞬时故障）；
用户显式 `mode="render"` 也不受此限制（内部省时间的启发式不能覆盖对外契约）。

#### 4.5.3 内核发现：按"可移植性"排序

发现顺序见 4.5.0 的表。核心取舍：

**为什么包内内核优先于系统浏览器**：系统 Edge / Chrome 各版本差异很大，
换台机器可能没装、或版本与 playwright 不兼容。包内那份随包分发，行为一致，
"装完就能跑"才成立。系统浏览器降为兜底——本机没装内核时仍能跑，
只是失去跨机器一致性。

**为什么不直接用 `channel="chromium"`**：那样会走 playwright 的版本匹配，
本机 playwright 1.63 期望 `chromium-1243`，而缓存里只有 `chromium-1210`，
直接 `launch()` 报 `Executable doesn't exist` —— 完整版内核其实就在那儿，
只是版本号对不上。**统一用 `executable_path` 启动**绕开版本匹配，
无论哪来的内核都能跑（实测系统 Edge 借此直接可用）。

#### 4.5.4 线程模型：一个刻意的设计决定

Playwright 的**同步 API 绑定创建它的线程**。最初按常规写法做成"线程池 + 池化 browser"，3 个并发渲染只有 1 个成功：

```
之前：3 个并发 → render_ok: 1, failed: 2   ← Greenlet / 线程绑定错误
现在：3 个并发 → render_ok: 3, failed: 0
```

改为**单工作线程独占 Playwright**：

```
调用方线程 A ─┐
调用方线程 B ─┼─→ queue.Queue ─→ [is-chromium 唯一工作线程] ─→ Playwright
调用方线程 C ─┘                    (惰性启动, 长期复用, 串行消费)
```

- 任务的投递 + 等待在工作线程内完成，调用方只等结果
- 浏览器进程在队列首次消费时启动，之后长期复用（省掉每次 ~1s 冷启动）
- **Playwright 的 init 和 teardown 都在同一线程内**，同步 API 的清理同样要求线程一致
- 检测到调用方在 asyncio 事件循环内时，自动投递到工作线程（同步 API 在事件循环内会直接报错）
- 队列容量 = `IS_RENDER_MAX_PAGES`，超出则调用方阻塞排队，天然限流

代价是渲染在单线程内串行。实测 3 个页面串行 53.4s（其中单页最慢 30s+）。因为页面渲染以 I/O 等待为主（网络 + JS 执行均释放 GIL），对单次抓取场景够用；确需更高并发时，把 `_worker_loop` 复制 N 份、每份一套 Playwright 即可。

#### 4.5.5 反爬与资源节省

| 手段 | 实现 |
|---|---|
| 抹除自动化指纹 | 启动参数 `--disable-blink-features=AutomationControlled` + `navigator.webdriver` 置 `undefined` |
| 完整环境伪装 | PC/移动 UA 轮换、`zh-CN` locale、1366×768 viewport、`Accept-Language` |
| 资源拦截 | 默认 abort `image` / `media` / `font` —— 渲染文字不需要它们，省流量也省时间 |
| 遵守 robots | 渲染前同样过 `RobotsCache` 判定 |
| 不硬刚验证码 | 命中验证页特征时走降级返回，**不反复重试**烧流量 |
| 限流 | 渲染与 HTTP 共用全局 5 QPS / 主机 crawl-delay 令牌桶 |

#### 4.5.6 降级与容错总表

| 故障 | 行为 | 可观测字段 |
|---|---|---|
| playwright 未安装 | 自动退回纯 HTTP | `reason="渲染内核不可用"`, `attempts=[..., "render:unavailable"]` |
| 找不到任何内核 | 同上 | `unavailable_reason` |
| 渲染超时 | 抛 `RenderError` → 降级返回 HTTP 结果 | `stats["timeouts"]` 递增 |
| 目标完全不可达 | 两级都失败 → 干净降级 | `degraded=True`, `fetch_mode="none"`, `attempts=["http_err:UPSTREAM_ERROR", "render_err:RENDER_ERROR"]` |
| 页面 JS 报错 | 不影响渲染，继续返回 | `js_errors[:5]` |
| 4xx 且响应体为空 | 浏览器只得出自带错误页 → 判失败，不冒充正文 | `attempts=[..., "render_err:RENDER_ERROR"]`, `ok=False` |
| 该主机近期渲染无产出 | 跳过重复启动浏览器（10 分钟内） | `attempts=[..., "render:skip_recent_failure"]` |

`GET /stats` 与 `GET /render-info` 暴露渲染运行时状态：

```json
{
  "render_enabled": true,
  "render_available": true,
  "engine": "chromium",
  "engine_detail": "chromium[.../site-packages/playwright/driver/package/.local-browsers/chromium-1243/chrome-win64/chrome.exe]",
  "headless": true,
  "max_pages": 4,
  "stats": {"http_ok": 12, "render_ok": 3, "degraded": 1, "failed": 0}
}
```

`engine` 是归一化的浏览器家族名。装了包内内核后实测为 `chromium`
（未装时若回退到系统浏览器则为 `msedge` / `chrome`）；
`GET /render-info` 的 `engine_detail` 还会带出完整可执行文件路径，可直接确认
当前用的是包内那份还是系统那份。

---

## 5. 部署与使用

### 5.1 本地

```bash
pip install -r requirements.txt     # 基础依赖：httpx
export PYTHONPATH=src               # 或直接 pip install -e .

# 启用浏览器渲染（不装也能跑，只是 JS 动态页面会退化成骨架页）
pip install playwright
python scripts/install_browser.py   # 装 chromium 到包内（约 150MB，仅首次）
python scripts/install_browser.py --check   # 确认内核状态
```

### 5.2 Docker

```bash
docker compose up -d
curl "http://127.0.0.1:8787/health"
curl "http://127.0.0.1:8787/search?q=AMD+Zen5&top_k=5"
```

渲染内核已装进镜像：`Dockerfile` 用 `PLAYWRIGHT_BROWSERS_PATH=0` 执行
`playwright install --with-deps chromium`，内核落在 `site-packages` 内，
容器自包含 —— 换宿主或换基础镜像都不会因缺浏览器而退化成纯 HTTP。
中文字体（`fonts-noto-cjk` / `fonts-wqy-zenhei`）必须保留，否则中文页渲染出豆腐块。

### 5.3 三种调用方式

**① Python API**

```python
from intellisearch import SearchEngine
from intellisearch.models import QueryOptions, Freshness

engine = SearchEngine()
resp = engine.search(
    "铭凡 UM880 Pro 的 NPU 算力多少",
    QueryOptions(top_k=5, freshness=Freshness.MONTH, site=None,
                 fetch_content=True, max_content_chars=4000),
)
resp.to_json()        # 完整 JSON
resp.to_context()     # 带引用的上下文文本
resp.conflicts        # 多源信息差异
```

**② CLI**

```bash
isearch "铭凡 UM880 Pro 的 NPU 算力多少" --top-k 5
isearch "..." --context                    # 输出可直接进 prompt 的文本
isearch "..." --brief                      # 只要标题/链接/时间
isearch "..." --freshness 1w --site github.com
isearch "..." --fetch --max-content 4000   # 抓正文
isearch --fetch-url https://example.com    # 单页抓取
isearch --fetch-url https://example.com --render auto|http|render
isearch --serve 8787                       # 起 HTTP 服务
isearch --mcp                              # MCP stdio 服务
isearch --stats                            # 运行状态
```

**③ MCP（给 AI 客户端用）**

```json
{
  "mcpServers": {
    "intellisearch": {
      "command": "python",
      "args": ["-m", "intellisearch.cli", "--mcp"]
    }
  }
}
```

暴露两个工具：`intellisearch_search`、`intellisearch_fetch`。

**④ HTTP REST**

| 接口 | 说明 |
|---|---|
| `GET /search?q=&top_k=&freshness=&site=&format=json\|context\|markdown` | 检索 |
| `POST /search` | 同上，body 为 JSON |
| `GET /fetch?url=&mode=auto\|http\|render` | 抓取并抽取正文，`mode` 控制是否走渲染 |
| `GET /render-info` | 渲染能力状态：内核来源、引擎名、是否可用、逐项统计 |
| `GET /stats` `GET /health` | 运维 |

### 5.4 配置项

全部支持环境变量，见 `.env.example`。常用：`IS_PROVIDERS`、`IS_TIMEOUT`、`IS_RETRIES`、`IS_CACHE_TTL`、`IS_GLOBAL_QPS`、`IS_PROXIES`、`IS_RESPECT_ROBOTS`、`IS_BLACKLIST`。

渲染相关：`IS_RENDER`、`IS_RENDER_HEADLESS`、`IS_RENDER_TIMEOUT`、`IS_RENDER_SETTLE_MS`、`IS_RENDER_WAIT_SELECTOR`、`IS_RENDER_SCROLL`、`IS_RENDER_BLOCK_RES`、`IS_RENDER_MAX_PAGES`、`IS_BROWSER_PATH`。

---

## 6. 验证用例

```bash
python -m pytest tests/ -q                          # 离线
python -m pytest tests/ -q --run-integration        # 追加 26 项真实网络/真实渲染
```

**实测结果（本机，2026-10-06）：**

| 运行方式 | 结果 |
|---|---|
| `pytest tests/`（离线） | **253 passed / 27 skipped**，151s |
| `pytest tests/ --run-integration`（真实网络 + **包内 Chromium**） | **276 passed / 1 skipped / 1 xfailed / 2 xpassed**，0 failed，566s |

> 跑测试时若看到"执行完了但没有 passed 统计行"，是环境的删除保护钩子拦了
> `%TEMP%` 下的 pytest 临时目录清理；本项目已把 `--basetemp=.pytest-tmp`
> 写进 `pyproject.toml` 的 `addopts`，正常执行即可拿到摘要。

其中 xfail/xpass 的是上游可用性探针（`test_all_providers_reachable`、
`test_real_search_chinese_yields_enough`、`test_real_search_english_yields_results`）：
上游反爬或软封时它们显示 xfail（而不是让套件常红），上游恢复时显示 xpass。
**同一批里正确性断言全部 pass** —— 状态合法、逐源有状态且失败有原因、
交给调用方的每条结果都与查询相关。

| 测试文件 | 覆盖点 |
|---|---|
| `test_dom.py` | DOM 解析、高亮标签顺序、script/style 跳过、畸形与深层嵌套不崩 |
| `test_query.py` | 分词、意图改写（含需求文档的 50/TOPS 示例）、布尔、时间、站点 |
| `test_providers.py` | 四个源对真实样本页的解析、字段完整性、异常状态 |
| `test_providers_fetcher.py` | **检索源 × 真实 SmartFetcher 联合路径**：params 必须透传到 HTTP 层、SERP 不得被启发式升级渲染、硬拦截仍救援且不重复、显式 `mode="render"` 不被内部记忆跳过 |
| `test_rank_fusion.py` | URL/标题去重、字段合并、相关性/时效/可信度评分、站点与时间过滤、中间页剔除、top_k 截断 |
| `test_extractor.py` | 正文抽取（含真实文档页）、导航/页脚剔除、元信息、段落切分、截断、类型识别 |
| `test_engine.py` | **状态收敛**：OK / PARTIAL / NO_RESULTS / ERROR / 限流；缓存命中与不缓存失败结果；输出字段齐备；压缩；会话记忆；冲突检测 |
| `test_cache.py` | TTL 过期、命名空间隔离、LRU 淘汰、损坏数据不抛异常 |
| `test_ratelimit.py` | 令牌桶、全局/会话/主机限流、黑名单、钓鱼站、付费墙标记 |
| `test_regression_2026_10_06.py` | **假成功回归**：404/403/验证页不得判成功（`fetch` 与 `_fetch_contents` 两条路径）、失败不写缓存、失败分支 key 契约一致、Crawl-delay 生效且缓存命中不排队、导航容器不得压过正文、分词不得丢失实词、疑问词不产生伪词、垃圾批次逐条过滤 |
| `test_robots.py` | Allow/Disallow 优先级、通配符与 `$` 锚定、agent 匹配、crawl-delay |
| `test_render.py` | 内核探测、引擎名、降级链判定、四种抓取模式、真实 SPA 渲染、超时生效、并发渲染、干净降级 |
| `test_browser_bundle.py` | **内置内核（11 项）**：包内内核优先级高于系统浏览器、显式路径最优先、缓存高于系统、完全无内核时降级为 `None` 不抛异常、候选去重、安装脚本存在且语法有效、`render` extra 已声明、**包目录扫描不依赖运行时环境变量**、`engine_detail` 在 `pool.info()` 与 `/render-info` 两处都带出内核绝对路径 |
| `test_renderer.py` | 候选发现与启动参数形状、`needs_render` 参数化、**密度判据按全文统计（骨架页必须判渲染 / 正文充足不得误判）**、`_looks_incomplete` 密度判据、接口契约、真实渲染与自动升级、资源拦截、并发 |
| `test_relevance_guard.py` | **反爬软封闸门**：整批无关结果被丢弃、多实词需命中≥2 个、仅域名命中不算、单实词查询不误杀、查询词为空不误杀、被判不可用的源如实上报 |
| `test_integration.py` | 真实中英文检索、多源管道完整性、软封时结果不外泄（`test_irrelevant_results_never_reach_caller`）、多源可用性（≥2 源，`xfail`）、站点/时效过滤、正文抓取、连续检索稳定性、无结果返回明确状态、超时生效 |

关键验证结论：

- **降级可见**：故意让一个源失败 → `status=partial`，仍返回可用结果，并在 `providers` 里给出该源的 `status=error` 与原因
- **无结果可判定**：全源正常但零结果 → `status=no_results` + "建议缩短查询词/放宽时间范围"，与"故障"明确区分
- **失败不穿透**：模拟源抛 `RuntimeError` → 引擎收敛为 `status=error`，不抛到调用方
- **缓存有效**：第二次相同查询命中缓存，`providers` 零调用，`cached=true`
- **多源共识**：同一结果被 2 个源命中时，`source_count=2` 并获得排序加成
- **静态页不付渲染成本**：`example.com` 走 Tier 1 HTTP 拿到 92,646 字节正文、1.7 秒，浏览器零启动
- **SPA 自动升级**：`quotes.toscrape.com/js/` 在 auto 模式下降级为 `fetch_mode="render"`，`selector_found=true`，正文从骨架页变为完整 10 条数据
- **渲染不可用不致命**：目标主机完全不存在时 `degraded=True`、`fetch_mode="none"`、`attempts=["http_err:UPSTREAM_ERROR", "render_err:RENDER_ERROR"]`，不抛异常
- **并发渲染全部成功**：3 个并发渲染 `render_ok: 3, failed: 0`（线程模型修复前为 1/3）

### 持续集成（GitHub Actions）

| workflow | 触发 | 内容 |
|---|---|---|
| `ci.yml` | push / PR / 手动 | 离线全量测试矩阵（ubuntu 3.9、ubuntu 3.13、windows 3.13）+ `isearch --help` 冒烟，单轮约 1 分钟 |
| `integration.yml` | 每日 UTC 02:17（≈ 北京 10:17）/ 手动 | 装包内 Chromium 后跑 `--run-integration` 真实网络全量回归；junit 报告作为 artifact 保留 7 天 |

集成回归刻意**不挂在 push 上**：依赖外部站点、又慢、还有上游波动，不适合做每次提交的门禁。
首次 CI 实测三条腿全部 `253 passed / 27 skipped`（顺带实证了 `requires-python >= 3.9`），
并当场暴露、修复了一个真实 bug —— en-US/cp1252 代码页下 `isearch --help` 打印中文
`UnicodeEncodeError` 崩溃（回归见 `tests/test_cli.py`）。

---

## 7. 已知边界

- **内置内核是体积换来的自包含**：解压后约 433MB（下载 195.6MB），装在
  `site-packages` 内随虚拟环境/镜像分发。官方 CDN 在国内长传输易被掐断
  （`Download failed: server closed connection`），此时用
  `python scripts/install_browser.py --mirror` 走 npmmirror。**不装内核也能跑**，
  只是 JS 动态页面退回骨架页，且 `/render-info` 会如实报
  `render_available=false` 与原因
- 内置源依赖第三方页面结构，搜索引擎改版会导致某源解析失效 —— 因此默认启用 4 个源互为备份，失效时状态可见且自动降级；生产环境建议配置 Tavily/SerpAPI 等 JSON API 源
- 高频使用可能触发目标站点反爬，建议配置 `IS_PROXIES` 代理池并调低 `IS_GLOBAL_QPS`
- **渲染是单工作线程串行的**：Playwright 同步 API 的线程绑定限制所致，代价是高并发时排队。确需更高吞吐需增加工作线程数（每线程一套 Playwright 实例）
- **不绕过验证码**：命中验证页时走降级返回而非反复重试。刻意不投入对抗验证码，否则既不稳定也可能违反站点意愿
- 渲染会执行页面 JS，等于替对方站点跑一遍脚本；抓取他人站点仍需自行确认符合其服务条款与 robots 约定
- 冲突检测基于"数值 + 单位"模式匹配，只覆盖可量化事实（算力/价格/频率等），不判定观点类分歧
- **检索源不做浏览器渲染兜底**：`rescue_only` 下只有 4xx/空正文才升级。原因是实测发现
  重渲染后的 SERP 选择器全部失效（Bing `b_algo` 10→0、360 `res-list` →0），
  渲染只会把可用的源变成 0 条并多花 5~11 秒。因此当某个源被反爬限流时，
  表现为该源 `status=empty`、其他源照常返回，这是设计预期而非故障
- **今日实测的源可用性**：`bing_rss` / `bing_html` 正常；`so360`（返回"访问异常页面"）
  与 `sogou`（403）处于外部反爬限流状态，换 UA、换 query、真实浏览器均无法绕过，
  冷却 8~10 分钟无恢复 —— 判定为 IP 信誉级封禁，非代码问题
- **零 key 方案的代价**：本工具的可用性直接绑在公开搜索接口上。上游一旦限流，
  表现为 `no_results` + 逐源原因（`empty` = 被拦无结果，`irrelevant` = 结果页被替换，
  `error` = 请求失败）。生产环境建议接入 Tavily / SerpAPI 等付费 JSON API 源作为主力，
  免费源作为兜底
- **软封检测是逐条粒度的**：按"命中 ≥2 个实词 / 单实词查询命中标题摘要 /
  权威域名例外"逐条判定，无关条目单独剔除，无关结果一条都不会到达调用方；
  只有**整批**都不命中时才把该源判为 `irrelevant`。若上游在正常结果里
  **混入**少数弱命中条目，仍可能漏过（要拦就得按比例阈值，而那会误杀
  排序不佳的真实结果）——这是刻意留的边界，见 4.4
