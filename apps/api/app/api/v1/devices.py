"""Devices router: 设备库 CRUD + 精确/模糊匹配 + 停产替代建议."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated
from urllib.parse import quote as urlquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.device_tree import CODE_PREFIX_BY_CATEGORY
from app.core.labels import label_of
from app.core.logging import get_logger
from app.core.permissions import can_see_cost
from app.db.session import get_db
from app.db.types import drop_none_for_not_null
from app.models.device import (
    Device,
    DeviceAccessoryGroup,
    DeviceAccessoryOption,
    DeviceCategory,
    DeviceCategoryNode,
    DevicePriceTier,
    DeviceStatus,
)
from app.models.quote_item import QuoteItem
from app.models.subsystem import Subsystem
from app.models.supplier import Supplier
from app.models.user import User
from app.schemas import (
    AccessorySaveIn,
    DeviceBulkPriceRequest,
    DeviceCategoryNodeIn,
    DeviceCategoryNodeOut,
    DeviceCategoryNodeUpdate,
    DeviceCategoryTreeOut,
    DeviceCreate,
    DeviceFromQuoteItemsRequest,
    DeviceOut,
    DeviceUpdate,
    Page,
    PriceTierIn,
    PriceTierOut,
)
from app.services.device_match import MatchCandidate, match_device, suggest_replacements
from app.services.excel_io import DEVICE_TEMPLATE_COLUMNS
from app.services.pricing import floor_from_cost

router = APIRouter(prefix="/devices", tags=["设备库"])
log = get_logger("api.devices")


# ---------------------------------------------------------------------------
# 分类树（2026-09-30 起：设备库不再是一层扁平分类）
# ---------------------------------------------------------------------------

async def _load_category_nodes(db: AsyncSession) -> dict[UUID, DeviceCategoryNode]:
    """一次把分类树全读进来（几十到几百个节点，比每行都去查父链便宜得多）。"""
    rows = await db.scalars(select(DeviceCategoryNode))
    return {n.id: n for n in rows.all()}


def _node_path(node_id: UUID | None, by_id: dict[UUID, DeviceCategoryNode]) -> str:
    """节点的完整路径，如「视频监控 / 摄像机」。找不到节点时返回空串。"""
    if node_id is None:
        return ""
    parts: list[str] = []
    seen: set[UUID] = set()
    cur = by_id.get(node_id)
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        parts.append(cur.name)
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return " / ".join(reversed(parts))


def _root_enum_of(node_id: UUID | None, by_id: dict[UUID, DeviceCategoryNode]) -> str | None:
    """沿父链找到一级节点的 `legacy_enum`（= DeviceCategory 的取值）。

    写设备时用它反推 `devices.category`，保证「枚举」与「树上的位置」永远一致
    （不能让一个设备说自己是 network，却挂在视频监控下面）。
    """
    seen: set[UUID] = set()
    cur = by_id.get(node_id) if node_id else None
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        if cur.legacy_enum:
            return cur.legacy_enum
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return None


def _descendant_ids(node_id: UUID, by_id: dict[UUID, DeviceCategoryNode]) -> set[UUID]:
    """节点自身 + 全部后代 id。

    点一个父节点必须能看到整棵子树 —— 否则"树"只是个装饰，用户点「视频监控」
    却发现下面是空的（东西都挂在「摄像机」子节点上）。
    """
    out = {node_id}
    changed = True
    while changed:
        changed = False
        for nid, node in by_id.items():
            if nid not in out and node.parent_id in out:
                out.add(nid)
                changed = True
    return out


def _effective_prefix(node_id: UUID | None, by_id: dict[UUID, DeviceCategoryNode]) -> str | None:
    """节点的编号前缀；本身为空则继承祖先。"""
    seen: set[UUID] = set()
    cur = by_id.get(node_id) if node_id else None
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        if cur.code_prefix:
            return cur.code_prefix
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return None


def _node_out(
    node: DeviceCategoryNode,
    by_id: dict[UUID, DeviceCategoryNode],
    *,
    device_count: int = 0,
    total_count: int = 0,
    children: list[DeviceCategoryNodeOut] | None = None,
) -> DeviceCategoryNodeOut:
    """显式构造节点的输出模型。

    ⚠ 不能图省事写 `DeviceCategoryNodeOut.model_validate(node)`：
    ORM 模型上有个 `children` 关系，pydantic 会去读它 → 在 async 上下文里
    触发懒加载 → MissingGreenlet（500）。字段逐个搬过来是这里唯一安全的做法。
    """
    return DeviceCategoryNodeOut(
        id=node.id,
        parent_id=node.parent_id,
        code=node.code,
        name=node.name,
        sort_order=node.sort_order,
        is_active=node.is_active,
        legacy_enum=node.legacy_enum,
        code_prefix=node.code_prefix,
        notes=node.notes,
        path=_node_path(node.id, by_id),
        device_count=device_count,
        total_count=total_count,
        children=children or [],
    )


def _device_out_for_view(
    user: User,
    device: Device,
    *,
    category_path: str | None = None,
) -> DeviceOut:
    """根据角色裁剪 DeviceOut：sales / viewer 看不到 cost_price。

    cost_price 走字段级权限（与 quote.cost_total 同一规则）：
    - admin / estimator / finance（含 superuser）→ 看到成本价
    - sales / viewer → 必须置空，否则内部资料从前端泄出去

    以前 `DeviceOut.model_validate(r)` 无差别地把 cost_price 透出来，
    即使 viewer 也调的是同一个 GET /devices，于是 sales / viewer 看
    设备列表时也能看到进价。这种「同一接口不同角色字段不同」的修复点
    必须在**序列化出口**做（而不是在 DB 层或中间层），否则换成其他
    `DeviceOut` 的返回路径就会漏。

    2026-09-30 起多带一个 `category_path`（「视频监控 / 摄像机」）：
    路径要顺着父链算，直接在 pydantic 里读 `device.category_node.parent…`
    会变成异步懒加载（MissingGreenlet），所以由调用方从预先读好的
    分类树里算好传进来。
    """
    out = DeviceOut.model_validate(device)
    if not can_see_cost(role=user.role, is_superuser=bool(user.is_superuser)):
        out.cost_price = None
    if category_path is not None:
        out.category_path = category_path
    return out


async def _next_device_code(
    db: AsyncSession,
    category: DeviceCategory,
    *,
    category_id: UUID | None = None,
    by_id: dict[UUID, DeviceCategoryNode] | None = None,
) -> str:
    """Generate code like V1011 — 前缀取自分类节点，序号按前缀递增。

    前缀原来写死在本函数的字典里；分类树把"分类"变成数据之后，前缀跟着**节点**走
    （节点的 code_prefix，子节点留空就继承父节点），一级分类的映射退化为兜底。
    """
    prefix: str | None = None
    if category_id is not None:
        if by_id is None:
            by_id = await _load_category_nodes(db)
        prefix = _effective_prefix(category_id, by_id)
    if not prefix:
        prefix = CODE_PREFIX_BY_CATEGORY.get(str(category), "X")

    rows = await db.scalars(
        select(Device.code).where(Device.code.like(f"{prefix}%"))
    )
    max_n = 0
    for code in rows.all():
        suffix = code[len(prefix):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:04d}"


@router.get("", response_model=Page[DeviceOut], summary="设备库列表（分页 + 多条件筛选）")
async def list_devices(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    keyword: str | None = Query(None, description="名称/型号/品牌 模糊搜索"),
    category: DeviceCategory | None = Query(None, description="一级分类（历史参数，兼容保留）"),
    category_id: UUID | None = Query(None, description="分类树节点 id"),
    include_descendants: bool = Query(
        True, description="按节点筛选时是否包含其全部子节点（树状浏览默认要包含）"
    ),
    uncategorized: bool = Query(False, description="只看未挂到分类树上的设备"),
    device_status: DeviceStatus | None = Query(None, alias="status"),
    brand: str | None = Query(None),
    only_common: bool = Query(False, description="只看常用设备"),
    is_owner_supplied: bool | None = Query(
        None, description="只看/排除甲供设备（原型第 5 页的「仅看甲供设备」）"
    ),
    has_replacement: bool | None = Query(
        None, description="是否只看已配替代型号的（原型第 5 页的「仅看停产有替代」）"
    ),
) -> Page[DeviceOut]:
    stmt = select(Device).where(Device.is_active.is_(True))
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            or_(
                Device.name.ilike(like),
                Device.model.ilike(like),
                Device.brand.ilike(like),
                Device.code.ilike(like),
            )
        )
    if category:
        stmt = stmt.where(Device.category == category)
    if uncategorized:
        stmt = stmt.where(Device.category_id.is_(None))
    elif category_id is not None:
        by_id = await _load_category_nodes(db)
        if category_id not in by_id:
            raise HTTPException(status_code=404, detail="分类节点不存在")
        # 点父节点要能看到整棵子树 —— 否则用户点「视频监控」看到空列表，
        # 而设备其实都挂在「摄像机」子节点上。
        ids = (
            _descendant_ids(category_id, by_id) if include_descendants else {category_id}
        )
        stmt = stmt.where(Device.category_id.in_(ids))
    if device_status:
        stmt = stmt.where(Device.status == device_status)
    if brand:
        stmt = stmt.where(Device.brand == brand)
    if only_common:
        stmt = stmt.where(Device.is_common.is_(True))
    if is_owner_supplied is not None:
        stmt = stmt.where(Device.is_owner_supplied.is_(is_owner_supplied))
    if has_replacement is not None:
        if has_replacement:
            stmt = stmt.where(Device.replacement_id.is_not(None))
        else:
            stmt = stmt.where(Device.replacement_id.is_(None))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(
            stmt.order_by(Device.code).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    # 分类路径只读一次分类树，避免每行都顺着父链查库
    nodes = await _load_category_nodes(db)
    return Page[DeviceOut](
        items=[
            _device_out_for_view(
                user, r, category_path=_node_path(r.category_id, nodes)
            )
            for r in rows
        ],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


# 子系统 → 设备一级分类：两边枚举的取值并不完全同名（access_control vs access、
# structured_cabling vs cabling、server_room 同名），这里显式写出来，不靠字符串猜。
_SUBSYSTEM_TO_CATEGORY: dict[str, DeviceCategory] = {
    "video_surveillance": DeviceCategory.VIDEO,
    "access_control": DeviceCategory.ACCESS,
    "intrusion": DeviceCategory.INTRUSION,
    "structured_cabling": DeviceCategory.CABLING,
    "network": DeviceCategory.NETWORK,
    "broadcast": DeviceCategory.BROADCAST,
    "conference": DeviceCategory.CONFERENCE,
    "server_room": DeviceCategory.SERVER_ROOM,
    "parking": DeviceCategory.PARKING,
    "info_publish": DeviceCategory.INFO_PUBLISH,
}


@router.post("/from-quote-items", summary="把手动录入的报价行收进设备库（原型「复制到系统」）")
async def devices_from_quote_items(
    body: DeviceFromQuoteItemsRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "create"))],
) -> dict:
    """报价行 → 企业设备库。

    跳过规则（都会如实说明原因，不做"静默少建"）：
      · 该行已关联设备库型号（`device_id` 非空）—— 库里本来就有；
      · 同「品牌 + 型号」已存在 —— 返回命中的编码，避免库里出现重复型号；
      · 没有型号也没有名称 —— 建了也没法用。
    """
    items = (
        await db.scalars(select(QuoteItem).where(QuoteItem.id.in_(body.item_ids)))
    ).all()
    found = {i.id: i for i in items}
    missing = [str(x) for x in body.item_ids if x not in found]

    # 同型号索引（品牌 + 型号，忽略大小写与空白）
    def _key(brand: str | None, model: str | None) -> str:
        return f"{(brand or '').strip().upper()}|{(model or '').strip().upper()}"

    existing = {
        _key(d.brand, d.model): d
        for d in (await db.scalars(select(Device))).all()
        if (d.model or "").strip()
    }

    # 报价行所属子系统 → 一级分类（拿不到就落 accessory）
    subsystem_ids = {i.subsystem_id for i in items if i.subsystem_id}
    subsystems = {
        s.id: s
        for s in (
            await db.scalars(select(Subsystem).where(Subsystem.id.in_(subsystem_ids)))
        ).all()
    } if subsystem_ids else {}

    nodes = await _load_category_nodes(db)
    created: list[dict] = []
    skipped: list[dict] = []

    for item in items:
        if item.device_id is not None:
            skipped.append({"item_id": str(item.id), "name": item.name,
                            "reason": "该行本来就来自设备库"})
            continue
        if not (item.model or "").strip() and not (item.name or "").strip():
            skipped.append({"item_id": str(item.id), "name": item.name,
                            "reason": "没有名称也没有型号，无法建库"})
            continue
        hit = existing.get(_key(item.brand, item.model)) if (item.model or "").strip() else None
        if hit is not None:
            skipped.append({"item_id": str(item.id), "name": item.name,
                            "reason": f"库里已有同型号：{hit.code} {hit.name}"})
            continue

        sub = subsystems.get(item.subsystem_id) if item.subsystem_id else None
        category = _SUBSYSTEM_TO_CATEGORY.get(str(sub.subsystem_type), DeviceCategory.ACCESSORY) \
            if sub is not None else DeviceCategory.ACCESSORY
        # 分类节点：显式指定 > 该一级分类在树上的节点
        node_id = body.category_id
        if node_id is None:
            node_id = next(
                (nid for nid, n in nodes.items()
                 if getattr(n, "legacy_enum", None) == str(category) and n.parent_id is None),
                None,
            )
        code = await _next_device_code(db, category, category_id=node_id, by_id=nodes)
        device = Device(
            code=code,
            name=(item.name or "").strip() or (item.model or "").strip(),
            brand=(item.brand or None),
            model=(item.model or None),
            unit=item.unit or "台",
            category=category,
            category_id=node_id,
            reference_price=item.unit_price or 0,
            cost_price=item.cost_price or 0,
            install_unit_price=item.install_unit_price or 0,
            status=DeviceStatus.ON_SALE,
            is_active=True,
            is_owner_supplied=bool(item.is_owner_supplied),
            moq=1,
            warranty_years=2,
        )
        db.add(device)
        await db.flush()
        existing[_key(device.brand, device.model)] = device
        created.append({"item_id": str(item.id), "device_id": str(device.id),
                        "code": device.code, "name": device.name,
                        "reference_price": float(device.reference_price or 0)})

    if created:
        await db.commit()
        log.info(
            "promoted {} quote rows to device library by {}",
            len(created), user.username,
        )

    return {"created": created, "skipped": skipped, "missing": missing,
            "created_count": len(created), "skipped_count": len(skipped)}


@router.post("/bulk-price", summary="批量调价（原型第 5 页工具栏；默认只预览不落库）")
async def bulk_price(
    body: DeviceBulkPriceRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "update"))],
) -> dict:
    """按百分比或金额批量改价。

    价格是钱，所以：
      · `dry_run=True`（默认）**只算不改**，先把逐行变更返回给用户确认；
      · 结果价不允许为负，负值一律夹到 0 并在返回里标注；
      · `round_yuan=True` 取整到元（报价实务里更常用），否则保留 2 位小数。
    """
    from decimal import ROUND_HALF_UP, Decimal

    devices = (
        await db.scalars(select(Device).where(Device.id.in_(body.device_ids)))
    ).all()
    found = {d.id: d for d in devices}
    missing = [str(i) for i in body.device_ids if i not in found]

    quant = Decimal("1") if body.round_yuan else Decimal("0.01")
    changes: list[dict] = []
    total_before = Decimal("0")
    total_after = Decimal("0")

    for d in devices:
        raw_before = getattr(d, body.field, None)
        before = Decimal(str(raw_before if raw_before is not None else 0))
        if body.mode == "percent":
            after = before * (Decimal("1") + body.value / Decimal("100"))
        else:
            after = before + body.value
        clamped = after < 0
        if clamped:
            after = Decimal("0")
        after = after.quantize(quant, rounding=ROUND_HALF_UP)

        changes.append({
            "device_id": str(d.id),
            "code": d.code,
            "name": d.name,
            "before": float(before),
            "after": float(after),
            "delta": float(after - before),
            "clamped": clamped,
        })
        total_before += before
        total_after += after
        if not body.dry_run:
            setattr(d, body.field, after)

    if not body.dry_run:
        await db.commit()
        log.info(
            "bulk-price applied by {}: field={} mode={} value={} rows={}",
            user.username, body.field, body.mode, body.value, len(changes),
        )

    changes.sort(key=lambda c: c["code"])
    return {
        "dry_run": body.dry_run,
        "field": body.field,
        "mode": body.mode,
        "value": float(body.value),
        "round_yuan": body.round_yuan,
        "count": len(changes),
        "missing": missing,
        "total_before": float(total_before),
        "total_after": float(total_after),
        "changes": changes,
    }


@router.get("/export.xlsx", summary="导出设备库（xlsx，列与导入模板一致，可回灌）")
async def export_devices(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> Response:
    """导出整份设备库。

    列**故意与导入模板一致**（`DEVICE_TEMPLATE_COLUMNS`）：导出的文件能直接再喂回
    「Excel 批量导入」，形成"导出 → 改 → 导回"的闭环，不需要另做一份列定义。

    只导出在用设备（`is_active`），与设备库页面的口径一致。
    """
    from io import BytesIO

    from openpyxl import Workbook

    devices = (
        await db.scalars(
            select(Device).where(Device.is_active.is_(True)).order_by(Device.code)
        )
    ).all()
    nodes = await _load_category_nodes(db)

    # 供应商名与替代型号一次查完，避免 N+1
    supplier_ids = {d.supplier_id for d in devices if d.supplier_id}
    suppliers = {
        s.id: s.name
        for s in (
            await db.scalars(select(Supplier).where(Supplier.id.in_(supplier_ids)))
        ).all()
    } if supplier_ids else {}
    repl_ids = {d.replacement_id for d in devices if d.replacement_id}
    replacements = {
        d.id: d
        for d in (
            await db.scalars(select(Device).where(Device.id.in_(repl_ids)))
        ).all()
    } if repl_ids else {}

    wb = Workbook()
    ws = wb.active
    ws.title = "设备库"
    ws.append(list(DEVICE_TEMPLATE_COLUMNS))
    for d in devices:
        repl = replacements.get(d.replacement_id) if d.replacement_id else None
        ws.append([
            d.name,
            d.brand or "",
            d.model or "",
            _node_path(d.category_id, nodes),
            d.unit,
            float(d.reference_price or 0),
            suppliers.get(d.supplier_id, "") if d.supplier_id else "",
            d.moq,
            d.warranty_years,
            "是" if d.is_owner_supplied else "否",
            label_of("device_status", d.status) or str(d.status),
            (repl.model or repl.name) if repl else "",
        ])
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    today = datetime.now().strftime("%Y%m%d")
    filename = f"设备库_{today}.xlsx"
    return Response(
        content=buffer.getvalue(),
        media_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
        headers={
            "Content-Disposition": (
                f'attachment; filename="devices_{today}.xlsx"; '
                f"filename*=UTF-8''{urlquote(filename, safe='')}"
            )
        },
    )


@router.get("/stats", summary="设备库统计（按状态/分类）")
async def device_stats(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> dict:
    by_status = {
        str(s): n
        for s, n in (
            await db.execute(
                select(Device.status, func.count())
                .where(Device.is_active.is_(True))
                .group_by(Device.status)
            )
        ).all()
    }
    by_category = {
        str(c): n
        for c, n in (
            await db.execute(
                select(Device.category, func.count())
                .where(Device.is_active.is_(True))
                .group_by(Device.category)
            )
        ).all()
    }
    total = await db.scalar(
        select(func.count()).select_from(Device).where(Device.is_active.is_(True))
    )
    with_replacement = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.replacement_id.is_not(None))
    )
    # 甲供设备数：原型第 5 页的统计条要显示「甲供 N」，以前这个数拿不到
    owner_supplied = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.is_active.is_(True), Device.is_owner_supplied.is_(True))
    )
    return {
        "total": total or 0,
        "by_status": by_status,
        "by_category": by_category,
        "with_replacement": with_replacement or 0,
        "owner_supplied": owner_supplied or 0,
    }


# ---------------------------------------------------------------------------
# 分类树：查询 + 节点维护
# ---------------------------------------------------------------------------

@router.get(
    "/category-tree",
    response_model=DeviceCategoryTreeOut,
    summary="设备库分类树（含每级设备数）",
)
async def category_tree(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
    include_inactive: bool = Query(False, description="含已停用的分类节点"),
) -> DeviceCategoryTreeOut:
    """返回整棵分类树。

    `device_count` 是节点**自身**挂的设备数，`total_count` 是它含所有子节点的合计 ——
    树上显示的是后者（点父节点能看到子树），前者用来判断"这个节点自己有没有东西"。
    """
    by_id = await _load_category_nodes(db)
    counts: dict[UUID, int] = dict.fromkeys(by_id, 0)
    for cid, n in (
        await db.execute(
            select(Device.category_id, func.count())
            .where(Device.is_active.is_(True), Device.category_id.is_not(None))
            .group_by(Device.category_id)
        )
    ).all():
        if cid in counts:
            counts[cid] = n
    unassigned = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.is_active.is_(True), Device.category_id.is_(None))
    )

    out_by_id: dict[UUID, DeviceCategoryNodeOut] = {}
    for nid, n in by_id.items():
        if not include_inactive and not n.is_active:
            continue
        out_by_id[nid] = DeviceCategoryNodeOut(
            id=n.id,
            parent_id=n.parent_id,
            code=n.code,
            name=n.name,
            sort_order=n.sort_order,
            is_active=n.is_active,
            legacy_enum=n.legacy_enum,
            code_prefix=n.code_prefix,
            notes=n.notes,
            path=_node_path(n.id, by_id),
            device_count=counts.get(nid, 0),
            total_count=0,
            children=[],
        )

    roots: list[DeviceCategoryNodeOut] = []
    for node in out_by_id.values():
        parent = out_by_id.get(node.parent_id) if node.parent_id else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)

    def _finalize(node: DeviceCategoryNodeOut) -> int:
        node.children.sort(key=lambda c: (c.sort_order, c.name))
        total = node.device_count
        for child in node.children:
            total += _finalize(child)
        node.total_count = total
        return total

    for root in roots:
        _finalize(root)
    roots.sort(key=lambda c: (c.sort_order, c.name))

    have_roots = {n.legacy_enum for n in by_id.values() if n.legacy_enum}
    all_active = sum(counts.values()) + (unassigned or 0)
    return DeviceCategoryTreeOut(
        nodes=roots,
        unassigned=unassigned or 0,
        total=all_active,
        missing_roots=[str(c) for c in DeviceCategory if str(c) not in have_roots],
    )


def _validate_legacy_enum(value: str | None) -> str | None:
    if value in (None, ""):
        return None
    allowed = {str(c) for c in DeviceCategory}
    if value not in allowed:
        raise HTTPException(
            status_code=422,
            detail=f"legacy_enum 取值非法：{value!r}，可选 {sorted(allowed)}",
        )
    return value


@router.post(
    "/categories",
    response_model=DeviceCategoryNodeOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增分类节点（层级不限）",
)
async def create_category(
    body: DeviceCategoryNodeIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "create"))],
) -> DeviceCategoryNodeOut:
    if await db.scalar(
        select(DeviceCategoryNode).where(DeviceCategoryNode.code == body.code)
    ):
        raise HTTPException(status_code=409, detail=f"分类代号 {body.code} 已存在")

    by_id = await _load_category_nodes(db)
    legacy = _validate_legacy_enum(body.legacy_enum)
    if body.parent_id is not None:
        if body.parent_id not in by_id:
            raise HTTPException(status_code=400, detail="父分类不存在")
        # 子节点的 legacy_enum 由父链推导，不接受显式传值
        # （否则会出现两个节点都声称自己是一级分类）
        legacy = None
    else:
        if legacy is None:
            raise HTTPException(
                status_code=422,
                detail="一级分类必须指定 legacy_enum（= 设备库一级分类取值）",
            )
        if any(n.legacy_enum == legacy for n in by_id.values()):
            raise HTTPException(
                status_code=409, detail=f"一级分类 {legacy} 已经有一个节点了"
            )

    node = DeviceCategoryNode(
        code=body.code,
        name=body.name,
        parent_id=body.parent_id,
        sort_order=body.sort_order,
        is_active=body.is_active,
        legacy_enum=legacy,
        code_prefix=body.code_prefix,
        notes=body.notes,
    )
    db.add(node)
    await db.flush()
    await db.refresh(node)
    log.info(f"新增设备分类节点 {node.code} {node.name}")
    return _node_out(node, {**by_id, node.id: node})


@router.patch(
    "/categories/{category_id}",
    response_model=DeviceCategoryNodeOut,
    summary="修改分类节点（改名/挪位置/排序/停用）",
)
async def update_category(
    category_id: UUID,
    body: DeviceCategoryNodeUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "update"))],
) -> DeviceCategoryNodeOut:
    node = await db.get(DeviceCategoryNode, category_id)
    if not node:
        raise HTTPException(status_code=404, detail="分类节点不存在")
    by_id = await _load_category_nodes(db)
    data = body.model_dump(exclude_unset=True)

    if "parent_id" in data:
        new_parent = data["parent_id"]
        if new_parent is not None:
            if new_parent not in by_id:
                raise HTTPException(status_code=400, detail="父分类不存在")
            if new_parent == node.id:
                raise HTTPException(status_code=400, detail="不能把自己设成父分类")
            # 防环：新父节点不能在自己的子树里
            if new_parent in _descendant_ids(node.id, by_id):
                raise HTTPException(
                    status_code=400, detail="不能把节点移动到它自己的子节点下面"
                )
            if node.legacy_enum:
                # 一级节点带 legacy_enum，挪到别人下面就不再是一级了 —— 不允许，
                # 否则 devices.category 与该节点声称的一级分类会不一致
                raise HTTPException(
                    status_code=400,
                    detail="一级分类不能移动到其他分类下面（请新建子节点）",
                )
        node.parent_id = new_parent
    for field in ("name", "sort_order", "is_active", "code_prefix", "notes"):
        if field in data:
            setattr(node, field, data[field])

    await db.flush()
    # 结构变了 → 子树下所有设备的 devices.category 要跟着新的一级分类走
    await _resync_device_categories(db, _descendant_ids(node.id, by_id))
    await db.refresh(node)
    fresh = await _load_category_nodes(db)
    used = await db.scalar(
        select(func.count())
        .select_from(Device)
        .where(Device.category_id == node.id, Device.is_active.is_(True))
    )
    return _node_out(node, fresh, device_count=used or 0)



# ---------------------------------------------------------------------------
# 阶梯价（2026-10-04）
# ---------------------------------------------------------------------------

@router.get(
    "/{device_id}/price-tiers",
    response_model=list[PriceTierOut],
    summary="设备的阶梯价",
)
async def list_price_tiers(
    device_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> list[DevicePriceTier]:
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    rows = (
        await db.scalars(
            select(DevicePriceTier)
            .where(DevicePriceTier.device_id == device_id)
            .order_by(DevicePriceTier.min_qty)
        )
    ).all()
    return list(rows)


@router.put(
    "/{device_id}/price-tiers",
    response_model=list[PriceTierOut],
    summary="整体替换设备的阶梯价（改档位就是改这一组）",
)
async def replace_price_tiers(
    device_id: UUID,
    body: list[PriceTierIn],
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "update"))],
) -> list[DevicePriceTier]:
    """整体替换而不是逐条增删：档位是一组有内在顺序的数据，
    逐条改容易出现"10-49 与 20-30 并存"这种重叠，取档结果就飘了。
    """
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")

    cleaned = sorted(body, key=lambda x: x.min_qty)
    # 重叠检查：相邻两档必须"上一档的上界 < 下一档的下界"
    for prev, cur in zip(cleaned, cleaned[1:], strict=False):
        if prev.max_qty is None:
            raise HTTPException(
                status_code=422,
                detail=f"档位 {prev.min_qty} 起没有上界，后面不能再有档位",
            )
        if Decimal(str(prev.max_qty)) >= Decimal(str(cur.min_qty)):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"档位区间重叠：{prev.min_qty}-{prev.max_qty} 与 {cur.min_qty} 起"
                ),
            )
    for tier in cleaned:
        if tier.max_qty is not None and Decimal(str(tier.max_qty)) < Decimal(str(tier.min_qty)):
            raise HTTPException(status_code=422, detail=f"档位 {tier.min_qty} 的上界小于下界")

    await db.execute(delete(DevicePriceTier).where(DevicePriceTier.device_id == device_id))
    db.expire(device, ["price_tiers"])
    for tier in cleaned:
        db.add(
            DevicePriceTier(
                device_id=device_id,
                min_qty=tier.min_qty,
                max_qty=tier.max_qty,
                unit_price=tier.unit_price,
                note=tier.note,
            )
        )
    await db.flush()
    log.info(f"设备 {device.code} 阶梯价更新为 {len(cleaned)} 档 by {user.username}")
    return list(
        (
            await db.scalars(
                select(DevicePriceTier)
                .where(DevicePriceTier.device_id == device_id)
                .order_by(DevicePriceTier.min_qty)
            )
        ).all()
    )

@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除分类节点（有子节点或有设备时拒绝）",
)
async def delete_category(
    category_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "delete"))],
) -> None:
    node = await db.get(DeviceCategoryNode, category_id)
    if not node:
        raise HTTPException(status_code=404, detail="分类节点不存在")

    # 拒绝而不是"顺手把设备也删了/挪走"：删分类不该动设备数据。
    # 设备表上虽然有 ON DELETE SET NULL 兜底，但那会静默把设备变成"未分类"。
    kids = await db.scalar(
        select(func.count())
        .select_from(DeviceCategoryNode)
        .where(DeviceCategoryNode.parent_id == node.id)
    )
    if kids:
        raise HTTPException(
            status_code=409,
            detail=f"该分类下还有 {kids} 个子分类，请先删除或移走子分类",
        )
    used = await db.scalar(
        select(func.count()).select_from(Device).where(Device.category_id == node.id)
    )
    if used:
        raise HTTPException(
            status_code=409,
            detail=f"该分类下还有 {used} 台设备，请先把设备改到其他分类",
        )
    await db.delete(node)
    log.warning(f"删除设备分类节点 {node.code} {node.name} by {user.username}")


async def _resync_device_categories(
    db: AsyncSession, node_ids: set[UUID] | None = None
) -> int:
    """把设备的一级分类枚举（devices.category）与它在树上的位置对齐。

    分类树动了结构（挪节点）之后，子树下设备的一级分类可能变了 —— 不重算的话
    会出现「设备挂在 门禁 下、category 却写着 video」这种两套真相。
    返回被修正的设备数。
    """
    by_id = await _load_category_nodes(db)
    stmt = select(Device)
    if node_ids:
        stmt = stmt.where(Device.category_id.in_(node_ids))
    fixed = 0
    for device in (await db.scalars(stmt)).all():
        want = _root_enum_of(device.category_id, by_id)
        if want and str(device.category) != want:
            device.category = DeviceCategory(want)
            fixed += 1
    if fixed:
        await db.flush()
        log.info(f"分类结构变更：修正了 {fixed} 台设备的一级分类")
    return fixed



@router.post("", response_model=DeviceOut, status_code=status.HTTP_201_CREATED, summary="新增设备")
async def create_device(
    body: DeviceCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "create"))],
) -> DeviceOut:
    if await db.scalar(select(Device).where(Device.code == body.code)):
        raise HTTPException(status_code=409, detail=f"设备编号 {body.code} 已存在")
    if body.replacement_id and not await db.get(Device, body.replacement_id):
        raise HTTPException(status_code=400, detail="替代型号不存在")

    payload = drop_none_for_not_null(Device, body.model_dump())
    # 挂了分类节点就以节点为准：从它所属的一级分类反推 category，
    # 保证「设备的一级分类」和「它在树上的位置」永远一致。
    if body.category_id is not None:
        by_id = await _load_category_nodes(db)
        if body.category_id not in by_id:
            raise HTTPException(status_code=400, detail="分类节点不存在")
        root_enum = _root_enum_of(body.category_id, by_id)
        if root_enum is None:
            raise HTTPException(
                status_code=400, detail="该分类节点没有关联一级分类，无法确定设备归属"
            )
        payload["category"] = DeviceCategory(root_enum)
    # 设备底价规则（2026-10-05）：没显式填底价 → 按成本价 × 1.35 派生
    if not payload.get("min_price"):
        derived = floor_from_cost(payload.get("cost_price"))
        if derived is not None:
            payload["min_price"] = derived
    device = Device(**payload)
    db.add(device)
    await db.flush()
    await db.refresh(device)
    log.info(f"新增设备 {device.code} {device.name}")
    nodes = await _load_category_nodes(db)
    return _device_out_for_view(
        user, device, category_path=_node_path(device.category_id, nodes)
    )


@router.post(
    "/auto-code",
    summary="按分类生成下一个设备编号（前端新增表单预填）",
)
async def gen_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "create"))],
    category: DeviceCategory = Query(..., description="一级分类"),
    category_id: UUID | None = Query(None, description="分类树节点（优先用它的编号前缀）"),
) -> dict:
    return {
        "code": await _next_device_code(db, category, category_id=category_id)
    }


@router.get("/{device_id}", response_model=DeviceOut, summary="设备详情")
async def get_device(
    device_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> DeviceOut:
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    nodes = await _load_category_nodes(db)
    return _device_out_for_view(
        user, device, category_path=_node_path(device.category_id, nodes)
    )


@router.patch("/{device_id}", response_model=DeviceOut, summary="更新设备")
async def update_device(
    device_id: UUID,
    body: DeviceUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "update"))],
) -> DeviceOut:
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    data = drop_none_for_not_null(Device, body.model_dump(exclude_unset=True))
    # 设备底价规则（2026-10-05）：改了成本价、且这次没显式给底价 → 底价跟着重算
    # （显式给了底价就以调用方的为准：个别机型确实是特例）
    if "cost_price" in data and "min_price" not in data:
        derived = floor_from_cost(data.get("cost_price"))
        if derived is not None:
            data["min_price"] = derived
    if data.get("replacement_id") == device.id:
        raise HTTPException(status_code=400, detail="替代型号不能是自己")
    # 改分类节点 → 一级分类跟着节点走（与 create 同一口径）
    if "category_id" in data and data["category_id"] is not None:
        by_id = await _load_category_nodes(db)
        if data["category_id"] not in by_id:
            raise HTTPException(status_code=400, detail="分类节点不存在")
        root_enum = _root_enum_of(data["category_id"], by_id)
        if root_enum is None:
            raise HTTPException(
                status_code=400, detail="该分类节点没有关联一级分类，无法确定设备归属"
            )
        data["category"] = DeviceCategory(root_enum)
    for key, value in data.items():
        setattr(device, key, value)
    await db.flush()
    await db.refresh(device)

    # 设为停产/临近停产但没填替代型号 → 提示（不阻断）
    if device.status in (DeviceStatus.EOL, DeviceStatus.NEAR_EOL) and not device.replacement_id:
        log.warning(f"设备 {device.code} 标记为 {device.status} 但未指定替代型号")
    nodes = await _load_category_nodes(db)
    return _device_out_for_view(
        user, device, category_path=_node_path(device.category_id, nodes)
    )


@router.delete(
    "/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="停用设备（软删除）",
)
async def deactivate_device(
    device_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "delete"))],
) -> None:
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    device.is_active = False
    log.warning(f"停用设备 {device.code}")


@router.post("/match", summary="设备匹配（精确 → 模糊），场景二用")
async def match(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
    name: str = Query(..., description="甲方清单里的设备名称"),
    model: str | None = Query(None, description="型号（如清单里有）"),
    brand: str | None = Query(None),
    threshold: float = Query(75.0, ge=0, le=100, description="模糊匹配阈值 %"),
    limit: int = Query(5, ge=1, le=20),
) -> dict:
    """返回匹配结果：exact / fuzzy / none + 候选列表 + 停产标记。

    匹配算法见 `app.services.device_match`：
        名称分词 40% + 型号特征 40% + 品牌 20%
    """
    devices = list((await db.scalars(select(Device).where(Device.is_active.is_(True)))).all())
    result = match_device(
        query_name=name,
        query_model=model,
        query_brand=brand,
        candidates=devices,
        threshold=threshold,
        limit=limit,
    )
    return result


@router.get("/{device_id}/replacements", summary="停产设备的替代型号建议")
async def replacements(
    device_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
    limit: int = Query(5, ge=1, le=20),
) -> dict:
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    # 1) 显式配置的替代型号优先
    if device.replacement_id:
        repl = await db.get(Device, device.replacement_id)
        if repl:
            # 两个分支必须回**同一种**候选结构，否则前端要为「配置的」和
            # 「相似度推荐的」写两套取值逻辑（前者曾经回 DeviceOut，用 id；
            # 后者回 MatchCandidate，用 device_id —— 这是踩过的坑）。
            configured = MatchCandidate(
                device=repl,
                score=100.0,
                name_score=1.0,
                model_score=1.0,
                brand_score=1.0,
            ).to_dict()
            configured["source"] = "configured"
            configured["price_delta"] = float(
                (repl.reference_price or 0) - (device.reference_price or 0)
            )
            return {
                "device": _device_out_for_view(user, device).model_dump(mode="json"),
                "source": "configured",
                "candidates": [configured],
            }

    # 2) 按同分类 + 名称/型号相似度推荐在售型号
    peers = list(
        (
            await db.scalars(
                select(Device).where(
                    Device.is_active.is_(True),
                    Device.category == device.category,
                    Device.status == DeviceStatus.ON_SALE,
                    Device.id != device.id,
                )
            )
        ).all()
    )
    cands = suggest_replacements(device, peers, limit=limit)
    for c in cands:
        c["source"] = "similarity"
    return {
        "device": _device_out_for_view(user, device).model_dump(mode="json"),
        "source": "similarity",
        "candidates": cands,
    }











# ---------------------------------------------------------------------------
# 配件 / 授权（2026-10-05）
# ---------------------------------------------------------------------------
# 主设备（如防火墙）可以配若干**槽**（授权/电源模块…），槽里放若干**候选**。
# 候选本身是产品库条目，所以这里返回它们的编码/名称/参考价，供界面直接选。
def _option_out(opt: DeviceAccessoryOption, dev: Device) -> dict:
    return {
        "accessory_id": dev.id,
        "code": dev.code,
        "name": dev.name,
        "brand": dev.brand,
        "model": dev.model,
        "unit": dev.unit,
        "reference_price": dev.reference_price,
        "is_default": bool(opt.is_default),
        "note": opt.note,
        "sort_order": opt.sort_order,
    }


@router.get("/{device_id}/accessories", summary="设备的配件槽与候选（含参考价）")
async def list_device_accessories(
    device_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> list[dict]:
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")
    groups = (
        await db.scalars(
            select(DeviceAccessoryGroup)
            .where(DeviceAccessoryGroup.device_id == device_id)
            .order_by(DeviceAccessoryGroup.sort_order, DeviceAccessoryGroup.label)
        )
    ).all()
    # 一次性把候选设备取出来，避免逐条查（N+1）
    opt_ids = [o.accessory_id for g in groups for o in g.options]
    devs: dict[UUID, Device] = {}
    if opt_ids:
        rows = (await db.scalars(select(Device).where(Device.id.in_(opt_ids)))).all()
        devs = {d.id: d for d in rows}
    return [
        {
            "id": g.id,
            "label": g.label,
            "is_required": g.is_required,
            "select_mode": g.select_mode,
            "quantity_mode": g.quantity_mode,
            "default_quantity": g.default_quantity,
            "note": g.note,
            "sort_order": g.sort_order,
            "is_active": g.is_active,
            "options": [
                _option_out(o, devs[o.accessory_id])
                for o in g.options
                if o.accessory_id in devs
            ],
        }
        for g in groups
    ]


@router.put("/{device_id}/accessories", summary="整体替换设备的配件槽（界面一个保存）")
async def save_device_accessories(
    device_id: UUID,
    body: AccessorySaveIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "update"))],
) -> list[dict]:
    """整体替换：界面编辑完一次提交，语义简单、不会留下半截数据。

    校验（都在这里挡住，别让脏数据进库）：
      · 设备必须存在；
      · 槽名不能为空、同一设备下不能重名；
      · 候选不能是主设备自己（防火墙不该把自己当授权）；
      · 同一个槽里同一个候选不能重复；
      · 候选必须真实存在；
      · 单选槽最多只能有一个默认项。
    """
    device = await db.get(Device, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="设备不存在")

    seen_labels: set[str] = set()
    all_ids = {o.accessory_id for g in body.groups for o in g.options}
    if device_id in all_ids:
        raise HTTPException(status_code=422, detail="配件不能是主设备自己")
    if all_ids:
        found = set((await db.scalars(select(Device.id).where(Device.id.in_(all_ids)))).all())
        missing = all_ids - found
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"有 {len(missing)} 个候选配件不存在（可能已被删除），请刷新后重试",
            )

    # 整体替换：先删旧槽（选项随 CASCADE 一起走）
    await db.execute(
        delete(DeviceAccessoryGroup).where(DeviceAccessoryGroup.device_id == device_id)
    )

    for gi, g in enumerate(body.groups):
        label = g.label.strip()
        if not label:
            raise HTTPException(status_code=422, detail="配件槽的名称不能为空")
        if label in seen_labels:
            raise HTTPException(status_code=422, detail=f"配件槽「{label}」重复了")
        seen_labels.add(label)
        opts, seen_opt = [], set()
        for o in g.options:
            if o.accessory_id in seen_opt:
                raise HTTPException(
                    status_code=422, detail=f"配件槽「{label}」里有重复的候选配件"
                )
            seen_opt.add(o.accessory_id)
            opts.append(o)
        defaults = [o for o in opts if o.is_default]
        if g.select_mode == "one" and len(defaults) > 1:
            raise HTTPException(
                status_code=422, detail=f"配件槽「{label}」是单选，只能有一个默认项"
            )
        group = DeviceAccessoryGroup(
            device_id=device_id, label=label, is_required=g.is_required,
            select_mode=g.select_mode, quantity_mode=g.quantity_mode,
            default_quantity=g.default_quantity, note=g.note,
            sort_order=g.sort_order if g.sort_order else gi, is_active=g.is_active,
        )
        db.add(group)
        await db.flush()          # 拿到 group.id
        for oi, o in enumerate(opts):
            db.add(DeviceAccessoryOption(
                group_id=group.id, accessory_id=o.accessory_id,
                is_default=o.is_default, note=o.note,
                sort_order=o.sort_order if o.sort_order else oi,
            ))
    await db.commit()
    return await list_device_accessories(device_id, db, user)
