"""Excel 导出渲染器单元测试（不碰数据库、不碰 HTTP）。

重点是两条容易悄悄坏掉的性质：
1. 金额是**数字**而不是文本（否则甲方在 Excel 里没法求和）；
2. 空串写成**空单元格**而不是 0（否则「这个口径没有这一项」被读成「零元」）。
"""

from __future__ import annotations

from io import BytesIO

from openpyxl import load_workbook

from app.services.export_data import (
    ExportContext,
    ExportFeeRow,
    ExportItemRow,
    ExportMeta,
    ExportSubsystemRow,
)
from app.services.export_excel import render


def _meta(**kw) -> ExportMeta:
    base = {
        "quote_code": "QT-202609-0001",
        "title": "智能化弱电工程报价单",
        "version_label": "2026-10-08 15:33:07",
        "status_label": "已提交",
        "project_code": "PRJ-202609-0001",
        "project_name": "某商业综合体弱电工程",
        "customer_name": "某某置业有限公司",
        "customer_contact": "张工",
        "customer_phone": "13800000000",
        "address": "宁波市鄞州区某路 1 号",
        "tax_method_label": "一般计税 9%",
        "tax_rate_pct": "9%",
        "generated_at": "2026-09-29 07:00",
        "include_cost": False,
        "notes": "本报价含安装调试，不含土建配合费。",
    }
    base.update(kw)
    return ExportMeta(**base)


def _ctx(*, include_cost: bool = False, items=None, subsystems=None, fees=None) -> ExportContext:
    if fees is None:
        fees = [
            ExportFeeRow("设备费", "394,488.00", ""),
            ExportFeeRow("安装费", "27,551.70", ""),
            # 故意留一个空串：定额口径下这一项不存在
            ExportFeeRow("辅材费", "23,669.28", ""),
            ExportFeeRow("小计（不含税）", "466,457.80", "1,043,498.20", separator=True),
            ExportFeeRow("税金", "45,339.70", ""),
            ExportFeeRow("含税合计", "549,114.12", "1,256,269.56",
                         emphasis=True, separator=True),
        ]
        if include_cost:
            fees += [
                ExportFeeRow("成本合计", "381,219.55", "", separator=True),
                ExportFeeRow("毛利", "167,894.57", ""),
                ExportFeeRow("毛利率", "30%", ""),
            ]
    if items is None:
        items = [
            ExportItemRow(1, "视频监控", "网络半球摄像机", "海康威视", "DS-2CD1321", "台",
                          "120", "680.00", "81,600.00", "设备库", "含支架"),
            ExportItemRow(2, "综合布线", "六类非屏蔽双绞线", "一舟", "CAT6-UTP", "箱",
                          "35", "890.00", "31,150.00", "清单导入", ""),
            # 甲供：金额列必须是空串
            ExportItemRow(3, "机房工程", "精密空调（甲供）", "维谛", "DME07", "台",
                          "2", "", "", "手工录入", "甲供（不计入报价）"),
        ]
    if subsystems is None:
        subsystems = [
            ExportSubsystemRow(1, "视频监控", "120", "81,600.00", "9,792.00", "4,896.00",
                               "91,392.00"),
            ExportSubsystemRow(2, "综合布线", "210", "31,150.00", "3,738.00", "1,869.00",
                               "34,888.00"),
        ]
    return ExportContext(meta=_meta(include_cost=include_cost), fees=fees,
                         subsystems=subsystems, items=items)


def _book(ctx: ExportContext):
    return load_workbook(BytesIO(render(ctx)))


def _flatten(ws) -> str:
    return "\n".join(
        str(v) for row in ws.iter_rows(values_only=True) for v in row if v is not None
    )


# ---------------------------------------------------------------------------

def test_renders_xlsx_bytes() -> None:
    data = render(_ctx())
    assert isinstance(data, bytes)
    assert data[:2] == b"PK"          # xlsx 就是个 zip
    assert len(data) > 3000


def test_three_sheets_with_chinese_names() -> None:
    wb = _book(_ctx())
    assert wb.sheetnames == ["报价汇总", "报价明细", "子系统汇总"]


def test_money_cells_are_numbers_not_text() -> None:
    """金额必须是数字 + 千分位格式，把 "394,488.00" 正确解析成 394488.0。"""
    ws = _book(_ctx())["报价汇总"]
    found = False
    for row in ws.iter_rows():
        for c in row:
            if c.value == 394488.0 or c.value == 394488:
                assert isinstance(c.value, (int, float))
                assert c.number_format == "#,##0.00", c.number_format
                found = True
    assert found, "没找到被正确解析成数字的设备费单元格"


def test_thousand_separator_parsed_correctly() -> None:
    ws = _book(_ctx())["报价明细"]
    vals = [c.value for row in ws.iter_rows() for c in row]
    assert 81600.0 in vals or 81600 in vals


