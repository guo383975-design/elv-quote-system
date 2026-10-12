"""报价编制业务逻辑测试 — 双行并列口径的费用瀑布逐项验算.

These are pure unit tests: no DB, no HTTP. Numbers are hand-computed in the
comments so a failure tells you exactly which step broke.
"""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.labor import LaborClass, LaborRate, QuotaCategory, QuotaItem
from app.models.quote import AuxiliaryBaseType, FeeBaseType, Quote
from app.models.quote_item import QuoteItem
from app.services.quote_compute import (
    compute_auxiliary_base,
    compute_fee_base,
    compute_quote,
    parse_quota_code,
    write_breakdown_to_quote,
)


def make_item(
    *,
    name: str = "摄像机",
    qty: str = "1",
    unit_price: str = "100.00",
    install: str = "10.00",
    cost: str | None = None,
    notes: str | None = None,
    is_shielded: bool = False,
    is_cat5e: bool = False,
    is_ceiling: bool = False,
) -> QuoteItem:
    it = QuoteItem(
        name=name,
        unit="台",
        quantity=Decimal(qty),
        unit_price=Decimal(unit_price),
        install_unit_price=Decimal(install),
        cost_price=Decimal(cost) if cost is not None else None,
        notes=notes,
        is_shielded=is_shielded,
        is_cat5e=is_cat5e,
        is_ceiling=is_ceiling,
    )
    return it


def base_kwargs(**overrides) -> dict:
    kw = {
        "auxiliary_base": AuxiliaryBaseType.DEVICE,
        "auxiliary_rate": Decimal("0.06"),
        "fee_base": FeeBaseType.DEVICE_AUX_INSTALL,
        "design_rate": Decimal("0.02"),
        "warranty_rate": Decimal("0.03"),
        "training_fee": Decimal("0"),
        "mgmt_profit_rate": Decimal("0.08"),
        "tax_rate": Decimal("0.09"),
    }
    kw.update(overrides)
    return kw


# ---------------------------------------------------------------------------
# 1. 单行最小用例 — 每一步都能手算
# ---------------------------------------------------------------------------

def test_single_item_full_waterfall():
    """1 台设备，单价 100，安装 10，成本 60。

    设备费      = 1 × 100            = 100.00
    安装费      = 1 × 10             =  10.00
    辅材费      = 设备费 × 6%        =   6.00
    fee_base    = 100 + 6 + 10       = 116.00
    设计费      = 116 × 2%           =   2.32
    维保费      = 设备费 × 3%        =   3.00
    培训费                           =   0.00
    税前小计    = 100+10+6+2.32+3    = 121.32
    管理费及利润 = 121.32 × 8%       =   9.7056 → 9.71
    税          = (121.32+9.71) × 9% =  11.7927 → 11.79
    含税合计    = 121.32+9.71+11.79  = 142.82
    成本        = 1 × 60             =  60.00
    毛利        = 121.32 - 60        =  61.32   ← 不含税收入 − 成本（税不是收入）
    毛利率      = 61.32 / 121.32     =   0.5054

    注意毛利曾经写成「含税合计 − 成本 = 82.82」—— 那等于把 9% 销项税当成利润，
    与 设计文档 §8.1 规则 1「先用不含税金额算毛利」冲突（2026-09-30 修正）。
    """
    b = compute_quote(items=[make_item(cost="60.00")], **base_kwargs())

    assert b.equipment_total == Decimal("100.00")
    assert b.install_total == Decimal("10.00")
    assert b.auxiliary_total == Decimal("6.00")
    assert b.design_fee == Decimal("2.32")
    assert b.warranty_fee == Decimal("3.00")
    assert b.training_fee == Decimal("0.00")
    assert b.subtotal_before_tax == Decimal("121.32")
    assert b.mgmt_profit == Decimal("9.71")
    assert b.tax_amount == Decimal("11.79")
    assert b.total_with_tax == Decimal("142.82")

    assert b.cost_total == Decimal("60.00")
    assert b.gross_profit == Decimal("61.32")
    assert b.gross_profit_rate == Decimal("0.5054")
    # 分子必须是不含税收入，不是含税合计（后者会多出 21.50 = 税额 + 管理费）
    assert b.gross_profit != b.total_with_tax - b.cost_total


# ---------------------------------------------------------------------------
# 2. 多行累加 + 数量小数
# ---------------------------------------------------------------------------

def test_multi_item_totals():
    """86 × 680 与 42 × 720。

    设备费 = 58,480.00 + 30,240.00 = 88,720.00
    安装费 = 86×112.60 + 42×112.60 = 9,683.60 + 4,729.20 = 14,412.80
    """
    items = [
        make_item(name="枪机", qty="86", unit_price="680.00", install="112.60", cost="510.00"),
        make_item(name="半球", qty="42", unit_price="720.00", install="112.60", cost="540.00"),
    ]
    b = compute_quote(items=items, **base_kwargs())

    assert b.equipment_total == Decimal("88720.00")
    assert b.install_total == Decimal("14412.80")

    # 每行金额已回写
    assert items[0].equipment_amount == Decimal("58480.00")
    assert items[0].install_amount == Decimal("9683.60")
    assert items[1].equipment_amount == Decimal("30240.00")
    assert items[1].install_amount == Decimal("4729.20")


