"""robots.txt 解析与访问判定。

特性:
- 按 agent 匹配 User-agent 段(支持 * 与前缀匹配);
- 支持 Allow/Disallow/Crawl-delay/Sitemap;
- 缓存已抓取的 robots, 失败时默认放行(避免网络抖动导致整个服务不可用);
- 提供 crawl_delay 供限流使用。
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

from ..errors import RobotsDisallowed


class RobotsRule:
    def __init__(self):
        self.allow: List[str] = []
        self.disallow: List[str] = []
        self.crawl_delay: Optional[float] = None

    def applies_to(self, path: str) -> Tuple[bool, bool]:
        """返回 (是否匹配到规则, 是否允许)。

        robots 语义: 最长匹配优先; 同长度时 Allow 优先于 Disallow。
        """
        best_len = -1
        best_allow = True
        matched = False
        for pat, is_allow in ([(p, True) for p in self.allow] +
                              [(p, False) for p in self.disallow]):
            if pat == "":
                # Disallow: 空值表示全部允许; Allow: 空值不参与匹配
                if not is_allow:
                    if 0 > best_len:
                        best_len, best_allow, matched = 0, True, True
                continue
            if _pattern_match(pat, path):
                matched = True
                if len(pat) > best_len or (len(pat) == best_len and is_allow):
                    best_len, best_allow = len(pat), is_allow
        return matched, best_allow


def _pattern_match(pattern: str, path: str) -> bool:
    """robots 前缀 + 通配匹配(* 任意字符, $ 结尾)。"""
    end_anchor = pattern.endswith("$")
    pat = pattern[:-1] if end_anchor else pattern
    segs = pat.split("*")
    idx = 0
    for i, seg in enumerate(segs):
        if seg == "":
            continue
        pos = path.find(seg, idx)
        if pos < 0:
            return False
        if i == 0 and not pat.startswith("*") and pos != 0:
            return False
        idx = pos + len(seg)
    if end_anchor and idx != len(path):
        return False
    return idx <= len(path)


class RobotsCache:
    """带 TTL 的 robots.txt 缓存。"""

    def __init__(self, fetcher=None, ttl: float = 3600.0, user_agent: str = "*"):
        self._fetcher = fetcher
        self._ttl = ttl
        self._ua = user_agent.lower()
        self._cache: Dict[str, Tuple[float, Optional[Dict[str, RobotsRule]]]] = {}

    def _parse(self, text: str) -> Dict[str, RobotsRule]:
        rules: Dict[str, RobotsRule] = {}
        current: List[str] = []
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line or ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "user-agent":
                current = [v.lower()]
                for name in current:
                    rules.setdefault(name, RobotsRule())
            elif k in ("allow", "disallow") and current:
                for name in current:
                    r = rules.setdefault(name, RobotsRule())
                    (r.allow if k == "allow" else r.disallow).append(v)
            elif k == "crawl-delay" and current:
                try:
                    for name in current:
                        rules.setdefault(name, RobotsRule()).crawl_delay = float(v)
                except ValueError:
                    pass
        return rules

    def _rules_for(self, host: str) -> Optional[Dict[str, RobotsRule]]:
        now = time.time()
        hit = self._cache.get(host)
        if hit and now - hit[0] < self._ttl:
            return hit[1]
        if self._fetcher is None:  # 离线模式(测试): 全部放行
            self._cache[host] = (now, None)
            return None
        for scheme in ("https", "http"):
            try:
                resp = self._fetcher(f"{scheme}://{host}/robots.txt")
                if resp and resp.status_code == 200 and resp.text:
                    parsed = self._parse(resp.text)
                    self._cache[host] = (now, parsed)
                    return parsed
            except Exception:
                continue
        self._cache[host] = (now, None)   # 抓不到 -> 放行
        return None

    @staticmethod
    def _host(url: str) -> str:
        p = urlparse(url)
        return f"{p.scheme}://{p.netloc}" if p.scheme else p.netloc

    def check(self, url: str, user_agent: str = None) -> Tuple[bool, Optional[float]]:
        """返回 (是否允许访问, crawl_delay)。"""
        host = self._host(url)
        rules = self._rules_for(host)
        if not rules:
            return True, None
        p = urlparse(url)
        path = p.path or "/"
        if p.query:
            path = path + "?" + p.query
        ua = (user_agent or self._ua).lower()
        # 优先精确匹配, 再回退到 *
        candidates = [r for name, r in rules.items() if name == ua]
        if not candidates:
            candidates = [r for name, r in rules.items() if name == "*"]
        if not candidates:
            # 前缀匹配
            candidates = [r for name, r in rules.items()
                          if name != "*" and ua.startswith(name)]
        if not candidates:
            return True, None
        allowed = True
        delay = None
        for r in candidates:
            matched, allow = r.applies_to(path)
            if matched:
                allowed = allowed and allow
            if r.crawl_delay:
                delay = r.crawl_delay if delay is None else max(delay, r.crawl_delay)
        return allowed, delay

    def assert_allowed(self, url: str, user_agent: str = None) -> None:
        allowed, _ = self.check(url, user_agent)
        if not allowed:
            raise RobotsDisallowed(f"robots.txt 禁止访问: {url}", url=url)
