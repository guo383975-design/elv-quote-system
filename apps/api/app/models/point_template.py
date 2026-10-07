"""点位模板（point template）：一个点位由哪些设备构成 + 该点位的安装单价。

原型里的出处有三处，这套模型要同时满足：
  · 顶栏导航「点位模板」页（模板的增删改查）
  · 报价编制页的「套用点位模板」「批量添加点位」
  · 添加设备弹窗第二个页签「按点位模板批量生成」
  · 报价明细表的提示「安装单价按【点位模板】自动带出（摄像机 80 元/点、球机 180 元/点…）」

一个模板 = 一个「点位」的物料构成，例如：

    球机点位模板（视频监控）  安装单价 180 元/点
      · 网络高清球型摄像机 × 1   ← 主设备（is_primary）
      · 壁装支架           × 1
      · 电源适配器         × 1

套用到报价单时：点位数 N → 每行数量 = 模板数量 × N；
**安装单价只计一次/点**，落在主设备行上（其余行安装费为 0），
否则一个点位里 3 个组件会各收一次"安装 180 元/点"，安装费翻 3 倍。
主设备没标时退化为按 sort_order 的第一行，并在接口返回里说明。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType, sa_enum
from app.models.device import Device
from app.models.subsystem import SubsystemType


class PointTemplate(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "point_templates"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    # 默认归属哪个子系统；套用时可以覆盖（同一个模板可能用在别的专业里）
    subsystem_type: Mapped[SubsystemType | None] = mapped_column(
        sa_enum(SubsystemType, "subsystem_type"), nullable=True
    )
    # 该点位的安装单价（元/点）—— 套用时写进主设备行的 install_unit_price
    install_unit_price: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=0, nullable=False
    )
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    items: Mapped[list[PointTemplateItem]] = relationship(
        "PointTemplateItem",
        back_populates="template",
        cascade="all, delete-orphan",
        order_by="PointTemplateItem.sort_order",
    )


class PointTemplateItem(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "point_template_items"

    template_id: Mapped[UUID] = mapped_column(
        UUIDType,
        ForeignKey("point_templates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 设备被停用/停产后模板行仍在（否则历史模板会莫名其妙缺行）；
    # 真删设备时才 SET NULL，套用时会跳过并报告缺设备的行。
    device_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("devices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 一个点位里用几个（如 1 个球机、2 个支架）
    quantity: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=1, nullable=False)
    # 主设备：该点位的"安装单价（元/点）"只落在这一行上
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(512), nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    template: Mapped[PointTemplate] = relationship("PointTemplate", back_populates="items")
    device: Mapped[Device | None] = relationship("Device")