def test_fractional_quantity_rounds_half_up():
    """0.5 箱 × 780.00 = 390.00；0.333 × 100 = 33.30 (33.30 exactly)。"""
    b = compute_quote(
        items=[make_item(qty="0.5", unit_price="780.00", install="0", cost="585.00")],
        **base_kwargs(),
    )
    assert b.equipment_total == Decimal("390.00")
    assert b.cost_total == Decimal("292.50")

    b2 = compute_quote(
        items=[make_item(qty="0.333", unit_price="100.00", install="0", cost="75.00")],
        **base_kwargs(),
    )
    # 0.333 × 100 = 33.3 → 33.30 ; 0.333 × 75 = 24.975 → 24.98 (HALF_UP)
    assert b2.equipment_total == Decimal("33.30")
    assert b2.cost_total == Decimal("24.98")


# ---------------------------------------------------------------------------
# 3. 辅材费基数三选一
# ---------------------------------------------------------------------------

def test_auxiliary_base_device_only():
    assert compute_auxiliary_base(
        AuxiliaryBaseType.DEVICE,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
    ) == Decimal("1000.00")


def test_auxiliary_base_device_plus_install():
    assert compute_auxiliary_base(
        AuxiliaryBaseType.DEVICE_INSTALL,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
    ) == Decimal("1200.00")


def test_auxiliary_base_by_quantity_falls_back_to_device():
    """by_quantity 需要逐项工程量，service 层暂以设备费兜底并保持可预测。"""
    assert compute_auxiliary_base(
        AuxiliaryBaseType.BY_QUANTITY,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
    ) == Decimal("1000.00")


def test_auxiliary_rate_changes_total():
    """辅材 6% vs 10%：差额 = 1000 × 4% = 40.00，且逐级放大到含税合计。"""
    one = compute_quote(
        items=[make_item(unit_price="1000.00", install="0", cost="700.00")],
        **base_kwargs(auxiliary_rate=Decimal("0.06")),
    )
    two = compute_quote(
        items=[make_item(unit_price="1000.00", install="0", cost="700.00")],
        **base_kwargs(auxiliary_rate=Decimal("0.10")),
    )
    assert one.auxiliary_total == Decimal("60.00")
    assert two.auxiliary_total == Decimal("100.00")
    assert two.total_with_tax > one.total_with_tax


# ---------------------------------------------------------------------------
# 4. 附加费率基数三选一
# ---------------------------------------------------------------------------

def test_fee_base_device_only():
    assert compute_fee_base(
        FeeBaseType.DEVICE_ONLY,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
        auxiliary_total=Decimal("60.00"),
    ) == Decimal("1000.00")


def test_fee_base_device_aux_install():
    """1000 + 60 + 200 = 1260。"""
    assert compute_fee_base(
        FeeBaseType.DEVICE_AUX_INSTALL,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
        auxiliary_total=Decimal("60.00"),
    ) == Decimal("1260.00")


def test_fee_base_direct_fee():
    """直接费按人+材口径 = install_total + auxiliary_total，不含设备费。

    业务拍板：本实现没有独立的人工/主材/机械三个字段，按人+材降级
    （等价于把设备费剥离后的「直接费」语义近似）。
    """
    assert compute_fee_base(
        FeeBaseType.DIRECT_FEE,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
        auxiliary_total=Decimal("60.00"),
    ) == Decimal("260.00")


def test_fee_base_direct_fee_excludes_equipment():
    """DIRECT_FEE 必须不含设备费 —— 与 DEVICE_AUX_INSTALL 必须不同。"""
    direct = compute_fee_base(
        FeeBaseType.DIRECT_FEE,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
        auxiliary_total=Decimal("60.00"),
    )
    legacy = compute_fee_base(
        FeeBaseType.DEVICE_AUX_INSTALL,
        equipment_total=Decimal("1000.00"),
        install_total=Decimal("200.00"),
        auxiliary_total=Decimal("60.00"),
    )
    assert direct == Decimal("260.00"), "直接费 = 200 + 60，不含 1000"
    assert legacy == Decimal("1260.00"), "老口径仍然含设备费（向后兼容）"
    assert legacy - direct == Decimal("1000.00"), "差额恰好是设备费"


def test_fee_base_changes_design_fee():
    """仅设备费 vs 设备+辅材+安装 → 设计费基数不同。

    device_only:      1000 × 2%     = 20.00
    device_aux_inst:  1260 × 2%     = 25.20
    """
    common = {"items": [make_item(unit_price="1000.00", install="200.00", cost="700.00")]}
    a = compute_quote(**common, **base_kwargs(fee_base=FeeBaseType.DEVICE_ONLY))
    b = compute_quote(**common, **base_kwargs(fee_base=FeeBaseType.DEVICE_AUX_INSTALL))

    assert a.design_fee == Decimal("20.00")
    assert b.design_fee == Decimal("25.20")


