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


__all__ = [
    "QuoteAuditLog", "ProjectAuditLog", "LoginAuditLog",
    "DeviceAuditLog", "DeviceAuditEntity", "DeviceAuditAction",
    "AuditAction",
]


class DeviceAuditEntity(StrEnum):
    """设备库留痕的对象类型（设备 / 分类节点）。"""

    DEVICE = "device"
    CATEGORY = "category"


class DeviceAuditAction(StrEnum):
    """设备库动作词表。

    前三个**刻意沿用既有留痕的取值** ✓（`create` / `update` / `delete` ✓，
    与 `AuditAction` 逐字一致 ✓）：这样 `GET /audit-logs/recent?action=delete`
    能把"谁把哪台设备停用了"和"谁删了哪张单"一起筛出来 ✓，
    不必让调用方记住两套词表。

    后三个是设备库独有的（分类节点增删改）✓ —— 别处没有对应动作，
    就**新造名字**而不是硬塞进 create/update/delete ✗（塞进去以后
    按 action=delete 筛选会同时捞出"删分类"和"删设备"，两个都不是一回事 ✗）。

    ⚠ 存的是 `String(32)` 而**不是** PG 枚举 ✗（与 `SettingAuditLog.action` 同一做法 ✓）：
    `audit_action` 那个 PG 枚举是给报价单/项目留痕共用的 ✓，
    给它加值要 `ALTER TYPE` ✗，而 PG 的 `ALTER TYPE ... ADD VALUE`
    不能在事务块里用（alembic 默认就在事务里）✗ —— 为几个设备库动作
    去动一张五处在用的共享枚举，风险远大于收益 ✗。
    """

    CREATE = "create"                      # 新增设备
    UPDATE = "update"                      # 改设备（含重新启用：is_active false→true）
    DELETE = "delete"                      # 停用设备（DELETE /devices/{id}，软删）
    CATEGORY_CREATE = "category_create"
    CATEGORY_UPDATE = "category_update"
    CATEGORY_DELETE = "category_delete"


class DeviceAuditLog(UUIDPKMixin, Base):
    """设备库操作留痕（2026-10-09 补）。

    问题现场：报价单 / 项目 / 登录 / 设置都有留痕 ✓，**设备库没有** ✗ ——
    改一台设备的进价、把型号标成停产、停用一台设备，事后都答不上
    "是谁、什么时候改的、改前是什么" ✗（设备价格直接决定报价的成本与毛利 ✓）。

    与 `ProjectAuditLog` 同构（谁 / 何时 / 从哪 / 做了什么 / 带了什么 ✓），
    但有两点**必须**跟着 `FactoryResetLog` 的做法（见该模块 docstring ✓）：

      · `operator_id` / `entity_id` **都不加外键** ✗ ——
        `factory_reset` 的 `scope=all` 会清空 `devices` / `device_categories` /
        `users`（见 services/factory_reset.py 的 BUSINESS_TABLES / ACCOUNT_TABLES ✓），
        加了 FK（哪怕是 SET NULL）就是给"留痕被级联带走"留后门 ✗。
        对象的代号与名称另存**字符串快照** ✓（`subject_code` / `subject_name` ✓），
        对象删了、账号没了，照样读得出"是谁动了哪台设备" ✓。
      · 本表**不在** `factory_reset` 的任何清理清单里 ✓（那是显式白名单 ✓），
        所以 `scope=all` 也删不掉它 ✓ —— 留痕的全部价值就在"出事之后它还在" ✓。

    不继承 `TimestampMixin`：一次操作一行、只插不改 ✓（与其它留痕表一致 ✓）。
    """

    __tablename__ = "device_audit_logs"

    # device / category（见 DeviceAuditEntity）
    entity: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # 对象 id。**刻意不加 ForeignKey**（见类 docstring）
    entity_id: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    # 对象快照：设备编号 / 分类代号 + 名称。对象删了也要看得出"是哪一台"
    subject_code: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    subject_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # 谁（**不加 FK**）+ 用户名快照（账号被删也看得出是谁）
    operator_id: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    operator_username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    action: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 一句话摘要（列表里直接显示，不用展开 changes）
    summary: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # 关键变更：[{field, from, to}]（与 SettingAuditLog 的 diff 同构 ✓）
    changes: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )

    # 谁、从哪来的（与其它留痕表一致：没有 IP 的留痕在内网里等于没留）
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(256), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        index=True,
    )


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
