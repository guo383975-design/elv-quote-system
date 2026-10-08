"""PDF 导出渲染器测试 — 纯单元测试（无数据库、无 HTTP、不落盘）。

重点防的是「中文变成黑方块却不报错」这种静默失败：PDF 内容流是 Flate 压缩的，
没法直接 grep 中文，所以改成断言**字体程序真的被嵌入了**（`FontFile2` 只可能
来自嵌入的 TrueType 字体；Helvetica 那种内置字体永远不会产生它）。
"""

from __future__ import annotations

import base64
import re
import zlib

import pytest
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics

from app.services import export_pdf
from app.services.export_data import (
    ExportContext,
    ExportFeeRow,
    ExportItemRow,
    ExportMeta,
    ExportSubsystemRow,
)

# ---------------------------------------------------------------------------
# 造数据（直接构造 ExportContext，不经过 ORM）
# ---------------------------------------------------------------------------

def _meta(**overrides) -> ExportMeta:
    base: dict = {
        "quote_code": "QT-2026-0001",
        "title": "宁波某园区智能化弱电工程报价单",
        "version_label": "2026-10-08 15:33:07",
        "status_label": "已审核",
        "project_code": "PRJ-2026-0007",
        "project_name": "宁波北仑某园区智能化改造工程",
        "customer_name": "宁波某某建设有限公司",
        "customer_contact": "张工",
        "customer_phone": "13800000000",
        "address": "浙江省宁波市北仑区某某路 1 号 3 号楼",
        "tax_method_label": "一般计税",
        "tax_rate_pct": "9%",
        "generated_at": "2026-09-29 07:00",
        "include_cost": False,
        "notes": "",
    }
    base.update(overrides)
    return ExportMeta(**base)


def _fees(*, include_cost: bool = False) -> list[ExportFeeRow]:
    rows = [
        ExportFeeRow("设备费", "1,234,567.00", ""),
        ExportFeeRow("安装费", "123,456.00", ""),
        # 小计在「点数单价口径」有值、「定额计价口径」另有值
        ExportFeeRow("小计（不含税）", "1,358,023.00", "1,500,000.00", separator=True),
        ExportFeeRow("含税合计", "1,480,245.07", "1,635,000.00", emphasis=True, separator=True),
    ]
    if include_cost:
        rows += [
            ExportFeeRow("成本合计", "1,000,000.00", "", separator=True),
            ExportFeeRow("毛利", "480,245.07", ""),
        ]
    return rows


def _subsystems() -> list[ExportSubsystemRow]:
    return [
        ExportSubsystemRow(1, "视频监控子系统", "128", "300,000.00",
                           "60,000.00", "18,000.00", "360,000.00"),
        ExportSubsystemRow(2, "门禁与出入口子系统", "36", "180,000.00",
                           "36,000.00", "10,800.00", "216,000.00"),
    ]


def _items(count: int, *, include_cost: bool = False) -> list[ExportItemRow]:
    rows: list[ExportItemRow] = []
    for i in range(1, count + 1):
        rows.append(
            ExportItemRow(
                seq=i,
                subsystem="视频监控子系统",
                name=f"网络高清枪型摄像机 400万 第{i}号点位（含安装支架及防水电源）",
                brand="海康威视",
                model="DS-2CD2T46WD-I5",
                unit="台",
                quantity="2",
                unit_price="680.00",
                amount="1,360.00",
                source_label="自有设备库",
                note="含安装支架及防水电源" if i % 3 else "",
                cost_price="510.00" if include_cost else "",
                cost_amount="1,020.00" if include_cost else "",
                gross="340.00" if include_cost else "",
            )
        )
    return rows


def _ctx(
    *,
    item_count: int = 3,
    include_cost: bool = False,
    with_fees: bool = True,
    with_subsystems: bool = True,
    with_items: bool = True,
    notes: str = "1. 本报价含税，税率见上表。\n2. 甲供设备不计入我方报价。",
) -> ExportContext:
    return ExportContext(
        meta=_meta(include_cost=include_cost, notes=notes),
        fees=_fees(include_cost=include_cost) if with_fees else [],
        subsystems=_subsystems() if with_subsystems else [],
        items=_items(item_count, include_cost=include_cost) if with_items else [],
    )


