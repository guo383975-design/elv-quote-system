"""设备库分类树：默认收起 + 记住展开状态（2026-10-05 用户要求）。

为什么值得钉住：`default-expand-all` 只在**首次渲染**生效，而分类树的数据是
异步加载的（初始为空）→ 行为不可预期（时而展开时而收起 ✗）。
改成"按本地记忆"之后，别再被改回去 ✗。
"""

from __future__ import annotations

import pathlib
import re

VIEW = (
    pathlib.Path(__file__).resolve().parents[3]
    / "apps" / "web" / "src" / "views" / "DeviceLibraryView.vue"
)


def _main_tree_block(src: str) -> str:
    """截出**主树**那个 el-tree 的标签块（不含编辑弹窗里的 el-tree-select）。"""
    start = src.index("<el-tree\n")
    end = src.index("</el-tree>", start)
    return src[start:end]


def test_device_tree_is_collapsed_by_default() -> None:
    src = VIEW.read_text(encoding="utf-8")
    block = _main_tree_block(src)
    assert "default-expand-all" not in block, (
        "设备库主树不该再用 default-expand-all（数据异步加载，它时灵时不灵）"
    )
    assert "treeExpandedKeys" in block, "主树应当用 :default-expanded-keys 控制展开"
    assert "rememberTreeExpand" in block, "展开/收起要记下来（@node-expand/@node-collapse）"


def test_device_tree_remembers_expand_state() -> None:
    src = VIEW.read_text(encoding="utf-8")
    assert "device-tree-expanded" in src, "应当用 localStorage 记住展开的节点"
    assert "getExpandedKeys" in src, "记状态时要读 el-tree 当前展开的 keys"
    # 没有记忆时应是空数组（= 全收起），不能默认塞一批 key
    assert re.search(r"treeExpandedKeys = ref<string\[\]>\(\[\]\)", src), (
        "展开状态初值必须是空数组（默认全部收起）"
    )
