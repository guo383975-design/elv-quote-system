"""Labor rate + quota routers: 人工信息价 / 定额子目 / 调价测算."""

from __future__ import annotations

from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.labor import LaborClass, LaborRate, QuotaCategory, QuotaItem
from app.models.user import User
from app.schemas import LaborRateOut, QuotaItemOut

router = APIRouter(tags=["人工与定额"])
log = get_logger("api.labor")

# 三类工日：请求体里的键名 → 枚举。调价测算里要遍历两遍，提成常量避免重复写。
_LABOR_CLASSES: tuple[tuple[str, LaborClass], ...] = (
    ("class_a", LaborClass.A),
    ("class_b", LaborClass.B),
    ("class_c", LaborClass.C),
)


def _parse_date(value) -> date:
    """Accept a `date`, an ISO string, or a datetime — normalize to `date`."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])

# 宁波市 2025-09 期信息价（一类/二类/三类），已由用户确认
DEFAULT_NINGBO_RATES = {
    LaborClass.A: Decimal("150.00"),
    LaborClass.B: Decimal("163.00"),
    LaborClass.C: Decimal("186.00"),
}


# ---------------------------------------------------------------------------
# 人工信息价
# ---------------------------------------------------------------------------

@router.get("/labor-rates", response_model=list[LaborRateOut], summary="人工市场信息价列表")
async def list_labor_rates(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("labor_rate", "read"))],
    region: str = Query("ningbo"),
    only_current: bool = Query(False),
) -> list[LaborRate]:
    stmt = select(LaborRate).where(LaborRate.region == region)
    if only_current:
        stmt = stmt.where(LaborRate.is_current.is_(True))
    rows = await db.scalars(stmt.order_by(LaborRate.effective_date.desc(), LaborRate.labor_class))
    return list(rows.all())


@router.get("/labor-rates/current", summary="当前生效的人工信息价（按工种）")
async def current_labor_rates(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("labor_rate", "read"))],
    region: str = Query("ningbo"),
) -> dict:
    rows = list(
        (
            await db.scalars(
                select(LaborRate).where(
                    LaborRate.region == region, LaborRate.is_current.is_(True)
                )
            )
        ).all()
    )
    if not rows:
        raise HTTPException(status_code=404, detail="尚未录入当前期人工信息价")

    # is_current 的语义是「该地区该工种当前生效」，所以每类工日各有一条。
    # 各类的生效日期可能不一致（例如只补录了某一类的新期次），这时必须告警：
    # 混用不同期次的信息价算出来的人工费是错的，而且从单个数字上看不出来。
    dates = {str(r.labor_class): r.effective_date for r in rows}
    effective = max(dates.values())
    mismatched = {k: v.isoformat() for k, v in dates.items() if v != effective}
    stale_warning = None
    if mismatched:
        detail = "、".join(
            f"{k} {v}" for k, v in sorted(mismatched.items(), key=lambda kv: kv[0])
        )
        stale_warning = (
            f"各类工日的生效期次不一致（最新 {effective.isoformat()}，另有 {detail}），"
            "请核对人工信息价是否录全"
        )
    if len(rows) < 3:
        missing = {"class_a", "class_b", "class_c"} - set(dates)
        stale_warning = (stale_warning or "") + (
            f"；缺少 {'、'.join(sorted(missing))} 的当期价，该工种将回退到内置兜底价"
        )

    return {
        "region": region,
        "effective_date": effective.isoformat(),
        "effective_dates": {k: v.isoformat() for k, v in dates.items()},
        "rates": {
            str(r.labor_class): {
                "price_per_day": float(r.price_per_day),
                "source": r.source,
            }
            for r in rows
        },
        "stale_warning": stale_warning,
    }


@router.post(
    "/labor-rates",
    response_model=LaborRateOut,
    status_code=status.HTTP_201_CREATED,
    summary="录入新一期人工信息价（自动切换 is_current）",
)
async def create_labor_rate(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("labor_rate", "create"))],
) -> LaborRate:
    """body: {region, labor_class, effective_date, price_per_day, source?, notes?}"""
    try:
        labor_class = LaborClass(body["labor_class"])
        effective_date = _parse_date(body["effective_date"])
        price = Decimal(str(body["price_per_day"]))
    except (KeyError, ValueError, InvalidOperation) as e:
        raise HTTPException(status_code=422, detail=f"参数错误: {e}") from e

    if price <= 0:
        raise HTTPException(status_code=422, detail="人工单价必须大于 0")

    region = body.get("region", "ningbo")

    # 同一期同工种已存在 → 409
    dup = await db.scalar(
        select(LaborRate).where(
            LaborRate.region == region,
            LaborRate.labor_class == labor_class,
            LaborRate.effective_date == effective_date,
        )
    )
    if dup:
        raise HTTPException(status_code=409, detail="该期该工种信息价已存在")

    # 新期生效 → 把**同类工日**的旧记录置为非当前。
    # 注意必须带上 labor_class：is_current 是「该地区该工种当前生效」，
    # 如果只按 region 过滤，逐条录入一/二/三类时会互相顶掉，
    # 最后只剩最后提交的那一类是 current，另外两类在计算时静默丢失。
    old_rows = await db.scalars(
        select(LaborRate).where(
            LaborRate.region == region,
            LaborRate.labor_class == labor_class,
            LaborRate.is_current.is_(True),
        )
    )
    for r in old_rows.all():
        r.is_current = False

    rate = LaborRate(
        region=region,
        labor_class=labor_class,
        effective_date=effective_date,
        price_per_day=price,
        source=body.get("source", "manual"),
        notes=body.get("notes"),
        is_current=True,
    )
    db.add(rate)
    await db.flush()
    await db.refresh(rate)
    log.info(f"录入人工信息价 {region} {labor_class} ¥{price} 生效 {effective_date}")
    return rate


@router.post("/labor-rates/adjust-simulate", summary="调价测算：给定期次对比人工费变化")
async def adjust_simulate(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("labor_rate", "read"))],
) -> dict:
    """body: {
        "labor_days": {"class_a": 68, "class_b": 300, "class_c": 50},
        "new_rates": {"class_a": 158, "class_b": 171, "class_c": 195}   # 可选
    }

    返回旧/新人工费与差额，供 Page 4「调价测算」按钮使用。
    """
    days = body.get("labor_days") or {}
    new_rates = body.get("new_rates") or {}

    current = list(
        (
            await db.scalars(
                select(LaborRate).where(
                    LaborRate.region == "ningbo", LaborRate.is_current.is_(True)
                )
            )
        ).all()
    )
    if not current:
        current_map = DEFAULT_NINGBO_RATES
        rate_source = "default-seed"
    else:
        current_map = {r.labor_class: Decimal(str(r.price_per_day)) for r in current}
        rate_source = "db"

    def total(rate_map: dict) -> Decimal:
        s = Decimal("0")
        for key, cls in _LABOR_CLASSES:
            d = Decimal(str(days.get(key, 0)))
            r = Decimal(str(rate_map.get(cls, rate_map.get(str(cls), 0))))
            s += d * r
        return s

    old_total = total(current_map)
    merged = dict(current_map)
    for key, cls in _LABOR_CLASSES:
        if key in new_rates:
            merged[cls] = Decimal(str(new_rates[key]))
    new_total = total(merged)

    delta = new_total - old_total
    pct = (delta / old_total * 100) if old_total else Decimal("0")
    return {
        "labor_days": days,
        "old_rates": {str(k): float(v) for k, v in current_map.items()},
        "new_rates": {str(k): float(v) for k, v in merged.items()},
        "old_total": float(old_total),
        "new_total": float(new_total),
        "delta": float(delta),
        "delta_pct": round(float(pct), 2),
        "rate_source": rate_source,
    }


# ---------------------------------------------------------------------------
# 定额子目
# ---------------------------------------------------------------------------

@router.get("/quota-items", response_model=list[QuotaItemOut], summary="定额子目列表")
async def list_quota_items(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quota", "read"))],
    category: QuotaCategory | None = None,
    keyword: str | None = None,
) -> list[QuotaItem]:
    stmt = select(QuotaItem).where(QuotaItem.is_active.is_(True))
    if category:
        stmt = stmt.where(QuotaItem.category == category)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(QuotaItem.name.ilike(like) | QuotaItem.code.ilike(like))
    rows = await db.scalars(stmt.order_by(QuotaItem.code))
    return list(rows.all())


@router.post(
    "/quota-items",
    response_model=QuotaItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增定额子目",
)
async def create_quota_item(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quota", "create"))],
) -> QuotaItem:
    if not body.get("code") or not body.get("name"):
        raise HTTPException(status_code=422, detail="code 与 name 必填")
    if await db.scalar(select(QuotaItem).where(QuotaItem.code == body["code"])):
        raise HTTPException(status_code=409, detail="定额编号已存在")

    item = QuotaItem(
        code=body["code"],
        category=body.get("category", "other"),
        name=body["name"],
        unit=body.get("unit", "100m"),
        labor_a_day=Decimal(str(body.get("labor_a_day", 0))),
        labor_b_day=Decimal(str(body.get("labor_b_day", 0))),
        labor_c_day=Decimal(str(body.get("labor_c_day", 0))),
        base_price=Decimal(str(body.get("base_price", 0))),
        shielded_coef=Decimal(str(body.get("shielded_coef", "1.20"))),
        cat5e_coef=Decimal(str(body.get("cat5e_coef", "1.10"))),
        ceiling_coef=Decimal(str(body.get("ceiling_coef", "1.20"))),
        notes=body.get("notes"),
    )
    db.add(item)
    await db.flush()
    await db.refresh(item)
    return item


@router.get("/quota-items/{code}", response_model=QuotaItemOut, summary="按定额编号查询")
async def get_quota_item(
    code: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quota", "read"))],
) -> QuotaItem:
    item = await db.scalar(select(QuotaItem).where(QuotaItem.code == code))
    if not item:
        raise HTTPException(status_code=404, detail="定额子目不存在")
    return item