# ---------------------------------------------------------------------------
# 从产出的 PDF 里取信息的小工具
# ---------------------------------------------------------------------------

def _page_count(data: bytes) -> int:
    """页数 = `/Type /Page` 出现次数 − `/Type /Pages`（页树节点）。"""
    return data.count(b"/Type /Page") - data.count(b"/Type /Pages")


def _text_of(cell) -> str:
    return getattr(cell, "text", str(cell))


def _find_table(story: list, headers: list[str]):
    """按**整行表头**定位表格（不能只比对第一列：明细表和子系统表都以「序号」开头）。"""
    for flow in story:
        cells = getattr(flow, "_cellvalues", None)
        if cells and [_text_of(cell) for cell in cells[0]] == list(headers):
            return flow
    raise AssertionError(f"story 里没找到表头为 {headers!r} 的表格")


def _table_texts(table) -> list[list[str]]:
    return [[_text_of(cell) for cell in row] for row in table._cellvalues]


# reportlab 的页面内容流是 ASCII85 + Flate 两层压缩，中文又被写成字形索引，
# 所以只能解压后按 Tf 操作符判断「正文到底是拿哪个字体画的」。
_A85_PAGE_STREAM = re.compile(rb"/Filter \[ /ASCII85Decode /FlateDecode \][^>]*>>\s*stream\r?\n")
_TEXT_TOKEN = re.compile(rb"/(F[0-9A-Za-z+]+) [\d.]+ Tf|(?:Tj|TJ)")


def _page_streams(data: bytes) -> list[bytes]:
    streams: list[bytes] = []
    for match in _A85_PAGE_STREAM.finditer(data):
        end = data.find(b"endstream", match.end())
        raw = data[match.end():end].strip()
        streams.append(zlib.decompress(base64.a85decode(raw, adobe=True)))
    return streams


# reportlab 会把生成时间戳写进 /Info（精确到秒），trailer 里还有一个基于时间的
# /ID；两次渲染只要跨过一秒这些字节就会变。做整文件比较前必须先归一化掉。
_VOLATILE_PDF_FIELDS = re.compile(rb"/(ID|CreationDate|ModDate)\s*(\(.*?\)|\[.*?\])", re.S)


def _fonts_used_to_draw_text(data: bytes) -> set[bytes]:
    """所有 Tj/TJ 之前最后一个 Tf 选中的字体资源名。"""
    used: set[bytes] = set()
    current = None
    for stream in _page_streams(data):
        for token in _TEXT_TOKEN.finditer(stream):
            if token.group(1) is not None:
                current = token.group(1)
            elif current is not None:
                used.add(current)
    return used


# 页面内容里所有带绝对坐标的几何操作：q/Q 栈、cm/Tm 变换、表格背景矩形、网格线段。
# 只认「独占一行」的 q/Q，免得把文本串里的字母当成栈操作。
_GEOMETRY = re.compile(
    r"^(q|Q)$"
    r"|1 0 0 1 (-?[\d.]+) (-?[\d.]+) (cm|Tm)"
    r"|n (-?[\d.]+) (-?[\d.]+) (-?[\d.]+) (-?[\d.]+) re"
    r"|n (-?[\d.]+) (-?[\d.]+) m (-?[\d.]+) (-?[\d.]+) l S",
    re.M,
)


