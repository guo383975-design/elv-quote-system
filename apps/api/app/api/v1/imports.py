"""Import router: 场景一（自有设备库 Excel）+ 场景二（甲方询价清单）."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import quote as urlquote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.device_tree import CODE_PREFIX_BY_CATEGORY
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.device import Device, DeviceCategory, DeviceCategoryNode, DeviceStatus
from app.models.import_batch import ImportBatch, ImportKind, ImportStatus
from app.models.user import User
from app.services.device_accessories import (
    fix_category_leaf,
    host_model_of,
    is_license_model,
    license_display_name,
    match_host_by_name,
    pick_default_license,
)
from app.services.device_category import auto_node_code, resolve_category
from app.services.device_match import match_device
from app.services.excel_io import (
    DEVICE_TEMPLATE_COLUMNS,
    INQUIRY_TEMPLATE_COLUMNS,
    detect_columns,
    parse_device_sheet,
    parse_inquiry_sheet,
)
from app.services.import_batch import record_batch
from app.services.pricing import floor_from_cost

router = APIRouter(prefix="/imports", tags=["批量导入"])
log = get_logger("api.imports")

# 单个文件的数据行上限（超过直接 422，不做"悄悄截断"）
MAX_ROWS = 5000

# 上传体积上限。nginx 侧是 client_max_body_size 25m（留了余量），
# 但直连 8000 可以绕过 nginx，所以后端自己也必须挡。
MAX_UPLOAD_BYTES = 20 * 1024 * 1024

# 询价清单"一次能预览/能匹配多少行"的上限。
# 为什么单独一个更小的数：链路 B 的匹配是 O(行 × 在售设备数) 的纯 CPU 计算，
# 实测（本机，device_match.match_device）：
#     36 台设备 × 200 行 → 0.14 s（0.7 ms/行）
#     500 台 × 200 行 → 2.1 s（10.4 ms/行）
#     2000 台 × 200 行 → 8.5 s（42.3 ms/行）
# 取 500 意味着最坏情况（2000 台设备）约 21 s —— 慢，但**是有限且可预期的**。
# 关键不是这个数多大，而是超过它时必须**说出来并阻断提交**：
# 以前前端硬编码 limit=50，200 行的清单静默入库 50 行还提示成功。
INQUIRY_MATCH_MAX_ROWS = 500


# ---------------------------------------------------------------------------
# 场景一：导入自有设备库
# ---------------------------------------------------------------------------

def _template_xlsx(
    columns: list[str], sheet_name: str, sample: list[object], filename: str
) -> Response:
    """生成**真 .xlsx** 模板（表头 + 一行示例）。

    2026-10-04 修 M-19：以前只回列定义、由前端拼一个 .csv 下载，
    而上传口只收 .xlsx —— 用户照着"下载的模板"填完**根本传不回去**。
    现在后端直接出 xlsx，与导入解析、设备库导出**用的是同一份列定义**。
    """
    from io import BytesIO

    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    ws.append(columns)
    if sample:
        ws.append(sample)
    for idx in range(1, len(columns) + 1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = 16
    buffer = BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    return Response(
        content=buffer.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{filename}"; '
                f"filename*=UTF-8''{urlquote(sheet_name + '导入模板.xlsx', safe='')}"
            )
        },
    )


@router.get("/device-template", summary="下载设备库导入模板列定义")
async def device_template(
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> dict:
    return {"columns": DEVICE_TEMPLATE_COLUMNS, "sheet_name": "设备清单"}


@router.get("/device-template.xlsx", summary="下载设备库导入模板（真 xlsx，可直接填完导回）")
async def device_template_xlsx(
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> Response:
    # 示例行只作格式示范：型号写 EXAMPLE-001，避免用户忘了改就真的导进来
    return _template_xlsx(
        DEVICE_TEMPLATE_COLUMNS,
        "设备清单",
        ["网络高清枪型摄像机（示例，可删除）", "海康威视", "EXAMPLE-001", "视频监控/摄像机",
         "台", 680, "宁波XX安防科技", 1, 2, "否", "在售", ""],
        "device_import_template.xlsx",
    )


@router.post("/devices/preview", summary="场景一：解析 + 校验（不写库）")
async def preview_device_import(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "create"))],
    file: Annotated[UploadFile, File(description="Excel 文件 .xlsx")],
    duplicate_policy: Annotated[str, Form()] = "skip",
    max_rows: Annotated[int, Form()] = 100,
    # 安装单价兜底（2026-10-05）：文件没写安装单价时用；授权一律 0
    install_price_default: Annotated[float, Form()] = 300.0,
    link_accessories: Annotated[bool, Form()] = True,
) -> dict:
    """解析 Excel，逐行校验，返回预览 + 统计。**不写库**。

    校验规则：
      - 设备名称/型号/分类 必填
      - 型号在库中已存在 → 按 duplicate_policy 处理（skip/overwrite/new）
      - 库中该型号状态为 eol/near_eol → 标记「停产型号」警告
      - 参考单价非数字 → 错误
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
        )

    try:
        rows, total = parse_device_sheet(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Excel 解析失败: {e}") from e

    if total > MAX_ROWS:
        raise HTTPException(status_code=422, detail=f"超过 {MAX_ROWS} 行上限")

    # 现存型号索引
    existing = {
        (d.model or "").strip().upper(): d
        for d in (await db.scalars(select(Device))).all()
        if d.model
    }

    preview: list[dict] = []
    stat = {"ok": 0, "warn": 0, "error": 0, "discontinued": 0, "duplicate": 0}

    # 分类树只读一次：每行都要解析分类路径（可能是多级）
    cat_nodes = await _load_category_nodes(db)
    enum_to_root = _enum_to_root_map(cat_nodes)

    for idx, r in enumerate(rows[: max(1, max_rows)], start=2):  # 表头占第 1 行
        # problems 决定这一行的级别（error/warn）；notes 只是给人看的提示，
        # 不影响级别 —— 以前两者混在一个 issues 里，于是"将新建分类"这种
        # 正常提示也会把整行降级成 warn（"全部正常"永远为 0）。
        problems: list[str] = []
        notes: list[str] = []
        level = "ok"

        name = (r.get("name") or "").strip()
        model = (r.get("model") or "").strip()

        # ---- 固化规则（2026-10-05，实现处 services/device_accessories）----
        # ① 按型号判定主机/授权，**不看**源表「产品类型」列
        #    （那列会把 LIC-* 标成"软件"，上一轮就是因此把授权放进了主机节点）
        is_lic = is_license_model(model, name)
        # ② 授权名按型号重新生成：TP 类的年限写在括号后面，用源描述会丢年限
        gen_name = license_display_name(model)
        if gen_name and gen_name != name:
            if name:
                notes.append(f"授权名已规范为「{gen_name}」")
            name = gen_name
        category_raw = (r.get("category_path") or r.get("category") or "").strip()
        # ③ 分类叶子在「主机 ↔ 授权」之间自动纠正
        fixed_path, fix_note = fix_category_leaf(category_raw, is_license=is_lic)
        if fix_note:
            notes.append(fix_note)
            category_raw = fixed_path

        if not name:
            problems.append("设备名称必填")
        if not model:
            problems.append("规格型号必填")

        # 分类：支持「视频监控/摄像机」这种多级路径（见 services/device_category.py）
        resolution = None
        category = None
        if not category_raw:
            problems.append("分类必填")
        else:
            resolution = resolve_category(
                category_raw, nodes=cat_nodes, enum_to_root=enum_to_root
            )
            if not resolution.ok:
                problems.append(f"分类「{category_raw}」无法识别")
                level = "warn"
            else:
                category = DeviceCategory(resolution.root_enum)
                if resolution.will_create:
                    # 按用户写的层级落树是预期行为，不是数据问题 → 只提示、不降级
                    notes.append("将新建分类：" + " / ".join(resolution.missing))

        price = None
        if r.get("reference_price") not in (None, ""):
            try:
                price = Decimal(str(r["reference_price"]))
                if price < 0:
                    problems.append("参考单价为负")
            except (InvalidOperation, ValueError):
                problems.append("参考单价不是数字")

        if problems:
            hard = any("必填" in i or "不是数字" in i or "为负" in i for i in problems)
            level = "error" if hard else "warn"

        # 型号已存在？
        dup = existing.get(model.upper()) if model else None
        if dup:
            stat["duplicate"] += 1
            if duplicate_policy == "overwrite":
                notes.append(f"将覆盖库中已有 {dup.code}")
            elif duplicate_policy == "new":
                notes.append("将作为新型号新增")
            else:
                notes.append(f"库中已存在 {dup.code}，跳过")
            level = max(level, "warn", key={"ok": 0, "warn": 1, "error": 2}.get)

        # 停产型号（库中该型号已停产）
        if dup and str(dup.status) in ("eol", "near_eol"):
            notes.append("停产型号")
            stat["discontinued"] += 1
            level = "warn" if level != "error" else level

        issues = problems + notes
        stat[{"ok": "ok", "warn": "warn", "error": "error"}[level]] += 1
        preview.append(
            {
                "row": idx,
                "name": name,
                "brand": (r.get("brand") or "").strip(),
                "model": model,
                "category": str(category) if category else category_raw,
                # 前端要显示"这条设备会落到哪个分类"，以及会不会新建子分类
                "category_path": (
                    resolution.node_path if resolution and resolution.ok else category_raw
                ),
                "category_new_nodes": (
                    resolution.missing if resolution and resolution.ok else []
                ),
                "unit": (r.get("unit") or "台").strip(),
                "reference_price": float(price) if price is not None else None,
                "status_in_db": str(dup.status) if dup else None,
                "existing_code": dup.code if dup else None,
                "level": level,
                "issues": issues,
            }
        )

    columns_info = detect_columns(raw)

    return {
        "file_name": file.filename,
        "sheet_rows": total,
        "preview_count": len(preview),
        "stat": stat,
        "preview": preview,
        "duplicate_policy": duplicate_policy,
        # 前端要拿它提示「未识别的列」。不返回的话前端 undefined.length 会崩。
        # 顺带把「字段映射」也回给前端（原型第 6 页左侧那栏）：
        # excel_io.detect_columns 早就写好了，但一直没有任何路由用它。
        "unmapped": columns_info["unmapped"],
        "mapping": columns_info["columns"],
        "header_row": columns_info["header_row"],
    }


def _compose_description(r: dict) -> str | None:
    """设备描述 = 文件里的描述 +（可选的）附注。

    源表常有「保卡类型」这类零散列 ✗（Device 上没有独立字段），
    并入描述比丢掉好：选产品悬停时就能看到完整信息。
    """
    parts = [(r.get("description") or "").strip()]
    extra = (r.get("notes") or "").strip()
    if extra:
        parts.append(f"保卡类型：{extra}")
    text = "\n".join(p for p in parts if p)
    return text[:4000] or None


async def _link_licenses_to_hosts(db: AsyncSession) -> tuple[int, int]:
    """把库里的**授权**按型号/名称挂到同名主机下（幂等）。

    匹配口径（用户 2026-10-05 明确："根据授权名称，绑定授权到同名称主机名下"）：
      · 从授权型号推主机型号：`LIC-USG6000F-S112-ATP-3Y` → `USG6000F-S112`
      · 主机 **model 完全相等** 或 **name 包含该型号** 即命中 ——
        只比 model 会全部落空（源表把「型号+描述」写进了型号列，真实踩坑）。
      · 命中后重建该主机的「授权」槽：必配 · 多选 · 随主设备数量，
        默认项优先 `ATP-3Y`（原来是 TP-1Y，用户改为 ATP-3Y）。

    返回 (挂靠的主机数, 挂上的授权数)。
    """
    from app.models.device import DeviceAccessoryGroup, DeviceAccessoryOption

    devs = list((await db.scalars(select(Device))).all())
    hosts = [d for d in devs if not is_license_model(d.model or "", d.name or "")]
    lics = [d for d in devs if is_license_model(d.model or "", d.name or "")]
    if not lics:
        return 0, 0

    by_model = {(h.model or "").strip(): h for h in hosts if (h.model or "").strip()}
    plan: dict[str, tuple[Device, list[Device]]] = {}
    for lic in lics:
        hm = host_model_of(lic.model or "")
        if not hm:
            continue
        host = by_model.get(hm) or next(
            (h for h in hosts if hm in (h.name or "")), None
        )
        if host is None:
            # 兜底：按名称匹配（锐捷这类编号不带主机型号的厂商靠这条）
            host = match_host_by_name(lic.model or "", lic.name or "", hosts)
        if host is None:
            continue
        entry = plan.setdefault(str(host.id), (host, []))
        entry[1].append(lic)

    # 先删掉这些主机上的旧槽（保留其它类型槽由业务决定，这里只重建「授权」）
    for host, items in plan.values():
        old = list(
            (
                await db.scalars(
                    select(DeviceAccessoryGroup).where(
                        DeviceAccessoryGroup.device_id == host.id,
                        DeviceAccessoryGroup.label == "授权",
                    )
                )
            ).all()
        )
        for g in old:
            await db.delete(g)
        await db.flush()

        models = [(i.model or "").strip() for i in items]
        default_model = pick_default_license(models)
        group = DeviceAccessoryGroup(
            device_id=host.id, label="授权", is_required=True, select_mode="many",
            quantity_mode="per_unit", default_quantity=Decimal("1"),
            note="按授权型号自动挂靠", sort_order=0, is_active=True,
        )
        db.add(group)
        await db.flush()
        for i, lic in enumerate(items):
            db.add(DeviceAccessoryOption(
                group_id=group.id, accessory_id=lic.id,
                is_default=(lic.model or "").strip() == default_model,
                note=None, sort_order=i,
            ))
    await db.flush()
    return len(plan), sum(len(v[1]) for v in plan.values())


@router.post(
    "/devices/commit",
    status_code=status.HTTP_201_CREATED,
    summary="场景一：确认导入（写库）",
)
async def commit_device_import(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "create"))],
    file: Annotated[UploadFile, File()],
    duplicate_policy: Annotated[str, Form()] = "skip",
    skip_errors: Annotated[bool, Form()] = True,
    # 安装单价兜底（2026-10-05）：文件没写安装单价时用；授权一律 0
    install_price_default: Annotated[float, Form()] = 300.0,
    # 落库后自动把授权挂到同名主机下（2026-10-05 固化）
    link_accessories: Annotated[bool, Form()] = True,
) -> dict:
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
        )

    try:
        rows, total = parse_device_sheet(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Excel 解析失败: {e}") from e

    if total > MAX_ROWS:
        raise HTTPException(status_code=422, detail=f"超过 {MAX_ROWS} 行上限")

    existing = {
        (d.model or "").strip().upper(): d
        for d in (await db.scalars(select(Device))).all()
        if d.model
    }

    created = updated = skipped = errors = 0
    created_categories: list[str] = []
    # 失败明细（#IM 批次留档用）：最多记 50 条，够用户当场核对，不撑爆一行
    error_rows: list[dict] = []
    cat_nodes = await _load_category_nodes(db)
    enum_to_root = _enum_to_root_map(cat_nodes)

    for idx, r in enumerate(rows, start=2):
        name = (r.get("name") or "").strip()
        model = (r.get("model") or "").strip()
        # ---- 固化规则（与预览同一套实现处）----
        # 按型号判主机/授权 + 授权名规范化 + 分类叶子纠正
        is_lic = is_license_model(model, name)
        gen_name = license_display_name(model)
        if gen_name:
            name = gen_name
        if not name or not model:
            errors += 1
            if len(error_rows) < 50:
                missing = "、".join(
                    k for k, v in (("名称", name), ("型号", model)) if not v
                )
                error_rows.append({"row": idx, "message": f"必填项为空（{missing}）"})
            if not skip_errors:
                raise HTTPException(status_code=422, detail=f"第 {idx} 行必填项为空")
            continue

        # 分类：多级路径按树解析；树上没有的子分类**自动建**（否则"不要都写在一起"
        # 就又变成一句空话 —— 用户写好的层级被压回一级分类里）。
        # 固化规则③：授权/主机的叶子自动纠正（源表常把 LIC-* 放在"主机"节点下）
        category_raw = (r.get("category_path") or r.get("category") or "").strip()
        category_raw, _ = fix_category_leaf(category_raw, is_license=is_lic)
        resolution = resolve_category(
            category_raw, nodes=cat_nodes, enum_to_root=enum_to_root
        )
        category = (
            DeviceCategory(resolution.root_enum)
            if resolution.ok
            else DeviceCategory.ACCESSORY
        )
        category_id = resolution.node_id if resolution.ok else None
        if resolution.ok and resolution.will_create:
            category_id = await _ensure_category_path(
                db, resolution, cat_nodes, created_categories
            )
        try:
            price = Decimal(str(r.get("reference_price") or 0))
        except (InvalidOperation, ValueError):
            price = Decimal("0")

        # 进价：优先文件里的（进价/工程提货价列），没有才退回"参考价×0.75"估算
        cost: Decimal | None = None
        raw_cost = r.get("cost_price")
        if raw_cost not in (None, ""):
            try:
                cost = Decimal(str(raw_cost))
            except (InvalidOperation, ValueError):
                cost = None
        # 安装单价：文件优先；授权一律 0（软件许可没有安装工）
        install: Decimal | None = None
        raw_install = r.get("install_unit_price")
        if raw_install not in (None, ""):
            try:
                install = Decimal(str(raw_install))
            except (InvalidOperation, ValueError):
                install = None
        if install is None:
            install = Decimal("0") if is_lic else Decimal(str(install_price_default))

        key = model.upper()
        if key in existing:
            if duplicate_policy == "skip":
                skipped += 1
                continue
            if duplicate_policy == "overwrite":
                d = existing[key]
                d.name = name
                d.brand = (r.get("brand") or "").strip() or d.brand
                d.unit = (r.get("unit") or "台").strip() or d.unit
                d.reference_price = price
                if cost is not None:
                    d.cost_price = cost
                if install is not None:
                    d.install_unit_price = install
                if (r.get("description") or "").strip():
                    d.description = (r.get("description") or "").strip()
                # 分类也按导入内容更新（否则"改了分类重导一次"看起来成功了却没变）
                d.category = category
                d.category_id = category_id
                updated += 1
                continue
            # new → fall through to insert

        code = await _next_code(db, category, category_id=category_id)
        d = Device(
            code=code,
            name=name,
            brand=(r.get("brand") or "").strip() or None,
            model=model,
            spec=(r.get("spec") or "").strip() or None,
            unit=(r.get("unit") or "台").strip() or "台",
            category=category,
            category_id=category_id,
            reference_price=price,
            # 进价：文件有就用文件的，没有才退回估算
            cost_price=cost if cost is not None else price * Decimal("0.75"),
            install_unit_price=install,
            # 设备底价规则（2026-10-05）：按成本价 × 1.35 派生（与建/改设备同一口径）
            min_price=floor_from_cost(cost if cost is not None else price * Decimal("0.75")),
            description=_compose_description(r),
            status=DeviceStatus.ON_SALE,
            is_owner_supplied=_truthy(r.get("is_owner_supplied")),
            moq=_as_int(r.get("moq"), 1),
            warranty_years=_as_int(r.get("warranty_years"), 2),
        )
        db.add(d)
        existing[key] = d
        created += 1
        # 立刻 flush：_next_code() 依赖 SELECT max(code) 看到前一行，
        # 否则同一分类的多行会拿到相同编号。
        await db.flush()

    await db.flush()
    # ---- 固化规则④：落库后把授权挂到同名主机下（2026-10-05）----
    # 以前这步是我写脚本手工跑的；现在导入即完成，且幂等（可重复导入/重跑）。
    linked_hosts = linked_lics = 0
    if link_accessories:
        linked_hosts, linked_lics = await _link_licenses_to_hosts(db)
    batch = await record_batch(
        db,
        kind=ImportKind.DEVICE,
        user=user,
        filename=file.filename,
        file_size=len(raw),
        total_rows=total,
        created=created,
        updated=updated,
        skipped=skipped,
        errors=error_rows,
        summary={
            "duplicate_policy": duplicate_policy,
            "skip_errors": skip_errors,
            "created_categories": created_categories,
            "linked_hosts": linked_hosts,
            "linked_licenses": linked_lics,
        },
    )
    log.info(
        f"设备库导入完成 by {user.username}（批次 {batch.batch_no}）: "
        f"新增 {created} 更新 {updated} 跳过 {skipped} 错误 {errors}"
        + (f"；新建分类 {len(created_categories)} 个" if created_categories else "")
    )
    return {
        "batch_no": batch.batch_no,
        "sheet_rows": total,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "created_categories": created_categories,
        # 自动挂靠结果（2026-10-05 固化）：授权按型号挂到同名主机下
        "linked_hosts": linked_hosts,
        "linked_licenses": linked_lics,
    }


# ---------------------------------------------------------------------------
# 场景二：导入甲方询价清单
# ---------------------------------------------------------------------------

@router.get("/inquiry-template", summary="下载甲方清单模板列定义")
async def inquiry_template(
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> dict:
    return {"columns": INQUIRY_TEMPLATE_COLUMNS, "sheet_name": "询价清单"}


@router.get("/inquiry-template.xlsx", summary="下载甲方清单导入模板（真 xlsx）")
async def inquiry_template_xlsx(
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> Response:
    return _template_xlsx(
        INQUIRY_TEMPLATE_COLUMNS,
        "询价清单",
        ["网络高清枪型摄像机（示例，可删除）", "DS-2CD2T46WD-I5", "海康威视", 10, "台", ""],
        "inquiry_import_template.xlsx",
    )


@router.post("/inquiry/preview", summary="场景二：解析 + 智能匹配（不写库）")
async def preview_inquiry(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
    file: Annotated[UploadFile, File()],
    threshold: Annotated[float, Form()] = 75.0,
    limit: Annotated[int, Form()] = INQUIRY_MATCH_MAX_ROWS,
) -> dict:
    """解析甲方清单并逐行匹配。**不写库**。

    ⚠ 本接口只返回前 `limit` 行的匹配结果，而 commit 只写回调用方提交的行。
    因此**必须**用返回的 `truncated` / `total_rows` 判断有没有丢行：
    截断为真时不允许提交（前端会禁用"写入"按钮），否则就是"少导了一半还提示成功"。
    这些字段是 2026-09-30 补的 —— 在这之前前端硬编码 limit=50，而后端默认 100，
    200 行的清单会静默丢掉 150 行。
    """
    raw = await file.read()
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413, detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
        )

    try:
        rows, total = parse_inquiry_sheet(raw)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Excel 解析失败: {e}") from e

    if total > MAX_ROWS:
        raise HTTPException(
            status_code=422,
            detail=f"清单有 {total} 行，超过 {MAX_ROWS} 行上限；请先拆分文件",
        )

    devices = list(
        (await db.scalars(select(Device).where(Device.is_active.is_(True)))).all()
    )

    results: list[dict] = []
    stat = {"exact": 0, "fuzzy": 0, "none": 0, "discontinued": 0}

    previewed = rows[: max(1, min(limit, INQUIRY_MATCH_MAX_ROWS))]
    for idx, r in enumerate(previewed, start=2):
        name = (r.get("name") or "").strip()
        model = (r.get("model") or "").strip() or None
        brand = (r.get("brand") or "").strip() or None
        qty = r.get("quantity") or 0

        m = match_device(
            query_name=name,
            query_model=model,
            query_brand=brand,
            candidates=devices,
            threshold=threshold,
        )
        stat[m["match_type"]] += 1
        m["row"] = idx
        m["quantity"] = _as_float(qty, 0.0)
        m["raw_name"] = name
        m["raw_model"] = model
        # 甲方清单里的单位（原型第 7 页左栏"原文"表有这列）
        m["unit"] = ((r.get("unit") or "").strip() or None) if isinstance(r, dict) else None

        # 逐行的校验结论。前端预览表要显示「提示」一列，
        # 以前一次都没返回过 issues/level，前端 undefined.length 直接崩。
        issues: list[str] = []
        if m["match_type"] == "none":
            level = "error"
            issues.append("未匹配到设备库型号，将按「待询价」（单价 0）写入，需人工补价")
        elif m["match_type"] == "fuzzy":
            level = "warn"
            best = m.get("best") or {}
            issues.append(f"模糊匹配（{best.get('score', '?')} 分），请人工确认是否为同一型号")
        else:
            level = "ok"

        if not model:
            issues.append("清单里没有型号，仅按名称/品牌匹配，准确度会下降")

        best = m.get("best") or {}
        if best.get("is_discontinued"):
            issues.append("命中已停产型号，写入时会自动换成替代型号")
            if level != "error":
                level = "warn"

        m["level"] = level
        m["issues"] = issues

        if m.get("discontinued_count"):
            stat["discontinued"] += m["discontinued_count"]
        results.append(m)

    return {
        "file_name": file.filename,
        # sheet_rows 保留（老前端在用），但语义是"数据行总数"、不是"表头行号"，
        # 而 total_rows/previewed_rows/truncated 才是判断有没有丢行的依据。
        "sheet_rows": total,
        "total_rows": total,
        "previewed_rows": len(results),
        "truncated": len(results) < total,
        "stat": stat,
        "results": results,
        "threshold": threshold,
    }


@router.post(
    "/inquiry/commit",
    status_code=status.HTTP_201_CREATED,
    summary="场景二：把确认后的匹配结果加入报价单",
)
async def commit_inquiry(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote_item", "create"))],
    quote_id: Annotated[UUID, Form()],
    payload: Annotated[
        str,
        Form(description="JSON: [{row, device_id?, name, model, quantity, subsystem_id?}]"),
    ],
) -> dict:
    """把前端确认过的行写入报价单。未匹配行以「待询价」占位（unit_price=0）。"""
    import json

    from sqlalchemy.orm import selectinload

    from app.models.quote import Quote
    from app.models.quote_item import ItemSource, QuoteItem

    quote = await db.scalar(
        select(Quote).options(selectinload(Quote.items)).where(Quote.id == quote_id)
    )
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")

    try:
        lines = json.loads(payload)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=422, detail=f"payload 不是合法 JSON: {e}") from e

    if not isinstance(lines, list):
        raise HTTPException(status_code=422, detail="payload 必须是数组")
    if not lines:
        raise HTTPException(status_code=422, detail="没有任何要写入的行")
    # 兜住"前端把整个文件都塞进来"的情况：这里的上限与 preview 的匹配上限一致，
    # 因为调用方能确认的行不可能多于 preview 返回的行。
    if len(lines) > MAX_ROWS:
        raise HTTPException(
            status_code=422, detail=f"一次最多写入 {MAX_ROWS} 行，收到 {len(lines)} 行"
        )

    added = pending = interrupted = 0
    for line in lines:
        # payload 是前端 JSON.stringify 出来的，id 都是字符串，必须先转 UUID。
        requested_id = _as_uuid(line.get("device_id"))
        device = None
        if requested_id:
            device = await db.get(Device, requested_id)

        replaced = False
        if device and str(device.status) in ("eol", "near_eol") and device.replacement_id:
            device = await db.get(Device, device.replacement_id)
            interrupted += 1
            replaced = True

        item = QuoteItem(
            quote_id=quote.id,
            subsystem_id=_as_uuid(line.get("subsystem_id")),
            device_id=device.id if device else None,
            name=device.name if device else (line.get("name") or "待询价"),
            brand=device.brand if device else line.get("brand"),
            model=device.model if device else line.get("model"),
            unit=device.unit if device else "台",
            # 同理：客户清单里的数量可能是「86 台」这种，直接 Decimal(str(...))
            # 会抛 InvalidOperation → 500，整批导入失败。
            quantity=Decimal(str(_as_float(line.get("quantity"), 0.0))),
            unit_price=device.reference_price if device else Decimal("0"),
            install_unit_price=device.install_unit_price if device else Decimal("0"),
            # 辅材「按工程量」口径要用（2026-10-04）
            auxiliary_unit_price=(
                (device.auxiliary_unit_price or Decimal("0")) if device else Decimal("0")
            ),
            cost_price=device.cost_price if device else Decimal("0"),
            source=ItemSource.IMPORTED if device else ItemSource.MANUAL,
            # 只在「确实换了替代型号」时为 True。
            # 以前写成 device.id != line.get("device_id")：左边是 UUID、右边是
            # JSON 里的字符串，UUID.__eq__(str) 返回 NotImplemented，于是 != 恒为 True，
            # 每一行正常匹配的设备都被打上「已替换」标记。
            is_replaced=replaced,
        )
        db.add(item)
        if device:
            added += 1
        else:
            pending += 1

    await db.flush()
    await db.refresh(quote, ["items"])

    from app.api.v1.quotes import _recompute

    await _recompute(db, quote, actor_id=user.id)
    # 批次留档（#IM）：added = 匹配到设备直接落价；pending = 待询价占位；
    # auto_replaced = 因为原型号停产而自动换成了替代型号（这几条最容易引起争议，
    # 事后必须能查到"这一批里换过哪几个"）。
    batch = await record_batch(
        db,
        kind=ImportKind.INQUIRY,
        user=user,
        filename=None,                      # 场景二是前端确认后回传，没有原始文件
        file_size=len(payload.encode("utf-8")),
        total_rows=len(lines),
        created=added,
        skipped=pending,
        errors=[],
        summary={
            "quote_id": str(quote.id),
            "quote_code": quote.code,
            "pending_inquiry": pending,
            "auto_replaced": interrupted,
        },
    )
    return {
        "batch_no": batch.batch_no,
        "added": added,
        "pending_inquiry": pending,
        "auto_replaced": interrupted,
        "total_with_tax": float(quote.total_with_tax),
    }


# ---------------------------------------------------------------------------
# 导入批次留档（原型里的 #IM-xxx）
# ---------------------------------------------------------------------------

@router.get("/batches", summary="导入批次列表（谁、什么时候、导了什么、成败多少）")
async def list_import_batches(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
    kind: str | None = None,
    status_filter: str | None = None,
    limit: int = 50,
) -> dict:
    stmt = select(ImportBatch).order_by(ImportBatch.created_at.desc())
    if kind:
        try:
            stmt = stmt.where(ImportBatch.kind == ImportKind(kind))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"未知导入类型: {kind}") from None
    if status_filter:
        try:
            stmt = stmt.where(ImportBatch.status == ImportStatus(status_filter))
        except ValueError:
            raise HTTPException(status_code=422, detail=f"未知状态: {status_filter}") from None
    rows = (await db.scalars(stmt.limit(max(1, min(limit, 200))))).all()
    return {
        "items": [
            {
                "id": str(b.id),
                "batch_no": b.batch_no,
                "kind": str(b.kind),
                "filename": b.filename,
                "file_size": b.file_size,
                "total_rows": b.total_rows,
                "created_rows": b.created_rows,
                "updated_rows": b.updated_rows,
                "skipped_rows": b.skipped_rows,
                "error_rows": b.error_rows,
                "status": str(b.status),
                "summary": b.summary or {},
                "created_by_name": b.created_by_name,
                "created_at": b.created_at.isoformat() if b.created_at else None,
            }
            for b in rows
        ]
    }


