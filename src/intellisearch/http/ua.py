"""User-Agent 与浏览器指纹池。

支持 PC / 移动 / 低识别度爬虫三种画像, 可按策略轮换, 用于绕过最基础的反爬。
"""
from __future__ import annotations

import random
from typing import Dict, List, Tuple


PC_AGENTS: List[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36 Edg/125.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.4 Safari/605.1.15",
]

MOBILE_AGENTS: List[str] = [
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Mobile Safari/537.36",
]

# 低识别度: 声明自己是爬虫, 部分站点反而放行(更友好、更合规)
BOT_AGENTS: List[str] = [
    "IntelliSearchBot/1.0 (+https://github.com/intellisearch; AI research crawler)",
    "Mozilla/5.0 (compatible; IntelliSearchBot/1.0; +https://github.com/intellisearch)",
]


class UAPool:
    """UA 轮换池。rotate=True 随机取, 否则固定取第一个(利于缓存命中)。"""

    PROFILES = {"pc": PC_AGENTS, "mobile": MOBILE_AGENTS, "bot": BOT_AGENTS}

    def __init__(self, profile: str = "pc", rotate: bool = True, seed: int = None):
        self.profile = profile if profile in self.PROFILES else "pc"
        self.rotate = rotate
        self._rng = random.Random(seed)

    def get(self) -> str:
        agents = self.PROFILES[self.profile]
        if not self.rotate:
            return agents[0]
        return self._rng.choice(agents)

    def headers(self, lang: str = "zh", extra: Dict[str, str] = None) -> Dict[str, str]:
        """生成一套完整的浏览器请求头(含 UA)。"""
        ua = self.get()
        h = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
                      "application/json;q=0.8,*/*;q=0.7",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8" if lang == "zh"
                               else "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        }
        # 移动端 UA 不附带桌面版 sec-ch-ua, 避免指纹自相矛盾
        if "iPhone" in ua or "Android" in ua:
            h["Sec-Fetch-Dest"] = "document"
        else:
            h["sec-ch-ua"] = '"Chromium";v="126", "Not(A:Brand";v="24"'
            h["sec-ch-ua-platform"] = '"Windows"'
        if extra:
            h.update(extra)
        return h
