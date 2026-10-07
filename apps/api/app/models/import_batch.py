"""Import batch model: 导入批次留档（#IM-年月日-序号）。

## 为什么要留档

导入是**批量改数据**的动作：一次传错文件，几十条设备的价格就变了，
而系统里事后只能看到"某条设备被谁什么时候改的"，看不到"这一批是一起来的、
原始文件叫什么、哪几行失败了"。原型里那个 `#IM-20260930-001` 就是这个东西。

现在每次 commit 都落一条批次记录：
  · 批次号 `IM-20261004-001`（按天递增，人能在对话里引用）
  · 原始文件名 / 大小 / 行数
  · 新增 / 更新 / 跳过 / 错误 的条数
  · 失败明细（最多 50 条，行号 + 原因）—— 用户当场就能核对
  · 操作人（外键 SET NULL，人删了留档还在）

**不改行为**：只记录，不拦截。重复策略等仍然是导入参数。
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import JSONType, UUIDType, sa_enum


class ImportKind(StrEnum):
    """导入类型。"""

    DEVICE = "device"          # 设备库导入（场景一）
    INQUIRY = "inquiry"        # 甲方询价清单导入（场景二）
    SUPPLIER_QUOTE = "supplier_quote"  # 供应商报价导入（AI 解析后人工确认，2026-10-04）


class ImportStatus(StrEnum):
    """批次结果。"""

    SUCCESS = "success"        # 全部成功
    PARTIAL = "partial"        # 有失败/跳过，但至少写进去一部分
    FAILED = "failed"          # 一条都没写进去


class ImportBatch(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "import_batches"

    # 形如 IM-20261004-001：人可以口头引用（"那张 IM-…-001 的单子"）
    batch_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    kind: Mapped[ImportKind] = mapped_column(
        sa_enum(ImportKind, "import_kind"), nullable=False, index=True
    )
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    skipped_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    status: Mapped[ImportStatus] = mapped_column(
        sa_enum(ImportStatus, "import_status"), nullable=False, index=True
    )
    # 参数与结果摘要：{duplicate_policy, created_categories: [...], created_items: n, ...}
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    # 失败明细 [{row: 12, message: "必填项为空"}]，最多 50 条（够用户核对，不撑爆行）
    errors: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    created_by: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)


__all__ = ["ImportBatch", "ImportKind", "ImportStatus"]