def _absolute_geometry(data: bytes) -> list[tuple[float, float, float, float, str]]:
    """把所有几何操作换算成绝对坐标 `(x1, y1, x2, y2, 类型)`。

    类型是 point（文字基线）、rect（表格背景）或 line（表格网格线）。
    reportlab 画表格时只做平移（`1 0 0 1 tx ty cm`），所以按 q/Q 栈累加 tx/ty 即可；
    每个文本块开头的 `Tm` 只是相对当前 CTM 设的绝对位移，不能继续累积。
    """
    boxes: list[tuple[float, float, float, float, str]] = []
    for stream in _page_streams(data):
        stack: list[tuple[float, float]] = []
        tx = ty = 0.0
        for match in _GEOMETRY.finditer(stream.decode("latin-1")):
            if match.group(1) is not None:
                if match.group(1) == "q":
                    stack.append((tx, ty))
                elif stack:
                    tx, ty = stack.pop()
            elif match.group(2) is not None:
                x, y = float(match.group(2)), float(match.group(3))
                if match.group(4) == "cm":
                    tx += x
                    ty += y
                else:
                    boxes.append((tx + x, ty + y, tx + x, ty + y, "point"))
            elif match.group(5) is not None:
                x, y, w, h = (float(g) for g in match.groups()[4:8])
                boxes.append((tx + x, ty + y, tx + x + w, ty + y + h, "rect"))
            else:
                x1, y1, x2, y2 = (float(g) for g in match.groups()[8:12])
                boxes.append((tx + x1, ty + y1, tx + x2, ty + y2, "line"))
    return boxes


# ---------------------------------------------------------------------------
# 1. 正常数据
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("include_cost", [False, True])
def test_render_returns_pdf_bytes(include_cost):
    data = export_pdf.render(_ctx(include_cost=include_cost, item_count=5))

    assert isinstance(data, bytes)
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    assert len(data) > 2000


def test_render_is_stable_across_calls():
    """纯函数语义：同样输入 → 同样的文档内容。

    这里**不**断言 byte-identical：reportlab 每次都往 /Info 写 /CreationDate、
    /ModDate，trailer 里还有个基于时间的 /ID，两次调用只要跨过一秒字节就会变
    （拿字节相等当断言会变成间歇性失败，比没有测试更糟）。改成断言真正
    想保证的性质：

    1. 正文（页面内容流）逐字节一致 —— 排版结果是确定的；
    2. 归一化掉那几个易变字段后，整个文件一致 —— 除了时间戳没有别的抖动。
    """
    ctx = _ctx(item_count=120, include_cost=True)
    first = export_pdf.render(ctx)
    second = export_pdf.render(ctx)

    first_pages = _page_streams(first)
    second_pages = _page_streams(second)
    assert len(first_pages) >= 2, "明细没撑出多页，这条测试就没测到跨页路径"
    assert first_pages == second_pages

    assert _VOLATILE_PDF_FIELDS.sub(b"", first) == _VOLATILE_PDF_FIELDS.sub(b"", second)


def test_volatile_field_normalization_really_erases_timestamps():
    """把「跨秒」这件事确定性地造出来，证明归一化不是个空操作。

    否则上面那条「稳定」测试有可能因为正则什么都没匹配到而假绿。
    """
    data = export_pdf.render(_ctx())
    assert _VOLATILE_PDF_FIELDS.search(data), "没找到易变字段，归一化正则已经失效"

    bumped = re.sub(rb"D:\d{14}[+-]\d{2}'\d{2}'", b"D:19990101000000+08'00'", data)
    bumped = re.sub(
        rb"/ID\s*\[\s*<[0-9a-fA-F]+>\s*<[0-9a-fA-F]+>\s*\]", b"/ID [<00><01>]", bumped
    )
    assert bumped != data, "没能造出不同的时间戳/摘要，这条测试就没意义了"

    assert _VOLATILE_PDF_FIELDS.sub(b"", bumped) == _VOLATILE_PDF_FIELDS.sub(b"", data)


# ---------------------------------------------------------------------------
# 2. 中文字体确实嵌入了（防「中文变方块却不报错」的关键断言）
# ---------------------------------------------------------------------------

def test_cjk_font_is_embedded():
    data = export_pdf.render(_ctx())

    # FontFile2 是嵌入的 TrueType 字体程序流；内置的 Helvetica 不可能有。
    assert b"FontFile2" in data, "中文字体没有嵌入，PDF 里的中文会变成黑方块"
    # 子集前缀（AAAAAA+）说明用的确实是嵌入的 TrueType 子集。
    assert re.search(rb"/BaseFont\s*/[A-Z]{6}\+", data), "没有子集化嵌入的字体"