def test_empty_amount_becomes_empty_cell_not_zero() -> None:
    """**关键断言**：定额口径的空串必须是空单元格，不能是 0。

    写成 0 会让甲方以为「定额口径下这一项是零元」，而事实是这个口径没有这一项。
    """
    wb = _book(_ctx())
    ws = wb["报价汇总"]
    # 设备费行的定额口径列（第 3 列）是空串
    for row in ws.iter_rows(min_col=3, max_col=3):
        for c in row:
            assert c.value != 0, f"空串被写成了 0：{c.coordinate}"
    # 明细里甲供那行的单价/合计列也必须是空的
    ws2 = wb["报价明细"]
    headers = [c.value for c in ws2[1]]
    name_col = headers.index("设备/材料名称")
    up = headers.index("单价")
    am = headers.index("合计")
    owner_row = next(
        (row for row in ws2.iter_rows(min_row=2)
         if row[name_col].value and "甲供" in str(row[name_col].value)),
        None,
    )
    assert owner_row is not None, "没找到甲供那一行"
    assert owner_row[up].value is None, "甲供单价应为空单元格"
    assert owner_row[am].value is None, "甲供合计应为空单元格"


def test_percent_is_numeric_with_percent_format() -> None:
    """毛利率 "30%" 应写成 0.3 + '0%' 格式，而不是文本 "30%"。"""
    wb = _book(_ctx(include_cost=True))
    ws = wb["报价汇总"]
    hit = [c for row in ws.iter_rows() for c in row if c.number_format == "0%"]
    assert hit, "没有找到百分数格式的单元格"
    assert any(abs(float(c.value) - 0.3) < 1e-9 for c in hit if isinstance(c.value, (int, float)))


def test_cost_columns_only_when_include_cost() -> None:
    customer = _book(_ctx(include_cost=False))
    internal = _book(_ctx(include_cost=True))
    t1 = _flatten(customer["报价明细"]) + _flatten(customer["报价汇总"])
    t2 = _flatten(internal["报价明细"]) + _flatten(internal["报价汇总"])
    assert "进价" not in t1 and "毛利" not in t1
    assert "进价" in t2 and "毛利" in t2
    assert "成本合计" in t2 and "成本合计" not in t1


def test_empty_items_and_subsystems_do_not_crash() -> None:
    ctx = _ctx(items=[], subsystems=[])
    data = render(ctx)
    assert data[:2] == b"PK"
    wb = load_workbook(BytesIO(data))
    assert "（本报价单暂无明细）" in _flatten(wb["报价明细"])
    assert "（本项目暂无子系统）" in _flatten(wb["子系统汇总"])


def test_print_setup_and_freeze_panes() -> None:
    """对应需求「打印」：A4 横向、适应页宽、跨页重复表头、冻结表头。"""
    wb = _book(_ctx())
    for name in ("报价明细", "子系统汇总"):
        ws = wb[name]
        assert ws.freeze_panes, f"{name} 没冻结表头"
        assert ws.print_title_rows, f"{name} 没设跨页重复表头"
        assert ws.page_setup.orientation == "landscape"
        assert ws.sheet_properties.pageSetUpPr.fitToPage is True
    ws = wb["报价明细"]
    assert ws.auto_filter.ref, "明细表没设自动筛选"


def test_item_row_cell_count_matches_headers() -> None:
    """含成本与不含成本两种形态下，行单元格数都必须等于表头数。"""
    for flag in (False, True):
        ctx = _ctx(include_cost=flag)
        n = len(ctx.item_headers())
        for item in ctx.items:
            assert len(ctx.item_row_cells(item)) == n


def test_money_columns_wide_enough_for_large_amounts() -> None:
    """金额列必须按内容撑宽。

    数字放不下时 Excel 显示的是「####」而不是溢出到邻列 ——
    要是只按表头（「单价」两个字）定宽，遇到千万级金额甲方看到的就是一排 ####。

    注意 openpyxl 的默认列宽是 13.0，所以「没撑宽」和「撑到 13」在数值上分不开；
    这里只断言**下界**：列宽必须 >= 该列最长内容的字符数 + 1。
    """
    from openpyxl.utils import get_column_letter

    big_unit = "12,345,678.90"      # 单价
    big_amount = "123,456,789.01"   # 合计
    fees = [
        ExportFeeRow("设备费", "12,345,678.90", ""),
        ExportFeeRow("含税合计", "98,765,432.10", big_amount, emphasis=True),
    ]
    items = [
        ExportItemRow(1, "视频监控", "大额设备", "海康威视", "M-1", "台",
                      "1000", big_unit, big_amount, "设备库", ""),
    ]
    ctx = ExportContext(meta=_meta(), fees=fees, subsystems=[], items=items)
    wb = load_workbook(BytesIO(render(ctx)))

    def width_of(ws, col_index: int) -> float:
        return ws.column_dimensions[get_column_letter(col_index)].width or 0

    ws = wb["报价明细"]
    headers = [c.value for c in ws[1]]
    for name, expected in (("单价", big_unit), ("合计", big_amount)):
        i = headers.index(name) + 1
        assert width_of(ws, i) >= len(expected) + 1, (
            f"明细表「{name}」列太窄：{width_of(ws, i)} < {len(expected) + 1}，"
            f"「{expected}」会显示成 ####"
        )

    # 汇总表两列金额口径（定额口径那一列放的是更长的数字）
    ws = wb["报价汇总"]
    assert width_of(ws, 2) >= len("98,765,432.10") + 1
    assert width_of(ws, 3) >= len(big_amount) + 1
