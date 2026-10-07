"""Subsystem model: per-project subsystem row (视频监控/门禁/...)."""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType, sa_enum


class SubsystemType(StrEnum):
    # 2026-10-04 修正拼写：成员名原来叫 VIDEO_SURVEIT（截断且尾部错成 T）。
    # 只改成员名 —— **值一直是 video_surveillance**，所以历史数据不需要迁移。
    VIDEO_SURVEILLANCE = "video_surveillance"   # 视频监控
    ACCESS_CONTROL = "access_control"       # 门禁一卡通
    INTRUSION = "intrusion"                # 入侵报警
    STRUCTURED_CABLING = "structured_cabling"  # 综合布线
    NETWORK = "network"                    # 计算机网络
    BROADCAST = "broadcast"                # 公共广播
    CONFERENCE = "conference"              # 会议系统
    SERVER_ROOM = "server_room"            # 机房工程
    PARKING = "parking"                    # 停车管理
    INFO_PUBLISH = "info_publish"          # 信息发布
    OTHER = "other"


class Subsystem(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "subsystems"
    __table_args__ = (
        # unique (project_id, subsystem_type) — one of each per project
    )

    project_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    subsystem_type: Mapped[SubsystemType] = mapped_column(
        sa_enum(SubsystemType, "subsystem_type"),
        nullable=False,
        index=True,
    )
    custom_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    point_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    equipment_subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    install_subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    auxiliary_subtotal: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # 供货商：可选 FK 到 suppliers.id。ON DELETE SET NULL（不是 CASCADE）：
    # 把供应商软删/硬删后，子系统的供货商挂载关系应当解除，但子系统本身要保留。
    # 索引让「按供货商聚合」查询能走索引。
    supplier_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    project = relationship("Project", back_populates="subsystems")
    # 挂着的供货商：use 软关 suppliers.is_active=False 时拉不回来，但 FK 仍在。
    supplier = relationship("Supplier", passive_deletes=True)
    # 注意：QuoteItem 的所有者是 Quote，不是 Subsystem。
    # 这里绝不能写 cascade="all, delete-orphan" —— 否则删除一个子系统会把
    # 挂在它下面的报价明细行一并删掉（造成报价数据静默丢失）。
    # 正确语义是 FK 上的 ondelete="SET NULL"：行保留，只解除归属。
    items: Mapped[list[QuoteItem]] = relationship(  # noqa: F821
        back_populates="subsystem",
        passive_deletes=True,
    )


__all__ = ["Subsystem", "SubsystemType"]