# ---------------------------------------------------------------------------
# 5. 维保费按设备费（不随 fee_base 变）
# ---------------------------------------------------------------------------

def test_warranty_always_on_equipment():
    a = compute_quote(
        items=[make_item(unit_price="1000.00", install="200.00", cost="700.00")],
        **base_kwargs(fee_base=FeeBaseType.DEVICE_ONLY),
    )
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="200.00", cost="700.00")],
        **base_kwargs(fee_base=FeeBaseType.DIRECT_FEE),
    )
    assert a.warranty_fee == b.warranty_fee == Decimal("30.00")


def test_gross_profit_rate_uses_subtotal_before_tax():
    """毛利率口径 = gross_profit / subtotal_before_tax（不含税收入）。

    特殊值 1：1 台 100/10/60（device_aux_install），
        subtotal_before_tax = 121.32, gross_profit = 61.32
        → 0.5054（已写在 test_single_item_full_waterfall 里）

    特殊值 2：1000/0/700 单台（device_aux_install），
        设备费=1000, 辅材=60, fee_base=1000+60+0=1060
        设计费=1060×2%=21.20, 维保=30, 培训=0
        subtotal_before_tax = 1000+0+60+21.20+30+0 = 1111.20
        管理费 = 1111.20 × 8% = 88.896 → 88.90
        税 = (1111.20+88.90)×9% = 108.009 → 108.01
        total_with_tax = 1111.20 + 88.90 + 108.01 = 1308.11
        成本 = 700
        毛利 = 1111.20 − 700 = 411.20
        gross_profit_rate = 411.20 / 1111.20 = 0.370051 → 0.3701
    """
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="0", cost="700.00")],
        **base_kwargs(),
    )
    # 验算
    assert b.subtotal_before_tax == Decimal("1111.20")
    assert b.total_with_tax == Decimal("1308.11")
    assert b.gross_profit == Decimal("411.20")
    assert b.gross_profit_rate == Decimal("0.3701"), (
        f"实际 {b.gross_profit_rate} —— 口径必须是 gross_profit / subtotal_before_tax"
    )


def test_gross_profit_excludes_tax():
    """反向断言：**分子**不能含税（这是 2026-09-30 修的那一处）。

    旧实现：gross_profit = total_with_tax − cost = 1308.11 − 700 = 608.11
            → 608.11 / 1111.20 = 0.5473（把 108.01 的税当成利润，虚高）
    新实现：gross_profit = subtotal_before_tax − cost = 1111.20 − 700 = 411.20
    设计依据：报价系统设计方案.md §8.1 规则 1「涉及税时先用不含税金额算毛利，
    税是价外流转的，不是成本也不是收入」。
    """
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="0", cost="700.00")],
        **base_kwargs(),
    )
    assert b.gross_profit == b.subtotal_before_tax - b.cost_total
    # 把税算进去就会得到下面这个数；一旦有人改回去，这条立刻报警
    assert b.gross_profit != Decimal("608.11"), (
        "毛利分子又带上税了 —— 必须用不含税收入(subtotal_before_tax)"
    )
    assert b.gross_profit_rate != Decimal("0.5473"), "毛利率虚高口径回归"


# ---------------------------------------------------------------------------
# 6. 甲供设备：设备费不计入我方报价，安装费照计
# ---------------------------------------------------------------------------

def test_owner_supplied_excludes_equipment_but_keeps_install():
    """甲供（is_owner_supplied）口径 —— 2026-09-30 统一。

    以前三处互相矛盾：UI 提示"不计入设备费、仅计安装费"、本 service 把设备费
    全额计入、导出又把该行金额留空但合计含它（客户版报价单的明细求和不等于合计）。
    现在的唯一口径：**设备费按 0 计，安装费照计**；我方也不承担它的采购成本。
    """
    supplied = make_item(
        name="交换机", qty="1", unit_price="1380.00", install="100.00", cost="1035.00"
    )
    supplied.is_owner_supplied = True

    b = compute_quote(items=[supplied], **base_kwargs())
    assert b.equipment_total == Decimal("0.00"), "甲供设备不计入我方设备费"
    assert b.install_total == Decimal("100.00"), "甲供设备的安装费是我方干的，照计"
    # 甲供设备不是我方采购 → 不进成本，否则会算出一个不存在的毛利
    assert b.cost_total == Decimal("0.00")
    # 行上的设备金额也要归零，这样子系统汇总/导出与整单口径一致
    assert supplied.equipment_amount == Decimal("0.00")
    assert supplied.install_amount == Decimal("100.00")
    assert b.total_with_tax > Decimal("0")


def test_owner_supplied_row_can_still_be_counted_when_not_flagged():
    """反向对照：没打甲供标记的行必须照常计入（口径来自字段，不是被悄悄丢掉）。"""
    normal = make_item(name="交换机", qty="1", unit_price="1380.00", install="0", cost="1035.00")
    b = compute_quote(items=[normal], **base_kwargs())
    assert b.equipment_total == Decimal("1380.00")
    assert b.cost_total == Decimal("1035.00")


