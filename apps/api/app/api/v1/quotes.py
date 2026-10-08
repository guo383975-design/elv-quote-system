"""Quotes router: CRUD + 明细行 + 重算（双行并列）+ 子系统汇总 + 成本/利润."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import get_current_user, require_permission
from app.core.labels import label_of
from app.core.logging import get_logger
from app.core.permissions import Action, can, can_see_cost, can_see_profit
from app.core.workflow import (
    can_move_quote,
    quote_transition_error,
    required_quote_action,
)
from app.db.session import get_db
from app.db.types import drop_none_for_not_null as _drop_none_for_not_null
from app.models.audit import AuditAction, QuoteAuditLog
from app.models.device import Device, DeviceStatus
from app.models.labor import LaborClass, LaborRate, QuotaItem
from app.models.project import Project
from app.models.quote import AuxiliaryBaseType, FeeBaseType, Quote, QuoteStatus
from app.models.quote_item import QuoteItem
from app.models.quote_revision import QuoteRevision
from app.models.subsystem import Subsystem
from app.models.user import User
from app.schemas import (
    ApprovalDecision,
    ComputeRequest,
    ComputeResult,
    Page,
    QuoteCreate,
    QuoteDetailOut,
    QuoteItemCreate,
    QuoteItemOut,
    QuoteItemUpdate,
    QuoteOut,
    QuoteUpdate,
)
from app.services.approval import required_levels
from app.services.audit import quote_audit
from app.services.export_data import _export_settings
from app.services.pricing import check_price_floor, pick_tier_price
from app.services.quote_compute import (
    compute_quote,
    parse_quota_code,
    write_breakdown_to_quote,
)
from app.services.revisions import (
    attach_revision_stats,
    diff_snapshots,
    diff_totals,
    save_revision,
)
from app.services.settings import load_export_settings, load_settings

router = APIRouter(prefix="/quotes", tags=["报价单"])
log = get_logger("api.quotes")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _load_quote(db: AsyncSession, quote_id: UUID) -> Quote:
    """Load a quote with items (and each item's device) eagerly loaded.

    `device` is eager-loaded because QuoteItemOut exposes the device's
    code/status for the 停产 badge. Without it, reading the property inside the
    async request would trigger a lazy load and raise MissingGreenlet.
    """
    quote = await db.scalar(
        select(Quote)
        .options(selectinload(Quote.items).selectinload(QuoteItem.device))
        .where(Quote.id == quote_id)
    )
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")
    return quote


async def _load_labor_rates(db: AsyncSession) -> dict[LaborClass, LaborRate]:
    """Current 宁波人工市场信息价 per class."""
    rows = await db.scalars(
        select(LaborRate).where(
            LaborRate.is_current.is_(True),
            LaborRate.region == "ningbo",
        )
    )
    return {r.labor_class: r for r in rows.all()}


async def _recompute(db: AsyncSession, quote: Quote, *, actor_id: UUID | None = None) -> Quote:
    """Run the fee waterfall and persist results.

    We explicitly re-read the item collection first: items added or deleted
    through ``quote_id=``/``db.delete()`` do not automatically update an
    already-loaded ``quote.items`` collection, which would silently compute
    totals from a stale list.
    """
    await db.flush()
    await db.refresh(quote, attribute_names=["items"])
    items = list(quote.items)
    labor_rates = await _load_labor_rates(db)

    # 定额子目：把 quote_item.notes 里约定的 "quota:5-1-001" 解析出来。
    # 必须走 parse_quota_code：以前这里写 it.notes.split("quota:", 1)[1].split()[0]，
    # 备注恰好是 "quota:"（或 "quota:   "）时 [1] 是空串、.split() 是空列表，
    # [0] 直接 IndexError —— 而这个列表推导每次重算都跑，一行坏备注会让整张
    # 报价单的加行/改价/重算/询价导入全部 500，同时别的正常定额行也一起取不到价。
    quota_map: dict[str, str] = {}
    for it in items:
        code = parse_quota_code(it.notes)
        if code:
            quota_map[str(it.id)] = code

    # 安装单价方式（2026-10-05）：**定额模式**下安装费由"套定额→基价"承担 ✓，
    # 因此不再叠加定额人工费 ✗（否则同一笔钱算两遍 ✓）。
    # 手动模式保持原行为 ✓（备注里若有 quota:xxx 仍按定额口径加人工费 ✓）。
    quota_mode = str(getattr(quote, "install_mode", "manual") or "manual") == "quota"
    quota_items: dict[str, QuotaItem] = {}
    if quota_map and not quota_mode:
        rows = await db.scalars(
            select(QuotaItem).where(QuotaItem.code.in_(set(quota_map.values())))
        )
        quota_items = {q.code: q for q in rows.all()}

    float_per_class = {
        k: Decimal(str(v))
        for k, v in (quote.labor_float_per_class or {}).items()
        if v is not None
    }
    # 整体浮动与分类浮动**都要传**：计算侧按「有效上浮率 = 整体 + 该类」处理。
    #
    # 这里原来是一段 fallback：`if not float_per_class:` 才把 labor_float_rate 摊到三类上。
    # 那个条件在前端路径下**永不成立**（ratePayload 永远发送三个 0，含 0 的字典是 truthy），
    # 于是界面上「整体浮动」填了不生效、也不报错（2026-10-03 修）。
    overall_float = Decimal(str(quote.labor_float_rate or 0))

    # 工艺系数由**报价级设置**提供（2026-10-05）：缺省 1.20 / 1.10 / 1.20 ✓
    setting_quote = (await load_settings(db)).get("quote") or {}
    breakdown = compute_quote(
        items=items,
        auxiliary_base=AuxiliaryBaseType(str(quote.auxiliary_base)),
        auxiliary_rate=Decimal(str(quote.auxiliary_rate)),
        fee_base=FeeBaseType(str(quote.fee_base)),
        design_rate=Decimal(str(quote.design_rate)),
        warranty_rate=Decimal(str(quote.warranty_rate)),
        training_fee=Decimal(str(quote.training_fee)),
        mgmt_profit_rate=Decimal(str(quote.mgmt_profit_rate)),
        tax_rate=Decimal(str(quote.tax_rate)),
        labor_float_rate=overall_float,
        # 定额模式（安装费=定额基价 ✓）下关掉定额口径，避免同一笔钱重复计 ✗
        quota_enabled=(str(getattr(quote, "install_mode", "manual") or "manual") != "quota"),
        labor_float_per_class=float_per_class,
        labor_rates=labor_rates,
        quota_items=quota_items,
        quote_items_quota_map=quota_map,

                    shielded_coef=Decimal(str(setting_quote.get("shielded_coef") or "1.20")),
                    cat5e_coef=Decimal(str(setting_quote.get("cat5e_coef") or "1.10")),
                    ceiling_coef=Decimal(str(setting_quote.get("ceiling_coef") or "1.20")),
                )
    write_breakdown_to_quote(quote, breakdown)

    # 任何一次重算都推进版本号。
    # 重算的输入包含明细行，所以明细一改版本就该变，客户端手里的 version 才会
    # 失效 → 下一次提交得到 409 而不是静默覆盖别人的修改。
    quote.version = int(quote.version or 1) + 1

    # 瞬态属性（非映射字段，不落库）：把定额退化之类的告警挂到 ORM 实例上，
    # 让 _compute_result 能一并回传给前端。否则「定额口径已退化为安装费」
    # 这种事只写在日志里，界面上完全看不出来，用户会拿错口径去对甲方。
    quote.compute_warnings = list(getattr(breakdown, "warnings", None) or [])
    # 同理把"有多少行成本是估算的"带出来：界面上看不到这个，
    # 就会把估算毛利当成实际毛利。
    quote.compute_estimated_rows = int(getattr(breakdown, "estimated_cost_rows", 0) or 0)
    quote.compute_estimated_amount = getattr(
        breakdown, "estimated_cost_amount", Decimal("0")
    )

    # 回写子系统小计
    await _rollup_subsystems(db, quote)

    db.add(
        quote_audit(
            quote_id=quote.id,
            quote_code=quote.code,
            user_id=actor_id,
            action=AuditAction.COMPUTE,
            payload={
                "total_with_tax": str(breakdown.total_with_tax),
                "quota_total_with_tax": str(breakdown.quota_total_with_tax),
                "item_count": len(items),
                "warnings": breakdown.warnings,
            },
        )
    )
    await db.flush()
    return quote


async def _rollup_subsystems(db: AsyncSession, quote: Quote) -> None:
    """重算**该项目全部报价单**在每个子系统上的小计。

    ⚠ 以前只按**当前这一张**报价单的明细求和并覆盖写入，而
    `subsystems.equipment_subtotal` 是**项目级**列 —— 于是一个项目有两张报价单时，
    编辑 B 单会把 A 单的贡献整个抹掉（实测 1000 → 500），
    项目级子系统汇总变成"最后被编辑的那张单"的值。

    ⚠ 也从来不写 `auxiliary_subtotal`（导出"子系统汇总"表的辅材费因此恒为 0，
     与同一份导出里"报价汇总"的辅材费自相矛盾）。辅材费是**报价单级**的
     一笔费用，没有逐行金额可加，所以按各子系统在本单中的
     (设备费+安装费) 占比分摊 —— 分摊之和恰好等于该单的辅材费。
    """
    subs = list(
        (
            await db.scalars(select(Subsystem).where(Subsystem.project_id == quote.project_id))
        ).all()
    )
    eq_sum: dict[UUID, Decimal] = {s.id: Decimal("0") for s in subs}
    ins_sum: dict[UUID, Decimal] = {s.id: Decimal("0") for s in subs}
    aux_sum: dict[UUID, Decimal] = {s.id: Decimal("0") for s in subs}
    pts_sum: dict[UUID, Decimal] = {s.id: Decimal("0") for s in subs}

    project_quotes = list(
        (
            await db.scalars(
                select(Quote)
                .options(selectinload(Quote.items))
                .where(Quote.project_id == quote.project_id)
            )
        ).all()
    )
    for q in project_quotes:
        q_items = [it for it in q.items if it.subsystem_id in eq_sum]
        # 分摊基数：本单里能落到子系统上的 (设备费 + 安装费)
        base = sum(
            (
                Decimal(str(it.equipment_amount or 0)) + Decimal(str(it.install_amount or 0))
                for it in q_items
            ),
            Decimal("0"),
        )
        q_aux = Decimal(str(q.auxiliary_total or 0))
        for it in q_items:
            sid = it.subsystem_id
            if sid is None:  # pragma: no cover - 上面的过滤已经排除
                continue
            eq = Decimal(str(it.equipment_amount or 0))
            ins = Decimal(str(it.install_amount or 0))
            eq_sum[sid] += eq
            ins_sum[sid] += ins
            pts_sum[sid] += Decimal(str(it.quantity or 0))
            if base > 0:
                aux_sum[sid] += q_aux * (eq + ins) / base

    cent = Decimal("0.01")
    for s in subs:
        s.equipment_subtotal = eq_sum[s.id].quantize(cent, rounding=ROUND_HALF_UP)
        s.install_subtotal = ins_sum[s.id].quantize(cent, rounding=ROUND_HALF_UP)
        s.auxiliary_subtotal = aux_sum[s.id].quantize(cent, rounding=ROUND_HALF_UP)
        s.point_count = int(pts_sum[s.id])
    await db.flush()


def _can_see_cost(user: User) -> bool:
    """能否看**成本/进价**（2026-10-05 起与利润分开）。

    判定在 core/visibility（可在「系统设置 → 数据可见性」按角色改），
    这里保留薄封装，让既有调用点不用改。
    注意：**能导出（quote.export，sales 也有）不等于能看成本**。
    """
    return can_see_cost(role=user.role, is_superuser=bool(user.is_superuser))


def _can_see_profit(user: User) -> bool:
    """能否看**利润**（毛利额/毛利率）。

    与成本分开：财务与总经理看利润，采购只看进价，工程与销售两样都不看。
    """
    return can_see_profit(role=user.role, is_superuser=bool(user.is_superuser))


def _compute_result(quote: Quote, user: User | None = None) -> ComputeResult:
    """构造重算结果。user 为空或无权时，成本/毛利字段一律不回传。"""
    show_cost = user is not None and _can_see_cost(user)
    show_profit = user is not None and _can_see_profit(user)
    return ComputeResult(
        equipment_total=quote.equipment_total,
        install_total=quote.install_total,
        auxiliary_total=quote.auxiliary_total,
        design_fee=quote.design_fee,
        warranty_fee=quote.warranty_fee,
        subtotal_before_tax=quote.subtotal_before_tax,
        mgmt_profit=quote.mgmt_profit,
        tax_amount=quote.tax_amount,
        total_with_tax=quote.total_with_tax,
        quota_total=quote.quota_total,
        quota_total_with_tax=quote.quota_total_with_tax,
        # 成本只看成本权限；**利润是另一档**（财务看得到、采购看不到）
        cost_total=quote.cost_total if show_cost else None,
        gross_profit=quote.gross_profit if show_profit else None,
        gross_profit_rate=quote.gross_profit_rate if show_profit else None,
        estimated_cost_rows=int(getattr(quote, "compute_estimated_rows", 0) or 0),
        estimated_cost_amount=(
            getattr(quote, "compute_estimated_amount", None) if show_cost else None
        ),
        warnings=list(getattr(quote, "compute_warnings", None) or []),
        # 本次保存有没有落版（瞬态属性，由 compute 端点挂上 ✓）：
        # 界面据此提示「已保存版本（时间）/ 内容未变化，未新增版本」✓
        revision_saved=bool(getattr(quote, "revision_saved", False)),
        revision_created_at=getattr(quote, "revision_created_at", None),
        revision_count=int(getattr(quote, "revision_count", 0) or 0),
    )


def _normalize_jsonable(value):
    """Convert Decimal-bearing dicts into plain floats.

    The `labor_float_per_class` column is JSON/JSONB, which is serialized with
    `json.dumps` — that cannot handle `Decimal`. Pydantic hands us Decimals.
    """
    if isinstance(value, dict):
        return {str(k): (float(v) if v is not None else 0.0) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# quote CRUD
# ---------------------------------------------------------------------------

@router.get("", response_model=Page[QuoteOut], summary="报价单列表")
async def list_quotes(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    project_id: UUID | None = Query(None),
    quote_status: QuoteStatus | None = Query(None, alias="status"),
    keyword: str | None = Query(None),
) -> Page[QuoteOut]:
    stmt = select(Quote)
    if project_id:
        stmt = stmt.where(Quote.project_id == project_id)
    if quote_status:
        stmt = stmt.where(Quote.status == quote_status)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(Quote.title.ilike(like) | Quote.code.ilike(like))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(
            stmt.order_by(Quote.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    # 业务版本（第几版）= quote_revisions.seq 最大值；列表一次 group by 全部查出来 ✓
    await attach_revision_stats(db, rows)
    return Page[QuoteOut](
        items=[QuoteOut.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.post("", response_model=QuoteOut, status_code=status.HTTP_201_CREATED, summary="创建报价单")
async def create_quote(
    body: QuoteCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "create"))],
) -> Quote:
    project = await db.get(Project, body.project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    # code/title 允许不传，由项目推导。前端「编制报价」只想给一个 project_id，
    # 强迫调用方先编出一个编号是没必要的门槛（以前这里是必填，导致 422，
    # 于是「编制报价」这个入口根本走不通）。
    # ⚠ 必须 exclude_unset=True ✓：否则 Pydantic 的**默认值**也会进 data ✗，
    #   下面「调用方没传就用系统设置 / 原单」的判断（data.get(_f) is None）永不成立 ✗。
    #   2026-10-07 由前端 agent 用**非默认费率**的原单实测发现：增项单费率会被重置成默认 ✗
    #   （套定额单增项会变成手工模式 ✗）。当年我用一张费率恰好全是默认值的原单验证，
    #   所以没暴露 ✗ —— 回归测试必须用非默认值 ✓（见 test_addon_quote_inherits_non_default_rates）。
    data = body.model_dump(exclude_unset=True)
    # 创建时把「系统设置 → 报价策略」里的费率落到本单（2026-10-05 用户决策④ ✓）：
    #   auxiliary_rate（辅材）/ mgmt_profit_rate（管理费+毛利）属于
    #   **创建报价时确定、之后不可改**的那一类 ✓。
    #   以前这里直接用模型里写死的默认值 ✗ → 系统设置改了不生效 ✓。
    #   调用方显式传了值就以调用方为准 ✓（将来创建界面可覆盖 ✓）。
    # ---- 增项：从原单复制（2026-10-05 用户要求）----
    _based_on = data.pop("based_on_quote_id", None)
    # 这些字段来自原单（增项）→ 后面的「项目级费率快照」不得再覆盖它们 ✓
    _from_src: set[str] = set()
    _src: Quote | None = None
    if _based_on:
        _src = await db.get(Quote, _based_on)
        if _src is None:
            raise HTTPException(status_code=404, detail="增项来源的报价单不存在")
        # 费率/条款快照：调用方显式传了就不覆盖 ✓（与本仓库"创建时定"的既有做法一致 ✓）
        for _f in (
            "tax_method", "tax_rate", "fee_base", "auxiliary_base", "auxiliary_rate",
            "design_rate", "warranty_rate", "training_fee", "mgmt_profit_rate",
            "labor_float_rate", "labor_float_per_class", "terms_override",
            "valid_days_override", "install_mode",
        ):
            if data.get(_f) is None and getattr(_src, _f, None) is not None:
                data[_f] = getattr(_src, _f)
                _from_src.add(_f)


    _qs = (await load_settings(db) or {}).get("quote") or {}
    for _field in ("auxiliary_rate", "mgmt_profit_rate", "tax_rate", "labor_float_rate"):
        if data.get(_field) is None and _qs.get(_field) is not None:
            data[_field] = _qs[_field]
    code = (data.get("code") or "").strip()
    if not code:
        # 同项目下已存在的报价单数量 + 1 → 编号后缀，冲突时继续往后找一个空位
        existing = list(
            (
                await db.scalars(
                    select(Quote.code).where(Quote.project_id == project.id)
                )
            ).all()
        )
        base = (project.code or "QUOTE").strip() or "QUOTE"
        n = len(existing) + 1
        code = f"{base}-Q{n:02d}"
        while await db.scalar(select(Quote).where(Quote.code == code)):
            n += 1
            code = f"{base}-Q{n:02d}"
    elif await db.scalar(select(Quote).where(Quote.code == code)):
        raise HTTPException(status_code=409, detail=f"报价单编号 {code} 已存在")

    data["code"] = code
    if not (data.get("title") or "").strip():
        data["title"] = f"{project.name} 报价单"

    # ----- 项目级费率快照（H-12，2026-10-03）-----
    #
    # 项目的 8 个费率列以前是彻头彻尾的死列：模型里有、`ProjectCreate` 里没有，
    # 界面上也没有 —— 既配不了、读不到，更不会影响报价单。现在补成
    # **「项目上配置 → 建单时抄进报价单」** 的快照语义：
    #
    #   · 抄的是**副本**：之后改项目费率不会回改已建的报价单（否则历史报价会
    #     被静默改数，等于篡改已发出去的清单）；
    #   · 调用方**显式指定**的字段以调用方为准（用 exclude_unset 判断"显式"，
    #     而不是"值等于默认值"—— 后者分不清"没传"和"就是要这个值"）。
    provided = body.model_dump(exclude_unset=True)
    copied: list[str] = []
    for field in _PROJECT_RATE_FIELDS:
        if field in provided or field in _from_src:
            continue
        raw = getattr(project, field, None)
        if raw is None:
            continue
        if field in _ENUM_RATE_FIELDS:
            # 项目侧是普通字符串列，报价单侧是 PG 枚举：转一下，非法值就跳过
            # （历史脏数据不该让建单直接 500）。
            try:
                raw = _ENUM_RATE_FIELDS[field](str(raw))
            except ValueError:
                log.warning(f"项目 {project.code} 的 {field}={raw!r} 不是合法取值，已跳过")
                continue
        data[field] = raw
        copied.append(field)
    if copied:
        log.info(f"报价单 {code} 沿用项目 {project.code} 的费率: {', '.join(copied)}")

    # labor_float_per_class 是 JSON 列，json.dumps 不认 Decimal —— pydantic 交给我们的是
    # Decimal，不转换的话带这个字段创建报价单会直接 500。
    if data.get("labor_float_per_class") is not None:
        data["labor_float_per_class"] = _normalize_jsonable(data["labor_float_per_class"])

    # 丢掉显式 None：否则会把 labor_float_per_class 写成 NULL（该列 NOT NULL），
    # 并绕过 Python 侧 default。
    payload = _drop_none_for_not_null(Quote, data)
    quote = Quote(**payload)
    if not quote.customer_name:
        quote.customer_name = project.customer_name
    if not quote.customer_contact:
        quote.customer_contact = project.customer_contact
    if not quote.customer_phone:
        quote.customer_phone = project.customer_phone
    db.add(quote)
    await db.flush()
    # 建单也留痕（H-10：原来只有重算/改状态/换型/导出有痕，"谁建的这张单"查不到）
    db.add(
        quote_audit(
            quote_id=quote.id,
            quote_code=quote.code,
            user_id=user.id,
            action=AuditAction.CREATE,
            payload={"project_id": str(quote.project_id), "title": quote.title},
        )
    )
    await db.refresh(quote)
    log.info(f"创建报价单 {quote.code} v{quote.version}")
    # 增项：把原单明细整份复制过来 ✓（含父子关系 ✓ 旧 id → 新 id 映射 ✓）
    if _src is not None:
        _rows = list(
            (
                await db.scalars(
                    select(QuoteItem)
                    .where(QuoteItem.quote_id == _src.id)
                    .order_by(QuoteItem.sort_order, QuoteItem.created_at)
                )
            ).all()
        )
        _idmap: dict[UUID, UUID] = {}
        for _r in _rows:
            _new = QuoteItem(
                quote_id=quote.id,
                subsystem_id=_r.subsystem_id,
                device_id=_r.device_id,
                name=_r.name,
                brand=_r.brand,
                model=_r.model,
                spec_text=_r.spec_text,
                unit=_r.unit,
                quantity=_r.quantity,
                unit_price=_r.unit_price,
                cost_price=_r.cost_price,
                install_unit_price=_r.install_unit_price,
                auxiliary_unit_price=_r.auxiliary_unit_price,
                is_owner_supplied=_r.is_owner_supplied,
                is_cat5e=_r.is_cat5e,
                is_ceiling=_r.is_ceiling,
                notes=_r.notes,
                sort_order=_r.sort_order,
                source=_r.source,
            )
            db.add(_new)
            await db.flush()
            _idmap[_r.id] = _new.id
        # 父子关系补齐 ✓（父行可能排在子行之后 ✓ 所以复制完再统一映射 ✓）
        for _r in _rows:
            if _r.parent_item_id and _r.parent_item_id in _idmap:
                _child = await db.get(QuoteItem, _idmap[_r.id])
                if _child is not None:
                    _child.parent_item_id = _idmap[_r.parent_item_id]
        await db.flush()
        quote.based_on_quote_id = _src.id

    return quote


@router.get("/{quote_id}", response_model=QuoteDetailOut, summary="报价单详情（含明细）")
async def get_quote(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> Quote:
    quote = await _load_quote(db, quote_id)
    # 页头要显示「第几版」——业务版本，不是乐观锁计数器 ✓
    await attach_revision_stats(db, [quote])
    return quote


async def _check_version(
    db: AsyncSession,
    quote: Quote,
    client_version: int | None,
    *,
    actor: User | None = None,
) -> None:
    """乐观锁比对：客户端手上的版本对不上就 409。

    quotes.version 这一列一直存在、也一直在响应里返回，但 QuoteUpdate 里没有
    这个字段，写入费率的两个口子（PATCH /quotes/{id} 和 POST /quotes/{id}/compute）
    又不比对也不递增 —— 两个人同时改同一张单时，后提交的会**静默覆盖**前一个，
    正是设计里「乐观锁从第一天就加」要防的事。

    ⚠ **不传 version 的调用方仍然放行**（脚本、老客户端）。
    这一点必须说清楚：它意味着乐观锁对"不配合的调用方"没有约束力，
    只能防住"两个都用界面在改"的情况。所以我们把"没带版本号的写入"
    记进日志留痕，让静默覆盖至少事后可查。
    """
    if client_version is None:
        # 不是错误，但值得留痕：这类写入不参与冲突检测。
        log.warning(
            f"报价单 {quote.code} 的写入未携带 version"
            f"（调用方 {getattr(actor, 'username', '?')}）—— 本次不做乐观锁比对"
        )
        return
    if int(client_version) != int(quote.version or 1):
        # 2026-10-05 用户反馈：每次冲突都让用户刷新很麻烦。
        #   原来只返回一句纯文本（整单级拒绝）→ 前端除了让用户刷新别无他法。
        #   现在返回结构化信息：前端据此判断「双方改的字段是否重叠」，
        #   不重叠就自动带最新版本重试（用户无感），重叠才弹窗让用户选。
        remote_fields: list[str] = []
        remote_by = None
        remote_at = None
        try:
            _last = (
                await db.execute(
                    select(QuoteAuditLog)
                    .where(
                        QuoteAuditLog.quote_id == quote.id,
                        QuoteAuditLog.action == AuditAction.UPDATE,
                    )
                    .order_by(QuoteAuditLog.created_at.desc())
                    .limit(1)
                )
            ).scalars().first()
            if _last is not None:
                _p = _last.payload if isinstance(_last.payload, dict) else {}
                remote_fields = [str(k) for k in _p]
                remote_at = _last.created_at.isoformat() if _last.created_at else None
                if _last.user_id:
                    _who = await db.get(User, _last.user_id)
                    remote_by = getattr(_who, "username", None)
        except Exception:  # noqa: BLE001 —— 拿不到上下文也不能挡住冲突本身
            pass
        raise HTTPException(
            status_code=409,
            detail={
                "error": "version_conflict",
                "message": (
                    f"这张单在你编辑期间被{remote_by or '他人'}改过"
                    # 措辞（2026-10-08 用户反馈）：这里出现的是**乐观锁计数器** ✗，
                    # 不能叫「版本」✗ ——「版本」现在是业务概念（第几版，见
                    # quote_revisions.seq）✓。叫「编辑序号」才不会被误读成"我改了 9 版"✓。
                    f"（你手上的编辑序号 {client_version}，当前 {quote.version}）。"
                    "系统会先尝试自动合并；若改的是同一项，会让你选择保留哪一份。"
                ),
                "your_version": int(client_version),
                "current_version": int(quote.version or 1),
                "remote_changed": remote_fields,
                "remote_by": remote_by,
                "remote_at": remote_at,
            },
        )


# 已批准/已归档的单据不允许再改内容。
# 原来只有"进入 approved"要做权限校验，"离开 approved（退回草稿）"不校验，
# 于是任何有 quote.UPDATE 的人都能把已批准的单退回草稿、改钱、再提交 ——
# 审批就成了一个没有约束力的标签。
_LOCKED_STATUSES = {QuoteStatus.APPROVED, QuoteStatus.ARCHIVED}

# 建报价单时要从项目抄过来的费率字段（H-12）。
# 顺序按"界面上出现的顺序"，便于日志对照。
_PROJECT_RATE_FIELDS = (
    "tax_rate",
    "fee_base",
    "auxiliary_base",
    "auxiliary_rate",
    "design_rate",
    "warranty_rate",
    "training_fee",
    "mgmt_profit_rate",
)

# 其中这两个在项目侧是字符串列、在报价单侧是 PG 枚举，需要显式转换
_ENUM_RATE_FIELDS = {
    "fee_base": FeeBaseType,
    "auxiliary_base": AuxiliaryBaseType,
}


def _assert_not_archived(quote: Quote) -> None:
    """归档单**不允许任何写入**（2026-10-08 补）。

    背景：_assert_editable 覆盖了「改内容」的入口 ✓，但 **POST /compute**（改费率并重算）
    当时没调用它 ✗ → 归档单仍能通过接口改写费率 ✓（界面挡住了 ✓，接口没挡 ✗）。
    口径刻意**窄**：只挡 archived ✓；approved 保持既有流程（允许有权限的人退回草稿再改 ✓），
    与 _assert_not_archived_revert 的分工一致 ✓。
    """
    if str(quote.status) == str(QuoteStatus.ARCHIVED):
        raise HTTPException(
            status_code=409,
            detail=(
                "归档的报价单不可修改（归档即定稿）。如需在此基础上增项，"
                "请新建一张关联本单的报价单。"
            ),
        )


def _assert_not_archived_revert(quote: Quote, new_status: object) -> None:
    """归档是不可逆的：不许把 archived 改回任何其它状态（2026-10-05 用户要求）。

    为什么单独加一道：_assert_editable 只挡"直接改内容" ✗，
    但状态退回 draft 是**允许**的 ✗ → 有 quote.APPROVE 的人退回草稿后照样能改 ✓，
    归档的锁就形同虚设 ✓。approved → draft 的退回**保留** ✓（那是正常审批流程 ✓）。
    """
    if str(quote.status) == str(QuoteStatus.ARCHIVED) and str(new_status) != str(
        QuoteStatus.ARCHIVED
    ):
        raise HTTPException(
            status_code=422,
            detail=(
                "归档的报价单不可修改，也不可退回其它状态（归档即定稿）。"
                "如需在此基础上增项，请新建一张关联本单的报价单。"
            ),
        )


def _assert_editable(quote: Quote, user: User) -> None:
    """已批准/已归档的报价单：先退回草稿（需要 quote.APPROVE）才能改。"""
    if str(quote.status) in {str(s) for s in _LOCKED_STATUSES}:
        raise HTTPException(
            status_code=409,
            detail=(
                f"报价单当前状态为 {quote.status}，不允许直接修改。"
                "请先由有审批权限的人把状态退回 draft，再修改金额或明细。"
            ),
        )


@router.patch("/{quote_id}", response_model=QuoteOut, summary="更新报价单（改费率/状态）")
async def update_quote(
    quote_id: UUID,
    body: QuoteUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> Quote:
    """改报价单（费率/标题/状态）。

    ⚠ 路由级权限**不能**只写 `require_permission("quote","update")`：
    审批动作（approve / 退回草稿）在 HTTP 上也走 PATCH，而 manager 只有
    `quote.approve` 没有 `quote.update` —— 写 update 的话审批请求在
    进函数体之前就被 403 了（2026-10-02 上机实测抓到）。

    所以这里改成「先放行有 update **或** approve 的人」，再在函数体内按
    **要做什么**细分，两道关：

      1. **方向**（业务规则）：`can_move_quote` —— 「必须先提交才能批准」就是这条。
         草稿直接批准会 409，跟有没有权限无关（见 core/workflow.py）。
      2. **权限**（谁能做）：该流转需要 `quote.update`（提交/撤回）还是
         `quote.approve`（批准/驳回/退回/归档），逐个流转查表，不再靠
         "是不是 approved" 这种间接判断。

    改其它字段（费率/标题/备注）另算：需要 `quote.update`，且单据未锁定。
    """
    can_update = can(user.role, "quote", "update") or user.is_superuser
    can_approve = can(user.role, "quote", "approve") or user.is_superuser
    if not (can_update or can_approve):
        raise HTTPException(
            status_code=403, detail="权限不足: quote.update / quote.approve"
        )

    quote = await _load_quote(db, quote_id)
    data = body.model_dump(exclude_unset=True)

    # 单据级条款里的变量也要校验（2026-10-05）：写错变量的后果是把
    # `{valid_dayss}` 原样印在客户版报价单上 —— 人工几乎不可能发现。
    if data.get("clauses_override"):
        from app.services.quote_terms import unknown_variables

        bad = unknown_variables(data["clauses_override"])
        if bad:
            raise HTTPException(
                status_code=422,
                detail=f"条款里有未定义的变量：{'、'.join('{' + v + '}' for v in bad)}",
            )

    # 乐观锁：见 _check_version 的说明。
    # 不传 version 的调用方仍然放行（保持向后兼容），但版本号照样会递增。
    await _check_version(db, quote, data.pop("version", None), actor=user)

    new_status = data.pop("status", None)
    status_changing = new_status and str(new_status) != str(quote.status)
    if status_changing:
        from_status = str(quote.status)
        target_status = str(new_status)
        # 归档不可逆（2026-10-05 用户要求）：宁可让用户新建增项单，也不许解开归档 ✓
        _assert_not_archived_revert(quote, target_status)

        # 第一关：方向合法吗（业务规则）
        if not can_move_quote(from_status, target_status):
            raise HTTPException(
                status_code=409, detail=quote_transition_error(from_status, target_status)
            )
        # 第二关：这个流转归谁做
        need = required_quote_action(from_status, target_status)
        if need == "approve" and not can_approve:
            raise HTTPException(
                status_code=403, detail="无权审批报价单（需要 quote.APPROVE）"
            )
        if need == "update" and not can_update:
            raise HTTPException(
                status_code=403,
                detail="无权重置报价单状态（需要 quote.UPDATE；审批人只能批准/驳回/退回）",
            )

        # 分级审批门槛（2026-10-04）：开启后，直接"批准"必须已经批满级数，
        # 否则任何人都能一步跳过经理 —— 那分级就是摆设。默认关闭，行为与现状一致。
        if target_status == str(QuoteStatus.APPROVED):
            from app.services.approval import approved_levels, can_finalize

            conf = (await load_settings(db)).get("approval") or {}
            # **只有开启分级审批时才拦**：没开启时"批准"就是一步到位（现状）。
            # 这里必须把 need 一次性算对 —— 先置 0 再被下一行覆盖的话，
            # 未开启也会按"需要 1 级"去拦，等于把既有审批流程全堵死。
            enabled = bool(conf.get("enabled"))
            need = (
                required_levels(quote.total_with_tax, conf.get("tiers") or [], enabled)
                if enabled
                else 0
            )
            done = len(await approved_levels(db, quote.id))
            if need and not can_finalize(await approved_levels(db, quote.id), need):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"本案已启用分级审批：含税合计需要 {need} 级审批，当前已批 {done} 级。"
                        "请走 POST /quotes/{id}/approvals 逐级审批"
                    ),
                )

        # 留痕动作按流转细分（以前只有 approve/update 两种，看不出"提交"这一步）
        if target_status == str(QuoteStatus.SUBMITTED):
            action = AuditAction.SUBMIT
        elif target_status == str(QuoteStatus.APPROVED):
            action = AuditAction.APPROVE
        elif target_status == str(QuoteStatus.REJECTED):
            action = AuditAction.REJECT
        else:
            action = AuditAction.UPDATE
        db.add(
            quote_audit(
                quote_id=quote.id,
                quote_code=quote.code,
                user_id=user.id,
                action=action,
                payload={"from": from_status, "to": target_status},
            )
        )
        quote.status = new_status
        # 版本快照（2026-10-04）：提交审核 = "报出去的那一版"；批准 = "定下来的那一版"。
        # 放在这里而不是"每次改动"：改一次数量不该长一条版本记录（那种粒度看操作留痕）。
        if target_status in (str(QuoteStatus.SUBMITTED), str(QuoteStatus.APPROVED)):
            await save_revision(db, quote, reason=target_status, user=user)

        # 通知（2026-10-04）：提交/进入审核 → 提醒有审批权的人；批准/驳回 → 通知报单的人。
        # 没有这一步的话，单子提交完就静静躺着，谁都不知道轮到自己了。
        from app.models.notification import NotificationKind
        from app.services.notifications import notify, users_with_permission

        if target_status in (str(QuoteStatus.SUBMITTED), str(QuoteStatus.REVIEWING)):
            approvers = await users_with_permission(db, "quote", Action.APPROVE)
            if approvers:
                notify(
                    db,
                    user_ids=approvers,
                    kind=str(NotificationKind.APPROVAL_PENDING),
                    title=f"待审批：{quote.code} {quote.title}",
                    body=(
                        f"含税合计 {quote.total_with_tax}；提交人 {user.username}"
                    ),
                    # 通知链接必须指向**真实存在的路由**（2026-10-05 用户反馈 404 ✓）：
                    #   应用里没有 /quotes/:id ✗，正确路由是
                    #   /projects/:projectId/workbench/:quoteId ✓
                    link=f"/projects/{quote.project_id}/workbench/{quote.id}",
                )
        elif target_status in (str(QuoteStatus.APPROVED), str(QuoteStatus.REJECTED)):
            # 用显式查询取 owner：`quote.project` 是懒加载关系，
            # 在 async 上下文里访问会抛 MissingGreenlet（不是"有时失败"，是必炸）。
            owner = (
                await db.scalar(
                    select(Project.owner_id).where(Project.id == quote.project_id)
                )
                if quote.project_id
                else None
            )
            targets = [owner] if owner else []
            kind = (
                str(NotificationKind.QUOTE_APPROVED)
                if target_status == str(QuoteStatus.APPROVED)
                else str(NotificationKind.QUOTE_REJECTED)
            )
            verb = "已批准" if target_status == str(QuoteStatus.APPROVED) else "被驳回"
            notify(
                db,
                user_ids=targets,
                kind=kind,
                title=f"报价单{verb}：{quote.code}",
                body=f"{quote.title}；操作人 {user.username}",
                # 通知链接必须指向**真实存在的路由**（2026-10-05 用户反馈 404 ✓）：
                    #   应用里没有 /quotes/:id ✗，正确路由是
                    #   /projects/:projectId/workbench/:quoteId ✓
                    link=f"/projects/{quote.project_id}/workbench/{quote.id}",
            )

    # 改内容（不只是改状态）时：① 锁定的单据必须拒绝；② 必须有 update 权限
    if data:
        if not can_update:
            raise HTTPException(
                status_code=403,
                detail="权限不足: quote.update（审批人只能审批/退回/重算，不能改报价内容）",
            )
        _assert_editable(quote, user)

    # JSONB 不接受 Decimal（2026-10-05 实测 500）：
    #   StatementError: Object of type Decimal is not JSON serializable
    #   schema 校验后是 Decimal ✓（要精确 ✓），但写 JSON 列前必须归一化 ✓。
    #   terms_override 与 labor_float_per_class 都是 JSON 列 ✓ → 一起处理 ✓。
    for _json_key in ("terms_override", "labor_float_per_class"):
        if data.get(_json_key) is not None:
            data[_json_key] = _normalize_jsonable(data[_json_key])

    # 创建后不可改的策略项（2026-10-05 用户决策④ ✓）：
    #   辅材率 / 管理费+毛利率 属于"建单时定死"的口径 ✓ —— 随手能改，
    #   等于所有历史单的算法都能被悄悄改掉 ✗。
    #   只开放 4 项：计税方式 / 税率 / 人工浮动 / 有效期 ✓。
    # 报价模式（定额 / 手工人工费）：创建单时确定，之后不可切换 ✓
    #   用户理由：「创建完以后不可以切换，这样能避免很多问题」✓
    #   注意：**提交相同的值仍然放行** ✓（幂等 ✓ 前端乐观切换失败回滚时不会误伤 ✓）
    if data.get("install_mode") is not None and str(data["install_mode"]) != str(
        quote.install_mode or "manual"
    ):
        raise HTTPException(
            status_code=422,
            detail="报价模式在创建报价单时确定、之后不可修改（如需更换，请新建报价单 ✓）",
        )
    _locked = {"auxiliary_rate": "辅材费率", "mgmt_profit_rate": "管理费+毛利率"}
    _touched = sorted(k for k in _locked if k in data)
    if _touched:
        raise HTTPException(
            status_code=422,
            detail="以下策略项在创建报价时确定、之后不可修改："
            + "、".join(_locked[k] for k in _touched)
            + "（如需调整，请新建报价单 ✓）",
        )

    # 条款/策略改动留痕（2026-10-05 ✓）：用户要求"改好的报价条款在保存时有记录" ✓
    #   记**改了哪些字段** + 条款全文与每单数字的快照 ✓（金额类字段也一并记 ✓）。
    _tracked = (
        "clauses_override", "terms_override", "valid_days_override",
        "tax_method", "tax_rate", "labor_float_rate", "labor_float_per_class",
    )
    _changed = {k: data[k] for k in _tracked if k in data}
    if _changed:
        db.add(
            quote_audit(
                quote_id=quote.id,
                quote_code=quote.code,
                user_id=user.id,
                action=AuditAction.UPDATE,
                # 审计载荷必须**自建一份 JSON 安全的值** ✓（2026-10-05 连环踩坑后的结论 ✓）：
                #   坑一：tax_rate 是 Decimal ✗ → 直接进审计 JSON 列会 500 ✗
                #   坑二：拿 _normalize_jsonable 去转**含 data 引用**的字典 ✗
                #          → 它会**就地**改字典 ✓ → 把还没写库的 data 也改了 ✗
                #          → 存进库/返回接口的值变成 list ✗（测试 float() 直接炸 ✗）
                #   → 所以这里逐个 str() 成新值 ✓，绝不触碰 data ✓
                payload={
                    "changed": sorted(_changed),
                    "clauses_override": [
                        str(x) for x in (_changed.get("clauses_override") or [])
                    ],
                    "terms_override": {
                        str(k): str(v)
                        for k, v in (_changed.get("terms_override") or {}).items()
                    },
                    "valid_days_override": (
                        None if _changed.get("valid_days_override") is None
                        else str(_changed.get("valid_days_override"))
                    ),
                    "tax": {
                        "method": _changed.get("tax_method"),
                        "rate": (
                            None if _changed.get("tax_rate") is None
                            else str(_changed.get("tax_rate"))
                        ),
                    },
                    "labor_float_rate": (
                        None if _changed.get("labor_float_rate") is None
                        else str(_changed.get("labor_float_rate"))
                    ),
                },
            )
        )
    for key, value in _drop_none_for_not_null(Quote, data).items():
        if key == "labor_float_per_class":
            value = _normalize_jsonable(value)
        setattr(quote, key, value)

    # 费率变了就重算
    if any(k in body.model_dump(exclude_unset=True) for k in (
        "auxiliary_rate", "design_rate", "warranty_rate", "training_fee",
        "mgmt_profit_rate", "tax_rate", "fee_base", "auxiliary_base",
        "labor_float_rate", "labor_float_per_class",
    )):
        await _recompute(db, quote, actor_id=user.id)
    else:
        # 没有触发重算的字段改动（改标题/状态/备注）也要推进版本号，
        # 否则「改了标题」这一步对别的编辑者是不可见的，乐观锁会漏掉它。
        quote.version = int(quote.version or 1) + 1
        await db.flush()
    await db.refresh(quote)
    # 流转到已提交/已批准时刚落了一版快照 → 让响应里的"第几版"立刻跟上 ✓
    await attach_revision_stats(db, [quote])
    return quote


@router.delete("/{quote_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除报价单")
async def delete_quote(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "delete"))],
) -> None:
    quote = await db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")
    # H-10：先写一条删除留痕再删。外键是 SET NULL，所以这条记录会活下来，
    # subject_code 里存着单号 —— 事后查"谁删了哪张单"答得上来。
    db.add(
        quote_audit(
            quote_id=quote.id,
            quote_code=quote.code,
            user_id=user.id,
            action=AuditAction.DELETE,
            payload={
                "code": quote.code,
                "title": quote.title,
                "status": str(quote.status),
                "total_with_tax": str(quote.total_with_tax),
                "item_count": len(quote.items or []),
            },
        )
    )
    await db.flush()
    # 显式把该单的留痕外键置空：不依赖数据库的 ON DELETE SET NULL 实现
    # （生产是 PG 会置空，但测试用的 SQLite 默认不启用外键，两边行为要一致）
    await db.execute(
        update(QuoteAuditLog).where(QuoteAuditLog.quote_id == quote.id).values(quote_id=None)
    )
    await db.delete(quote)
    log.warning(f"删除报价单 {quote.code} by {user.username}")


# ---------------------------------------------------------------------------
# compute
# ---------------------------------------------------------------------------

@router.post("/{quote_id}/quota-suggest", summary="AI 预套定额（只给建议，不写库）")
async def quota_suggest(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> dict:
    """按产品分类 + 定额名称**预套一次** ✓：规则打分排序，模糊的交给 AI 在候选里挑 ✓。

    本接口**只返回建议**、绝不写库 ✗ —— 定额错一行就是人工费错一笔钱，
    必须由人确认后再套（见 2026-10-05 用户要求）✓。
    """
    quote = await db.get(Quote, quote_id)
    if quote is None:
        raise HTTPException(status_code=404, detail="报价单不存在")
    from app.services.quota_match import suggest_for_quote

    return await suggest_for_quote(db, quote)


@router.post(
    "/{quote_id}/compute",
    response_model=ComputeResult,
    summary="重算报价（双行并列口径）",
)
async def compute(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "compute"))],
    # body 必须排在最后：Python 不允许「无默认值的参数跟在有默认值的参数后面」，
    # 放在 db/user 前面会直接 SyntaxError，整个 app 都 import 不起来。
    body: ComputeRequest | None = None,
) -> ComputeResult:
    quote = await _load_quote(db, quote_id)
    _assert_not_archived(quote)   # 归档单不可写费率（2026-10-08 补）
    if body:
        data = body.model_dump(exclude_unset=True)
        # 必须在写入循环之前摘掉 version：它不是 Quote 的字段，
        # 留着会被 setattr 到 ORM 实例上（还会绕过下面的比对）。
        await _check_version(db, quote, data.pop("version", None), actor=user)
        for key, value in data.items():
            if value is not None:
                if key == "labor_float_per_class":
                    value = _normalize_jsonable(value)
                setattr(quote, key, value)
    quote = await _recompute(db, quote, actor_id=user.id)
    # ★ 版本快照（2026-10-08 用户要求）：「每次保存并重算就应该生成一个版本，方便追溯，
    #   版本号以时间为标记」✓ —— 这个端点就是界面上那个「保存并重算」按钮 ✓。
    #
    # ⚠ 必须**只**在这条路径插版本 ✗ 不能放进 `_recompute()` ✗：
    #   加明细 / 改数量 / 改单价 / 删行 / 改费率 都会顺带调 `_recompute` ✓，
    #   放进去就等于"选一台录像机涨一格" ✗ —— 那正是用户上一个投诉 ✓。
    #
    # `dedupe=True`：快照与最新一版完全一致（没改任何东西又点了一次）就跳过 ✓，
    #   不造"同一份内容的一堆时间戳"这种噪声版本 ✓（用户的诉求是**可追溯** ✓，
    #   重复的空版本只会让历史更难读 ✗）。内容有变 → 一定落新版 ✓。
    rev = await save_revision(db, quote, reason="saved", user=user)
    # 把「这次有没有落版 / 落的是哪一版」交给界面 ✓（语义变了，提示必须跟上 ✓）：
    #   · rev 非 None → 刚落的这版就是最新一版，它的 created_at 取自
    #     attach_revision_stats 的 max(created_at)（库侧值 ✓，不用 refresh →
    #     避开 server_default 在 async 里触发的同步懒加载 MissingGreenlet ✗）；
    #   · rev 为 None（去重跳过）→ 不给时间 ✗，界面提示「内容未变化，未新增版本」✓。
    await attach_revision_stats(db, [quote])
    quote.revision_saved = rev is not None
    quote.revision_created_at = quote.latest_revision_at if rev is not None else None
    return _compute_result(quote, user)


@router.get("/{quote_id}/subsystems", summary="客户版：子系统汇总（含占比）")
async def subsystem_summary(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> dict:
    quote = await _load_quote(db, quote_id)
    subs = list(
        (
            await db.scalars(
                select(Subsystem)
                .where(Subsystem.project_id == quote.project_id)
                .order_by(Subsystem.sort_order)
            )
        ).all()
    )
    rows = []
    grand = Decimal("0")
    for s in subs:
        eq = Decimal(str(s.equipment_subtotal))
        ins = Decimal(str(s.install_subtotal))
        amount = eq + ins
        grand += amount
        rows.append(
            {
                "subsystem_id": str(s.id),
                "subsystem_type": str(s.subsystem_type),
                "name": s.custom_name or label_of("subsystem_type", s.subsystem_type),
                "point_count": s.point_count,
                "equipment_subtotal": float(eq),
                "install_subtotal": float(ins),
                "amount": float(amount),
            }
        )
    for r in rows:
        r["share"] = round(r["amount"] / float(grand) * 100, 2) if grand else 0.0

    return {
        "quote_id": str(quote.id),
        "quote_code": quote.code,
        "subsystems": rows,
        "grand_total": float(grand),
    }


# ---------------------------------------------------------------------------
# quote items
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# 分级审批（2026-10-04）
# ---------------------------------------------------------------------------

@router.get("/{quote_id}/approvals", summary="审批链状态（需要几级、已批几级）")
async def list_approvals(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> dict:
    from app.services.approval import all_approvals, approved_levels, can_finalize
    from app.services.settings import load_settings

    quote = await _load_quote(db, quote_id)
    settings = await load_settings(db)
    conf = settings.get("approval") or {}
    tiers = conf.get("tiers") or []
    enabled = bool(conf.get("enabled"))
    required = required_levels(quote.total_with_tax, tiers, enabled)
    approved = await approved_levels(db, quote.id)
    rows = await all_approvals(db, quote.id)
    return {
        "enabled": enabled,
        "required_levels": required,
        "approved_levels": len(approved),
        "can_finalize": can_finalize(approved, required),
        "thresholds": tiers,
        "records": [
            {
                "level": r.level,
                "decision": r.decision,
                "comment": r.comment,
                "approver_name": r.approver_name,
                "approver_role": r.approver_role,
                "required_levels": r.required_levels,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.post(
    "/{quote_id}/approvals",
    status_code=status.HTTP_201_CREATED,
    summary="审批一次（通过/驳回）；批满级数自动置为已批准",
)
async def decide_approval(
    quote_id: UUID,
    body: ApprovalDecision,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "approve"))],
) -> dict:
    from app.models.quote_approval import QuoteApproval
    from app.services.approval import (
        all_approvals,
        approved_levels,
        can_finalize,
        next_level,
    )
    from app.services.revisions import save_revision
    from app.services.settings import load_settings

    quote = await _load_quote(db, quote_id)
    if str(quote.status) not in (str(QuoteStatus.SUBMITTED), str(QuoteStatus.REVIEWING)):
        raise HTTPException(
            status_code=409,
            detail=f"当前状态 {quote.status} 不处于审批中（需要 submitted / reviewing）",
        )

    settings = await load_settings(db)
    conf = settings.get("approval") or {}
    enabled = bool(conf.get("enabled"))
    tiers = conf.get("tiers") or []
    required = required_levels(quote.total_with_tax, tiers, enabled)
    approved = await approved_levels(db, quote.id)

    comment = (body.comment or "").strip() or None
    if body.decision == "rejected" and conf.get("reject_requires_comment", True) and not comment:
        raise HTTPException(status_code=422, detail="驳回必须写明意见（改什么、为什么）")

    level = next_level(approved)
    record = QuoteApproval(
        quote_id=quote.id,
        level=level,
        required_levels=required,
        decision=body.decision,
        comment=comment,
        approver_id=user.id,
        approver_name=user.username,
        approver_role=str(user.role),
    )
    db.add(record)
    await db.flush()

    finalized = False
    if body.decision == "rejected":
        quote.status = QuoteStatus.REJECTED
        db.add(
            quote_audit(
                quote_id=quote.id, quote_code=quote.code, user_id=user.id,
                action=AuditAction.REJECT,
                payload={"level": level, "comment": comment},
            )
        )
    else:
        approved = await approved_levels(db, quote.id)
        if can_finalize(approved, required):
            quote.status = QuoteStatus.APPROVED
            quote.approved_at = datetime.now(tz=UTC)
            finalized = True
            await save_revision(db, quote, reason="approved", user=user)
            db.add(
                quote_audit(
                    quote_id=quote.id, quote_code=quote.code, user_id=user.id,
                    action=AuditAction.APPROVE,
                    payload={"level": level, "required_levels": required, "comment": comment},
                )
            )
        else:
            quote.status = QuoteStatus.REVIEWING
            db.add(
                quote_audit(
                    quote_id=quote.id, quote_code=quote.code, user_id=user.id,
                    action=AuditAction.APPROVE,
                    payload={
                        "level": level,
                        "required_levels": required,
                        "comment": comment,
                        "note": f"第 {level}/{required} 级通过，尚未批满",
                    },
                )
            )

    await db.flush()
    log.info(
        f"报价单 {quote.code} 第 {level}/{required} 级审批 {body.decision} by {user.username}"
        + ("（已批准）" if finalized else "")
    )
    # 审批结果通知报单的人（分级审批路径）
    from app.models.notification import NotificationKind
    from app.services.notifications import notify

    if body.decision == "rejected" or finalized:
        # 显式查询：`quote.project` 懒加载在 async 下会抛 MissingGreenlet
        owner = (
            await db.scalar(select(Project.owner_id).where(Project.id == quote.project_id))
            if quote.project_id
            else None
        )
        targets = [owner] if owner else []
        notify(
            db,
            user_ids=targets,
            kind=str(
                NotificationKind.QUOTE_REJECTED
                if body.decision == "rejected"
                else NotificationKind.QUOTE_APPROVED
            ),
            title=(
                f"报价单被驳回：{quote.code}"
                if body.decision == "rejected"
                else f"报价单已批准：{quote.code}"
            ),
            body=(comment or "") + f"（{user.username}）",
            # 通知链接必须指向**真实存在的路由**（2026-10-05 用户反馈 404 ✓）：
                    #   应用里没有 /quotes/:id ✗，正确路由是
                    #   /projects/:projectId/workbench/:quoteId ✓
                    link=f"/projects/{quote.project_id}/workbench/{quote.id}",
        )

    records = await all_approvals(db, quote.id)
    return {
        "level": level,
        "required_levels": required,
        "approved_levels": len([r for r in records if r.decision == "approved"]),
        "status": str(quote.status),
        "finalized": finalized,
    }

# ---------------------------------------------------------------------------
# 版本历史与对比（2026-10-04）
# ---------------------------------------------------------------------------

@router.get("/{quote_id}/revisions", summary="版本历史（提交/批准/手动存档）")
async def list_revisions(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> list[dict]:
    quote = await _load_quote(db, quote_id)
    rows = (
        await db.scalars(
            select(QuoteRevision)
            .where(QuoteRevision.quote_id == quote.id)
            .order_by(QuoteRevision.seq.desc())
        )
    ).all()
    return [
        {
            "id": str(r.id),
            "seq": r.seq,
            "version": r.version,
            "reason": r.reason,
            "status": r.status,
            "title": r.title,
            "totals": r.totals or {},
            "note": r.note,
            "created_by_name": r.created_by_name,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "item_count": len((r.snapshot or {}).get("items") or []),
        }
        for r in rows
    ]


@router.post(
    "/{quote_id}/revisions",
    status_code=status.HTTP_201_CREATED,
    summary="手动保存当前版本（谈判过程打点）",
)
async def create_revision(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "update"))],
    note: str | None = Query(None, max_length=255),
) -> dict:
    quote = await _load_quote(db, quote_id)
    _assert_editable(quote, user)
    rev = await save_revision(db, quote, reason="manual", user=user, note=note, dedupe=False)
    if rev is None:  # pragma: no cover - dedupe=False 时不会发生
        raise HTTPException(status_code=409, detail="无法保存版本")
    # 先 refresh 再读 created_at：它是 `server_default=func.now()`（库侧写入的），
    # 在 async 上下文里直接访问会触发**同步**懒加载 → MissingGreenlet ✗。
    # 界面按时间标记提示「已保存版本（2026-10-08 17:04:23）」需要这个值 ✓。
    await db.refresh(rev)
    return {
        "id": str(rev.id),
        "seq": rev.seq,
        "reason": rev.reason,
        "created_at": rev.created_at.isoformat() if rev.created_at else None,
    }


@router.get("/{quote_id}/revisions/diff", summary="两版之间的差异（含税合计差了多少钱）")
async def diff_revisions(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
    from_seq: int = Query(..., ge=1, description="旧版序号"),
    to_seq: int = Query(..., ge=1, description="新版序号"),
) -> dict:
    quote = await _load_quote(db, quote_id)
    rows = (
        await db.scalars(
            select(QuoteRevision).where(
                QuoteRevision.quote_id == quote.id,
                QuoteRevision.seq.in_([from_seq, to_seq]),
            )
        )
    ).all()
    by_seq = {r.seq: r for r in rows}
    if from_seq not in by_seq or to_seq not in by_seq:
        raise HTTPException(status_code=404, detail="指定的版本序号不存在")
    old, new = by_seq[from_seq], by_seq[to_seq]
    body = diff_snapshots(old.snapshot or {}, new.snapshot or {})
    body["totals"] = diff_totals(old.totals or {}, new.totals or {})
    body["from"] = {"seq": old.seq, "reason": old.reason, "created_at":
                    old.created_at.isoformat() if old.created_at else None,
                    "created_by_name": old.created_by_name}
    body["to"] = {"seq": new.seq, "reason": new.reason, "created_at":
                  new.created_at.isoformat() if new.created_at else None,
                  "created_by_name": new.created_by_name}
    return body

@router.get("/{quote_id}/items", response_model=list[QuoteItemOut], summary="报价单明细行")
async def list_items(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote_item", "read"))],
) -> list[QuoteItem]:
    quote = await _load_quote(db, quote_id)
    return sorted(quote.items, key=lambda i: (i.sort_order, str(i.id)))


@router.post(
    "/{quote_id}/items",
    response_model=QuoteItemOut,
    status_code=status.HTTP_201_CREATED,
    summary="添加明细行（来自设备库或手动录入）",
)
async def add_item(
    quote_id: UUID,
    body: QuoteItemCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote_item", "create"))],
) -> QuoteItem:
    quote = await _load_quote(db, quote_id)
    _assert_editable(quote, user)
    # 配件行只能挂在本单的主设备行上（2026-10-05）：
    # 不校验的话，跨单挂靠会让"这台防火墙配没配授权"的判断彻底失真。
    if body.parent_item_id:
        parent = await db.get(QuoteItem, body.parent_item_id)
        if not parent or parent.quote_id != quote.id:
            raise HTTPException(
                status_code=422, detail="parent_item_id 必须指向本报价单里的明细行"
            )
    data = _drop_none_for_not_null(QuoteItem, body.model_dump())
    # below_floor_reason 是「放行理由」，不是明细行字段（2026-10-04）
    data.pop("below_floor_reason", None)

    # 来自设备库 → 补齐单价/成本/单位，并做停产检查
    warnings: list[str] = []
    device = await db.get(Device, body.device_id) if body.device_id else None
    if body.device_id:
        if not device:
            raise HTTPException(status_code=404, detail="设备不存在")
        data.setdefault("unit", device.unit)
        data["unit"] = device.unit
        if not data.get("unit_price"):
            # 阶梯价（2026-10-04）：按数量取档；没有档位就退回参考价
            tier_price = pick_tier_price(device.price_tiers, data.get("quantity"))
            data["unit_price"] = tier_price if tier_price is not None else device.reference_price
            if tier_price is not None:
                warnings.append(f"按 {data.get('quantity')} 取阶梯价 {tier_price}")
            # 安装调试费改为**手动填写**（2026-10-05 用户要求）：
            # 不再从设备库带值 —— 设备库也不再按设备维护这个字段 ✓
        # 单位辅材费也跟着设备走（「按工程量」口径要用）
        if not data.get("auxiliary_unit_price"):
            data["auxiliary_unit_price"] = device.auxiliary_unit_price or Decimal("0")
        if data.get("cost_price") is None:
            data["cost_price"] = device.cost_price
        if body.brand is None:
            data["brand"] = device.brand
        if body.model is None:
            data["model"] = device.model
        # 设备参数（2026-10-05）：从设备描述带一份快照进报价单。
        # 只做快照不做实时读取 —— 报价单是对外文档，设备描述后来改了
        # 不该悄悄改写已经发出去的报价单（与价格快照同理）。
        if not (data.get("spec_text") or "").strip() and device.description:
            data["spec_text"] = device.description
        # 甲供要跟着设备库走：以前不回填，于是设备库里标了"甲供"的设备
        # 一旦进报价单就悄悄变成"我方供货"（设备费照算）。
        # 只有调用方明确传了 True 才覆盖（允许单行例外）。
        if not body.is_owner_supplied:
            data["is_owner_supplied"] = bool(device.is_owner_supplied)

        if str(device.status) in (DeviceStatus.EOL, DeviceStatus.NEAR_EOL):
            repl = await db.get(Device, device.replacement_id) if device.replacement_id else None
            warnings.append(
                f"设备 {device.code} 状态为 {device.status}"
                + (f"，建议替换为 {repl.code} {repl.name}" if repl else "，且未配置替代型号")
            )

    # 价格底线（2026-10-04）：低于底价要经理/管理员 + 理由，放行也留痕
    floor_note = check_price_floor(
        device=device, unit_price=data.get("unit_price"), user=user,
        reason=body.below_floor_reason,
    )

    item = QuoteItem(quote_id=quote.id, **data)
    db.add(item)
    await db.flush()

    if floor_note:
        db.add(
            quote_audit(
                quote_id=quote.id, quote_code=quote.code, user_id=user.id,
                action=AuditAction.PRICE_FLOOR_OVERRIDE,
                payload={"item_id": str(item.id), "name": item.name, "note": floor_note},
            )
        )
        log.warning(f"报价单 {quote.code} 低于底价放行 by {user.username}: {floor_note}")

    await _recompute(db, quote, actor_id=user.id)
    await db.refresh(item)
    if warnings:
        log.warning(f"报价单 {quote.code} 添加停产型号: {'; '.join(warnings)}")
    return item


@router.patch(
    "/{quote_id}/items/{item_id}",
    response_model=QuoteItemOut,
    summary="修改明细行（数量/单价）",
)
async def update_item(
    quote_id: UUID,
    item_id: UUID,
    body: QuoteItemUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote_item", "update"))],
) -> QuoteItem:
    quote = await _load_quote(db, quote_id)
    _assert_editable(quote, user)
    item = await db.get(QuoteItem, item_id)
    if not item or item.quote_id != quote.id:
        raise HTTPException(status_code=404, detail="明细行不存在")

    patch = _drop_none_for_not_null(QuoteItem, body.model_dump(exclude_unset=True))
    # 价格底线（2026-10-04）：改单价也要校验；显式给了单价才检查，
    # 只改数量时不误判（否则"少报一台"会因为阶梯价与实际单价对不上而被拦）。
    floor_note = None
    if "unit_price" in patch:
        device = await db.get(Device, item.device_id) if item.device_id else None
        floor_note = check_price_floor(
            device=device, unit_price=patch.get("unit_price"), user=user,
            reason=body.below_floor_reason,
        )
    patch.pop("below_floor_reason", None)

    for key, value in patch.items():
        setattr(item, key, value)
    await db.flush()

    if floor_note:
        db.add(
            quote_audit(
                quote_id=quote.id, quote_code=quote.code, user_id=user.id,
                action=AuditAction.PRICE_FLOOR_OVERRIDE,
                payload={"item_id": str(item.id), "name": item.name, "note": floor_note},
            )
        )
        log.warning(f"报价单 {quote.code} 明细改价低于底价放行 by {user.username}: {floor_note}")

    await _recompute(db, quote, actor_id=user.id)
    await db.refresh(item)
    return item


@router.delete(
    "/{quote_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除明细行",
)
async def delete_item(
    quote_id: UUID,
    item_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote_item", "delete"))],
) -> None:
    quote = await _load_quote(db, quote_id)
    _assert_editable(quote, user)
    item = await db.get(QuoteItem, item_id)
    if not item or item.quote_id != quote.id:
        raise HTTPException(status_code=404, detail="明细行不存在")
    await db.delete(item)
    await db.flush()
    await _recompute(db, quote, actor_id=user.id)


@router.post(
    "/{quote_id}/items/{item_id}/replace",
    response_model=QuoteItemOut,
    summary="把停产型号替换为指定替代型号（自动带出新单价）",
)
async def replace_item_device(
    quote_id: UUID,
    item_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote_item", "update"))],
    # 同 compute()：带默认值的参数必须排在最后，否则 SyntaxError。
    # Query(...) 也算「有默认值」。
    new_device_id: UUID = Query(..., description="替代型号的设备库 ID"),
) -> QuoteItem:
    quote = await _load_quote(db, quote_id)
    _assert_editable(quote, user)
    item = await db.get(QuoteItem, item_id)
    if not item or item.quote_id != quote.id:
        raise HTTPException(status_code=404, detail="明细行不存在")

    new_dev = await db.get(Device, new_device_id)
    if not new_dev:
        raise HTTPException(status_code=404, detail="替代型号不存在")
    if str(new_dev.status) in (DeviceStatus.EOL, DeviceStatus.DISCONTINUED):
        raise HTTPException(status_code=400, detail="替代型号本身已退市，不能作为替代")

    old = {
        "device_id": str(item.device_id),
        "model": item.model,
        "unit_price": float(item.unit_price),
        "is_owner_supplied": bool(item.is_owner_supplied),
    }
    item.device_id = new_dev.id
    item.name = new_dev.name
    item.brand = new_dev.brand
    item.model = new_dev.model
    item.unit = new_dev.unit
    item.unit_price = new_dev.reference_price
    # 换型号也**不动**安装费：它是手工填的，换设备不该把它冲掉（2026-10-05）
    item.auxiliary_unit_price = new_dev.auxiliary_unit_price or Decimal("0")
    item.cost_price = new_dev.cost_price
    # 甲供属性跟着替代型号走（与 add_item 同一口径）
    item.is_owner_supplied = bool(new_dev.is_owner_supplied)
    item.is_replaced = True

    db.add(
        quote_audit(
            quote_id=quote.id,
            quote_code=quote.code,
            user_id=user.id,
            action=AuditAction.REPLACE_DEVICE,
            payload={"item_id": str(item.id), "from": old, "to": new_dev.code},
        )
    )
    await db.flush()
    await _recompute(db, quote, actor_id=user.id)
    await db.refresh(item)
    return item


@router.get(
    "/{quote_id}/export-clauses",
    summary="客户版报价说明条款 + 公司抬头（与 PDF 导出同一出处）",
)
async def quote_export_clauses(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> dict:
    """客户版预览页要用它渲染「报价说明」，保证与导出的 PDF 完全一致。"""
    exists = await db.scalar(select(Quote.id).where(Quote.id == quote_id))
    if exists is None:
        raise HTTPException(status_code=404, detail="报价单不存在")
    # 条款与有效期：**单据级覆盖 → 系统设置 → 常量**（与 PDF 导出同一出处，
    # 见 export_data._export_settings）。返回渲染后的文本 + 原始模板 + 变量，
    # 前端既能显示"最终发给客户的样子"，也能显示"本单是否自定义过"。
    from app.services.quote_terms import CLAUSE_VARIABLES

    quote = await db.get(Quote, quote_id)
    s = await load_export_settings(db)
    exp = _export_settings(
        s,
        settings_overrides={
            "clauses": getattr(quote, "clauses_override", None),
            "valid_days": getattr(quote, "valid_days_override", None),
            # 每单条款数字（2026-10-05）✓：本单改过就用本单的 ✓
            "terms_override": getattr(quote, "terms_override", None),
        },
    )
    return {
        "clauses": list(exp["clauses"]),
        "clause_template": list(exp["clause_template"]),
        "valid_days": exp["valid_days"],
        "variables": exp["variables"],
        "available_variables": CLAUSE_VARIABLES,
        "clauses_overridden": exp["clauses_overridden"],
        "valid_days_overridden": exp["valid_days_overridden"],
        # 供前端把"本单数字"填进输入框 ✓（未覆盖就是系统设置里的生效值 ✓）
        "terms_override": getattr(quote, "terms_override", None),
        "terms_overridden": exp["terms_overridden"],
        "company": s["company"],
    }


@router.get("/{quote_id}/cost", summary="对内视图：成本与利润")
async def cost_view(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> dict:
    """对内视图：**成本**与**利润**是两档权限（2026-10-05 起）。

    审计发现（2026-10-05）：这里原来只判了"成本"，于是"成本可见、利润不可见"的角色
    （例如只把 `data_cost` 开给工程部）能从这里读到毛利。
    现在按两档分别给：成本字段照给，**利润字段无权限时置空**并说明原因。
    """
    show_cost = _can_see_cost(user)
    show_profit = _can_see_profit(user)
    if not show_cost and not show_profit:
        raise HTTPException(status_code=403, detail="无权查看成本与利润信息")
    quote = await db.get(Quote, quote_id)
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")
    # H-10：看成本本身是敏感动作，留一条痕（谁在什么时候看了哪张单的成本/毛利）
    db.add(
        quote_audit(
            quote_id=quote.id,
            quote_code=quote.code,
            user_id=user.id,
            action=AuditAction.COST_VIEW,
            payload={"gross_profit_rate": str(quote.gross_profit_rate)},
        )
    )
    await db.commit()
    return {
        "quote_id": str(quote.id),
        "quote_code": quote.code,
        "total_with_tax": float(quote.total_with_tax),
        "cost_total": float(quote.cost_total) if show_cost else None,
        # 利润与成本**分开判**：只有成本权限的人拿不到毛利（审计发现的越权点）
        "gross_profit": float(quote.gross_profit) if show_profit else None,
        "gross_profit_rate": float(quote.gross_profit_rate) if show_profit else None,
        "profit_hidden_reason": (
            None if show_profit else "当前账号没有利润权限（权限矩阵里的「利润/毛利」行）"
        ),
        "equipment_total": float(quote.equipment_total),
        "install_total": float(quote.install_total),
        "auxiliary_total": float(quote.auxiliary_total),
    }


@router.get("/{quote_id}/audit-logs", summary="操作留痕")
async def audit_logs(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("audit_log", "read"))],
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    rows = await db.scalars(
        select(QuoteAuditLog)
        .where(QuoteAuditLog.quote_id == quote_id)
        .order_by(QuoteAuditLog.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(r.id),
            "user_id": str(r.user_id) if r.user_id else None,
            "action": str(r.action),
            "payload": r.payload,
            "note": r.note,
            # H-10：留痕要能回答"谁、从哪来的"
            "ip": r.ip,
            "user_agent": r.user_agent,
            "subject_code": r.subject_code,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows.all()
    ]













@router.get("/{quote_id}/accessory-check", summary="配单检查：哪些必配配件还没配")
async def quote_accessory_check(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "read"))],
) -> dict:
    """列出"主设备行 + 它的必配槽 + 一个都没配"的组合，并给出候选。

    为什么要有这个（而不是只在"加明细时弹一次"）：用户可能是**先加了防火墙、
    后来才想起来要配授权**，也可能是这份单子是配件功能上线前建的。
    所以检查放在"整单"层面，随时能跑，界面据此常驻提示。

    判定：某个主设备行下，若槽的候选里**一个都没出现在配件行**里 → 算没配。
    """
    from app.models.device import DeviceAccessoryGroup

    quote = await _load_quote(db, quote_id)
    items = list(quote.items or [])
    children: dict[UUID, list] = {}
    for it in items:
        if it.parent_item_id:
            children.setdefault(it.parent_item_id, []).append(it)

    missing: list[dict] = []
    for it in items:
        if not it.device_id or it.parent_item_id:
            continue
        groups = (
            await db.scalars(
                select(DeviceAccessoryGroup)
                .where(
                    DeviceAccessoryGroup.device_id == it.device_id,
                    DeviceAccessoryGroup.is_active.is_(True),
                )
                .order_by(DeviceAccessoryGroup.sort_order)
            )
        ).all()
        if not groups:
            continue
        kids = children.get(it.id, [])
        opt_ids = [o.accessory_id for g in groups for o in g.options]
        devs: dict[UUID, Device] = {}
        if opt_ids:
            rows = (await db.scalars(select(Device).where(Device.id.in_(opt_ids)))).all()
            devs = {d.id: d for d in rows}
        for g in groups:
            cand = {o.accessory_id for o in g.options}
            chosen = [k.name for k in kids if k.device_id in cand]
            if chosen:
                continue                       # 配上了（不论必配与否）就不提
            if not g.is_required:
                continue                       # 非必配且没配 → 不打扰
            missing.append({
                "item_id": it.id,
                "item_name": it.name,
                "device_id": it.device_id,
                "device_code": it.device.code if it.device else None,
                "quantity": it.quantity,
                "group_label": g.label,
                "is_required": g.is_required,
                "select_mode": g.select_mode,
                "quantity_mode": g.quantity_mode,
                "default_quantity": g.default_quantity,
                "chosen": chosen,
                "options": [
                    {
                        "accessory_id": devs[o.accessory_id].id,
                        "code": devs[o.accessory_id].code,
                        "name": devs[o.accessory_id].name,
                        "brand": devs[o.accessory_id].brand,
                        "model": devs[o.accessory_id].model,
                        "unit": devs[o.accessory_id].unit,
                        "reference_price": devs[o.accessory_id].reference_price,
                        "is_default": o.is_default,
                        "note": o.note,
                        "sort_order": o.sort_order,
                    }
                    for o in g.options
                    if o.accessory_id in devs
                ],
            })

    if not missing:
        summary = "配件齐全：本单所有必配配件都已选择"
    else:
        labels = "、".join(sorted({m["group_label"] for m in missing}))
        summary = f"有 {len(missing)} 项必配配件还没选（{labels}），建议补齐后再出单"

    return {"quote_id": quote.id, "missing": missing, "summary": summary}
