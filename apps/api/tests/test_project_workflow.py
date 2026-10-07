"""项目状态机 + 操作留痕 + 报价单计数（2026-10-02）。

背景：项目状态原来是裸 `setattr` —— `draft → won` 也允许、改完不留痕，
「谁把这一单标成已中标的」答不上来。现在：

  · 状态走 `app/core/workflow.py` 的流转表（推进 + 有据可依的回退）
  · 归档是收敛态，只有管理员能改出去
  · 每次状态变更写一条 `project_audit_logs`
  · 新建时可以选「报价中」，但同样过状态机（不能一出生就是"已中标"）
  · 列表返回 quote_count / pending_quote_count，经理一眼看到"有几张等我批"
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import (
    allowed_project_transitions,
    can_move_project,
    ordered_targets,
)
from app.models.project import ProjectStatus
from app.models.quote import Quote, QuoteStatus


def _payload(code: str = "PRJ-T-0001", **extra) -> dict:
    body = {"code": code, "name": "状态机测试项目", "customer_name": "测试客户"}
    body.update(extra)
    return body


# ---------------------------------------------------------------------------
# 纯函数层：流转表
# ---------------------------------------------------------------------------

def test_transition_table_forward_path() -> None:
    assert can_move_project(ProjectStatus.DRAFT, ProjectStatus.QUOTING)
    assert can_move_project(ProjectStatus.QUOTING, ProjectStatus.QUOTED)
    assert can_move_project(ProjectStatus.QUOTED, ProjectStatus.WON)
    assert can_move_project(ProjectStatus.QUOTED, ProjectStatus.LOST)
    assert can_move_project(ProjectStatus.WON, ProjectStatus.ARCHIVED)
    assert can_move_project(ProjectStatus.LOST, ProjectStatus.ARCHIVED)


def test_transition_table_rejects_skips() -> None:
    # 不能跳过报价直接中标/失单
    assert not can_move_project(ProjectStatus.DRAFT, ProjectStatus.WON)
    assert not can_move_project(ProjectStatus.DRAFT, ProjectStatus.QUOTED)
    assert not can_move_project(ProjectStatus.QUOTING, ProjectStatus.WON)
    # 没报过价就不该"已失单"
    assert not can_move_project(ProjectStatus.DRAFT, ProjectStatus.LOST)


def test_transition_table_allows_documented_backsteps() -> None:
    """回退是业务现实（填错了/客户改主意），堵死只会逼人到库里改数据。"""
    assert can_move_project(ProjectStatus.QUOTING, ProjectStatus.DRAFT)
    assert can_move_project(ProjectStatus.QUOTED, ProjectStatus.QUOTING)
    assert can_move_project(ProjectStatus.WON, ProjectStatus.QUOTED)
    assert can_move_project(ProjectStatus.LOST, ProjectStatus.QUOTING)


def test_archived_is_terminal_except_admin() -> None:
    assert not can_move_project(ProjectStatus.ARCHIVED, ProjectStatus.QUOTING)
    assert not can_move_project(ProjectStatus.ARCHIVED, ProjectStatus.QUOTED)
    # 管理员可以把它退回草稿（结案后发现要返工）
    assert can_move_project(ProjectStatus.ARCHIVED, ProjectStatus.DRAFT, is_admin=True)


def test_same_status_is_always_allowed() -> None:
    for s in ProjectStatus:
        assert can_move_project(s, s)
        assert can_move_project(ProjectStatus.ARCHIVED, ProjectStatus.ARCHIVED)


def test_allowed_transitions_includes_archive_from_everywhere() -> None:
    for s in (ProjectStatus.DRAFT, ProjectStatus.QUOTING, ProjectStatus.QUOTED,
              ProjectStatus.WON, ProjectStatus.LOST):
        assert ProjectStatus.ARCHIVED in allowed_project_transitions(s)


def test_ordered_targets_puts_forward_steps_first() -> None:
    """下拉顺序：前进在前、回退在后（决定用户点错的概率）。"""
    assert ordered_targets(ProjectStatus.DRAFT) == [
        ProjectStatus.QUOTING, ProjectStatus.ARCHIVED,
    ]
    assert ordered_targets(ProjectStatus.QUOTING) == [
        ProjectStatus.QUOTED, ProjectStatus.LOST, ProjectStatus.ARCHIVED,
        ProjectStatus.DRAFT,
    ]
    # 已中标：先给收敛动作（归档），再给回退（已报价）
    assert ordered_targets(ProjectStatus.WON) == [
        ProjectStatus.ARCHIVED, ProjectStatus.QUOTED,
    ]
    # 非管理员看不到归档的出口
    assert ordered_targets(ProjectStatus.ARCHIVED) == []
    assert ordered_targets(ProjectStatus.ARCHIVED, is_admin=True) == [ProjectStatus.DRAFT]


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_project_status_flow_over_http(client: AsyncClient, admin_headers) -> None:
    r = await client.post("/api/v1/projects", headers=admin_headers, json=_payload())
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    assert r.json()["status"] == "draft"

    path = ["quoting", "quoted", "won", "archived"]
    for target in path:
        rr = await client.patch(
            f"/api/v1/projects/{pid}", headers=admin_headers, json={"status": target}
        )
        assert rr.status_code == 200, f"{target}: {rr.text}"
        assert rr.json()["status"] == target


@pytest.mark.asyncio
async def test_project_status_jump_is_409(client: AsyncClient, admin_headers) -> None:
    r = await client.post("/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0002"))
    pid = r.json()["id"]
    rr = await client.patch(
        f"/api/v1/projects/{pid}", headers=admin_headers, json={"status": "won"}
    )
    assert rr.status_code == 409, rr.text
    # 文案要说清楚"当前能改成什么"，不能只说"不允许"
    assert "草稿" in rr.json()["detail"] and "报价中" in rr.json()["detail"]


@pytest.mark.asyncio
async def test_archived_project_needs_admin_to_leave(
    client: AsyncClient, admin_headers, sales_headers
) -> None:
    r = await client.post("/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0003"))
    pid = r.json()["id"]
    await client.patch(f"/api/v1/projects/{pid}", headers=admin_headers,
                       json={"status": "archived"})
    # 销售（有 project.update）改不出去
    rr = await client.patch(
        f"/api/v1/projects/{pid}", headers=sales_headers, json={"status": "quoting"}
    )
    assert rr.status_code == 409, rr.text
    assert "归档" in rr.json()["detail"]
    # 管理员可以退回草稿
    rr2 = await client.patch(
        f"/api/v1/projects/{pid}", headers=admin_headers, json={"status": "draft"}
    )
    assert rr2.status_code == 200, rr2.text


@pytest.mark.asyncio
async def test_create_project_with_status_goes_through_machine(
    client: AsyncClient, admin_headers
) -> None:
    # 建单即「报价中」是允许的（边谈边做）
    r = await client.post(
        "/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0004", status="quoting")
    )
    assert r.status_code == 201, r.text
    assert r.json()["status"] == "quoting"
    # 但一出生就是「已中标」不行
    r2 = await client.post(
        "/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0005", status="won")
    )
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
async def test_project_status_change_is_audited(
    client: AsyncClient, admin_headers, finance_headers, sales_headers
) -> None:
    r = await client.post("/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0006"))
    pid = r.json()["id"]
    await client.patch(f"/api/v1/projects/{pid}", headers=admin_headers,
                       json={"status": "quoting"})
    await client.patch(f"/api/v1/projects/{pid}", headers=admin_headers,
                       json={"status": "quoted"})

    # 财务有 audit_log.read，能看
    rl = await client.get(f"/api/v1/projects/{pid}/audit-logs", headers=finance_headers)
    assert rl.status_code == 200, rl.text
    logs = rl.json()
    assert len(logs) == 2, logs
    # 倒序：最新在前
    assert logs[0]["payload"] == {"from": "quoting", "to": "quoted"}
    assert logs[1]["payload"] == {"from": "draft", "to": "quoting"}
    # 销售没有审计权限
    assert (await client.get(f"/api/v1/projects/{pid}/audit-logs",
                             headers=sales_headers)).status_code == 403


@pytest.mark.asyncio
async def test_create_as_quoting_is_audited(client: AsyncClient, admin_headers,
                                            finance_headers) -> None:
    r = await client.post(
        "/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0007", status="quoting")
    )
    pid = r.json()["id"]
    logs = (await client.get(f"/api/v1/projects/{pid}/audit-logs",
                             headers=finance_headers)).json()
    assert len(logs) == 1
    assert logs[0]["payload"] == {"from": None, "to": "quoting"}


@pytest.mark.asyncio
async def test_allowed_statuses_endpoint(client: AsyncClient, admin_headers) -> None:
    r = await client.post("/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0008"))
    pid = r.json()["id"]
    rr = await client.get(f"/api/v1/projects/{pid}/allowed-statuses", headers=admin_headers)
    assert rr.status_code == 200, rr.text
    allowed = rr.json()
    assert allowed[0] == "draft", "第一个永远是当前状态（编辑时下拉要能显示原值）"
    assert set(allowed[1:]) == {"quoting", "archived"}

    await client.patch(f"/api/v1/projects/{pid}", headers=admin_headers,
                       json={"status": "quoting"})
    rr2 = await client.get(f"/api/v1/projects/{pid}/allowed-statuses", headers=admin_headers)
    assert set(rr2.json()[1:]) == {"quoted", "draft", "lost", "archived"}


@pytest.mark.asyncio
async def test_status_options_map_endpoint(client: AsyncClient, admin_headers) -> None:
    """一次拉全表：前端不许自己抄状态机（抄了迟早漂移成"下拉有选项但点下去 409"）。

    顺序也有约定：**前进方向在前、回退在后** —— 下拉里第一个应该是
    "下一步该干的事"（报价中 → 已报价），而不是"退回草稿"。
    """
    r = await client.get("/api/v1/projects/status-options", headers=admin_headers)
    assert r.status_code == 200, r.text
    options = r.json()
    assert options["draft"] == ["quoting", "archived"]
    assert options["quoting"] == ["quoted", "lost", "archived", "draft"]
    assert options["quoted"] == ["won", "lost", "archived", "quoting"]
    assert options["won"] == ["archived", "quoted"]
    assert options["lost"] == ["archived", "quoting"]
    # 归档是收敛态：管理员能退回草稿
    assert options["archived"] == ["draft"]


@pytest.mark.asyncio
async def test_project_list_reports_quote_counts(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    r = await client.post("/api/v1/projects", headers=admin_headers, json=_payload("PRJ-T-0009"))
    pid = UUID(r.json()["id"])
    db.add_all([
        Quote(project_id=pid, code="Q-A", title="草稿单", status=QuoteStatus.DRAFT),
        Quote(project_id=pid, code="Q-B", title="待审批单", status=QuoteStatus.SUBMITTED),
        Quote(project_id=pid, code="Q-C", title="审核中单", status=QuoteStatus.REVIEWING),
        Quote(project_id=pid, code="Q-D", title="已批准单", status=QuoteStatus.APPROVED),
    ])
    await db.commit()

    rl = await client.get("/api/v1/projects?keyword=PRJ-T-0009", headers=admin_headers)
    assert rl.status_code == 200, rl.text
    row = rl.json()["items"][0]
    assert row["quote_count"] == 4
    assert row["pending_quote_count"] == 2, "待审批 = 已提交 + 审核中"