# ---------------------------------------------------------------------------
# 7. 成本兜底：未提供成本价时按设备价 75% 估算
# ---------------------------------------------------------------------------

def test_cost_fallback_is_75_percent_and_marked_as_estimated():
    """没有进价 → 成本按设备价 ×75% 估算，但**必须可识别**。

    估算本身是有意的兜底（否则毛利会被算成 100%），危险的是"看不出来是估算的"：
    报表里的毛利率会混着编造的数字，据此做经营决策就是拿错数据。
    所以这里同时断言行数与金额被带出来、并且产生一条 warning。
    """
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="0", cost=None)],
        **base_kwargs(),
    )
    assert b.cost_total == Decimal("750.00")
    assert b.estimated_cost_rows == 1
    assert b.estimated_cost_amount == Decimal("750.00")
    assert any("估算" in w for w in b.warnings), "估算成本必须产生可见告警"


def test_cost_fallback_not_flagged_when_cost_is_known():
    """有进价的行不得被标成估算（否则告警就没人信了）。"""
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="0", cost="700.00")],
        **base_kwargs(),
    )
    assert b.cost_total == Decimal("700.00")
    assert b.estimated_cost_rows == 0
    assert not any("估算" in w for w in b.warnings)


# ---------------------------------------------------------------------------
# 8. 定额口径（双行并列的第二行）
# ---------------------------------------------------------------------------

def make_labor_rates() -> dict[LaborClass, LaborRate]:
    return {
        LaborClass.A: LaborRate(labor_class=LaborClass.A, effective_date="2025-09-01",
                                price_per_day=Decimal("150.00")),
        LaborClass.B: LaborRate(labor_class=LaborClass.B, effective_date="2025-09-01",
                                price_per_day=Decimal("163.00")),
        LaborClass.C: LaborRate(labor_class=LaborClass.C, effective_date="2025-09-01",
                                price_per_day=Decimal("186.00")),
    }


def test_quota_total_uses_labor_days_and_rates():
    """定额 5-6-001：A 0.35 工日 / B 1.40 工日 / C 0。

    单台人工费 = 0.35×150 + 1.40×163 = 52.50 + 228.20 = 280.70
    10 台       = 2807.00
    """
    item = make_item(name="摄像机", qty="10", unit_price="680.00", install="112.60",
                     cost="510.00", notes="quota:5-6-001")
    item.id = __import__("uuid").uuid4()

    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("112.60"),
        )
    }
    b = compute_quote(
        items=[item],
        **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(item.id): "5-6-001"},
    )
    assert b.quota_total == Decimal("2807.00")
    assert b.quota_total_with_tax > b.quota_total


def test_quota_absent_falls_back_to_install_total_with_warning():
    """没传定额库 → 定额口径退化为安装费口径，并产生一条 warning。"""
    b = compute_quote(
        items=[make_item(qty="2", unit_price="100.00", install="10.00", cost="60.00")],
        **base_kwargs(),
    )
    assert b.quota_total == b.install_total == Decimal("20.00")
    assert b.quota_total_with_tax == b.total_with_tax
    assert len(b.warnings) == 1
    assert "定额库未加载" in b.warnings[0]


def test_quota_all_referenced_blank_labor_falls_back_with_root_cause():
    """真值子目导入后、工日还没回填时：三类工日全为 0。

    这种情况**不能**把 0 当成"算出来的定额合计"显示 —— 界面上那个 0 看起来
    像是"这套工程定额就值这么点钱"。必须退化为安装费口径并说清原因。
    """
    item = make_item(name="探测器", qty="10", unit_price="680.00", install="112.60",
                     cost="510.00", notes="quota:5-6-1")
    item.id = uuid4()

    quota_items = {
        "5-6-1": QuotaItem(
            code="5-6-1", category=QuotaCategory.SECURITY,
            name="入侵探测器 门磁、窗磁开关有线", unit="待补",
            labor_a_day=Decimal("0"), labor_b_day=Decimal("0"),
            labor_c_day=Decimal("0"), base_price=Decimal("0"),
        )
    }
    b = compute_quote(
        items=[item],
        **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(item.id): "5-6-1"},
    )
    assert b.quota_total == b.install_total
    assert b.quota_total_with_tax == b.total_with_tax
    assert any("人工消耗量均未回填" in w and "5-6-1" in w for w in b.warnings), b.warnings


def test_quota_partially_blank_labor_warns_but_still_computes():
    """部分子目缺工日：已回填的照算，但必须点出少算了哪几条。"""
    filled = make_item(name="摄像机", qty="10", unit_price="680.00", install="112.60",
                       cost="510.00", notes="quota:5-6-001")
    filled.id = uuid4()
    blank = make_item(name="探测器", qty="4", unit_price="200.00", install="30.00",
                      cost="150.00", notes="quota:5-6-1")
    blank.id = uuid4()

    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("112.60"),
        ),
        "5-6-1": QuotaItem(
            code="5-6-1", category=QuotaCategory.SECURITY, name="入侵探测器", unit="待补",
            labor_a_day=Decimal("0"), labor_b_day=Decimal("0"),
            labor_c_day=Decimal("0"), base_price=Decimal("0"),
        ),
    }
    b = compute_quote(
        items=[filled, blank],
        **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(filled.id): "5-6-001", str(blank.id): "5-6-1"},
    )
    # 与 test_quota_total_uses_labor_days_and_rates 同一笔：0.35×150 + 1.40×163 = 280.70 → ×10
    assert b.quota_total == Decimal("2807.00"), "已回填的那条必须照算，不能被降级"
    assert any("尚未回填" in w and "5-6-1" in w for w in b.warnings), b.warnings


