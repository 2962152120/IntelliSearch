# IntelliSearch

[![CI](https://github.com/2962152120/IntelliSearch/actions/workflows/ci.yml/badge.svg)](https://github.com/2962152120/IntelliSearch/actions/workflows/ci.yml)
[![Integration](https://github.com/2962152120/IntelliSearch/actions/workflows/integration.yml/badge.svg)](https://github.com/2962152120/IntelliSearch/actions/workflows/integration.yml)

> **Language / 语言**：[中文](README.md) | **English**

A self-hosted web search tool built for LLMs. Feed it keywords or a natural-language question; it returns **structured results you can cite directly as model context**.

```bash
git clone https://github.com/2962152120/IntelliSearch.git
cd IntelliSearch
pip install -r requirements.txt
PYTHONPATH=src python -m intellisearch.cli "What is the NPU compute of the Minisforum UM880 Pro" --top-k 5
```

```python
from intellisearch import SearchEngine
engine = SearchEngine()
resp = engine.search("What is the NPU compute of the Minisforum UM880 Pro", top_k=5)
print(resp.to_context())     # numbered [1][2] citations, ready to drop into a prompt
```

---

## 1. Search sources and strategy: aggregate third parties, not a self-built index

This has to be stated up front, because it defines the tool's capability boundary:

| Approach | This project | Rationale |
|---|---|---|
| Self-built page-level index (crawler + inverted index) | ❌ Not used | Requires continuously crawling the whole web and maintaining a hundred-billion-scale index — cost and latency suit neither a single machine nor a small team; freshness also lags commercial engines |
| **Aggregate third-party public search APIs (metasearch)** | ✅ **Used** | Reuses each engine's real-time index at zero indexing cost; parallel multi-source naturally removes single-engine bias |
| Self-built cache / credibility / session index | ✅ Used (supporting layer) | SQLite result cache, domain credibility table, session entity memory — a "result-level" self-built index |

**Built-in search sources** (all public endpoints, no API key needed):

| Source | Type | Notes |
|---|---|---|
| `bing_rss` | Structured RSS | Cleanest fields (title/link/description/pubDate), **default primary source** |
| `bing_html` | HTML parsing | Supplemental coverage, includes site attribution |
| `sogou` | HTML parsing | Good Chinese coverage, results carry dates |
| `so360` | HTML parsing | Independent index, complements Sogou |

**Optional commercial sources** (auto-enabled when an API key is configured; return JSON, resilient to layout changes): `tavily` / `serpapi` / `brave`.

Sources ruled out after testing: `baidu` (security-verification page), `mojeek` (CAPTCHA), `toutiao` (hashed class names, unstable structure), `quark` (SPA, title/snippet misalignment).

### Wiring in your own index

Providers are an abstract interface — hooking up a local inverted index or vector DB only needs one class:

```python
from intellisearch.providers.base import SearchProvider
from intellisearch.models import SearchResult

class MyIndexProvider(SearchProvider):
    name = "my_index"
    def search(self, query, options, ctx):
        hits = my_vector_db.search(query, k=options.top_k)   # your index
        return [SearchResult(title=h.title, url=h.url, snippet=h.text,
                             publish_time=h.date, source=self.name)
                for h in hits]

# once registered it joins parallel search and fusion
from intellisearch.providers import REGISTRY
REGISTRY["my_index"] = MyIndexProvider
```

---

## 2. Overall architecture

```
┌──────────────────────────────────────────────────────────────┐
│ Access    CLI(cli.py) │ HTTP REST │ MCP stdio │ Python API     │
│           server/http_api.py   server/mcp_server.py           │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ AI layer  formatter (structure/compress/citations)            │
│           conflict (cross-source conflicts)  context (memory)  │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Orchestr. engine.py — single entry,全链 status convergence     │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Retrieval query (tokenize/boolean/time/site + rewriting)       │
│           fusion (parallel·dedupe)  rank (rank·filter·trim)    │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Provider  bing_rss │ bing_html │ sogou │ so360 │ tavily …      │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Fetch     client (timeouts/backoff/concurrency/proxy/UA/filter)│
│           ua (fingerprints)          robots (robots.txt)       │
│           fetcher (★ 3-tier fallback orchestration)           │
│           └ browser (discovery/pool) → renderer (Chromium)     │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Extract   dom (zero-dep DOM)  extractor (body/meta)  clean     │
└───────────────────────────┬──────────────────────────────────┘
┌───────────────────────────▼──────────────────────────────────┐
│ Support   cache (SQLite)  ratelimit (3 layers)  blacklist      │
└──────────────────────────────────────────────────────────────┘
```

### Module responsibilities

| Module | Responsibility | Key design |
|---|---|---|
| `engine.py` | **Single entry point**. Orchestrates the pipeline, converges any exception into a Status | Never throws business exceptions; always returns `SearchResponse` |
| `retrieval/query.py` | Tokenization, boolean search, time/site filters, **AI-oriented query rewriting** | Intent-term expansion table; pluggable LLM rewriter |
| `retrieval/fusion.py` | Parallel sources, dedupe/merge, backfill | One source failing doesn't affect others; backfill only when <2 results |
| `retrieval/rank.py` | Ranking, filtering, result cap | Relevance > freshness > credibility > length |
| `providers/*` | Request and parsing per source | Unified abstraction; adding a source needs one class |
| `http/client.py` | Timeouts, exponential backoff, concurrency, proxy rotation, resource filtering, anti-bot detection | Global + per-host dual concurrency caps |
| `http/fetcher.py` | **3-tier fallback**: HTTP → render → degraded return | Rendering only when content looks incomplete; zero cost for static pages |
| `http/browser.py` | Chromium discovery, browser pool, `RenderRequest/Result` | Picks a kernel by "availability" rather than version; auto-offloads out of an event loop |
| `http/renderer.py` | Playwright rendering, anti-detection, resource blocking, **unified `needs_render`** | **Single worker thread owns Playwright** (sync API is thread-bound) |
| `http/robots.py` | robots.txt parsing and access decisions | Allows on fetch failure (avoid network jitter taking down the service) |
| `extract/dom.py` | Zero-dependency HTML DOM | **Text fragments are nodes too**, so highlight tags don't scramble title order |
| `extract/extractor.py` | Body extraction + metadata | Container scoring (text volume × low link density × paragraph count × semantic class) |
| `extract/clean.py` | Denoising, truncation, type detection, extractive summary | Strips date residue from snippets |
| `ai/formatter.py` | Structured output, compression, citation text | Trim body → trim snippet → reduce count (last resort) |
| `ai/conflict.py` | Cross-source conflict detection | Extracts "keyword + number + unit" facts for comparison |
| `ai/context.py` | Session-scoped context memory | Only associates prior turns when the query has pronouns |
| `cache.py` | SQLite result/body cache | TTL + LRU eviction, corrupt rows dropped automatically |
| `safety/ratelimit.py` | Global/session/host three-layer rate limiting | Token bucket, host limiting honors crawl-delay |
| `safety/blacklist.py` | Blacklist, phishing sites, paywalls | Paywalls kept but marked + down-ranked |

---

## 3. Key data structures

```python
class Status(str, Enum):
    OK = "ok"                  # at least one source succeeded with results
    PARTIAL = "partial"        # some sources failed, usable results remain
    NO_RESULTS = "no_results"  # sources healthy but genuinely no match (normal state)
    ERROR = "error"            # all sources failed / invalid params / rate limited
    BLOCKED = "blocked"        # hit a safety policy or robots disallow

@dataclass
class SearchResult:            # a single result
    title: str                 # title
    url: str                   # link
    snippet: str               # snippet
    publish_time: str|None     # publish time (ISO8601)
    source: str                # originating search source
    source_type: SourceType    # page type: doc/wiki/news/forum/product/blog/code/pdf…
    domain: str
    relevance_score: float     # relevance 0~1
    credibility: float         # source credibility 0~1
    clean_text: str            # cleaned body text (when fetch_content=True)
    segments: list[dict]       # paragraph-level citations: [{"index":1,"text":"…"}]
    extra: dict                # sources / _score / paywall / fetch_status …

@dataclass
class SearchResponse:          # unified return (always this object)
    query, status, results, parsed_query, providers,
    total_found, elapsed_ms, cached, message, conflicts

    to_dict() / to_json()      # full structure
    to_ai_dict()               # compact structure (7 fields per result)
    to_context()               # prompt text with [n] citation numbers
```

Compact structure for the LLM (fixed fields, stable types, directly parseable):

```json
{
  "title": "Minisforum UM880Pro specs_PConline product pricing",
  "url": "https://product.pconline.com.cn/pc/minisforum/2490479_detail.html",
  "publish_time": "2026-09-15T00:00:00+00:00",
  "source_type": "product",
  "snippet": "Detailed Minisforum UM880Pro specifications…",
  "relevance_score": 0.455,
  "credibility": 0.53
}
```

---

## 4. Core capabilities

### 4.1 Query rewriting (AI-oriented)

```
Minisforum UM880 Pro NPU compute
  → Minisforum UM880 Pro NPU compute TOPS specs

How to use Python list comprehensions → Python list comprehension usage docs tutorial
How much is the iPhone 16            → iPhone 16 price quote
AI chip releases in the last week     → AI chip release news launch  (time goes to freshness=1w)
rust async runtime -tokio            → rust async runtime            (exclusion term applied)
```

Flow: strip question phrasing → split on function words (so "compute how much" doesn't yield fragments) → remove stop words → append intent terms → preserve original term order.

### 4.2 Dedupe / ranking / freshness / result cap

- **Dedupe**: URL normalization (strip www / trailing slash / fragment / 27 tracking params) + title similarity (0.88) as a second check; merging fills missing fields and keeps the longer snippet and better title
- **Ranking**: `relevance(1.0) > freshness(0.35) > credibility(0.3) > length(0.08)`; freshness weight doubles with an explicit time need; cross-source consensus adds a bonus
- **Paywall down-ranking**: items with "sign in to read / subscriber only / subscribe to read" signals are kept (title and snippet remain usable) but flagged `paywall=True` and multiplied by `0.85` in the composite score — so paywalls don't crowd out citations
- **Freshness**: `exp(-days/180)` decay; no publish time gets a neutral 0.45 (no penalty — most pages have none)
- **Filtering**: site allow/deny lists, time ranges, ad and scraper sites, search-engine intermediary pages (e.g. `ai.so.com/search/...`)
- **Result cap**: `top_k` truncation (1~50)

### 4.3 Reliability

| Mechanism | Implementation |
|---|---|
| Timeouts | connect 5s / total 15s, configurable |
| Retries | exponential backoff + jitter; no retry on 4xx (except 429) |
| Concurrency | global 8 + per-host 2, to avoid IP bans |
| Proxy pool | HTTP/SOCKS rotation |
| UA rotation | three fingerprint sets: desktop / mobile / low-detectability bot |
| Rate limiting | global 5 QPS / session 1 QPS / host crawl-delay |
| Cache | SQLite, search 30min, body 2h, TTL + LRU |
| Fallback | one source fails → PARTIAL; all fail → ERROR + per-source reasons |
| No results | NO_RESULTS + actionable advice (shorten query / widen time) |
| **robots compliance** | robots checks before both fetch and render; rule URLs are built from a bare netloc (so a double scheme can't be mistakenly blocked by SSRF validation and silently disable the rules); allow when robots.txt can't be fetched |
| **Anti-bot soft-block** | filter irrelevant results one by one; if the whole batch is filtered, drop it and mark the source `irrelevant` (see 4.4) |

### 4.4 Anti-bot soft-block defense: better no results than wrong citations

A rate-limited search engine doesn't **necessarily** return 403 or a CAPTCHA. A subtler case seen in the wild:

```
Query: AMD Zen5 architecture
Source: bing_rss / bing_html → 200, well-formed, 10 results
Content: Iran conflict timeline / JD October sale roundup / how to disable Win10 safe mode
```

The page parses fine, the count is 10, the status is 200 — but the content is unrelated. This tool's output is **used as citations for an LLM**, so these "plausible but wrong links" are far more harmful than an outright failure: they get written into the answer verbatim.

So `parallel_search` has a relevance gate (`fusion._drop_irrelevant`) that decides **per result** rather than blanket-dropping:

- Only **content words** count as evidence: single characters and question words (what/how/why…) don't — otherwise searching "啥" would pass a dictionary page (see 4.4.1);
- A result needs **≥2** content-word hits to survive. A URL-domain-only hit (`amd.com` for "AMD Zen5 architecture") is too weak and doesn't count; **URL hits never count as evidence at all** (not just the domain — one hit in the title plus another in the URL can't be added up to reach "≥2"). One hit in title/snippet from an authoritative site (official docs / vendor site) is kept as an exception;
- If at least one result survives → keep that subset (so a single correct result isn't thrown out with the junk);
- If **none** survive → the whole results page was likely swapped; drop the batch, mark the source `ok=False`, `status="irrelevant"`, with an explanatory `error`;
- Query made entirely of function words (e.g. "what is" → no content words) → skip filtering, to avoid false positives.

Before/after on the same query:

| | Before | After |
|---|---|---|
| Returned | 10 unrelated Iran/JD sale links | 0 |
| Status | `ok`, "search succeeded: 5 results" | `no_results` + which sources were deemed unusable |
| Per-source | all `ok`/10 | `bing_html: irrelevant`, `bing_rss: irrelevant`, with reasons |

#### 4.4.1 Two sources of upstream false success

The gate itself was once bypassed, due to **upstream** causes — the query was tokenized badly, so junk results matched "legitimately":

| Defect | Symptom | Fix |
|---|---|---|
| `query.py` used **single-character** stop words as split points | `为啥`→`啥`, `人工智能`→`人工智`, `最新`→`新`; searching "啥" passed a dictionary page wholesale (6 junk results returned as normal, observed) | Stop-word list is whole-word only; no longer broken into single characters injected into the splitter |
| Question words split character by character | `为什么`→`为什`+`么选择` (2-char pseudo-words treated as content; "为什的拼音" dictionary page passed again) | Multi-character question words skipped as a whole, not split |

Hard constraint on the tokenizer (locked down by `test_real_words_are_never_destroyed`):
**never destroy content words in user input**. Many Chinese function words participate in compound words, and any heuristic that "drops the leading character" eats whole words — `并发`/`和平`/`在线`/`是非`/`给力`/`请求` all begin with a function word. Split points therefore only apply after ≥2 characters are accumulated, and the leading character is always kept.

The integration test `test_irrelevant_results_never_reach_caller` actually checks, when a soft-block occurs, that "every result handed to the caller contains a query term"; `test_all_providers_reachable` (≥2 sources available) is marked `xfail(strict=False)` — under upstream soft-blocks it shows as xfail rather than keeping the suite permanently red, while a real regression still surfaces.

### 4.5 Web rendering (zero API key, bundled kernel)

Search engines give an "index snapshot"; body text is often unavailable. Three measured examples of why a browser kernel is needed:

| Target | Lightweight HTTP | Headless Chromium |
|---|---|---|
| Zhihu search page | **403 / 584 B** | 200 / **43,502 B** |
| Eastmoney quote page | 200 / 20,963 B, but only **1,057** visible characters (all navigation) | 200 / **102,028 B** |
| `quotes.toscrape.com/js/` | 200 / skeleton, no `.quote` nodes | 200 / full 10 records |

#### 4.5.0 Where the kernel comes from: bundled, no dependency on the target machine

The Chromium kernel **is not shipped inside the pip package** (PyPI size limits), but one command installs it into the package directory so it travels with the virtualenv / Docker image:

```bash
pip install -r requirements.txt
pip install playwright
python scripts/install_browser.py          # install chromium (~150MB, first time only)
python scripts/install_browser.py --check  # check only, no download
python scripts/install_browser.py --launch # check and actually launch once
python scripts/install_browser.py --force  # reinstall even if present (kernel upgrade)
python scripts/install_browser.py --mirror # use npmmirror when the official CDN is unreachable
```

The script **tries the official CDN first, then the npmmirror fallback** (on Chinese networks `cdn.playwright.dev` is often cut off mid-transfer with `Download failed: server closed connection`; add `--mirror` to go straight to the fallback).

The script sets `PLAYWRIGHT_BROWSERS_PATH=0`, installing into
`site-packages/playwright/driver/package/.local-browsers/`, so:

- The target machine needs **no preinstalled** Edge / Chrome — rendering works right after install;
- The kernel version matches this environment's playwright exactly, avoiding "expected chromium-1243 but only chromium-1210" mismatches;
- Docker images are self-contained the same way (see `Dockerfile`).

> **headless kernel rename in playwright 1.5x**: newer versions renamed the slim kernel executable from
> `headless_shell.exe` to `chrome-headless-shell.exe` and its directory from `chrome-win64` to
> `chrome-headless-shell-win64`. Both the install script and runtime discovery (`browser.py`'s
> `_PKG_BROWSER_GLOBS`) **accept both old and new layouts** — otherwise a minimal deployment with only
> the headless kernel would be misdetected as "no bundled kernel" and fall back to a system browser.
> `--check` lists both the full and headless kernels (measured with playwright 1.65.2 / chromium-1255: ~219.7MB total).

**Discovery order** (`http/browser.py: find_chromium`):

| Priority | Source | Notes |
|---|---|---|
| 1 | explicit `IS_BROWSER_PATH` / `IS_CHROME_PATH` | highest priority when explicitly set |
| 2 | **bundled chromium in the package** | installed with the package, consistent across machines |
| 3 | `ms-playwright` cache | any version of the full kernel, descending version order |
| 4 | system browser | Edge / Chrome / Chromium, as a last resort |
| — | none | returns `None`, falls back to plain HTTP and reports the reason honestly |

> **No silent "looks fine" when no kernel is installed**: `reason="渲染内核不可用"` /
> `unavailable_reason` shows up in `/render-info`, and JS-heavy pages only yield a skeleton.
> `scripts/install_browser.py --check` confirms status at any time.

#### 4.5.1 Three-tier fallback chain

```
        fetch(url, mode="auto")
                │
    ┌───────────▼────────────┐
    │ Tier 1  lightweight    │  httpx, ~100ms, ~70% of public pages
    │   client.get()         │  resource filter: images/video/font/CSS not downloaded by default
    └───────────┬────────────┘
                │ _looks_incomplete() detects JS skeleton / empty body
                ▼
    ┌───────────▼────────────┐
    │ Tier 2 Headless Chromium│  Playwright drives the bundled chromium
    │   browser → renderer    │  executes JS, waits for selector, blocks resources
    └───────────┬────────────┘
                │ kernel unavailable / timeout / blocked / empty
                ▼
    ┌───────────▼────────────┐
    │ Tier 3 degraded return │  returns the best content obtained so far
    │   degraded = True      │  with reason and attempts trace, never throws
    └────────────────────────┘
```

**Key point: rendering is on by default, but not paid for by default.** The browser only starts when Tier 1 judges the content incomplete — a static doc page gets its full body over HTTP in 1.7s with zero browser cost.

Three modes:

```python
f.fetch(url, mode="auto")    # default: HTTP first, auto-upgrade when needed
f.fetch(url, mode="http")    # force plain HTTP, never start a browser
f.fetch(url, mode="render")  # force rendering, go through the browser even if HTTP got content
```

The `mode="render"` contract is **either it renders, or it clearly tells you it didn't**: if rendering is disabled by `IS_RENDER=0` or no kernel is found, it does not silently fall back to HTTP and claim success — it returns `degraded=True`, `fetch_mode="http"` (when content was obtained) or `"none"`, and writes "rendering disabled (IS_RENDER=0), cannot satisfy mode=render; degraded to lightweight fetch" into `reason`. Also, when rendering succeeds but the page itself is 4xx/5xx (e.g. a Cloudflare block page), that HTML **does not count as a successful render**: it is not accepted, `degraded=True`, `reason="render returned HTTP 503 block page, not accepted"`, so a block page is never handed downstream as body text.

#### 4.5.2 Render-necessity decision

Any of four signal groups triggers an upgrade. The decision lives in `renderer.needs_render` (`fetcher._looks_incomplete` is a compatibility wrapper sharing the same thresholds):

1. **HTTP layer blocked** — 403 / 429 / 5xx
2. **Framework mount point present** — `id="root"` / `id="app"` / `__NEXT_DATA__` / `__NUXT__` / `__INITIAL_STATE__` / `ng-version` / `v-cloak` (no page-length limit; short skeletons match too)
3. **Text density too low** — `visible text < 400 and HTML > 3×text`, or `visible text < 1200 and HTML > 8×text` (covers pages like Eastmoney with no framework markers but async-loaded body). **Visible text is counted over the whole document** (capped at 400KB), not a prefix — the numerator is full length; if the denominator used only the first 20KB the two would use different scopes: long docs would compute 67:1 and rely on the fallback threshold, while navigation skeletons could truncate mid-tag in the prefix, count attribute text as body, and land just over the threshold (measured on that page: `n_head=1200 > n_full=1057`, stuck at `1200 < 1200`). With one consistent scope, real articles are 6.2:1 and skeletons 19.8:1 — a clean separation
4. **Verification / loading placeholder** — `captcha` / `安全验证` / `人机验证` / `unusual traffic`, or a page containing only "loading / Just a moment"

Thresholds are deliberately loose: better to render one extra time (~2s) than hand the AI an empty body of navigation menus.

**Important exception: search result pages don't use this heuristic.** Search sources (`providers/*`) pass `rescue_only=True` via `ProviderContext.fetch()`; only **hard signals** trigger rendering:

- HTTP throws (`BlockedError` / `UpstreamError`, i.e. 403/429/5xx converted by `HttpClient`)
- status 401/403/405 or ≥500
- empty body

Everything else uses the HTTP response as-is. Reason (measured):

| Target | Direct HTTP | After browser re-render |
|---|---|---|
| Bing SERP | `b_algo` 10 results | `b_algo` **0** (DOM structure changed) |
| 360 SERP (when blocked) | 9.8 KB "access anomaly page" | 326 KB real page, but `res-list` **0** |

SERP pages are script-heavy with little visible text, so signal group 3 above **always matches** — each source would start a browser (5~7s), and the re-rendered DOM isn't the same tree as the original HTML, so every selector breaks — **turning a parseable source into 0 results**. After the fix, end-to-end latency for the same query dropped from ~11s to **0.3~1.1s**, and `bing_html` recovered from 0 to 10 results.

**"Dead host" memory for rendering**: once a host's render is confirmed fruitless (4xx/5xx or empty), no browser starts for it for 10 minutes — a blocked source is visited every round, and without this memo you'd wait 5~10s for nothing each time. Timeout jitter is not recorded (transient); an explicit `mode="render"` is also exempt (an internal time-saving heuristic can't override the external contract).

#### 4.5.3 Kernel discovery: ordered by portability

See the table in 4.5.0 for discovery order. The core trade-offs:

**Why bundled beats system browser**: system Edge / Chrome vary wildly by version; a different machine may not have them or may have a version incompatible with playwright. The bundled copy is distributed with the package and behaves consistently — "install and it runs" only holds this way. System browsers are the fallback: the tool still runs without a bundled kernel, just without cross-machine consistency.

**Why not just use `channel="chromium"`**: that goes through playwright's version matching. A local playwright 1.63 expects `chromium-1243` while the cache only has `chromium-1210`, so `launch()` reports `Executable doesn't exist` — the full kernel is right there, the version just doesn't match. **Launching everything via `executable_path`** bypasses version matching, so any kernel runs (measured: a system Edge works this way too).

Package scanning is independent of runtime environment variables: `_package_browsers()` scans `.local-browsers` under `site-packages` directly, regardless of `PLAYWRIGHT_BROWSERS_PATH` — playwright's own `executable_path` only points into the package directory if that variable is also set at runtime; relying on it alone means the kernel is lost on another machine and "bundled distribution" becomes empty words. Both full and headless kernels in the directory are registered as `executable_path`s, with globs covering both old and new naming.

#### 4.5.4 Threading model: a deliberate design decision

Playwright's **sync API is bound to the thread that created it**. The conventional approach — thread pool plus pooled browsers — gave only 1 success out of 3 concurrent renders:

```
Before: 3 concurrent → render_ok: 1, failed: 2   ← Greenlet / thread binding error
After:  3 concurrent → render_ok: 3, failed: 0
```

The fix: **a single worker thread owns Playwright**:

```
Caller thread A ─┐
Caller thread B ─┼─→ queue.Queue ─→ [sole is-chromium worker] ─→ Playwright
Caller thread C ─┘                   (lazy start, long-lived, serial consumption)
```

- Task submission and waiting happen in the worker thread; callers only wait for the result
- The browser process starts on first queue consumption, then is reused (saves ~1s cold start each time)
- **Playwright init and teardown happen in the same thread** — sync API cleanup requires the same too
- If the caller is inside an asyncio event loop, submission is auto-offloaded to the worker thread (the sync API errors directly inside a loop)
- Queue capacity = `IS_RENDER_MAX_PAGES`; over-capacity callers block and queue, giving natural throttling

The cost is serial rendering in one thread. Measured: 3 pages serially in 53.4s (slowest single page 30s+). Since rendering is mostly I/O-bound waiting (network and JS both release the GIL), this is enough for single-fetch scenarios; for more concurrency, copy `_worker_loop` N times with one Playwright instance each.

#### 4.5.5 Anti-bot and resource savings

| Measure | Implementation |
|---|---|
| Strip automation fingerprint | launch args `--disable-blink-features=AutomationControlled` + `navigator.webdriver` set to `undefined` |
| Full environment disguise | desktop/mobile UA rotation, 1366×768 viewport, `Accept-Language`; locale mapped from `lang` (zh→`zh-CN`, en→`en-US`, explicit `xx-YY` used verbatim) |
| Resource blocking | abort `image` / `media` / `font` by default — text rendering doesn't need them, saving bandwidth and time; `IS_RENDER_BLOCK_RES=0` or per-request `RenderRequest(block_resources=False)` disables blocking (measured: with it off, image requests really reach the site) |
| robots compliance | render also goes through `RobotsCache` |
| Don't fight CAPTCHAs | on verification-page signals, degrade rather than retry repeatedly and burn traffic |
| Rate limiting | rendering shares the global 5 QPS / host crawl-delay token bucket with HTTP |

#### 4.5.6 Degradation and tolerance table

| Failure | Behavior | Observable fields |
|---|---|---|
| playwright not installed | auto-fall back to plain HTTP | `reason="渲染内核不可用"`, `attempts=[..., "render:unavailable"]` |
| no kernel found | same as above | `unavailable_reason` |
| render timeout | throws `RenderError` → degraded return of HTTP result | `stats["timeouts"]` increments |
| target totally unreachable | both tiers fail → clean degradation | `degraded=True`, `fetch_mode="none"`, `attempts=["http_err:UPSTREAM_ERROR", "render_err:RENDER_ERROR"]` |
| page JS error | doesn't affect rendering, still returned | `js_errors[:5]` |
| 4xx with empty body | browser only produces its own error page → judged failure, not passed off as body | `attempts=[..., "render_err:RENDER_ERROR"]`, `ok=False` |
| host recently produced nothing | skip re-starting browser (10 minutes) | `attempts=[..., "render:skip_recent_failure"]` |
| explicit `mode="render"` but rendering disabled / kernel missing | no silent fake success: return best content with explicit label | `degraded=True`, `reason="rendering disabled (IS_RENDER=0), cannot satisfy mode=render…"`, `attempts=[..., "render:unavailable"]` |
| render succeeds but page itself is 4xx/5xx | block/error page not accepted, degrade as render failure | `degraded=True`, `reason="render returned HTTP 503 block page, not accepted"`, `ok=False` |

`GET /stats` and `GET /render-info` expose rendering runtime state:

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

`engine` is a normalized browser family name. With the bundled kernel installed it reads `chromium` in practice (without it, if falling back to a system browser, `msedge` / `chrome`); `/render-info`'s `engine_detail` carries the full executable path so you can confirm whether the bundled or system copy is in use.

---

## 5. Deployment and usage

### 5.1 Local

```bash
pip install -r requirements.txt     # base dependency: httpx
export PYTHONPATH=src               # or pip install -e .

# enable browser rendering (works without it; JS-heavy pages just degrade to skeletons)
pip install playwright
python scripts/install_browser.py   # install chromium into the package (~150MB, first time only)
python scripts/install_browser.py --check   # confirm kernel status
```

### 5.2 Docker

```bash
docker compose up -d
curl "http://127.0.0.1:8787/health"
curl "http://127.0.0.1:8787/search?q=AMD+Zen5&top_k=5"
```

The in-container service listens on `0.0.0.0` (the `Dockerfile` CMD explicitly passes `--host 0.0.0.0`) so docker-compose's `8787:8787` mapping is reachable from the host; with the default `127.0.0.1` the service would only listen on the container's loopback and the two curls above would fail. `HEALTHCHECK` still self-checks via `127.0.0.1` inside the container, unaffected by the listen address.

The rendering kernel is baked into the image: the `Dockerfile` runs `playwright install --with-deps chromium` with `PLAYWRIGHT_BROWSERS_PATH=0`, so the kernel lands under `site-packages` and the container is self-contained — changing hosts or base images won't degrade it to plain HTTP. Chinese fonts (`fonts-noto-cjk` / `fonts-wqy-zenhei`) must be kept, or Chinese pages render as tofu boxes.

### 5.3 Four ways to call it

**① Python API**

```python
from intellisearch import SearchEngine
from intellisearch.models import QueryOptions, Freshness

engine = SearchEngine()
resp = engine.search(
    "What is the NPU compute of the Minisforum UM880 Pro",
    QueryOptions(top_k=5, freshness=Freshness.MONTH, site=None,
                 fetch_content=True, max_content_chars=4000),
)
resp.to_json()        # full JSON
resp.to_context()     # citation-annotated context text
resp.conflicts        # cross-source discrepancies
```

**② CLI**

```bash
isearch "What is the NPU compute of the Minisforum UM880 Pro" --top-k 5
isearch "..." --context                    # text ready to drop into a prompt
isearch "..." --brief                      # title / link / time only

isearch "..." --freshness 1w --site github.com
isearch "..." --fetch --max-content 4000   # fetch body text
isearch --fetch-url https://example.com    # fetch a single page
isearch --fetch-url https://example.com --render auto|http|render
isearch --serve 8787                       # start HTTP service (loopback by default)
isearch --serve 8787 --host 0.0.0.0        # choose listen address (0.0.0.0 required in containers)
isearch --mcp                              # MCP stdio service
isearch --stats                            # runtime status
```

**③ MCP (for AI clients)**

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

Exposes two tools: `intellisearch_search` and `intellisearch_fetch`.

**④ HTTP REST**

| Endpoint | Description |
|---|---|
| `GET /search?q=&top_k=&freshness=&site=&providers=&format=json\|context\|markdown` | search; `providers` is comma-separated to run only chosen sources (e.g. `providers=bing_rss,sogou`) |
| `POST /search` | same, JSON body |
| `GET /fetch?url=&mode=auto\|http\|render` | fetch and extract body text; `mode` controls rendering |
| `GET /render-info` | rendering capability status: kernel source, engine name, availability, per-item stats |
| `GET /stats` `GET /health` | operations |

HTTP status codes map from search status: `ok` / `partial` / `no_results` → **200**, `blocked` → **403**, `error` → **502**. `no_results` is the normal business state "sources healthy but no match", not a 502 (check `status` / `ok` in the response body). When all `providers=` sources are unavailable, the result converges to `status=error` with explanations in `providers[]`.

### 5.4 Configuration

All options support environment variables; see `.env.example`. Common ones: `IS_PROVIDERS`, `IS_TIMEOUT`, `IS_RETRIES`, `IS_CACHE_TTL`, `IS_GLOBAL_QPS`, `IS_PROXIES`, `IS_RESPECT_ROBOTS`, `IS_BLACKLIST`.

Rendering: `IS_RENDER`, `IS_RENDER_HEADLESS`, `IS_RENDER_TIMEOUT`, `IS_RENDER_SETTLE_MS`, `IS_RENDER_WAIT_SELECTOR`, `IS_RENDER_SCROLL`, `IS_RENDER_BLOCK_RES`, `IS_RENDER_MAX_PAGES`, `IS_BROWSER_PATH`.

---

## 6. Verification suite

```bash
python -m pytest tests/ -q                          # offline
python -m pytest tests/ -q --run-integration        # add real-network / real-render cases (skipped by default)
```

**Measured results (2026-11-25):**

| Run | Result |
|---|---|
| `pytest tests/` (offline, local) | **348 passed / 29 skipped**, ~41s |
| `pytest tests/test_renderer.py tests/test_render.py -q --run-integration` (render subset, real kernel) | **70 passed**, ~86s |
| `pytest tests/ --run-integration` (GitHub Actions: real network + bundled Chromium) | 0 failed; upstream-availability probes show xfail/xpass depending on network |

> If you see "finished but no passed summary line", a delete-protection hook blocked pytest's cleanup of its temp dir under `%TEMP%`. This project sets `--basetemp=.pytest-tmp` in `pyproject.toml`'s `addopts`, so a normal run gives you the summary.

The xfail/xpass cases are upstream-availability probes (`test_all_providers_reachable`, `test_real_search_chinese_yields_enough`, `test_real_search_english_yields_results`, `test_content_fetch_end_to_end`): under upstream anti-bot or soft-blocks they show xfail (rather than keeping the suite red), and xpass once upstream recovers. Assertions include per-result `fetch_status` so the report distinguishes "no results" from "results blocked by anti-bot / robots". **Correctness assertions in the same batch all pass** — legal statuses, per-source status with reasons on failure, and every result handed to the caller relevant to the query.

| Test file | Coverage |
|---|---|
| `test_dom.py` | DOM parsing, highlight-tag order, script/style skipping, no crashes on malformed/deep nesting |
| `test_query.py` | tokenization, intent rewriting (incl. the spec's 50/TOPS example), boolean, time, site |
| `test_providers.py` | four sources parsing real sample pages, field completeness, exception states |
| `test_providers_fetcher.py` | **sources × real SmartFetcher joint path**: params must reach the HTTP layer, SERPs must not be heuristic-upgraded to rendering, hard blocks still rescued without repetition, explicit `mode="render"` not skipped by internal memo |
| `test_rank_fusion.py` | URL/title dedupe, field merging, relevance/freshness/credibility scoring, **paywall marks actually down-rank in sorting**, site/time filters, intermediary-page removal, top_k truncation |
| `test_extractor.py` | body extraction (incl. real doc pages), nav/footer removal, metadata, paragraph splitting, truncation, type detection |
| `test_engine.py` | **status convergence**: OK / PARTIAL / NO_RESULTS / ERROR / rate limit; **`providers=` runs only named sources, all-unmatched converges to ERROR**; cache hits and no caching of failures; **`cache_key` distinguishes source sets**; complete output fields; compression; session memory; **conflict detection and fact keywords not truncated (price/product must not collapse into `pr` and create a false conflict)**; **snippet pre-extraction consumed by the output layer** |
| `test_cache.py` | TTL expiry, namespace isolation, **LRU eviction by exact rowid (same key across namespaces not wrongly deleted)**, corrupt data doesn't throw |
| `test_http_client.py` | **streaming download**: media skipped at zero bytes after headers (no more "download fully then discard"), `IS_FETCH_MAX_BYTES` stops download on reaching cap (a 10KB page reads only 3KB), stream closed on exception paths |
| `test_http_api.py` | **HTTP status mapping**: `ok/partial/no_results`→200, `blocked`→403, `error`→502 (consistent across JSON/context/markdown), `providers=` parsing and passthrough, POST behavior |
| `test_mcp_server.py` | **MCP single search**: both `format=context` and `json` run `search` only once (regression: context used to search twice), output contract |
| `test_ratelimit.py` | token bucket, global/session/host limiting, blacklist, phishing sites, paywall marks |
| `test_regression_2026_10_06.py` | **false-success regressions**: 404/403/verification pages not judged successful (both `fetch` and `_fetch_contents` paths), failures not cached, failure-branch key contract consistent, Crawl-delay effective and cache hits don't queue, nav container must not override body, tokenizer must not destroy content words, question words produce no pseudo-words, junk batches filtered one by one |
| `test_robots.py` | Allow/Disallow priority, wildcards and `$` anchoring, agent matching, crawl-delay, **robots.txt request URL has no double scheme (regression: used to build `http://https://`, blocked by SSRF so rules never took effect)** |
| `test_render.py` | kernel probe, engine name, fallback decisions, four fetch modes, **`mode="render"` explicit degraded when rendering disabled / kernel missing**, **4xx/5xx block pages not accepted (false-success regression)**, **`block_resources` switch and `lang`/locale wiring (traced on a local site)**, real SPA render, timeout, concurrent render, clean degradation |
| `test_browser_bundle.py` | **bundled kernel (12 cases)**: bundled kernel preferred over system browser, explicit path highest priority, cache above system, no-kernel-at-all degrades to `None` without throwing, candidate dedup, install script present and syntactically valid, `render` extra declared, **package scan independent of runtime env var**, **kernel glob matches both new and old headless layouts (`chrome-headless-shell-win64` / old `chrome-win64`)**, `engine_detail` carries the absolute kernel path in both `pool.info()` and `/render-info` |
| `test_renderer.py` | candidate discovery and launch args shape, parameterized `needs_render`, **density judged over full text (skeleton must render / sufficient body must not false-trigger)**, **`RenderRequest.block_resources/lang` passthrough and `_locale_for` mapping**, `_looks_incomplete` density, interface contract, real render and auto-upgrade, resource blocking, concurrency |
| `test_relevance_guard.py` | **anti-bot soft-block gate**: whole irrelevant batches dropped, multi-content-word queries need ≥2 hits, **URL hits can't pad content hits**, domain-only hits don't count, single-content-word queries not falsely killed, empty query not falsely killed, unusable sources reported honestly |
| `test_integration.py` | real Chinese/English search, multi-source plumbing intact, soft-block results never leak (`test_irrelevant_results_never_reach_caller`), multi-source availability (≥2 sources, `xfail`), site/freshness filters, body fetching, repeated-search stability, no-results clear status, timeout |

Key verification conclusions:

- **Degradation visible**: deliberately fail one source → `status=partial`, usable results still returned, that source's `status=error` and reason in `providers`
- **No-results decidable**: all sources healthy but zero results → `status=no_results` + "try shortening the query / widening the time range", clearly distinct from "failure"
- **Failures don't leak through**: simulate a source raising `RuntimeError` → engine converges to `status=error`, nothing thrown to the caller
- **Cache works**: second identical query hits cache, zero `providers` calls, `cached=true`
- **Cross-source consensus**: one result hit by 2 sources → `source_count=2` with a ranking bonus
- **Static pages pay no render cost**: `example.com` gets 92,646 bytes of body over Tier 1 HTTP in 1.7s, browser never starts
- **SPA auto-upgrade**: `quotes.toscrape.com/js/` becomes `fetch_mode="render"` in auto mode, `selector_found=true`, body goes from skeleton to 10 full records
- **Render unavailability not fatal**: target host totally nonexistent → `degraded=True`, `fetch_mode="none"`, `attempts=["http_err:UPSTREAM_ERROR", "render_err:RENDER_ERROR"]`, no exception
- **Concurrent rendering all succeeds**: 3 concurrent renders `render_ok: 3, failed: 0` (1/3 before the threading fix)
- **HTTP semantics correct**: `/search` returns 200 for `no_results` (normal business state, not 502), `blocked`→403, `error`→502; `?providers=` runs only named sources, all-unavailable converges to `status=error` with per-source explanations
- **MCP doesn't search twice**: both `format=context` and `format=json` run one search (before the fix, context ran the whole round twice, doubling network and rate-limit budget)
- **Render false success sealed**: when render returns a 4xx/5xx block page, `rendered=False`, `degraded=True`, and that HTML is not accepted; `mode="render"` with rendering disabled / kernel missing explicitly returns `degraded=True` and a reason, no longer silently falling back to HTTP and claiming success
- **Render switches really work**: `IS_RENDER_BLOCK_RES=0` / `RenderRequest(block_resources=False)` makes image requests really reach the site (hard-asserted by request traces on a local site); `lang` maps to browser locale (zh→`zh-CN`, en→`en-US`)
- **Paywalls really down-rank**: the safety layer's `_penalty` is consumed by ranking — an identical free result ranks above the paywalled one (before the fix the mark was written but never read)
- **Snippets really consumed**: `extra["summary"]` written by `summarize_results` is preferred by `to_context` / `build_context` / `to_markdown`, while `with_content=True` still returns the full body (before the fix it was write-only, pure overhead)
- **Relevance evidence content-only**: a result needs ≥2 content-word hits in title/snippet to pass; URL hits never pad (title 1 + URL 1 no longer passes)
- **No wasted media/huge pages**: with `IS_FETCH_MEDIA=false`, images/video are skipped right after headers (0-byte body); `IS_FETCH_MAX_BYTES` stops download immediately at the cap (a 10KB text page reads only 3KB in tests)

### Continuous integration (GitHub Actions)

| workflow | Trigger | Content |
|---|---|---|
| `ci.yml` | push / PR / manual | offline full-test matrix (ubuntu 3.9, ubuntu 3.13, windows 3.13) + `isearch --help` smoke, ~1 min per run |
| `integration.yml` | daily UTC 02:17 (≈ 10:17 Beijing) / manual | installs bundled Chromium then runs `--run-integration` full real-network regression; junit report kept as artifact for 7 days |

Integration regression is deliberately **not** attached to push: it depends on external sites, is slow, and has upstream volatility — unsuitable as a per-commit gate. The first CI run had all three legs at `253 passed / 27 skipped` (also confirming `requires-python >= 3.9`) and exposed and fixed a real bug on the spot — under the en-US/cp1252 code page `isearch --help` crashed with `UnicodeEncodeError` printing Chinese (regression in `tests/test_cli.py`).
A 2026-11-13 CI run had all three legs at `289 passed / 29 skipped`; the same day's manual full Integration (real network + real Chromium) was `314 passed / 1 skipped / 3 xpassed`, 0 failed. After three P2 fixes (paywall down-ranking / fact-keyword truncation / relevance-evidence scope): offline `294 passed / 29 skipped`, Integration `319 passed / 1 skipped / 3 xpassed`, 0 failed. Another wrap-up round (HTTP streaming / zero-byte media skip, compose key passthrough, doc drift): offline `300 passed / 29 skipped`, Integration `325 passed / 1 skipped / 3 xpassed`, 0 failed.
The 2026-11-22 Integration saw a one-off failure of `test_content_fetch_end_to_end` (real-network body fetching) when the CI egress IP was anti-bot blocked; that upstream-availability assertion was changed to `xfail(strict=False)`. Offline then grew to `336 passed / 29 skipped` with new regression tests. Two latent issues were also fixed in the same period: the double scheme in robots.txt request URLs (which had kept robots rules from ever taking effect), and bundled-kernel detection/discovery misses after the playwright 1.5x rename.
The 2026-11-27 performance and correctness pass changed `compress` from re-serializing the whole payload per removed result (O(n²)) to a binary search (O(log n)), and made `max_chars` measure against the actual `indent=2` output; the paywall penalty is no longer applied twice; `site` filtering now uses hostname suffix matching; consecutive `User-agent` lines in robots.txt correctly share rules; title dedupe gained length bucketing, robots fetching became single-flight, and render waiting switched to an Event. Offline tests grew to `348 passed / 29 skipped`.

---

## 7. Known limitations

- **The bundled kernel trades size for self-containment**: measured at playwright 1.65.2 / chromium-1255, `--check` reports ~219.7MB (two executables: full chrome.exe + headless shell; total download including archives is larger, ~300MB for Linux images), installed under `site-packages` and distributed with the virtualenv / image. The official CDN is easily cut off mid-transfer in China (`Download failed: server closed connection`); use `python scripts/install_browser.py --mirror` for npmmirror. **It runs without the kernel too** — JS-heavy pages just degrade to skeletons, and `/render-info` honestly reports `render_available=false` with the reason
- Built-in sources depend on third-party page structure; a search engine redesign can break one source's parser — hence 4 sources are enabled by default as mutual backups, with visible status and automatic degradation on failure; for production, configure JSON API sources such as Tavily / SerpAPI
- Heavy use can trigger target-site anti-bot; configure the `IS_PROXIES` pool and lower `IS_GLOBAL_QPS`
- **Rendering is serial in a single worker thread**: a consequence of the Playwright sync API's thread binding, at the cost of queueing under high concurrency. More throughput needs more worker threads (one Playwright instance each)
- **No CAPTCHA circumvention**: verification pages trigger a degraded return rather than repeated retries. Deliberately no investment in beating CAPTCHAs — unstable and potentially against a site's wishes
- Rendering executes page JS, effectively running a site's scripts on your behalf; fetching others' sites still requires you to confirm compliance with their terms and robots policy
- Conflict detection is based on "number + unit" pattern matching, covering only quantifiable facts (compute / price / frequency etc.), not opinion-level disagreements
- **Search sources don't get a browser-render fallback**: under `rescue_only` only 4xx / empty body triggers rendering. Reason: measured re-rendered SERPs break every selector (Bing `b_algo` 10→0, 360 `res-list` →0), turning a usable source into 0 results while costing 5~11s more. So when a source is anti-bot rate-limited, it shows `status=empty` while other sources return normally — expected behavior, not a fault
- **Source availability as measured today**: `bing_rss` / `bing_html` normal; `so360` (returns an "access anomaly page") and `sogou` (403) were under external anti-bot rate limiting — no bypass via UA changes, query changes, or a real browser, no recovery after an 8~10 minute cooldown — judged an IP-reputation-level ban, not a code issue
- **The cost of a zero-key approach**: availability is directly tied to public search endpoints. When upstream rate-limits, the result is `no_results` + per-source reasons (`empty` = blocked with no results, `irrelevant` = results page swapped, `error` = request failed). For production, connect a paid JSON API source such as Tavily / SerpAPI as the primary and keep free sources as backup
- **Soft-block detection is per-result**: decided one by one by "≥2 content-word hits / single-content-word query hits title+snippet / authoritative-domain exception"; irrelevant entries are removed individually, and none reach the caller; only when the **whole batch** misses is the source marked `irrelevant`. If upstream **mixes** a few weak hits into otherwise normal results, those may still slip through (blocking them would require a proportional threshold, which would wrongly kill poorly ranked real results) — a deliberate boundary, see 4.4
