"""设备导入：源表列 → 系统字段的**词表与匹配规则**（2026-10-09）。

## 为什么要单独一个模块

设备库导入原先有三列是"看得见、用不上"的 ✗（列名在 `excel_io._SYNONYMS` 里
早就认出来了 ✓，但落库逻辑压根没读 ✗）：

  · **设备状态** —— 硬编码 `status=DeviceStatus.ON_SALE` ✗，源表写"停产"也照当在售 ✗；
  · **供应商** —— 完全不落 ✗（`devices.supplier_id` 一直空着 ✗）；
  · **替代型号** —— 完全不落 ✗（`devices.replacement_id` 一直空着 ✗）。

预览与落库**必须用同一套规则** ✓（否则又是"预览说会挂供应商、落库却没挂"✗ ——
这个坑在分类解析上已经踩过一次，见 services/device_category.py 的注释 ✓），
所以规则集中在这里，两边都调它 ✓。

## 三条口径（用户 2026-10-09 明确）

1. **设备状态**：源表词表与系统枚举不同 ✓ → 查表映射 ✓；
   **认不出的值一律按"在售"处理并报告出来** ✓（绝不静默 ✗ ——
   一台实际停产的设备被静默当在售，报价时就会拿它去配单 ✗）。
2. **供应商**：**只按名称匹配已有供应商** ✓；没有就**报告** ✗ 不新建 ✗
   （源表里"厂家/代理商"混着写是常态 ✗，自动新建会长出一堆近义重复的供应商 ✗）。
3. **替代型号**：设备表**有**对应字段（`devices.replacement_id` ✓，指向另一台设备 ✓）
   → 按型号匹配后落库 ✓；匹配不到就**报告并跳过** ✓
   （**不塞进描述** ✗ —— 描述是给客户看的技术参数，塞个型号进去是脏数据 ✗）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from app.models.device import DeviceStatus

# ---------------------------------------------------------------------------
# ① 设备状态词表
# ---------------------------------------------------------------------------
# 归一化：去空白（含全角空格）、转小写、去掉括号与其中的内容（"在售(现货)" → "在售"）。
# 键是**系统枚举值** ✓，值是源表里可能出现的写法 ✓（中英文都收 ✓）。
_STATUS_SYNONYMS: dict[str, tuple[str, ...]] = {
    DeviceStatus.ON_SALE: (
        "在售", "正常", "正常销售", "销售中", "在售中", "现货", "可售", "在产", "量产",
        "on_sale", "onsale", "on sale", "available", "active", "insale",
    ),
    DeviceStatus.NEAR_EOL: (
        "临近停产", "即将停产", "停产在售", "即将停售", "尾货", "清仓", "最后一批",
        "near_eol", "neareol", "near eol", "last buy",
    ),
    DeviceStatus.EOL: (
        "停产", "已停产", "停止生产", "停售", "已停售", "不再生产", "停产型号",
        "eol", "end of life", "endoflife", "obsolete",
    ),
    DeviceStatus.DISCONTINUED: (
        "退市", "已退市", "下架", "已下架", "淘汰", "停用",
        "discontinued", "discontinue", "phase out",
    ),
}

# 认不出具体写法时的**兜底关键词**规则（顺序即优先级 ✓）：
# 先判"停产但在售/临近停产"✓，再判"停产"✗ —— 顺序反了，"停产在售"会被判成 eol ✗。
_STATUS_SUBSTRING_RULES: tuple[tuple[tuple[str, ...], DeviceStatus], ...] = (
    (("退市", "下架", "淘汰"), DeviceStatus.DISCONTINUED),
    (("临", "即将", "快"), DeviceStatus.NEAR_EOL),
    (("停产", "停售", "停止生产", "不再生产"), DeviceStatus.EOL),
    (("在售", "销售中", "现货", "正常"), DeviceStatus.ON_SALE),
)

# 默认值：源表这一列**空着**或**认不出来**时的取值 ✓（在售 ✓ —— 导入进来的
# 目录绝大多数是在售品 ✓，而"停产"这种少数派必须自己去标 ✓）
DEFAULT_DEVICE_STATUS = DeviceStatus.ON_SALE


def normalize_token(raw: Any) -> str:
    """归一化一个词：去空白 / 全角空格 / 括号内容，转小写。

    例：「在售（现货）」→「在售」✓；「On Sale」→「on sale」✓；「 EOL 」→「eol」✓。
    """
    if raw is None:
        return ""
    text = str(raw).strip().lower().replace("\u3000", "").replace(" ", "")
    # 去掉中英文括号及其内容："在售(现货)" / "在售（现货）"
    for left, right in (("(", ")"), ("（", "）"), ("[", "]")):
        while left in text and right in text:
            start = text.index(left)
            end = text.index(right, start)
            text = text[:start] + text[end + 1 :]
    return text.strip()


def map_device_status(raw: Any) -> tuple[DeviceStatus, bool]:
    """源表的「设备状态」→ 系统枚举 + **是否识别出来** ✓。

    返回 `(status, recognized)`：
      · 认出来 → `(映射值, True)` ✓；
      · 空值 → `(在售, False)` ✓（没说 ≠ 说错了，报告里单独算"未填" ✓）；
      · 认不出 → `(在售, False)` ✓ **必须报告** ✗ 不许静默 ✗。

    调用方拿 `recognized` 去做统计 ✓（见 api/v1/imports.py 的 status_report）。
    """
    token = normalize_token(raw)
    if not token:
        return DEFAULT_DEVICE_STATUS, False
    for status, synonyms in _STATUS_SYNONYMS.items():
        if token in (normalize_token(s) for s in synonyms):
            return status, True
    for keywords, status in _STATUS_SUBSTRING_RULES:
        if any(k in token for k in keywords):
            return status, True
    return DEFAULT_DEVICE_STATUS, False


def status_label(value: DeviceStatus | str) -> str:
    """系统枚举 → 中文（导入报告里给人看 ✓）。"""
    return {
        str(DeviceStatus.ON_SALE): "在售",
        str(DeviceStatus.NEAR_EOL): "临近停产",
        str(DeviceStatus.EOL): "已停产",
        str(DeviceStatus.DISCONTINUED): "退市",
    }.get(str(value), str(value))


# ---------------------------------------------------------------------------
# ② / ③ 名称与型号匹配
# ---------------------------------------------------------------------------

def normalize_supplier_name(raw: Any) -> str:
    """供应商名的匹配键：去空白、去括号内容、**不区分大小写** ✓。

    ⚠ 刻意**不做**模糊匹配 ✗（"宁波XX安防" 与 "宁波XX安防科技" 是两家还是一家的两种写法，
    猜错了就把设备的采购来源挂到别人家 ✗）。宁可不挂 + 报告，让人自己决定 ✓。
    """
    return normalize_token(raw)


def match_supplier(raw: Any, suppliers: Iterable[Any]) -> Any | None:
    """按名称匹配**在用的**供应商；没有就返回 None ✓（**绝不新建** ✗）。

    停用的同名供应商不算命中 ✓ —— 挂上去等于让设备的采购来源指向一个已经停用的户头 ✗，
    这种情况由调用方报告为"同名供应商已停用" ✓。
    """
    key = normalize_supplier_name(raw)
    if not key:
        return None
    for s in suppliers:
        if normalize_supplier_name(getattr(s, "name", "")) == key and bool(
            getattr(s, "is_active", True)
        ):
            return s
    return None


def find_inactive_supplier(raw: Any, suppliers: Iterable[Any]) -> Any | None:
    """同名但**已停用**的供应商（用来把报告说清楚 ✓：「同名供应商已停用」✓）。"""
    key = normalize_supplier_name(raw)
    if not key:
        return None
    for s in suppliers:
        if normalize_supplier_name(getattr(s, "name", "")) == key and not bool(
            getattr(s, "is_active", True)
        ):
            return s
    return None


def normalize_model(raw: Any) -> str:
    """型号的匹配键：去空白、**大写** ✓（型号大小写不敏感是行业惯例 ✓）。"""
    return str(raw or "").strip().upper()


def match_replacement(raw: Any, devices: Iterable[Any], *, exclude_id: Any = None) -> Any | None:
    """按型号匹配"替代型号"指向的那台设备 ✓。

    · 匹配不到 → None ✓（调用方**报告并跳过** ✓，不塞进描述 ✗）；
    · 匹配到自己（源表把本型号写进了替代型号列）→ None ✓
      （`replacement_id` 指向自己是环 ✗，接口层也拒绝 ✓）。

    ⚠ 同一文件内的**前向引用**（A 的替代型号写在 A 后面的那一行）匹配不到 ✓：
    解析是逐行的，此刻那台设备还没建 ✓。这种情况会如实进报告 ✓，不会静默丢 ✗。
    """
    key = normalize_model(raw)
    if not key:
        return None
    for d in devices:
        if normalize_model(getattr(d, "model", "")) == key:
            if exclude_id is not None and getattr(d, "id", None) == exclude_id:
                return None
            return d
    return None


def device_is_usable(device: Any) -> bool:
    """替代型号得是个**能选的**设备：停用的设备不能当替代品 ✓。"""
    return bool(getattr(device, "is_active", True))


# ---------------------------------------------------------------------------
# 导入报告（预览与落库共用）
# ---------------------------------------------------------------------------

@dataclass
class ColumnMappingReport:
    """三列的映射结果统计 —— **摆在明面上**，不静默 ✗。

    `to_dict()` 的形状直接进接口返回 / 导入批次 summary ✓：
      预览：`{... "status_report": {...}}` ✓
      落库：写进 `import_batches.summary` ✓（事后查"这一批里状态被改成了什么"✓）
    """

    # 设备状态：{源表写法: 行数}（识别成功 ✓ / 未识别 ✗ / 未填 ✓ 三类分开 ✓）
    status_mapped: dict[str, int] = field(default_factory=dict)
    status_unrecognized: dict[str, int] = field(default_factory=dict)
    status_blank: int = 0
    # 最终落库的状态分布 {状态: 行数}
    status_applied: dict[str, int] = field(default_factory=dict)
    # 供应商
    supplier_matched: dict[str, int] = field(default_factory=dict)
    supplier_missing: dict[str, int] = field(default_factory=dict)
    supplier_inactive: dict[str, int] = field(default_factory=dict)
    # 替代型号
    replacement_matched: int = 0
    replacement_missing: dict[str, int] = field(default_factory=dict)
    replacement_self: int = 0

    @staticmethod
    def _bump(bucket: dict[str, int], key: str) -> None:
        bucket[key] = bucket.get(key, 0) + 1

    def note_status(self, raw: Any, applied: DeviceStatus) -> None:
        token = normalize_token(raw)
        if not token:
            self.status_blank += 1
        elif map_device_status(raw)[1]:
            self._bump(self.status_mapped, str(raw).strip())
        else:
            self._bump(self.status_unrecognized, str(raw).strip())
        self._bump(self.status_applied, status_label(applied))

    def note_supplier(self, raw: Any, matched: Any | None, inactive: Any | None = None) -> None:
        name = str(raw or "").strip()
        if not name:
            return
        if matched is not None:
            self._bump(self.supplier_matched, name)
        elif inactive is not None:
            self._bump(self.supplier_inactive, name)
        else:
            self._bump(self.supplier_missing, name)

    def note_replacement(
        self, raw: Any, matched: Any | None, *, self_referenced: bool = False
    ) -> None:
        model = str(raw or "").strip()
        if not model:
            return
        if matched is not None:
            self.replacement_matched += 1
        elif self_referenced:
            self.replacement_self += 1
        else:
            self._bump(self.replacement_missing, model)

    def unmatched_counts(self) -> dict[str, int]:
        return {
            "status_unrecognized": sum(self.status_unrecognized.values()),
            "status_blank": self.status_blank,
            "supplier_missing": sum(self.supplier_missing.values()),
            "supplier_inactive": sum(self.supplier_inactive.values()),
            "replacement_missing": sum(self.replacement_missing.values()),
            "replacement_self": self.replacement_self,
        }

    def to_dict(self) -> dict[str, Any]:
        """给接口/批次留档的形状 ✓（含**逐值明细** ✓ —— "哪些写法没认出来"必须列出来 ✓）。"""
        return {
            "status": {
                "mapped": self.status_mapped,
                "unrecognized": self.status_unrecognized,
                "blank": self.status_blank,
                "applied": self.status_applied,
            },
            "supplier": {
                "matched": self.supplier_matched,
                "missing": self.supplier_missing,
                "inactive": self.supplier_inactive,
            },
            "replacement": {
                "matched": self.replacement_matched,
                "missing": self.replacement_missing,
                "self": self.replacement_self,
            },
            "unmatched_counts": self.unmatched_counts(),
        }


__all__ = [
    "ColumnMappingReport",
    "DEFAULT_DEVICE_STATUS",
    "device_is_usable",
    "find_inactive_supplier",
    "map_device_status",
    "match_replacement",
    "match_supplier",
    "normalize_model",
    "normalize_supplier_name",
    "normalize_token",
    "status_label",
]
