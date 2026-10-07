"""定价规则：阶梯价取档 + 价格底线校验（2026-10-04）。

单独一个模块的原因：**取档和底线必须在所有写入点用同一套逻辑**
（手工加行 / 改行 / 换型号 / 导入），否则会出现"手工改价被拦、导入却能绕过"。
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from fastapi import HTTPException, status

from app.core.permissions import Role

# 谁能放行"低于底价"：管理员、经理（与报价批准是同一批人）
FLOOR_OVERRIDE_ROLES = {Role.ADMIN, Role.MANAGER}

# ---------------------------------------------------------------------------
# 设备底价 = 成本价 × 系数（2026-10-05 用户定的口径）
# ---------------------------------------------------------------------------
# 集中在这里：建设备 / 改设备 / 批量导入 三个写入点都调它，
# 口径要变只改这一处（以前底价只能手工填，谁都不会去填 ✗）。
DEFAULT_FLOOR_MULTIPLIER = Decimal("1.35")


def floor_from_cost(
    cost: Decimal | float | int | str | None,
    multiplier: Decimal | float | int | str | None = None,
) -> Decimal | None:
    """按成本价派生设备底价；没有成本价（<=0）返回 None（不编数据）。

    金额保留 2 位、四舍五入（与 Numeric(12,2) 一致）。
    """
    if cost is None or cost == "":
        return None
    try:
        c = Decimal(str(cost))
    except Exception:  # noqa: BLE001 - 脏数据不该让写入失败
        return None
    if c <= 0:
        return None
    m = Decimal(str(multiplier if multiplier is not None else DEFAULT_FLOOR_MULTIPLIER))
    return (c * m).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def pick_tier_price(tiers, qty: Decimal | float | int | None) -> Decimal | None:
    """按数量取阶梯价；没有匹配档位返回 None（由调用方决定退回原价还是报错）。"""
    if not tiers or qty is None:
        return None
    try:
        q = Decimal(str(qty))
    except Exception:  # noqa: BLE001 - 脏数据不该让整单失败
        return None
    for tier in sorted(tiers, key=lambda x: Decimal(str(x.min_qty))):
        low = Decimal(str(tier.min_qty))
        high = Decimal(str(tier.max_qty)) if tier.max_qty is not None else None
        if q >= low and (high is None or q <= high):
            return Decimal(str(tier.unit_price))
    return None


def can_override_floor(user) -> bool:
    return bool(
        getattr(user, "is_superuser", False)
        or str(getattr(user, "role", "")) in {str(r) for r in FLOOR_OVERRIDE_ROLES}
    )


def check_price_floor(
    *,
    device,
    unit_price: Decimal | float | None,
    user,
    reason: str | None,
) -> str | None:
    """检查单价是否低于底价。

    返回 None = 通过；返回字符串 = **本次是越权放行**（调用方负责留痕）。
    抛 422 = 不允许（没底线 / 价格正常 / 无权限 / 没写理由）。
    """
    if device is None or unit_price is None:
        return None
    floor = getattr(device, "min_price", None)
    if floor is None:
        return None
    price = Decimal(str(unit_price))
    floor_dec = Decimal(str(floor))
    if price >= floor_dec:
        return None

    if not can_override_floor(user):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"单价 {price} 低于设备 {device.code} 的底价 {floor_dec}；"
                "低于底价需要经理/管理员操作并写明理由"
            ),
        )
    text = (reason or "").strip()
    if not text:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"单价 {price} 低于底价 {floor_dec}，放行必须填写理由"
                "（below_floor_reason），否则事后无法解释这单为什么亏着报"
            ),
        )
    return f"{price} < 底价 {floor_dec}：{text}"
