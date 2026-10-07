"""QuoteApproval: 报价单的分级审批记录（2026-10-04）。"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPKMixin
from app.db.types import UUIDType


class QuoteApproval(Base, UUIDPKMixin):
    __tablename__ = "quote_approvals"

    quote_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 第几级（1 起）
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    # 本次提交时算出的"总共需要几级"（留档：以后改了阈值也能解释当时为什么批两级）
    required_levels: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)   # approved / rejected
    comment: Mapped[str | None] = mapped_column(String(500), nullable=True)
    approver_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approver_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    approver_role: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["QuoteApproval"]
