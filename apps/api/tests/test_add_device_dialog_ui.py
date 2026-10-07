"""「添加设备 / 材料」弹窗尺寸（2026-10-05 用户要求放大到 1920×1080）。

钉住两点，避免以后被改回小尺寸：
  ① 外框必须用**视口单位**（96vw），不是写死的 1200px ✗
  ② 里面的设备表格高度必须**按视口算**，不是写死的 380px ✗
     （只放大外框、里面还是那么小 = 用户看不出变化）
"""

from __future__ import annotations

import pathlib
import re

VIEW = (
    pathlib.Path(__file__).resolve().parents[3]
    / "apps" / "web" / "src" / "components" / "AddDeviceDialog.vue"
)


def test_dialog_uses_viewport_width() -> None:
    src = VIEW.read_text(encoding="utf-8")
    m = re.search(r'width="(\d+)vw"', src)
    assert m, "弹窗宽度应当用 vw（视口单位），不要写死像素"
    assert int(m.group(1)) >= 90, f"宽度只有 {m.group(1)}vw，太小（用户要求做大一些）"


def test_inner_table_height_follows_viewport() -> None:
    src = VIEW.read_text(encoding="utf-8")
    assert ':height="tableHeight"' in src, "设备表格高度应当绑定 tableHeight（按视口算）"
    assert re.search(r"height=\"\d+\"", src) is None, (
        "不该再有写死的表格 height（原来 380px 在大屏上太小）"
    )
    assert "viewportH" in src and "resize" in src, "大小要跟着窗口变化（监听 resize）"
