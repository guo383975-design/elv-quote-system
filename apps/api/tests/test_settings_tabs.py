"""把新的页签布局钉进断言（2026-10-05）。

用户要求：「配置变更留痕」改名「配置变更记录」并单独页签；
「角色管理」+「权限矩阵」单独页签。结构断言要跟着写死，
否则下次重构又会被悄悄挪回去（这个坑我在同一个文件里踩了三次）。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
VIEW = ROOT / "apps/web/src/views/SystemSettingsView.vue"


def _pane_ranges(src: str) -> dict[str, tuple[int, int]]:
    """页签标签 → (起始行, 闭合行)，按嵌套深度配对。"""
    lines = src.splitlines()
    out: dict[str, tuple[int, int]] = {}
    for i, ln in enumerate(lines):
        if "<el-tab-pane" not in ln:
            continue
        m = re.search(r'label="([^"]+)"', ln)
        if not m:
            continue
        depth = 0
        for j in range(i, len(lines)):
            depth += lines[j].count("<el-tab-pane") - lines[j].count("</el-tab-pane>")
            if depth <= 0:
                out[m.group(1)] = (i, j)
                break
    return out


def test_settings_tabs_are_grouped_as_requested() -> None:
    """① 两个独立页签存在，且各自的卡片落在自己那一页里。"""
    src = VIEW.read_text(encoding="utf-8")
    panes = _pane_ranges(src)
    assert "配置变更记录" in panes, f"没有「配置变更记录」页签：{sorted(panes)}"
    assert "角色管理与权限矩阵" in panes, f"没有「角色管理与权限矩阵」页签：{sorted(panes)}"

    lines = src.splitlines()

    def card_line(title: str) -> int:
        idx = next(i for i, ln in enumerate(lines) if ln.strip() == title)
        while 'class="card st-card"' not in lines[idx]:
            idx -= 1
        return idx

    log_pane = panes["配置变更记录"]
    perm_pane = panes["角色管理与权限矩阵"]
    basic_pane = panes["系统设置"]

    # 配置变更记录：只在自己的页签里，且 basic 页签里没有它
    log_card = card_line("配置变更记录")
    assert log_pane[0] < log_card < log_pane[1], "「配置变更记录」卡片不在自己的页签内"
    assert not (basic_pane[0] < log_card < basic_pane[1]), "「配置变更记录」还留在系统设置页签里"

    # 角色管理 + 权限矩阵：都在「角色管理与权限矩阵」页签里
    for title in ("角色管理（自定义角色）", "权限矩阵（按角色）"):
        c = card_line(title)
        assert perm_pane[0] < c < perm_pane[1], f"「{title}」不在权限页签内"
        assert not (basic_pane[0] < c < basic_pane[1]), f"「{title}」还在系统设置页签里"

    # 权限矩阵**只应出现一次**（拆分时曾因行号位移把它删掉/插重）
    assert src.count("权限矩阵（按角色）") == 1, "权限矩阵卡片出现了多次（或消失了）"


def test_no_duplicate_cards_in_settings_view() -> None:
    """② 卡片不许重复 —— 拆分/搬移时最容易出的错。"""
    lines = VIEW.read_text(encoding="utf-8").splitlines()
    for title in ("报价策略", "报价说明条款", "配置导出 / 导入",
                  "角色管理（自定义角色）", "权限矩阵（按角色）", "配置变更记录"):
        # 只数"整行就是标题"的（卡片标题各占一行）；
        # 直接 src.count 会把正文/提示语里的同名字符串也算进去。
        n = sum(1 for ln in lines if ln.strip() == title)
        assert n == 1, f"「{title}」卡片出现了 {n} 次（应为 1 次）"


def test_data_visibility_card_is_gone() -> None:
    """「数据可见性」卡片已合并进权限矩阵（2026-10-05，用户要求）。

    成本/利润现在是矩阵里的两行字段级资源（data_cost / data_profit），
    界面只应有一处能配 —— 旧的独立卡片不该再出现。
    """
    src = VIEW.read_text(encoding="utf-8")
    assert "数据可见性" not in src, "「数据可见性」卡片还在（应已并入权限矩阵）"
    assert "cost_roles" not in src and "profit_roles" not in src, "旧的可见性字段还在"
