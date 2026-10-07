"""防回归：设置页的卡片必须落在页签内（2026-10-05）。

用户发现：「登录留痕里面的权限矩阵是不是重复了？」

真相：五张卡片（配置变更留痕/导出导入/角色管理/权限矩阵/数据可见性）是
`el-tabs` 的**直接子级**，不属于任何页签。Element Plus 只把 `el-tab-pane` 当页签，
其它子元素**常驻显示** —— 于是切到「登录留痕」时它们还挂在页面下方，
看起来就像"登录留痕里也有权限矩阵"。

这个坑踩过两次（上一次是「恢复出厂设置」那张卡片），所以钉一条断言：
**设置页里每张卡片都必须位于某个 `el-tab-pane` 之内**。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
VIEWS = ROOT / "apps/web/src/views"


def test_settings_cards_live_inside_tab_panes() -> None:
    """每张卡片都要在页签内 —— 否则会"跟着所有页签常驻显示"。"""
    src = (VIEWS / "SystemSettingsView.vue").read_text(encoding="utf-8")
    lines = src.splitlines()
    opens = [i for i, ln in enumerate(lines) if "<el-tab-pane" in ln]
    closes = [i for i, ln in enumerate(lines) if "</el-tab-pane>" in ln]
    assert opens and closes, "设置页没有页签了？（结构变了，请更新这条测试）"

    cards = [
        i for i, ln in enumerate(lines)
        if 'class="card st-card"' in ln or 'class="fr-card"' in ln
    ]
    assert cards, "一张卡片都没找到？"
    outside = [
        i + 1 for i in cards
        if not (sum(1 for o in opens if o < i) > sum(1 for c in closes if c < i))
    ]
    assert not outside, (
        f"这些卡片不在任何页签内（会常驻显示在所有页签下方）：第 {outside} 行。"
        "把它们移进对应的 <el-tab-pane>…</el-tab-pane> 里。"
    )


def test_tab_panes_are_balanced_in_views_using_tabs() -> None:
    """用 el-tabs 的视图里，页签标签必须成对（少一个闭合会让后面的内容跑到页签外）。"""
    bad: list[str] = []
    for path in sorted(VIEWS.glob("*.vue")):
        src = path.read_text(encoding="utf-8")
        if "<el-tabs" not in src:
            continue
        opens = len(re.findall(r"<el-tab-pane\b", src))
        closes = len(re.findall(r"</el-tab-pane>", src))
        if opens != closes:
            bad.append(f"{path.name}（开 {opens} / 闭 {closes}）")
    assert not bad, f"这些文件的页签标签不成对：{bad}"
