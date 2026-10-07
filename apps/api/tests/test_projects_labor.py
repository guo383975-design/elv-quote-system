"""项目 / 子系统 / 人工信息价 / 定额 / 元数据 endpoint 测试."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# 项目 CRUD
# ---------------------------------------------------------------------------

async def test_create_and_get_project(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "code": "PRJ-202609-9001",
            "name": "宁波XX大厦智能化弱电工程",
            "customer_name": "宁波XX置业有限公司",
            "customer_contact": "陈工",
            "project_type": "commercial",
            "total_area_sqm": "38000.00",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "PRJ-202609-9001"
    assert body["status"] == "draft"

    got = await client.get(f"/api/v1/projects/{body['id']}", headers=admin_headers)
    assert got.status_code == 200
    assert got.json()["customer_name"] == "宁波XX置业有限公司"


async def test_create_project_duplicate_code_409(client: AsyncClient, admin_headers) -> None:
    payload = {"code": "PRJ-DUP-1", "name": "A", "customer_name": "B"}
    assert (await client.post("/api/v1/projects", headers=admin_headers, json=payload)).status_code == 201
    assert (await client.post("/api/v1/projects", headers=admin_headers, json=payload)).status_code == 409


async def test_auto_code_does_not_collide_after_delete(
    client: AsyncClient, admin_headers
) -> None:
    """自动编号必须按「已有**最大**编号 + 1」算，不能按「本月行数 + 1」算。

    删项目走的是硬删除，行数会回退：建过 0001/0002 之后删掉 0001，
    count+1 会重新算出 0002，而 0002 还在 → 撞唯一索引。
    （_next_project_code 之前就是这么写的，而且根本没被调用过。）
    """
    ym = date.today().strftime("%Y%m")
    prefix = f"PRJ-{ym}-"
    a = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"code": f"{prefix}0001", "name": "撞号甲", "customer_name": "客户"},
    )
    assert a.status_code == 201, a.text
    b = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"code": f"{prefix}0002", "name": "撞号乙", "customer_name": "客户"},
    )
    assert b.status_code == 201, b.text

    d = await client.delete(f"/api/v1/projects/{a.json()['id']}", headers=admin_headers)
    assert d.status_code == 204, d.text

    r = await client.post("/api/v1/projects/auto-code", headers=admin_headers)
    assert r.status_code == 200, r.text
    generated = r.json()["code"]
    assert generated != b.json()["code"], f"自动编号撞上仍然存在的 {generated}"
    assert int(generated.rsplit("-", 1)[1]) > 2, generated

    # 拿这个编号真去建一单：撞号的话这里会失败
    created = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"code": generated, "name": "撞号丙", "customer_name": "客户"},
    )
    assert created.status_code == 201, created.text


async def test_lifecycle_date_is_coerced_to_date(client: AsyncClient, admin_headers) -> None:
    """schema 里是 datetime，DB 列是 Date — 必须正常落库。"""
    r = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "code": "PRJ-DATE-1",
            "name": "日期测试",
            "customer_name": "客户",
            "expected_start": "2026-10-15T00:00:00",
        },
    )
    assert r.status_code == 201, r.text


async def test_list_projects_keyword_and_pagination(client: AsyncClient, admin_headers) -> None:
    for i in range(3):
        await client.post(
            "/api/v1/projects",
            headers=admin_headers,
            json={"code": f"PRJ-LIST-{i}", "name": f"鄞州项目{i}",
                  "customer_name": "同一客户"},
        )
    r = await client.get(
        "/api/v1/projects", headers=admin_headers,
        params={"keyword": "鄞州", "page": 1, "page_size": 2},
    )
    body = r.json()
    assert body["total"] == 3
    assert len(body["items"]) == 2
    assert body["page_size"] == 2


async def test_filter_projects_by_status(client: AsyncClient, admin_headers) -> None:
    await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-ST-1", "name": "状态测试", "customer_name": "客户"},
    )
    r = await client.get("/api/v1/projects", headers=admin_headers, params={"status": "draft"})
    assert r.json()["total"] >= 1
    r2 = await client.get("/api/v1/projects", headers=admin_headers, params={"status": "won"})
    assert r2.json()["total"] == 0


async def test_update_project(client: AsyncClient, admin_headers) -> None:
    created = (await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-UPD-1", "name": "旧名", "customer_name": "客户"},
    )).json()

    r = await client.patch(
        f"/api/v1/projects/{created['id']}", headers=admin_headers,
        json={"name": "新名", "status": "quoting"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "quoting"

    got = await client.get(f"/api/v1/projects/{created['id']}", headers=admin_headers)
    # ProjectOut 不含 status 之外的细节，重新取一次确认已持久化
    assert got.json()["status"] == "quoting"


async def test_viewer_cannot_update_project(client: AsyncClient, admin_headers, viewer_headers) -> None:
    created = (await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-RO-1", "name": "只读测试", "customer_name": "客户"},
    )).json()
    r = await client.patch(
        f"/api/v1/projects/{created['id']}", headers=viewer_headers, json={"name": "x"}
    )
    assert r.status_code == 403


async def test_delete_project_cascades_quote(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = (await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-DEL-1", "name": "待删", "customer_name": "客户"},
    )).json()
    q = (await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": p["id"], "code": "QT-DEL-1", "title": "t"},
    )).json()

    r = await client.delete(f"/api/v1/projects/{p['id']}", headers=admin_headers)
    assert r.status_code == 204

    assert (await client.get(f"/api/v1/quotes/{q['id']}", headers=admin_headers)).status_code == 404


# ---------------------------------------------------------------------------
# 子系统
# ---------------------------------------------------------------------------

async def test_create_subsystem_and_reject_duplicate_type(
    client: AsyncClient, admin_headers
) -> None:
    # 这个用例测的是"新建子系统 + 重复拒绝"，所以要一个**没有默认专业**的干净项目
    # （2026-10-04 起新建项目会自带 6 个常用专业）。
    p = (await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-SUB-1", "name": "子系统测试", "customer_name": "客户",
              "create_default_subsystems": False},
    )).json()

    payload = {"project_id": p["id"], "subsystem_type": "video_surveillance",
               "custom_name": "视频监控", "sort_order": 0}
    assert (await client.post("/api/v1/subsystems", headers=admin_headers, json=payload)).status_code == 201

    dup = await client.post("/api/v1/subsystems", headers=admin_headers, json=payload)
    assert dup.status_code == 409


async def test_all_ten_subsystem_types_accepted(client: AsyncClient, admin_headers) -> None:
    # 同理：10 个专业要一个个手工建，得先关掉默认创建
    p = (await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-SUB-10", "name": "十子系统", "customer_name": "客户",
              "create_default_subsystems": False},
    )).json()

    types = [
        "video_surveillance", "access_control", "intrusion", "structured_cabling",
        "network", "broadcast", "conference", "server_room", "parking", "info_publish",
    ]
    for i, t in enumerate(types):
        r = await client.post(
            "/api/v1/subsystems", headers=admin_headers,
            json={"project_id": p["id"], "subsystem_type": t, "sort_order": i},
        )
        assert r.status_code == 201, f"{t}: {r.text}"

    listing = await client.get(
        "/api/v1/subsystems", headers=admin_headers, params={"project_id": p["id"]}
    )
    assert len(listing.json()) == 10


async def test_delete_subsystem(client: AsyncClient, admin_headers) -> None:
    p = (await client.post(
        "/api/v1/projects", headers=admin_headers,
        json={"code": "PRJ-SUB-DEL", "name": "x", "customer_name": "c",
              "create_default_subsystems": False},
    )).json()
    s = (await client.post(
        "/api/v1/subsystems", headers=admin_headers,
        json={"project_id": p["id"], "subsystem_type": "network"},
    )).json()

    assert (await client.delete(f"/api/v1/subsystems/{s['id']}", headers=admin_headers)).status_code == 204
    assert (await client.get(f"/api/v1/subsystems/{s['id']}", headers=admin_headers)).status_code == 404


# ---------------------------------------------------------------------------
# 子系统 ↔ 供货商
# ---------------------------------------------------------------------------

async def _create_project_and_supplier(
    ac: AsyncClient, headers: dict, code: str
) -> tuple[str, str]:
    p = (await ac.post(
        "/api/v1/projects", headers=headers,
        # 关掉默认专业：这些用例要自己手工建子系统来测 supplier_id
        json={"code": code, "name": "子系统-供应商测试", "customer_name": "测试客户",
              "create_default_subsystems": False},
    )).json()
    s = (await ac.post(
        "/api/v1/suppliers", headers=headers,
        json={"code": f"SUP-{code[-4:]}", "name": f"供应商-{code}"},
    )).json()
    return p["id"], s["id"]


async def test_create_subsystem_with_supplier_id(
    client: AsyncClient, admin_headers
) -> None:
    """创建子系统时直接挂供货商 —— 一条 INSERT 把 supplier_id 写进去。"""
    project_id, supplier_id = await _create_project_and_supplier(
        client, admin_headers, "PRJ-SUBSUP-1"
    )
    r = await client.post(
        "/api/v1/subsystems",
        headers=admin_headers,
        json={
            "project_id": project_id,
            "subsystem_type": "video_surveillance",
            "supplier_id": supplier_id,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["supplier_id"] == supplier_id


async def test_patch_subsystem_supplier_id_attach_clear(
    client: AsyncClient, admin_headers
) -> None:
    """PATCH 三种语义：挂上 → 改 → 解除。"""
    project_id, supplier_id = await _create_project_and_supplier(
        client, admin_headers, "PRJ-SUBSUP-2"
    )
    sub = (await client.post(
        "/api/v1/subsystems", headers=admin_headers,
        json={"project_id": project_id, "subsystem_type": "network"},
    )).json()
    assert sub["supplier_id"] is None

    # 1. 挂上
    r = await client.patch(
        f"/api/v1/subsystems/{sub['id']}",
        headers=admin_headers, json={"supplier_id": supplier_id},
    )
    assert r.status_code == 200, r.text
    assert r.json()["supplier_id"] == supplier_id

    # 2. 解除（传 null）
    r = await client.patch(
        f"/api/v1/subsystems/{sub['id']}",
        headers=admin_headers, json={"supplier_id": None},
    )
    assert r.status_code == 200, r.text
    assert r.json()["supplier_id"] is None

    # 3. PATCH 不带 supplier_id 应该不动它
    sub_with = (await client.post(
        "/api/v1/subsystems", headers=admin_headers,
        json={"project_id": project_id, "subsystem_type": "broadcast",
              "supplier_id": supplier_id},
    )).json()
    assert sub_with["supplier_id"] == supplier_id
    r = await client.patch(
        f"/api/v1/subsystems/{sub_with['id']}",
        headers=admin_headers, json={"custom_name": "公共广播（改名测试）"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["supplier_id"] == supplier_id  # 没动
    assert r.json()["custom_name"] == "公共广播（改名测试）"


async def test_subsystem_rejects_inactive_or_missing_supplier(
    client: AsyncClient, admin_headers
) -> None:
    """挂一个**不存在**或**软删掉**的 supplier_id，必须 400，不能静默接受。"""
    project_id, supplier_id = await _create_project_and_supplier(
        client, admin_headers, "PRJ-SUBSUP-3"
    )
    # 软删这个 supplier
    d = await client.delete(f"/api/v1/suppliers/{supplier_id}", headers=admin_headers)
    assert d.status_code == 204

    # 已停用 → 400
    r = await client.post(
        "/api/v1/subsystems", headers=admin_headers,
        json={"project_id": project_id, "subsystem_type": "parking",
              "supplier_id": supplier_id},
    )
    assert r.status_code == 400, r.text

    # 不存在的 UUID → 400
    r = await client.post(
        "/api/v1/subsystems", headers=admin_headers,
        json={"project_id": project_id, "subsystem_type": "server_room",
              "supplier_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 400, r.text


async def test_subsystem_out_exposes_supplier_id(
    client: AsyncClient, admin_headers
) -> None:
    """SubsystemOut 必须把 supplier_id 透出来，前端下拉编辑靠这个字段。"""
    project_id, supplier_id = await _create_project_and_supplier(
        client, admin_headers, "PRJ-SUBSUP-4"
    )
    create = await client.post(
        "/api/v1/subsystems", headers=admin_headers,
        json={"project_id": project_id, "subsystem_type": "intrusion",
              "supplier_id": supplier_id, "notes": "测试备注"},
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["supplier_id"] == supplier_id
    assert body["notes"] == "测试备注"

    # GET 也要回 supplier_id
    sid = body["id"]
    fetched = await client.get(f"/api/v1/subsystems/{sid}", headers=admin_headers)
    assert fetched.status_code == 200
    assert fetched.json()["supplier_id"] == supplier_id
    assert fetched.json()["notes"] == "测试备注"


# ---------------------------------------------------------------------------
# 人工市场信息价
# ---------------------------------------------------------------------------

async def test_current_labor_rates(client: AsyncClient, admin_headers, labor_rates) -> None:
    r = await client.get("/api/v1/labor-rates/current", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["region"] == "ningbo"
    assert body["rates"]["class_a"]["price_per_day"] == 150.00
    assert body["rates"]["class_b"]["price_per_day"] == 163.00
    assert body["rates"]["class_c"]["price_per_day"] == 186.00


async def test_current_labor_rates_404_when_empty(client: AsyncClient, admin_headers) -> None:
    r = await client.get("/api/v1/labor-rates/current", headers=admin_headers)
    assert r.status_code == 404


async def test_create_new_labor_rate_switches_current_flag(
    client: AsyncClient, db: AsyncSession, admin_headers, labor_rates
) -> None:
    """录入新一期 → **同类工日**的旧记录置为非当前。

    这里曾经断言「一类/三类也被置为非当前」，那是把 bug 写成了预期：
    一个期次要分三次提交（一/二/三类），如果每次都把整个地区的 is_current 清掉，
    最后只剩最后提交的那一类是 current，另外两类在算定额口径时被静默跳过
    （_compute_quota_total 里缺价就 continue），定额合计会算成 0。
    """
    r = await client.post(
        "/api/v1/labor-rates",
        headers=admin_headers,
        json={
            "region": "ningbo",
            "labor_class": "class_b",
            "effective_date": "2026-08-01",
            "price_per_day": "171.00",
            "source": "宁波市建设工程造价信息 2026-08",
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_current"] is True

    current = (await client.get("/api/v1/labor-rates/current", headers=admin_headers)).json()
    # 二类用新价，一类/三类仍保留原期次，不能丢
    assert current["rates"]["class_b"]["price_per_day"] == 171.00
    assert current["rates"]["class_a"]["price_per_day"] == 150.00
    assert current["rates"]["class_c"]["price_per_day"] == 186.00
    assert current["effective_date"] == "2026-08-01"
    assert current["effective_dates"] == {
        "class_a": "2025-09-01",
        "class_b": "2026-08-01",
        "class_c": "2025-09-01",
    }

    # 每类工日各留一条 current，共 3 条
    all_rows = (await client.get("/api/v1/labor-rates", headers=admin_headers)).json()
    current_rows = [r for r in all_rows if r["is_current"]]
    assert len(current_rows) == 3
    assert {r["labor_class"] for r in current_rows} == {"class_a", "class_b", "class_c"}

    # 期次不一致必须告警，否则「一类还是旧价」这件事没人会发现
    assert current["stale_warning"]
    assert "class_a" in current["stale_warning"]


async def test_current_labor_rates_warns_when_a_class_is_missing(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """只录了一类 → /current 要提示缺哪几类，而不是静默少给两条。"""
    r = await client.post(
        "/api/v1/labor-rates",
        headers=admin_headers,
        json={
            "region": "ningbo",
            "labor_class": "class_a",
            "effective_date": "2026-08-01",
            "price_per_day": "160.00",
        },
    )
    assert r.status_code == 201, r.text

    current = (await client.get("/api/v1/labor-rates/current", headers=admin_headers)).json()
    assert current["rates"]["class_a"]["price_per_day"] == 160.00
    assert "class_b" in current["stale_warning"]
    assert "class_c" in current["stale_warning"]


async def test_duplicate_labor_rate_409(client: AsyncClient, admin_headers, labor_rates) -> None:
    r = await client.post(
        "/api/v1/labor-rates",
        headers=admin_headers,
        json={"region": "ningbo", "labor_class": "class_a",
              "effective_date": "2025-09-01", "price_per_day": "150.00"},
    )
    assert r.status_code == 409


async def test_labor_rate_rejects_non_positive_price(
    client: AsyncClient, admin_headers
) -> None:
    r = await client.post(
        "/api/v1/labor-rates",
        headers=admin_headers,
        json={"region": "ningbo", "labor_class": "class_a",
              "effective_date": "2027-01-01", "price_per_day": "0"},
    )
    assert r.status_code == 422


async def test_viewer_cannot_create_labor_rate(
    client: AsyncClient, viewer_headers
) -> None:
    r = await client.post(
        "/api/v1/labor-rates",
        headers=viewer_headers,
        json={"region": "ningbo", "labor_class": "class_a",
              "effective_date": "2027-02-01", "price_per_day": "160"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 调价测算（Page 4 的「调价测算」按钮）
# ---------------------------------------------------------------------------

async def test_adjust_simulate_with_explicit_new_rates(
    client: AsyncClient, admin_headers, labor_rates
) -> None:
    """68 一类 + 300 二类 + 50 三类：
    旧 = 68×150 + 300×163 + 50×186 = 10200 + 48900 + 9300 = 68400
    新 = 68×158 + 300×171 + 50×195 = 10744 + 51300 + 9750 = 71794
    差 = 3394
    """
    r = await client.post(
        "/api/v1/labor-rates/adjust-simulate",
        headers=admin_headers,
        json={
            "labor_days": {"class_a": 68, "class_b": 300, "class_c": 50},
            "new_rates": {"class_a": 158, "class_b": 171, "class_c": 195},
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["old_total"] == 68400.00
    assert body["new_total"] == 71794.00
    assert body["delta"] == 3394.00
    assert body["delta_pct"] == pytest.approx(4.96, abs=0.01)


async def test_adjust_simulate_without_new_rates_is_zero_delta(
    client: AsyncClient, admin_headers, labor_rates
) -> None:
    r = await client.post(
        "/api/v1/labor-rates/adjust-simulate",
        headers=admin_headers,
        json={"labor_days": {"class_a": 68, "class_b": 300, "class_c": 50}},
    )
    body = r.json()
    assert body["old_total"] == 68400.00
    assert body["delta"] == 0


async def test_adjust_simulate_falls_back_to_seed_defaults(
    client: AsyncClient, admin_headers
) -> None:
    """库里没录信息价时用内置的宁波默认值，且标明来源。"""
    r = await client.post(
        "/api/v1/labor-rates/adjust-simulate",
        headers=admin_headers,
        json={"labor_days": {"class_a": 68, "class_b": 300, "class_c": 50}},
    )
    body = r.json()
    assert body["rate_source"] == "default-seed"
    assert body["old_total"] == 68400.00


# ---------------------------------------------------------------------------
# 定额子目
# ---------------------------------------------------------------------------

async def test_create_and_list_quota_items(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/quota-items",
        headers=admin_headers,
        json={
            "code": "5-1-001",
            "category": "5-1",
            "name": "管内穿放双绞线 4 对以内",
            "unit": "100m",
            "labor_a_day": "0.42",
            "labor_b_day": "1.85",
            "base_price": "128.50",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert Decimal(str(body["labor_b_day"])) == Decimal("1.85")
    # 默认工艺系数按 2022.7.13 智能化纪要
    assert Decimal(str(body["shielded_coef"])) == Decimal("1.20")
    assert Decimal(str(body["cat5e_coef"])) == Decimal("1.10")
    assert Decimal(str(body["ceiling_coef"])) == Decimal("1.20")

    listing = await client.get(
        "/api/v1/quota-items", headers=admin_headers, params={"category": "5-1"}
    )
    assert len(listing.json()) == 1

    by_code = await client.get("/api/v1/quota-items/5-1-001", headers=admin_headers)
    assert by_code.status_code == 200
    assert by_code.json()["name"] == "管内穿放双绞线 4 对以内"


async def test_duplicate_quota_code_409(client: AsyncClient, admin_headers) -> None:
    payload = {"code": "5-6-001", "category": "5-6", "name": "安装摄像机"}
    assert (await client.post("/api/v1/quota-items", headers=admin_headers, json=payload)).status_code == 201
    assert (await client.post("/api/v1/quota-items", headers=admin_headers, json=payload)).status_code == 409


async def test_quota_keyword_search(client: AsyncClient, admin_headers) -> None:
    await client.post(
        "/api/v1/quota-items", headers=admin_headers,
        json={"code": "5-7-001", "category": "5-7", "name": "安装金属线槽"},
    )
    r = await client.get(
        "/api/v1/quota-items", headers=admin_headers, params={"keyword": "线槽"}
    )
    assert len(r.json()) == 1


# ---------------------------------------------------------------------------
# 元数据字典（前端下拉用）
# ---------------------------------------------------------------------------

async def test_meta_enums(client: AsyncClient) -> None:
    r = await client.get("/api/v1/meta/enums")
    assert r.status_code == 200
    body = r.json()
    assert "subsystem_type" in body
    assert "device_status" in body
    subs = {e["value"] for e in body["subsystem_type"]}
    assert "video_surveillance" in subs
    assert len(subs) == 11
    roles = {e["value"] for e in body["role"]}
    assert roles == {"admin", "purchaser", "sales", "estimator", "manager", "finance", "viewer"}


async def test_meta_labels_cover_all_device_statuses(client: AsyncClient) -> None:
    r = await client.get("/api/v1/meta/labels")
    labels = r.json()
    assert labels["device_status"]["eol"] == "已停产"
    assert labels["device_status"]["near_eol"] == "临近停产"
    assert labels["subsystem_type"]["video_surveillance"] == "视频监控"


async def test_openapi_schema_builds(client: AsyncClient) -> None:
    """若路由或 schema 有循环引用，这里会炸。"""
    r = await client.get("/api/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/v1/quotes/{quote_id}/compute" in paths
    assert "/api/v1/devices/match" in paths
    assert "/api/v1/imports/inquiry/preview" in paths


# ---------------------------------------------------------------------------
# 新建项目自动建默认子系统（2026-10-04）
# ---------------------------------------------------------------------------

async def test_create_project_builds_default_subsystems(
    client: AsyncClient, admin_headers
) -> None:
    """新建项目就带上标准专业 —— 否则用户建完项目以为"加设备加不进去"。"""
    from app.services.settings import DEFAULT_SUBSYSTEMS

    r = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "code": "PRJ-AUTO-1", "name": "自动建子系统", "customer_name": "客户",
            "project_type": "commercial",
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]

    r = await client.get(f"/api/v1/projects/{pid}/subsystems", headers=admin_headers)
    assert r.status_code == 200, r.text
    types = {s["subsystem_type"] for s in r.json()}
    assert types == set(DEFAULT_SUBSYSTEMS), (types, DEFAULT_SUBSYSTEMS)


async def test_create_project_can_skip_default_subsystems(
    client: AsyncClient, admin_headers
) -> None:
    """单个项目可以显式跳过（清空清单 or 传 false）。"""
    r = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "code": "PRJ-AUTO-2", "name": "不自动建", "customer_name": "客户",
            "project_type": "commercial",
            "create_default_subsystems": False,
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.get(f"/api/v1/projects/{pid}/subsystems", headers=admin_headers)
    assert r.json() == [], r.text


async def test_default_subsystems_come_from_settings(
    client: AsyncClient, admin_headers
) -> None:
    """清单来自「系统设置」：改了就按新的建（这里只留两个专业）。"""
    r = await client.patch(
        "/api/v1/settings",
        headers=admin_headers,
        json={"project": {"default_subsystems": ["network", "server_room"]}},
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={
            "code": "PRJ-AUTO-3", "name": "只建两个专业", "customer_name": "客户",
            "project_type": "commercial",
        },
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    r = await client.get(f"/api/v1/projects/{pid}/subsystems", headers=admin_headers)
    assert {s["subsystem_type"] for s in r.json()} == {"network", "server_room"}, r.text



# ---------------------------------------------------------------------------
# 客户档案（2026-10-04）
# ---------------------------------------------------------------------------

async def test_customer_crud_and_code_autogen(client: AsyncClient, admin_headers) -> None:
    """建客户不传编号 → 后端按 CUS-0001 递增分配。"""
    r = await client.post(
        "/api/v1/customers",
        headers=admin_headers,
        json={"name": "宁波测试置业有限公司", "contact_name": "陈工",
              "contact_phone": "13800000000", "tax_no": "91330200TEST0001"},
    )
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["code"].startswith("CUS-"), c
    assert c["is_active"] is True

    # 同名再建 → 409（否则按客户统计会出现两个"同一家"）
    dup = await client.post(
        "/api/v1/customers", headers=admin_headers, json={"name": "宁波测试置业有限公司"}
    )
    assert dup.status_code == 409, dup.text

    # 改联系人
    patched = await client.patch(
        f"/api/v1/customers/{c['id']}", headers=admin_headers,
        json={"contact_name": "李工", "contact_phone": "13900000000"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["contact_name"] == "李工"

    # 停用（不是物理删除）
    gone = await client.delete(f"/api/v1/customers/{c['id']}", headers=admin_headers)
    assert gone.status_code == 204, gone.text
    listed = await client.get("/api/v1/customers", headers=admin_headers)
    assert all(x["id"] != c["id"] for x in listed.json()["items"]), "停用后默认列表不该出现"
    with_inactive = await client.get(
        "/api/v1/customers", params={"include_inactive": True}, headers=admin_headers
    )
    assert any(x["id"] == c["id"] for x in with_inactive.json()["items"])


async def test_project_inherits_customer_contact(client: AsyncClient, admin_headers) -> None:
    """项目引用客户 → 自动带出名称/联系人/电话；显式填的值优先。"""
    c = (
        await client.post(
            "/api/v1/customers",
            headers=admin_headers,
            json={"name": "宁波带出置业", "contact_name": "档案联系人",
                  "contact_phone": "0574-11112222"},
        )
    ).json()

    p = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"code": "PRJ-CUS-1", "name": "带出测试", "customer_id": c["id"]},
    )
    assert p.status_code == 201, p.text
    body = p.json()
    assert body["customer_name"] == "宁波带出置业"
    assert body["customer_contact"] == "档案联系人"
    assert body["customer_phone"] == "0574-11112222"

    # 显式填的名字/联系人**不被覆盖**（项目可以有专属联系人）
    p2 = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"code": "PRJ-CUS-2", "name": "专属联系人", "customer_id": c["id"],
              "customer_name": "手工填的甲方", "customer_contact": "项目专属"},
    )
    assert p2.status_code == 201, p2.text
    assert p2.json()["customer_name"] == "手工填的甲方"
    assert p2.json()["customer_contact"] == "项目专属"


async def test_customer_rename_syncs_projects(client: AsyncClient, admin_headers) -> None:
    """客户改名要同步到引用它的项目（项目列表显示的是名称快照）。"""
    c = (
        await client.post(
            "/api/v1/customers", headers=admin_headers, json={"name": "改名前甲方"}
        )
    ).json()
    p = (
        await client.post(
            "/api/v1/projects",
            headers=admin_headers,
            json={"code": "PRJ-CUS-3", "name": "同步测试", "customer_id": c["id"],
                  "create_default_subsystems": False},
        )
    ).json()
    assert p["customer_name"] == "改名前甲方"

    r = await client.patch(
        f"/api/v1/customers/{c['id']}", headers=admin_headers, json={"name": "改名后甲方"}
    )
    assert r.status_code == 200, r.text
    got = (await client.get(f"/api/v1/projects/{p['id']}", headers=admin_headers)).json()
    assert got["customer_name"] == "改名后甲方", got


async def test_project_rejects_unknown_customer(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/projects",
        headers=admin_headers,
        json={"code": "PRJ-CUS-4", "name": "脏数据", "customer_name": "x",
              "customer_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r.status_code == 422, r.text


async def test_customer_write_requires_permission(
    client: AsyncClient, admin_headers
) -> None:
    """viewer 能读客户、不能建。"""
    me = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={"username": "cusviewer", "email": "cusviewer@example.com",
              "password": "Viewer12345!", "role": "viewer"},
    )
    assert me.status_code == 201, me.text
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "cusviewer", "password": "Viewer12345!"},
        )
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}

    assert (await client.get("/api/v1/customers", headers=h)).status_code == 200
    denied = await client.post(
        "/api/v1/customers", headers=h, json={"name": "viewer 不该能建"}
    )
    assert denied.status_code == 403, denied.text