def test_labor_float_increases_quota_total():
    """二类人工上浮 10% → 280.70 中的 228.20 → 251.02，单台 303.52，10 台 3035.20。

    0.35×150 × 1.0 = 52.50
    1.40×163 × 1.1 = 251.02
    合计           = 303.52
    ×10            = 3035.20
    """
    item = make_item(qty="10", notes="quota:5-6-001")
    item.id = __import__("uuid").uuid4()
    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("0"),
        )
    }
    b = compute_quote(
        items=[item],
        **base_kwargs(),
        labor_float_per_class={"class_b": Decimal("0.10")},
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(item.id): "5-6-001"},
    )
    assert b.quota_total == Decimal("3035.20")


def test_labor_overall_float_alone_increases_quota_total():
    """回归：**整体浮动**必须自己就能生效（2026-10-03 修掉的静默失效）。

    修之前 `labor_float_rate` 只在 `labor_float_per_class` 为空时才被使用，
    而前端永远发送三个 0（含 0 的字典是 truthy）→ 界面上填「整体浮动」
    金额一点不变。这里只传整体浮动，必须看到 280.70 → 308.77。
    """
    item = make_item(qty="10", notes="quota:5-6-001")
    item.id = __import__("uuid").uuid4()
    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("0"),
        )
    }
    b = compute_quote(
        items=[item],
        **base_kwargs(),
        labor_float_rate=Decimal("0.10"),
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(item.id): "5-6-001"},
    )
    # (0.35×150 + 1.40×163) × 1.1 = 280.70 × 1.1 = 308.77 → ×10 台 = 3087.70
    assert b.quota_total == Decimal("3087.70")


def test_labor_overall_and_per_class_floats_are_additive():
    """整体 + 分类 = 相加（界面标题写的就是"叠加在宁波信息价之上"）。

    前端实际发送的形态：per_class 三个 0 + 整体 10% —— 必须与"只传整体 10%"
    得到同一个数（这正是修之前失效的那条路径）。
    """
    item = make_item(qty="10", notes="quota:5-6-001")
    item.id = __import__("uuid").uuid4()
    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("0"),
        )
    }
    common = dict(
        items=[item], **base_kwargs(), labor_rates=make_labor_rates(),
        quota_items=quota_items, quote_items_quota_map={str(item.id): "5-6-001"},
    )
    only_overall = compute_quote(labor_float_rate=Decimal("0.10"), **common)
    ui_shape = compute_quote(
        labor_float_rate=Decimal("0.10"),
        labor_float_per_class={"class_a": Decimal("0"), "class_b": Decimal("0"),
                               "class_c": Decimal("0")},
        **common,
    )
    assert ui_shape.quota_total == only_overall.quota_total == Decimal("3087.70")

    # 整体 10% 会加到**每一类**上，再叠加该类的 10%：
    #   A: 52.50 × (1 + 0.10)      = 57.75
    #   B: 228.20 × (1 + 0.10+0.10) = 273.84
    #   单台 331.59 → ×10 = 3315.90
    both = compute_quote(
        labor_float_rate=Decimal("0.10"),
        labor_float_per_class={"class_b": Decimal("0.10")},
        **common,
    )
    assert both.quota_total == Decimal("3315.90")


def test_labor_float_never_produces_negative_labor():
    """两个字段各自可以取到 -50%，相加 -100% 会让系数变成 0 —— 但要防住更低的
    脏数据（历史行没有约束时可能存成 -1.5），人工费不能是负数。"""
    item = make_item(qty="10", notes="quota:5-6-001")
    item.id = __import__("uuid").uuid4()
    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("0"),
        )
    }
    b = compute_quote(
        items=[item],
        **base_kwargs(),
        labor_float_rate=Decimal("-5"),          # 故意越界（绕过 schema 的历史数据）
        labor_float_per_class={"class_b": Decimal("-5")},
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(item.id): "5-6-001"},
    )
    assert b.quota_total >= Decimal("0"), f"人工费不该是负数：{b.quota_total}"
    # 夹到 -0.9999 → 系数 0.0001 → 2807.00 × 0.0001 取整后为 0.28
    assert b.quota_total == Decimal("0.28")


