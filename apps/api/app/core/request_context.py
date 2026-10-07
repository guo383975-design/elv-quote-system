"""请求上下文：把 IP / User-Agent 从 HTTP 层带到业务层（2026-10-04，H-10）。

为什么用 ContextVar + 中间件，而不是给每个路由加 `Request` 参数：
留痕写入点散在 4 个模块（quotes / projects / exports / 以后的），
逐个加参数既吵又容易漏；中间件一次设置、业务层随手可读。

IP 取值顺序：`X-Forwarded-For` 第一跳 → `X-Real-IP` → `request.client.host`。
本系统前面是 nginx，不读转发头的话所有人都会被记成 127.0.0.1。
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass

# 长度与 audit 表的列保持一致，超长直接截断（不让中间件因为 UA 太长而 500）
_IP_MAX = 64
_UA_MAX = 256


@dataclass(frozen=True)
class RequestContext:
    ip: str | None = None
    user_agent: str | None = None


_EMPTY = RequestContext()
_ctx: ContextVar[RequestContext] = ContextVar("request_context", default=_EMPTY)


def set_request_context(ip: str | None, user_agent: str | None) -> None:
    _ctx.set(
        RequestContext(
            ip=(ip or "").strip()[:_IP_MAX] or None,
            user_agent=(user_agent or "").strip()[:_UA_MAX] or None,
        )
    )


def current_request_context() -> RequestContext:
    """没有请求上下文时（脚本/定时任务）返回空值，不抛异常。"""
    return _ctx.get()


def client_ip_from_headers(
    forwarded_for: str | None, real_ip: str | None, peer: str | None
) -> str | None:
    """从转发头里取客户端 IP（取 XFF 的第一跳）。"""
    if forwarded_for:
        first = forwarded_for.split(",")[0].strip()
        if first:
            return first
    if real_ip and real_ip.strip():
        return real_ip.strip()
    return peer
