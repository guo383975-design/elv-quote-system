"""断言页签顺序与名称（2026-10-05）。

用户要求：「登录留痕」改「登录日志」；「配置变更记录」放到**最后**。
把这两条钉住 —— 否则下次重构又会被挪回去（这个文件已经踩过四次结构类问题）。
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]
VIEW = ROOT / "apps/web/src/views/SystemSettingsView.vue"

EXPECTED_ORDER = [
    "系统设置",
    "角色管理与权限矩阵",
    "用户管理",
    "AI 助理",
    "登录日志",
    "配置变更记录",
]


def _tab_labels() -> list[str]:
    src = VIEW.read_text(encoding="utf-8")
    return re.findall(r'<el-tab-pane[^>]*\blabel="([^"]+)"', src)


def test_tab_order_and_names() -> None:
    labels = _tab_labels()
    assert labels == EXPECTED_ORDER, (
        f"页签顺序/名称不符：\n  实际：{labels}\n  期望：{EXPECTED_ORDER}"
    )


def test_login_tab_renamed_to_log() -> None:
    """旧名不该再出现在界面上（注释里的历史说明也已改口）。"""
    src = VIEW.read_text(encoding="utf-8")
    assert "登录日志" in src
    assert 'label="登录留痕"' not in src, "页签名还没改过来"
    # 界面文案里不该再有「登录留痕」这个词
    assert "登录留痕" not in src, "代码/文案里仍残留「登录留痕」"