def test_quota_shielded_coefficient_applied_from_notes():
    """回归测试：以前 notes 含 'shielded' → 乘 1.2。

    新版已改为结构化 boolean 字段（is_shielded），下面这条 notes 路径不再生效。
    保留这条断言是为了把「移除文本关键字匹配」这一改动钉在测试里：
    现在 should NOT 乘 1.2（= 仅按基线 1.0）。
    """
    item = make_item(qty="10", notes="quota:5-6-001 shielded")
    item.id = __import__("uuid").uuid4()
    quota_items = {
        "5-6-001": QuotaItem(
            code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
            labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.00"), base_price=Decimal("0"),
            shielded_coef=Decimal("1.20"),
        )
    }
    b = compute_quote(
        items=[item],
        **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items=quota_items,
        quote_items_quota_map={str(item.id): "5-6-001"},
    )
    # 新行为：备注里的关键字不再触发 → 走基线 1.0
    assert b.quota_total == Decimal("2807.00"), (
        f"notes 文本匹配已废弃；当前 quota_total={b.quota_total}；"
        "工艺系数必须走结构化 boolean 字段"
    )


# ---------------------------------------------------------------------------
# 8a. 工艺系数结构化字段（is_shielded / is_cat5e / is_ceiling）
# ---------------------------------------------------------------------------

def test_install_coefficient_shielded_only():
    """仅 is_shielded=True：install 单价 × 1.20。
    设备 100/台 × 10 台 = 1000.00
    安装 10/台 × 10 台 × 1.20 = 120.00
    """
    item = make_item(qty="10", unit_price="100.00", install="10.00",
                     cost="60.00", is_shielded=True)
    b = compute_quote(items=[item], **base_kwargs())
    assert item.install_amount == Decimal("120.00"), (
        f"is_shielded=True 时安装费应 ×1.20，实际 {item.install_amount}"
    )
    assert b.install_total == Decimal("120.00")


def test_install_coefficient_cat5e_only():
    """仅 is_cat5e=True：install 单价 × 1.10。"""
    item = make_item(qty="10", unit_price="100.00", install="10.00",
                     cost="60.00", is_cat5e=True)
    _ = compute_quote(items=[item], **base_kwargs())
    assert item.install_amount == Decimal("110.00"), (
        f"is_cat5e=True 时安装费应 ×1.10，实际 {item.install_amount}"
    )


def test_install_coefficient_ceiling_only():
    """仅 is_ceiling=True：install 单价 × 1.20。"""
    item = make_item(qty="10", unit_price="100.00", install="10.00",
                     cost="60.00", is_ceiling=True)
    _ = compute_quote(items=[item], **base_kwargs())
    assert item.install_amount == Decimal("120.00"), (
        f"is_ceiling=True 时安装费应 ×1.20，实际 {item.install_amount}"
    )


def test_install_coefficient_none_baseline():
    """全 False：× 1.0（baseline）。"""
    item = make_item(qty="10", unit_price="100.00", install="10.00", cost="60.00")
    _ = compute_quote(items=[item], **base_kwargs())
    assert item.install_amount == Decimal("100.00"), (
        f"全 False 时安装费必须 = 100；实际 {item.install_amount}"
    )


def test_install_coefficient_all_three_stacked():
    """三个字段都 True：× 1.20 × 1.10 × 1.20 = 1.584。
    10 × 10 × 1.584 = 158.40
    """
    item = make_item(qty="10", unit_price="100.00", install="10.00", cost="60.00",
                     is_shielded=True, is_cat5e=True, is_ceiling=True)
    _ = compute_quote(items=[item], **base_kwargs())
    assert item.install_amount == Decimal("158.40"), (
        f"全 True 时安装费 = 100 × 1.20 × 1.10 × 1.20 = 158.40；实际 {item.install_amount}"
    )


def test_install_coefficient_boundary_false_vs_true():
    """边界对比 —— False/False vs True/True。
    单价 100, 数量 1, 安装 10
        False/False → 10.00
        True/True   → 10 × 1.20 × 1.20 = 14.40（假设 shielded + ceiling）
    """
    plain = make_item(name="起源", qty="1", unit_price="100.00",
                      install="10.00", cost="60.00")
    boosted = make_item(name="系列", qty="1", unit_price="100.00",
                        install="10.00", cost="60.00",
                        is_shielded=True, is_ceiling=True)

    _ = compute_quote(items=[plain], **base_kwargs())
    _ = compute_quote(items=[boosted], **base_kwargs())

    assert plain.install_amount == Decimal("10.00")
    assert boosted.install_amount == Decimal("14.40")
    # 边界断言：True/True 必须比 False/False 大、且大多少可算出来
    assert boosted.install_amount - plain.install_amount == Decimal("4.40")


