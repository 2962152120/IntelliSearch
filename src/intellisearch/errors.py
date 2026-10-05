"""IntelliSearch 异常体系。

设计原则: 任何一路检索源失败都必须收敛为可判定的异常类型，
由引擎聚合成明确的状态码, 绝不让异常穿透到调用方。
"""


class IntelliSearchError(Exception):
    """所有 IntelliSearch 异常的基类。"""

    code = "INTERNAL_ERROR"
    retryable = False

    def __init__(self, message: str, **detail):
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self):
        return {"code": self.code, "message": self.message, "detail": self.detail}


class ConfigError(IntelliSearchError):
    code = "CONFIG_ERROR"


class RateLimitError(IntelliSearchError):
    """本地限流触发。"""

    code = "RATE_LIMITED"
    retryable = True


class UpstreamError(IntelliSearchError):
    """检索源/抓取目标返回错误。"""

    code = "UPSTREAM_ERROR"
    retryable = True

    def __init__(self, message: str, source: str = "", status_code: int = 0, **detail):
        super().__init__(message, source=source, status_code=status_code, **detail)
        self.source = source
        self.status_code = status_code


class TimeoutError_(IntelliSearchError):
    code = "TIMEOUT"
    retryable = True


class EmptyResultError(IntelliSearchError):
    """所有检索源均无结果。这是一个正常的业务状态, 不是故障。"""

    code = "NO_RESULTS"


class BlockedError(IntelliSearchError):
    """被目标站点反爬拦截（验证码 / 403 / 重定向到安全页）。"""

    code = "BLOCKED"
    retryable = True


class RobotsDisallowed(IntelliSearchError):
    """robots.txt 禁止访问。"""

    code = "ROBOTS_DISALLOWED"


class ContentError(IntelliSearchError):
    """内容解析/抽取失败。"""

    code = "CONTENT_ERROR"


class SafetyBlocked(IntelliSearchError):
    """命中内容安全黑名单。"""

    code = "SAFETY_BLOCKED"
