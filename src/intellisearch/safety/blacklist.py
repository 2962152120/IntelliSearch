"""安全与合规(需求五)。

1. 内容黑名单: 敏感词拦截(可配置词表)
2. 恶意站点: 钓鱼/可疑域名特征识别
3. 版权策略: 只取公开摘要, 识别并跳过需登录/付费才能查看的内容
"""
from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from ..errors import SafetyBlocked
from ..models import SearchResult

# 需登录/付费墙特征
PAYWALL_PATTERNS = re.compile(
    r"(?i)(登录后查看|请先登录|登录后可|需要登录|会员专享|开通会员|"
    r"付费阅读|订阅后|购买后查看|sign in to continue|subscribe to read|"
    r"premium content|paywall|members only)")

# 可疑域名特征(钓鱼常用: 仿冒域名、超长随机子域、敏感词+数字组合)
SUSPICIOUS_RE = re.compile(
    r"(?i)(login|account|verify|update|secure|banking|paypal|wallet)"
    r"[-.](?:[a-z0-9]{4,}\.)?(?:tk|ml|ga|cf|gq|xyz|top|click|link|work)$")

IP_HOST_RE = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}$")


class SafetyGuard:
    """内容安全守卫。默认只做"明显风险"的拦截, 不做过度审查。"""

    def __init__(self, blacklist: Sequence[str] = None,
                 block_suspicious: bool = True,
                 skip_paywall: bool = True):
        self.blacklist = [w.strip().lower() for w in (blacklist or []) if w.strip()]
        self.block_suspicious = block_suspicious
        self.skip_paywall = skip_paywall

    # ---------- query ----------
    def check_query(self, query: str) -> None:
        """检索前检查 query。命中黑名单直接拒绝。"""
        if not self.blacklist or not query:
            return
        q = query.lower()
        for w in self.blacklist:
            if w and w in q:
                raise SafetyBlocked(f"查询命中内容黑名单: {w[:2]}**", query=query)

    # ---------- 结果 ----------
    def filter_results(self, results: Iterable[SearchResult]
                       ) -> Tuple[List[SearchResult], int]:
        """过滤不安全/不可用的结果, 返回 (保留, 剔除数)。"""
        kept, dropped = [], 0
        for r in results:
            if self._hit_blacklist(r):
                dropped += 1
                continue
            if self.block_suspicious and self.is_suspicious(r.url):
                dropped += 1
                continue
            if self.skip_paywall and self.is_paywalled(r):
                r.extra["paywall"] = True
                # 付费墙内容仍保留(标题/摘要可用), 但标记并降权
                r.extra["_penalty"] = 0.85
            kept.append(r)
        return kept, dropped

    def _hit_blacklist(self, r: SearchResult) -> bool:
        if not self.blacklist:
            return False
        text = f"{r.title} {r.snippet}".lower()
        return any(w and w in text for w in self.blacklist)

    # ---------- 判定 ----------
    @staticmethod
    def is_suspicious(url: str) -> bool:
        if not url:
            return True
        try:
            p = urlparse(url)
        except Exception:
            return True
        host = (p.hostname or "").lower()
        if not host:
            return True
        if IP_HOST_RE.match(host):
            return True
        if SUSPICIOUS_RE.search(host):
            return True
        if p.scheme not in ("http", "https"):
            return True
        return False

    @staticmethod
    def is_paywalled(r: SearchResult) -> bool:
        text = f"{r.snippet} {(r.clean_text or '')[:800]}"
        return bool(PAYWALL_PATTERNS.search(text))

    @staticmethod
    def is_public(url: str) -> bool:
        """是否公开可访问(排除明显需要鉴权的路径特征)。"""
        low = (url or "").lower()
        return not re.search(r"(?i)(/login|/signin|/auth|/member|/vip|"
                             r"/subscribe|/checkout)", low)