def test_drawn_text_uses_the_embedded_font_not_a_builtin():
    """正文里每一段文字都必须用嵌入字体画。

    reportlab 每页开头都会先写一句 `/F1 12 Tf`（内置 Helvetica 的默认状态），
    它不画任何字，所以「PDF 里有 Helvetica」并不代表退化；真正要盯的是
    **Tj/TJ 之前最后选中的字体**。内置标准字体没有子集前缀，一画中文就是方块。
    """
    data = export_pdf.render(_ctx())
    streams = _page_streams(data)
    assert streams, "没解出页面内容流：reportlab 可能改了内容流压缩方式，请更新本测试"

    drawn = _fonts_used_to_draw_text(data)
    assert drawn, "页面里一个字都没画出来"
    for name in drawn:
        assert b"+" in name, f"文字用了非嵌入字体 {name!r}，中文会变方块"


def test_emphasis_renders_even_without_a_bold_face(monkeypatch):
    """wqy-zenhei 只有一个字重：找不到粗体不是错误，合计行改用浅灰底标识。"""
    monkeypatch.setattr(export_pdf, "_BOLD_COMPANIONS", {})
    monkeypatch.setattr(export_pdf, "_registered_fonts", {})

    data = export_pdf.render(_ctx())
    assert data.startswith(b"%PDF-")
    assert b"FontFile2" in data

    fonts = export_pdf._prepare_fonts()
    assert fonts.has_bold is False
    assert fonts.bold == fonts.base


def test_tables_stay_inside_the_page_margins():
    """表格不能越出 15mm 页边距。

    SimpleDocTemplate 的 Frame 自带 6pt 内边距，表格宽度按「纸张 − 左右边距」
    算就会往右多出 6pt；这个断言就是防这个的（曾经真的踩过）。
    """
    data = export_pdf.render(_ctx(item_count=40, include_cost=True))
    boxes = _absolute_geometry(data)
    assert boxes, "没解出任何几何操作：reportlab 可能改了内容流编码，请更新本测试"

    # 纸张尺寸以 export_pdf 的 _PAGE_SIZE 为准（2026-10-05 ✓）：
    #   这里原来写死 A4（纵向）✗ —— 改成 A4 横向后，右边距变成 799.5pt ✓，
    #   而旧上界 595pt ✗ 会把**完全合规**的 799.37pt 误报成"越过右边距" ✓。
    _pw, _ph = export_pdf._PAGE_SIZE
    left, right = 15 * mm, _pw - 15 * mm
    top = _ph - 15 * mm
    footer_floor = 8 * mm - 1        # 页脚画在下边距以内，属预期
    for x1, y1, x2, y2, kind in boxes:
        assert min(x1, x2) >= left - 0.5, f"越过左边距：{kind} x={min(x1, x2):.2f}"
        assert max(x1, x2) <= right + 0.5, f"越过右边距：{kind} x={max(x1, x2):.2f}"
        assert min(y1, y2) >= footer_floor, f"画到页脚下面去了：{kind} y={min(y1, y2):.2f}"
        assert max(y1, y2) <= top + 0.5, f"越过上边距：{kind} y={max(y1, y2):.2f}"
        if kind == "rect":           # 表格背景一定属于正文，必须整块在页边距以内
            assert min(y1, y2) >= 15 * mm - 0.5, f"表格越出下边距：y={min(y1, y2):.2f}"


# ---------------------------------------------------------------------------
# 2b. 列宽必须按真实字体度量算（防「1200 被折成 120 + 0」）
# ---------------------------------------------------------------------------

