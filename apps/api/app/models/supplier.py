"""Supplier model."""

from __future__ import annotations

from sqlalchemy import Boolean, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.models.device import Device


class Supplier(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "suppliers"

    # 业务编码（如 SUP-001），唯一。`name` 在 0001 迁移里也是 unique，
    # 现在两个 unique 共存：code 是新建表单的强约束（前端按编码检索），
    # name 保留是因为旧数据 / 导入场景还在按 name 唯一定位供应商。
    code: Mapped[str] = mapped_column(
        String(32), unique=True, index=True, nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    contact_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    contact_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    contact_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    address: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(String(64), nullable=True)
    notes: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Device.supplier 用了 back_populates="devices"，这个属性必须真实存在，
    # 否则第一次 configure_mappers() 就抛
    #   InvalidRequestError: Mapper[Supplier] has no property 'devices'
    # 结果是每一个走 ORM 的请求都 500、整个测试套全 error。
    # 注释里写「反向引用在 device.py 中」是不行的 —— 那只是单向关系。
    #
    # 这里刻意用**真实的运行时 import**（不是 TYPE_CHECKING + 字符串注解）：
    # device.py 不反向 import supplier，所以没有循环依赖；而
    # `Mapped[list["Device"]]` 这种写法在 from __future__ import annotations 下
    # 是一整条字符串注解，SQLAlchemy 求值时要能在模块命名空间里找到 Device，
    # 只在 TYPE_CHECKING 下导入会让它求值失败。显式 target 是第二道保险。
    devices: Mapped[list[Device]] = relationship(
        "Device",
        back_populates="supplier",
        passive_deletes=True,
    )


__all__ = ["Supplier"]
