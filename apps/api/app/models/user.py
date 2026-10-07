"""User + auth model."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.permissions import Role
from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import UUIDType, sa_enum


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    PENDING = "pending"  # 邀请后未激活


class User(UUIDPKMixin, TimestampMixin, Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    department: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 组织架构树上的实际归属；department 那个自由文本字段由它反推（= 节点名），
    # 避免"设计部 / 设计 / 设计中心"在报表里被当成三个部门。
    department_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 2026-10-05：枚举 → varchar(32)，以支持自定义角色
    # （PG 原生枚举不能删成员/改名，不适合做可增删的角色）
    role: Mapped[str] = mapped_column(
        String(32), nullable=False, default=str(Role.SALES), index=True
    )
    status: Mapped[UserStatus] = mapped_column(
        sa_enum(UserStatus, "user_status"),
        default=UserStatus.ACTIVE,
        nullable=False,
    )
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # 会话版本：强制下线/改密码时 +1，已发出的令牌立即失效（H-8）
    token_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # 是否必须先改口令才能继续使用（2026-10-05 ✓）：
    #   初始/重置口令（尤其 seed 出来的 admin）置 true ✓ → 前端弹阻塞对话框 ✓。
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # 归属部门（树节点）。注意 relationship 名字不能叫 department ——
    # 上面那个同名的自由文本列会与之冲突。
    department_node = relationship("Department", back_populates="users")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<User {self.username} role={self.role}>"


__all__ = ["User", "UserStatus"]



