"""Auth + RBAC tests."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Action, Role, can, can_see_cost, visible_resources
from app.models.user import User


@pytest.mark.asyncio
async def test_health(client: AsyncClient) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


@pytest.mark.asyncio
async def test_login_success_json(client: AsyncClient, admin_user) -> None:
    r = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "admin", "password": "admin12345"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["expires_in"] > 0


@pytest.mark.asyncio
async def test_login_accepts_email(client: AsyncClient, admin_user) -> None:
    r = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "admin@test.com", "password": "admin12345"},
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_login_wrong_password_401(client: AsyncClient, admin_user) -> None:
    r = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "admin", "password": "wrong-password"},
    )
    assert r.status_code == 401
    assert "用户名或密码错误" in r.json()["detail"]


@pytest.mark.asyncio
async def test_login_oauth2_form(client: AsyncClient, admin_user) -> None:
    """Swagger 的 Authorize 按钮走这个端点。"""
    r = await client.post(
        "/api/v1/auth/login",
        data={"username": "admin", "password": "admin12345"},
    )
    assert r.status_code == 200
    assert r.json()["access_token"]


@pytest.mark.asyncio
async def test_me_requires_token(client: AsyncClient) -> None:
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_me_returns_current_user(client: AsyncClient, admin_headers) -> None:
    r = await client.get("/api/v1/auth/me", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["username"] == "admin"
    assert r.json()["role"] == "admin"


@pytest.fixture
def bootstrap(monkeypatch):
    """临时打开引导注册（默认是关闭的）—— 模拟"全新库 + 配了 BOOTSTRAP_TOKEN"。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "bootstrap_token", "test-bootstrap-token", raising=False)
    return "test-bootstrap-token"


@pytest.fixture
def no_bootstrap(monkeypatch):
    """显式关掉引导注册（不依赖开发者本地 .env 恰好没配）。"""
    from app.core.config import settings

    monkeypatch.setattr(settings, "bootstrap_token", "", raising=False)
    return ""


def _reg_body(**over) -> dict:
    body = {
        "username": "firstuser",
        "email": "first@test.com",
        "password": "first12345",
        "full_name": "第一人",
    }
    body.update(over)
    return body


@pytest.mark.asyncio
async def test_register_closed_by_default(client: AsyncClient, no_bootstrap) -> None:
    """没有引导令牌 → 匿名注册一律 403。

    这是本项目的安全边界：以前这个接口完全开放，而"第一个用户"直接是
    admin + superuser，内网任何人先调一次就能接管系统。
    """
    r = await client.post("/api/v1/auth/register", json=_reg_body())
    assert r.status_code == 403, r.text
    assert "已关闭" in r.json()["detail"]


@pytest.mark.asyncio
async def test_register_rejects_wrong_bootstrap_token(
    client: AsyncClient, bootstrap
) -> None:
    r = await client.post(
        "/api/v1/auth/register", json=_reg_body(bootstrap_token="not-the-token")
    )
    assert r.status_code == 403, r.text
    assert "引导令牌不正确" in r.json()["detail"]


@pytest.mark.asyncio
async def test_register_with_token_on_empty_db_creates_admin(
    client: AsyncClient, bootstrap, db
) -> None:
    """唯一允许的注册路径：空库 + 正确引导令牌 → 第一个用户是管理员。"""
    r = await client.post(
        "/api/v1/auth/register", json=_reg_body(bootstrap_token=bootstrap)
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "admin"
    assert r.json()["is_superuser"] is True


@pytest.mark.asyncio
async def test_register_closed_once_a_user_exists(
    client: AsyncClient, admin_user, bootstrap
) -> None:
    """库里有用户之后，即使拿着正确的引导令牌也不能再注册。"""
    r = await client.post(
        "/api/v1/auth/register", json=_reg_body(bootstrap_token=bootstrap)
    )
    assert r.status_code == 403, r.text
    assert "已有用户" in r.json()["detail"]


@pytest.mark.asyncio
async def test_bootstrap_admin_must_change_password(
    client: AsyncClient, db: AsyncSession, bootstrap
) -> None:
    """引导注册出来的第一个管理员必须带「首次登录强制改口令」标记。

    2026-10-08 离线包演练抓到的真缺陷：register 建 User 时没传
    must_change_password，于是落到模型默认 False —— 而 create-admin.sh
    结尾印的是「首次登录会强制改口令」。安装者设的初始口令因此永远不会被
    强制轮换。这里从**两条**口径同时钉住，避免只改了一处又回归：
      · 登录响应里的 must_change_password（前端就是按它弹阻塞对话框的）；
      · 库里那一列本身。
    （注册接口自己的响应体里**没有**这个字段 —— UserOut 不暴露它，这是对的，
      不用去改 UserOut。）
    """
    r = await client.post(
        "/api/v1/auth/register", json=_reg_body(bootstrap_token=bootstrap)
    )
    assert r.status_code == 201, r.text

    r2 = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "firstuser", "password": "first12345"},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["must_change_password"] is True, r2.text

    row = await db.scalar(select(User).where(User.username == "firstuser"))
    assert row is not None
    assert row.must_change_password is True


