"""报价单状态机测试（2026-10-02）。

业务要求：「**必须先提交才能批准**」。

在此之前报价单**只有权限约束**（有 `quote.approve` 就能批准），于是
`草稿 → 已批准` 是放行的 —— 审批环节可以被完全绕过。现在补上流转表
（`app/core/workflow.py`），并且明确每个流转归谁做：

  · 提交审核 / 撤回 / 重新提交 → `quote.update`（销售 · 设计）
  · 批准 / 驳回 / 开始审核 / 退回草稿 / 归档 → `quote.approve`（经理）

两道关是**分开**的：方向不对 → 409（跟权限无关）；方向对但没权限 → 403。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.workflow import (
    can_move_quote,
    quote_transitions,
    required_quote_action,
)
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote import QuoteStatus

pytestmark = pytest.mark.asyncio


async def _project(db: AsyncSession, code: str) -> Project:
    p = Project(code=code, name="状态机项目", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _quote(client: AsyncClient, headers, project_id, code: str) -> dict:
    r = await client.post("/api/v1/quotes", headers=headers, json={
        "project_id": str(project_id), "code": code, "title": "状态机报价单",
    })
    assert r.status_code == 201, r.text
    return r.json()


async def _status(client: AsyncClient, headers, quote_id: str, target: str):
    return await client.patch(f"/api/v1/quotes/{quote_id}", headers=headers,
                              json={"status": target})


# ---------------------------------------------------------------------------
# 纯函数层
# ---------------------------------------------------------------------------

def test_draft_cannot_jump_to_approved() -> None:
    """这条就是业务要求本身。"""
    assert not can_move_quote(QuoteStatus.DRAFT, QuoteStatus.APPROVED)
    assert not can_move_quote(QuoteStatus.DRAFT, QuoteStatus.REJECTED)
    assert not can_move_quote(QuoteStatus.DRAFT, QuoteStatus.REVIEWING)
    assert can_move_quote(QuoteStatus.DRAFT, QuoteStatus.SUBMITTED)


def test_submitted_must_be_claimed_before_any_decision() -> None:
    """「审核中」是必经步骤：提交后必须先接手，才能批准/驳回。"""
    assert can_move_quote(QuoteStatus.SUBMITTED, QuoteStatus.REVIEWING)
    assert not can_move_quote(QuoteStatus.SUBMITTED, QuoteStatus.APPROVED)
    assert not can_move_quote(QuoteStatus.SUBMITTED, QuoteStatus.REJECTED)
    # 提交人可以撤回
    assert can_move_quote(QuoteStatus.SUBMITTED, QuoteStatus.DRAFT)
    # 接手之后才能拍板
    assert can_move_quote(QuoteStatus.REVIEWING, QuoteStatus.APPROVED)
    assert can_move_quote(QuoteStatus.REVIEWING, QuoteStatus.REJECTED)


def test_rejected_must_be_resubmitted() -> None:
    assert not can_move_quote(QuoteStatus.REJECTED, QuoteStatus.APPROVED)
    assert can_move_quote(QuoteStatus.REJECTED, QuoteStatus.SUBMITTED)
    assert can_move_quote(QuoteStatus.REJECTED, QuoteStatus.DRAFT)


def test_approved_cannot_be_rejected_directly() -> None:
    """要改已批准的单：先退回草稿 —— 这样"批准过又驳回"不会没有痕迹。"""
    assert not can_move_quote(QuoteStatus.APPROVED, QuoteStatus.REJECTED)
    assert can_move_quote(QuoteStatus.APPROVED, QuoteStatus.DRAFT)
    assert can_move_quote(QuoteStatus.APPROVED, QuoteStatus.ARCHIVED)


def test_transition_actions_split_by_role() -> None:
    assert required_quote_action(QuoteStatus.DRAFT, QuoteStatus.SUBMITTED) == "update"
    assert required_quote_action(QuoteStatus.REJECTED, QuoteStatus.SUBMITTED) == "update"
    assert required_quote_action(QuoteStatus.SUBMITTED, QuoteStatus.DRAFT) == "update"
    # 接手（开始审核）与随后的拍板都归审批人
    assert required_quote_action(QuoteStatus.SUBMITTED, QuoteStatus.REVIEWING) == "approve"
    assert required_quote_action(QuoteStatus.REVIEWING, QuoteStatus.APPROVED) == "approve"
    assert required_quote_action(QuoteStatus.REVIEWING, QuoteStatus.REJECTED) == "approve"
    assert required_quote_action(QuoteStatus.APPROVED, QuoteStatus.DRAFT) == "approve"
    assert required_quote_action(QuoteStatus.ARCHIVED, QuoteStatus.DRAFT) == "approve"
    # 未接手就不能拍板：这两种流转**根本不存在**
    assert required_quote_action(QuoteStatus.SUBMITTED, QuoteStatus.APPROVED) == ""
    assert required_quote_action(QuoteStatus.SUBMITTED, QuoteStatus.REJECTED) == ""
    # 非法流转没有动作
    assert required_quote_action(QuoteStatus.DRAFT, QuoteStatus.APPROVED) == ""


def test_quote_transitions_forward_first() -> None:
    assert quote_transitions(QuoteStatus.DRAFT) == [QuoteStatus.SUBMITTED,
                                                   QuoteStatus.ARCHIVED]
    assert quote_transitions(QuoteStatus.SUBMITTED) == [QuoteStatus.REVIEWING,
                                                      QuoteStatus.DRAFT]
    assert quote_transitions(QuoteStatus.REVIEWING) == [QuoteStatus.APPROVED,
                                                       QuoteStatus.REJECTED,
                                                       QuoteStatus.DRAFT]
    assert quote_transitions(QuoteStatus.APPROVED) == [QuoteStatus.ARCHIVED,
                                                      QuoteStatus.DRAFT]


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------

async def test_approve_draft_is_409_even_for_manager(
    client: AsyncClient, db: AsyncSession, admin_headers, manager_headers
) -> None:
    """核心场景：经理直接批准一张草稿 → 409，并且文案要说清楚该先提交。"""
    p = await _project(db, "PRJ-QW-001")
    q = await _quote(client, admin_headers, p.id, "QT-QW-001")

    r = await _status(client, manager_headers, q["id"], "approved")
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert "提交审核" in detail, detail


async def test_submit_then_approve_happy_path(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, manager_headers
) -> None:
    """完整链路：提交 → （必须）开始审核 → 批准。"""
    p = await _project(db, "PRJ-QW-002")
    q = await _quote(client, admin_headers, p.id, "QT-QW-002")

    r1 = await _status(client, sales_headers, q["id"], "submitted")
    assert r1.status_code == 200, r1.text
    assert r1.json()["status"] == "submitted"

    # 提交后不能直接批准 —— 必须先接手（这就是「审核中」的强制力）
    r_mid = await _status(client, manager_headers, q["id"], "approved")
    assert r_mid.status_code == 409, r_mid.text

    r2 = await _status(client, manager_headers, q["id"], "reviewing")
    assert r2.status_code == 200, r2.text
    r3 = await _status(client, manager_headers, q["id"], "approved")
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "approved"


async def test_submitted_requires_claim_before_any_decision(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, manager_headers
) -> None:
    """「审核中」是必经步骤：提交后批准和驳回都要先接手，且文案要说清楚。"""
    p = await _project(db, "PRJ-QW-009")
    q = await _quote(client, admin_headers, p.id, "QT-QW-009")
    await _status(client, sales_headers, q["id"], "submitted")

    for target in ("approved", "rejected"):
        r = await _status(client, manager_headers, q["id"], target)
        assert r.status_code == 409, f"{target}: {r.text}"
        detail = r.json()["detail"]
        assert "开始审核" in detail, detail

    # 接手后两种拍板都通
    await _status(client, manager_headers, q["id"], "reviewing")
    assert (await _status(client, manager_headers, q["id"], "rejected")).status_code == 200


async def test_manager_cannot_submit_and_sales_cannot_approve(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, manager_headers
) -> None:
    p = await _project(db, "PRJ-QW-003")
    q = await _quote(client, admin_headers, p.id, "QT-QW-003")

    # 提交是编制者的活：经理没有 quote.update
    r1 = await _status(client, manager_headers, q["id"], "submitted")
    assert r1.status_code == 403, r1.text

    await _status(client, sales_headers, q["id"], "submitted")
    await _status(client, manager_headers, q["id"], "reviewing")
    # 批准是审批人的活：销售没有 quote.approve
    r2 = await _status(client, sales_headers, q["id"], "approved")
    assert r2.status_code == 403, r2.text


async def test_submitter_can_withdraw_before_approval(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers
) -> None:
    p = await _project(db, "PRJ-QW-004")
    q = await _quote(client, admin_headers, p.id, "QT-QW-004")
    await _status(client, sales_headers, q["id"], "submitted")
    r = await _status(client, sales_headers, q["id"], "draft")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "draft"


async def test_rejected_must_be_resubmitted_before_approval(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, manager_headers
) -> None:
    p = await _project(db, "PRJ-QW-005")
    q = await _quote(client, admin_headers, p.id, "QT-QW-005")
    await _status(client, sales_headers, q["id"], "submitted")
    await _status(client, manager_headers, q["id"], "reviewing")
    assert (await _status(client, manager_headers, q["id"], "rejected")).status_code == 200

    # 驳回后直接批准 → 409
    r = await _status(client, manager_headers, q["id"], "approved")
    assert r.status_code == 409, r.text
    assert "重新提交" in r.json()["detail"]

    # 重新提交 → 重新接手 → 批准
    assert (await _status(client, sales_headers, q["id"], "submitted")).status_code == 200
    r_sub = await _status(client, manager_headers, q["id"], "approved")
    assert r_sub.status_code == 409, "重新提交后仍要先接手"
    assert (await _status(client, manager_headers, q["id"], "reviewing")).status_code == 200
    assert (await _status(client, manager_headers, q["id"], "approved")).status_code == 200


async def test_approved_must_be_reverted_before_editing(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, manager_headers
) -> None:
    p = await _project(db, "PRJ-QW-006")
    q = await _quote(client, admin_headers, p.id, "QT-QW-006")
    await _status(client, sales_headers, q["id"], "submitted")
    await _status(client, manager_headers, q["id"], "reviewing")
    await _status(client, manager_headers, q["id"], "approved")

    # 已批准：既不能改内容（409），也不能直接驳回（409）
    r1 = await client.patch(f"/api/v1/quotes/{q['id']}", headers=sales_headers,
                            json={"design_rate": "0.03"})
    assert r1.status_code == 409, r1.text
    assert (await _status(client, manager_headers, q["id"], "rejected")).status_code == 409

    # 退回草稿（审批权限）后可以改
    assert (await _status(client, manager_headers, q["id"], "draft")).status_code == 200
    r2 = await client.patch(f"/api/v1/quotes/{q['id']}", headers=sales_headers,
                            json={"design_rate": "0.03"})
    assert r2.status_code == 200, r2.text


async def test_reviewing_state_flow(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, manager_headers
) -> None:
    """submitted → reviewing（接手）→ approved；草稿不能直接进审核中。"""
    p = await _project(db, "PRJ-QW-007")
    q = await _quote(client, admin_headers, p.id, "QT-QW-007")
    # 草稿不能直接进审核中
    assert (await _status(client, manager_headers, q["id"], "reviewing")).status_code == 409
    await _status(client, sales_headers, q["id"], "submitted")
    assert (await _status(client, manager_headers, q["id"], "reviewing")).status_code == 200
    assert (await _status(client, manager_headers, q["id"], "approved")).status_code == 200


async def test_status_changes_are_audited_with_distinct_actions(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers,
    manager_headers, finance_headers
) -> None:
    p = await _project(db, "PRJ-QW-008")
    q = await _quote(client, admin_headers, p.id, "QT-QW-008")
    await _status(client, sales_headers, q["id"], "submitted")
    await _status(client, manager_headers, q["id"], "reviewing")
    await _status(client, manager_headers, q["id"], "approved")
    await _status(client, manager_headers, q["id"], "draft")

    r = await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=finance_headers)
    assert r.status_code == 200, r.text
    actions = [x["action"] for x in r.json()]
    # 倒序：最新在前。接手那一步记 update（它不是批准/驳回/提交）
    assert actions[:4] == ["update", "approve", "update", "submit"], actions

