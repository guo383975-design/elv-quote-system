"""全局留痕查询（2026-10-04，H-10）。

按单查留痕（`/quotes/{id}/audit-logs`）在**单被删掉之后就没法查了** ——
而那正是最需要查的时刻（"谁把这张单删了"）。这里给一个跨单/跨项目的视图，
数据源是两张留痕表 + 登录尝试表，都带 IP / User-Agent / 单号快照。

权限：`audit_log:read`（admin / finance）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.db.session import get_db
from app.models.audit import LoginAuditLog, ProjectAuditLog, QuoteAuditLog
from app.models.user import User

router = APIRouter(prefix="/audit-logs", tags=["留痕"])


def _row(kind: str, r: QuoteAuditLog | ProjectAuditLog) -> dict:
    return {
        "kind": kind,
        "id": str(r.id),
        "subject_code": r.subject_code,
        "subject_id": str(getattr(r, "quote_id", None) or getattr(r, "project_id", None) or "")
        or None,
        "user_id": str(r.user_id) if r.user_id else None,
        "action": str(r.action),
        "payload": r.payload,
        "note": r.note,
        "ip": r.ip,
        "user_agent": r.user_agent,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        # 外键被 SET NULL 说明"对象已删、留痕还在" —— 前端可以据此显示"（单据已删除）"
        "subject_deleted": (
            getattr(r, "quote_id", None) is None
            if kind == "quote"
            else getattr(r, "project_id", None) is None
        ),
    }


@router.get("/recent", summary="最近的留痕（跨报价单/项目，含已删除单据）")
async def recent_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("audit_log", "read"))],
    subject_code: str | None = Query(None, description="按单号快照过滤，如 QT-202609-0001"),
    action: str | None = Query(None, description="按动作过滤，如 delete"),
    include_login: bool = Query(False, description="是否把登录尝试一起带出来"),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    qstmt = select(QuoteAuditLog).order_by(QuoteAuditLog.created_at.desc()).limit(limit)
    pstmt = select(ProjectAuditLog).order_by(ProjectAuditLog.created_at.desc()).limit(limit)
    if subject_code:
        qstmt = qstmt.where(QuoteAuditLog.subject_code == subject_code)
        pstmt = pstmt.where(ProjectAuditLog.subject_code == subject_code)
    if action:
        qstmt = qstmt.where(QuoteAuditLog.action == action)
        pstmt = pstmt.where(ProjectAuditLog.action == action)

    quotes = (await db.scalars(qstmt)).all()
    projects = (await db.scalars(pstmt)).all()
    rows = [_row("quote", r) for r in quotes] + [_row("project", r) for r in projects]
    rows.sort(key=lambda r: r["created_at"] or "", reverse=True)

    logins: list[dict] = []
    if include_login:
        lstmt = select(LoginAuditLog).order_by(LoginAuditLog.created_at.desc()).limit(limit)
        if action:
            lstmt = lstmt.where(LoginAuditLog.success.is_(action == "login"))
        logins = [
            {
                "username": r.username,
                "success": r.success,
                "reason": r.reason,
                "ip": r.ip,
                "user_agent": r.user_agent,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in (await db.scalars(lstmt)).all()
        ]

    return {"total": len(rows), "items": rows[:limit], "logins": logins}
