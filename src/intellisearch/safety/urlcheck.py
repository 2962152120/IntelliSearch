"""公网 URL 校验 —— 防止 SSRF(服务端请求伪造)。

背景: /fetch 与 MCP fetch 接受任意 URL, 若不校验, 攻击者可让本工具去请求
内网服务、云元数据端点(169.254.169.254)或 file:// 等本地资源, 把响应
当作"正文"带回来 —— 等于一个内网探测 + 凭据读取的代理。

防护策略(默认零 DNS 开销):
1. scheme 必须是 http/https;
2. host 不能是回环/私网/链路本地/保留地址(含云元数据段);
3. host 不能是 localhost / *.localhost / *.internal;
4. 识别整数/十六进制/点分缩写等 IP 变形, 防止 `http://2130706433/` 绕过;
5. 由调用方在每一跳重定向后复查最终 URL(DNS rebinding 的可选防护见
   Config.ssrf_dns_check, 默认关闭以免离线环境误伤)。

设计原则: 只挡"明确不安全"的地址, 不误伤公网域名与公网 IP。
"""
from __future__ import annotations

import ipaddress
import os
import re
import socket
from typing import Optional
from urllib.parse import urlparse

from ..errors import UnsafeUrlError

# 明确非法的主机名后缀(云元数据常用 .internal)
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal")
_LOCAL_NAMES = ("localhost", "localhost.localdomain")

# 点分/整数/十六进制 IP 变形: 允许数字 + 点 + 0x 前缀
_IPISH_RE = re.compile(r"^(?:0x[0-9a-f]+|\d+)(?:\.(?:0x[0-9a-f]+|\d+)){0,3}$", re.I)


def _expand_ipish(host: str) -> Optional[str]:
    """把 `127.1` / `2130706433` / `0x7f000001` 展开成标准点分 IP。

    返回 None 表示不是 IP 变形。ipaddress 本身不接受缩写形式, 这里手工展开。
    """
    h = host.lower()
    if not _IPISH_RE.match(h):
        return None
    try:
        if h.startswith("0x") and "." not in h:
            n = int(h, 16)
            if 0 <= n <= 0xFFFFFFFF:
                return str(ipaddress.IPv4Address(n))
            return None
        parts = h.split(".")
        # 纯单段十进制整数(如 2130706433)按 32 位整数解析
        if len(parts) == 1:
            n = int(parts[0], 10)
            if 0 <= n <= 0xFFFFFFFF:
                return str(ipaddress.IPv4Address(n))
            return None
        # 点分缩写: 最后一段补齐到 4 段 (a.b.c -> a.b.c.0, a.b -> a.0.0.b)
        nums = [int(p, 16) if p.startswith("0x") else int(p, 10) for p in parts]
        if len(nums) == 2:
            nums = [nums[0], 0, 0, nums[1]]
        elif len(nums) == 3:
            nums = [nums[0], nums[1], 0, nums[2]]
        if len(nums) != 4 or any(n < 0 or n > 255 for n in nums):
            return None
        return ".".join(str(n) for n in nums)
    except (ValueError, ipaddress.AddressValueError):
        return None


def is_public_url(url: str) -> bool:
    """判断 URL 是否指向公网。不做 DNS 解析, 只做字面量判定。"""
    if not url or not isinstance(url, str):
        return False
    try:
        p = urlparse(url)
    except Exception:                       # noqa: BLE001
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower().rstrip(".")
    if not host:
        return False

    # 本地开发/测试豁免: 显式开启 IS_ALLOW_LOCALHOST 时才允许回环/私网地址。
    # 生产默认关闭, 避免 /fetch 成为内网探测与云元数据读取的代理。
    allow_local = os.getenv("IS_ALLOW_LOCALHOST", "").strip().lower() in (
        "1", "true", "yes", "on")

    if host in _LOCAL_NAMES or host.endswith(_LOCAL_SUFFIXES):
        return allow_local

    # 直接 IP 字面量
    candidate = host
    expanded = _expand_ipish(host)
    if expanded:
        candidate = expanded
    try:
        ip = ipaddress.ip_address(candidate)
    except ValueError:
        ip = None
    if ip is not None:
        # 豁免只覆盖回环(本地测试用), 不放行链路本地(169.254 元数据)与其它私网
        if allow_local and ip.is_loopback:
            return True
        return bool(ip.is_global)

    # 可选 DNS 复查: 域名若解析到私网 IP(DNS rebinding / 内网域名), 同样拒绝。
    # 默认关闭(IS_SSRF_DNS_CHECK), 避免离线环境/无 DNS 时误伤; 开启后解析
    # 失败仍放行(可用性优先), 只拦"明确解析到非公网"的情况。
    dns_check = os.getenv("IS_SSRF_DNS_CHECK", "").strip().lower() in (
        "1", "true", "yes", "on")
    if dns_check:
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
            for info in infos:
                addr = info[4][0]
                resolved = ipaddress.ip_address(addr)
                if not resolved.is_global:
                    return False
        except (socket.gaierror, ValueError, OSError):
            pass
    return True


def validate_public_url(url: str) -> None:
    """校验 URL 可安全请求。不安全时抛 UnsafeUrlError。"""
    if not url or not isinstance(url, str) or not url.strip():
        raise UnsafeUrlError("URL 为空", url=str(url))
    if not is_public_url(url):
        raise UnsafeUrlError(
            "拒绝访问非公网地址(疑似 SSRF): 仅允许 http/https 且目标不是"
            "回环/私网/链路本地/云元数据地址", url=str(url)[:200])


def check_redirect_target(url: str) -> None:
    """重定向后复查(与 validate_public_url 同义, 单独命名便于调用点表意)。"""
    validate_public_url(url)
