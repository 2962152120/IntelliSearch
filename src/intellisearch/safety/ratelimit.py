"""分层限流(需求五.3)。

- 全局桶: 保护下游检索源与本机出口带宽
- 会话桶: 按 session_id 限制单会话 QPS, 防滥用
- 主机桶: 单域名最小请求间隔(尊重目标站点, 配合 robots crawl-delay)

Token Bucket 实现, 线程安全, 支持"借用 burst"的突发容忍。
"""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Dict, Optional

from ..errors import RateLimitError


class TokenBucket:
    """经典令牌桶。rate=每秒生成令牌数, burst=桶容量。"""

    def __init__(self, rate: float, burst: int = 1, name: str = ""):
        self.rate = max(float(rate), 0.01)
        self.capacity = max(int(burst), 1)
        self.name = name
        self._tokens = float(self.capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: float = 1.0, block: bool = False,
                timeout: float = 0.0) -> bool:
        """取用令牌。不足时返回 False(或阻塞等待)。

        block=True 且 timeout<=0 时给一个保守的 5s 上限 —— 旧实现把 deadline
        设成 now, 导致阻塞模式"立即返回 False", 与 block 语义自相矛盾。
        """
        if block:
            wait_cap = timeout if timeout > 0 else 5.0
            deadline = time.monotonic() + wait_cap
        else:
            deadline = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return True
                deficit = (tokens - self._tokens) / self.rate
            if not block:
                return False
            if time.monotonic() + deficit > deadline:
                return False
            time.sleep(min(deficit, 0.5))

    def refund(self, tokens: float = 1.0) -> None:
        """归还令牌(上限不超过桶容量)。用于跨桶原子性失败时回滚。"""
        with self._lock:
            self._tokens = min(self.capacity, self._tokens + tokens)

    @property
    def available(self) -> float:
        with self._lock:
            return self._tokens


class RateLimiter:
    """全局 + 会话 + 主机 三层限流。"""

    def __init__(self, global_qps: float = 5.0, global_burst: int = 10,
                 session_qps: float = 1.0, session_burst: int = 3,
                 enabled: bool = True, max_sessions: int = 500):
        self.enabled = enabled
        self.global_bucket = TokenBucket(global_qps, global_burst, "global")
        self.session_qps = session_qps
        self.session_burst = session_burst
        self.max_sessions = max_sessions
        self._sessions: "OrderedDict[str, TokenBucket]" = OrderedDict()
        self._hosts: Dict[str, float] = {}     # host -> 下次允许请求时间
        self._lock = threading.Lock()

    def _session_bucket(self, sid: str) -> TokenBucket:
        with self._lock:
            b = self._sessions.get(sid)
            if b is None:
                b = TokenBucket(self.session_qps, self.session_burst, sid)
                self._sessions[sid] = b
                self._sessions.move_to_end(sid)
                while len(self._sessions) > self.max_sessions:
                    self._sessions.popitem(last=False)
            return b

    def acquire(self, session_id: str = None, block: bool = False,
                timeout: float = 0.0) -> None:
        """申请一次检索配额。超限抛 RateLimitError。"""
        if not self.enabled:
            return
        if not self.global_bucket.consume(1.0, block=block, timeout=timeout):
            raise RateLimitError(
                f"全局检索限流触发(上限 {self.global_bucket.rate:.1f} QPS)")
        if session_id:
            b = self._session_bucket(session_id)
            if not b.consume(1.0, block=False):
                # 会话桶失败时把刚扣掉的全局令牌还回去 —— 否则一次会话限流
                # 会白白吃掉一个全局配额, 237 个不同会话的滥用能拖垮全局。
                self.global_bucket.refund(1.0)
                raise RateLimitError(
                    f"会话 {session_id} 检索过于频繁(上限 {b.rate:.1f} QPS)")

    def wait_host(self, url_or_host: str, crawl_delay: float = None,
                  default_delay: float = 0.0) -> None:
        """按域名限速: 保证同一主机的两次请求间隔。"""
        if not self.enabled:
            return
        host = url_or_host.split("//")[-1].split("/")[0].lower()
        delay = crawl_delay if crawl_delay is not None else default_delay
        if delay <= 0:
            return
        with self._lock:
            now = time.monotonic()
            next_ok = self._hosts.get(host, 0.0)
            wait = max(0.0, next_ok - now)
            self._hosts[host] = max(now, next_ok) + delay
        if wait > 0:
            # 完整遵守站点声明的 crawl-delay(此前 min(wait, 5.0) 会截断,
            # 声明 7s/10s 的站点实际只等 5s —— 等于没遵守 robots)。
            # 60s 上限只是防止恶意超大值把线程挂死, 正常站点远低于此。
            time.sleep(min(wait, 60.0))

    def stats(self) -> dict:
        return {
            "enabled": self.enabled,
            "global_available": round(self.global_bucket.available, 2),
            "sessions": len(self._sessions),
            "hosts_tracked": len(self._hosts),
        }
