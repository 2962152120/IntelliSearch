# syntax=docker/dockerfile:1
FROM python:3.11-slim

LABEL org.opencontainers.image.title="IntelliSearch" \
      org.opencontainers.image.description="面向大模型的自研联网检索服务"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    IS_CACHE_PATH=/data/cache.sqlite3 \
    IS_AUDIT_PATH=/data/audit.log

WORKDIR /app

# ---- 渲染内核: 装进镜像, 不依赖运行时环境 ----
# 用 playwright 自带的 chromium(PLAYWRIGHT_BROWSERS_PATH=0 装进 site-packages),
# 而不是 Debian 的chromium 包: 这样"包内内核优先"的发现策略在容器里同样成立,
# 镜像自包含, 换宿主/换基础镜像都不会因为缺浏览器而退化成纯 HTTP。
# 仍需装字体, 否则中文页面渲染出的是豆腐块。
RUN apt-get update && apt-get install -y --no-install-recommends \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# playwright 库 + 内核(装在包内, 与 find_chromium 的"内置优先"策略对应)
ENV PLAYWRIGHT_BROWSERS_PATH=0
RUN pip install --no-cache-dir playwright \
    && python -m playwright install --with-deps chromium

COPY pyproject.toml README.md ./
COPY src/ ./src/

# 非 root 运行(chromium sandbox 需要额外能力, 故加 --no-sandbox 启动参数)
RUN useradd -m -u 10001 isearch && mkdir -p /data && chown -R isearch /data
USER isearch

EXPOSE 8787

# 启动即自检: 内核没装好就直接暴露, 而不是运行期静默降级
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "intellisearch.cli", "--serve", "8787"]