@router.get("/batches/{batch_id}", summary="导入批次明细（含失败行）")
async def get_import_batch(
    batch_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "read"))],
) -> dict:
    b = await db.get(ImportBatch, batch_id)
    if b is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return {
        "id": str(b.id),
        "batch_no": b.batch_no,
        "kind": str(b.kind),
        "filename": b.filename,
        "file_size": b.file_size,
        "total_rows": b.total_rows,
        "created_rows": b.created_rows,
        "updated_rows": b.updated_rows,
        "skipped_rows": b.skipped_rows,
        "error_rows": b.error_rows,
        "status": str(b.status),
        "summary": b.summary or {},
        "errors": b.errors or [],
        "created_by_name": b.created_by_name,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# 一级分类的模糊词表搬到 services/device_category.py 了（预览与落库共用同一份，
# 否则会出现"预览说能识别、落库却落到别处"）。这里不再保留第二份别名表。


async def _load_category_nodes(db: AsyncSession) -> dict[UUID, DeviceCategoryNode]:
    rows = await db.scalars(select(DeviceCategoryNode))
    return {n.id: n for n in rows.all()}


def _enum_to_root_map(nodes: dict[UUID, DeviceCategoryNode]) -> dict[str, UUID]:
    """{一级分类枚举值: 根节点id}，供分类路径解析用。"""
    return {
        str(n.legacy_enum): n.id
        for n in nodes.values()
        if n.parent_id is None and n.legacy_enum
    }


async def _ensure_category_path(
    db: AsyncSession,
    resolution,
    nodes: dict[UUID, DeviceCategoryNode],
    created_log: list[str],
) -> UUID | None:
    """把解析结果里"树上还没有"的层级建出来，返回最深那个节点的 id。

    · 连一级节点都还没有（新库/没跑迁移建根的库）→ 先按 legacy_enum 建根；
    · 之后逐层建子节点，代号用 `auto_node_code(父代号, 名字)`（内容哈希）——
      同一条 Excel 反复导入必须落到同一个节点，否则每导一次就长出一批重复分类。
    """
    parent_id = resolution.node_id
    missing: list[str] = list(resolution.missing)

    if parent_id is None:
        # 根节点也要建：devices.category 是 NOT NULL，没有根就没有归属
        root_enum = resolution.root_enum
        if not root_enum or not missing:
            return None
        root_name = missing.pop(0)
        root = await db.scalar(
            select(DeviceCategoryNode).where(DeviceCategoryNode.legacy_enum == root_enum)
        )
        if root is None:
            sort_order = len([n for n in nodes.values() if n.parent_id is None])
            root = DeviceCategoryNode(
                code=root_enum,
                name=root_name,
                parent_id=None,
                sort_order=sort_order,
                is_active=True,
                legacy_enum=root_enum,
                code_prefix=CODE_PREFIX_BY_CATEGORY.get(root_enum),
            )
            db.add(root)
            await db.flush()
            log.info(f"导入时新建设备一级分类：{root_name}（{root_enum}）")
            created_log.append(root_name)
        nodes[root.id] = root
        parent_id = root.id

    for name in missing:
        parent = nodes.get(parent_id)
        if parent is None:  # pragma: no cover - 上面保证 parent 存在
            return None
        # 幂等：同父同名已存在就直接用（上一次导入可能建过）
        child = await db.scalar(
            select(DeviceCategoryNode).where(
                DeviceCategoryNode.parent_id == parent_id,
                DeviceCategoryNode.name == name,
            )
        )
        if child is None:
            code = auto_node_code(parent.code, name)
            if await db.scalar(
                select(DeviceCategoryNode).where(DeviceCategoryNode.code == code)
            ):
                code = f"{code}-{len(created_log) + 1}"
            child = DeviceCategoryNode(
                code=code,
                name=name,
                parent_id=parent_id,
                sort_order=99,
                is_active=True,
            )
            db.add(child)
            await db.flush()
            log.info(f"导入时新建设备分类：{parent.name} / {name}（code={code}）")
            created_log.append(f"{parent.name} / {name}")
        nodes[child.id] = child
        parent_id = child.id

    return parent_id


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "y", "yes", "是", "甲供")