# 线上真实数据的长相：4 字品牌、15 字符型号、5~7 字子系统、4 位数量、7 位金额。
_LONG_ROWS: tuple[tuple[str, ...], ...] = (
    # 数量   金额            品牌      型号              子系统
    ("1200", "1,256,269.56", "海康威视", "DS-2CD2T46WD-I5", "视频监控子系统"),
    ("2360", "1,256,269.56", "中控智慧", "ST8000VX010", "门禁一卡通"),
    ("128", "998,001.07", "霍尼韦尔", "DS-2CD2T46WD-I5", "综合布线"),
    ("4096", "1,000,000.00", "海康威视", "ST8000VX010", "门禁一卡通"),
)

_LONG_NAME = "网络高清枪型摄像机400万（含安装支架及防水电源）"


def _long_ctx(*, include_cost: bool) -> ExportContext:
    """每一列都塞到「最长」—— 按权重分列宽时这组数据必然让金额/数量折行。"""
    items = [
        ExportItemRow(
            seq=index,
            subsystem=subsystem,
            name=_LONG_NAME,
            brand=brand,
            model=model,
            unit="台",
            quantity=quantity,
            unit_price="12,345.67",
            amount=amount,
            source_label="自有设备库",
            note="quota:5-6-001",
            cost_price="8,888.88" if include_cost else "",
            cost_amount=amount if include_cost else "",
            gross="200,000.00" if include_cost else "",
        )
        for index, (quantity, amount, brand, model, subsystem) in enumerate(_LONG_ROWS, start=1)
    ]
    return ExportContext(
        meta=_meta(include_cost=include_cost, notes="按实结算。"),
        fees=_fees(include_cost=include_cost),
        subsystems=_subsystems(),
        items=items,
    )


@pytest.mark.parametrize("include_cost", [False, True])
def test_item_widths_fit_the_longest_content(include_cost):
    """不可折行列（数量/单价/合计/进价/成本小计/毛利/品牌/来源/序号/单位/子系统）
    必须一行放得下最长内容 —— 折行的金额会让甲方读错数。

    这条就是本次缺陷的回归网：把列宽换回硬编码权重，它立刻失败。
    """
    ctx = _long_ctx(include_cost=include_cost)
    theme = export_pdf._theme_for(ctx, export_pdf._prepare_fonts())
    headers, rows, widths = export_pdf._item_table(ctx, theme)

    label = "含成本 14 列" if include_cost else "客户版 11 列"
    size = theme.item.left.fontSize
    room = [width - 2 * export_pdf._ITEM_PADDING for width in widths]
    assert len(widths) == len(headers)

    for col, header in enumerate(headers):
        if header in export_pdf._WRAPPABLE_HEADERS:
            continue                      # 名称/型号/备注 折行属于正常排版
        need_header = pdfmetrics.stringWidth(header, theme.fonts.bold, size)
        assert room[col] >= need_header - 1e-6, (
            f"{label}：表头「{header}」放不下（可用 {room[col]:.2f}pt < 需要 {need_header:.2f}pt）"
        )
        worst = max(rows, key=lambda row: pdfmetrics.stringWidth(row[col], theme.fonts.base, size))[col]
        need = pdfmetrics.stringWidth(worst, theme.fonts.base, size)
        assert room[col] >= need - 1e-6, (
            f"{label}：「{header}」列放不下最长内容 {worst!r}"
            f"（可用 {room[col]:.2f}pt < 需要 {need:.2f}pt）"
        )


@pytest.mark.parametrize("include_cost", [False, True])
def test_non_wrappable_cells_really_render_on_one_line(include_cost):
    """直接用排版引擎验证「一个数字不会被拆成两行」——这才是客户看到的样子。

    宽度够不够是算出来的，会不会折行是 platypus 决定的，所以这里问 platypus 本人：
    `Paragraph.breakLines(可用宽度)` 返回的行数必须 ≤ 1。
    """
    ctx = _long_ctx(include_cost=include_cost)
    theme = export_pdf._theme_for(ctx, export_pdf._prepare_fonts())
    headers, rows, widths = export_pdf._item_table(ctx, theme)

    for col, header in enumerate(headers):
        if header in export_pdf._WRAPPABLE_HEADERS:
            continue
        room = widths[col] - 2 * export_pdf._ITEM_PADDING
        style = export_pdf._pick(theme.item, export_pdf._align_for(header))
        for text in [header, *(row[col] for row in rows)]:
            if not text:
                continue
            lines = export_pdf._cell(text, style).breakLines(room).lines
            assert len(lines) <= 1, f"「{header}」列把 {text!r} 折成了 {len(lines)} 行"


