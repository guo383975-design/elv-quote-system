"""报表中心：项目/报价/设备库/人工费的总览统计（2026-10-04）。

原型顶栏的「报表中心」一直是置灰占位。这里不做"每个报表一张表"的大工程，
先给一屏**能被追问的总览**，每一项都能点进去看明细：

  · 报价状态分布 —— 各状态单数 + 金额（管理层最常问的"在谈多少、批了多少"）
  · 待办 —— 待审批 / 审核中的单数与金额（审批链堵在哪一目了然）
  · 项目状态分布
  · 设备库健康度 —— 在用/在售/停产/临近停产/已配替代/甲供
  · 停产型号影响 —— 有多少张报价单里还挂着已停产型号（换型没做完的量）
  · 人工费信息价 —— 当前生效期次与三类工日单价

金额一律用 `Decimal` 求和后转 float 出参，避免浮点累加误差。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.labels import label_of
from app.core.permissions import can_see_profit
from app.db.session import get_db
from app.models.device import Device, DeviceStatus
from app.models.labor import LaborRate
from app.models.project import Project
from app.models.quote import Quote, QuoteStatus
from app.models.quote_item import QuoteItem
from app.models.user import User

router = APIRouter(prefix="/reports", tags=["报表中心"])

# 审批链上"还没定"的两个状态
_PENDING_STATUSES = ("submitted", "reviewing")


def _f(value: object) -> float:
    return float(Decimal(str(value if value is not None else 0)))


@router.get("/overview", summary="报表总览（项目/报价/设备/人工费）")
async def overview(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "read"))],
) -> dict:
    # ---- 报价状态分布（单数 + 金额）----
    quote_rows = (
        await db.execute(
            select(Quote.status, func.count(), func.coalesce(func.sum(Quote.total_with_tax), 0))
            .group_by(Quote.status)
        )
    ).all()
    quote_by_status = [
        {
            "status": str(st),
            "label": label_of("quote_status", st),
            "count": int(n or 0),
            "amount": _f(total),
            "pending": str(st) in _PENDING_STATUSES,
        }
        for st, n, total in quote_rows
    ]
    quote_by_status.sort(key=lambda r: r["status"])
    quote_total = sum(r["amount"] for r in quote_by_status)
    pending = [r for r in quote_by_status if r["pending"]]

    # ---- 项目状态分布 ----
    project_rows = (
        await db.execute(select(Project.status, func.count()).group_by(Project.status))
    ).all()
    project_by_status = [
        {
            "status": str(st),
            "label": label_of("project_status", st),
            "count": int(n or 0),
        }
        for st, n in project_rows
    ]
    project_by_status.sort(key=lambda r: r["status"])

    # ---- 设备库健康度 ----
    device_rows = (
        await db.execute(select(Device.status, func.count()).group_by(Device.status))
    ).all()
    device_by_status = {
        str(st): int(n or 0) for st, n in device_rows
    }
    devices_total = await db.scalar(select(func.count()).select_from(Device))
    with_replacement = await db.scalar(
        select(func.count()).select_from(Device).where(Device.replacement_id.is_not(None))
    )
    owner_supplied = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.is_owner_supplied.is_(True))
    )

    # ---- 停产型号影响：还有多少张报价单挂着停产型号 ----
    # 只统计未归档的单：归档单是历史，修不修都无所谓。
    affected = (
        await db.execute(
            select(
                func.count(func.distinct(QuoteItem.quote_id)),
                func.count(QuoteItem.id),
            )
            .select_from(QuoteItem)
            .join(Device, Device.id == QuoteItem.device_id)
            .join(Quote, Quote.id == QuoteItem.quote_id)
            .where(
                Device.status.in_([DeviceStatus.EOL, DeviceStatus.DISCONTINUED]),
                Quote.status != "archived",
            )
        )
    ).one()
    affected_quotes, affected_items = int(affected[0] or 0), int(affected[1] or 0)
    affected_list = (
        await db.execute(
            select(Quote.code, Quote.status, func.count(QuoteItem.id))
            .select_from(QuoteItem)
            .join(Device, Device.id == QuoteItem.device_id)
            .join(Quote, Quote.id == QuoteItem.quote_id)
            .where(
                Device.status.in_([DeviceStatus.EOL, DeviceStatus.DISCONTINUED]),
                Quote.status != "archived",
            )
            .group_by(Quote.code, Quote.status)
            .order_by(func.count(QuoteItem.id).desc())
            .limit(10)
        )
    ).all()

    # ---- 人工费信息价（当前生效）----
    labor_rows = (
        await db.execute(
            select(LaborRate.labor_class, LaborRate.price_per_day, LaborRate.region,
                   LaborRate.effective_date)
            .where(LaborRate.is_current.is_(True))
            .order_by(LaborRate.labor_class)
        )
    ).all()
    labor_current = [
        {
            "labor_class": str(lc),
            "label": label_of("labor_class", lc),
            "price_per_day": _f(price),
            "region": region,
            "effective_date": str(eff)[:10] if eff else None,
        }
        for lc, price, region, eff in labor_rows
    ]

    return {
        "quotes": {
            "total": sum(r["count"] for r in quote_by_status),
            "amount": quote_total,
            "by_status": quote_by_status,
            "pending_count": sum(r["count"] for r in pending),
            "pending_amount": sum(r["amount"] for r in pending),
        },
        "projects": {
            "total": sum(r["count"] for r in project_by_status),
            "by_status": project_by_status,
        },
        "devices": {
            "total": int(devices_total or 0),
            "by_status": device_by_status,
            "on_sale": device_by_status.get("on_sale", 0),
            "near_eol": device_by_status.get("near_eol", 0),
            "eol": device_by_status.get("eol", 0) + device_by_status.get("discontinued", 0),
            "with_replacement": int(with_replacement or 0),
            "owner_supplied": int(owner_supplied or 0),
        },
        "discontinued_impact": {
            "quote_count": affected_quotes,
            "item_count": affected_items,
            "quotes": [
                {"code": code, "status": str(st), "status_label": label_of("quote_status", st),
                 "item_count": int(n or 0)}
                for code, st, n in affected_list
            ],
        },
        "labor_current": labor_current,
    }


@router.get("/profit-dashboard", summary="利润看板（近 N 月的金额/成本/毛利与趋势）")
async def profit_dashboard(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "read"))],
    months: int = Query(6, ge=1, le=24, description="趋势回看的月份数"),
) -> dict:
    """利润看板：总览 + 按项目 + 按月趋势。

    只统计**已批准**的报价单，且每个项目只取最新那一版已批准报价
    —— 否则同一项目改三次价就会被算三次业绩。

    **利润可见性单独判定**（2026-10-05）：能看报价单（project.read）不等于能看利润。
    工程部与销售点进来会拿到 403 + 一句人话，而不是一片 0 —— 后者会被误读成
    "这个月没赚钱"。
    """
    if not can_see_profit(role=user.role, is_superuser=bool(user.is_superuser)):
        raise HTTPException(
            status_code=403,
            detail=(
                "你没有查看利润的权限（利润属于敏感数据，"
                "由「系统设置 → 数据可见性」按角色配置）。"
            ),
        )
    from datetime import UTC, datetime, timedelta
    from decimal import Decimal

    # 判定统一走 core/visibility.can_see_profit（可在「系统设置 → 数据可见性」
    # 按角色配置）。以前这里另有一份写死的 {admin, manager, finance} ——
    # 两份口径迟早会打架（而且那份写死的工程部/销售也看得到利润）。

    # 最新一版已批准报价：按 (project_id, version) 取最大
    rows = (
        await db.execute(
            select(
                Quote.id,
                Quote.code,
                Quote.title,
                Quote.project_id,
                Quote.version,
                Quote.equipment_total,
                Quote.total_with_tax,
                Quote.cost_total,
                Quote.gross_profit,
                Quote.gross_profit_rate,
                Quote.approved_at,
                Quote.created_at,
                Project.code.label("project_code"),
                Project.name.label("project_name"),
                Project.customer_name,
            )
            .join(Project, Project.id == Quote.project_id)
            .where(Quote.status == QuoteStatus.APPROVED)
            .order_by(Quote.version.desc())
        )
    ).all()

    # 「最新一版」= **批准时间最晚**的那张单。
    # 不能用 quotes.version 排序：那是单内乐观锁计数器，每张单都从 1 开始，
    # 拿它比大小等于随机挑一张（这里就踩过：同一项目两张单都 version=1）。
    def approved_when(row):
        # 2026-10-04 起 quotes.approved_at 是真字段（微秒精度）；
        # 老数据没回填到的退回 created_at
        return row.approved_at or row.created_at

    latest: dict = {}
    for r in rows:
        key = str(r.project_id)
        cur = latest.get(key)
        if cur is None:
            latest[key] = r
            continue
        a, b = approved_when(r), approved_when(cur)
        if a is not None and (b is None or a > b):
            latest[key] = r

    def dec(v) -> Decimal:
        return Decimal(str(v or 0))

    total_amount = sum((dec(r.total_with_tax) for r in latest.values()), Decimal("0"))
    total_cost = sum((dec(r.cost_total) for r in latest.values()), Decimal("0"))
    total_profit = sum((dec(r.gross_profit) for r in latest.values()), Decimal("0"))
    rate = (total_profit / total_amount) if total_amount else Decimal("0")

    by_project = sorted(
        (
            {
                "project_code": r.project_code,
                "project_name": r.project_name,
                "customer_name": r.customer_name,
                "quote_code": r.code,
                "total_with_tax": float(dec(r.total_with_tax)),
                "cost_total": float(dec(r.cost_total)),
                "gross_profit": float(dec(r.gross_profit)),
                "gross_profit_rate": float(dec(r.gross_profit_rate)),
                "approved_at": r.approved_at.isoformat() if r.approved_at else None,
            }
            for r in latest.values()
        ),
        key=lambda x: x["gross_profit"],
    )

    # 月度趋势：按批准日期归月（没有批准日期的退回创建日期）
    since = datetime.now(tz=UTC) - timedelta(days=31 * months)
    buckets: dict[str, dict] = {}
    for r in latest.values():
        when = approved_when(r)
        if when is None:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        if when < since:
            continue
        label = when.strftime("%Y-%m")
        b = buckets.setdefault(
            label, {"month": label, "quotes": 0, "amount": Decimal("0"),
                    "cost": Decimal("0"), "profit": Decimal("0")}
        )
        b["quotes"] += 1
        b["amount"] += dec(r.total_with_tax)
        b["cost"] += dec(r.cost_total)
        b["profit"] += dec(r.gross_profit)

    trend = []
    for label in sorted(buckets):
        b = buckets[label]
        trend.append(
            {
                "month": b["month"],
                "quotes": b["quotes"],
                "amount": float(b["amount"]),
                "cost": float(b["cost"]),
                "profit": float(b["profit"]),
                "rate": float(b["profit"] / b["amount"]) if b["amount"] else 0.0,
            }
        )

    return {
        "summary": {
            "projects": len(latest),
            "amount": float(total_amount),
            "cost": float(total_cost),
            "profit": float(total_profit),
            "rate": float(rate),
        },
        "by_project": by_project,
        "trend": trend,
        "months": months,
    }
