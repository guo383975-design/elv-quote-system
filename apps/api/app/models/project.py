"""Project model: a customer engagement producing one or more quotes."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Date, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType, sa_enum


class ProjectType(StrEnum):
    COMMERCIAL = "commercial"        # 商业楼宇
    PUBLIC = "public"                # 政府/学校/医院
    RESIDENTIAL = "residential"      # 园区/小区
    DATA_CENTER = "data_center"      # 数据中心/机房
    OTHER = "other"


class ProjectStatus(StrEnum):
    DRAFT = "draft"
    QUOTING = "quoting"
    QUOTED = "quoted"
    WON = "won"
    LOST = "lost"
    ARCHIVED = "archived"


class Project(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    # 业务字段
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # 客户主数据引用（可空）：老数据/一次性客户仍然只填下面的文本字段
    customer_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("customers.id", ondelete="SET NULL"), nullable=True, index=True
    )
    customer_name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    customer_contact: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    project_type: Mapped[ProjectType] = mapped_column(
        sa_enum(ProjectType, "project_type"),
        default=ProjectType.COMMERCIAL,
        nullable=False,
        index=True,
    )
    status: Mapped[ProjectStatus] = mapped_column(
        sa_enum(ProjectStatus, "project_status"),
        default=ProjectStatus.DRAFT,
        nullable=False,
        index=True,
    )
    expected_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_duration_days: Mapped[int | None] = mapped_column(nullable=True)
    total_area_sqm: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # 项目级费率配置（覆盖全局默认）
    auxiliary_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.06, nullable=False)
    design_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.02, nullable=False)
    warranty_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.03, nullable=False)
    training_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    mgmt_profit_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.08, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.09, nullable=False)
    auxiliary_base: Mapped[str] = mapped_column(
        # ⚠ 默认值必须与 Quote 的 `AuxiliaryBaseType.DEVICE` 一致（2026-10-03 修正）。
        # 原来写的是 "device_install"，而报价单那边是 "device" —— 两张表的
        # server_default 从 0001 起就不一致。以前没人看得出来，因为这列既没有
        # API 也没有界面入口；一旦开始"建报价单时抄项目费率"（H-12），
        # 每张新建的报价单都会从 device 变成 device_install，**辅材费基数被静默改掉**。
        # 已确认口径是"按设备金额"（README「已确认的业务口径」第 3 项）。
        String(32), default="device", nullable=False
    )
    fee_base: Mapped[str] = mapped_column(String(32), default="device_aux_install", nullable=False)

    # 关系
    owner_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    customer: Mapped[Customer | None] = relationship(  # noqa: F821
        back_populates="projects"
    )
    subsystems: Mapped[list[Subsystem]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    quotes: Mapped[list[Quote]] = relationship(  # noqa: F821
        back_populates="project", cascade="all, delete-orphan", lazy="selectin"
    )
    # 项目操作留痕（状态变更等）。不留 lazy="selectin"：留痕表会随时间变长，
    # 列表接口不该顺手把每一单的全部留痕都拉出来。
    # 同样不带 delete 级联（2026-10-09）：删项目必须留下"谁删的"这条痕，
    # 见 api/v1/projects.py 的 delete_project 与 models/quote.py 的同名注释。
    audit_logs: Mapped[list[ProjectAuditLog]] = relationship(  # noqa: F821
        back_populates="project", cascade="save-update, merge"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Project {self.code} {self.name}>"


__all__ = ["Project", "ProjectType", "ProjectStatus"]
