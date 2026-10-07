"""恢复出厂设置测试（2026-10-05）。

这是**不可撤销**的操作，所以把每道闸都测一遍：
  ① 只有 admin 能用（其它角色 403，前端也隐藏）；
  ② 确认短语 + 当前密码，两个都必须对；
  ③ **没有备份就拒绝清库**（测试环境是 SQLite，正好验证这条）；
  ④ 只清该清的（业务数据），保留账号与系统设置；
  ⑤ 全量范围也要保住"当前操作者"的账号，否则自己立刻掉线；
  ⑥ 预演不删任何东西。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.services.factory_reset import CONFIRM_PHRASE, SCOPE_ALL, SCOPE_BUSINESS, perform

OK = CONFIRM_PHRASE


async def _mk_manager(client: AsyncClient, admin_headers) -> dict:
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "frmgr", "email": "frmgr@example.com",
        "password": "Mgr12345!", "role": "manager"})
    tok = (await client.post("/api/v1/auth/login/json", json={
        "username": "frmgr", "password": "Mgr12345!"})).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


async def _seed_business(client: AsyncClient, headers) -> dict:
    """造一点业务数据（项目 + 报价单 + 设备），供删除统计用。"""
    dev = (await client.post("/api/v1/devices", headers=headers, json={
        "code": "VFR1", "name": "出厂测试设备", "model": "FR-1",
        "category": "video", "unit": "台", "reference_price": "100",
        "cost_price": "60"})).json()
    prj = (await client.post("/api/v1/projects", headers=headers, json={
        "code": "PRJ-FR-1", "name": "出厂测试项目", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    qt = (await client.post("/api/v1/quotes", headers=headers, json={
        "project_id": prj["id"], "code": "QT-FR-1", "title": "出厂测试单",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0"})).json()
    return {"device": dev, "project": prj, "quote": qt}


async def test_factory_reset_is_admin_only(client: AsyncClient, admin_headers) -> None:
    mh = await _mk_manager(client, admin_headers)
    assert (await client.get("/api/v1/settings/factory-reset/preview",
                             headers=mh)).status_code == 403
    assert (await client.post("/api/v1/settings/factory-reset", headers=mh, json={
        "scope": "business", "confirm_text": OK, "password": "Mgr12345!"})).status_code == 403


async def test_factory_reset_requires_phrase_and_password(
    client: AsyncClient, admin_headers, db: AsyncSession
) -> None:
    admin = await db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None

    # 短语不对
    try:
        await perform(db, scope=SCOPE_BUSINESS, user=admin, password="admin12345",
                      confirm_text="随便写点什么", require_backup=False)
        raise AssertionError("短语不对却通过了")
    except PermissionError as exc:
        assert "确认短语" in str(exc)

    # 密码不对
    try:
        await perform(db, scope=SCOPE_BUSINESS, user=admin, password="wrong-password",
                      confirm_text=OK, require_backup=False)
        raise AssertionError("密码不对却通过了")
    except PermissionError as exc:
        assert "密码" in str(exc)

    # 空密码
    try:
        await perform(db, scope=SCOPE_BUSINESS, user=admin, password="",
                      confirm_text=OK, require_backup=False)
        raise AssertionError("空密码却通过了")
    except PermissionError as exc:
        assert "密码" in str(exc)


async def test_factory_reset_refuses_without_backup(
    client: AsyncClient, admin_headers
) -> None:
    """**关键安全线**：备份没做成就不许清库（测试库是 SQLite，pg_dump 不可用）。

    宁可这个功能在异常环境里用不了，也不能出现"没备份就清空"。
    """
    await _seed_business(client, admin_headers)
    r = await client.post("/api/v1/settings/factory-reset", headers=admin_headers, json={
        "scope": "business", "confirm_text": OK, "password": "admin12345"})
    assert r.status_code == 409, r.text
    assert "备份" in r.json()["detail"]

    # 数据当然还在
    got = (await client.get("/api/v1/projects", headers=admin_headers,
                            params={"keyword": "PRJ-FR-1"})).json()
    assert got["total"] == 1, got


async def test_factory_reset_preview_and_dry_run_are_safe(
    client: AsyncClient, admin_headers, db: AsyncSession
) -> None:
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))

    pv = (await client.get("/api/v1/settings/factory-reset/preview",
                           headers=admin_headers, params={"scope": "business"})).json()
    rows = {x["table"]: x["rows"] for x in pv["items"]}
    assert rows["quotes"] >= 1 and rows["projects"] >= 1 and rows["devices"] >= 1, rows
    assert pv["total"] >= 3

    out = await perform(db, scope=SCOPE_BUSINESS, user=admin, password="admin12345",
                        confirm_text=OK, dry_run=True, require_backup=False)
    assert out["dry_run"] is True and out["total_deleted"] == 0
    still = (await client.get("/api/v1/projects", headers=admin_headers,
                              params={"keyword": "PRJ-FR-1"})).json()
    assert still["total"] == 1, "预演不该删数据"


async def test_factory_reset_business_keeps_users_and_settings(
    client: AsyncClient, admin_headers, db: AsyncSession
) -> None:
    """业务范围：业务数据清空，**账号与系统设置保留**。"""
    await _seed_business(client, admin_headers)
    # 动一下系统设置，验证它不会被误删
    await client.patch("/api/v1/settings", headers=admin_headers,
                       json={"quote": {"valid_days": 45}})
    admin = await db.scalar(select(User).where(User.username == "admin"))
    users_before = int(await db.scalar(select(func.count()).select_from(User)) or 0)

    out = await perform(db, scope=SCOPE_BUSINESS, user=admin, password="admin12345",
                        confirm_text=OK, require_backup=False)
    assert out["total_deleted"] > 0, out
    detail = {x["table"]: x["deleted"] for x in out["deleted"]}
    assert detail["quotes"] >= 1 and detail["projects"] >= 1 and detail["devices"] >= 1, detail

    after = (await client.get("/api/v1/projects", headers=admin_headers)).json()
    assert after["total"] == 0, after
    devs = (await client.get("/api/v1/devices", headers=admin_headers)).json()
    assert devs["total"] == 0, devs
    # 账号与设置还在
    assert int(await db.scalar(select(func.count()).select_from(User)) or 0) == users_before
    s = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert s["quote"]["valid_days"] == 45, s["quote"]


async def test_factory_reset_all_keeps_current_admin(
    client: AsyncClient, admin_headers, db: AsyncSession
) -> None:
    """全量范围：其它账号清掉，**当前操作者必须留着**（否则自己立刻掉线）。"""
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "frdel", "email": "frdel@example.com",
        "password": "Viewer12345!", "role": "viewer"})
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))
    assert admin is not None

    out = await perform(db, scope=SCOPE_ALL, user=admin, password="admin12345",
                        confirm_text=OK, require_backup=False)
    assert out["total_deleted"] > 0

    names = [u.username for u in (await db.scalars(select(User))).all()]
    assert names == ["admin"], names
    # 系统设置也清了（回到默认值 30 天）
    s = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert s["quote"]["valid_days"] == 30, s["quote"]["valid_days"]
    # 当前账号仍可用（会话没断）
    assert (await client.get("/api/v1/auth/me", headers=admin_headers)).status_code == 200
