"""① 安装调试费手动填写（不再从设备库带入）② 子系统可同类多套（2026-10-05）。

这两条都是**行为级**验证 ✓ —— 不只看源码有没有改，而是真的走一遍接口：
  · 建一台安装单价 300 的设备 → 加进报价单 → 明细行的安装单价必须是 **0**（手动填 ✓）
  · 同一项目：同类型 + 不同名称 → **能建**（201 ✓）；同类型 + 同名 → 409 ✓
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient


async def _project(client: AsyncClient, headers) -> dict:
    r = await client.post("/api/v1/projects", headers=headers, json={
        "code": f"T{uuid.uuid4().hex[:8].upper()}",
        "name": "测试项目",
        "customer_name": "测试客户",
    })
    assert r.status_code in (200, 201), r.text
    return r.json()


async def test_install_price_is_manual_not_from_device(
    client: AsyncClient, admin_headers
) -> None:
    """设备库里的安装单价不再自动带进明细行（用户 2026-10-05 要求手动填写）。"""
    dev = await client.post("/api/v1/devices", headers=admin_headers, json={
        "code": f"INS{uuid.uuid4().hex[:6].upper()}", "name": "安装费测试设备",
        "model": "INS-1", "category": "network", "unit": "台",
        "reference_price": "1000", "cost_price": "600",
        "install_unit_price": "300",          # 库里故意给一个值 ✗
    })
    assert dev.status_code == 201, dev.text
    device_id = dev.json()["id"]

    prj = await _project(client, admin_headers)
    quote = await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": prj["id"], "name": "安装费测试报价",
    })
    assert quote.status_code in (200, 201), quote.text
    quote_id = quote.json()["id"]

    r = await client.post(f"/api/v1/quotes/{quote_id}/items", headers=admin_headers,
                          json={"device_id": device_id, "name": "安装费测试行",
                                "unit": "台", "quantity": 2})
    assert r.status_code in (200, 201), r.text
    item = r.json()
    assert float(item.get("install_unit_price") or 0) == 0, (
        f"明细行的安装单价应当为 0（手动填），实际 {item.get('install_unit_price')} ✗"
    )

    # 手动填进去要能生效（不然"手动"就是空话）
    upd = await client.patch(f"/api/v1/quotes/{quote_id}/items/{item['id']}",
                             headers=admin_headers, json={"install_unit_price": "260"})
    assert upd.status_code == 200, upd.text
    assert float(upd.json().get("install_unit_price") or 0) == 260.0, upd.json()

    await client.delete(f"/api/v1/projects/{prj['id']}", headers=admin_headers)


async def test_subsystem_same_type_different_name_allowed(
    client: AsyncClient, admin_headers
) -> None:
    """子系统：同类型不同名称可以多套 ✓；同名同类型才是重复 ✓。"""
    prj = await _project(client, admin_headers)

    first = await client.post("/api/v1/subsystems", headers=admin_headers, json={
        "project_id": prj["id"], "subsystem_type": "video_surveillance",
        "custom_name": "一期监控",
    })
    assert first.status_code == 201, first.text

    # 同类型 + 不同名称 → 应当允许（这正是用户说的"新增子系统无法增加" ✗）
    second = await client.post("/api/v1/subsystems", headers=admin_headers, json={
        "project_id": prj["id"], "subsystem_type": "video_surveillance",
        "custom_name": "二期监控",
    })
    assert second.status_code == 201, (
        f"同类型不同名称应当能建，实际 {second.status_code} {second.text}"
    )

    # 同名同类型 → 409
    dup = await client.post("/api/v1/subsystems", headers=admin_headers, json={
        "project_id": prj["id"], "subsystem_type": "video_surveillance",
        "custom_name": "一期监控",
    })
    assert dup.status_code == 409, dup.text

    # 默认子系统（不带 custom_name 的那套）要能删掉
    await client.delete(f"/api/v1/subsystems/{first.json()['id']}", headers=admin_headers)
    await client.delete(f"/api/v1/projects/{prj['id']}", headers=admin_headers)