@pytest.mark.parametrize("include_cost", [False, True])
def test_item_widths_fill_the_usable_width_exactly(include_cost):
    """列宽之和必须正好等于可用宽度，否则表格右边会留一道缝或越出页边距。"""
    ctx = _long_ctx(include_cost=include_cost)
    theme = export_pdf._theme_for(ctx, export_pdf._prepare_fonts())
    _headers, _rows, widths = export_pdf._item_table(ctx, theme)

    assert abs(sum(widths) - export_pdf._USABLE_WIDTH) < 1e-6


def test_item_font_size_shrinks_for_the_14_column_layout():
    """14 列比 11 列挤，字号候选要自动收档，而不是让金额折行。"""
    fonts = export_pdf._prepare_fonts()
    plain = export_pdf._pick_item_font_size(_long_ctx(include_cost=False), fonts)
    with_cost = export_pdf._pick_item_font_size(_long_ctx(include_cost=True), fonts)

    assert plain in export_pdf._ITEM_FONT_SIZES
    assert with_cost in export_pdf._ITEM_FONT_SIZES
    assert with_cost <= plain, "列更多反而用了更大的字号"


def test_absurd_amounts_never_collapse_a_column_to_zero_width():
    """金额长到怎么排都放不下时（保命分支），也绝不能出现 0 宽列 ——
    0 宽的 Table 列会把文字画成一堆重叠的鬼画符。

    2026-10-05 ✓：纸张改成 A4 横向后可用宽度变大 ✓ → 触发门槛跟着变 ✓。
    所以这里**自动加长金额**（金额列不可压缩 ✓）直到真的超过可用宽度 ✓，
    不再依赖"某个固定的数据长度刚好够" ✗（那样一改纸型就失效 ✓）。
    """
    base = "1,234,567,890,123.45"
    fonts = export_pdf._prepare_fonts()

    needs = None
    rows: list = []
    for extra in range(0, 10):
        huge = base + "0" * extra          # 加长会同时变宽 ✓
        items = [
            ExportItemRow(i, "视频监控子系统", _LONG_NAME, "海康威视", "DS-2CD2T46WD-I5", "台",
                          "123456", huge, huge, "自有设备库", "quota:5-6-001", huge, huge, huge)
            for i in range(1, 4)
        ]
        ctx = ExportContext(
            meta=_meta(include_cost=True, notes=""),
            fees=_fees(include_cost=True),
            subsystems=_subsystems(),
            items=items,
        )
        theme = export_pdf._theme_for(ctx, fonts)
        _headers, rows, widths = export_pdf._item_table(ctx, theme)
        needs = export_pdf._column_needs(
            ctx.item_headers(), rows, header_font=fonts.bold, cell_font=fonts.base,
            size=theme.item.left.fontSize, padding=export_pdf._ITEM_PADDING,
        )
        if needs.hard_sum >= export_pdf._USABLE_WIDTH:
            break

    assert needs is not None
    assert needs.hard_sum >= export_pdf._USABLE_WIDTH, (
        f"加长到 {len(base) + extra} 个字符仍没触发保命分支（hard_sum="
        f"{needs.hard_sum:.1f} < usable={export_pdf._USABLE_WIDTH:.1f}）✗ 测试失效"
    )
    assert min(widths) > 0, "出现了 0 宽列 ✗"
    assert abs(sum(widths) - export_pdf._USABLE_WIDTH) < 1e-6
    assert export_pdf.render(ctx).startswith(b"%PDF-")



