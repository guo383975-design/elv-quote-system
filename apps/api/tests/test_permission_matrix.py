"""权限矩阵可调 测试（2026-10-05）。

用户选了"先做可调**现有角色**的权限"、"默认值保持现状"。这里证五件事：

  ① 默认矩阵与以前**完全一致**（没人在设置里改过时）；
  ② 改了**立刻生效**（含菜单可见资源的变化）；
  ③ admin 不可被覆盖（防自锁 —— 这是最要紧的一条）；
  ④ 非法资源/动作被拦（422），不会因为错别字多给或少给整块权限；
  ⑤ 清空覆盖 = 回到默认。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.permissions import (
    PERMISSION_MATRIX,
    Role,
    can,
    default_matrix_for,
    set_permission_overrides,
)

# 默认矩阵的几个关键格子（"保持现状"的锚点：改动默认值会在这里失败）
EXPECTED_DEFAULTS = {
    ("sales", "quote", "read"): True,
    ("sales", "quote", "create"): True,
    ("sales", "device_library", "read"): True,
    ("sales", "device_library", "update"): False,
    ("sales", "user", "read"): False,
    ("finance", "quote", "read"): True,
    ("finance", "quote", "create"): False,
    ("manager", "quote", "approve"): True,
    ("manager", "quote", "create"): False,
    ("viewer", "quote", "read"): True,
    ("viewer", "quote", "create"): False,
    ("estimator", "device_library", "update"): True,
    ("purchaser", "supplier", "create"): True,
}


def test_defaults_unchanged() -> None:
    """① 默认矩阵保持现状（这条失败说明有人动了默认值）。"""
    set_permission_overrides(None)
    wrong = {
        k: can(*k) for k, want in EXPECTED_DEFAULTS.items() if can(*k) is not want
    }
    assert not wrong, f"默认矩阵变了：{wrong}"


async def test_matrix_endpoint_shape(client: AsyncClient, admin_headers) -> None:
    r = await client.get("/api/v1/settings/permission-matrix", headers=admin_headers)
    assert r.status_code == 200, r.text
    d = r.json()
    assert "admin" not in d["editable_roles"], "admin 不该出现在可编辑角色里"
    assert set(d["editable_roles"]) == {str(x) for x in Role if x is not Role.ADMIN}
    assert d["catalog"]["quote"] and "approve" in d["catalog"]["quote"]
    assert d["defaults"]["sales"]["quote"], d["defaults"]["sales"]
    assert d["overrides"] == {}, d["overrides"]


async def test_matrix_endpoint_requires_permission(
    client: AsyncClient, sales_headers
) -> None:
    r = await client.get("/api/v1/settings/permission-matrix", headers=sales_headers)
    assert r.status_code == 403, r.text


async def test_override_takes_effect_immediately(
    client: AsyncClient, admin_headers, sales_headers
) -> None:
    """② 把销售限成"只能看报价" → 立刻进不去客户档案；清空后恢复。"""
    # 改之前：销售能看客户档案
    assert (await client.get("/api/v1/customers", headers=sales_headers)).status_code == 200

    # ⚠ 语义（2026-10-05 起）：覆盖是**按资源合并** —— 没提到的行仍走默认矩阵。
    # 所以要"只让销售看报价"，必须把其余资源**显式给空数组**
    # （界面上等价于把那一行的勾全取消，矩阵编辑器提交的就是这种结构）。
    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {
            "sales": {
                "quote": ["read", "create", "update", "export", "compute"],
                "customer": [], "device_library": [], "project": [],
                "labor_rate": [], "point_template": [], "quota": [],
                "subsystem": [], "supplier": [], "quote_item": [],
            },
        }}})
    assert r.status_code == 200, r.text

    # 立刻生效：客户档案 403、报价单仍 200
    denied = await client.get("/api/v1/customers", headers=sales_headers)
    assert denied.status_code == 403, denied.text
    assert (await client.get("/api/v1/quotes", headers=sales_headers)).status_code == 200

    # 菜单可见资源也跟着变（/me/permissions 的 resources）
    perms = (await client.get("/api/v1/auth/me/permissions", headers=sales_headers)).json()
    assert "customer" not in perms["resources"], perms["resources"]
    assert "quote" in perms["resources"], perms["resources"]

    # ⑤ 清空覆盖 → 回到默认
    r2 = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {}}})
    assert r2.status_code == 200, r2.text
    assert (await client.get("/api/v1/customers", headers=sales_headers)).status_code == 200
    for k, want in EXPECTED_DEFAULTS.items():
        assert can(*k) is want, f"清空覆盖后 {k} 应回到 {want}"


async def test_admin_cannot_be_overridden(client: AsyncClient, admin_headers) -> None:
    """③ admin 不可覆盖 —— 否则一旦取消自己的 setting.update，谁都改不回来。"""
    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {"admin": {"quote": []}}}})
    assert r.status_code == 422, r.text
    assert "admin" in r.json()["detail"]
    assert can("admin", "quote", "read") is True
    assert can("admin", "setting", "update") is True


async def test_invalid_resource_or_action_rejected(
    client: AsyncClient, admin_headers
) -> None:
    """④ 错别字要拦：否则一次手滑可能给错一整块权限。"""
    bad_res = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {"sales": {"quotez": ["read"]}}}})
    assert bad_res.status_code == 422 and "quotez" in bad_res.json()["detail"]

    bad_act = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {"sales": {"quote": ["read", "approve2"]}}}})
    assert bad_act.status_code == 422 and "approve2" in bad_act.json()["detail"]

    bad_role = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {"engineer": {"quote": ["read"]}}}})
    assert bad_role.status_code == 422 and "engineer" in bad_role.json()["detail"]

    # 一次都没生效
    assert can("sales", "quote", "read") is True


def test_default_matrix_for_matches_matrix() -> None:
    """辅助函数与实际判定一致（前端"默认值/恢复默认"靠它）。"""
    for role in ("sales", "estimator", "finance", "purchaser", "manager", "viewer"):
        d = default_matrix_for(role)
        for res, actions in d.items():
            for a in actions:
                assert can(role, res, a) is True, f"{role}.{res}.{a} 默认应为 True"
        # 该资源在默认矩阵里、但没给这个角色的动作，必须为 False
        for res, matrix_actions in PERMISSION_MATRIX.items():
            for action in matrix_actions:
                if str(action) not in d.get(res, []):
                    if Role.ADMIN in matrix_actions[action]:
                        continue
                    assert can(role, res, action) is False, f"{role}.{res}.{action}"
