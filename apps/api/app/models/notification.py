"""Notification: 站内通知（2026-10-04）。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType


class NotificationKind(StrEnum):
    APPROVAL_PENDING = "approval_pending"    # 有单子等你审批
    QUOTE_APPROVED = "quote_approved"        # 你的单子批了
    QUOTE_REJECTED = "quote_rejected"        # 你的单子被驳回
    DEVICE_EOL = "device_eol"                # 设备停产提醒（预留）
    IMPORT_FAILED = "import_failed"          # 导入有失败行（预留）


class Notification(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "notifications"

    # 收件人：删用户时通知一起删（通知对别人没有意义）
    user_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 前端跳转用（如 /quotes/<id> 或 /projects/<id>）
    link: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


__all__ = ["Notification", "NotificationKind"]
