# 更新记录

本项目所有重要变更都会记录在此文件中。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/spec/v2.0.0.html)。

---

## [未发布]

### 修复

- **运行时内置内核定位漏掉 headless shell**：`browser.py` 的 `_PKG_BROWSER_GLOBS`
  沿用 playwright 1.4x 的旧路径/文件名（`chrome-win64/headless_shell.exe`），
  而 1.5x 起实际是 `chrome-headless-shell-win64/chrome-headless-shell.exe`。
  导致"只装 headless 内核"的精简部署被误判为无内置内核、退回系统浏览器。
  新旧布局 glob 全部兼容；新增伪造包目录的回归测试。
- **robots.txt 在生产环境从未真正生效**：`_host()` 返回的 host 已带 scheme，
  `_rules_for()` 又拼一次 `{scheme}://{host}`，生成 `http://https://...` 双重前缀，
  被 SSRF 校验拦下后两个 scheme 均失败、规则一律放行。现剥掉原 scheme 只留
  netloc 再拼；新增覆盖 fetcher URL 的回归测试（此前测试都预填缓存绕过了该路径）。
- `scripts/install_browser.py` 的内核检测在 Windows 上漏认 headless 内核：
  playwright 1.5x 起其可执行文件从 `headless_shell.exe` 改为
  `chrome-headless-shell.exe`，旧名单导致 `--check` 只报告几 MB 的 chrome.exe、
  漏掉 200MB+ 的 headless shell。三个平台均补上带连字符的名字。
- 夜间集成工作流的 `test_content_fetch_end_to_end` 偶发失败：该用例依赖真实网络，
  CI/云主机出口 IP 被搜索源或文章站反爬（200 拦截页 / 403）时会拿不到正文。
  与同文件其它上游可用性探针一致标记为 `xfail(strict=False)`——上游正常时仍执行
  并报告（XPASS），被拦截时不再让整个工作流失败；断言同时附上逐条 `fetch_status`
  便于从报告定位是「无结果」还是「结果被反爬/robots 拦截」。

---

## [1.0.1] - 2026-11-18

安全加固与检索正确性修复版本。合并了两条独立开发线的成果：本地安全审计修复
与上游 `main` 的 P1/P2 系列修复。

### 安全

- **SSRF 防护**：新增 `safety/urlcheck.py`，在 `engine.fetch()`、HTTP 客户端请求前
  与重定向 hook、浏览器渲染 `page.goto` 前后三处拦截内网/回环/链路本地/云元数据
  地址及 IP 变形（整数、十六进制、点分缩写）。命中返回 `status=UNSAFE_URL`。
  可选 `IS_SSRF_DNS_CHECK` 对解析出的 IP 复查。本地开发/测试可用
  `IS_ALLOW_LOCALHOST` 仅豁免回环地址（生产默认关闭）。
- **API 密钥不再进 URL**：Tavily 改 `Authorization: Bearer`、SerpAPI 改 `X-API-Key`
  走请求头；新增 `redact_url()` 对日志与响应元数据里的敏感查询参数脱敏。
- **HTTP 服务加固**：新增可选 `IS_API_TOKEN` 鉴权（Bearer / X-API-Key，`/health`
  仍放行）、`IS_CORS_ORIGINS` 来源白名单、`IS_MAX_BODY_BYTES` POST 请求体上限。
- **Docker 部署收敛**：容器内监听 `0.0.0.0` 使端口映射生效，宿主侧默认绑回环；
  CLI 新增 `--host`，本机默认 `127.0.0.1`。

### 修复

- 相关性闸门不再把 URL 命中算作独立证据；权威域名判定改 hostname 精确/后缀匹配，
  杜绝 `evil.com/?ref=github.com` 绕过。
- 付费墙 `_penalty` 降权真正参与排序；布尔 `NOT` / `-排除词` 在结果侧生效。
- 全部检索源被判无关（软封）时返回 `NO_RESULTS` 而非 `ERROR`；403 不再重试，
  避免加深 IP 信誉惩罚（并修复 `BlockedError` 缺失 `status_code` 属性）。
- 商业源配置 Key 后自动启用（显式传 `providers=` 时以调用方为准）。
- 配置改为**实例化时**读取环境变量，运行时修改即刻生效。
- 限流：`block` 模式等待语义修正、会话超限退还全局令牌、crawl-delay 不再被
  5 秒截断。
- MCP `format=context` 不再重复检索；正文缓存 key 两条路径统一；缓存 key 纳入
  `providers` / `vertical` 维度；`_merge_options` 不再原地改写调用方对象。
- 浏览器池单例 key 改用配置值而非 `id(config)`，避免对象回收后 ID 复用。

### 变更

- Dockerfile / docker-compose / README / `.env.example` 同步上述安全配置与部署说明。
- 新增 34 项安全回归测试；脆弱的外部页面断言改为本地 fixture。

### 上游既有修复（P1/P2，随合并纳入本版）

- 渲染链降级与假成功判定、资源与 locale 接线、HTTP providers、NO_RESULTS 状态码、
  MCP 双检索、缓存淘汰（P1）。
- 付费墙降权、事实关键词不再截断、相关性证据只看内容（P2）。
- HTTP 流式下载（媒体资源零字节跳过）、compose Key 透传、CI 工作流、
  Chromium 内核随包分发、非 UTF-8 代码页兼容。

---

## [1.0.0] - 2026-10

首个公开发布版本。

### 新增

- 面向大模型的联网检索库：多源聚合（bing_rss / bing_html / sogou / so360，
  可选 tavily / serpapi / brave），输出带引用编号的结构化上下文。
- 六层架构：接入层（Python API / CLI / HTTP REST / MCP）、AI 适配、引擎编排、
  检索核心、Provider 与抓取、抽取与支撑。
- 三级抓取降级链（轻量 HTTP → Playwright 渲染 → 降级返回）、零依赖 DOM 正文抽取、
  SQLite 缓存、三层限流、反爬软封相关性闸门、会话上下文记忆与多源冲突检测。
