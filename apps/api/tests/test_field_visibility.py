"""字段级可见性（成本/利润）＝ 权限矩阵里的两行（2026-10-05）。

以前是独立的「数据可见性」设置 + `core/visibility` 模块；用户要求"合并成一个界面"，
于是：

  · `data_cost` / `data_profit` 进了 `PERMISSION_MATRIX`（只有 `read` 一个动作）；
  · `can_see_cost` / `can_see_profit` 就是 `can(role, "data_cost"/"data_profit", READ)`
    —— 判定只剩一处，不再有第二份口径；
  · 配置走同一个 `permissions.overrides`，且语义改为**按资源合并**
    （只覆盖勾过的行，其余仍走默认矩阵）。

最后一条是本文件里最要紧的断言：**只调字段行，不能连带冻结功能权限**
（旧的"整角色替换"语义会，那正是合并时必须先改掉的东西）。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.core.permissions import (
    FIELD_RESOURCES,
    PERMISSION_MATRIX,
    can,
    can_see_cost,
    can_see_profit,
    permission_catalog,
    visible_resources,
)

# 用户口径：工程部只看设备清单、销售只看销售价、财务可看利润、总经理看全部
EXPECTED_COST = {"admin", "manager", "finance", "purchaser"}
EXPECTED_PROFIT = {"admin", "manager", "finance"}


def test_field_rows_are_in_the_matrix_with_only_read() -> None:
    catalog = permission_catalog()
    for res in FIELD_RESOURCES:
        assert res in PERMISSION_MATRIX, f"{res} 不在权限矩阵里"
        assert catalog[res] == ["read"], f"{res} 只应有 read 一个动作：{catalog[res]}"


def test_default_matrix_matches_requirement() -> None:
    roles = ("admin", "manager", "finance", "purchaser", "estimator", "sales", "viewer")
    cost = {r for r in roles if can_see_cost(role=r)}
    profit = {r for r in roles if can_see_profit(role=r)}
    assert cost == EXPECTED_COST, cost
    assert profit == EXPECTED_PROFIT, profit


def test_field_rows_do_not_pollute_menu_resources() -> None:
    """字段级资源不是"能导航的模块"，不该出现在菜单/可见资源里。"""
    for role in ("admin", "finance", "purchaser", "sales"):
        res = visible_resources(role)
        assert not (res & FIELD_RESOURCES), f"{role} 的可见资源里有字段级行：{res & FIELD_RESOURCES}"


async def test_field_visibility_configurable_via_matrix(
    client: AsyncClient, admin_headers, est_headers
) -> None:
    """在**权限矩阵**里给工程部勾上"成本/进价" → 立刻能看到设备进价。"""
    dev = (await client.post("/api/v1/devices", headers=admin_headers, json={
        "code": "VIS2", "name": "字段可见性设备", "model": "VIS-2",
        "category": "video", "unit": "台", "reference_price": "500",
        "cost_price": "300"})).json()
    assert dev["cost_price"] is not None

    r0 = (await client.get("/api/v1/devices", headers=est_headers,
                           params={"keyword": "VIS2"})).json()
    assert r0["items"][0]["cost_price"] is None, "工程部默认不该看到进价"

    # 只覆盖这一行（矩阵编辑器提交的也是"资源 → 动作"这种结构）
    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {"estimator": {"data_cost": ["read"]}}}})
    assert r.status_code == 200, r.text
    assert can_see_cost(role="estimator") is True

    r1 = (await client.get("/api/v1/devices", headers=est_headers,
                           params={"keyword": "VIS2"})).json()
    assert r1["items"][0]["cost_price"] is not None, "勾了却看不到 → 配置没生效"

    # 还原
    await client.patch("/api/v1/settings", headers=admin_headers,
                       json={"permissions": {"overrides": {}}})
    r2 = (await client.get("/api/v1/devices", headers=est_headers,
                           params={"keyword": "VIS2"})).json()
    assert r2["items"][0]["cost_price"] is None


async def test_overriding_one_row_does_not_freeze_other_permissions(
    client: AsyncClient, admin_headers
) -> None:
    """**关键**：只覆盖字段行，不能连带冻结该角色的功能权限。

    旧的"整角色替换"语义会：给采购开个利润，就把采购的设备库/供应商权限全清掉。
    改成"按资源合并"之后，没勾过的行仍走默认矩阵。
    """
    # 先记下采购的默认功能权限
    assert can("purchaser", "device_library", "read") is True
    assert can("purchaser", "supplier", "read") is True
    assert can("purchaser", "quote", "read") is False

    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "permissions": {"overrides": {"purchaser": {"data_profit": ["read"]}}}})
    assert r.status_code == 200, r.text
    try:
        assert can_see_profit(role="purchaser") is True, "利润没开上"
        # 功能权限必须原样保留
        assert can("purchaser", "device_library", "read") is True, "设备库权限被连带清掉了"
        assert can("purchaser", "supplier", "read") is True, "供应商权限被连带清掉了"
        assert can("purchaser", "quote", "read") is False, "不该凭空多出报价权限"
    finally:
        await client.patch("/api/v1/settings", headers=admin_headers,
                           json={"permissions": {"overrides": {}}})
    assert can_see_profit(role="purchaser") is False


async def test_profit_dashboard_follows_the_field_row(
    client: AsyncClient, finance_headers, est_headers, sales_headers
) -> None:
    """利润看板跟着 data_profit 那一行：财务 200，工程/销售 403。"""
    assert (await client.get("/api/v1/reports/profit-dashboard",
                             headers=finance_headers)).status_code == 200
    for h in (est_headers, sales_headers):
        r = await client.get("/api/v1/reports/profit-dashboard", headers=h)
        assert r.status_code == 403, r.text
        assert "利润" in r.json()["detail"], r.json()


async def test_my_permissions_exposes_both_flags(
    client: AsyncClient, sales_headers, finance_headers
) -> None:
    """前端靠这两个布尔值隐藏成本/利润入口。"""
    s = (await client.get("/api/v1/auth/me/permissions", headers=sales_headers)).json()
    assert s["can_see_cost"] is False and s["can_see_profit"] is False, s
    f = (await client.get("/api/v1/auth/me/permissions", headers=finance_headers)).json()
    assert f["can_see_cost"] is True and f["can_see_profit"] is True, f
