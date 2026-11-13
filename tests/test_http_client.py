"""HttpClient 的流式下载与资源过滤(离线, 不需要真实网络)。

回归背景(2026-11-13): 旧实现先 `cli.get()` 再 `resp.content[:limit]` ——
httpx 的 get() 会**立即读完整响应体**, 于是
- "默认不下载图片/视频"实际是"先完整下载再丢弃";
- `IS_FETCH_MAX_BYTES` 只是事后截断, 超大页面仍要全量拉完。
现在改为 stream() + 逐块读取: 媒体在读到响应头后直接跳过(零字节),
文本页读满上限立刻停止。
"""
import httpx
import pytest

from intellisearch.config import Config
from intellisearch.errors import BlockedError
from intellisearch.http.client import HttpClient


class _CountingStream(httpx.SyncByteStream):
    """可计数的响应体流: 记录被读取的字节数与是否关闭。"""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.bytes_read = 0
        self.closed = False

    def __iter__(self):
        for c in self.chunks:
            self.bytes_read += len(c)
            yield c

    def close(self):
        self.closed = True


def _client(handler, **cfg_kw) -> HttpClient:
    """构造注入了 MockTransport 的 HttpClient(不产生真实网络请求)。"""
    cfg = Config(**cfg_kw)
    h = HttpClient(cfg)
    h._client = httpx.Client(transport=httpx.MockTransport(handler))
    return h


def _respond(req, status=200, content_type="text/html; charset=utf-8",
             stream=None, content=None, headers=None):
    hdrs = {"content-type": content_type}
    if headers:
        hdrs.update(headers)
    return httpx.Response(status, headers=hdrs, request=req,
                          stream=stream, content=content)


# ---------------------------------------------------------------- 媒体过滤
def test_media_is_skipped_without_reading_body():
    """媒体响应在读到响应头后即跳过, 响应体一个字节都不读。"""
    stream = _CountingStream([b"\x89PNG" + b"0" * 500] * 4)
    h = _client(lambda req: _respond(req, content_type="image/png",
                                     stream=stream))
    try:
        r = h.get("https://example.com/photo.png", retries=0)
    finally:
        h.close()
    assert r.skipped is True
    assert r.ok is False
    assert "资源过滤" in r.skip_reason
    assert stream.bytes_read == 0, "媒体资源不应把响应体读进内存"
    assert stream.closed is True, "跳过后必须关闭响应流"


def test_media_url_suffix_is_filtered_even_if_content_type_is_wrong():
    """content-type 缺失时按 URL 扩展名兜底识别媒体。"""
    stream = _CountingStream([b"x" * 100])
    h = _client(lambda req: _respond(req, content_type="", stream=stream))
    try:
        r = h.get("https://example.com/a.mp4", retries=0)
    finally:
        h.close()
    assert r.skipped is True and stream.bytes_read == 0


def test_media_allowed_when_configured():
    """fetch_media=True 时必须读取正文(不能误跳过)。"""
    stream = _CountingStream([b"binary-ish"])
    h = _client(lambda req: _respond(req, content_type="image/png",
                                     stream=stream), fetch_media=True)
    try:
        r = h.get("https://example.com/photo.png", retries=0)
    finally:
        h.close()
    assert r.skipped is False
    assert r.text == "binary-ish"
    assert stream.bytes_read == len(b"binary-ish")


# ---------------------------------------------------------------- 读取上限
def test_text_body_read_stops_at_fetch_max_bytes():
    """文本页读满 fetch_max_bytes 后应停止, 而不是先全量下载再截断。"""
    stream = _CountingStream([b"a" * 1000] * 10)      # 10KB
    h = _client(lambda req: _respond(req, stream=stream), fetch_max_bytes=2500)
    try:
        r = h.get("https://example.com/big", retries=0)
    finally:
        h.close()
    assert r.ok is True
    assert len(r.text) == 2500, "正文应被截断到 fetch_max_bytes"
    assert stream.bytes_read == 3000, (
        f"应在达到上限后停止下载, 实际读了 {stream.bytes_read} 字节(共 10000)")


def test_normal_page_is_fully_read():
    body = "正常页面的正文内容。" * 50
    stream = _CountingStream([body.encode("utf-8")])
    h = _client(lambda req: _respond(req, stream=stream))
    try:
        r = h.get("https://example.com/normal", retries=0)
    finally:
        h.close()
    assert r.text == body
    assert stream.bytes_read == len(body.encode("utf-8"))
    assert r.content_type == "text/html"


# ---------------------------------------------------------------- 错误路径
def test_blocked_response_still_raises_and_closes_stream():
    stream = _CountingStream(["<html>captcha 安全验证</html>".encode("utf-8")])
    h = _client(lambda req: _respond(req, status=403,
                                     content_type="text/html", stream=stream))
    try:
        with pytest.raises(BlockedError):
            h.get("https://example.com/blocked", retries=0)
    finally:
        h.close()
    assert stream.closed is True