def test_quota_total_uses_structured_coefficients():
    """定额口径同样用结构化 boolean → cat5e_coef / ceiling_coef 真用上。

    单台人工费基线 = 0.35×150 + 1.40×163 = 280.70
    三类工日都齐的前提下：
        is_cat5e=True → × 1.10 → 308.77（×10 = 3087.70）
        is_ceiling=True → × 1.20 → 336.84（×10 = 3368.40）
    """
    qi = QuotaItem(
        code="5-6-001", category=QuotaCategory.SECURITY, name="安装摄像机", unit="台",
        labor_a_day=Decimal("0.35"), labor_b_day=Decimal("1.40"),
        labor_c_day=Decimal("0.00"), base_price=Decimal("0"),
        cat5e_coef=Decimal("1.10"), ceiling_coef=Decimal("1.20"),
    )

    item_plain = make_item(qty="10", notes="quota:5-6-001")
    item_plain.id = __import__("uuid").uuid4()
    item_cat5e = make_item(qty="10", notes="quota:5-6-001", is_cat5e=True)
    item_cat5e.id = __import__("uuid").uuid4()
    item_ceiling = make_item(qty="10", notes="quota:5-6-001", is_ceiling=True)
    item_ceiling.id = __import__("uuid").uuid4()

    # 三组分别算（互相不干扰）
    quota_map_plain = {str(item_plain.id): "5-6-001"}
    quota_map_cat5e = {str(item_cat5e.id): "5-6-001"}
    quota_map_ceiling = {str(item_ceiling.id): "5-6-001"}

    bp = compute_quote(
        items=[item_plain], **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items={"5-6-001": qi},
        quote_items_quota_map=quota_map_plain,
    )
    bc = compute_quote(
        items=[item_cat5e], **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items={"5-6-001": qi},
        quote_items_quota_map=quota_map_cat5e,
    )
    bce = compute_quote(
        items=[item_ceiling], **base_kwargs(),
        labor_rates=make_labor_rates(),
        quota_items={"5-6-001": qi},
        quote_items_quota_map=quota_map_ceiling,
    )

    assert bp.quota_total == Decimal("2807.00"), "全 False 应 = 2807.00"
    assert bc.quota_total == Decimal("3087.70"), "is_cat5e 时 2807 × 1.10 = 3087.70"
    assert bce.quota_total == Decimal("3368.40"), "is_ceiling 时 2807 × 1.20 = 3368.40"


# ---------------------------------------------------------------------------
# 8b. 直接费口径切换的警告（差异 > 0.1% 时必有审计警告）
# ---------------------------------------------------------------------------

def test_direct_fee_emits_human_plus_material_warning():
    """DIRECT_FEE：必出现「人+材口径」提示。"""
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="200.00", cost="700.00")],
        **base_kwargs(fee_base=FeeBaseType.DIRECT_FEE),
    )
    assert any("人+材" in w for w in b.warnings), b.warnings
    # 实际设计费 = (200 + 60) × 2% = 5.20
    assert b.design_fee == Decimal("5.20"), (
        "DIRECT_FEE 已不含设备费，基数 = install + auxiliary = 200 + 60 = 260，"
        f"× 2% = 5.20；实际 {b.design_fee}"
    )


def test_direct_fee_emits_audit_warning_when_diff_exceeds_0_1_pct():
    """设备费 > 0 → 直接费与老口径必然差 > 0.1%，必须有审计警告。

    老口径 = 1060, 新口径 = 260；差异比例 = 800/1060 ≈ 75.5%
    """
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="200.00", cost="700.00")],
        **base_kwargs(fee_base=FeeBaseType.DIRECT_FEE),
    )
    audit = [w for w in b.warnings if "差异" in w]
    assert audit, f"差异 > 0.1% 时必须给审计警告；当前 warnings = {b.warnings}"
    # 必须有具体的百分比提示
    assert "%" in audit[0], audit[0]


# ---------------------------------------------------------------------------
# 9. 写回 ORM
# ---------------------------------------------------------------------------

def test_write_breakdown_to_quote_sets_all_columns():
    quote = Quote(
        title="测试", code="QT-1",
        auxiliary_base=AuxiliaryBaseType.DEVICE,
        fee_base=FeeBaseType.DEVICE_AUX_INSTALL,
    )
    b = compute_quote(
        items=[make_item(unit_price="1000.00", install="100.00", cost="700.00")],
        **base_kwargs(training_fee=Decimal("123.45")),
    )
    write_breakdown_to_quote(quote, b)

    assert quote.equipment_total == b.equipment_total
    assert quote.install_total == b.install_total
    assert quote.auxiliary_total == b.auxiliary_total
    assert quote.design_fee == b.design_fee
    assert quote.warranty_fee == b.warranty_fee
    assert quote.training_fee == b.training_fee
    assert quote.subtotal_before_tax == b.subtotal_before_tax
    assert quote.mgmt_profit == b.mgmt_profit
    assert quote.tax_amount == b.tax_amount
    assert quote.total_with_tax == b.total_with_tax
    assert quote.quota_total == b.quota_total
    assert quote.quota_total_with_tax == b.quota_total_with_tax
    assert quote.cost_total == b.cost_total
    assert quote.gross_profit == b.gross_profit
    assert quote.gross_profit_rate == b.gross_profit_rate


