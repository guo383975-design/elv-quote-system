"""安全与留痕加固测试（2026-10-04）。

对应三件事：
  · **H-10** 留痕带 IP/UA/单号快照；删单不销毁留痕；看成本也留痕；登录尝试留痕
  · **H-8** 登录失败锁定；强制下线（会话版本）；令牌刷新；改密码失效旧令牌
  · **M-10** 人工信息价的 (地区+工种+期次) 唯一约束
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.labor import LaborClass, LaborRate
from app.models.project import Project, ProjectStatus, ProjectType

FORWARDED = {"X-Forwarded-For": "203.0.113.9, 10.0.0.1", "User-Agent": "hardening-test/1.0"}


async def _project(db: AsyncSession, code: str) -> Project:
    p = Project(
        code=code, name="加固测试项目", customer_name="客户",
        project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.flush()
    return p


async def _quote(client: AsyncClient, headers, project_id, code: str) -> dict:
    r = await client.post(
        "/api/v1/quotes",
        headers={**headers, **FORWARDED},
        json={
            "project_id": str(project_id), "code": code, "title": "加固测试单",
            "fee_base": "device_aux_install", "auxiliary_base": "device",
            "auxiliary_rate": "0.06", "design_rate": "0.02", "warranty_rate": "0.03",
            "mgmt_profit_rate": "0.08", "tax_rate": "0.09",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _make_user(client: AsyncClient, admin_headers, username: str, password: str) -> dict:
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": username, "email": f"{username}@example.com",
            "password": password, "full_name": "加固测试用户", "role": "viewer",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# H-10 留痕
# ---------------------------------------------------------------------------

async def test_audit_records_ip_ua_and_subject_code(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _project(db, "PRJ-H10-1")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, "QT-H10-0001")

    r = await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=admin_headers)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert rows, "建单就应该有一条留痕"
    row = rows[0]
    assert row["action"] == "create", row
    assert row["ip"] == "203.0.113.9", row          # 取 X-Forwarded-For 第一跳，不是 127.0.0.1
    assert row["user_agent"] == "hardening-test/1.0", row
    assert row["subject_code"] == "QT-H10-0001", row  # 单号快照


async def test_audit_survives_quote_delete(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """删单不销毁留痕：删掉之后仍能回答"谁把哪张单删了"。"""
    p = await _project(db, "PRJ-H10-2")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, "QT-H10-0002")

    r = await client.delete(f"/api/v1/quotes/{q['id']}", headers={**admin_headers, **FORWARDED})
    assert r.status_code == 204, r.text

    # 全局留痕里必须还在，而且标着"对象已删除"
    # （按单查在单被删后查不到，这本来就正常 —— 关键是"删了哪张单"这个事实留住了）
    r = await client.get(
        "/api/v1/audit-logs/recent", params={"subject_code": "QT-H10-0002"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert items, "删单留痕不该消失"
    deleted = [x for x in items if x["action"] == "delete"]
    assert deleted, items
    assert deleted[0]["subject_deleted"] is True
    assert deleted[0]["subject_code"] == "QT-H10-0002"
    assert deleted[0]["ip"] == "203.0.113.9"


async def test_cost_view_is_audited(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _project(db, "PRJ-H10-3")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, "QT-H10-0003")

    r = await client.get(f"/api/v1/quotes/{q['id']}/cost", headers={**admin_headers, **FORWARDED})
    assert r.status_code == 200, r.text

    rows = (
        await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=admin_headers)
    ).json()
    assert any(x["action"] == "cost_view" for x in rows), rows


async def test_login_attempts_are_recorded(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    await _make_user(client, admin_headers, "h10login", "Viewer12345!")

    bad = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "h10login", "password": "wrong-password"},
        headers=FORWARDED,
    )
    assert bad.status_code == 401, bad.text
    good = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "h10login", "password": "Viewer12345!"},
        headers=FORWARDED,
    )
    assert good.status_code == 200, good.text

    r = await client.get(
        "/api/v1/auth/login-logs", params={"username": "h10login"}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    logs = r.json()
    assert [x["success"] for x in logs[:2]] == [True, False], logs[:2]
    assert all(x["ip"] == "203.0.113.9" for x in logs[:2]), logs[:2]


async def test_login_logs_are_admin_only(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    await _make_user(client, admin_headers, "h10viewer", "Viewer12345!")
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "h10viewer", "password": "Viewer12345!"},
        )
    ).json()["access_token"]
    r = await client.get(
        "/api/v1/auth/login-logs", headers={"Authorization": f"Bearer {tok}"}
    )
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# H-8 登录锁定 / 强制下线 / 刷新
# ---------------------------------------------------------------------------

async def test_login_lockout_after_repeated_failures(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    from app.api.v1.auth import LOCKOUT_MAX_FAILURES

    await _make_user(client, admin_headers, "h8lock", "Viewer12345!")

    for _ in range(LOCKOUT_MAX_FAILURES):
        r = await client.post(
            "/api/v1/auth/login/json",
            json={"username": "h8lock", "password": "wrong-password"},
            headers=FORWARDED,
        )
        assert r.status_code == 401, r.text

    # 第 N+1 次：即使口令正确也先被锁住
    r = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "h8lock", "password": "Viewer12345!"},
        headers=FORWARDED,
    )
    assert r.status_code == 429, r.text
    assert "锁定" in r.json()["detail"]

    # 别的账号不受影响
    assert (await client.get("/api/v1/auth/me", headers=admin_headers)).status_code == 200


async def test_refresh_issues_working_token(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    await _make_user(client, admin_headers, "h8refresh", "Viewer12345!")
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "h8refresh", "password": "Viewer12345!"},
        )
    ).json()["access_token"]

    r = await client.post("/api/v1/auth/refresh", headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    new_tok = r.json()["access_token"]
    assert new_tok
    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {new_tok}"})
    assert me.status_code == 200 and me.json()["username"] == "h8refresh"


async def test_force_logout_invalidates_existing_tokens(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    user = await _make_user(client, admin_headers, "h8drop", "Viewer12345!")
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "h8drop", "password": "Viewer12345!"},
        )
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200

    r = await client.post(f"/api/v1/users/{user['id']}/force-logout", headers=admin_headers)
    assert r.status_code == 200, r.text

    # 老令牌立刻失效，且提示是"会话已失效"而不是普通 401
    r = await client.get("/api/v1/auth/me", headers=h)
    assert r.status_code == 401, r.text
    assert "会话已失效" in r.json()["detail"], r.json()

    # 重新登录后可用（版本已 +1）
    tok2 = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "h8drop", "password": "Viewer12345!"},
        )
    ).json()["access_token"]
    assert (
        await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {tok2}"})
    ).status_code == 200


async def test_password_change_invalidates_existing_tokens(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    user = await _make_user(client, admin_headers, "h8pwd", "Viewer12345!")
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "h8pwd", "password": "Viewer12345!"},
        )
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 200

    r = await client.patch(
        f"/api/v1/users/{user['id']}",
        headers=admin_headers,
        json={"password": "BrandNew12345!"},
    )
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/auth/me", headers=h)).status_code == 401


# ---------------------------------------------------------------------------
# M-10 人工信息价唯一约束
# ---------------------------------------------------------------------------

def test_labor_rate_has_unique_constraint() -> None:
    names = {c.name for c in LaborRate.__table__.constraints if c.name}
    assert "uq_labor_rates_region_class_date" in names, names


async def test_labor_rate_duplicate_is_rejected_by_db(
    client: AsyncClient, db: AsyncSession
) -> None:
    """绕开接口直接写库也必须被数据库拦住（唯一约束才是并发下的保险）。"""
    from datetime import date
    from decimal import Decimal

    db.add(
        LaborRate(
            region="ningbo", labor_class=LaborClass.A, effective_date=date(2030, 1, 1),
            price_per_day=Decimal("199.00"),
        )
    )
    await db.commit()

    db.add(
        LaborRate(
            region="ningbo", labor_class=LaborClass.A, effective_date=date(2030, 1, 1),
            price_per_day=Decimal("188.00"),
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


