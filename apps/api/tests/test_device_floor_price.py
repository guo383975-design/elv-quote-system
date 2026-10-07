"""设备底价规则：底价 = 成本价 × 1.35（2026-10-05 用户定）。

三条口径（对应三条测试）：
  ① 新增设备：没填底价 → 按成本 × 1.35 自动设
  ② 改设备：改了成本、没显式给底价 → 底价跟着重算
  ③ 显式给了底价 → 以调用方为准（个别机型确实是特例），不被派生覆盖

另外导入路径也走同一规则（在 test_import_rules.py 里覆盖）。
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient


async def _mk(client: AsyncClient, headers, **over) -> dict:
    body = {"code": "FLR1", "name": "底价测试设备", "model": "FLR-1",
            "category": "network", "unit": "台", "reference_price": "2000",
            "cost_price": "1000"}
    body.update(over)
    r = await client.post("/api/v1/devices", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_create_derives_floor_from_cost(
    client: AsyncClient, admin_headers
) -> None:
    """① 建号：成本 1000 → 底价 1350。"""
    d = await _mk(client, admin_headers)
    assert d["min_price"] is not None, d
    assert Decimal(str(d["min_price"])) == Decimal("1350.00"), d


async def test_update_cost_recomputes_floor(
    client: AsyncClient, admin_headers
) -> None:
    """② 改成本 2000 → 底价重算为 2700。"""
    d = await _mk(client, admin_headers, code="FLR2")
    r = await client.patch(f"/api/v1/devices/{d['id']}", headers=admin_headers,
                           json={"cost_price": "2000"})
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["min_price"])) == Decimal("2700.00"), r.json()


async def test_explicit_floor_wins(client: AsyncClient, admin_headers) -> None:
    """③ 显式给底价（同时改成本）→ 用显式值，不被 2000×1.35 覆盖。"""
    d = await _mk(client, admin_headers, code="FLR3")
    r = await client.patch(f"/api/v1/devices/{d['id']}", headers=admin_headers,
                           json={"cost_price": "2000", "min_price": "9999"})
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["min_price"])) == Decimal("9999.00"), r.json()

    # 顺带：建号时显式给底价也一样生效
    d2 = await _mk(client, admin_headers, code="FLR4", min_price="888",
                   cost_price="1000")
    assert Decimal(str(d2["min_price"])) == Decimal("888.00"), d2
