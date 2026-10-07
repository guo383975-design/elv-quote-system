"""报价单 → PDF 字节流（纯内存：不落盘、不走网络、不查库）。

三条不能让步的约束，写在最前面：

1. **中文必须嵌入真实字形。** reportlab 内置的 Helvetica 没有任何 CJK 字形，
   拿它渲染中文会得到满页黑方块**而且不报错** —— 这是最坏的失败方式，
   因为调用方拿到的是一份「看起来成功」的废纸。所以找不到中文字体时
   本模块直接抛 `RuntimeError`，绝不静默退化成 Helvetica。
2. **空串 ≠ 0。** 费用表的某一级在某个计价口径下不存在时，`export_data`
   给的是空串；渲染时必须原样留空。补 0 会让甲方以为「这一项是零元」，
   而实际是「这个口径里根本没有这一项」。
3. **明细必须能跨页、且每页重复表头。** 明细行数没有上限，分页交给
   platypus（`repeatRows=1`），不做任何 canvas 手工定位。

页脚要写「第 X 页 / 共 Y 页」，而总页数只有在文档排完版之后才知道，
因此用**两遍法**：第一遍只数页数（不画页脚），第二遍把总数画进页脚。
两遍走同一条 story 生成路径，保证分页完全一致；页脚画在 15mm 页边距
以内，不占版面，不影响分页。

本模块只消费 `export_data.ExportContext`，不碰 ORM、不碰权限、不碰配置。
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Frame,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.services.export_data import QUOTE_CLAUSES, ExportContext
from app.services.quote_terms import build_variables, render_clauses

__all__ = ["parse_font_spec", "render", "resolve_font"]


# ---------------------------------------------------------------------------
# 字体：中文必须嵌入
# ---------------------------------------------------------------------------

FONT_ENV_VAR = "QUOTE_EXPORT_FONT"

# 按顺序取第一个**存在**的字体文件。Linux 服务器装的是 wqy-zenhei（.ttc，
# 中文字形在第 0 号子字体里，必须显式传 subfontIndex，否则可能取到不含
# 中文的那一份）。
FALLBACK_FONT_PATHS: tuple[str, ...] = (
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "C:\\Windows\\Fonts\\msyh.ttc",
    "C:\\Windows\\Fonts\\simsun.ttc",
    "C:\\Windows\\Fonts\\simhei.ttf",
)

# 粗体只用于「合计行 / 表头 / 小标题」。按字体家族写清楚各自的粗体搭档：
# 宋体本来没有粗体（用黑体顶上），wqy-zenhei 只有一个字重，所以 Linux 上
# 注定找不到粗体 —— 那不是错误，重点行改用浅灰底标识（见 _fee_flowables）。
_BOLD_COMPANIONS: dict[str, tuple[str, ...]] = {
    "msyh.ttc": ("msyhbd.ttc",),
    "msyh.ttf": ("msyhbd.ttf",),
    "simsun.ttc": ("simhei.ttf",),
    "simhei.ttf": (),          # 自带重字重，无需搭档
    "wqy-zenhei.ttc": (),      # 单字重
}

_FONT_NAME = "QuoteCJK"
_FONT_BOLD_NAME = "QuoteCJK-Bold"

# 已注册字体缓存：key 是 (路径, 子字体索引)。注意**解析永远照做**
# （resolve_font 每次都跑），缓存只跳过重复读取几十 MB 的字体文件这一步。
_registered_fonts: dict[tuple[str, int], _FontSet] = {}


@dataclass(frozen=True)
class _FontSet:
    """一次渲染要用的字体名（都已注册进 pdfmetrics）。"""

    base: str
    bold: str
    has_bold: bool
    path: str


def parse_font_spec(spec: str) -> tuple[str, int]:
    """字体描述 → `(路径, 子字体索引)`。

    `QUOTE_EXPORT_FONT` 允许写成 `/path/a.ttc#1` 来指定 `.ttc` 里的子字体；
    没有 `#`（或 `#` 后面不是数字）时索引按 0 处理。顺手剥掉两侧引号，
    免得在 `.env` 里写成 `QUOTE_EXPORT_FONT="C:\\Windows\\Fonts\\msyh.ttc"` 时踩坑。
    """
    text = (spec or "").strip().strip('"').strip("'")
    path, sep, index = text.rpartition("#")
    if sep and path and index.strip().isdigit():
        return path, int(index.strip())
    return text, 0


def _is_file(path: str) -> bool:
    try:
        return Path(path).is_file()
    except OSError:  # 路径过长 / 权限异常等，都按「不存在」处理
        return False


def resolve_font() -> tuple[str, int]:
    """按优先级返回第一个存在的中文字体 `(路径, 子字体索引)`。

    顺序：`QUOTE_EXPORT_FONT` → wqy-zenhei → Windows 系统字体。
    环境变量指向的文件不存在时**继续往下找**，而不是直接失败 ——
    「找不到第一个存在的」才算找不到。

    一个都不存在时抛 `RuntimeError`，消息里必须出现「中文字体」字样，
    并且把尝试过的路径列出来，方便运维一眼看出该装什么。
    """
    candidates: list[str] = []
    env_value = os.environ.get(FONT_ENV_VAR, "")
    if env_value.strip():
        candidates.append(env_value)
    candidates.extend(FALLBACK_FONT_PATHS)

    tried: list[str] = []
    for spec in candidates:
        path, index = parse_font_spec(spec)
        if not path:
            continue
        tried.append(path)
        if _is_file(path):
            return path, index

    raise RuntimeError(
        "没找到中文字体，PDF 会变方块，请装 fonts-wqy-zenhei 或设 "
        f"{FONT_ENV_VAR} 指向一个存在的中文字体文件（.ttc 可用 /path/a.ttc#0 "
        f"指定子字体）。已尝试：{'、'.join(tried) or '（无候选路径）'}"
    )


def _resolve_bold_face(regular_path: str) -> tuple[str, int] | None:
    """找常规字体的粗体搭档；没有就返回 None（不是错误）。

    先在常规字体所在目录里找，再退回 Windows 字体目录。wqy-zenhei 只有
    一个字重，Linux 上注定返回 None —— 此时重点行改用浅灰底标识。
    """
    names = _BOLD_COMPANIONS.get(Path(regular_path).name.lower(), ())
    if not names:
        return None
    directories = [Path(regular_path).parent, Path("C:\\Windows\\Fonts")]
    for directory in directories:
        for name in names:
            candidate = directory / name
            if str(candidate) != regular_path and _is_file(str(candidate)):
                return str(candidate), 0
    return None


def _prepare_fonts() -> _FontSet:
    """解析 + 注册中文字体，返回可用的字体名。

    先解析（找不到就抛 `RuntimeError`）再查缓存，这样「字体全被挪走」的
    场景不会被缓存掩盖掉。
    """
    path, index = resolve_font()
    key = (path, index)
    cached = _registered_fonts.get(key)
    if cached is not None:
        return cached

    pdfmetrics.registerFont(TTFont(_FONT_NAME, path, subfontIndex=index))

    bold_face = _resolve_bold_face(path)
    if bold_face is not None and bold_face != key:
        pdfmetrics.registerFont(TTFont(_FONT_BOLD_NAME, bold_face[0], subfontIndex=bold_face[1]))
        bold_name = _FONT_BOLD_NAME
        has_bold = True
    else:
        bold_name = _FONT_NAME
        has_bold = False

    # 显式登记字族：单元格文本里万一带 <b> 标签，也不会去猜一个不存在的字重。
    pdfmetrics.registerFontFamily(
        _FONT_NAME,
        normal=_FONT_NAME,
        bold=bold_name,
        italic=_FONT_NAME,
        boldItalic=bold_name,
    )

    fonts = _FontSet(base=_FONT_NAME, bold=bold_name, has_bold=has_bold, path=path)
    _registered_fonts[key] = fonts
    return fonts


# ---------------------------------------------------------------------------
# 版面常量
# ---------------------------------------------------------------------------

_PAGE_MARGIN = 15 * mm

# SimpleDocTemplate 给页面 Frame 留了默认内边距（左右各 6pt），flowable 真正能用
# 的宽度比「纸张宽度 − 左右页边距」窄 12pt。不扣掉它，表格右边缘会越过右边距
# 6pt（左边距 15mm、右边距只剩 13mm，肉眼能看出来）。这里直接从 reportlab 的
# Frame 默认值读出来，而不是写死 12，免得 reportlab 改默认值时悄悄错位。
_PROBE_FRAME = Frame(0, 0, 100, 100)
_FRAME_SIDE_PADDING = _PROBE_FRAME.leftPadding + _PROBE_FRAME.rightPadding
# A4 横向（2026-10-05 用户要求 ✓）：客户版 PDF 一律横向 ✓
#   纵向时表格可用宽度只有 180mm ✗ → 表头被挤成"一个字一行" ✓（实测 ✓）
_PAGE_SIZE = landscape(A4)
_USABLE_WIDTH = _PAGE_SIZE[0] - 2 * _PAGE_MARGIN - _FRAME_SIDE_PADDING
_FOOTER_Y = 8 * mm

_BODY_SIZE = 9.5      # 信息块 / 备注
_TABLE_SIZE = 9.0     # 费用表 / 子系统表
_TITLE_SIZE = 16.0
_HEADING_SIZE = 10.5
_SUBTITLE_SIZE = 8.5
_FOOTER_SIZE = 7.5

# 明细表字号候选：先用 9pt，放不下再逐档收。7.5pt 是底线 —— 再小就伤可读性了，
# 而且明细列数随 include_cost 变化（11 列 / 14 列），固定字号必然有一边不合适。
_ITEM_FONT_SIZES = (9.0, 8.5, 8.0, 7.5)

# 表格单元格左右内边距（和 _table_commands 的 padding 用同一个值，
# 列宽计算要靠它换算「文字可用宽度」）。
_TABLE_PADDING = 2.0
_ITEM_PADDING = 1.5

_HEADER_BG = colors.HexColor("#EDEDED")
_LABEL_BG = colors.HexColor("#F7F7F7")
_EMPHASIS_BG = colors.HexColor("#E8E8E8")
_GRID = colors.HexColor("#9E9E9E")
_GRID_LIGHT = colors.HexColor("#C4C4C4")
_MUTED = colors.HexColor("#555555")

# 单元格里超过这个长度的文本会被截断（附省略号）。防的是「一行备注写了三千字」
# 把单个表格行撑得比整页还高——那种行 platypus 无法拆分，会直接打出界。
# 正常报价数据远不到这个长度，触发即说明上游数据有问题。
_MAX_CELL_CHARS = 200

# ---------------------------------------------------------------------------
# 列宽策略：**按真实字体度量算**，不靠硬编码权重
#
# 为什么不能用权重：权重是「比例」，跟字体度量没关系。9pt 下「序号」两个字要 18pt、
# 「1200」要 21.6pt，而按权重分下来只有 19.2pt（文字可用 16.2pt）—— 必然折行。
# 「1200」被折成「120」+「0」不是难看，是**让甲方读错数字**。
# 所以：量出每一列真正需要多宽，再决定怎么分。
# ---------------------------------------------------------------------------

# 这三列的文字折行属于正常排版，让它们去吸收挤压；其余列（序号/单位/数量/金额/
# 品牌/来源/子系统…）折行就是「读错数」或「一眼不专业」，必须优先满足。
_WRAPPABLE_HEADERS = frozenset({"设备/材料名称", "型号", "备注"})

# 可折行列的绝对下限（pt）：再挤也要留出能放下五六个汉字的宽度。
_WRAPPABLE_FLOORS: dict[str, float] = {
    "设备/材料名称": 52.0,
    "型号": 44.0,
    "备注": 38.0,
}

# 按列上限（2026-10-05 用户反馈 ✓）：**比例上限 0.30 对"设备参数/备注"太宽容** ✗ ——
#   参数一长就把整页宽度吃掉 ✓，害得名称/品牌/型号/价格竖排 ✓（客户看着难受 ✓）。
#   这里给具体列单独设上限（占可用宽度的比例 ✓），其余仍走 _WRAPPABLE_MAX_RATIO ✓。
#   原则：**多行无所谓的列（参数/备注）让位，需要一眼读到的列（名称/单价）优先** ✓。
_WRAPPABLE_CAPS: dict[str, float] = {
    "设备参数": 0.12,
    "备注": 0.12,
    "设备/材料名称": 0.26,
}


# 空间充足时单列可折行列最多占可用宽度的 30%，免得一列吃掉整张表。
_WRAPPABLE_MAX_RATIO = 0.30

# 每列额外留 0.5pt 余量。platypus 折行时是逐字符累加宽度再和可用宽度比大小，
# 累加出来的浮点误差会让「正好一样宽」的单元格被折成两行（实测：7 个汉字 9pt
# 累加成 63.00000000000001 > 63.0，于是「视频监控子系统」被拆掉最后一个字）。
_WIDTH_SLACK = 0.5

# 极端情况（不可折行列自己就把整页占满）下留给可折行列的份额，防止出现 0 宽列。
_SQUEEZE_TEXT_SHARE = 0.15

# 信息块：两列网格 = 4 个物理列（标签 / 值 / 标签 / 值），标签列按实测文字宽度给。
_INFO_LABEL_PADDING = 3.0

_CENTER_HEADERS = frozenset({"序号", "单位", "数量", "点位数", "版本"})
_RIGHT_HEADERS = frozenset(
    {"单价", "合计", "小计", "进价", "成本小计", "毛利", "设备费", "安装费", "辅材费"}
)


# ---------------------------------------------------------------------------
# 样式
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _CellStyles:
    """同一字号下三种对齐方式的单元格样式（表格里一律用 Paragraph 承载文本，
    这样长设备名会自动换行，不会把表格撑出页面）。"""

    left: ParagraphStyle
    right: ParagraphStyle
    center: ParagraphStyle
    header: ParagraphStyle


@dataclass(frozen=True)
class _Theme:
    fonts: _FontSet
    title: ParagraphStyle
    subtitle: ParagraphStyle
    heading: ParagraphStyle
    label: ParagraphStyle
    value: ParagraphStyle
    note: ParagraphStyle
    empty: ParagraphStyle
    fee: _CellStyles
    fee_bold: _CellStyles
    subsystem: _CellStyles
    item: _CellStyles
    item_bold: _CellStyles


def _cell_styles(fonts: _FontSet, size: float, *, bold: bool) -> _CellStyles:
    name = fonts.bold if bold else fonts.base
    leading = size * 1.3
    common: dict[str, Any] = {
        "fontName": name,
        "fontSize": size,
        "leading": leading,
        "textColor": colors.black,
    }
    return _CellStyles(
        left=ParagraphStyle("cell-left", alignment=TA_LEFT, **common),
        right=ParagraphStyle("cell-right", alignment=TA_RIGHT, **common),
        center=ParagraphStyle("cell-center", alignment=TA_CENTER, **common),
        # 表头始终用粗体名（没有独立粗体时它就是常规字体，不会报错）。
        header=ParagraphStyle(
            "cell-header",
            fontName=fonts.bold,
            fontSize=size,
            leading=leading,
            alignment=TA_CENTER,
            textColor=colors.black,
        ),
    )


def _make_theme(fonts: _FontSet, *, item_size: float) -> _Theme:
    return _Theme(
        fonts=fonts,
        title=ParagraphStyle(
            "quote-title",
            fontName=fonts.bold,
            fontSize=_TITLE_SIZE,
            leading=_TITLE_SIZE * 1.3,
            alignment=TA_CENTER,
            spaceAfter=2,
        ),
        subtitle=ParagraphStyle(
            "quote-subtitle",
            fontName=fonts.base,
            fontSize=_SUBTITLE_SIZE,
            leading=_SUBTITLE_SIZE * 1.5,
            alignment=TA_CENTER,
            textColor=_MUTED,
        ),
        heading=ParagraphStyle(
            "section-heading",
            fontName=fonts.bold,
            fontSize=_HEADING_SIZE,
            leading=_HEADING_SIZE * 1.4,
            spaceBefore=9,
            spaceAfter=3,
        ),
        label=ParagraphStyle(
            "info-label",
            fontName=fonts.bold,
            fontSize=_BODY_SIZE,
            leading=_BODY_SIZE * 1.35,
        ),
        value=ParagraphStyle(
            "info-value",
            fontName=fonts.base,
            fontSize=_BODY_SIZE,
            leading=_BODY_SIZE * 1.35,
        ),
        note=ParagraphStyle(
            "notes",
            fontName=fonts.base,
            fontSize=_BODY_SIZE,
            leading=_BODY_SIZE * 1.5,
        ),
        empty=ParagraphStyle(
            "empty-hint",
            fontName=fonts.base,
            fontSize=_TABLE_SIZE,
            leading=_TABLE_SIZE * 1.5,
            textColor=_MUTED,
        ),
        fee=_cell_styles(fonts, _TABLE_SIZE, bold=False),
        fee_bold=_cell_styles(fonts, _TABLE_SIZE, bold=True),
        subsystem=_cell_styles(fonts, _TABLE_SIZE, bold=False),
        item=_cell_styles(fonts, item_size, bold=False),
        item_bold=_cell_styles(fonts, item_size, bold=True),
    )


# ---------------------------------------------------------------------------
# 表格小工具
# ---------------------------------------------------------------------------

def _esc(value: object) -> str:
    """任意值 → Paragraph 安全的文本（转义 &<>，换行变 <br/>）。"""
    text = "" if value is None else str(value)
    text = escape(text)
    for token in ("\r\n", "\n", "\r"):
        text = text.replace(token, "<br/>")
    return text


def _clip(text: str, limit: int = _MAX_CELL_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _cell(value: object, style: ParagraphStyle) -> Paragraph:
    return Paragraph(_esc(_clip("" if value is None else str(value))), style)


def _fit(cells: Sequence[str], ncols: int) -> list[str]:
    """把一行单元格对齐到表头列数：多了截断，少了补空。

    正常情况两者一定相等；这里是防上游契约漂移导致 Table 直接抛异常。
    """
    out = ["" if c is None else str(c) for c in cells]
    if len(out) > ncols:
        return out[:ncols]
    return out + [""] * (ncols - len(out))


def _text_width(value: object, font_name: str, size: float) -> float:
    """单元格文本在给定字体/字号下要占多宽（多行文本按最长一行算）。

    必须用**当前实际注册的字体**去量（Windows 的 msyh 和服务器上的 wqy-zenhei
    度量不同），写死某个字体的度量就会「本机好看、服务器折行」。
    """
    text = _clip("" if value is None else str(value))
    lines = text.splitlines() or [""]
    return max(pdfmetrics.stringWidth(escape(line), font_name, size) for line in lines)


@dataclass(frozen=True)
class _ColumnNeeds:
    """一张表的列宽需求。`natural` 是「一行放下最长内容」所需的宽度。"""

    natural: list[float]           # 含左右 padding
    wrappable: list[bool]
    floors: list[float]            # 可折行列的下限（不可折行列记 0）
    hard_sum: float                # 所有不可折行列的 natural 之和

    @property
    def floor_sum(self) -> float:
        return sum(self.floors)


def _column_needs(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    header_font: str,
    cell_font: str,
    size: float,
    padding: float,
) -> _ColumnNeeds:
    """量出每一列真正需要多宽（表头与单元格取最大值）。

    `padding` 是**单侧**内边距，列的期望宽度 = 文字宽度 + 2×padding。
    """
    natural: list[float] = []
    wrappable: list[bool] = []
    floors: list[float] = []
    for index, header in enumerate(headers):
        need = _text_width(header, header_font, size) + 2 * padding + _WIDTH_SLACK
        for row in rows:
            if index < len(row):
                need = max(
                    need, _text_width(row[index], cell_font, size) + 2 * padding + _WIDTH_SLACK
                )
        natural.append(need)
        is_wrappable = header.strip() in _WRAPPABLE_HEADERS
        wrappable.append(is_wrappable)
        if is_wrappable:
            # 下限不低于自己的表头宽度：表头折行是「一眼就不专业」的那类问题。
            header_width = _text_width(header, header_font, size) + 2 * padding + _WIDTH_SLACK
            floor = max(_WRAPPABLE_FLOORS.get(header.strip(), 0.0), header_width)
            floors.append(min(need, floor))
        else:
            floors.append(0.0)
    hard_sum = sum(n for n, wrap in zip(natural, wrappable, strict=True) if not wrap)
    return _ColumnNeeds(natural=natural, wrappable=wrappable, floors=floors, hard_sum=hard_sum)


def _normalize(widths: Sequence[float], available: float) -> list[float]:
    """等比缩放并让末列吃掉浮点误差，保证列宽之和**正好**等于可用宽度。"""
    total = sum(widths)
    if total <= 0:
        return [available / len(widths)] * len(widths)
    scaled = [w * available / total for w in widths]
    scaled[-1] = available - sum(scaled[:-1])
    return scaled


def _fit_widths(
    headers: Sequence[str],
    rows: Sequence[Sequence[str]],
    *,
    header_font: str,
    cell_font: str,
    size: float,
    padding: float,
) -> list[float]:
    """按内容分配列宽，优先级从高到低：

    1. **不可折行列拿到自己需要的全部宽度** —— 金额、数量折行会让甲方读错数；
    2. 富余宽度按期望比例分给可折行列（名称/型号/备注），单列上限 30% 可用宽度；
    3. 放不下时先把可折行列按各自下限等比压缩 —— 它们折行只是难看，不会读错；
    4. 连不可折行列都放不下（极端长金额）才整体等比缩放，这是最后的保命手段。
    """
    needs = _column_needs(
        headers, rows, header_font=header_font, cell_font=cell_font, size=size, padding=padding
    )
    hard = [0.0 if w else n for n, w in zip(needs.natural, needs.wrappable, strict=True)]
    wrap_index = [i for i, w in enumerate(needs.wrappable) if w]

    if needs.hard_sum >= _USABLE_WIDTH:
        # 极端情况：不可折行列本身就把整页占满。只能整体压缩，但绝不能把某一列
        # 压成 0 宽（Table 会画出一堆重叠的文字），所以先给文字列留一小块。
        budget = min(_USABLE_WIDTH * _SQUEEZE_TEXT_SHARE, needs.floor_sum) or 0.0
        if wrap_index and needs.floor_sum > 0:
            widths = _normalize(hard, _USABLE_WIDTH - budget)
            for i in wrap_index:
                widths[i] = budget * needs.floors[i] / needs.floor_sum
        else:
            widths = _normalize(hard, _USABLE_WIDTH)
        return _normalize(widths, _USABLE_WIDTH)

    if not wrap_index:
        # 没有可折行列（费用表/子系统表）：余量按各自需求等比放大，比例不变。
        return _normalize(hard, _USABLE_WIDTH)

    def _cap(header: str) -> float:
        # 具体列优先 ✓（设备参数/备注收紧、名称给足 ✓），其余走默认 30% ✓
        return _USABLE_WIDTH * _WRAPPABLE_CAPS.get(header.strip(), _WRAPPABLE_MAX_RATIO)

    wants = [
        min(n, _caps) if w else 0.0
        for n, w, _caps in zip(
            needs.natural,
            needs.wrappable,
            (_cap(h) for h in headers),
            strict=True,
        )
    ]
    rest = _USABLE_WIDTH - needs.hard_sum
    want_sum = sum(wants[i] for i in wrap_index)
    floor_sum = sum(needs.floors[i] for i in wrap_index)
    if want_sum > 0 and rest >= want_sum:
        base, factor = wants, rest / want_sum
    elif floor_sum > 0:
        base, factor = needs.floors, rest / floor_sum
    else:  # pragma: no cover - 只有表头文字为空才会走到
        base, factor = wants, rest / len(wrap_index)

    widths = [base[i] * factor if wrap else hard[i]
              for i, wrap in enumerate(needs.wrappable)]
    return _normalize(widths, _USABLE_WIDTH)


def _pick_item_font_size(ctx: ExportContext, fonts: _FontSet) -> float:
    """选一个「所有不可折行列都放得下」的最大字号。

    列数多（含成本 14 列）或金额位数长时自动收档，而不是靠列数硬编码。
    """
    headers = ctx.item_headers()
    rows = [_fit(ctx.item_row_cells(row), len(headers)) for row in ctx.items]
    for size in _ITEM_FONT_SIZES:
        needs = _column_needs(
            headers, rows, header_font=fonts.bold, cell_font=fonts.base,
            size=size, padding=_ITEM_PADDING,
        )
        if needs.hard_sum + needs.floor_sum <= _USABLE_WIDTH:
            return size
    return _ITEM_FONT_SIZES[-1]


def _theme_for(ctx: ExportContext, fonts: _FontSet) -> _Theme:
    return _make_theme(fonts, item_size=_pick_item_font_size(ctx, fonts))


def _item_table(
    ctx: ExportContext, theme: _Theme
) -> tuple[list[str], list[list[str]], list[float]]:
    """明细表的 `(表头, 单元格文本, 列宽)` —— 渲染和测试共用同一份计算。"""
    headers = ctx.item_headers()
    rows = [_fit(ctx.item_row_cells(row), len(headers)) for row in ctx.items]
    widths = _fit_widths(
        headers, rows, header_font=theme.fonts.bold, cell_font=theme.fonts.base,
        size=theme.item.left.fontSize, padding=_ITEM_PADDING,
    )
    return headers, rows, widths


def _align_for(header: str) -> str:
    key = header.strip()
    if key in _CENTER_HEADERS:
        return "center"
    if key in _RIGHT_HEADERS:
        return "right"
    return "left"


def _pick(styles: _CellStyles, align: str) -> ParagraphStyle:
    if align == "right":
        return styles.right
    if align == "center":
        return styles.center
    return styles.left


def _table_commands(
    *,
    header_rows: int = 1,
    emphasis_rows: Sequence[int] = (),
    separator_rows: Sequence[int] = (),
    padding: float = _TABLE_PADDING,
    grid_color: colors.Color = _GRID,
) -> list[tuple[Any, ...]]:
    """所有表格共用的网格/内边距/表头底色。

    emphasis 行只有在**没有独立粗体**时才补浅灰底，否则加粗本身就够醒目了。
    """
    commands: list[tuple[Any, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.4, grid_color),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), padding),
        ("TOPPADDING", (0, 0), (-1, -1), 1.5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ]
    if header_rows > 0:
        commands += [
            ("BACKGROUND", (0, 0), (-1, header_rows - 1), _HEADER_BG),
            ("LINEBELOW", (0, header_rows - 1), (-1, header_rows - 1), 0.6, _GRID),
        ]
    for row in separator_rows:
        commands.append(("LINEABOVE", (0, row), (-1, row), 0.7, colors.black))
    return commands


def _section(title: str, theme: _Theme) -> list[Any]:
    return [Paragraph(_esc(title), theme.heading)]


# ---------------------------------------------------------------------------
# 各区块
# ---------------------------------------------------------------------------

def _title_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    meta = ctx.meta
    parts: list[Any] = []
    # 公司抬头（来自「系统设置」）—— 客户拿到的第一眼就该是公司名
    if meta.company_name:
        parts.append(Paragraph(_esc(meta.company_name), theme.subtitle))
        contact = "　".join(
            x for x in (meta.company_address, meta.company_phone, meta.company_email) if x
        )
        if contact:
            parts.append(Paragraph(_esc(contact), theme.note))
    parts.append(Paragraph(_esc(meta.title or "报价单"), theme.title))
    bits = [bit for bit in (meta.quote_code, f"V{meta.version}" if meta.version else "",
                            meta.status_label) if bit]
    if bits:
        parts.append(Paragraph(_esc(" · ".join(bits)), theme.subtitle))
    parts.append(Spacer(1, 4 * mm))
    return parts


def _info_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    """报价单/项目/客户信息块 —— 两列网格（每行两组「标签 + 值」）。"""
    meta = ctx.meta
    pairs: list[tuple[str, str, str, str]] = [
        ("报价单号", meta.quote_code, "版本", f"V{meta.version}"),
        ("项目编号", meta.project_code, "项目名称", meta.project_name),
        ("客户名称", meta.customer_name, "联系人", meta.customer_contact),
        ("联系电话", meta.customer_phone, "工程地址", meta.address),
        ("计税方式", meta.tax_method_label, "税率", meta.tax_rate_pct),
        ("报价日期", meta.quote_date, "有效期至", meta.valid_until),
        (
            "报价口径",
            # 客户版上不写「不含成本」—— 客户不该在报价单上看到"成本"两个字
            "含成本（内部版）" if meta.include_cost else "客户版",
            "导出时间",
            meta.generated_at,
        ),
    ]
    rows = [
        [
            _cell(label_a, theme.label),
            _cell(value_a, theme.value),
            _cell(label_b, theme.label),
            _cell(value_b, theme.value),
        ]
        for label_a, value_a, label_b, value_b in pairs
    ]
    commands = _table_commands(header_rows=0, padding=3.0, grid_color=_GRID_LIGHT)
    for row_index in range(len(rows)):
        commands.append(("BACKGROUND", (0, row_index), (0, row_index), _LABEL_BG))
        commands.append(("BACKGROUND", (2, row_index), (2, row_index), _LABEL_BG))

    table = Table(rows, colWidths=_info_widths(pairs, theme), hAlign="LEFT")
    table.setStyle(TableStyle(commands))
    return [table]


def _info_widths(pairs: Sequence[tuple[str, str, str, str]], theme: _Theme) -> list[float]:
    """信息块列宽：两个标签列按实测最宽标签给，余下两个值列平分。"""
    labels = [label for row in pairs for label in (row[0], row[2])]
    label_width = max(
        _text_width(label, theme.fonts.bold, _BODY_SIZE) for label in labels
    ) + 2 * _INFO_LABEL_PADDING
    value_width = (_USABLE_WIDTH - 2 * label_width) / 2
    return [label_width, value_width, label_width, value_width]


def _fee_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    """费用汇总：两种计价口径并列。空串留空，绝不补 0。"""
    flow: list[Any] = _section("费用汇总", theme)
    headers = ctx.fee_headers()
    if not ctx.fees:
        flow.append(Paragraph("（本报价单暂无费用数据）", theme.empty))
        return flow

    ncols = len(headers)
    rows: list[list[Paragraph]] = [
        [_cell(header, theme.fee.header) for header in headers]
    ]
    plain: list[list[str]] = [[str(header) for header in headers]]
    emphasis_rows: list[int] = []
    separator_rows: list[int] = []
    for index, fee in enumerate(ctx.fees, start=1):
        cells = _fit([fee.label, fee.amount_point, fee.amount_quota], ncols)
        plain.append(list(cells))
        styles = theme.fee_bold if fee.emphasis else theme.fee
        # 第一列是费用项名称（左对齐），其余列都是金额（右对齐）。
        rows.append(
            [_cell(text, styles.left if col == 0 else styles.right)
             for col, text in enumerate(cells)]
        )
        if fee.separator:
            separator_rows.append(index)
        if fee.emphasis and not theme.fonts.has_bold:
            emphasis_rows.append(index)

    commands = _table_commands(separator_rows=separator_rows)
    for row in emphasis_rows:
        commands.append(("BACKGROUND", (0, row), (-1, row), _EMPHASIS_BG))

    widths = _fit_widths(
        headers, plain, header_font=theme.fonts.bold, cell_font=theme.fonts.base,
        size=_TABLE_SIZE, padding=_TABLE_PADDING,
    )
    table = Table(rows, colWidths=widths, hAlign="LEFT")
    table.setStyle(TableStyle(commands))
    flow.append(table)
    return flow


def _subsystem_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    flow: list[Any] = _section("子系统汇总", theme)
    headers = ctx.subsystem_headers()
    if not ctx.subsystems:
        flow.append(Paragraph("（本报价单暂无子系统）", theme.empty))
        return flow

    rows: list[list[Paragraph]] = [
        [_cell(header, theme.subsystem.header) for header in headers]
    ]
    plain: list[list[str]] = [[str(header) for header in headers]]
    for row in ctx.subsystems:
        cells = _fit(ctx.subsystem_row_cells(row), len(headers))
        plain.append(list(cells))
        rows.append([
            _cell(text, _pick(theme.subsystem, _align_for(header)))
            for header, text in zip(headers, cells, strict=True)
        ])

    widths = _fit_widths(
        headers, plain, header_font=theme.fonts.bold, cell_font=theme.fonts.base,
        size=_TABLE_SIZE, padding=_TABLE_PADDING,
    )
    table = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle(_table_commands()))
    flow.append(table)
    return flow


def _item_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    """报价明细。列数随 include_cost 变化，表头跨页重复（repeatRows=1）。

    列宽由 `_item_table` 按真实字体度量算出来：金额/数量列绝不折行。
    """
    flow: list[Any] = _section("报价明细", theme)
    if not ctx.items:
        flow.append(Paragraph("（本报价单暂无明细）", theme.empty))
        return flow

    headers, plain, widths = _item_table(ctx, theme)
    rows: list[list[Paragraph]] = [[_cell(header, theme.item.header) for header in headers]]
    for cells in plain:
        rows.append([
            _cell(text, _pick(theme.item, _align_for(header)))
            for header, text in zip(headers, cells, strict=True)
        ])

    table = Table(rows, colWidths=widths, hAlign="LEFT", repeatRows=1)
    table.setStyle(TableStyle(_table_commands(padding=_ITEM_PADDING)))
    flow.append(table)
    return flow


def _notes_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    notes = (ctx.meta.notes or "").strip()
    if not notes:
        return []
    return _section("备注", theme) + [Paragraph(_esc(notes), theme.note)]


def _clauses_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    """报价说明（原型第 3 页的 6 条）。

    条款文案的单一出处在 `export_data.QUOTE_CLAUSES`；前端的客户版预览页也读同一份
    （走 `GET /quotes/{id}/export-clauses`），避免"预览有条款、导出没有"。

    ⚠ 兜底那份常量里是**变量占位符**（`{valid_days}` 等），必须渲染后再印 ——
    否则任何没走 `build_context` 的调用（单测、将来的新调用点）会把
    `{valid_days}` 原样印到给客户的 PDF 上。
    """
    clauses = ctx.meta.clauses or render_clauses(QUOTE_CLAUSES, build_variables({}))
    if not clauses:
        return []
    # 用比备注更紧的行距：条数固定 6 条 + 签章区，压紧后才能让"小单据"仍然一页装下
    compact = ParagraphStyle("clause", parent=theme.note, leading=12.5, spaceAfter=0)
    flow: list[Any] = [Spacer(1, 6), *_section("报价说明", theme)]
    for i, text in enumerate(clauses, start=1):
        flow.append(Paragraph(f"{i}. {_esc(text)}", compact))
    return flow


def _sign_flowables(ctx: ExportContext, theme: _Theme) -> list[Any]:
    """签章区：报价单位（盖章）/ 授权代表（签字）/ 日期（原型第 3 页底部）。

    原型把它压在一行里（左侧盖章、右侧日期），这里照着压 —— 三行分开写会把
    小单据顶到第 2 页。
    """
    compact = ParagraphStyle("sign", parent=theme.note, leading=13, spaceAfter=0)
    return [
        Spacer(1, 14),
        Paragraph("报价单位（盖章）：________________", compact),
        Spacer(1, 12),
        Paragraph(
            "授权代表（签字）：________________　　　日期：______ 年 ____ 月 ____ 日",
            compact,
        ),
    ]


def _story(ctx: ExportContext, theme: _Theme) -> list[Any]:
    """按文档顺序拼出整条 story。

    注意：platypus 在 build 过程中会改 flowable 对象，所以**每一遍都必须
    重新调用本函数**，两遍不能复用同一批 flowable。
    """
    flow: list[Any] = []
    flow += _title_flowables(ctx, theme)
    flow += _info_flowables(ctx, theme)
    flow += _fee_flowables(ctx, theme)
    flow += _subsystem_flowables(ctx, theme)
    flow += _item_flowables(ctx, theme)
    flow += _notes_flowables(ctx, theme)
    flow += _clauses_flowables(ctx, theme)
    flow += _sign_flowables(ctx, theme)
    return flow


# ---------------------------------------------------------------------------
# 文档构建
# ---------------------------------------------------------------------------

def _make_doc(buffer: BytesIO, ctx: ExportContext) -> SimpleDocTemplate:
    doc = SimpleDocTemplate(
        buffer,
        pagesize=_PAGE_SIZE,   # A4 横向 ✓（与客户版预览的纸张一致 ✓）
        leftMargin=_PAGE_MARGIN,
        rightMargin=_PAGE_MARGIN,
        topMargin=_PAGE_MARGIN,
        bottomMargin=_PAGE_MARGIN,
    )
    doc.title = ctx.meta.title or f"报价单 {ctx.meta.quote_code}".strip()
    doc.subject = ctx.meta.quote_code
    doc.creator = ctx.meta.company_name or "报价系统"
    return doc


def _draw_footer(
    canvas: Any,
    doc: Any,
    *,
    ctx: ExportContext,
    fonts: _FontSet,
    total_pages: int,
) -> None:
    """页脚：左「报价单号」，右「第 X 页 / 共 Y 页」。

    画在 15mm 下边距以内，不占版面，所以不会改变两遍之间的分页结果。
    """
    canvas.saveState()
    canvas.setStrokeColor(_GRID_LIGHT)
    canvas.setLineWidth(0.4)
    canvas.line(doc.leftMargin, _FOOTER_Y + 11, _PAGE_SIZE[0] - doc.rightMargin, _FOOTER_Y + 11)
    canvas.setFont(fonts.base, _FOOTER_SIZE)
    canvas.setFillColor(_MUTED)
    company = ctx.meta.company_name or ""
    left = f"{company} · 报价单 {ctx.meta.quote_code}".strip(" ·")
    canvas.drawString(doc.leftMargin, _FOOTER_Y, left)
    canvas.drawRightString(
        _PAGE_SIZE[0] - doc.rightMargin,
        _FOOTER_Y,
        f"第 {canvas.getPageNumber()} 页 / 共 {total_pages} 页",
    )
    canvas.restoreState()


def _count_pages(ctx: ExportContext, theme: _Theme) -> int:
    """第一遍：空跑一次排版，只为了知道总页数。"""
    seen = {"pages": 0}

    def _note_page(canvas: Any, _doc: Any) -> None:
        seen["pages"] = canvas.getPageNumber()

    buffer = BytesIO()
    _make_doc(buffer, ctx).build(
        _story(ctx, theme),
        onFirstPage=_note_page,
        onLaterPages=_note_page,
    )
    return seen["pages"]


def _build_pdf(ctx: ExportContext, theme: _Theme, total_pages: int) -> bytes:
    """第二遍：把总页数画进页脚，返回最终字节。"""
    buffer = BytesIO()

    def _on_page(canvas: Any, doc: Any) -> None:
        _draw_footer(canvas, doc, ctx=ctx, fonts=theme.fonts, total_pages=total_pages)

    _make_doc(buffer, ctx).build(
        _story(ctx, theme),
        onFirstPage=_on_page,
        onLaterPages=_on_page,
    )
    return buffer.getvalue()


def render(ctx: ExportContext) -> bytes:
    """把 `ExportContext` 渲染成完整的 PDF 字节流。

    纯函数：不打印、不写文件、不读环境变量以外的全局状态（`QUOTE_EXPORT_FONT`
    用于定位中文字体）。字体解析失败时抛 `RuntimeError`，不会退化成 Helvetica。
    """
    fonts = _prepare_fonts()
    theme = _theme_for(ctx, fonts)
    total_pages = _count_pages(ctx, theme)
    return _build_pdf(ctx, theme, total_pages)





