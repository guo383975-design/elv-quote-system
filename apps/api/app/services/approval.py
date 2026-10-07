"""分级审批的规则计算（2026-10-04）。

阈值判定、已批级数、能否批准 —— 全部收在这里，接口层只调用不判断，
避免"改价接口放行、审批接口拦"这种两套标准。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.quote_approval import QuoteApproval


def required_levels(total_with_tax, tiers: list[dict] | None, enabled: bool) -> int:
    """按含税合计算出需要几级审批。未开启一律 1 级（等同现状）。"""
    if not enabled:
        return 1
    amount = Decimal(str(total_with_tax or 0))
    levels = 1
    for tier in sorted(tiers or [], key=lambda x: Decimal(str(x.get("min_amount", 0)))):
        if amount >= Decimal(str(tier.get("min_amount", 0))):
            levels = max(levels, int(tier.get("levels", 1)))
    return max(1, levels)


async def approved_levels(db: AsyncSession, quote_id) -> list[QuoteApproval]:
    rows = (
        await db.scalars(
            select(QuoteApproval)
            .where(QuoteApproval.quote_id == quote_id, QuoteApproval.decision == "approved")
            .order_by(QuoteApproval.level)
        )
    ).all()
    return list(rows)


async def all_approvals(db: AsyncSession, quote_id) -> list[QuoteApproval]:
    rows = (
        await db.scalars(
            select(QuoteApproval)
            .where(QuoteApproval.quote_id == quote_id)
            .order_by(QuoteApproval.created_at)
        )
    ).all()
    return list(rows)


def next_level(approved: list[QuoteApproval]) -> int:
    """下一级该批第几级：已批级数 + 1（同一个人可以批多级，现实里小公司就是这样）。"""
    return len(approved) + 1


def can_finalize(approved: list[QuoteApproval], required: int) -> bool:
    return len(approved) >= max(1, required)