@pytest.mark.asyncio
async def test_change_password_clears_must_change_flag(
    client: AsyncClient, bootstrap
) -> None:
    """改完口令这个标记必须消失（否则对话框永远关不掉）。"""
    r = await client.post(
        "/api/v1/auth/register", json=_reg_body(bootstrap_token=bootstrap)
    )
    assert r.status_code == 201, r.text
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "firstuser", "password": "first12345"},
        )
    ).json()["access_token"]

    r3 = await client.post(
        "/api/v1/auth/change-password",
        headers={"Authorization": f"Bearer {tok}"},
        json={"old_password": "first12345", "new_password": "first54321"},
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["must_change_password"] is False

    r4 = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "firstuser", "password": "first54321"},
    )
    assert r4.status_code == 200, r4.text
    assert r4.json()["must_change_password"] is False, r4.text


# ---------------------------------------------------------------------------
# 管理员建号（POST /users）—— 以前收裸 dict：非法枚举 500、还能塞 is_superuser
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_user_invalid_role_is_422_not_500(
    client: AsyncClient, admin_headers
) -> None:
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "user01", "email": "user01@test.com",
            "password": "p12345678", "role": "bogus",
        },
    )
    assert r.status_code == 422, f"非法枚举必须是 422（以前是 500）：{r.text}"
    # 断言"因为 role 被拒"，而不是因为别的字段先失败（避免假通过）
    locs = [e.get("loc") for e in r.json()["detail"]]
    assert any("role" in (loc or []) for loc in locs), f"422 的原因不是 role：{locs}"


@pytest.mark.asyncio
async def test_create_user_invalid_status_is_422(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "user02", "email": "user02@test.com",
            "password": "p12345678", "status": "bogus",
        },
    )
    assert r.status_code == 422, r.text
    locs = [e.get("loc") for e in r.json()["detail"]]
    assert any("status" in (loc or []) for loc in locs), f"422 的原因不是 status：{locs}"


@pytest.mark.asyncio
async def test_create_user_cannot_self_grant_superuser(
    client: AsyncClient, admin_headers
) -> None:
    """请求体里的 is_superuser 必须被忽略。

    is_superuser 会绕过 require_roles（deps.py）与 can_see_cost（permissions.py），
    属于权限开关，不该由被调用的接口决定。
    """
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "sneaky", "email": "sneaky@test.com",
            "password": "p12345678", "is_superuser": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_superuser"] is False


