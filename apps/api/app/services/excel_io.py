"""Excel parsing helpers — 列名同义词识别 + 无表头容错.

Column synonym dictionary: the customer's Excel rarely matches our field names.
We accept 规格 / 型号 / Model / 产品型号 → model, etc.
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import load_workbook

# ---------------------------------------------------------------------------
# Column synonym tables
# ---------------------------------------------------------------------------

# 场景一：自有设备库
# 「分类路径」支持多级：写「视频监控/摄像机」会落到分类树的对应节点上；
# 只写「视频监控」或英文代号 video 也能识别（分类解析见 api/v1/imports.py）。
DEVICE_TEMPLATE_COLUMNS = [
    "设备名称", "品牌", "规格型号", "分类路径", "计量单位",
    "参考单价", "进价", "安装单价", "设备描述", "供应商", "起订量",
    "质保年限", "是否甲供", "设备状态", "替代型号",
]

# 场景二：甲方询价清单（通常只有名称和数量）
INQUIRY_TEMPLATE_COLUMNS = ["设备名称", "规格型号", "品牌", "数量", "单位", "备注"]

_SYNONYMS: dict[str, set[str]] = {
    "name": {
        "设备名称", "产品名称", "名称", "品名", "货物名称", "材料名称",
        "设备", "产品", "name", "product", "item",
    },
    "brand": {"品牌", "厂牌", "厂商", "生产厂家", "厂家", "brand", "manufacturer", "vendor"},
    "model": {
        "规格型号", "型号", "规格", "产品型号", "设备型号", "model", "型号规格",
        "modelno", "规格及型号",
    },
    # "spec" 的"说明"与 notes 的"说明"以前同抢一个别名（审计 M-7），这里只留一份
    "spec": {"技术参数", "参数", "描述", "spec", "description"},
    # 分类只保留一个字段名：以前 "type"/"说明"/"vendor" 被两个字段同时声明，
    # 反向索引互相覆盖 → 英文表头会映射到错字段。现在统一走 category_path，
    # 单级名（"视频监控"）与多级路径（"视频监控/摄像机"）都由解析器处理。
    "category_path": {
        "分类路径", "所属分类", "分类", "类别", "子系统", "系统", "小类", "子类",
        "category", "categorypath", "type", "system",
    },
    "unit": {"计量单位", "单位", "unit", "uom"},
    "reference_price": {
        "参考单价", "单价", "设备单价", "报价", "价格", "含税单价", "参考价",
        "京东控价", "控价", "销售价", "price", "unitprice", "单价(元)",
    },
    "quantity": {"数量", "工程量", "qty", "quantity", "数目"},
    "supplier": {"供应商", "供货商", "厂商名称", "supplier"},
    "moq": {"起订量", "最小起订量", "moq", "最小订货量"},
    "warranty_years": {"质保年限", "质保", "保修期", "保修年限", "warranty"},
    "is_owner_supplied": {"是否甲供", "甲供", "甲供材", "owner_supplied"},
    "status": {"设备状态", "状态", "销售状态", "status"},
    "replacement_model": {"替代型号", "替换型号", "替代品", "replacement"},
    "notes": {"备注", "note", "remark", "说明", "保卡类型"},
    "code": {"设备编号", "编号", "编码", "code", "物料编码"},
    "install_unit_price": {
        "安装单价", "人工单价", "安装费", "安装工费", "install_price", "安装单价(元)",
    },
    # 进价/成本（2026-10-05）：以前模板没有这一列，导完只能逐个补 ✗
    "cost_price": {
        "进价", "成本价", "成本单价", "进价(元)", "成本", "采购价", "提货价",
        "工程提货价", "供货价", "cost", "costprice", "cost_price",
    },
    # 多行描述（性能参数）—— 与一行的 spec 分开：
    # 「描述」仍归 spec（不破坏老文件），「产品描述/性能参数」归 description
    "description": {
        "设备描述", "产品描述", "性能参数", "参数描述", "详细描述",
        "设备参数", "description",
    },
}

# 反向索引：归一化列名 → 字段名
_REVERSE = {
    alias.strip().lower().replace(" ", "").replace("（", "(").replace("）", ")"): field
    for field, aliases in _SYNONYMS.items()
    for alias in aliases
}


def _norm_header(value: Any) -> str:
    return str(value or "").strip().lower().replace(" ", "").replace("\u3000", "")


def _map_headers(header_row: list[Any]) -> dict[int, str]:
    """Map column index → system field name. Unmapped columns are skipped."""
    mapping: dict[int, str] = {}
    for idx, cell in enumerate(header_row):
        key = _norm_header(cell)
        if not key:
            continue
        if key in _REVERSE:
            mapping[idx] = _REVERSE[key]
            continue
        # 包含式匹配（"设备名称(必填)" → name）
        for alias, field in _REVERSE.items():
            if alias and alias in key:
                mapping[idx] = field
                break
    return mapping


def _find_header_row(rows: list[tuple[Any, ...]], scan: int = 5) -> int:
    """Some 询价单 have a title row above the header. Pick the row with most hits."""
    best_idx, best_hits = 0, -1
    for i, row in enumerate(rows[:scan]):
        hits = len(_map_headers(list(row)))
        if hits > best_hits:
            best_idx, best_hits = i, hits
    return best_idx


def _load_rows(content: bytes) -> list[tuple[Any, ...]]:
    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    ws = wb.active
    if ws is None:
        return []
    return [tuple(r) for r in ws.iter_rows(values_only=True)]


def _build_records(content: bytes) -> tuple[list[dict], int]:
    rows = _load_rows(content)
    if not rows:
        return [], 0

    header_idx = _find_header_row(rows)
    mapping = _map_headers(list(rows[header_idx]))
    if not mapping:
        raise ValueError("表头无法识别，请确认包含「设备名称」「规格型号」等列")

    records: list[dict] = []
    for row in rows[header_idx + 1:]:
        if row is None or all(c is None or str(c).strip() == "" for c in row):
            continue
        rec: dict[str, Any] = {}
        for idx, field in mapping.items():
            if idx < len(row):
                rec[field] = row[idx]
        if rec:
            records.append(rec)
    return records, len(rows) - header_idx - 1


def parse_device_sheet(content: bytes) -> tuple[list[dict], int]:
    """Scenario 1: enterprise device library import."""
    return _build_records(content)


def parse_inquiry_sheet(content: bytes) -> tuple[list[dict], int]:
    """Scenario 2: customer inquiry list (often name + qty only)."""
    return _build_records(content)


__all__ = [
    "DEVICE_TEMPLATE_COLUMNS",
    "INQUIRY_TEMPLATE_COLUMNS",
    "parse_device_sheet",
    "parse_inquiry_sheet",
    "describe_headers",
    "detect_columns",
]


def describe_headers(content: bytes) -> tuple[dict[int, str], list[str]]:
    """一次解析出「已识别的列」和「没认出来的表头文字」。

    preview 接口要把 unmapped 回给前端做提示（「未识别的列：xxx」）。
    以前只在 detect_columns 里算过、没有任何路由暴露它，
    前端拿到 undefined 后直接 .length，整个预览步骤崩掉。
    """
    rows = _load_rows(content)
    if not rows:
        return {}, []
    header_idx = _find_header_row(rows)
    mapping = _map_headers(list(rows[header_idx]))
    unmapped = [
        str(c)
        for i, c in enumerate(rows[header_idx])
        if i not in mapping and str(c or "").strip()
    ]
    return mapping, unmapped


def detect_columns(content: bytes) -> dict:
    """Return header mapping for the frontend's 字段映射 step."""
    rows = _load_rows(content)
    if not rows:
        return {"header_row": 0, "columns": [], "unmapped": []}
    header_idx = _find_header_row(rows)
    mapping, unmapped = describe_headers(content)
    return {
        "header_row": header_idx + 1,
        "columns": [
            {
                "excel_index": idx,
                "excel_header": str(rows[header_idx][idx]) if idx < len(rows[header_idx]) else "",
                "system_field": field,
                "sample": str(rows[header_idx + 1][idx])
                if header_idx + 1 < len(rows) and idx < len(rows[header_idx + 1])
                else "",
            }
            for idx, field in sorted(mapping.items())
        ],
        "unmapped": unmapped,
    }
