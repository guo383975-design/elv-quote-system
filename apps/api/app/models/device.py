"""Device library model: 企业标准设备库."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType, sa_enum


class DeviceCategory(StrEnum):
    """一级分类（历史字段，兼容保留）。

    ⚠ 设备库的分类从 2026-09-30 起改用**分类树**（`DeviceCategoryNode` /
    `devices.category_id`）：一个设备挂在树的某个节点上，节点可以是任意层级。
    这个枚举仍然保留，作用是：

      1. 一级分类的**稳定代号**（`legacy_enum`），存量数据、过滤器、编号前缀都还在用它；
      2. 树上一个一级节点的 `legacy_enum` 指向它，写设备时由节点反推这个字段，
         保证「枚举 = 所在一级分类」不会两边打架（不允许出现两个真相）。
    """

    VIDEO = "video"                    # 视频监控
    ACCESS = "access"                  # 门禁一卡通
    INTRUSION = "intrusion"            # 入侵报警
    CABLING = "cabling"                # 综合布线
    NETWORK = "network"                # 计算机网络
    BROADCAST = "broadcast"            # 公共广播
    CONFERENCE = "conference"          # 会议系统
    SERVER_ROOM = "server_room"        # 机房
    PARKING = "parking"                # 停车管理
    INFO_PUBLISH = "info_publish"      # 信息发布
    POWER = "power"                    # 电源/线缆
    ACCESSORY = "accessory"            # 辅材


class DeviceStatus(StrEnum):
    ON_SALE = "on_sale"           # 在售
    NEAR_EOL = "near_eol"         # 临近停产
    EOL = "eol"                   # 已停产
    DISCONTINUED = "discontinued"  # 退市


class DeviceCategoryNode(UUIDPKMixin, TimestampMixin, Base):
    """设备库分类**树**的一个节点（自引用，层级不限）。

    为什么要有这张表（而不是继续用一个扁平枚举）：
      枚举只有一层，"摄像机 / 录像机 / 光模块 / 桥架 …" 全挤在 12 个桶里，
      到了几百上千条设备就变成"都写在一起"。分类树让每个设备落到自己那一格，
      并且**由使用方自己维护**（改名/挪位/加子类都不用改代码和迁移）。

    与旧枚举的关系：
      - 一级节点带 `legacy_enum`（= DeviceCategory 的取值），`devices.category`
        由它回填，保证枚举与树一致；
      - `code_prefix` 决定设备自动编号的前缀（V/A/I/…），原来写死在 devices.py 的
        字典里，现在跟着节点走（子节点留空则继承父节点）。
    """

    __tablename__ = "device_categories"

    parent_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        # 删父节点必须先把子节点处理掉：这里用 SET NULL 只是兜底，
        # 路由层会先拒绝"还有子节点/还有设备"的删除（见 api/v1/devices.py）。
        ForeignKey("device_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # 一级分类的旧枚举值（video/access/…）；子节点为 NULL
    legacy_enum: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # 设备编号前缀（V/A/I/…）；子节点为空表示继承父节点
    code_prefix: Mapped[str | None] = mapped_column(String(4), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    children: Mapped[list[DeviceCategoryNode]] = relationship(  # noqa: F821
        "DeviceCategoryNode",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="DeviceCategoryNode.sort_order",
    )
    parent: Mapped[DeviceCategoryNode | None] = relationship(  # noqa: F821
        "DeviceCategoryNode", back_populates="children", remote_side="DeviceCategoryNode.id"
    )
    devices: Mapped[list[Device]] = relationship(  # noqa: F821
        "Device", back_populates="category_node"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<DeviceCategoryNode {self.code} {self.name}>"


class DevicePriceTier(UUIDPKMixin, TimestampMixin, Base):
    """设备阶梯价：`[min_qty, max_qty]` 区间内的单价（max_qty 为空 = 该档以上）。

    唯一约束 (device_id, min_qty)：同一设备不允许两个"从 10 台起"的档位 ——
    否则取档结果取决于数据库返回顺序，报价会飘。
    """

    __tablename__ = "device_price_tiers"
    __table_args__ = (
        UniqueConstraint("device_id", "min_qty", name="uq_device_price_tiers_device_min"),
    )

    device_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    min_qty: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    # 空 = 无上限（"50 台以上"这一档）
    max_qty: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    note: Mapped[str | None] = mapped_column(String(128), nullable=True)

    device: Mapped[Device] = relationship(back_populates="price_tiers")


class Device(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "devices"

    # 业务编码（自动生成，如 V1010/A5001）
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    brand: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    spec: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 设备描述 / 性能参数（2026-10-05）：多行文本，写"吞吐 10Gbps；并发 200 万；
    # 8×GE+4×SFP+"这类基础信息。选产品时悬浮显示，加进报价单后成为"设备参数"。
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    unit: Mapped[str] = mapped_column(String(16), default="台", nullable=False)
    # 单位工程量辅材费（2026-10-04）：随设备带进报价单，供「按工程量」口径用
    auxiliary_unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 4),
        default=0,
        nullable=False,
        # 与 0017 迁移里的列注释保持一致（否则 alembic check 报 modify_comment）
        comment="单位工程量辅材费（元/单位）；辅材口径选「按工程量」时按 Σ(数量×本列) 计",
    )
    # 价格底线（2026-10-04）：低于它报价需要经理授权 + 理由（见 services/pricing.py）。
    # 可空 = 没设底线，不做拦截。
    min_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    # 阶梯价：按数量分档（1-9 / 10-49 / 50+）。加明细行时按数量自动取档。
    price_tiers: Mapped[list[DevicePriceTier]] = relationship(
        back_populates="device",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="DevicePriceTier.min_qty",
    )

    category: Mapped[DeviceCategory] = mapped_column(
        sa_enum(DeviceCategory, "device_category"),
        nullable=False,
        index=True,
    )
    # 分类树节点（2026-09-30 起）：设备的**实际归属**。
    # 允许为空：迁移会把存量设备回填到对应的一级节点，但手工跑 SQL 插入的行
    # 可能没有；界面上显示为「未分类」，不会因此丢数据。
    category_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("device_categories.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subsystem_default: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 价格
    reference_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    cost_price: Mapped[float] = mapped_column(Numeric(12, 2), default=0, nullable=False)
    install_unit_price: Mapped[float] = mapped_column(
        Numeric(12, 2), default=0, nullable=False,
        comment="点位单价（元/点）；0 表示无安装费",
    )

    # 状态
    status: Mapped[DeviceStatus] = mapped_column(
        sa_enum(DeviceStatus, "device_status"),
        default=DeviceStatus.ON_SALE,
        nullable=False,
        index=True,
    )
    is_owner_supplied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_common: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # 停产/替代
    replacement_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
    )
    eol_at: Mapped[date | None] = mapped_column(Date, nullable=True)

    # 采购约束
    moq: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    warranty_years: Mapped[int] = mapped_column(Integer, default=2, nullable=False)

    # 供应商（默认）
    supplier_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("suppliers.id", ondelete="SET NULL"),
        nullable=True,
    )

    # 最后询价
    last_inquiry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_inquiry_price: Mapped[float | None] = mapped_column(Numeric(12, 2), nullable=True)

    # 关系
    replacement = relationship("Device", remote_side="Device.id", foreign_keys=[replacement_id])
    supplier = relationship("Supplier", back_populates="devices")
    category_node = relationship("DeviceCategoryNode", back_populates="devices")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Device {self.code} {self.name}>"


__all__ = [
    "Device",
    "DeviceCategory",
    "DeviceCategoryNode",
    "DevicePriceTier",
    "DeviceStatus",
]



# ---------------------------------------------------------------------------
# 配件 / 授权（2026-10-05）
# ---------------------------------------------------------------------------
# 场景：买一台防火墙要买授权，而授权有不同档（基础/高级/三年/五年…）。
# 所以这里分两层：**槽**（要配哪一类东西）+ **候选**（这一类里能选哪些）。
# 配件本身也是产品库里的设备条目 —— 这样授权有自己的编码与价格，
# 能单独报价、算成本毛利，也能走采购，而不是一段拍脑袋的说明文字。
class DeviceAccessoryGroup(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "device_accessory_groups"

    device_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 槽名（界面上显示）：授权 / 电源模块 / 光模块 …
    label: Mapped[str] = mapped_column(String(64), nullable=False)
    # 必配：配单时没选就要提示（软提醒，不硬拦 —— 有的单子确实不配）
    is_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # one=单选（如授权档位）/ many=多选（如多个光模块）
    select_mode: Mapped[str] = mapped_column(String(16), default="one", nullable=False)
    # per_unit=数量随主设备 / fixed=固定数量（如整套一张授权）
    quantity_mode: Mapped[str] = mapped_column(
        String(16), default="per_unit", nullable=False
    )
    default_quantity: Mapped[Decimal] = mapped_column(
        Numeric(12, 3), default=1, nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    options: Mapped[list[DeviceAccessoryOption]] = relationship(
        back_populates="group",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="DeviceAccessoryOption.sort_order",
    )


class DeviceAccessoryOption(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "device_accessory_options"

    group_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("device_accessory_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 候选配件：指向产品库里的另一条设备（授权也是设备）
    accessory_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    group: Mapped[DeviceAccessoryGroup] = relationship(back_populates="options")

    __table_args__ = (
        # 同一个槽里同一个配件只应出现一次（重复添加没有意义，只会让界面变乱）
        UniqueConstraint("group_id", "accessory_id", name="uq_device_accessory_option"),
    )
