"""版本快照与差异计算（2026-10-04）。

差异算法刻意做成**字段级**而不是整块 JSON diff：
使用者想知道的是"含税合计从 62 万变成 68 万、A 行单价 500→600、B 行被删了"，
不是"JSON 第 37 个字符不一样"。
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.quote import Quote
from app.models.quote_item import QuoteItem
from app.models.quote_revision import QuoteRevision

log = get_logger("services.revisions")

# 表头参与对比的字段（费率与口径；标题/状态单列出来）
HEADER_FIELDS = (
    "fee_base",
    "auxiliary_base",
    "auxiliary_rate",
    "design_rate",
    "warranty_rate",
    "training_fee",
    "mgmt_profit_rate",
    "tax_rate",
    "labor_float_rate",
)
TOTAL_FIELDS = (
    "equipment_total",
    "install_total",
    "auxiliary_total",
    "design_fee",
    "warranty_fee",
    "training_fee",
    "subtotal_before_tax",
    "tax_amount",
    "total_with_tax",
)
# 明细参与对比的字段
ITEM_FIELDS = (
    "quantity",
    "unit_price",
    "install_unit_price",
    "auxiliary_unit_price",
    "equipment_amount",
    "install_amount",
    "is_owner_supplied",
)


def _num(value: Any) -> str:
    if value is None:
        return "0"
    try:
        return str(Decimal(str(value)).normalize())
    except Exception:  # noqa: BLE001
        return str(value)


def _item_key(item: dict[str, Any], index: int) -> str:
    """明细的匹配键：优先 device_id，其次"型号+名称"，最后退回序号。

    用序号做键的话，中间插一行会让后面所有行都被判成"改了" —— 差异表就没法看了。
    """
    if item.get("device_id"):
        return f"dev:{item['device_id']}"
    model = (item.get("model") or "").strip()
    name = (item.get("name") or "").strip()
    if model or name:
        return f"nm:{name}|{model}"
    return f"idx:{index}"


def build_snapshot(quote: Quote, items_in: list | None = None) -> dict[str, Any]:
    """把报价单当前内容冻结成快照。

    `items_in` 由调用方显式传入：`quote.items` 是懒加载集合，在 flush 之后访问
    会触发同步 IO，在 async 上下文里直接抛 MissingGreenlet（第二次审批时必现）。
    """
    items = []
    for idx, it in enumerate(items_in if items_in is not None else (quote.items or [])):
        items.append(
            {
                "key": _item_key(
                    {"device_id": str(it.device_id) if it.device_id else None,
                     "model": it.model, "name": it.name},
                    idx,
                ),
                "name": it.name,
                "brand": it.brand,
                "model": it.model,
                "unit": it.unit,
                "quantity": _num(it.quantity),
                "unit_price": _num(it.unit_price),
                "install_unit_price": _num(it.install_unit_price),
                "auxiliary_unit_price": _num(getattr(it, "auxiliary_unit_price", 0)),
                "equipment_amount": _num(it.equipment_amount),
                "install_amount": _num(it.install_amount),
                "is_owner_supplied": bool(it.is_owner_supplied),
                "subsystem_id": str(it.subsystem_id) if it.subsystem_id else None,
            }
        )
    return {
        "header": {f: _num(getattr(quote, f, 0)) for f in HEADER_FIELDS},
        "items": items,
    }


def build_totals(quote: Quote) -> dict[str, str]:
    return {f: _num(getattr(quote, f, 0)) for f in TOTAL_FIELDS}


async def attach_revision_stats(db: AsyncSession, quotes: Sequence[Quote]) -> None:
    """把**业务版本**挂到 Quote 实例上（瞬态属性：不落库、不改任何列）。

    为什么需要这一步（2026-10-08 用户反馈的真因）：
    `quotes.version` 是**乐观锁计数器** —— 每写一次库就 +1（加明细/改数量/算价/
    流转状态…），它回答的是"这份数据有没有被别人动过"，**不是**"这是第几版" ✗。
    展示层把它当版本号显示，于是新建的单"只选了一台录像机"就显示 V9 ✗
    （生产实证：该单 `quote_revisions` 只有 2 行、`quotes.version`=9 ✓）。

    真正的业务版本 = **人保存出来的第几版** = `quote_revisions.seq` 的最大值 ✓。
    没保存过 → 0 → 展示层显示「—」，绝不显示计数器 ✓。

    2026-10-08 用户补充：「版本号以时间为标记」✓ —— 所以这里同时挂上
    **最新一版的 created_at**（`latest_revision_at` ✓），界面与导出件显示它 ✓；
    `seq` 仍然保留 ✓（排序 / 对比 / 追溯要用 ✓）。

    列表页一次 group by 查全部（不给前端留 N+1 ✗）。
    """
    ids = [q.id for q in quotes if getattr(q, "id", None) is not None]
    stats: dict[Any, tuple[int, int, Any]] = {}
    if ids:
        rows = await db.execute(
            select(
                QuoteRevision.quote_id,
                func.count(QuoteRevision.id),
                func.coalesce(func.max(QuoteRevision.seq), 0),
                func.max(QuoteRevision.created_at),
            )
            .where(QuoteRevision.quote_id.in_(ids))
            .group_by(QuoteRevision.quote_id)
        )
        stats = {
            quote_id: (int(count or 0), int(latest or 0), latest_at)
            for quote_id, count, latest, latest_at in rows.all()
        }
    for q in quotes:
        count, latest, latest_at = stats.get(getattr(q, "id", None), (0, 0, None))
        q.revision_count = count
        q.latest_revision_seq = latest
        q.latest_revision_at = latest_at


async def save_revision(
    db: AsyncSession,
    quote: Quote,
    *,
    reason: str,
    user,
    note: str | None = None,
    dedupe: bool = True,
) -> QuoteRevision | None:
    """落一版快照。

    `dedupe=True`：内容与上一版完全一致就跳过（提交一次 → 批准一次，
    中间没改动的话不该出现两个一模一样的"版本"）。
    """
    items = list(
        (
            await db.scalars(
                select(QuoteItem)
                .where(QuoteItem.quote_id == quote.id)
                .order_by(QuoteItem.created_at)
            )
        ).all()
    )
    snapshot = build_snapshot(quote, items)
    totals = build_totals(quote)

    last = await db.scalar(
        select(QuoteRevision)
        .where(QuoteRevision.quote_id == quote.id)
        .order_by(QuoteRevision.seq.desc())
        .limit(1)
    )
    # 去重条件是「内容一样 **且** 原因一样」：
    # 提交→批准 中间没人改动内容，但"批准"这个事实必须留一版（否则版本列表里
    # 看不出这单被批过）；而同一个原因连续存两次同样的内容才是噪声。
    if (
        dedupe
        and last is not None
        and last.snapshot == snapshot
        and last.reason == reason
        and not note
    ):
        log.info(f"报价单 {quote.code} 内容与原因均未变，跳过 {reason} 版本快照")
        return None

    seq = (
        await db.scalar(
            select(func.coalesce(func.max(QuoteRevision.seq), 0)).where(
                QuoteRevision.quote_id == quote.id
            )
        )
    ) or 0

    rev = QuoteRevision(
        quote_id=quote.id,
        seq=int(seq) + 1,
        version=int(quote.version or 0),
        reason=reason,
        status=str(quote.status),
        title=quote.title,
        totals=totals,
        snapshot=snapshot,
        note=note,
        created_by=getattr(user, "id", None),
        created_by_name=getattr(user, "username", None),
    )
    db.add(rev)
    await db.flush()
    who = getattr(user, "username", "?")
    log.info(f"报价单 {quote.code} 落第 {rev.seq} 版（{reason}）by {who}")
    return rev


def diff_snapshots(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """两版快照的字段级差异。"""
    # ----- 表头 -----
    header = []
    for field in HEADER_FIELDS:
        a = (old.get("header") or {}).get(field, "0")
        b = (new.get("header") or {}).get(field, "0")
        if _num(a) != _num(b):
            header.append({"field": field, "from": a, "to": b})

    # ----- 明细 -----
    old_items = {it.get("key"): it for it in (old.get("items") or [])}
    new_items = {it.get("key"): it for it in (new.get("items") or [])}

    added = [
        {"name": it.get("name"), "model": it.get("model"),
         "quantity": it.get("quantity"), "unit_price": it.get("unit_price")}
        for k, it in new_items.items()
        if k not in old_items
    ]
    removed = [
        {"name": it.get("name"), "model": it.get("model"),
         "quantity": it.get("quantity"), "unit_price": it.get("unit_price")}
        for k, it in old_items.items()
        if k not in new_items
    ]
    changed = []
    for key, new_it in new_items.items():
        old_it = old_items.get(key)
        if old_it is None:
            continue
        fields = []
        for field in ITEM_FIELDS:
            a = old_it.get(field)
            b = new_it.get(field)
            if field == "is_owner_supplied":
                if bool(a) != bool(b):
                    fields.append({"field": field, "from": a, "to": b})
            elif _num(a) != _num(b):
                fields.append({"field": field, "from": a, "to": b})
        if fields:
            changed.append(
                {"name": new_it.get("name"), "model": new_it.get("model"), "fields": fields}
            )

    return {
        "header": header,
        "items_added": added,
        "items_removed": removed,
        "items_changed": changed,
        "has_changes": bool(header or added or removed or changed),
    }


def diff_totals(old: dict[str, str], new: dict[str, str]) -> list[dict[str, str]]:
    out = []
    for field in TOTAL_FIELDS:
        a = (old or {}).get(field, "0")
        b = (new or {}).get(field, "0")
        if _num(a) == _num(b):
            continue
        try:
            delta = Decimal(str(b)) - Decimal(str(a))
            delta_s = f"{delta:.2f}"
        except Exception:  # noqa: BLE001
            delta_s = ""
        out.append({"field": field, "from": a, "to": b, "delta": delta_s})
    return out
