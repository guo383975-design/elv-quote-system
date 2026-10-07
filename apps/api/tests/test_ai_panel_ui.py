"""防回归：解析方式不能再用浮层下拉（2026-10-05 用户报障「只能选自动」）。

根因：AI 面板是 `z-index: 2100` 的固定侧边栏，而 Element Plus 的浮层默认约 2000
且被传送到 body → 浮层落在面板**底下** → 点得着却选不中。

这条测试盯两件事：
  ① 解析方式用**分段按钮**（el-radio-group），不用 el-select（不产生浮层）；
  ② 全局样式里保留"抬高浮层层级"的规则 —— 面板内其它下拉（供应商）靠它。
"""
from __future__ import annotations

import pathlib

WEB = pathlib.Path(__file__).resolve().parents[3] / "apps" / "web" / "src"


def test_engine_mode_uses_segmented_buttons_not_dropdown() -> None:
    src = (WEB / "components" / "AiAssistantPanel.vue").read_text(encoding="utf-8")
    assert 'v-model="engineMode"' in src, "解析方式控件没了"
    # 控件必须是分段按钮
    assert "el-radio-group" in src and 'value="rule"' in src and 'value="llm"' in src, (
        "解析方式应当用 el-radio-group + el-radio-button（value=auto/rule/llm）"
    )
    # 且不能退回到浮层下拉：那正是"只能选自动"的成因
    idx = src.index('v-model="engineMode"')
    around = src[max(0, idx - 400) : idx + 200]
    assert "<el-select" not in around, (
        "解析方式不能用 el-select：面板 z-index 高于浮层，选项会点不中"
    )


def test_global_styles_raise_popper_z_index() -> None:
    css = (WEB / "styles" / "main.css").read_text(encoding="utf-8")
    assert "el-dropdown__popper" in css and "z-index: 2200" in css, (
        "全局样式需要把浮层层级抬到 AI 面板（2100）之上，"
        "否则面板里的「供应商」下拉也点不动"
    )
