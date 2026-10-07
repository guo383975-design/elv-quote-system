"""AI 配置与调用留痕的数据模型（2026-10-04）。

  · `AiUsageLog`：每次模型调用的账（谁、什么任务、模型、token、耗时、成败）。
    不设费用上限（用户决定），但**必须能查** —— 不然出了问题连"谁在用、用了多少"都不知道。
  · `AiParseJob`：一次"供应商报价解析"的任务与结果。结果存下来有两个用处：
    人工确认（可能要过几分钟才点确认）与事后追溯（这一行是 AI 提的还是人改的）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin, UUIDPKMixin
from app.db.types import JSONType, UUIDType


class AiMessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


class AiTask(StrEnum):
    TEST = "test"                          # 测试连接
    PARSE_SUPPLIER_QUOTE = "parse_supplier_quote"   # 供应商报价解析
    ANALYZE_QUOTE = "analyze_quote"        # 报价审查（P2）


class AiParseStatus(StrEnum):
    PENDING = "pending"        # 已建任务，正在解析
    READY = "ready"            # 解析完成，等人工确认
    COMMITTED = "committed"    # 已确认落库
    FAILED = "failed"          # 解析失败（含模型返回不合法）


class AiUsageLog(Base, UUIDPKMixin):
    __tablename__ = "ai_usage_logs"

    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    task: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # token 用量与耗时：不设上限，但要知道花了多少
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    ok: Mapped[bool] = mapped_column(default=True, nullable=False, index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 关联对象（如解析任务 / 报价单），便于"这条日志对应哪次操作"
    ref_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )


class AiParseJob(Base, UUIDPKMixin, TimestampMixin):
    __tablename__ = "ai_parse_jobs"

    kind: Mapped[str] = mapped_column(String(32), default="supplier_quote", nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # 文件指纹：同一份文件重复上传时能提示"这份解析过了"
    file_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    file_size: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    mime: Mapped[str | None] = mapped_column(String(128), nullable=True)

    supplier_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("suppliers.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )

    # 抽取到的原文（截断保存，给"逐字校验"和人工核对用）
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 解析结果：{rows: [...], warnings: [...], engine: "rule"|"llm", model: "..."}
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    # 人工确认落库后的统计：{target, created, updated, skipped, batch_no}
    commit_info: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    created_by: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_name: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AiConversation(Base, UUIDPKMixin, TimestampMixin):
    """一次会话（一个话题）。标题由首条用户消息裁剪生成，用户可改。"""

    __tablename__ = "ai_conversations"

    title: Mapped[str] = mapped_column(String(120), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(
        UUIDType, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # 用户名快照：用户被删后历史还在，显示得出来
    user_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    archived: Mapped[bool] = mapped_column(default=False, nullable=False)

    messages: Mapped[list[AiMessage]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="AiMessage.created_at",
    )


class AiMessage(Base, UUIDPKMixin):
    """一条消息：用户 / 助理 / 工具结果。"""

    __tablename__ = "ai_messages"

    conversation_id: Mapped[UUID] = mapped_column(
        UUIDType, ForeignKey("ai_conversations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)   # user / assistant / tool
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 助理这一条触发的工具调用：[{id, name, arguments}]
    tool_calls: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    # 工具结果消息专用
    tool_call_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tool_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 附件（文件名、解析任务 id、抽取引擎）——"解析这份报价"的上下文
    attachments: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    # 给前端的额外信息（工具执行摘要、待确认卡片等）
    meta: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # created_at 决定展示顺序，也决定**回灌给模型的工具结果顺序**。
    #
    # 原来只用 server_default=func.now()：PostgreSQL 的 now() 是**事务时间**，
    # 同一轮对话（user → assistant(tool_calls) → N 条 tool 结果 → assistant，一次
    # commit）里所有行的 created_at 完全相同，ORDER BY created_at 取回来的顺序是
    # 任意的。结果排在调用之前、或两个结果互换位置时，上游判 11148
    # （tool calls and tool results do not match）并**顶死整条会话**。
    #
    # 改成 Python 侧逐行生成微秒时间戳（保留 server_default 兜底裸 SQL 插入）：
    # 同一事务内也单调递增，顺序不再依赖数据库给平局的返回顺序。
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(tz=UTC),
        server_default=func.now(),
        nullable=False,
        index=True,
    )

    conversation: Mapped[AiConversation] = relationship(back_populates="messages")


__all__ = [
    "AiConversation", "AiMessage", "AiMessageRole", "AiParseJob", "AiParseStatus",
    "AiTask", "AiUsageLog",
]
