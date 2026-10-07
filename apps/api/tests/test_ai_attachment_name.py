"""防回归：AI 附件名不能丢（2026-10-05）。

用户报的 bug：「附件：undefined（123 行）」。
根因：轮询解析完成时执行了 `pendingParse.value = { ...r, ... }`，
而 `r`（= `job.result`，解析结果）里**没有 filename** —— 一覆盖，文件名就没了；
发送时把 undefined 存进消息，历史记录里也一直是 undefined。
"""
from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[3]
PANEL = ROOT / "apps/web/src/components/AiAssistantPanel.vue"


def test_poll_preserves_attachment_filename() -> None:
    src = PANEL.read_text(encoding="utf-8")
    assert "job.filename" in src, (
        "轮询完成时没有用后端返回的 filename 兜底 —— "
        "`job.result` 不含文件名，直接覆盖会把附件名抹成 undefined"
    )
    assert "...pendingParse.value" in src, (
        "覆盖 pendingParse 时没有先展开原对象，附件名（以及其它字段）会丢"
    )


def test_attachment_name_has_fallback_everywhere() -> None:
    """界面与发消息两处都要兜底：绝不让 undefined 显示给用户。"""
    src = PANEL.read_text(encoding="utf-8")
    assert "{{ a.filename || '附件' }}" in src, "消息里的附件名没有兜底"
    assert "pendingParse.filename || '附件'" in src, "待解析卡片的文件名没有兜底"
    assert "pendingParse.value.filename || '附件'" in src, "发消息时没有兜底文件名"
