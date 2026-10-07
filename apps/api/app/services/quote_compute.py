"""Quote computation: 双行并列报价 (点位单价 + 定额计价).

This module implements the entire fee waterfall:

    equipment_total      = Σ (qty × unit_price)        [设备费；甲供行按 0 计]
    install_total        = Σ (qty × install_unit_price) [安装调试费，甲供行照计]
    auxiliary_total      = based on auxiliary_base + rate  [辅材费]
    design_fee           = fee_base × design_rate
    warranty_fee         = equipment_total × warranty_rate
    training_fee         = (passed in)
    subtotal_before_tax  = equipment + install + aux + design + warranty + training
    mgmt_profit          = subtotal_before_tax × mgmt_profit_rate
    tax_amount           = (subtotal_before_tax + mgmt_profit) × tax_rate
    total_with_tax       = subtotal_before_tax + mgmt_profit + tax_amount

    cost_total           = Σ (qty × cost_price)；没有进价的行按 qty × unit_price × 0.75
                           估算（估算行数与金额会写进 warnings）
    gross_profit         = subtotal_before_tax − cost_total   [不含税收入 − 成本]
    gross_profit_rate    = gross_profit / subtotal_before_tax  [夹在 Numeric(5,4) 范围内]

Quota口径 (定额计价) is computed separately via定额 table × 工程量 × 采用单价 × 工艺系数
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.models.labor import LaborClass, LaborRate, QuotaItem
from app.models.quote import AuxiliaryBaseType, FeeBaseType, Quote
from app.models.quote_item import QuoteItem

# 两位小数舍入 (人民币金额)
QTY_2 = Decimal("0.01")
# 四位小数舍入 (比率，如毛利率 0.5011 = 50.11%)
QTY_4 = Decimal("0.0001")

# 比率的可存储范围：gross_profit_rate 是 Numeric(5,4)，只能表示 |x| < 10。
# 不夹的话极端单（成本远大于收入，或"待询价"行带库内成本）会触发
# PostgreSQL 的 numeric field overflow → 500，而不是一个能看的数。
RATE_MIN = Decimal("-9.9999")
RATE_MAX = Decimal("9.9999")


def _clamp_rate(value: Decimal) -> Decimal:
    """把比率夹进 Numeric(5,4) 能表示的范围。"""
    return max(RATE_MIN, min(RATE_MAX, value))


def _q(value: Decimal | float | int) -> Decimal:
    """Round a money amount to 2 decimal places using HALF_UP."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(QTY_2, rounding=ROUND_HALF_UP)


def _r(value: Decimal | float | int) -> Decimal:
    """Round a ratio to 4 decimal places (Numeric(5,4) columns)."""
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(QTY_4, rounding=ROUND_HALF_UP)


def _class_labor(days: object, price_per_day: object, float_pct: object) -> Decimal:
    """某一类工日的定额人工费 = 工日消耗量 × 当期信息价 × (1 + 上浮率)。

    抽出来是因为三类工日各算一遍、公式完全一样，写三遍长表达式既容易写错，
    也必然超出行长上限。
    """
    return (
        Decimal(str(days))
        * Decimal(str(price_per_day))
        * (Decimal("1") + Decimal(str(float_pct)))
    )


def effective_labor_float(
    overall: object, per_class: object
) -> Decimal:
    """一类工日的**有效上浮率** = 整体浮动 + 该类浮动。

    2026-10-03 修的静默失效：原来 `labor_float_rate`（整体浮动）只在
    `labor_float_per_class` **为空**时被摊到三类上（见旧版 api/v1/quotes.py
    的 `if not float_per_class:`），而前端永远会发送三个 0 —— 含 0 的字典是
    truthy，那个分支永不成立。结果是界面上「整体浮动」填了 5%，金额一点不变，
    也不报错。界面标题写的是"叠加在宁波信息价之上"，所以正确语义是**相加**。

    下限夹到 -0.9999：两个字段各自可以取到 -0.5，相加就是 -1.0（系数 0），
    再叠加历史脏数据就可能变成**负的**人工费 —— 宁可夹住，也不能算出一个
    负成本。
    """
    total = Decimal(str(overall or 0)) + Decimal(str(per_class or 0))
    floor = Decimal("-0.9999")
    return total if total > floor else floor


