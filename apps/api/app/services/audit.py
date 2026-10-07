"""留痕构造助手（2026-10-04，H-10）。

原来各处以 `QuoteAuditLog(...)` 直接构造，**没有人填 IP/UA，也没有单号快照**
→ 一旦报价单被删（外键 CASCADE）整条留痕跟着消失，事后无法回答"谁在什么时候删了哪张单"。

现在统一走这里：
  · 自动带上当前请求的 IP / User-Agent（`request_context`）；
  · 自动把 `quote_code` / `project_code` 快照进 `subject_code` ——
    外键改成 `ON DELETE SET NULL` 之后，删单留下的留痕仍然看得出是哪张单。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from app.core.request_context import current_request_context
from app.models.audit import (
    AuditAction,
    LoginAuditLog,
    ProjectAuditLog,
    QuoteAuditLog,
    SettingAuditAction,
    SettingAuditLog,
)


def _ctx() -> tuple[str | None, str | None]:
    c = current_request_context()
    return c.ip, c.user_agent


def quote_audit(
    *,
    quote_id: UUID | None,
    quote_code: str | None = None,
    user_id: UUID | None,
    action: AuditAction,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> QuoteAuditLog:
    ip, ua = _ctx()
    return QuoteAuditLog(
        quote_id=quote_id,
        subject_code=quote_code,
        user_id=user_id,
        action=action,
        payload=payload or {},
        note=note,
        ip=ip,
        user_agent=ua,
    )


def project_audit(
    *,
    project_id: UUID | None,
    project_code: str | None = None,
    user_id: UUID | None,
    action: AuditAction,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> ProjectAuditLog:
    ip, ua = _ctx()
    return ProjectAuditLog(
        project_id=project_id,
        subject_code=project_code,
        user_id=user_id,
        action=action,
        payload=payload or {},
        note=note,
        ip=ip,
        user_agent=ua,
    )


def login_audit(
    *,
    username: str,
    user_id: UUID | None,
    success: bool,
    reason: str | None = None,
) -> LoginAuditLog:
    """登录尝试留痕。既是审计，也是**登录失败锁定**的计数依据（见 auth.py）。"""
    ip, ua = _ctx()
    return LoginAuditLog(
        username=(username or "").strip()[:64],
        user_id=user_id,
        success=success,
        reason=(reason or None),
        ip=ip,
        user_agent=ua,
    )


def setting_audit(
    *,
    user_id: UUID | None,
    username: str | None,
    action: SettingAuditAction,
    summary: str | None = None,
    diff: list[dict[str, Any]] | None = None,
) -> SettingAuditLog:
    """设置/权限变更留痕。

    `diff` 用 `_settings_diff` 算好的结构（组 + 字段 + 前后值）。
    **调用方负责不要把密钥塞进来** —— 这里只做搬运。
    """
    ip, ua = _ctx()
    return SettingAuditLog(
        user_id=user_id,
        username=(username or "")[:64] or None,
        action=str(action),
        summary=(summary or "")[:512] or None,
        payload={"diff": diff or []},
        ip=ip,
        user_agent=ua,
    )
