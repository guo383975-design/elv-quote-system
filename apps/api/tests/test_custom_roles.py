"""自定义角色 测试（2026-10-05）。

证六件事：
  ① 能建自定义角色，并出现在角色清单里；
  ② 新角色**默认没有任何权限**（不给就进不去任何模块），要显式勾；
  ③ 勾了权限后立刻能用（含菜单可见资源）；
  ④ 能配数据可见性（成本/利润）；
  ⑤ 标识不合法 / 撞内置 / 重名 → 422；给用户分不存在的角色 → 422；
  ⑥ **还有人在用的角色不许删**（否则那些人会变成"角色不存在"）。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.roles import known_role_keys, role_label


async def _make_role(client: AsyncClient, admin_headers, key: str, name: str) -> None:
    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "roles": {"custom": [{"key": key, "name": name}]}})
    assert r.status_code == 200, r.text


async def _login(client: AsyncClient, username: str, password: str) -> dict:
    tok = (await client.post("/api/v1/auth/login/json", json={
        "username": username, "password": password})).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


async def test_create_custom_role_and_assign(
    client: AsyncClient, admin_headers
) -> None:
    """①②③ 建角色 → 默认为空 → 勾权限后立刻能用。"""
    await _make_role(client, admin_headers, "engineering", "工程部")

    roles = (await client.get("/api/v1/users/roles", headers=admin_headers)).json()
    keys = {r["key"] for r in roles}
    assert "engineering" in keys, roles
    assert any(r["key"] == "engineering" and r["name"] == "工程部" and not r["builtin"]
               for r in roles), roles

    # 分给人（角色现在由 schema 校验，未知角色会 422）
    created = await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "eng01", "email": "eng01@example.com",
        "password": "Eng12345!", "role": "engineering"})
    assert created.status_code in (200, 201), created.text
    h = await _login(client, "eng01", "Eng12345!")

    me = (await client.get("/api/v1/auth/me", headers=h)).json()
    assert me["role"] == "engineering", me

    # ② 默认没有任何权限：进不去报价单
    assert (await client.get("/api/v1/quotes", headers=h)).status_code == 403
    perms = (await client.get("/api/v1/auth/me/permissions", headers=h)).json()
    assert perms["resources"] == [], perms

    # ③ 勾上"报价单：查看"→ 立刻能进
    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {
            "engineering": {"quote": ["read"], "device_library": ["read"]}}}})
    assert r.status_code == 200, r.text
    assert (await client.get("/api/v1/quotes", headers=h)).status_code == 200
    assert (await client.get("/api/v1/devices", headers=h)).status_code == 200
    perms2 = (await client.get("/api/v1/auth/me/permissions", headers=h)).json()
    assert set(perms2["resources"]) == {"quote", "device_library"}, perms2


async def test_custom_role_data_visibility(
    client: AsyncClient, admin_headers
) -> None:
    """④ 自定义角色也能配成本/利润可见性。"""
    await _make_role(client, admin_headers, "auditor", "审计")
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "aud01", "email": "aud01@example.com",
        "password": "Aud12345!", "role": "auditor"})
    h = await _login(client, "aud01", "Aud12345!")

    await client.patch("/api/v1/settings", headers=admin_headers, json={
        # 利润看板路由本身要求 project.read；字段级可见性（2026-10-05 起）
        # 就是矩阵里的 data_profit 行，不再是独立的 visibility 组
        "permissions": {"overrides": {"auditor": {
            "quote": ["read"], "project": ["read"], "data_profit": ["read"]}}}})
    try:
        perms = (await client.get("/api/v1/auth/me/permissions", headers=h)).json()
        assert perms["can_see_profit"] is True, perms
        assert (await client.get("/api/v1/reports/profit-dashboard",
                                 headers=h)).status_code == 200
    finally:
        await client.patch("/api/v1/settings", headers=admin_headers,
                           json={"permissions": {"overrides": {}}})


async def test_invalid_role_definitions_rejected(
    client: AsyncClient, admin_headers
) -> None:
    """⑤ 标识不合法 / 撞内置 / 给用户分不存在的角色 → 全部 422。"""
    bad_cases = [
        [{"key": "工程部", "name": "中文键"}],          # 标识规则
        [{"key": "a", "name": "太短"}],                  # 至少 2 位
        [{"key": "admin", "name": "撞内置"}],            # 内置角色
        [{"key": "newrole", "name": ""}],                # 名称必填
        [{"key": "okrole", "name": "重复"}, {"key": "okrole", "name": "重复"}],
    ]
    for custom in bad_cases:
        r = await client.patch("/api/v1/settings", headers=admin_headers,
                               json={"roles": {"custom": custom}})
        assert r.status_code == 422, f"{custom} 竟然通过了：{r.text}"

    bad_user = await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "norole01", "email": "norole01@example.com",
        "password": "No123456!", "role": "not_a_role"})
    assert bad_user.status_code == 422, bad_user.text


async def test_role_in_use_cannot_be_deleted(
    client: AsyncClient, admin_headers
) -> None:
    """⑥ 角色还有人在用 → 不许删；把人改走之后就能删。"""
    await _make_role(client, admin_headers, "temp_role", "临时角色")
    user = (await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "tmp01", "email": "tmp01@example.com",
        "password": "Tmp12345!", "role": "temp_role"})).json()

    blocked = await client.patch("/api/v1/settings", headers=admin_headers,
                                 json={"roles": {"custom": []}})
    assert blocked.status_code == 422, blocked.text
    assert "还有人在用" in blocked.json()["detail"], blocked.json()

    # 把人改成别的角色，再删
    await client.patch(f"/api/v1/users/{user['id']}", headers=admin_headers,
                       json={"role": "viewer"})
    ok = await client.patch("/api/v1/settings", headers=admin_headers,
                            json={"roles": {"custom": []}})
    assert ok.status_code == 200, ok.text
    assert "temp_role" not in known_role_keys()


def test_role_label_helper() -> None:
    assert role_label("admin") == "管理员"
    assert role_label("unknown_key") == "unknown_key"