@dataclass
class FeeBreakdown:
    """All computed fees, ready to write back to the Quote row."""

    equipment_total: Decimal = Decimal("0")
    install_total: Decimal = Decimal("0")
    auxiliary_total: Decimal = Decimal("0")
    design_fee: Decimal = Decimal("0")
    warranty_fee: Decimal = Decimal("0")
    training_fee: Decimal = Decimal("0")
    subtotal_before_tax: Decimal = Decimal("0")
    mgmt_profit: Decimal = Decimal("0")
    tax_amount: Decimal = Decimal("0")
    total_with_tax: Decimal = Decimal("0")

    # 定额口径
    quota_total: Decimal = Decimal("0")
    quota_total_with_tax: Decimal = Decimal("0")

    # 成本与毛利
    cost_total: Decimal = Decimal("0")
    gross_profit: Decimal = Decimal("0")
    gross_profit_rate: Decimal = Decimal("0")

    # 成本口径的可识别性：有多少行没有进价、成本是**估算**出来的。
    # 不把这件事显式带出来，读报表的人就无法区分"真实毛利"与"编造毛利"。
    estimated_cost_rows: int = 0
    estimated_cost_amount: Decimal = Decimal("0")

    # 警告（用于回传给前端）
    warnings: list[str] = field(default_factory=list)


QUOTA_NOTE_MARKER = "quota:"


# 工艺系数（2022.7.13 智能化纪要）
SHIELDED_COEF = Decimal("1.20")  # 屏蔽线缆
CAT5E_COEF = Decimal("1.10")     # 超五类线
CEILING_COEF = Decimal("1.20")   # 吊顶内敷设


def parse_quota_code(notes: str | None) -> str | None:
    """从明细行备注里取出定额编号（形如 ``"屏蔽线 quota:5-1-12"``）。

    取标记后的**第一个空白分隔 token**，而不是整段剩余文本：
    备注后面通常还有说明（"quota:5-1-12 屏蔽"），把整段当编号的话
    在定额表里永远匹配不上，定额口径会静默退化成 0 或安装费。
    标记后为空（备注正好是 "quota:"）时返回 None，而不是抛 IndexError。

    两处调用方（quotes.py 的 recompute、scripts/seed.py 的演示报价）共用这一份，
    以前两边各写一遍 split，seed 那边就是漏了 ``.split()[0]``。
    """
    if not notes or QUOTA_NOTE_MARKER not in notes:
        return None
    rest = notes.split(QUOTA_NOTE_MARKER, 1)[1].split()
    return rest[0] if rest else None


def compute_auxiliary_base(
    base_type: AuxiliaryBaseType,
    *,
    equipment_total: Decimal,
    install_total: Decimal,
) -> Decimal:
    """Compute the auxiliary fee's BASE amount (before applying the rate)."""
    if base_type == AuxiliaryBaseType.DEVICE:
        return equipment_total
    if base_type == AuxiliaryBaseType.DEVICE_INSTALL:
        return equipment_total + install_total
    # BY_QUANTITY — caller handles this via per-item auxiliary columns
    # (not implemented here; falls back to device_total as a sane default)
    return equipment_total


def compute_fee_base(
    base_type: FeeBaseType,
    *,
    equipment_total: Decimal,
    install_total: Decimal,
    auxiliary_total: Decimal,
) -> Decimal:
    """Compute the BASE for design / warranty / etc.

    按本任务的口径定义（手动拍板）：
        - DEVICE_ONLY          = 设备费
        - DEVICE_AUX_INSTALL   = 设备 + 辅材 + 安装（仍含设备费，老口径）
        - DIRECT_FEE           = 人工 + 主材 + 机械  →  本实现里没有这三项
                                            的明细子项，按「人 + 材」降级为
                                            install_total + auxiliary_total。
                                            严格意义上是「不含设备费」：
                                            与 DEVICE_AUX_INSTALL 算出的设计费
                                            一般会差 设备费×design_rate。
    """
    if base_type == FeeBaseType.DEVICE_ONLY:
        return equipment_total
    if base_type == FeeBaseType.DEVICE_AUX_INSTALL:
        return equipment_total + auxiliary_total + install_total
    if base_type == FeeBaseType.DIRECT_FEE:
        # 「直接费」业务定义：人工 + 主材 + 机械，不含设备费。
        # 本实现里 install_total 已包含人工 + 机械两项，auxiliary_total
        # 是辅材，所以「人工费 + 辅材费」≈ install_total + auxiliary_total。
        # 如果未来引入独立的「主材费」/「机械费」字段，把这里换成那三项的和即可。
        return install_total + auxiliary_total
    raise ValueError(f"Unknown fee_base: {base_type}")


