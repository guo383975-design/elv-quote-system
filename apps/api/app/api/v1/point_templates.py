"""点位模板路由：CRUD + 套用到报价单（2026-10-04）。

原型里「点位模板」被引用四处，这个路由满足其中的数据面：
  · 模板管理页的增删改查
  · 报价编制的「套用点位模板」/「批量添加点位」（`POST /{id}/apply`）
  · 添加设备弹窗第 2 个页签（读模板列表 + 套用）

**安装单价的计法**：模板的 `install_unit_price` 是「元/点」，套用时只写到
`is_primary` 的那一行；其余组件行安装费为 0。理由：一个点位常有 3～4 个组件
（球机 + 支架 + 电源 + 线缆），若每行都收一次"180 元/点"，安装费会翻几倍。
模板没标主设备时退化为 `sort_order` 第一行，并在返回里用 `primary_fallback` 说明。
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_permission

# 金额列的算法只有一份（费用瀑布 + 明细金额回写 + 子系统小计回滚），
# 套用模板写完行必须跑它，否则用户看到的是一堆"小计 0"的行。
from app.api.v1.quotes import _recompute
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.device import Device
from app.models.point_template import PointTemplate, PointTemplateItem
from app.models.quote import Quote, QuoteStatus
from app.models.quote_item import ItemSource, QuoteItem
from app.models.subsystem import Subsystem
from app.models.user import User
from app.schemas import (
    Page,
    PointTemplateApplyRequest,
    PointTemplateCreate,
    PointTemplateItemOut,
    PointTemplateOut,
    PointTemplateUpdate,
)

router = APIRouter(prefix="/point-templates", tags=["点位模板"])
log = get_logger("api.point_templates")

# 报价单一旦批准/归档就锁定内容 —— 与 quotes.py 的口径保持一致
_LOCKED_STATUSES = {QuoteStatus.APPROVED, QuoteStatus.ARCHIVED}


def _item_out(item: PointTemplateItem, device: Device | None) -> PointTemplateItemOut:
    return PointTemplateItemOut(
        id=item.id,
        device_id=item.device_id,
        quantity=item.quantity,
        is_primary=item.is_primary,
        notes=item.notes,
        sort_order=item.sort_order,
        device_code=device.code if device else None,
        device_name=device.name if device else None,
        device_model=device.model if device else None,
        device_unit=device.unit if device else None,
        device_reference_price=device.reference_price if device else None,
        device_status=str(device.status) if device else None,
    )


def _template_out(tpl: PointTemplate, devices: dict[UUID, Device]) -> PointTemplateOut:
    out = PointTemplateOut.model_validate(tpl)
    out.items = [
        _item_out(i, devices.get(i.device_id) if i.device_id else None) for i in tpl.items
    ]
    return out


async def _load_devices(db: AsyncSession, tpls: list[PointTemplate]) -> dict[UUID, Device]:
    ids = {i.device_id for t in tpls for i in t.items if i.device_id}
    if not ids:
        return {}
    rows = (await db.scalars(select(Device).where(Device.id.in_(ids)))).all()
    return {d.id: d for d in rows}


async def _replace_items(
    db: AsyncSession, tpl: PointTemplate, items: list
) -> None:
    """整表替换模板明细。

    这里**不能**遍历 `tpl.items` 删除：那是惰性加载，在 async 会话里会抛
    MissingGreenlet（新建的模板根本没加载过 items）。直接发 DELETE 更稳也更快。
    """
    await db.execute(
        delete(PointTemplateItem).where(PointTemplateItem.template_id == tpl.id)
    )
    await db.flush()
    # 关键：模板对象此时可能已在身份映射里且 items 已加载（旧集合）。
    # 不 expire 的话，同一请求里紧接着的 get_template 会返回**替换前**的明细
    # —— PATCH 明明删到只剩 1 行，接口却回 3 行。
    db.expire(tpl, ["items"])
    for order, it in enumerate(items):
        db.add(
            PointTemplateItem(
                template_id=tpl.id,
                device_id=it.device_id,
                quantity=it.quantity,
                is_primary=it.is_primary,
                notes=it.notes,
                sort_order=it.sort_order if it.sort_order else order,
            )
        )
    await db.flush()


@router.get("", response_model=Page[PointTemplateOut], summary="点位模板列表")
async def list_templates(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("point_template", "read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: str | None = Query(None, description="编码/名称 模糊搜索"),
    subsystem_type: str | None = Query(None),
    is_active: bool = Query(True),
    include_inactive: bool = Query(False),
) -> Page[PointTemplateOut]:
    stmt = select(PointTemplate).options(selectinload(PointTemplate.items))
    if not include_inactive:
        stmt = stmt.where(PointTemplate.is_active.is_(is_active))
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(or_(PointTemplate.code.ilike(like), PointTemplate.name.ilike(like)))
    if subsystem_type:
        stmt = stmt.where(PointTemplate.subsystem_type == subsystem_type)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(
            stmt.order_by(PointTemplate.sort_order, PointTemplate.code)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    devices = await _load_devices(db, list(rows))
    return Page[PointTemplateOut](
        items=[_template_out(t, devices) for t in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.get("/{template_id}", response_model=PointTemplateOut, summary="点位模板详情")
async def get_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("point_template", "read"))],
) -> PointTemplateOut:
    tpl = await db.scalar(
        select(PointTemplate)
        .options(selectinload(PointTemplate.items))
        .where(PointTemplate.id == template_id)
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="点位模板不存在")
    devices = await _load_devices(db, [tpl])
    return _template_out(tpl, devices)


@router.post(
    "", response_model=PointTemplateOut, status_code=status.HTTP_201_CREATED,
    summary="新建点位模板",
)
async def create_template(
    body: PointTemplateCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("point_template", "create"))],
) -> PointTemplateOut:
    if await db.scalar(select(PointTemplate).where(PointTemplate.code == body.code)):
        raise HTTPException(status_code=409, detail=f"模板编码已存在：{body.code}")
    tpl = PointTemplate(
        code=body.code,
        name=body.name,
        subsystem_type=body.subsystem_type,
        install_unit_price=body.install_unit_price,
        notes=body.notes,
        is_active=body.is_active,
        sort_order=body.sort_order,
    )
    db.add(tpl)
    await db.flush()
    await _replace_items(db, tpl, body.items)
    await db.commit()
    await db.refresh(tpl, attribute_names=["items"])
    log.info("point template created: {} ({})", tpl.code, tpl.name)
    return await get_template(tpl.id, db, user)


@router.patch("/{template_id}", response_model=PointTemplateOut, summary="更新点位模板")
async def update_template(
    template_id: UUID,
    body: PointTemplateUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("point_template", "update"))],
) -> PointTemplateOut:
    tpl = await db.scalar(
        select(PointTemplate)
        .options(selectinload(PointTemplate.items))
        .where(PointTemplate.id == template_id)
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="点位模板不存在")

    data = body.model_dump(exclude_unset=True, exclude={"items"})
    for key, value in data.items():
        setattr(tpl, key, value)
    if body.items is not None:
        await _replace_items(db, tpl, body.items)
    await db.commit()
    return await get_template(template_id, db, user)


@router.delete(
    "/{template_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除点位模板"
)
async def delete_template(
    template_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("point_template", "delete"))],
) -> None:
    tpl = await db.scalar(select(PointTemplate).where(PointTemplate.id == template_id))
    if tpl is None:
        raise HTTPException(status_code=404, detail="点位模板不存在")
    # 硬删除：模板不被历史数据引用（报价行只记设备，不记模板 id），
    # 但保留「停用」给需要留档的场景（is_active=false）。
    await db.delete(tpl)
    await db.commit()


@router.post("/{template_id}/apply", summary="套用点位模板到报价单（预演或写入）")
async def apply_template(
    template_id: UUID,
    body: PointTemplateApplyRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("point_template", "read"))],
    # 套用会**写报价明细**，所以除了"能读模板"，还必须"能建报价行" ——
    # 否则只读角色（viewer/finance）能借这个接口往报价单里塞行。
    writer: Annotated[User, Depends(require_permission("quote_item", "create"))],
) -> dict:
    """点位数 N → 每行数量 = 模板数量 × N；安装单价（元/点）只落在主设备行。

    `dry_run=True` 只返回将要生成的行，不写库（界面先预览再确认）。
    """
    quote = await db.scalar(
        select(Quote).options(selectinload(Quote.items)).where(Quote.id == body.quote_id)
    )
    if quote is None:
        raise HTTPException(status_code=404, detail="报价单不存在")
    # 锁只挡**写入**：预览（dry_run）看的是"如果套用会生成什么"，对已批准的单一无所害，
    # 而且送审/批准后回看模板效果是正常诉求。
    if quote.status in _LOCKED_STATUSES and not body.dry_run:
        raise HTTPException(
            status_code=409,
            detail=f"报价单已{('批准' if quote.status == QuoteStatus.APPROVED else '归档')}，"
                   "内容已锁定；请先退回草稿再套用模板",
        )

    tpl = await db.scalar(
        select(PointTemplate)
        .options(selectinload(PointTemplate.items))
        .where(PointTemplate.id == template_id)
    )
    if tpl is None:
        raise HTTPException(status_code=404, detail="点位模板不存在")
    if not tpl.is_active:
        raise HTTPException(status_code=409, detail="该点位模板已停用")
    if not tpl.items:
        raise HTTPException(status_code=422, detail="该点位模板没有明细行，套用不会产生任何内容")

    subsystem_id = body.subsystem_id
    if subsystem_id is not None:
        sub = await db.scalar(select(Subsystem).where(Subsystem.id == subsystem_id))
        if sub is None or sub.project_id != quote.project_id:
            raise HTTPException(status_code=422, detail="子系统不属于该报价单所在项目")

    device_ids = {i.device_id for i in tpl.items if i.device_id}
    devices = {
        d.id: d
        for d in (
            await db.scalars(select(Device).where(Device.id.in_(device_ids)))
        ).all()
    } if device_ids else {}

    # 主设备：没标就退化为第一行（并在返回里说明，别让用户以为配置生效了）
    primary_item = next((i for i in tpl.items if i.is_primary), None)
    primary_fallback = primary_item is None
    if primary_item is None:
        primary_item = sorted(tpl.items, key=lambda i: i.sort_order)[0]

    point_count = Decimal(str(body.point_count))
    base_order = max((it.sort_order or 0 for it in quote.items), default=-1) + 1

    planned: list[dict] = []
    skipped: list[dict] = []
    equipment_total = Decimal("0")
    install_total = Decimal("0")

    for idx, item in enumerate(sorted(tpl.items, key=lambda i: i.sort_order)):
        device = devices.get(item.device_id) if item.device_id else None
        if device is None:
            skipped.append({
                "sort_order": item.sort_order,
                "reason": "设备已不存在（模板行 device_id 为空或设备被真删）",
                "notes": item.notes,
            })
            continue
        qty = Decimal(str(item.quantity)) * point_count
        is_primary = item.id == primary_item.id
        install_unit = (
            Decimal(str(tpl.install_unit_price)) if is_primary else Decimal("0")
        )
        equipment_amount = (Decimal(str(device.reference_price or 0)) * qty).quantize(
            Decimal("0.01")
        )
        install_amount = (install_unit * qty).quantize(Decimal("0.01"))
        equipment_total += equipment_amount
        install_total += install_amount
        planned.append({
            "device_id": str(device.id),
            "device_code": device.code,
            "name": device.name,
            "brand": device.brand,
            "model": device.model,
            "unit": device.unit,
            "quantity": float(qty),
            "unit_price": float(device.reference_price or 0),
            "install_unit_price": float(install_unit),
            "equipment_amount": float(equipment_amount),
            "install_amount": float(install_amount),
            "is_primary": is_primary,
            "sort_order": base_order + idx,
            "device_status": str(device.status),
        })

    created = 0
    if not body.dry_run and planned:
        for row in planned:
            db.add(
                QuoteItem(
                    quote_id=quote.id,
                    subsystem_id=subsystem_id,
                    device_id=UUID(row["device_id"]),
                    name=row["name"],
                    brand=row["brand"],
                    model=row["model"],
                    unit=row["unit"],
                    quantity=Decimal(str(row["quantity"])),
                    unit_price=Decimal(str(row["unit_price"])),
                    install_unit_price=Decimal(str(row["install_unit_price"])),
                    source=ItemSource.LIBRARY,
                    sort_order=row["sort_order"],
                )
            )
            created += 1
        # 写完行立刻重算：equipment_amount / install_amount 是**存储列**，
        # 不重算的话报价单合计与逐行金额都还是 0。
        await _recompute(db, quote, actor_id=user.id)
        await db.commit()
        log.info(
            "point template {} applied to quote {}: {} rows × {} 点位",
            tpl.code, quote.code, created, point_count,
        )

    return {
        "dry_run": body.dry_run,
        "template_code": tpl.code,
        "template_name": tpl.name,
        "point_count": float(point_count),
        "install_unit_price": float(tpl.install_unit_price),
        "primary_device_code": next(
            (r["device_code"] for r in planned if r["is_primary"]), None
        ),
        "primary_fallback": primary_fallback,
        "created": created,
        "rows": planned,
        "skipped": skipped,
        "equipment_total": float(equipment_total),
        "install_total": float(install_total),
    }






