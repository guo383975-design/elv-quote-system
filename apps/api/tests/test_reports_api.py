"""报表中心测试（2026-10-04）。"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceCategory, DeviceStatus
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote import Quote, QuoteStatus


async def test_reports_overview_shape_and_counts(
    client: AsyncClient, db: AsyncSession, admin_headers, labor_rates
) -> None:
    """总览要给出各面板的数据，并且数字与库里一致。"""
    p = Project(
        code="PRJ-RPT-1", name="报表测试项目", customer_name="客户",
        project_type=ProjectType.COMMERCIAL, status=ProjectStatus.QUOTED,
    )
    db.add(p)
    await db.flush()
    db.add_all([
        Quote(project_id=p.id, code="QT-RPT-1", version=1, title="批了的",
              status=QuoteStatus.APPROVED, total_with_tax=Decimal("1000.00")),
        Quote(project_id=p.id, code="QT-RPT-2", version=1, title="待审批的",
              status=QuoteStatus.SUBMITTED, total_with_tax=Decimal("500.00")),
        Device(code="RPT-1", name="在售设备", unit="台", category=DeviceCategory.VIDEO,
               status=DeviceStatus.ON_SALE, reference_price=10),
        Device(code="RPT-2", name="停产的", unit="台", category=DeviceCategory.VIDEO,
               status=DeviceStatus.EOL, reference_price=10),
    ])
    await db.commit()

    r = await client.get("/api/v1/reports/overview", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()

    # 报价：总数/金额/待办
    assert body["quotes"]["total"] == 2, body["quotes"]
    assert abs(body["quotes"]["amount"] - 1500.0) < 0.01
    assert body["quotes"]["pending_count"] == 1, body["quotes"]
    assert abs(body["quotes"]["pending_amount"] - 500.0) < 0.01
    by = {row["status"]: row for row in body["quotes"]["by_status"]}
    assert by["approved"]["label"], "状态要有中文标签"
    assert by["approved"]["count"] == 1

    # 项目
    assert body["projects"]["total"] >= 1
    assert any(row["status"] == "quoted" for row in body["projects"]["by_status"])

    # 设备库
    assert body["devices"]["total"] >= 2
    assert body["devices"]["eol"] >= 1
    assert body["devices"]["on_sale"] >= 1

    # 人工费（conftest 里种了当期信息价）
    assert body["labor_current"], "应有当前生效的人工信息价"
    first = body["labor_current"][0]
    assert first["label"] and first["price_per_day"] > 0

    # 停产影响（本次没造报价行，字段必须存在且为 0/列表）
    assert "discontinued_impact" in body
    assert isinstance(body["discontinued_impact"]["quotes"], list)


async def test_reports_requires_project_read(
    client: AsyncClient, viewer_headers, admin_headers
) -> None:
    """只读角色也能看报表（它是只读视图），但必须登录。"""
    r = await client.get("/api/v1/reports/overview", headers=viewer_headers)
    assert r.status_code == 200, r.text

    r = await client.get("/api/v1/reports/overview")
    assert r.status_code == 401, r.text



# ---------------------------------------------------------------------------
# 利润看板（2026-10-04）
# ---------------------------------------------------------------------------

async def test_profit_dashboard_only_counts_approved_latest(
    client: AsyncClient, admin_headers
) -> None:
    """只统计已批准，且每个项目只取最新那一版（改三次价不能算三次业绩）。"""
    p = (await client.post("/api/v1/projects", headers=admin_headers, json={
        "code": "PRJ-PROFIT-1", "name": "利润看板", "customer_name": "客户",
        "create_default_subsystems": False})).json()

    # 草稿单：不该进看板
    q_draft = (await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": p["id"], "code": "QT-PROFIT-D", "title": "草稿",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0.09"})).json()
    await client.post(f"/api/v1/quotes/{q_draft['id']}/items", headers=admin_headers,
                      json={"name": "设备", "quantity": "1", "unit_price": "10000",
                            "install_unit_price": "0", "cost_price": "5000"})
    await client.post(f"/api/v1/quotes/{q_draft['id']}/compute", headers=admin_headers)

    before = (await client.get("/api/v1/reports/profit-dashboard", headers=admin_headers)).json()

    # 两版都批准：只算最新那版
    for code, price in (("QT-PROFIT-A", "20000"), ("QT-PROFIT-B", "30000")):
        q = (await client.post("/api/v1/quotes", headers=admin_headers, json={
            "project_id": p["id"], "code": code, "title": code,
            "fee_base": "device_aux_install", "auxiliary_base": "device",
            "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
            "mgmt_profit_rate": "0", "tax_rate": "0"})).json()
        await client.post(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
                          json={"name": "设备", "quantity": "1", "unit_price": price,
                                "install_unit_price": "0", "cost_price": "10000"})
        await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers)
        await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                           json={"status": "submitted"})
        await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                           json={"status": "reviewing"})
        r = await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                               json={"status": "approved"})
        assert r.status_code == 200, r.text

    body = (await client.get("/api/v1/reports/profit-dashboard", headers=admin_headers)).json()
    mine = [x for x in body["by_project"] if x["project_code"] == "PRJ-PROFIT-1"]
    assert len(mine) == 1, mine            # 一个项目一行（不含草稿单、不含旧版）
    assert mine[0]["quote_code"] == "QT-PROFIT-B", mine
    assert Decimal(str(mine[0]["total_with_tax"])) == Decimal("30000")
    assert Decimal(str(mine[0]["cost_total"])) == Decimal("10000")
    assert Decimal(str(mine[0]["gross_profit"])) == Decimal("20000")
    # 汇总里包含这个项目
    assert body["summary"]["projects"] >= before["summary"]["projects"]
    assert body["trend"], "趋势里应有本月数据"


async def test_profit_dashboard_permission(client: AsyncClient, admin_headers) -> None:
    """利润看板按**利润可见性**判定（2026-10-05 起可在系统设置里按角色配）。

    默认：admin / manager / finance 能看；工程部、销售、采购、viewer 不能。
    以前这里另有一份写死的 {admin, manager, finance}，与 can_see_cost 并存，
    迟早打架；现在统一走 core/visibility.can_see_profit。
    """
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "profitviewer", "email": "profitviewer@example.com",
        "password": "Viewer12345!", "role": "viewer"})
    tok = (await client.post("/api/v1/auth/login/json", json={
        "username": "profitviewer", "password": "Viewer12345!"})).json()["access_token"]
    r = await client.get("/api/v1/reports/profit-dashboard",
                         headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 403, r.text
    assert "利润" in r.json()["detail"]

    # 经理可以看
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "profitmgr", "email": "profitmgr@example.com",
        "password": "Mgr12345!", "role": "manager"})
    tok2 = (await client.post("/api/v1/auth/login/json", json={
        "username": "profitmgr", "password": "Mgr12345!"})).json()["access_token"]
    ok = await client.get("/api/v1/reports/profit-dashboard",
                          headers={"Authorization": f"Bearer {tok2}"})
    assert ok.status_code == 200, ok.text
    assert "summary" in ok.json()