def test_many_items_span_multiple_pages():
    small = export_pdf.render(_ctx(item_count=3, include_cost=True))
    large = export_pdf.render(_ctx(item_count=120, include_cost=True))

    assert _page_count(large) >= 2, "120 行明细居然只有一页，分页没生效"
    assert len(large) > len(small)


def test_footer_total_matches_real_page_count():
    """两遍法算出来的总页数必须等于最终文档的真实页数（防页码写错）。

    单页文档上「页数稳定」是废话，所以这里用 120 行明细撑出多页，
    并且连渲染两次确认第二遍排版没有改变分页。
    """
    ctx = _ctx(item_count=120, include_cost=True)
    theme = export_pdf._theme_for(ctx, export_pdf._prepare_fonts())

    total = export_pdf._count_pages(ctx, theme)
    assert total >= 2, "120 行明细没撑出多页，这条测试就失去意义了"

    for _ in range(2):
        assert _page_count(export_pdf.render(ctx)) == total


def test_single_page_total_is_one():
    """少量明细应当很快出完；并**固化"客户版 PDF 必须是 A4 横向"**（2026-10-05 用户要求 ✓）。

    横向页高度只有 595pt ✓（纵向 842pt ✓）→ 同样的内容会更早翻页 ✓，
    所以这里按"≤2 页"断言 ✓，而不是旧的"恰好 1 页" ✗。
    """
    ctx = _ctx(item_count=3)
    theme = export_pdf._theme_for(ctx, export_pdf._prepare_fonts())
    assert export_pdf._count_pages(ctx, theme) <= 2
    assert export_pdf._PAGE_SIZE[0] > export_pdf._PAGE_SIZE[1], "客户版 PDF 必须是 A4 横向 ✓"


# ---------------------------------------------------------------------------
# 4. 空数据不崩
# ---------------------------------------------------------------------------

def test_all_collections_empty_does_not_crash():
    data = export_pdf.render(_ctx(with_fees=False, with_subsystems=False, with_items=False))

    assert data.startswith(b"%PDF-")
    assert _page_count(data) == 1


@pytest.mark.parametrize(
    ("with_fees", "with_subsystems", "with_items"),
    [(False, True, True), (True, False, True), (True, True, False)],
)
def test_partially_empty_collections_do_not_crash(with_fees, with_subsystems, with_items):
    data = export_pdf.render(
        _ctx(with_fees=with_fees, with_subsystems=with_subsystems, with_items=with_items)
    )
    assert data.startswith(b"%PDF-")
    assert b"FontFile2" in data


def test_empty_fee_amount_is_left_blank_not_zero():
    """费用表里空串必须留空 —— 补 0 会变成「这一项是零元」的错误说法。"""
    ctx = _ctx()
    theme = export_pdf._theme_for(ctx, export_pdf._prepare_fonts())
    table = _find_table(export_pdf._story(ctx, theme), ctx.fee_headers())
    texts = _table_texts(table)

    # 第 0 行是表头，第 1 行是「设备费」：定额口径是空串。
    assert texts[0] == ctx.fee_headers()
    assert texts[1][0] == "设备费"
    assert texts[1][2] == ""
    assert all(cell != "0.00" for row in texts for cell in row)


def test_item_columns_follow_include_cost():
    """明细列数随 include_cost 变化，渲染器不得硬编码。"""
    plain = _ctx(include_cost=False, item_count=2)
    with_cost = _ctx(include_cost=True, item_count=2)

    assert len(plain.item_headers()) == 11
    assert len(with_cost.item_headers()) == 14
    assert export_pdf.render(plain).startswith(b"%PDF-")
    assert export_pdf.render(with_cost).startswith(b"%PDF-")

    theme = export_pdf._theme_for(with_cost, export_pdf._prepare_fonts())
    table = _find_table(export_pdf._story(with_cost, theme), with_cost.item_headers())
    assert len(table._cellvalues[0]) == 14
    assert [_text_of(c) for c in table._cellvalues[0]] == with_cost.item_headers()


# ---------------------------------------------------------------------------
# 5. 找不到字体 → RuntimeError（而不是静默变方块）
# ---------------------------------------------------------------------------