def compute_quote(
    *,
    items: Iterable[QuoteItem],
    auxiliary_base: AuxiliaryBaseType,
    auxiliary_rate: Decimal,
    fee_base: FeeBaseType,
    design_rate: Decimal,
    warranty_rate: Decimal,
    training_fee: Decimal,
    mgmt_profit_rate: Decimal,
    tax_rate: Decimal,
    labor_float_rate: Decimal | None = None,
    # 工艺系数（2026-10-05 改为**报价级参数** ✓）
    #   以前屏蔽/超五类/吊顶三个系数存在**每个定额子目**上 ✗ ——
    #   但它们其实是"这次施工条件"的属性（只有线缆用得上），该在报价时调 ✓。
    #   现在两条计算路径（设备行 / 定额行）统一用这三个入参 ✓。
    # 定额口径总开关（2026-10-05）：定额模式（安装费=基价）下必须关掉 ✗
    # —— 否则同一笔安装钱算两遍 ✓（实测：安装费 552.30 + 定额人工费 552.30 ✗）
    quota_enabled: bool = True,
    shielded_coef: Decimal = Decimal("1.20"),   # 屏蔽线缆
    cat5e_coef: Decimal = Decimal("1.10"),      # 超五类线
    ceiling_coef: Decimal = Decimal("1.20"),    # 吊顶内敷设
    labor_float_per_class: dict[str, Decimal] | None = None,
    labor_rates: dict[LaborClass, LaborRate] | None = None,
    quota_items: dict[str, QuotaItem] | None = None,
    quote_items_quota_map: dict[str, str] | None = None,
) -> FeeBreakdown:
    """Compute the full quote breakdown.

    Args:
        items: QuoteItems to sum up.
        auxiliary_base / auxiliary_rate: 辅材费基数 + 比例。
        fee_base / design_rate / warranty_rate: 设计/维保基础比例。
        mgmt_profit_rate: 管理费及利润比例。
        tax_rate: 销项税。
        labor_float_rate: 人工**整体**浮动（三类都加），与下面那个**相加**。
        labor_float_per_class: 分人工种浮动 {"class_a": ..., "class_b": ..., "class_c": ...}.
            有效上浮率 = labor_float_rate + labor_float_per_class[该类]，
            由 `effective_labor_float()` 统一算（含下限夹取）。
        labor_rates: 当前人工市场信息价 (定额口径用).
        quota_items: 定额子目库 (定额口径用, code -> QuotaItem).
        quote_items_quota_map: {quote_item.id -> quota_code} 把报价项映射到定额子目.

    Returns:
        FeeBreakdown — round all monetary values to 2dp.

    Note: QuoteItem.cost_price should already be populated when items came from library.
    If not, cost estimation defaults to 0.
    """
    items = list(items)
    labor_float_per_class = labor_float_per_class or {}
    labor_rates = labor_rates or {}
    quota_items = quota_items or {}
    quote_items_quota_map = quote_items_quota_map or {}

    breakdown = FeeBreakdown()

    # ----- 主口径（点位单价）-----
    estimated_rows = 0
    estimated_amount = Decimal("0")
    for it in items:
        # 甲供（is_owner_supplied）：设备由甲方供货，**不计入我方的设备费**，
        # 但安装费照计（安装是我方做的）。
        #
        # 为什么必须在这里统一：这三种口径以前互相矛盾，而且是会出事故的那种矛盾 ——
        #   · UI（QuoteItemDialog.vue）提示"甲供设备不计入设备费，仅计入安装费"
        #   · 本函数无条件把 equipment 计入 → 报价单的存储合计里含甲供
        #   · export_data 又把甲供行的单价/合计**留空**，而费用表的"设备费/含税合计"
        #     取的是含甲供的存储值 → 客户版报价单的明细求和不等于它自己的合计
        # 现在统一为"设备费不算、安装费照算"，与 UI 提示一致，也与导出留空的写法一致。
        owner_supplied = bool(getattr(it, "is_owner_supplied", False))
        if owner_supplied:
            equip = Decimal("0")
        else:
            equip = _q(it.quantity * it.unit_price)

        # 安装费 = 数量 × 安装单价 × 工艺系数
        # 工艺系数走结构化 boolean 字段（is_shielded / is_cat5e / is_ceiling），
        # 全 False 时 mult=1.0（向后兼容）。
        # 以前靠 notes 文本里 "shielded" 关键字匹配 → 多写一个字就静默失效。
        mult = Decimal("1.0")
        if it.is_shielded:
            mult *= shielded_coef
        if it.is_cat5e:
            mult *= cat5e_coef
        if it.is_ceiling:
            mult *= ceiling_coef
        install = _q(it.quantity * it.install_unit_price * mult)

        it.equipment_amount = equip
        it.install_amount = install

        breakdown.equipment_total += equip
        breakdown.install_total += install

        # 成本口径：数量 × 采购成本价。
        # 甲供设备不是我方采购的 → 不进我方成本（否则会算出一个不存在的毛利）。
        if owner_supplied:
            continue
        if it.cost_price is not None and it.cost_price > 0:
            breakdown.cost_total += _q(it.quantity * it.cost_price)
        else:
            # 没有进价 → 按设备价 75% 估算（行业经验值）。
            # 关键是**可识别**：估算出来的成本必须能被报表和导出件标出来，
            # 否则"毛利率"里混着编造的数字而没人看得见（详见下面的 warning）。
            estimate = _q(it.quantity * it.unit_price * Decimal("0.75"))
            breakdown.cost_total += estimate
            estimated_rows += 1
            estimated_amount += estimate

    breakdown.equipment_total = _q(breakdown.equipment_total)
    breakdown.install_total = _q(breakdown.install_total)
    breakdown.cost_total = _q(breakdown.cost_total)
    breakdown.estimated_cost_rows = estimated_rows
    breakdown.estimated_cost_amount = _q(estimated_amount)
    if estimated_rows:
        breakdown.warnings.append(
            f"{estimated_rows} 行没有进价，成本按设备价 ×75% 估算"
            f"（估算金额 {breakdown.estimated_cost_amount}）；"
            "这些行的毛利不是实际毛利，补齐进价后重算才算准"
        )

    # ----- 辅材费 -----
    if auxiliary_base == AuxiliaryBaseType.BY_QUANTITY:
        # 按工程量（2026-10-04 实现）：辅材费 = Σ(数量 × 单位辅材费)。
        # 单位金额已经是绝对值，所以**费率不参与**；但要明说，不能静默丢掉。
        aux_sum = Decimal("0")
        rows_with_aux = 0
        for it in items:
            unit_aux = Decimal(str(getattr(it, "auxiliary_unit_price", 0) or 0))
            if unit_aux == 0:
                continue
            aux_sum += Decimal(str(it.quantity or 0)) * unit_aux
            rows_with_aux += 1
        breakdown.auxiliary_total = _q(aux_sum)
        if rows_with_aux == 0:
            breakdown.warnings.append(
                "辅材口径是「按工程量」，但没有任何明细行填了单位辅材费 —— "
                "辅材费按 0 计。请在明细里填「单位辅材费」（桥架按米、网线按箱）"
            )
        if Decimal(str(auxiliary_rate or 0)) != 0:
            breakdown.warnings.append(
                f"辅材口径为「按工程量」时费率不参与计算（当前费率 {auxiliary_rate} 已忽略）；"
                "单位辅材费本身就是绝对金额"
            )
    else:
        aux_base_amount = compute_auxiliary_base(
            auxiliary_base,
            equipment_total=breakdown.equipment_total,
            install_total=breakdown.install_total,
        )
        breakdown.auxiliary_total = _q(aux_base_amount * auxiliary_rate)

    # ----- 设计/维保/培训 -----
    fee_base_amount = compute_fee_base(
        fee_base,
        equipment_total=breakdown.equipment_total,
        install_total=breakdown.install_total,
        auxiliary_total=breakdown.auxiliary_total,
    )

    if fee_base == FeeBaseType.DIRECT_FEE:
        # 新口径：直接费 = install_total + auxiliary_total（人 + 材，不含设备费）。
        # 必须告诉前端「和老的『设备费 + 辅材费 + 安装费』口径」之间差了多少。
        legacy_base = (
            breakdown.equipment_total + breakdown.auxiliary_total + breakdown.install_total
        )
        if legacy_base > 0:
            diff_pct = abs(fee_base_amount - legacy_base) / legacy_base
        else:
            diff_pct = Decimal("0")
        # 口径已变 → 永远是降级 + 人+材口径提示（向后兼容，必须用 info 级别，
        # 不能 422，否则旧数据调一次 compute 就被挡）
        breakdown.warnings.append(
            "设计费基数「直接费」按人+材口径计算"
            f"（install_total + auxiliary_total = {fee_base_amount}），"
            "不含设备费；如需按造价老口径『设备+辅材+安装』请改用 device_aux_install"
        )
        # 与老口径相差 > 0.1% → 再补一条审计警告
        if diff_pct > Decimal("0.001"):
            legacy_design = _q(legacy_base * design_rate)
            new_design = _q(fee_base_amount * design_rate)
            breakdown.warnings.append(
                f"直接费基数已切换为「人+材」，设计费从 {legacy_design} 变为 {new_design}"
                f"（差异 {diff_pct * 100:.2f}%），请确认是否为预期口径切换"
            )

    breakdown.design_fee = _q(fee_base_amount * design_rate)
    breakdown.warranty_fee = _q(breakdown.equipment_total * warranty_rate)
    breakdown.training_fee = _q(training_fee)

    # ----- 小计 + 管理费 + 税 -----
    breakdown.subtotal_before_tax = _q(
        breakdown.equipment_total
        + breakdown.install_total
        + breakdown.auxiliary_total
        + breakdown.design_fee
        + breakdown.warranty_fee
        + breakdown.training_fee
    )
    breakdown.mgmt_profit = _q(breakdown.subtotal_before_tax * mgmt_profit_rate)
    breakdown.tax_amount = _q(
        (breakdown.subtotal_before_tax + breakdown.mgmt_profit) * tax_rate
    )
    breakdown.total_with_tax = _q(
        breakdown.subtotal_before_tax + breakdown.mgmt_profit + breakdown.tax_amount
    )

    # ----- 毛利 -----
    # 口径（报价系统设计方案.md §8.1 规则 1）：
    #   "涉及税时，**先用不含税金额算毛利**，税是价外流转的，不是成本也不是收入"
    #   → 毛利 = 不含税收入(subtotal_before_tax) − 成本
    #
    # 以前分子用的是**含税合计**：v2.1 只把分母换成了不含税收入，分子没动，
    # 于是"把税当成利润"这件事原样保留，毛利率系统性虚高 (1+管理费率)×税率
    # —— demo 报价单报 54.29%，真实税前毛利只有 44.57%，差 9.7 个百分点。
    # 分子分母口径必须一致，否则这个比率没有任何财务含义。
    breakdown.gross_profit = _q(breakdown.subtotal_before_tax - breakdown.cost_total)
    if breakdown.subtotal_before_tax > 0:
        breakdown.gross_profit_rate = _clamp_rate(
            _r(breakdown.gross_profit / breakdown.subtotal_before_tax)
        )

    # ----- 定额口径 -----
    # 必须三类工日的当期价都齐了才算得动：_compute_quota_total 里只要有一类
    # 缺价就会 `continue` 跳过整条明细，结果是 quota_total 静默变成 0，
    # 界面上却显示成一个「看着像样」的数字。宁可不给数，也要给一句能看懂的话。
    _need = (LaborClass.A, LaborClass.B, LaborClass.C)
    _missing = [c.value for c in _need if c not in labor_rates]
    if quota_items and not _missing:
        breakdown.quota_total = _q(
            _compute_quota_total(
                items,
                quota_items=({} if not quota_enabled else quota_items),
                labor_rates=labor_rates,
                labor_float_rate=labor_float_rate,
                labor_float_per_class=labor_float_per_class,
                quote_items_quota_map=quote_items_quota_map,

                shielded_coef=shielded_coef,
                cat5e_coef=cat5e_coef,
                ceiling_coef=ceiling_coef,
            )
        )
        # 定额口径下的设计/管理费/税沿用点位单价口径比例
        quota_subtotal = breakdown.quota_total + breakdown.auxiliary_total
        quota_mgmt = _q(quota_subtotal * mgmt_profit_rate)
        quota_tax = _q((quota_subtotal + quota_mgmt) * tax_rate)
        breakdown.quota_total_with_tax = _q(quota_subtotal + quota_mgmt + quota_tax)

        # ----- 未回填保护（2026-10-06）-----
        # 真值定额库（2018 版第五册 843 条）导入时**只有编号与名称**，人工消耗量
        # 还是 0。这类子目会被 _compute_quota_total 安静地按 0 累加，界面上却显示
        # 成一个"看着像算出来了"的定额合计 —— 比"没有数"更危险，因为没人会去查。
        # 宁可退化成安装费口径并把原因写在 warnings 里，也不给一个无意义的 0。
        referenced, blank = _quota_labor_gaps(
            items, quota_items=quota_items, quote_items_quota_map=quote_items_quota_map
        )
        if referenced and len(blank) == len(referenced):
            if quota_enabled:
                breakdown.quota_total = breakdown.install_total
                breakdown.quota_total_with_tax = breakdown.total_with_tax
                breakdown.warnings.append(
                    "所引用的定额子目（"
                    + _code_list(blank)
                    + "）人工消耗量均未回填，定额口径已临时按安装费口径显示；"
                    "请在「人工费与定额」页回填工日后重算"
                )
            else:
                # 定额模式（2026-10-05）：安装费 = 定额基价 × 数量 ✓
                # 这里若再把安装费**镜像**成定额人工费 ✗ 会让人以为同一笔钱算了两遍 ✓
                breakdown.quota_total = Decimal("0")
                breakdown.quota_total_with_tax = Decimal("0")
                breakdown.warnings.append(
                    "本单为「套定额」模式：安装费 = 定额基价 × 数量 ✓，"
                    "定额口径不再单列（人工费已含在基价内 ✓）"
                )
        elif blank:
            breakdown.warnings.append(
                "定额子目（"
                + _code_list(blank)
                + "）的人工消耗量尚未回填，本次按 0 计入定额口径，合计偏小；"
                "请回填后重算"
            )
    else:
        if quota_enabled:
            # 无定额数据时，定额口径 = 主口径（避免回写 null）
            breakdown.quota_total = breakdown.install_total
            breakdown.quota_total_with_tax = breakdown.total_with_tax
        else:
            # 定额模式（2026-10-05）：安装费 = 定额基价 × 数量 ✓ ——
            # 这里**不再**把安装费镜像成定额人工费 ✗（实测就是这一条在生效 ✓）
            breakdown.quota_total = Decimal("0")
            breakdown.quota_total_with_tax = Decimal("0")
        # 先报「根因」再报「次因」：连定额库都没有的时候去提示「补齐人工单价」，
        # 会把用户引到完全错误的操作路径上（补完单价照样算不出来）。
        if not quota_items:
            breakdown.warnings.append(
                "定额库未加载，定额口径 = 安装费主口径，请上传定额表后再核对"
            )
        elif _missing:
            breakdown.warnings.append(
                "当前人工信息价缺少 "
                + "、".join(_missing)
                + "，定额口径无法计算，已临时按安装费口径显示；"
                "请在「人工费与定额」页补齐该期各类工日的单价后再重算"
            )

    return breakdown


