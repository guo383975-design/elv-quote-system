"""前端结构守卫（2026-10-04）。

起因：AI 助理悬浮窗第一次上线时，被批处理脚本误插进了**通知气泡
（`el-popover`）内部**。气泡内容是懒渲染的 —— 表现就是"配置都开了，
悬浮框永远不出现"，而且 vue-tsc 与构建都不报错。

这类错只能靠结构断言挡住，所以这里跨层检查一下 Vue 源码的挂载位置。
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_ai_panel_mounted_at_root_of_app_layout() -> None:
    src = _read("apps/web/src/layouts/AppLayout.vue")

    assert "<AiAssistantPanel" in src, "AI 悬浮窗没有被挂载"
    panel = src.index("<AiAssistantPanel")
    popover_close = src.index("</el-popover>")
    container_close = src.index("</el-container>")

    # 必须在通知气泡**之外**：气泡内容懒渲染，放进去等于不渲染
    assert panel > popover_close, (
        "AI 悬浮窗出现在通知气泡（el-popover）内部 —— 气泡懒渲染，"
        "配置打开后悬浮框也不会出现"
    )
    assert panel < container_close, "AI 悬浮窗必须在根容器 el-container 内"


def test_ai_panel_hidden_when_printing() -> None:
    """打印报价单时不该把 AI 悬浮窗一起打出来。"""
    src = _read("apps/web/src/components/AiAssistantPanel.vue")
    assert ".ai-root" in src and "@media print" in src, "AI 面板缺少打印隐藏样式"
    assert "display: none !important" in src


def test_keep_alive_views_refresh_on_activate() -> None:
    """被 `keep-alive` 缓存的页面，必须在切回时刷新数据。

    真实事故：`AppLayout` 缓存了 `DeviceLibraryView`，而它只在 `onMounted` 拉数据
    （只跑一次）。用户清空数据后切回设备库，看到的是缓存里的旧列表 ——
    以为「数据又自己回来了」（库里其实是 0 台）。看到的数据不是库里的数据，这类
    问题比报错更危险：用户会据此做判断。
    """
    layout = (ROOT / "apps/web/src/layouts/AppLayout.vue").read_text(encoding="utf-8")
    m = re.search(r":include=\"\[([^\]]*)\]\"", layout)
    assert m, "找不到 keep-alive 的 include 列表（结构变了？）"
    names = re.findall(r"'([A-Za-z]+)'", m.group(1))
    assert names, "keep-alive 的 include 列表是空的？"

    for name in names:
        view = ROOT / f"apps/web/src/views/{name}.vue"
        assert view.exists(), f"{name}.vue 不存在"
        src = view.read_text(encoding="utf-8")
        assert "onActivated" in src, (
            f"{name} 被 keep-alive 缓存，但没有 onActivated 刷新 —— "
            "切回来会显示过期数据（清库/别处改动后尤其危险）"
        )
        # 注意找**调用点**而不是 import 行（index("onActivated") 会先撞上 import）
        pos = src.find("onActivated(()")
        assert pos != -1, f"{name} 只是 import 了 onActivated，没有真正使用"
        act = src[pos : pos + 800]
        assert "load" in act, f"{name} 的 onActivated 里没有重新拉数据"