def test_missing_font_raises_runtime_error(monkeypatch):
    monkeypatch.delenv(export_pdf.FONT_ENV_VAR, raising=False)
    monkeypatch.setattr(
        export_pdf,
        "FALLBACK_FONT_PATHS",
        ("/nonexistent/font-a.ttc", "C:\\nonexistent\\font-b.ttf"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        export_pdf.render(_ctx())

    message = str(excinfo.value)
    assert "中文字体" in message
    assert "QUOTE_EXPORT_FONT" in message


def test_env_font_wins_then_falls_through(monkeypatch):
    real_path, real_index = export_pdf.resolve_font()

    # 环境变量指向真实字体：优先用它，哪怕候选列表全是假路径。
    monkeypatch.setenv(export_pdf.FONT_ENV_VAR, f"{real_path}#{real_index}")
    monkeypatch.setattr(export_pdf, "FALLBACK_FONT_PATHS", ("/nonexistent/a.ttc",))
    assert export_pdf.resolve_font() == (real_path, real_index)

    # 环境变量指向不存在的文件：继续找下一个存在的候选，而不是直接失败。
    monkeypatch.setenv(export_pdf.FONT_ENV_VAR, "/nonexistent/missing.ttc")
    monkeypatch.setattr(export_pdf, "FALLBACK_FONT_PATHS", (real_path,))
    assert export_pdf.resolve_font() == (real_path, 0)


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("/usr/share/fonts/wqy-zenhei.ttc#1", ("/usr/share/fonts/wqy-zenhei.ttc", 1)),
        ("/usr/share/fonts/wqy-zenhei.ttc", ("/usr/share/fonts/wqy-zenhei.ttc", 0)),
        ('"C:\\Windows\\Fonts\\msyh.ttc"', ("C:\\Windows\\Fonts\\msyh.ttc", 0)),
        ("", ("", 0)),
    ],
)
def test_parse_font_spec(spec, expected):
    assert export_pdf.parse_font_spec(spec) == expected


def test_export_settings_valid_days_precedence() -> None:
    """有效期的取值链 + 一致性（2026-10-08 修 bug 的回归测试）。

    bug 现象：条款**文本**按每单覆盖渲染 ✓，但「有效期至」用的是系统默认 ✗
    → 同一份发给客户的文件里自相矛盾（写「报价有效期 7 天」却算 30 天）✗。
    真因：_export_settings 算 valid_days 时只看了旧字段 valid_days_override ✗，
    没看新版 terms_override.valid_days ✗（预览页/导出 PDF/export-clauses 都取自它）。
    所以这条测试**同时**钉住"数字"和"条款文本里的天数" ✓ —— 只钉一个仍会漏 ✗。
    """
    from app.services.export_data import _export_settings

    settings = {"quote": {"valid_days": 30, "clauses": None}}

    # ① 每单条款覆盖优先（本次修的正是这条）
    out = _export_settings(settings, {"terms_override": {"valid_days": 7}})
    assert out["valid_days"] == 7, out["valid_days"]
    assert any("7 天" in c for c in out["clauses"]), out["clauses"]
    assert out["terms_overridden"] is True

    # ② 旧字段仍兼容（老调用方/老数据）
    out2 = _export_settings(settings, {"valid_days": 15})
    assert out2["valid_days"] == 15, out2["valid_days"]
    assert any("15 天" in c for c in out2["clauses"]), out2["clauses"]

    # ③ 两者都有 → 每单条款（新版）优先于旧字段
    out3 = _export_settings(settings, {"valid_days": 15, "terms_override": {"valid_days": 7}})
    assert out3["valid_days"] == 7, out3["valid_days"]

    # ④ 都没有 → 系统设置
    out4 = _export_settings({"quote": {"valid_days": 45}}, None)
    assert out4["valid_days"] == 45, out4["valid_days"]

    # ⑤ 全空 → 模块常量（30）
    out5 = _export_settings(None, None)
    assert out5["valid_days"] == 30, out5["valid_days"]
