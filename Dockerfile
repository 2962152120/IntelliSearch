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

# ---- 系统 Chromium(渲染能力) ----
# 用 Debian 自带的 chromium 而不是 playwright 自带内核: 体积小得多,
# 且与 playwright 通过 executable_path 启动即可, 不需要下载 ms-playwright 缓存。
RUN apt-get update && apt-get install -y --no-install-recommends \
        chromium \
        fonts-noto-cjk \
        fonts-wqy-zenhei \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# playwright 只装库, 不执行 `playwright install`(内核已由系统 chromium 提供)
RUN pip install --no-cache-dir playwright

COPY pyproject.toml README.md ./
COPY src/ ./src/

ENV IS_BROWSER_PATH=/usr/bin/chromium

# 非 root 运行(chromium sandbox 需要额外能力, 故加 --no-sandbox 启动参数)
RUN useradd -m -u 10001 isearch && mkdir -p /data && chown -R isearch /data
USER isearch

EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4).status==200 else 1)"

CMD ["python", "-m", "intellisearch.cli", "--serve", "8787"]