# ---------------------------------------------------------------------------
# 10. 内部恒等式 — 无论参数怎么变都必须成立
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("aux_rate", ["0.00", "0.03", "0.06", "0.12"])
@pytest.mark.parametrize("tax_rate", ["0.00", "0.09", "0.13"])
@pytest.mark.parametrize("mgmt_rate", ["0.00", "0.08", "0.15"])
def test_identity_total_equals_components(aux_rate, tax_rate, mgmt_rate):
    """含税合计 ≡ 税前小计 + 管理费利润 + 税额（四舍五入后仍成立）。"""
    b = compute_quote(
        items=[
            make_item(qty="7", unit_price="333.33", install="47.77", cost="211.11"),
            make_item(qty="13", unit_price="99.99", install="8.88", cost="66.66"),
        ],
        **base_kwargs(
            auxiliary_rate=Decimal(aux_rate),
            tax_rate=Decimal(tax_rate),
            mgmt_profit_rate=Decimal(mgmt_rate),
        ),
    )
    assert b.total_with_tax == (
        b.subtotal_before_tax + b.mgmt_profit + b.tax_amount
    ), "含税合计必须等于三项之和"

    assert b.subtotal_before_tax == (
        b.equipment_total + b.install_total + b.auxiliary_total
        + b.design_fee + b.warranty_fee + b.training_fee
    ), "税前小计必须等于六项之和"


def test_zero_items_produces_zeroes_not_nan():
    b = compute_quote(items=[], **base_kwargs())
    for field in (
        "equipment_total", "install_total", "auxiliary_total", "design_fee",
        "warranty_fee", "subtotal_before_tax", "mgmt_profit", "tax_amount",
        "total_with_tax", "cost_total", "gross_profit",
    ):
        assert getattr(b, field) == Decimal("0.00"), f"{field} 应为 0"
    assert b.gross_profit_rate == Decimal("0.0000")


# ---------------------------------------------------------------------------
# 定额编号解析（备注 → 定额表主键）
# ---------------------------------------------------------------------------

def test_parse_quota_code_plain():
    assert parse_quota_code("quota:5-1-12") == "5-1-12"


def test_parse_quota_code_ignores_trailing_note():
    """备注后面常跟说明，只能取第一个 token。

    以前 seed.py 直接用 split("quota:", 1)[1] 取整段，
    于是 "quota:5-1-12 屏蔽" 会拿 "5-1-12 屏蔽" 去查定额表，永远查不到，
    演示报价的定额口径就静默退化成安装费口径。
    """
    assert parse_quota_code("超五类线 quota:5-1-12 屏蔽") == "5-1-12"
    assert parse_quota_code("quota:5-7-3\t吊顶内") == "5-7-3"


def test_parse_quota_code_handles_missing_and_empty():
    assert parse_quota_code(None) is None
    assert parse_quota_code("") is None
    assert parse_quota_code("普通备注，没有定额标记") is None
    # 备注正好是 "quota:" 时曾经抛 IndexError，直接 500
    assert parse_quota_code("quota:") is None
    assert parse_quota_code("quota:   ") is None


def test_quota_note_without_code_does_not_break_compute():
    """带 quota: 但没有编号的明细行不能让整个重算崩掉。

    以前 _recompute 用 it.notes.split("quota:", 1)[1].split()[0]，
    备注正好是 "quota:" 时 split()[0] 抛 IndexError → 整个重算 500。
    """
    rates = make_labor_rates()
    qi = QuotaItem(
        code="5-1-12", name="管内穿线", unit="100m",
        category=QuotaCategory.CABLING, labor_a_day=Decimal("1.5"),
    )
    it = make_item(notes="quota:")
    it.id = uuid4()
    b = compute_quote(
        items=[it],
        quota_items={qi.code: qi},
        quote_items_quota_map={},
        labor_rates=rates,
        **base_kwargs(),
    )
    # 没有映射 → 该行不参与定额口径，但主口径照常算出来
    assert b.quota_total == Decimal("0.00")
    assert b.equipment_total == Decimal("100.00")
    # 三类工日均已就位，不该出现「信息价缺失」的降级告警
    assert not any("信息价" in w for w in b.warnings)


def test_incomplete_labor_rates_falls_back_with_warning():
    """缺任何一类工日的当期价 → 定额口径必须降级并告警，而不是静默算成 0。

    这是真实踩过的坑：/labor-rates 的 is_current 曾经按地区整体翻转，
    连录三条一/二/三类之后只剩最后一类是 current，另外两类在
    _compute_quota_total 里被 `continue` 跳过，定额合计变成 0，
    界面上却显示成一个看着像样的数字，没有任何提示。
    """
    qi = QuotaItem(
        code="5-1-12", name="管内穿线", unit="100m",
        category=QuotaCategory.CABLING, labor_a_day=Decimal("1.5"),
    )
    it = make_item(notes="quota:5-1-12")
    it.id = uuid4()

    only_a = {LaborClass.A: make_labor_rates()[LaborClass.A]}
    b = compute_quote(
        items=[it],
        quota_items={qi.code: qi},
        quote_items_quota_map={str(it.id): "5-1-12"},
        labor_rates=only_a,
        **base_kwargs(),
    )
    assert b.quota_total == b.install_total, "缺价时应退化为安装费口径"
    assert b.quota_total_with_tax == b.total_with_tax
    assert any("class_b" in w and "class_c" in w for w in b.warnings), b.warnings
