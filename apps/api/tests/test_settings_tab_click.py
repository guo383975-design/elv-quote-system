"""断言：每个页签的 name 都必须能被 resolveTab 认出来（2026-10-05）。

用户报的：「角色管理与权限矩阵」「配置变更记录」这两个标签**点不了**。

根因：页签与 URL 查询参数双向同步，而 `TabKey`/`resolveTab` 只认旧页签 ——
点新页签会立刻被解析回 `basic`，看起来就是"点不了"。

这条断言把它焊住：模板里出现的每个 `name="x"`，都必须在 `TabKey` 与
`resolveTab` 里登记过。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
VIEW = ROOT / "apps/web/src/views/SystemSettingsView.vue"


def test_every_tab_name_is_resolvable() -> None:
    src = VIEW.read_text(encoding="utf-8")
    names = re.findall(r'<el-tab-pane[^>]*\bname="([^"]+)"', src)
    assert names, "设置页里没找到页签 name（结构变了？请更新这条测试）"
    assert len(names) == len(set(names)), f"页签 name 有重复：{names}"

    m = re.search(r"function resolveTab\(value: unknown\): TabKey \{(?:.*?\n)\}", src, re.S)
    assert m, "找不到 resolveTab"
    body = m.group(0)
    missing = [n for n in names if f"'{n}'" not in body]
    assert not missing, (
        f"这些页签没有被 resolveTab 处理：{missing}。"
        "页签与 URL 是双向同步的 —— 没登记就会被解析回 basic，"
        "表现为「这个标签点不了」。"
    )

    tm = re.search(r"type TabKey = ([^\n]+)", src)
    assert tm, "找不到 TabKey"
    missing2 = [n for n in names if f"'{n}'" not in tm.group(1)]
    assert not missing2, f"这些页签没进 TabKey：{missing2}"


def test_tab_switch_loads_are_declared() -> None:
    """切页签时的加载动作要写在 watch(tab) 里（否则切过去是空的）。"""
    src = VIEW.read_text(encoding="utf-8")
    m = re.search(r"watch\(tab, \(value\) => \{(?:.*?\n)\}\)", src, re.S)
    assert m, "找不到 watch(tab)"
    body = m.group(0)
    # 三个数据页签各自要有对应的加载
    for needle, why in (
        ("loadLoginLogs", "登录留痕"),
        ("loadAi", "AI 助理"),
        ("loadPermMatrix", "角色/权限矩阵"),
        ("loadAuditLogs", "配置变更记录"),
    ):
        assert needle in body, f"切到「{why}」时没有加载动作（{needle} 不在 watch(tab) 里）"
