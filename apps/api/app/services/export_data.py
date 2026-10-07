"""导出用的「中间数据」：把 ORM 对象摊平成纯数值 + 已格式化字符串。

为什么要多这一层（而不是让 Excel / PDF 两个渲染器各自去读 ORM）：

1. **业务判断只能有一份。** 「客户版要不要含成本」「甲供设备算不算钱」
   「取费基数该显示哪个中文」这些判断如果写在两个渲染器里，迟早会出现
   Excel 有成本列、PDF 没有这种要命的偏差。
2. **渲染器保持哑巴。** 两个渲染器只认 `ExportContext` 里的字符串和 Decimal，
   不碰 SQLAlchemy、不碰权限、不碰币种格式，因此可以单独测、互不干扰。
3. **成本泄露是安全边界，不是排版问题。** `include_cost` 只有一处判定，
   且**必须**由调用方按 `_can_see_cost` 的结果传进来；本模块只负责执行。

⚠ 本模块只做「取数 + 格式化」，不做任何 I/O，也**不查数据库**。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from app.core.labels import label_of
from app.services.quote_terms import build_variables, render_clauses

# 金额一律两位小数、四舍五入（与 quote_compute 的 QTY_2 口径一致），
# 保证导出件上的数字和界面上看到的一分不差。
_Q2 = Decimal("0.01")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 报价说明条款（**单一出处**）
# ---------------------------------------------------------------------------
# 原型第 3 页「报价说明」的 6 条。以前只存在于设计稿里：PDF 导出没有、前端预览也没有。
# 现在放这里，PDF 导出（export_pdf）与前端客户版预览（/quotes/{id}/export-clauses）都读它，
# 保证"预览上看到的 = 发给客户的 PDF 上的"。
# 条款里的数字用**变量**写（{valid_days} 等），渲染时从「系统设置」的结构化字段取值 ——
# 详见 services/quote_terms.py。以前这些数字是写死在文本里的：改了有效天数，
# 条款那句还写着旧数字，给客户的报价单自相矛盾。
QUOTE_CLAUSES: tuple[str, ...] = (
    "本报价含设备供货、辅材、安装调试、系统联调及首年免费维保；",
    "报价有效期 {valid_days} 天；设备价格随市场波动，逾期需重新确认；",
    "付款方式：合同签订后预付 {prepay_ratio}%，验收合格付 {accept_ratio}%，"
    "质保金 {warranty_ratio}% 一年后付；",
    "工期：合同签订并收到预付款后 {build_days} 个日历天内完成安装调试；",
    "质保期：整体系统 {warranty_months} 个月，设备按厂家标准执行；",
    "本报价不含：土建配合、桥架施工、强电引至点位、运营商线路费用。",
)
# 报价有效期天数（第 2 条引用它；默认值在 quote_terms.DEFAULT_TERMS）
QUOTE_VALID_DAYS = 30


@dataclass(frozen=True)
class ExportMeta:
    """封面/表头信息。所有金额以外的字段都已转成可直接打印的字符串。"""

    quote_code: str
    title: str
    version: int
    status_label: str
    project_code: str
    project_name: str
    customer_name: str
    customer_contact: str
    customer_phone: str
    address: str
    tax_method_label: str
    tax_rate_pct: str          # 已乘 100 并带 % 号，如 "9%"
    generated_at: str          # 本地时间，如 "2026-09-29 07:00"
    include_cost: bool
    # 是否输出「设备参数」列（2026-10-05 ✓）：
    #   客户版 PDF 不出 ✓（用户要求 C：横向列太挤 ✓，参数会把价格挤成竖排 ✓）；
    #   Excel 与内部版保持原样 ✓。默认 True = 老调用点行为不变 ✓。
    show_spec_column: bool = True
    # 利润是**独立一档**权限（2026-10-05 审计发现：只给成本却带出了毛利）。
    # None = 跟随 include_cost（老调用点行为不变）。
    include_profit: bool | None = None
    notes: str = ""
    # 客户版要印「报价日期」与「有效期至」（原型第 3 页信息表里有这两项）
    quote_date: str = "—"
    valid_until: str = "—"
    # 公司抬头与报价说明（2026-10-04 起来自「系统设置」，缺项回落到 DEFAULTS）。
    # 以前这几处写死在 export_excel/export_pdf 里，改抬头要改代码。
    company_name: str = ""
    company_address: str = ""
    company_phone: str = ""
    company_email: str = ""
    clauses: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportFeeRow:
    """费用瀑布的一行。

    两种计价口径**并列**显示（就是界面上的「双行并列」）：
    `amount_point` 是点数单价口径，`amount_quota` 是定额口径。
    某一级在某一口径下不存在时该列为空串，渲染器遇到空串要留空而不是补 0
    —— 补 0 会让甲方以为「这一项是零元」，而实际是「这个口径里没有这一项」。
    """

    label: str
    amount_point: str
    amount_quota: str
    emphasis: bool = False     # 合计行加粗
    separator: bool = False    # 该行上方画一条分隔线


@dataclass(frozen=True)
class ExportSubsystemRow:
    seq: int
    name: str
    point_count: str
    equipment: str
    install: str
    auxiliary: str
    amount: str


@dataclass(frozen=True)
class ExportItemRow:
    seq: int
    subsystem: str
    name: str
    brand: str
    model: str
    unit: str
    quantity: str
    unit_price: str
    amount: str
    source_label: str
    note: str
    # 下面三列只有在 include_cost 为真时才有值，否则是空串
    # 设备参数（2026-10-05）：报价单明细的一栏，来自设备描述快照
    spec_text: str = ""
    cost_price: str = ""
    cost_amount: str = ""
    gross: str = ""


@dataclass(frozen=True)
class ExportContext:
    meta: ExportMeta
    fees: list[ExportFeeRow] = field(default_factory=list)
    subsystems: list[ExportSubsystemRow] = field(default_factory=list)
    items: list[ExportItemRow] = field(default_factory=list)

    @property
    def include_cost(self) -> bool:
        return self.meta.include_cost

    @property
    def show_spec(self) -> bool:
        """是否输出「设备参数」列。

        **客户版 PDF 不输出**（2026-10-05 用户要求 C ✓）：
          A4 横向 14 列本就很紧 ✓，参数列一长就把「设备名称/品牌/型号/价格」
          挤成竖排 ✓（实测：30% 上限允许它独吞 79mm ✗）→ 客户看着难受 ✓。
          参数只在**内部版**（含成本的版本）保留 ✓ —— 那是给自己看的 ✓。
        """
        if not getattr(self.meta, "show_spec_column", True):
            return False
        return any((row.spec_text or "").strip() for row in self.items)

    @property
    def show_profit(self) -> bool:
        """能否印毛利（独立一档权限；未指定时跟随 include_cost）。"""
        return self.meta.include_cost if self.meta.include_profit is None else bool(
            self.meta.include_profit
        )

    # ---- 渲染器要用的列定义放在这里，避免两个渲染器各写一套表头 ----
    def item_headers(self) -> list[str]:
        cols = ["序号", "子系统", "设备/材料名称", "品牌", "型号"]
        if self.show_spec:
            cols.append("设备参数")
        cols += ["单位", "数量", "单价", "合计"]
        if self.include_cost:
            cols += ["进价", "成本小计"]
        if self.show_profit:
            cols += ["毛利"]
        cols += ["来源", "备注"]
        return cols

    def item_row_cells(self, row: ExportItemRow) -> list[str]:
        cols = [
            str(row.seq), row.subsystem, row.name, row.brand, row.model,
        ]
        if self.show_spec:
            cols.append(row.spec_text)
        cols += [row.unit, row.quantity, row.unit_price, row.amount]
        if self.include_cost:
            cols += [row.cost_price, row.cost_amount]
        if self.show_profit:
            cols += [row.gross]
        cols += [row.source_label, row.note]
        return cols

    def subsystem_headers(self) -> list[str]:
        return ["序号", "子系统", "点位数", "设备费", "安装费", "辅材费", "小计"]

    def subsystem_row_cells(self, row: ExportSubsystemRow) -> list[str]:
        return [str(row.seq), row.name, row.point_count, row.equipment,
                row.install, row.auxiliary, row.amount]

    def fee_headers(self) -> list[str]:
        return ["费用项", "点数单价口径", "定额计价口径"]


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def money(value: Any, *, blank_if_zero: bool = False) -> str:
    """金额 → 千分位两位小数字符串。None → 空串（**不是 0**）。

    None 与 0 必须区分：None 表示「这个口径没有这一项」，0 表示「确实是零元」。
    """
    if value is None:
        return ""
    if isinstance(value, str) and not value.strip():
        return ""
    d = Decimal(str(value)).quantize(_Q2, rounding=ROUND_HALF_UP)
    if blank_if_zero and d == 0:
        return ""
    return f"{d:,.2f}"


def qty_text(value: Any) -> str:
    """数量：去掉无意义的尾随零（35.000 → 35，0.500 → 0.5）。"""
    if value is None:
        return ""
    d = Decimal(str(value)).normalize()
    if d == d.to_integral_value():
        return f"{d:.0f}"
    return format(d, "f")


def rate_pct_text(value: Any) -> str:
    """比率 → 百分数字符串（0.09 → "9%"）。"""
    if value is None:
        return ""
    d = (Decimal(str(value)) * 100).normalize()
    if d == d.to_integral_value():
        return f"{d:.0f}%"
    return f"{format(d, 'f')}%"


def _s(value: Any) -> str:
    """任意值 → 去空格字符串；None → 空串。"""
    if value is None:
        return ""
    return str(value).strip()


# ---------------------------------------------------------------------------
# 构建
# ---------------------------------------------------------------------------

def build_fee_rows(
    quote: Any, *, include_cost: bool, include_profit: bool | None = None
) -> list[ExportFeeRow]:
    """按 quote_compute 的费用瀑布顺序拼出费用表。

    顺序必须与 services/quote_compute.py 的瀑布一致，否则甲方逐项核对时会串行。

    `include_profit`：**毛利/毛利率是独立一档权限**（2026-10-05 审计发现的越权点）——
    只要成本权限就带出毛利，等于把两档权限合并了。默认跟随 include_cost（老调用点行为不变）。
    """
    if include_profit is None:
        include_profit = include_cost
    rows = [
        ExportFeeRow("设备费", money(quote.equipment_total), ""),
        ExportFeeRow("安装费", money(quote.install_total), ""),
        ExportFeeRow("辅材费", money(quote.auxiliary_total), ""),
        ExportFeeRow("设计费", money(quote.design_fee), ""),
        ExportFeeRow("维保费", money(quote.warranty_fee), ""),
        ExportFeeRow("培训费", money(quote.training_fee), ""),
        ExportFeeRow("小计（不含税）", money(quote.subtotal_before_tax),
                     money(quote.quota_total), separator=True),
        ExportFeeRow("管理费及利润", money(quote.mgmt_profit), ""),
        ExportFeeRow("税金", money(quote.tax_amount), ""),
        ExportFeeRow("含税合计", money(quote.total_with_tax),
                     money(quote.quota_total_with_tax), emphasis=True, separator=True),
    ]
    if include_cost:
        rows += [
            ExportFeeRow("成本合计", money(quote.cost_total), "", separator=True),
        ]
    # ⚠ 毛利/毛利率是**独立一档权限**（2026-10-05 审计发现的越权点）：
    # 以前只要成本权限就带出毛利，等于把两档权限合并了。
    if include_profit:
        rows += [
            ExportFeeRow("毛利", money(quote.gross_profit), ""),
            ExportFeeRow("毛利率", rate_pct_text(quote.gross_profit_rate), ""),
        ]
    return rows


def _subsystem_sort_key(s: Any) -> tuple[int, str]:
    return (
        getattr(s, "sort_order", 0) or 0,
        label_of("subsystem_type", getattr(s, "subsystem_type", "")),
    )


def ordered_subsystems(subsystems: Iterable[Any]) -> list[Any]:
    """按 sort_order 排，同序时按中文名。两次导出顺序必须一致，否则对单会串行。"""
    return sorted(subsystems, key=_subsystem_sort_key)


def build_subsystem_rows(subsystems: Iterable[Any]) -> list[ExportSubsystemRow]:
    """子系统小计。"""
    out: list[ExportSubsystemRow] = []
    for i, s in enumerate(ordered_subsystems(subsystems), start=1):
        eq = Decimal(str(s.equipment_subtotal or 0))
        ins = Decimal(str(s.install_subtotal or 0))
        aux = Decimal(str(s.auxiliary_subtotal or 0))
        out.append(
            ExportSubsystemRow(
                seq=i,
                name=_s(getattr(s, "custom_name", "")) or label_of(
                    "subsystem_type", getattr(s, "subsystem_type", "")
                ),
                point_count=qty_text(getattr(s, "point_count", None)),
                equipment=money(eq),
                install=money(ins),
                auxiliary=money(aux),
                amount=money(eq + ins),
            )
        )
    return out


def build_item_rows(
    items: Sequence[Any],
    *,
    subsystem_names: dict[str, str],
    include_cost: bool,
) -> list[ExportItemRow]:
    """明细行。**甲供设备照样列出来**，但金额列留空。

    甲供（is_owner_supplied）不计入我方报价，钱是甲方出的。这里保留行、
    清空单价与合计，是为了让甲方看到「这台设备由你方提供」而不是把它藏掉。
    （与 quote_compute 的口径一致：甲供行的设备费按 0 计入合计、安装费照计。）
    """
    out: list[ExportItemRow] = []
    for i, it in enumerate(items, start=1):
        owner = bool(getattr(it, "is_owner_supplied", False))
        qty = Decimal(str(it.quantity or 0))
        unit_price = Decimal(str(it.unit_price or 0))
        amount = Decimal(str(it.equipment_amount or 0))
        sub = subsystem_names.get(str(getattr(it, "subsystem_id", "") or ""), "")

        cost_price_txt = cost_amount_txt = gross_txt = ""
        estimated = False
        if include_cost:
            cp = Decimal(str(getattr(it, "cost_price", 0) or 0))
            # 与 quote_compute 同一判定：没有进价的行，成本是按设备价 ×75% 估算的。
            # 必须在行上标出来 —— 否则内部版的"毛利"列里混着编造的数字，
            # 看报表的人（和据此做决策的人）完全无法区分。
            estimated = (not owner) and cp <= 0
            if estimated:
                cp = (unit_price * Decimal("0.75")).quantize(_Q2, rounding=ROUND_HALF_UP)
            cost_amount = (qty * cp).quantize(_Q2, rounding=ROUND_HALF_UP)
            cost_price_txt = money(cp)
            cost_amount_txt = money(cost_amount)
            if not owner:
                gross_txt = money(amount - cost_amount)

        note = _s(getattr(it, "notes", ""))
        if owner and "甲供" not in note:
            note = f"甲供（不计入设备费）{('；' + note) if note else ''}"
        if estimated and "估算" not in note:
            note = f"成本按设备价×75%估算{('；' + note) if note else ''}"
        if getattr(it, "is_replaced", False):
            note = f"已替换同档型号{('；' + note) if note else ''}"

        out.append(
            ExportItemRow(
                seq=i,
                subsystem=sub,
                # 子目行（挂在主设备下 ✓）名称前加缩进 → 导出/打印呈广联达式层级 ✓
                # （2026-10-05 阶段2；只动显示，不动任何金额 ✓）
                name=(('　　└ ' + _s(it.name))
                      if getattr(it, 'parent_item_id', None) else _s(it.name)),
                brand=_s(getattr(it, "brand", "")),
                model=_s(getattr(it, "model", "")),
                unit=_s(getattr(it, "unit", "")) or "台",
                quantity=qty_text(qty),
                # 甲供：金额列留空，不写 0
                # 子目行（定额子目 ✓）的钱在 install_unit_price（=定额基价 ✓）里，
                # 而 unit_price 是 0 ✗ → 这里把它并进「单价/合计」✓
                # 否则导出里子目那两格是 0，看不出安装钱 ✓（2026-10-05 阶段2）
                unit_price=("" if owner else
                            money(unit_price + (getattr(it, "install_unit_price", 0)
                                                if getattr(it, "parent_item_id", None) else 0))),
                amount=("" if owner else
                        money(amount + (qty * getattr(it, "install_unit_price", 0)
                                        if getattr(it, "parent_item_id", None) else 0))),
                source_label=label_of("item_source", getattr(it, "source", "")),
                note=note,
                spec_text=_s(getattr(it, "spec_text", "")),
            cost_price=cost_price_txt,
                cost_amount=cost_amount_txt,
                gross=gross_txt,
            )
        )
    return out


def _quote_date(quote: Any) -> str:
    """报价日期：用报价单创建时间（本地日期）。"""
    created = getattr(quote, "created_at", None)
    if created is None:
        return ""
    try:
        return created.astimezone().strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return str(created)[:10]


def _valid_until(quote: Any, valid_days: int = QUOTE_VALID_DAYS) -> str:
    """有效期至 = 报价日期 + 有效期天数（默认 30 天，可在「系统设置」里改）。"""
    created = getattr(quote, "created_at", None)
    if created is None:
        return ""
    try:
        return (created.astimezone() + timedelta(days=valid_days)).strftime("%Y-%m-%d")
    except (AttributeError, ValueError):
        return ""


def _export_settings(
    settings: dict | None, settings_overrides: dict | None = None
) -> dict[str, Any]:
    """把「系统设置」里的公司与报价口径整理成导出用的一小撮值。

    `settings` 传 None（老调用点、单测）时全部回落到模块常量 —— 行为与加设置之前一致。
    """
    company = dict((settings or {}).get("company") or {})
    quote = dict((settings or {}).get("quote") or {})

    # 取值优先级：**单据级覆盖 → 系统设置 → 模块常量**（2026-10-05）
    overrides = settings_overrides or {}
    valid_days = int(
        overrides.get("valid_days")
        or quote.get("valid_days")
        or QUOTE_VALID_DAYS
    )
    override_clauses = overrides.get("clauses")
    template = (
        tuple(override_clauses)
        if override_clauses
        else (tuple(quote.get("clauses")) if quote.get("clauses") else QUOTE_CLAUSES)
    )
    # 变量：结构化数字（有效期用最终生效值，覆盖后变量也要跟着变）
    # 每单条款数字（2026-10-05）：本单覆盖 → 系统设置 → 内置默认 ✓
    variables = build_variables(
        quote, valid_days=valid_days, overrides=overrides.get("terms_override")
    )
    return {
        "company_name": company.get("name") or "",
        "company_address": company.get("address") or "",
        "company_phone": company.get("phone") or "",
        "company_email": company.get("email") or "",
        "valid_days": valid_days,
        # 渲染后的条款（给 PDF/预览直接用）
        "clauses": render_clauses(template, variables),
        # 原始模板与变量也带出来：设置页/接口要给用户看"这句话是怎么来的"
        "clause_template": template,
        "variables": variables,
        "clauses_overridden": bool(override_clauses),
        "valid_days_overridden": bool(overrides.get("valid_days")),
        "terms_overridden": bool(overrides.get("terms_override")),
    }


def _quote_overrides(quote: Any) -> dict:
    """从报价单上读"本单自定义"（没设就是空 dict）。

    字段是 `clauses_override` / `valid_days_override` / `terms_override`；
    都可为 NULL，表示"用全局设置"。这样单据级覆盖不会污染其它报价单。

    `terms_override`（2026-10-05）：每单可不同的 6 个条款数字 ✓
    （有效期/预付/验收/质保金/质保期/工期 ✓）。
    """
    return {
        "terms_override": getattr(quote, "terms_override", None),
        "clauses": getattr(quote, "clauses_override", None),
        "valid_days": getattr(quote, "valid_days_override", None),
    }


def build_context(
    *,
    quote: Any,
    project: Any,
    items: Sequence[Any],
    subsystems: Sequence[Any],
    include_cost: bool,
    # 利润是**独立一档**权限（2026-10-05）：不给就剥掉毛利行。
    # None = 跟随 include_cost（老调用点行为不变）。
    include_profit: bool | None = None,
    # 客户版 PDF 传 False ✓（Excel 不传 → 保持原样 ✓）
    show_spec_column: bool = True,
    generated_at: datetime | None = None,
    settings: dict | None = None,
) -> ExportContext:
    """把 ORM 对象摊平成渲染器能直接吃的 `ExportContext`。

    `include_cost` 由调用方按 `_can_see_cost(user)` 决定；本函数不做权限判断，
    只负责执行「含成本 / 不含成本」两种形态。
    """
    now = generated_at or datetime.now()
    ordered = ordered_subsystems(subsystems)
    subs = build_subsystem_rows(ordered)
    # strict=True：两个列表都由同一份 ordered 生成，长度必然相等。
    # 写 strict 是为了万一以后有人改了其中一个的过滤逻辑，这里会立刻炸而不是错位。
    name_by_id = {
        str(getattr(s, "id", "")): row.name for s, row in zip(ordered, subs, strict=True)
    }
    # 单据级覆盖（每张单可自定义条款/有效期）→ 全局设置 → 常量
    _exp = _export_settings(settings, settings_overrides=_quote_overrides(quote))

    meta = ExportMeta(
        quote_code=_s(getattr(quote, "code", "")),
        title=_s(getattr(quote, "title", "")),
        version=int(getattr(quote, "version", 1) or 1),
        status_label=label_of("quote_status", getattr(quote, "status", "")),
        project_code=_s(getattr(project, "code", "")),
        project_name=_s(getattr(project, "name", "")),
        customer_name=_s(getattr(quote, "customer_name", ""))
        or _s(getattr(project, "customer_name", "")),
        customer_contact=_s(getattr(quote, "customer_contact", ""))
        or _s(getattr(project, "customer_contact", "")),
        customer_phone=_s(getattr(quote, "customer_phone", ""))
        or _s(getattr(project, "customer_phone", "")),
        address=_s(getattr(project, "address", "")),
        tax_method_label=label_of("tax_method", getattr(quote, "tax_method", "")),
        tax_rate_pct=rate_pct_text(getattr(quote, "tax_rate", None)),
        generated_at=now.strftime("%Y-%m-%d %H:%M"),
        include_cost=include_cost,
        show_spec_column=show_spec_column,
        include_profit=include_profit,
        notes=_s(getattr(quote, "notes", "")),
        quote_date=(_quote_date(quote) or "—"),
        valid_until=(_valid_until(quote, _exp["valid_days"]) or "—"),
        company_name=_exp["company_name"],
        company_address=_exp["company_address"],
        company_phone=_exp["company_phone"],
        company_email=_exp["company_email"],
        clauses=_exp["clauses"],
    )
    return ExportContext(
        meta=meta,
        fees=build_fee_rows(
            quote, include_cost=include_cost, include_profit=include_profit
        ),
        subsystems=subs,
        items=build_item_rows(items, subsystem_names=name_by_id, include_cost=include_cost),
    )


__all__ = [
    "ExportContext", "ExportFeeRow", "ExportItemRow", "ExportMeta", "ExportSubsystemRow",
    "build_context", "build_fee_rows", "build_item_rows", "build_subsystem_rows",
    "money", "qty_text", "rate_pct_text",
]





