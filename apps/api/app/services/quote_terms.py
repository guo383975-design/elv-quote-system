"""条款模板变量（2026-10-05）。

## 要解决的问题

条款文本里写着一堆数字（有效期 30 天、预付 30%、验收 60%、质保金 10%、工期 45 天、
质保 12 个月），而其中**只有"有效期"有对应字段**（`valid_days`，用来算"有效期至"日期）。
于是：

> 把 `valid_days` 改成 45 → PDF 抬头写"有效期至 …+45 天"，
> 条款第 2 条**还写着"报价有效期 30 天"** → 给客户的报价单自相矛盾。

## 做法

  · 这些数字变成**结构化字段**（`settings.quote` 下的 prepay_ratio 等）；
  · 条款文本里用**变量**引用它们：`报价有效期 {valid_days} 天`；
  · 渲染时替换 —— 数字只有一处真相，改一处、条款自动跟着变；
  · 保存设置时**校验变量名**：写错的变量直接 422，不会带着 `{错别字}` 发给客户。
"""

from __future__ import annotations

import re
from typing import Any

# 变量 → 人类可读说明（设置页会把它当提示列出来）
CLAUSE_VARIABLES: dict[str, str] = {
    "valid_days": "报价有效期（天）",
    "prepay_ratio": "预付比例（%）",
    "accept_ratio": "验收后付款比例（%）",
    "warranty_ratio": "质保金比例（%）",
    "warranty_months": "质保期（月）",
    "build_days": "工期（日历天）",
}

# 这些数字的默认值（也是「系统设置」里的默认项）
DEFAULT_TERMS: dict[str, Any] = {
    "valid_days": 30,
    "prepay_ratio": 30,
    "accept_ratio": 60,
    "warranty_ratio": 10,
    "warranty_months": 12,
    "build_days": 45,
}

_VAR_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def unknown_variables(clauses: Any) -> list[str]:
    """找出条款里用到、但**没有定义**的变量名（保存时用来拦错别字）。"""
    bad: list[str] = []
    for text in clauses or []:
        for name in _VAR_RE.findall(str(text)):
            if name not in CLAUSE_VARIABLES and name not in bad:
                bad.append(name)
    return bad


def render_clauses(clauses: Any, variables: dict[str, Any]) -> tuple[str, ...]:
    """把 `{变量}` 换成实际数字。

    未定义的变量**原样保留**（保存时已被拦过；这里兜底是为了老数据里
    可能已经存了写错的变量名 —— 宁可让用户看到 `{valid_dayss}` 去找问题，
    也不要静默替换成空字符串把条款变成病句）。
    """

    def _sub(m: re.Match[str]) -> str:
        name = m.group(1)
        if name in variables and variables[name] is not None:
            return str(variables[name])
        return m.group(0)

    return tuple(_VAR_RE.sub(_sub, str(c)) for c in (clauses or []))


def build_variables(
    quote_settings: dict[str, Any], *, valid_days: int | None = None
) -> dict[str, Any]:
    """从报价设置里整理出渲染变量（有效期可被单据级覆盖传入）。"""
    out: dict[str, Any] = {}
    for name in CLAUSE_VARIABLES:
        value = (quote_settings or {}).get(name)
        out[name] = value if value is not None else DEFAULT_TERMS.get(name)
    if valid_days is not None:
        out["valid_days"] = valid_days
    return out
