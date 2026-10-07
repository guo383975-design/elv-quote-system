"""Quote model: 报价单 (a project can have multiple quote revisions)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import JSONType, UUIDType, sa_enum


class QuoteStatus(StrEnum):
    DRAFT = "draft"
    REVIEWING = "reviewing"
    SUBMITTED = "submitted"
    APPROVED = "approved"
    REJECTED = "rejected"
    ARCHIVED = "archived"


class FeeBaseType(StrEnum):
    """附加费率基数选择."""

    DEVICE_ONLY = "device_only"                  # 仅设备费
    DEVICE_AUX_INSTALL = "device_aux_install"    # 设备+辅材+安装（默认）
    DIRECT_FEE = "direct_fee"                    # 直接费（=设备+辅材+安装+其他）


class AuxiliaryBaseType(StrEnum):
    """辅材费基数."""

    DEVICE = "device"                # 设备费
    DEVICE_INSTALL = "device_install"  # 设备费+安装费
    BY_QUANTITY = "by_quantity"      # 按工程量（桥架按米、网线按箱）


class Quote(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "quotes"

    project_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[QuoteStatus] = mapped_column(
        sa_enum(QuoteStatus, "quote_status"),
        default=QuoteStatus.DRAFT,
        nullable=False,
        index=True,
    )

    # 客户信息
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_contact: Mapped[str | None] = mapped_column(String(64), nullable=True)
    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)

    # 计税方式
    tax_method: Mapped[str] = mapped_column(String(32), default="general_9pct", nullable=False)
    tax_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.09, nullable=False)

    # 附加费率（项目级，可覆盖全局）
    fee_base: Mapped[FeeBaseType] = mapped_column(
        sa_enum(FeeBaseType, "fee_base"),
        default=FeeBaseType.DEVICE_AUX_INSTALL,
        nullable=False,
    )
    # 安装单价方式（2026-10-05 用户设计 ✓）：
    #   manual = 手动填安装单价（界面不显示定额列 ✓）
    #   quota  = 套定额模式（套定额后基价自动填进安装单价 ✓，
    #            此时不再叠加定额人工费 —— 基价里已含人工 ✓）
    install_mode: Mapped[str] = mapped_column(
        String(16), default="manual", server_default="manual", nullable=False
    )
    auxiliary_base: Mapped[AuxiliaryBaseType] = mapped_column(
        sa_enum(AuxiliaryBaseType, "auxiliary_base"),
        default=AuxiliaryBaseType.DEVICE,
        nullable=False,
    )
    auxiliary_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.06, nullable=False)
    design_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.02, nullable=False)
    warranty_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.03, nullable=False)
    training_fee: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    mgmt_profit_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0.08, nullable=False)

    # 人工费浮动
    labor_float_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0, nullable=False)
    labor_float_per_class: Mapped[dict[str, Any]] = mapped_column(
        JSONType,
        default=lambda: {"class_a": 0, "class_b": 0, "class_c": 0},
        nullable=False,
        # 与 0001 迁移里的 server_default **逐字一致**（`\:` 是 SQLAlchemy text()
        # 对冒号的转义写法）。补这一行是为了消除模型↔迁移漂移（设计方案 §15.8 #13）：
        # 以前只有迁移里有，`alembic revision --autogenerate` 每次都以为要删掉它。
        server_default=text("'{\"class_a\": 0, \"class_b\": 0, \"class_c\": 0}'"),
    )

    # 单据级条款覆盖（2026-10-05）：NULL = 用系统设置里那份；
    # 非空 = 这一张单用自己那几条（不污染其它报价单）。
    clauses_override: Mapped[list[Any] | None] = mapped_column(JSONType, nullable=True)
    # 单据级有效期覆盖（天）：NULL = 用系统设置
    valid_days_override: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # 增项来源：本单是从哪张报价单增项而来（2026-10-05 用户要求）
    based_on_quote_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("quotes.id", ondelete="SET NULL"), nullable=True
    )

    # 每单条款数字覆盖（2026-10-05）：6 个变量的本单取值 ✓
    #   {valid_days, prepay_ratio, accept_ratio, warranty_ratio,
    #    warranty_months, build_days} ✓
    #   为空 = 全部继承「系统设置」里的默认 ✓（行为与以前完全一致 ✓）
    #   渲染时：本单优先 → 全局兜底 ✓（services/quote_terms.build_variables ✓）
    terms_override: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    # 批准时间（2026-10-04）：看板按它取"最新一版"与月度趋势。
    # 应用层写 datetime.now(UTC)（微秒精度）；靠 created_at 推断在秒级库里分不出先后。
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # 计算结果：冗余存储（列表页可直接排序筛选，不必每次重算）。
    # 由应用层写入，见 app/services/quote_compute.py 的 write_breakdown_to_quote()。
    # 库里**没有**任何触发器 —— 别再写成「触发器更新」，那是错的。
    equipment_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    install_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    auxiliary_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    design_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    warranty_fee: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    subtotal_before_tax: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    mgmt_profit: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    total_with_tax: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    # 定额计价口径
    quota_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    quota_total_with_tax: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)

    # 成本（对内视图）
    cost_total: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    gross_profit: Mapped[float] = mapped_column(Numeric(14, 2), default=0, nullable=False)
    gross_profit_rate: Mapped[float] = mapped_column(Numeric(5, 4), default=0, nullable=False)

    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # 关系
    project = relationship("Project", back_populates="quotes")
    items: Mapped[list[QuoteItem]] = relationship(  # noqa: F821
        back_populates="quote", cascade="all, delete-orphan", lazy="selectin",
        # 明细行**稳定排序**（2026-10-05）：
        #   以前这里没有 order_by ✗ → 顺序由数据库的返回顺序决定 ✓，
        #   而**改过某一行**（填安装单价 / 套定额都会 UPDATE ✓）之后，
        #   PostgreSQL 可能把该行挪到别的物理位置 ✓ → 界面上看起来"顺序变了" ✗。
        #   现在固定按 sort_order → created_at → id ✓（created_at 都已写入 ✓）
        order_by="QuoteItem.sort_order, QuoteItem.created_at, QuoteItem.id",
    )
    audit_logs: Mapped[list[QuoteAuditLog]] = relationship(  # noqa: F821
        back_populates="quote", cascade="all, delete-orphan", lazy="selectin"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Quote {self.code} v{self.version} {self.total_with_tax}>"


__all__ = [
    "Quote",
    "QuoteStatus",
    "FeeBaseType",
    "AuxiliaryBaseType",
]


