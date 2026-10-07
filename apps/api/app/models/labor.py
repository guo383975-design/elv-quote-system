"""Labor rate + quota (定额) reference models."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import Boolean, Date, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import sa_enum


class LaborClass(StrEnum):
    A = "class_a"  # 一类人工
    B = "class_b"  # 二类人工
    C = "class_c"  # 三类人工


class LaborRate(UUIDPKMixin, TimestampMixin, Base):
    """人工市场信息价（宁波市）."""

    __tablename__ = "labor_rates"
    # M-10：以前没有唯一约束，(地区 + 工种 + 期次) 可以重复录入，
    # 而 is_current 是按"地区+工种"翻转的 —— 重复行会被静默折叠成一行，没人发现。
    __table_args__ = (
        UniqueConstraint(
            "region", "labor_class", "effective_date", name="uq_labor_rates_region_class_date"
        ),
    )

    region: Mapped[str] = mapped_column(String(32), default="ningbo", nullable=False, index=True)
    labor_class: Mapped[LaborClass] = mapped_column(
        sa_enum(LaborClass, "labor_class"),
        nullable=False,
        index=True,
    )
    effective_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    price_per_day: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    source: Mapped[str] = mapped_column(String(64), default="nbzj-info", nullable=False)
    is_current: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(256), nullable=True)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<LaborRate {self.region} {self.labor_class} ¥{self.price_per_day}/d>"


class QuotaCategory(StrEnum):
    """《浙江省通用安装工程预算定额（2018版）》第五册 建筑智能化工程 的章节。

    2026-10-06 按**真值**校正：原枚举把 5-1 记作「综合布线」、5-7 记作「管槽」，
    与定额书不符，而且缺 5-2 / 5-4 / 5-8 三章 —— 真值子目（如 5-2-13 双绞线缆
    管内穿放）在系统里无处安放，`category` 筛选也会给出错误分组。
    真值来源：广联达浙江定额库安装包内 `数据/子目索引.Index`（第五册 843 条
    子目的所属章），导入脚本见 `scripts/import_quota_zj2018.py`。
    """

    NETWORK = "5-1"        # 5-1 计算机、网络设备：网络打印机/KVM/交换机/防火墙
    CABLING = "5-2"        # 5-2 综合布线：机柜、线缆穿放、信息插座、配线架、桥架线槽
    BUILDING_AUTO = "5-3"  # 5-3 楼宇自控：中央管理系统、DDC、传感器
    BROADCAST_TV = "5-4"   # 5-4 广播与有线电视：前端射频设备、调制器
    AV = "5-5"             # 5-5 音视频与会议：传声器、功放、投影、显示屏
    SECURITY = "5-6"       # 5-6 安全防范：入侵探测器、摄像机、门禁
    SPD = "5-7"            # 5-7 电涌保护（防雷）
    HOME_AUTO = "5-8"      # 5-8 家居智能化：智能控制箱、可视对讲
    CLOCK = "5-9"          # 5-9 时钟系统（**2025 版新增**）：母钟、子钟、GPS 接收单元
    OTHER = "other"        # 第五册之外 / 待归类


class QuotaItem(UUIDPKMixin, TimestampMixin, Base):
    """定额子目（浙 2018 第五册）."""

    __tablename__ = "quota_items"

    code: Mapped[str] = mapped_column(String(32), unique=True, index=True, nullable=False)
    category: Mapped[QuotaCategory] = mapped_column(
        sa_enum(QuotaCategory, "quota_category"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    unit: Mapped[str] = mapped_column(String(16), default="100m", nullable=False)
    labor_a_day: Mapped[Decimal] = mapped_column(Numeric(10, 3), default=0, nullable=False)
    labor_b_day: Mapped[Decimal] = mapped_column(Numeric(10, 3), default=0, nullable=False)
    labor_c_day: Mapped[Decimal] = mapped_column(Numeric(10, 3), default=0, nullable=False)
    # 定额三费（2026-10-05 阶段 0 ✓）：供广联达模式显示「定额人工/辅材/机械」✓
    # 来源：《浙江省通用安装工程定额（2025版）》第五册扫描件视觉识读 ✓，
    # 闸门是 人工+材料+机械 = 基价（894/894 通过 ✓）。**不参与现有计算** ✗
    labor_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=0, server_default="0", nullable=False
    )
    material_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=0, server_default="0", nullable=False
    )
    machine_fee: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=0, server_default="0", nullable=False
    )
    base_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0, nullable=False)

    # 工艺系数默认（按 2022.7.13 纪要）
    shielded_coef: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=1.20, nullable=False)
    cat5e_coef: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=1.10, nullable=False)
    ceiling_coef: Mapped[Decimal] = mapped_column(Numeric(4, 2), default=1.20, nullable=False)

    notes: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    def __repr__(self) -> str:  # pragma: no cover
        return f"<QuotaItem {self.code} {self.name}>"


__all__ = ["LaborRate", "QuotaItem", "LaborClass", "QuotaCategory"]