def _code_list(codes: list[str]) -> str:
    """把编号列表写成一句人话，最多列 5 个（列表太长会撑爆界面提示）。"""
    shown = "、".join(codes[:5])
    if len(codes) > 5:
        return f"{shown} 等 {len(codes)} 条"
    return shown


def _quota_labor_gaps(
    items: Iterable[QuoteItem],
    *,
    quota_items: dict[str, QuotaItem],
    quote_items_quota_map: dict[str, str],
) -> tuple[list[str], list[str]]:
    """找出「引用了定额子目、但人工消耗量还没回填」的编号。

    返回 ``(referenced, blank)``：
      - ``referenced``：明细行引用、且能在定额库里查到的编号（去重，保持出现顺序）
      - ``blank``：其中一类/二类/三类工日**全部为 0** 的编号

    只统计"引用到了也查到了"的行：查不到的编号属于另一类问题（编号写错 /
    未导入），由 ``_compute_quota_total`` 静默跳过，不在本函数职责内。
    """
    referenced: list[str] = []
    blank: list[str] = []
    for it in items:
        code = quote_items_quota_map.get(str(it.id))
        if not code or code in referenced:
            continue
        qi = quota_items.get(code)
        if qi is None:
            continue
        referenced.append(code)
        days = (
            Decimal(str(qi.labor_a_day or 0))
            + Decimal(str(qi.labor_b_day or 0))
            + Decimal(str(qi.labor_c_day or 0))
        )
        if days == 0:
            blank.append(code)
    return referenced, blank


