"""点位模板测试（2026-10-04）。

覆盖：CRUD / 编码冲突 / 套用（预演与写入）/ **安装单价只计一次/点** /
主设备回退 / 锁定单拒写 / 权限 / 缺设备行的降级报告。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote import Quote


async def _project(db: AsyncSession, code: str = "PRJ-PT-1") -> Project:
    p = Project(
        code=code, name="点位模板测试项目", customer_name="测试客户",
        project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.flush()
    return p


async def _quote(client: AsyncClient, headers, project_id, code="QT-PT-1") -> dict:
    r = await client.post(
        "/api/v1/quotes",
        headers=headers,
        json={
            "project_id": str(project_id), "code": code, "title": "点位模板测试单",
            "fee_base": "device_aux_install", "auxiliary_base": "device",
            "auxiliary_rate": "0.06", "design_rate": "0.02", "warranty_rate": "0.03",
            "mgmt_profit_rate": "0.08", "tax_rate": "0.09",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _template(client: AsyncClient, headers, devices, **over) -> dict:
    body = {
        "code": over.pop("code", "PT-DOME"),
        "name": "球机点位模板",
        "install_unit_price": "180",
        "items": [
            {"device_id": str(devices[0].id), "quantity": "1", "is_primary": True, "sort_order": 0},
            {"device_id": str(devices[1].id), "quantity": "1", "is_primary": False, "sort_order": 1},
            {"device_id": str(devices[2].id), "quantity": "2", "is_primary": False, "sort_order": 2},
        ],
    }
    body.update(over)
    r = await client.post("/api/v1/point-templates", json=body, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

async def test_template_crud(client: AsyncClient, db: AsyncSession, admin_headers, devices) -> None:
    tpl = await _template(client, admin_headers, devices)
    assert tpl["code"] == "PT-DOME"
    assert len(tpl["items"]) == 3
    # 详情里带设备投影（页面要显示"设备名 ×1 单价"）
    first = tpl["items"][0]
    assert first["device_name"] and first["device_reference_price"] is not None

    r = await client.get("/api/v1/point-templates", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["total"] == 1

    r = await client.patch(
        f"/api/v1/point-templates/{tpl['id']}",
        json={"name": "球机点位模板（修订）", "install_unit_price": "200",
              "items": [{"device_id": str(devices[0].id), "quantity": "1", "is_primary": True}]},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "球机点位模板（修订）"
    assert float(body["install_unit_price"]) == 200.0
    assert len(body["items"]) == 1, "items 传了要整表替换"

    r = await client.delete(f"/api/v1/point-templates/{tpl['id']}", headers=admin_headers)
    assert r.status_code == 204, r.text
    r = await client.get("/api/v1/point-templates", headers=admin_headers)
    assert r.json()["total"] == 0


async def test_duplicate_code_conflict(
    client: AsyncClient, admin_headers, devices
) -> None:
    await _template(client, admin_headers, devices, code="PT-DUP")
    r = await client.post(
        "/api/v1/point-templates",
        json={"code": "PT-DUP", "name": "重复编码", "items": []},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text


# ---------------------------------------------------------------------------
# 套用：安装单价只计一次/点
# ---------------------------------------------------------------------------

async def test_apply_dry_run_then_write_install_only_on_primary(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _project(db)
    await db.commit()
    q = await _quote(client, admin_headers, p.id)
    tpl = await _template(client, admin_headers, devices)

    payload = {"quote_id": q["id"], "point_count": "3", "dry_run": True}
    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply", json=payload, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    prev = r.json()
    assert prev["created"] == 0, "预演不能写库"
    by_code = {row["device_code"]: row for row in prev["rows"]}
    c0, c1, c2 = (d.code for d in devices[:3])
    assert by_code[c0]["quantity"] == 3.0, by_code[c0]
    assert by_code[c0]["install_unit_price"] == 180.0, "主设备行才有安装单价"
    assert by_code[c1]["install_unit_price"] == 0.0
    assert by_code[c2]["quantity"] == 6.0, "模板数量 2 × 点位 3"
    assert by_code[c2]["install_unit_price"] == 0.0
    # 安装合计只算一次/点：180 × 3
    assert prev["install_total"] == 540.0, prev["install_total"]

    detail = (await client.get(f"/api/v1/quotes/{q['id']}", headers=admin_headers)).json()
    assert detail["items"] == [], "预演后报价单仍是空的"

    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply",
        json={**payload, "dry_run": False},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 3

    # 测试内所有请求共用一个会话，读取前先失效缓存，避免读到预演阶段加载过的空 items
    db.expire_all()  # 同步方法（async 会话里只有 IO 方法可 await）
    detail = (await client.get(f"/api/v1/quotes/{q['id']}", headers=admin_headers)).json()
    items = detail["items"]
    assert len(items) == 3, detail
    # 按**设备编码**取行，不能按名称 —— fixture 里 V1001 与 V1008 同名（不同品牌），
    # 用名字做键会互相覆盖，之前就是被这个坑到过。
    by_code = {i["device_code"]: i for i in items}
    primary = by_code[c0]
    assert float(primary["quantity"]) == 3.0, primary
    assert float(primary["install_unit_price"]) == 180.0, primary
    assert float(primary["install_amount"]) == 540.0, primary
    for other in (c1, c2):
        assert float(by_code[other]["install_unit_price"]) == 0.0, "非主设备行不该计安装单价"


async def test_apply_reports_primary_fallback(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """没标主设备时退化为第一行，并明确告知（primary_fallback=true）。"""
    p = await _project(db, "PRJ-PT-2")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, code="QT-PT-2")
    tpl = await _template(
        client, admin_headers, devices, code="PT-NOPRIMARY",
        items=[
            {"device_id": str(devices[0].id), "quantity": "1", "sort_order": 0},
            {"device_id": str(devices[1].id), "quantity": "1", "sort_order": 1},
        ],
    )
    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply",
        json={"quote_id": q["id"], "point_count": "1", "dry_run": True},
        headers=admin_headers,
    )
    body = r.json()
    assert body["primary_fallback"] is True
    assert body["primary_device_code"] == devices[0].code
    assert sum(1 for row in body["rows"] if row["is_primary"]) == 1


async def test_apply_skips_rows_without_device(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """模板里有设备被真删（device_id 为空）→ 跳过并如实报告，不整单失败。"""
    p = await _project(db, "PRJ-PT-3")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, code="QT-PT-3")
    tpl = await _template(
        client, admin_headers, devices, code="PT-MISSING",
        items=[
            {"device_id": str(devices[0].id), "quantity": "1", "is_primary": True},
            {"device_id": None, "quantity": "1", "notes": "设备已被删除"},
        ],
    )
    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply",
        json={"quote_id": q["id"], "point_count": "2", "dry_run": True},
        headers=admin_headers,
    )
    body = r.json()
    assert len(body["rows"]) == 1 and len(body["skipped"]) == 1
    assert "设备已不存在" in body["skipped"][0]["reason"]


async def test_apply_rejects_locked_quote(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """已批准的报价单已锁定：套用要 409，提示先退回草稿（与 quotes.py 同一口径）。"""
    p = await _project(db, "PRJ-PT-4")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, code="QT-PT-4")
    quote = await db.get(Quote, UUID(q["id"]))
    quote.status = "approved"
    await db.commit()

    tpl = await _template(client, admin_headers, devices, code="PT-LOCKED")
    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply",
        json={"quote_id": q["id"], "point_count": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert "退回草稿" in r.json()["detail"]


async def test_apply_requires_quote_item_permission(
    client: AsyncClient, db: AsyncSession, admin_headers, viewer_headers, devices
) -> None:
    """只读角色能看模板，但不能借套用往报价单里写行。"""
    p = await _project(db, "PRJ-PT-5")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, code="QT-PT-5")
    tpl = await _template(client, admin_headers, devices, code="PT-PERM")

    r = await client.get("/api/v1/point-templates", headers=viewer_headers)
    assert r.status_code == 200, "只读角色应能看模板"

    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply",
        json={"quote_id": q["id"], "point_count": "1"},
        headers=viewer_headers,
    )
    assert r.status_code == 403, r.text


async def test_inactive_template_cannot_be_applied(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _project(db, "PRJ-PT-6")
    await db.commit()
    q = await _quote(client, admin_headers, p.id, code="QT-PT-6")
    tpl = await _template(client, admin_headers, devices, code="PT-OFF")
    r = await client.patch(
        f"/api/v1/point-templates/{tpl['id']}", json={"is_active": False}, headers=admin_headers
    )
    assert r.status_code == 200, r.text
    r = await client.post(
        f"/api/v1/point-templates/{tpl['id']}/apply",
        json={"quote_id": q["id"], "point_count": "1"},
        headers=admin_headers,
    )
    assert r.status_code == 409, r.text
    assert "停用" in r.json()["detail"]


def test_install_price_precision_is_two_decimals() -> None:
    """模板安装单价按 2 位小数存（Numeric(12,2)），避免套用后出现半厘钱。"""
    from app.models.point_template import PointTemplate

    col = PointTemplate.__table__.c.install_unit_price
    assert col.type.precision == 12 and col.type.scale == 2
    assert Decimal("180.00") == Decimal("180")



