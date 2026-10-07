"""Quote audit log: 报价单操作留痕."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, UUIDPKMixin
from app.db.types import JSONType, UUIDType, sa_enum


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class AuditAction(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    COMPUTE = "compute"          # 重算
    SUBMIT = "submit"            # 提交审批
    APPROVE = "approve"
    REJECT = "reject"
    EXPORT = "export"
    IMPORT = "import"
    REPLACE_DEVICE = "replace_device"
    COST_VIEW = "cost_view"
    # 低于设备底价放行（2026-10-04）：必须记下"谁批的、什么价、什么理由"
    PRICE_FLOOR_OVERRIDE = "price_floor_override"      # 查看成本/毛利（H-10：这类"看"也要留痕）


class QuoteAuditLog(UUIDPKMixin, Base):
    __tablename__ = "quote_audit_logs"

    quote_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        # H-10：删单不再销毁留痕 —— SET NULL + 可空，配合 subject_code 快照
        ForeignKey("quotes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # 单号快照：报价单被删后，留痕仍看得出"是哪张单"
    subject_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 谁、从哪来的（H-10：原来完全没有 IP/UA）
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        sa_enum(AuditAction, "audit_action"),
        nullable=False,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 只建 quote_id / action 两个索引（与迁移一致）；本表不继承 TimestampMixin，
    # 因为留痕是一次性插入、永不更新，不需要 updated_at。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    quote = relationship("Quote", back_populates="audit_logs")


class ProjectAuditLog(UUIDPKMixin, Base):
    """项目操作留痕（2026-10-02 补）。

    报价单有 `quote_audit_logs`，项目状态却一直是裸 setattr、改完不留痕 ——
    「谁把这一单标成已中标的」答不上来。补一张同构的表，读接口同样按
    `audit_log.read`（管理员 · 财务）放行。
    """

    __tablename__ = "project_audit_logs"

    project_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        # 同理：删项目也保留留痕
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    subject_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    action: Mapped[AuditAction] = mapped_column(
        sa_enum(AuditAction, "audit_action"),
        nullable=False,
        index=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    note: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )

    project = relationship("Project", back_populates="audit_logs")


__all__ = ["QuoteAuditLog", "ProjectAuditLog", "LoginAuditLog", "AuditAction"]


class LoginAuditLog(UUIDPKMixin, Base):
    """登录尝试留痕（H-10）。

    两个用途：
      1. **审计**：谁在什么时候、从哪个 IP 尝试登录、成功还是失败（原因）；
      2. **登录失败锁定**：按 username + IP 统计最近窗口内的失败次数（见 api/v1/auth.py）。

    不继承 TimestampMixin：一次尝试一行、只插不改。
    """

    __tablename__ = "login_audit_logs"

    username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    success: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    reason: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False, index=True
    )


class SettingAuditAction(StrEnum):
    """设置类变更的动作（2026-10-05）。"""

    SETTINGS_UPDATE = "settings_update"   # 保存系统设置
    CONFIG_IMPORT = "config_import"       # 导入配置档
    AI_CONFIG = "ai_config"               # 改 AI 配置（含 Key 重设，但不记 Key）


class SettingAuditLog(UUIDPKMixin, Base):
    """设置/权限变更留痕。

    字段与其它留痕表一致（谁、什么时候、从哪、做了什么、带了什么），
    另外把"差异"整份存进 `payload`：事后能看出**具体改了什么**，
    而不只是"某人保存过设置"。
    """

    __tablename__ = "setting_audit_logs"

    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType, nullable=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 一句话摘要（列表里直接显示，不用展开 payload）
    summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 差异明细：[{group, changes:[{field, from, to}]}]
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, nullable=False, default=dict
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"), index=True,
    )