@pytest.mark.asyncio
async def test_create_user_defaults_to_sales(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": "user03", "email": "user03@test.com", "password": "p12345678"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["role"] == "sales"
    assert r.json()["status"] == "active"


@pytest.mark.asyncio
async def test_admin_cannot_demote_or_disable_self(
    client: AsyncClient, admin_headers, admin_user
) -> None:
    """改自己角色的后果是"再也进不来"，必须挡住。"""
    # 先把值取出来：任何一次返回错误的请求都会让 conftest 的共享 session
    # rollback()，而 rollback 会 expire 掉 ORM 实例 —— 之后再读 admin_user.id
    # 就会触发懒加载，在 async 上下文里直接抛 MissingGreenlet（测试假故障）。
    uid = admin_user.id
    r = await client.patch(
        f"/api/v1/users/{uid}", headers=admin_headers, json={"role": "viewer"}
    )
    assert r.status_code == 400, r.text
    r = await client.patch(
        f"/api/v1/users/{uid}", headers=admin_headers, json={"status": "disabled"}
    )
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_user_patch_duplicate_email_409(
    client: AsyncClient, admin_headers, admin_user, sales_user
) -> None:
    sales_id = sales_user.id
    taken_email = admin_user.email
    r = await client.patch(
        f"/api/v1/users/{sales_id}",
        headers=admin_headers,
        json={"email": taken_email},
    )
    assert r.status_code == 409, r.text


@pytest.mark.asyncio
async def test_me_permissions_shape(client: AsyncClient, sales_headers) -> None:
    r = await client.get("/api/v1/auth/me/permissions", headers=sales_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "sales"
    assert "project" in body["resources"]
    assert "read" in body["actions"]["project"]
    # 销售不能删项目
    assert "delete" not in body["actions"]["project"]


# ---------------------------------------------------------------------------
# RBAC matrix unit tests (no HTTP)
# ---------------------------------------------------------------------------

def test_admin_can_do_anything() -> None:
    for res in ("user", "project", "device_library", "quote", "quota"):
        for act in (Action.READ, Action.CREATE, Action.UPDATE, Action.DELETE):
            assert can(Role.ADMIN, res, act)


def test_viewer_is_read_only() -> None:
    assert can(Role.VIEWER, "project", Action.READ)
    assert not can(Role.VIEWER, "project", Action.CREATE)
    assert not can(Role.VIEWER, "project", Action.DELETE)
    assert not can(Role.VIEWER, "quote", Action.COMPUTE)


def test_sales_cannot_delete_project() -> None:
    assert can(Role.SALES, "project", Action.CREATE)
    assert can(Role.SALES, "project", Action.UPDATE)
    assert not can(Role.SALES, "project", Action.DELETE)


def test_estimator_owns_device_library_and_quota() -> None:
    assert can(Role.ESTIMATOR, "device_library", Action.CREATE)
    assert can(Role.ESTIMATOR, "quota", Action.UPDATE)
    assert can(Role.ESTIMATOR, "quote", Action.COMPUTE)
    # 造价员不能管用户
    assert not can(Role.ESTIMATOR, "user", Action.CREATE)


def test_estimator_can_export_quote_but_viewer_cannot() -> None:
    """quote.export：业务拍板，造价员可导、客户视角不可导。

    一旦矩阵改回把 estimator 拿掉、或者顺手把 viewer 加进去，下面两条会立刻报警。
    """
    assert can(Role.ESTIMATOR, "quote", Action.EXPORT), (
        "造价员必须能导出自己做的报价单（带工艺系数核对单常要交付销售/审计）"
    )
    assert not can(Role.VIEWER, "quote", Action.EXPORT), (
        "viewer 仍然不能导出 —— 客户视角没有下载权限"
    )
    # 反向：放开 estimator 不能把 admin / sales / finance 弄丢
    for role in (Role.ADMIN, Role.SALES, Role.FINANCE):
        assert can(role, "quote", Action.EXPORT), f"{role} 必须能导出"


def test_finance_reads_audit_log() -> None:
    assert can(Role.FINANCE, "audit_log", Action.READ)
    assert not can(Role.FINANCE, "quote", Action.CREATE)


def test_unknown_role_or_resource_denied() -> None:
    assert not can("nonexistent", "project", Action.READ)
    assert not can(Role.ADMIN, "nonexistent_resource", Action.READ)
    assert not can(Role.SALES, "project", "nonexistent_action")


def test_visible_resources_differs_by_role() -> None:
    admin_res = visible_resources(Role.ADMIN)
    viewer_res = visible_resources(Role.VIEWER)
    assert "user" in admin_res
    assert "user" not in viewer_res
    assert "project" in viewer_res


def test_purchaser_imports_materials_but_cannot_approve() -> None:
    """采购：能导入物料（设备库/供应商）、能看进价，但不管报价审批。"""
    assert can(Role.PURCHASER, "device_library", Action.CREATE)
    assert can(Role.PURCHASER, "device_library", Action.EXPORT)
    assert can(Role.PURCHASER, "supplier", Action.CREATE)
    assert can_see_cost(role=Role.PURCHASER)
    assert not can(Role.PURCHASER, "quote", Action.CREATE)
    assert not can(Role.PURCHASER, "quote", Action.APPROVE)


def test_manager_approves_but_does_not_edit_quote() -> None:
    """经理：审核/批准报价、看毛利、**可重算对齐数字**，但不建/不改报价。

    compute 之所以要给经理：明细被人改过、或口径变更之后，审批人看到的
    可能是旧数字 —— 他不能改明细，只能自己重算对齐。只读 + 可重算
    不等于可编辑，数据仍然只能由销售/设计改。
    """
    assert can(Role.MANAGER, "quote", Action.APPROVE)
    assert can(Role.MANAGER, "quote", Action.EXPORT)
    assert can(Role.MANAGER, "quote", Action.COMPUTE)
    assert can_see_cost(role=Role.MANAGER)
    assert not can(Role.MANAGER, "quote", Action.CREATE)
    assert not can(Role.MANAGER, "quote", Action.UPDATE)
    assert not can(Role.MANAGER, "quote_item", Action.CREATE)
    assert not can(Role.MANAGER, "quote_item", Action.UPDATE)


def test_estimator_no_longer_approves() -> None:
    """审批权已从设计(estimator)移到经理(manager)。"""
    assert not can(Role.ESTIMATOR, "quote", Action.APPROVE)
    assert can(Role.ESTIMATOR, "quote", Action.CREATE)
    assert can(Role.ESTIMATOR, "quote", Action.COMPUTE)


# ---------------------------------------------------------------------------
# RBAC enforced over HTTP
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_viewer_cannot_create_project(client: AsyncClient, viewer_headers) -> None:
    r = await client.post(
        "/api/v1/projects",
        headers=viewer_headers,
        json={"code": "PRJ-X-1", "name": "测试", "customer_name": "客户"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_sales_cannot_list_users(client: AsyncClient, sales_headers) -> None:
    r = await client.get("/api/v1/users", headers=sales_headers)
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_admin_can_list_users(client: AsyncClient, admin_headers, admin_user) -> None:
    r = await client.get("/api/v1/users", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["total"] >= 1
