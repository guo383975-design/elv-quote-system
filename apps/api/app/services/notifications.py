"""通知的写入与查询（2026-10-04）。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.permissions import PERMISSION_MATRIX, Action
from app.models.notification import Notification
from app.models.user import User, UserStatus

log = get_logger("services.notifications")


async def users_with_permission(
    db: AsyncSession, resource: str, action: Action
) -> list[UUID]:
    """找出所有对该资源动作有权限的**在用**用户（含 superuser）。

    直接查权限矩阵而不是维护一份"审批人名单"：矩阵改了通知对象自动跟着变，
    不会出现"新加的经理收不到审批提醒"。
    """
    roles = PERMISSION_MATRIX.get(resource, {}).get(action, set())
    if not roles:
        return []
    rows = (
        await db.scalars(
            select(User.id).where(
                User.status == UserStatus.ACTIVE,
                (User.role.in_([str(r) for r in roles])) | (User.is_superuser.is_(True)),
            )
        )
    ).all()
    return list(rows)


def notify(
    db: AsyncSession,
    *,
    user_ids: list[UUID],
    kind: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
) -> int:
    """给若干人各写一条通知（调用方 flush/commit）。返回写入条数。"""
    seen: set[UUID] = set()
    n = 0
    for uid in user_ids:
        if uid is None or uid in seen:
            continue
        seen.add(uid)
        db.add(
            Notification(user_id=uid, kind=kind, title=title[:200], body=body, link=link)
        )
        n += 1
    if n:
        log.info(f"写入 {n} 条通知（{kind}）：{title[:60]}")
    return n


async def unread_count(db: AsyncSession, user_id: UUID) -> int:
    from sqlalchemy import func

    return (
        await db.scalar(
            select(func.count()).select_from(Notification).where(
                Notification.user_id == user_id, Notification.is_read.is_(False)
            )
        )
    ) or 0


async def mark_read(db: AsyncSession, user_id: UUID, notification_id: UUID) -> bool:
    row = await db.get(Notification, notification_id)
    if row is None or row.user_id != user_id:
        return False
    row.is_read = True
    row.read_at = datetime.now(tz=UTC)
    await db.flush()
    return True


async def mark_all_read(db: AsyncSession, user_id: UUID) -> int:
    res = await db.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.is_read.is_(False))
        .values(is_read=True, read_at=datetime.now(tz=UTC))
    )
    await db.flush()
    return int(res.rowcount or 0)
