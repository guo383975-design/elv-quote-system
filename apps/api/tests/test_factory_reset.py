"""恢复出厂设置测试（2026-10-05，2026-10-08 加固后扩充）。

这是**不可撤销**的操作，所以把每道闸都测一遍：
  ① 环境闸门 `ALLOW_FACTORY_RESET` 默认关闭 → 接口 403（**新增**）；
  ② 只有 admin 能用（其它角色 403，前端也隐藏）；
  ③ 确认短语 + 当前密码，两个都必须对；
  ④ **没有备份就拒绝清库**（测试环境是 SQLite，正好验证这条）；
  ⑤ 只清该清的（业务数据），保留账号与系统设置；
  ⑥ 全量范围也要保住"当前操作者"的账号，否则自己立刻掉线；
  ⑦ 预演不删任何东西；
  ⑧ **必定留痕**：清库之后还查得到"谁、什么时候、什么范围、删了多少"，
     且留痕行**不在任何清理清单里**（scope=all 也删不掉）——**新增**。

⚠ 本文件全程不碰生产库：`conftest` 用的是内存 SQLite（`sqlite+aiosqlite:///:memory:`）。
"""

from __future__ import annotations

import json

import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.models.factory_reset_log import (
    STATUS_COMPLETED,
    STATUS_DRY_RUN,
    FactoryResetLog,
)
from app.models.user import User
from app.services.factory_reset import (
    CONFIRM_PHRASE,
    SCOPE_ALL,
    SCOPE_BUSINESS,
    SCOPE_BUSINESS_SETTINGS,
    SCOPES,
    TRACE_TABLE,
    FactoryResetDisabledError,
    _tables_for,
    perform,
    write_trace_file,
)

OK = CONFIRM_PHRASE


@pytest_asyncio.fixture
async def allow_reset(monkeypatch):
    """把环境闸门打开（等价于 .env 里 `ALLOW_FACTORY_RESET=true`）。

    本文件多数用例测的是**闸门之后**的那几道门，所以显式打开；
    "闸门默认关闭 → 403" 由 `test_factory_reset_disabled_by_default` 单独覆盖。
    用 monkeypatch 而不是改 .env：不改运行环境、用例之间互不污染。
    """
    monkeypatch.setattr(app_settings, "allow_factory_reset", True)
    return True


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
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
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
    client: AsyncClient, admin_headers, allow_reset
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
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
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
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
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
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
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


# ---------------------------------------------------------------------------
# P0-2 环境闸门（2026-10-08 事故后新增）
# ---------------------------------------------------------------------------

async def test_factory_reset_disabled_by_default(
    client: AsyncClient, admin_headers, db: AsyncSession, monkeypatch
) -> None:
    """**默认配置下接口必须 403**，而且一个字节都不许删。

    事故：任何拿到 admin 令牌的人/脚本（包括临时签发的子代理令牌）
    只要知道确认短语就能一键清库。现在默认连门都没有。
    """
    monkeypatch.setattr(app_settings, "allow_factory_reset", False)
    await _seed_business(client, admin_headers)

    r = await client.post("/api/v1/settings/factory-reset", headers=admin_headers, json={
        "scope": "business", "confirm_text": OK, "password": "admin12345"})
    assert r.status_code == 403, r.text
    detail = r.json()["detail"]
    # 报错要说清楚"为什么"和"真要用怎么开"
    assert "ALLOW_FACTORY_RESET" in detail, detail

    # 数据完好无损
    got = (await client.get("/api/v1/projects", headers=admin_headers,
                            params={"keyword": "PRJ-FR-1"})).json()
    assert got["total"] == 1, got
    # 连留痕都不该多一行（什么都没发生）
    n = int(await db.scalar(select(func.count()).select_from(FactoryResetLog)) or 0)
    assert n == 0, f"闸门关着却写了 {n} 条留痕"


async def test_factory_reset_disabled_blocks_direct_service_call(
    db: AsyncSession, admin_user: User, monkeypatch
) -> None:
    """闸门在**服务层**也拦得住 —— 否则"绕过路由直接调 perform"就能清库。"""
    monkeypatch.setattr(app_settings, "allow_factory_reset", False)
    try:
        await perform(db, scope=SCOPE_BUSINESS, user=admin_user, password="admin12345",
                      confirm_text=OK, dry_run=True, require_backup=False)
        raise AssertionError("闸门关着却通过了")
    except FactoryResetDisabledError as exc:
        assert "ALLOW_FACTORY_RESET" in str(exc)

    # 预演也一并拦下：默认配置下这个功能整体不可用
    assert int(await db.scalar(select(func.count()).select_from(FactoryResetLog)) or 0) == 0


async def test_factory_reset_dry_run_returns_counts_when_gate_open(
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
) -> None:
    """闸门打开后，dry_run 预演要能正常返回"各表多少行"，且不删任何东西。"""
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))

    out = await perform(db, scope=SCOPE_BUSINESS, user=admin, password="admin12345",
                        confirm_text=OK, dry_run=True, require_backup=False)
    assert out["dry_run"] is True
    assert out["total_deleted"] == 0
    rows = {x["table"]: x["rows"] for x in out["preview"]["items"]}
    assert rows["projects"] >= 1 and rows["quotes"] >= 1 and rows["devices"] >= 1, rows
    assert out["backup_path"] is None            # require_backup=False（测试专用）
    assert out["trace_id"]                        # 预演也留痕
    # 数据原封不动
    still = (await client.get("/api/v1/projects", headers=admin_headers,
                              params={"keyword": "PRJ-FR-1"})).json()
    assert still["total"] == 1


