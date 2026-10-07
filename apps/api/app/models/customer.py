"""Customer model: 客户档案（甲方主数据）。

字段取舍：
  · `code` 形如 CUS-0001，人可读、可口头引用；
  · 联系人与电话**冗余在客户上**，项目引用客户时自动带出（项目里仍可覆盖成
    该项目专属的联系人）；
  · `tax_no` 等开票信息是报价单/合同抬头要用的，放主数据里避免每次翻聊天记录；
  · 停用而不是删除：历史项目还引用着它。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType

if TYPE_CHECKING:
    from app.models.project import Project


class Customer(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "customers"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    short_name: Mapped[str | None] = mapped_column(String(64), nullable=True)

    contact_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 开票/合同用
    tax_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bank_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(64), nullable=True)

    industry: Mapped[str | None] = mapped_column(String(64), nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)

    created_by: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    projects: Mapped[list[Project]] = relationship(back_populates="customer")


__all__ = ["Customer"]
