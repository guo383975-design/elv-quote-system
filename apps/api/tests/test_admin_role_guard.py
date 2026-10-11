"""「管理员」角色的授予/撤销只有 superuser 能做（2026-10-09）。

修的是什么：`users.py` 原来只拦「改**自己**的角色」，改**别人**毫无门槛，
而 admin 在 `can()` 里对任何资源恒定全权。所以

    user.update  ==  可以造出管理员，也可以把管理员踢出局

默认矩阵里 user.update 只给 admin，但「系统设置 → 权限矩阵」能把它授给
自定义角色 —— 一授出去就等于给了提权能力，而界面上只显示成开了个子菜单。

这里同时守另一面：**收紧不能收过头**。非 superuser 的 admin 仍然是
正常的管理员（能建号、能改资料、能改非管理员角色的角色），只是不能碰
「管理员」这个角色本身。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import hash_password
from app.models.user import User, UserStatus

PWD = "pass123456"


async def _non_superuser_admin(
    db: AsyncSession, client: AsyncClient, username: str = "admin2"
) -> dict[str, str]:
    """造一个 role=admin、is_superuser=False 的账号 —— 生产上 guoys 就是这种。"""
    user = User(
        username=username,
        email=f"{username}@test.com",
        hashed_password=hash_password(PWD),
        full_name="运营管理员",
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
        is_superuser=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    r = await client.post(
        "/api/v1/auth/login/json", json={"username": username, "password": PWD}
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


# ---------------------------------------------------------------------------
# 收紧面
# ---------------------------------------------------------------------------

async def test_non_superuser_cannot_promote_to_admin(
    db: AsyncSession, client: AsyncClient, sales_user: User
) -> None:
    headers = await _non_superuser_admin(db, client)

    r = await client.patch(
        f"/api/v1/users/{sales_user.id}", headers=headers, json={"role": "admin"}
    )
    assert r.status_code == 403, r.text
    assert "超级管理员" in r.json()["detail"]


async def test_non_superuser_cannot_create_admin(
    db: AsyncSession, client: AsyncClient
) -> None:
    headers = await _non_superuser_admin(db, client)

    r = await client.post(
        "/api/v1/users",
        headers=headers,
        json={
            "username": "sneaky",
            "email": "sneaky@test.com",
            "password": PWD,
            "full_name": "偷偷管理员",
            "role": "admin",
        },
    )
    assert r.status_code == 403, r.text


async def test_non_superuser_cannot_demote_another_admin(
    db: AsyncSession, client: AsyncClient
) -> None:
    """反向也要挡：把管理员降级 = 让对手失去管理能力。"""
    headers = await _non_superuser_admin(db, client, username="admin2")
    other = await _non_superuser_admin(db, client, username="admin3")

    # admin3 的 id 从 /users 列表里拿
    rows = (await client.get("/api/v1/users", headers=headers)).json()
    target = next(u for u in rows["items"] if u["username"] == "admin3")

    r = await client.patch(
        f"/api/v1/users/{target['id']}", headers=headers, json={"role": "viewer"}
    )
    assert r.status_code == 403, r.text
    del other


async def test_non_superuser_cannot_disable_admin(
    db: AsyncSession, client: AsyncClient
) -> None:
    headers = await _non_superuser_admin(db, client, username="admin2")
    await _non_superuser_admin(db, client, username="admin3")
    rows = (await client.get("/api/v1/users", headers=headers)).json()
    target = next(u for u in rows["items"] if u["username"] == "admin3")

    r = await client.delete(f"/api/v1/users/{target['id']}", headers=headers)
    assert r.status_code == 403, r.text


async def test_non_superuser_cannot_force_logout_admin(
    db: AsyncSession, client: AsyncClient
) -> None:
    headers = await _non_superuser_admin(db, client, username="admin2")
    await _non_superuser_admin(db, client, username="admin3")
    rows = (await client.get("/api/v1/users", headers=headers)).json()
    target = next(u for u in rows["items"] if u["username"] == "admin3")

    r = await client.post(f"/api/v1/users/{target['id']}/force-logout", headers=headers)
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 别收过头：普通管理动作照常
# ---------------------------------------------------------------------------

async def test_non_superuser_admin_can_still_manage_non_admin_roles(
    db: AsyncSession, client: AsyncClient, sales_user: User
) -> None:
    headers = await _non_superuser_admin(db, client)

    r = await client.patch(
        f"/api/v1/users/{sales_user.id}", headers=headers, json={"role": "finance"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "finance"


async def test_non_superuser_admin_can_set_profile_fields(
    db: AsyncSession, client: AsyncClient, sales_user: User
) -> None:
    headers = await _non_superuser_admin(db, client)

    r = await client.patch(
        f"/api/v1/users/{sales_user.id}", headers=headers, json={"full_name": "张三"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["full_name"] == "张三"


# ---------------------------------------------------------------------------
# superuser 不受影响
# ---------------------------------------------------------------------------

async def test_superuser_can_promote_and_demote(
    client: AsyncClient, admin_headers: dict[str, str], sales_user: User
) -> None:
    up = await client.patch(
        f"/api/v1/users/{sales_user.id}", headers=admin_headers, json={"role": "admin"}
    )
    assert up.status_code == 200, up.text

    down = await client.patch(
        f"/api/v1/users/{sales_user.id}", headers=admin_headers, json={"role": "sales"}
    )
    assert down.status_code == 200, down.text


async def test_superuser_can_create_admin(
    client: AsyncClient, admin_headers: dict[str, str]
) -> None:
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "admin9",
            "email": "admin9@test.com",
            "password": PWD,
            "full_name": "第二管理员",
            "role": "admin",
        },
    )
    assert r.status_code == 201, r.text
    # 建出来的仍是**非 superuser** 的管理员（superuser 只能在库里改）
    assert r.json()["is_superuser"] is False
