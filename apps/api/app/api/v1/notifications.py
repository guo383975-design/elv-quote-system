"""站内通知路由（2026-10-04）。

通知是**按人**的：任何接口都只返回/修改当前登录用户自己的通知
（不存在"管理员帮别人标已读"的需求，也就没给这个口子）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.db.session import get_db
from app.models.notification import Notification
from app.models.user import User
from app.services.notifications import mark_all_read, mark_read, unread_count

router = APIRouter(prefix="/notifications", tags=["通知"])


@router.get("", summary="我的通知（默认只看未读）")
async def list_notifications(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    unread_only: bool = True,
    limit: int = 50,
) -> dict:
    stmt = select(Notification).where(Notification.user_id == user.id)
    if unread_only:
        stmt = stmt.where(Notification.is_read.is_(False))
    rows = (
        await db.scalars(
            stmt.order_by(Notification.created_at.desc()).limit(max(1, min(limit, 200)))
        )
    ).all()
    return {
        "unread": await unread_count(db, user.id),
        "items": [
            {
                "id": str(n.id),
                "kind": n.kind,
                "title": n.title,
                "body": n.body,
                "link": n.link,
                "is_read": n.is_read,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            }
            for n in rows
        ],
    }


@router.get("/unread-count", summary="未读数（顶栏红点）")
async def get_unread_count(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    return {"unread": await unread_count(db, user.id)}


@router.post("/{notification_id}/read", status_code=status.HTTP_204_NO_CONTENT,
             summary="标记一条为已读")
async def read_one(
    notification_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> None:
    from uuid import UUID

    try:
        nid = UUID(notification_id)
    except ValueError:
        raise HTTPException(status_code=404, detail="通知不存在") from None
    ok = await mark_read(db, user.id, nid)
    if not ok:
        raise HTTPException(status_code=404, detail="通知不存在（或不属于当前用户）")


@router.post("/read-all", summary="全部标记已读")
async def read_all(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    return {"marked": await mark_all_read(db, user.id)}