# ---------------------------------------------------------------------------
# P0-1 必定留痕（2026-10-08 事故后新增）
# ---------------------------------------------------------------------------

async def test_factory_reset_trace_table_is_never_cleared() -> None:
    """留痕表**不能**出现在任何 scope 的清理清单里 —— 这是留痕能活下来的前提。

    事故里"没有痕迹"的直接原因就是清理清单包含了三张 *_audit_logs。
    """
    for scope in SCOPES:
        names = {table for table, _ in _tables_for(scope)}
        assert TRACE_TABLE not in names, f"{scope} 的清理清单里混进了留痕表 {TRACE_TABLE}"


async def test_factory_reset_leaves_trace_after_clearing(
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
) -> None:
    """清库之后必须还查得到：谁、什么时候、什么范围、清了哪些表各多少行。"""
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))

    out = await perform(db, scope=SCOPE_BUSINESS, user=admin, password="admin12345",
                        confirm_text=OK, require_backup=False)

    # 业务数据确实没了
    after = (await client.get("/api/v1/projects", headers=admin_headers)).json()
    assert after["total"] == 0, after

    # 但留痕还在，而且内容完整
    row = await db.scalar(select(FactoryResetLog))
    assert row is not None, "清完库连一条留痕都没有 —— 这正是 2026-10-08 事故的样子"
    assert str(row.id) == out["trace_id"]
    assert row.operator_username == "admin"
    assert row.scope == SCOPE_BUSINESS
    assert row.dry_run is False
    assert row.status == STATUS_COMPLETED
    assert row.total_deleted == out["total_deleted"] > 0
    # 清理前各表行数（"将失去什么"）与清理结果都在
    counts = {x["table"]: x["rows"] for x in row.counts["items"]}
    assert counts["projects"] >= 1 and counts["quotes"] >= 1
    deleted = {x["table"]: x["deleted"] for x in row.deleted["items"]}
    assert deleted["projects"] >= 1 and deleted["quotes"] >= 1
    assert row.backup_path is None          # 测试路径不备份；生产路径必定有


async def test_factory_reset_trace_survives_scope_all(
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
) -> None:
    """`scope=all` 会删掉除自己外的所有用户 —— 留痕必须照样活着。

    所以 `operator_id` 刻意不加外键、另外存一份 `operator_username` 快照。
    """
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "frdel2", "email": "frdel2@example.com",
        "password": "Viewer12345!", "role": "viewer"})
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))

    await perform(db, scope=SCOPE_ALL, user=admin, password="admin12345",
                  confirm_text=OK, require_backup=False)

    # 其它账号确实被清了
    assert [u.username for u in (await db.scalars(select(User))).all()] == ["admin"]
    # 留痕还在
    row = await db.scalar(select(FactoryResetLog))
    assert row is not None, "scope=all 把留痕一起清了"
    assert row.scope == SCOPE_ALL
    assert row.operator_username == "admin"
    assert row.status == STATUS_COMPLETED


async def test_factory_reset_trace_json_next_to_backup(tmp_path) -> None:
    """留痕 JSON 落在**备份文件旁边**（同名不同后缀），且没有备份时不写文件。"""
    dump = tmp_path / "factory-reset_20261008_101010.dump"
    payload = {
        "operator_username": "admin",
        "scope": "business",
        "backup_path": str(dump),
        "counts": {"total": 5},
    }

    # 没有备份 → 不写（否则测试/开发机会被塞进一堆文件）
    assert write_trace_file(payload, backup=None) is None
    assert write_trace_file(payload, backup={}) is None

    written = write_trace_file(payload, backup={"path": str(dump)})
    assert written is not None
    assert written.name == "factory-reset_20261008_101010.json"
    assert written.parent == tmp_path

    got = json.loads(written.read_text(encoding="utf-8"))
    assert got["operator_username"] == "admin"
    assert got["scope"] == "business"
    assert got["backup_path"] == str(dump)


async def test_factory_reset_trace_marks_dry_run(
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
) -> None:
    """预演留下的痕迹要能区分出来（状态的 dry_run + 没删东西），不能冒充真清库。"""
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))

    await perform(db, scope=SCOPE_BUSINESS, user=admin, password="admin12345",
                  confirm_text=OK, dry_run=True, require_backup=False)

    row = await db.scalar(select(FactoryResetLog))
    assert row is not None
    assert row.status == STATUS_DRY_RUN
    assert row.dry_run is True
    assert row.total_deleted == 0
    assert row.deleted == {}


async def test_factory_reset_scope_business_settings_is_traced(
    client: AsyncClient, admin_headers, db: AsyncSession, allow_reset
) -> None:
    """三档 scope 都要留痕，别只覆盖 business。"""
    await _seed_business(client, admin_headers)
    admin = await db.scalar(select(User).where(User.username == "admin"))

    await perform(db, scope=SCOPE_BUSINESS_SETTINGS, user=admin, password="admin12345",
                  confirm_text=OK, require_backup=False)

    row = await db.scalar(select(FactoryResetLog))
    assert row is not None and row.scope == SCOPE_BUSINESS_SETTINGS
    assert int(await db.scalar(select(func.count()).select_from(FactoryResetLog)) or 0) == 1

