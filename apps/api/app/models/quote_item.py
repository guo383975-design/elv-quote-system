"""Quote item: 报价单内一行设备."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType, sa_enum


class ItemSource(StrEnum):
    LIBRARY = "library"          # 来自设备库
    MANUAL = "manual"            # 手动录入（未入设备库）
    IMPORTED = "imported"        # 批量导入


class QuoteItem(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "quote_items"

    quote_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("quotes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subsystem_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("subsystems.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    device_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 配件行指向它的主设备行（2026-10-05）。
    # 用来判断"这台防火墙配没配授权"，界面上也能把配件折叠挂在主设备下面。
    # 主设备行删除时配件行跟着删（CASCADE）—— 配件没有独立存在的意义。
    parent_item_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("quote_items.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    # 业务字段（脱库时可手动录入）
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    brand: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="台", nullable=False)
    quantity: Mapped[float] = mapped_column(Numeric(12, 3), default=0, nullable=False)

    # 价格
    unit_price: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    equipment_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    install_unit_price: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    install_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    cost_price: Mapped[float] = mapped_column(Numeric(12, 4), default=0, nullable=False)
    # 单位工程量的辅材费（2026-10-04）：辅材口径选「按工程量」时，
    # 辅材费 = Σ(数量 × 这一列)。桥架按米、网线按箱、摄像机按台。
    auxiliary_unit_price: Mapped[float] = mapped_column(
        Numeric(12, 4),
        default=0,
        nullable=False,
        # 与 0017 迁移里的列注释保持一致（否则 alembic check 报 modify_comment）
        comment="单位工程量辅材费（元/单位）；辅材口径选「按工程量」时按 Σ(数量×本列) 计",
    )
    is_owner_supplied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    source: Mapped[ItemSource] = mapped_column(
        sa_enum(ItemSource, "item_source"),
        default=ItemSource.LIBRARY,
        nullable=False,
    )

    # 替换/停产标记
    is_replaced: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    replaced_by_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("quote_items.id", ondelete="SET NULL"),
        nullable=True,
    )

    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 设备参数（2026-10-05）：加设备时从设备描述带过来，可手工改。
    # 报价单是对外文档 —— 快照一份，设备描述后来改了不会悄悄改写已发出的报价单。
    spec_text: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # 工艺系数（结构化字段）
    # 2022.7.13 智能化纪要：屏蔽线 / 超五类 / 吊顶内 三种工艺场景要单独计价。
    # 以前写死在 notes 里靠 "shielded" 之类的关键字匹配，容易被多写一个字就
    # 静默失效（见 compute_quote._compute_quota_total 的注释）；改成结构化
    # boolean 字段后，UI 是 checkbox、计算是真用 cat5e_coef / ceiling_coef。
    is_shielded: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_cat5e: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_ceiling: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 关系
    quote = relationship("Quote", back_populates="items")
    subsystem = relationship("Subsystem", back_populates="items")
    device = relationship("Device")

    # 下面两个是给 QuoteItemOut 用的只读投影。
    # 刻意走 __dict__.get() 而不是 self.device：这样即使调用方忘了 eager load，
    # 也只会得到 None，而不会在 async 上下文里触发懒加载抛 MissingGreenlet。
    @property
    def device_code(self) -> str | None:
        d = self.__dict__.get("device")
        return d.code if d is not None else None

    @property
    def device_status(self) -> str | None:
        d = self.__dict__.get("device")
        return str(d.status) if d is not None else None


__all__ = ["QuoteItem", "ItemSource"]
