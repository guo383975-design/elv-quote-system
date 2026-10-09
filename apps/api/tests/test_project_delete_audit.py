"""删项目必须留痕，且**级联删掉的报价单也不能无痕**（2026-10-09）。

问题现场：`delete_project` 原来只做 `db.delete(project)`，ORM 级联把
subsystems / quotes / quote_items 一起删掉，**一条留痕都不写**。
于是「删一个项目 = 一整套报价数据消失，且查不到是谁删的」——
这正是 2026-10-08 清库事故那类"无痕"路径的残余。

这里守三条：
1. 项目被删 → `project_audit_logs` 有一条 DELETE（project_id 置空、单号留下）；
2. 级联删掉的每张报价单 → `quote_audit_logs` 各有一条 DELETE；
3. 留痕**必须活下来**（不能被 ORM 的 delete-orphan 级联一起抹掉）。
"""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import AuditAction, ProjectAuditLog, QuoteAuditLog


async def _make_project_with_quotes(
    client: AsyncClient, headers: dict[str, str], code: str, *, quote_count: int = 2
) -> tuple[dict, list[dict]]:
    project = (
        await client.post(
            "/api/v1/projects",
            headers=headers,
            json={
                "code": code,
                "name": f"{code} 删单留痕测试",
                "customer_name": "测试客户",
                "create_default_subsystems": False,
            },
        )
    ).json()
    quotes = []
    for _ in range(quote_count):
        r = await client.post(
            "/api/v1/quotes", headers=headers, json={"project_id": project["id"]}
        )
        assert r.status_code in (200, 201), r.text
        quotes.append(r.json())
    return project, quotes


async def test_delete_project_writes_project_audit(
    client: AsyncClient, admin_headers: dict[str, str], db: AsyncSession
) -> None:
    project, _ = await _make_project_with_quotes(client, admin_headers, "PRJ-DEL-0001")

    r = await client.delete(f"/api/v1/projects/{project['id']}", headers=admin_headers)
    assert r.status_code == 204, r.text

    logs = (
        await db.scalars(
            select(ProjectAuditLog).where(
                ProjectAuditLog.subject_code == "PRJ-DEL-0001",
                ProjectAuditLog.action == AuditAction.DELETE,
            )
        )
    ).all()
    assert logs, "删项目必须写一条项目级 DELETE 留痕"
    log = logs[0]
    assert log.project_id is None, "外键必须置空，留痕才能活下来"
    assert log.payload.get("code") == "PRJ-DEL-0001"
    assert log.payload.get("quote_count") == 2, "留痕里要写清连带删了几张单"


async def test_delete_project_logs_each_cascaded_quote(
    client: AsyncClient, admin_headers: dict[str, str], db: AsyncSession
) -> None:
    project, quotes = await _make_project_with_quotes(client, admin_headers, "PRJ-DEL-0002")
    codes = {q["code"] for q in quotes}

    r = await client.delete(f"/api/v1/projects/{project['id']}", headers=admin_headers)
    assert r.status_code == 204, r.text

    logs = (
        await db.scalars(
            select(QuoteAuditLog).where(
                QuoteAuditLog.subject_code.in_(codes),
                QuoteAuditLog.action == AuditAction.DELETE,
            )
        )
    ).all()
    assert {log.subject_code for log in logs} == codes, "每张被级联删的单都要留痕"
    assert all(log.quote_id is None for log in logs)
    # 明确指出这是"删项目带走的"，而不是有人逐张删的
    assert all(log.payload.get("via") == "project_delete" for log in logs)


async def test_deleted_quote_rows_are_gone_but_history_survives(
    client: AsyncClient, admin_headers: dict[str, str], db: AsyncSession
) -> None:
    """业务数据必须真删掉；留痕必须留下来。两件事都要成立。"""
    project, quotes = await _make_project_with_quotes(
        client, admin_headers, "PRJ-DEL-0003", quote_count=1
    )
    qid = UUID(quotes[0]["id"])

    r = await client.delete(f"/api/v1/projects/{project['id']}", headers=admin_headers)
    assert r.status_code == 204, r.text

    # 报价单没了（再读 404）
    assert (await client.get(f"/api/v1/quotes/{qid}", headers=admin_headers)).status_code == 404
    # 但它的历史还在
    rows = (
        await db.scalars(
            select(QuoteAuditLog).where(QuoteAuditLog.subject_code == quotes[0]["code"])
        )
    ).all()
    assert rows, "删单不能把历史留痕一起抹掉"


async def test_delete_project_requires_permission(
    client: AsyncClient, admin_headers: dict[str, str], sales_headers: dict[str, str]
) -> None:
    """销售能建项目，但删项目仍然只有管理员能做（回归）。"""
    project, _ = await _make_project_with_quotes(client, admin_headers, "PRJ-DEL-0004")
    r = await client.delete(f"/api/v1/projects/{project['id']}", headers=sales_headers)
    assert r.status_code == 403


async def test_direct_quote_delete_keeps_history(
    client: AsyncClient, admin_headers: dict[str, str], db: AsyncSession
) -> None:
    """走单张删除接口那条路（quotes.py 里显式置空外键的那段）也要留住历史。

    这条单独立出来，是因为它和删项目走的是**两条不同的代码路径** ——
    任何一条把留痕删了，事后都答不上"谁删了哪张单"。
    """
    _, quotes = await _make_project_with_quotes(
        client, admin_headers, "PRJ-DEL-0005", quote_count=1
    )
    r = await client.delete(f"/api/v1/quotes/{quotes[0]['id']}", headers=admin_headers)
    assert r.status_code == 204, r.text

    rows = (
        await db.scalars(
            select(QuoteAuditLog).where(QuoteAuditLog.subject_code == quotes[0]["code"])
        )
    ).all()
    assert rows, "删单不能把这条单的历史留痕一起抹掉"
    assert any(row.action == AuditAction.DELETE for row in rows), "删单本身要有 DELETE 留痕"