def _as_int(value: object, default: int) -> int:
    """表格里的整数列要容忍脏数据。

    造价员手填的 Excel 里「起订量」写成「10台」「十」、质保写成「3年」都很常见；
    int("10台") 抛 ValueError，冒到全局 handler 就是 500，
    整个导入批次白干 —— 而降级成默认值只是丢一个次要字段。
    """
    if value is None or str(value).strip() == "":
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        # "10台" / "3.0" 这类：抠出开头的数字
        m = re.match(r"[-+]?\d+(?:\.\d+)?", str(value).strip())
        if m:
            try:
                return int(float(m.group(0)))
            except (TypeError, ValueError):
                return default
        return default


def _as_float(value: object, default: float) -> float:
    """同上，用于数量列（"86 台" / "1,200"）。"""
    if value is None or str(value).strip() == "":
        return default
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        m = re.search(r"[-+]?\d+(?:\.\d+)?", str(value).replace(",", ""))
        if m:
            try:
                return float(m.group(0))
            except (TypeError, ValueError):
                return default
        return default


def _as_uuid(value: object) -> UUID | None:
    """把 JSON payload 里的字符串 id 转成真正的 UUID。

    为什么必须显式转：`UUIDType = Uuid(as_uuid=True)` 的绑定处理器会直接取
    `value.hex`，传入 str 就抛
        AttributeError: 'str' object has no attribute 'hex'
    （SQLite 上必现；PostgreSQL 原生 uuid 恰好容忍字符串，于是这个坑只在测试环境
    暴露，属于「测试和线上行为不一致」的典型）。顺带把非法 UUID 降级成 None，
    而不是让它冒成 500。
    """
    if value is None:
        return None
    if isinstance(value, UUID):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return UUID(text)
    except (ValueError, AttributeError, TypeError):
        return None


async def _next_code(
    db: AsyncSession, category: DeviceCategory, *, category_id: UUID | None = None
) -> str:
    """复用设备库的编号生成器，别在这里再写一份。

    这里以前是 select(func.max(Device.code))：字符串列取 max 是**字典序**，
    库里同时有 V999 和 V1000 时 max 得到 "V999"，下一个编号又生成 V1000 →
    唯一索引冲突，整个导入批次 409 中断，而且只在编号过 4 位后才暴露。
    devices._next_device_code 是逐个 int() 取最大值，没有这个问题。

    2026-09-30 起多收一个 category_id：编号前缀跟着**分类树节点**走
    （节点上的 code_prefix，留空继承父节点），一级分类映射退化为兜底。
    """
    from app.api.v1.devices import _next_device_code

    return await _next_device_code(db, category, category_id=category_id)





