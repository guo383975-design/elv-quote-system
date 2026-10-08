"""QuoteRevision: 报价单版本快照（2026-10-04）。

快照存 JSONB 而不是"关联到明细表的另一套表"：
明细会继续被改，快照必须**冻结**当时的样子 —— 用引用就等于没存。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPKMixin
from app.db.types import JSONType, UUIDType


class QuoteRevision(Base, UUIDPKMixin):
    __tablename__ = "quote_revisions"
    __table_args__ = (
        UniqueConstraint("quote_id", "seq", name="uq_quote_revisions_quote_seq"),
    )

    quote_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("quotes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 单内自增序号（1,2,3…）。**内部用** ✓：版本排序、两版对比、追溯 ✓。
    # ⚠ 2026-10-08 起**不再当"版本号"展示给用户** ✗ —— 用户看到的是
    #   `created_at` 的**时间标记**（"版本号以时间为标记"✓，见 services/revisions.py
    #   的 attach_revision_stats 与 web 的 dateTimeSec）。
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # 快照时刻 quotes.version 的值（乐观锁计数器）
    version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 为什么落这一版：submit / approve / manual
    reason: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 快照时的状态与标题（列表里直接显示，不用展开 JSON）
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    # 合计快照：{total_with_tax, equipment_total, install_total, auxiliary_total, ...}
    totals: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    # 完整快照：{header: {...}, items: [...]}
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


__all__ = ["QuoteRevision"]
