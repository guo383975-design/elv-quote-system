"""系统设置：键值型配置表（2026-10-04）。

原型顶栏的「系统设置」一直是置灰占位。这里只做**会影响输出**的配置 ——
那些"看起来能配、其实没人读"的开关不做：

  · 公司信息（抬头/地址/电话/邮箱）→ 客户版 PDF 导出与预览页的抬头
  · 报价有效期天数            → PDF / 预览页的「有效期至」
  · 报价说明条款              → PDF 与预览页（此前是代码里的常量）

存 JSONB：一批设置项一起改、读的时候一次取全，比一列一个键好扩展。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType


class AppSetting(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_by: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True)
