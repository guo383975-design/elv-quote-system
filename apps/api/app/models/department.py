"""Department model: 组织架构树（用户归属的部门）。

为什么要有这张表：`users.department` 原来是一个**自由文本**字段（String(64)），
于是"设计部" / "设计" / "设计中心" 会被当成三个不同的部门 —— 既没法按部门
可靠筛选，也没法表达"部门下面还有组"（比如 设计部 → 智能化设计组）。
改成树之后：

  · 用户挂在树的某个节点上（`users.department_id`）；
  · 旧的自由文本字段**保留**，但由节点反推（= 节点名），保证任何还在读它的
    地方（报表、导出、旧脚本）看到的是同一个写法，不出现两套真相。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType


class Department(UUIDPKMixin, TimestampMixin, Base):
    """组织架构节点（自引用，层级不限）。"""

    __tablename__ = "departments"

    parent_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        # 删父节点要先把子节点处理掉：SET NULL 只是兜底，
        # 路由层会先拒绝"还有子节点 / 还有用户"的删除（见 api/v1/users.py）。
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    notes: Mapped[str | None] = mapped_column(String(255), nullable=True)

    children: Mapped[list[Department]] = relationship(  # noqa: F821
        "Department",
        back_populates="parent",
        cascade="all, delete-orphan",
        order_by="Department.sort_order",
    )
    parent: Mapped[Department | None] = relationship(  # noqa: F821
        "Department", back_populates="children", remote_side="Department.id"
    )
    users: Mapped[list[User]] = relationship(  # noqa: F821
        "User", back_populates="department_node"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Department {self.code} {self.name}>"


__all__ = ["Department"]
