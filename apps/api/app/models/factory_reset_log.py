"""恢复出厂设置留痕（2026-10-08 事故后新增）。

## 为什么要单独一张表，而且必须**排除在清理范围之外**

2026-10-08 生产库的 `projects` / `quotes` / `quote_items` 全被清空，而
`quote_audit_logs` / `project_audit_logs` / `login_audit_logs` **也一起没了** ——
因为 `services/factory_reset.py` 的清理范围里就包含着三张留痕表。
于是「谁在什么时候点的、清了什么」查无对证，只能靠备份文件的时间戳反推。

留痕的全部价值就在「清库之后它还在」，所以本表：

  · **不在** `BUSINESS_TABLES` / `SETTINGS_TABLES` / `ACCOUNT_TABLES` 任何清单里
    （见 `services/factory_reset.py`），`scope=all` 也删不到它；
    `tests/test_factory_reset.py` 有一条用例专门守住这件事；
  · 与删除**同一个事务**写入，所以「删了但没记」不可能发生；
  · 同一时刻还会写一份 JSON 到备份目录旁 + 一条 WARNING 到 journal，
    三处互为备份：库连不上看日志，日志滚掉了看 JSON，都还在就查表。

`operator_id` 刻意**不加外键**：`scope=all` 会删掉除当前账号外的所有用户，
而留痕必须活下来 —— 加 FK 会带来「删用户导致留痕被置空/连带删除」的麻烦。
用户名另存一份字符串快照，账号本身没了也照样能读出「是谁」。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, UUIDPKMixin
from app.db.types import JSONType, UUIDType

# 状态机：started（已开始）→ completed / completed_with_errors（结束）
STATUS_STARTED = "started"
STATUS_DRY_RUN = "dry_run"
STATUS_COMPLETED = "completed"
STATUS_COMPLETED_WITH_ERRORS = "completed_with_errors"


class FactoryResetLog(UUIDPKMixin, Base):
    """一次「恢复出厂设置」的完整记录：谁、什么时候、什么范围、清了什么。"""

    __tablename__ = "factory_reset_logs"

    # 不加 ForeignKey（见模块 docstring）：账号可能被本次操作删掉
    operator_id: Mapped[UUID | None] = mapped_column(UUIDType, nullable=True, index=True)
    # 用户名快照：账号被删了也要看得出是谁
    operator_username: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # 备份（`pg_dump`）落点 —— 事后恢复要用的就是这个路径
    backup_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    backup_filename: Mapped[str | None] = mapped_column(String(256), nullable=True)

    # 动手前各表有多少行（"将要失去什么"）
    counts: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    # 各表实际删了多少行
    deleted: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False, default=dict)
    total_deleted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # 单表失败原因（有值时 status=completed_with_errors）
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP"),
        index=True,
    )


__all__ = [
    "FactoryResetLog",
    "STATUS_COMPLETED",
    "STATUS_COMPLETED_WITH_ERRORS",
    "STATUS_DRY_RUN",
    "STATUS_STARTED",
]