def _compute_quota_total(
    items: Iterable[QuoteItem],
    *,
    quota_items: dict[str, QuotaItem],
    labor_rates: dict[LaborClass, LaborRate],
    labor_float_per_class: dict[str, Decimal],
    quote_items_quota_map: dict[str, str],
    labor_float_rate: Decimal | None = None,
    shielded_coef: Decimal = Decimal("1.20"),
    cat5e_coef: Decimal = Decimal("1.10"),
    ceiling_coef: Decimal = Decimal("1.20"),
) -> Decimal:
    """Compute the定额口径人工费合计.

    For each item, look up its quota子目 by ID → apply:
        sum = (labor_a × rate_a + labor_b × rate_b + labor_c × rate_c) × quantity
               × (1 + 整体浮动 + 该类浮动)
               × 工艺系数
    """
    total = Decimal("0")
    for it in items:
        quota_code = quote_items_quota_map.get(str(it.id))
        if not quota_code:
            continue
        qi = quota_items.get(quota_code)
        if not qi:
            continue

        # 获取当前人工市场信息价
        rate_a = labor_rates.get(LaborClass.A)
        rate_b = labor_rates.get(LaborClass.B)
        rate_c = labor_rates.get(LaborClass.C)
        if not (rate_a and rate_b and rate_c):
            continue

        # 应用浮动：整体浮动 + 该类浮动（两者**相加**，见 effective_labor_float）
        f_a = effective_labor_float(labor_float_rate, labor_float_per_class.get("class_a"))
        f_b = effective_labor_float(labor_float_rate, labor_float_per_class.get("class_b"))
        f_c = effective_labor_float(labor_float_rate, labor_float_per_class.get("class_c"))

        # 工艺系数：按结构化 boolean 字段（is_shielded / is_cat5e / is_ceiling）
        # 走定额子目表里的对应系数（qi.shielded_coef / cat5e_coef / ceiling_coef）。
        # 顺序与点位单价口径保持一致：屏蔽 → 超五类 → 吊顶。
        # 以前是 ``if "shielded" in it.notes.lower()``，改成结构化字段后不再
        # 依赖备注文本，新增的超五类 / 吊顶两种场景也能直接乘上去。
        coef = Decimal("1.0")
        # 与设备行同一套系数（都来自报价级设置 ✓）：
        # 以前取**子目上的** qi.shielded_coef ✗ —— 同一个子目在吊顶/非吊顶无法区分 ✓
        if it.is_shielded:
            coef *= shielded_coef
        if it.is_cat5e:
            coef *= cat5e_coef
        if it.is_ceiling:
            coef *= ceiling_coef

        item_labor = Decimal("0")
        item_labor += _class_labor(qi.labor_a_day, rate_a.price_per_day, f_a)
        item_labor += _class_labor(qi.labor_b_day, rate_b.price_per_day, f_b)
        item_labor += _class_labor(qi.labor_c_day, rate_c.price_per_day, f_c)
        item_labor *= coef

        total += item_labor * Decimal(str(it.quantity))

    return total


def write_breakdown_to_quote(quote: Quote, b: FeeBreakdown) -> None:
    """Apply a FeeBreakdown back onto a Quote ORM instance."""
    quote.equipment_total = b.equipment_total
    quote.install_total = b.install_total
    quote.auxiliary_total = b.auxiliary_total
    quote.design_fee = b.design_fee
    quote.warranty_fee = b.warranty_fee
    quote.subtotal_before_tax = b.subtotal_before_tax
    quote.mgmt_profit = b.mgmt_profit
    quote.tax_amount = b.tax_amount
    quote.total_with_tax = b.total_with_tax
    quote.quota_total = b.quota_total
    quote.quota_total_with_tax = b.quota_total_with_tax
    quote.cost_total = b.cost_total
    quote.gross_profit = b.gross_profit
    quote.gross_profit_rate = b.gross_profit_rate


__all__ = [
    "FeeBreakdown",
    "compute_quote",
    "compute_auxiliary_base",
    "compute_fee_base",
    "write_breakdown_to_quote",
]
