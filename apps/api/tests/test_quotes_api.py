"""报价单 API 集成测试 — 走完整 HTTP 路径（双行并列回算、停产替换、成本视图）."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus, ProjectType
from app.models.subsystem import Subsystem, SubsystemType

pytestmark = pytest.mark.asyncio


def dec(value) -> Decimal:
    """Normalize an API scalar to a 2dp Decimal.

    Pydantic may serialize `Decimal` as a JSON number or a numeric string
    depending on version/mode, so tests compare through this helper rather
    than asserting on the wire format.
    """
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def _make_project(db: AsyncSession, code: str = "PRJ-TEST-001") -> Project:
    p = Project(
        code=code,
        name="测试项目",
        customer_name="测试客户",
        project_type=ProjectType.COMMERCIAL,
        status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _make_quote(client: AsyncClient, headers, project_id, code="QT-TEST-001"):
    r = await client.post(
        "/api/v1/quotes",
        headers=headers,
        json={
            "project_id": str(project_id),
            "code": code,
            "title": "测试报价单",
            "fee_base": "device_aux_install",
            "auxiliary_base": "device",
            "auxiliary_rate": "0.06",
            "design_rate": "0.02",
            "warranty_rate": "0.03",
            "mgmt_profit_rate": "0.08",
            "tax_rate": "0.09",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _detail(client: AsyncClient, headers, quote_id) -> dict:
    r = await client.get(f"/api/v1/quotes/{quote_id}", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# 1. 创建 + 空单
# ---------------------------------------------------------------------------

async def test_create_quote_inherits_customer_from_project(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    assert q["customer_name"] == "测试客户"
    assert dec(q["total_with_tax"]) == Decimal("0.00")


async def test_create_quote_duplicate_code_409(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _make_project(db)
    await _make_quote(client, admin_headers, p.id, code="QT-DUP")
    r = await client.post(
        "/api/v1/quotes",
        headers=admin_headers,
        json={"project_id": str(p.id), "code": "QT-DUP", "title": "x"},
    )
    assert r.status_code == 409


async def test_create_quote_unknown_project_404(client: AsyncClient, admin_headers) -> None:
    import uuid

    r = await client.post(
        "/api/v1/quotes",
        headers=admin_headers,
        json={"project_id": str(uuid.uuid4()), "code": "QT-X", "title": "x"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2. 添加明细行 → 自动重算（含 stale-collection 回归）
# ---------------------------------------------------------------------------

async def test_add_item_from_device_library_pulls_prices(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    camera = devices[0]  # V1001 680.00 / 112.60 / 510.00

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(camera.id), "install_unit_price": str(camera.install_unit_price), "name": camera.name, "quantity": "86"},
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert Decimal(str(item["unit_price"])) == Decimal("680")
    assert Decimal(str(item["install_unit_price"])) == Decimal("112.60")
    assert dec(item["equipment_amount"]) == Decimal("58480.00")
    assert dec(item["install_amount"]) == Decimal("9683.60")


async def test_add_item_triggers_recompute(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """回归：新加的行必须立刻计入总额（不能漏算 stale 集合）。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)

    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "86"},
    )
    detail = await _detail(client, admin_headers, q["id"])

    assert dec(detail["equipment_total"]) == Decimal("58480.00")
    assert dec(detail["install_total"]) == Decimal("9683.60")
    # 辅材 = 58480 × 6% = 3508.80
    assert dec(detail["auxiliary_total"]) == Decimal("3508.80")
    assert len(detail["items"]) == 1
    assert float(detail["total_with_tax"]) > float(detail["equipment_total"])

    # 成本/毛利**不应该**出现在通用详情 DTO 里（viewer 也能调它），
    # 只能从受权限保护的 /cost 端点取。
    assert "cost_total" not in detail
    assert "gross_profit" not in detail
    assert "gross_profit_rate" not in detail

    cost = await client.get(f"/api/v1/quotes/{q['id']}/cost", headers=admin_headers)
    assert cost.status_code == 200
    assert dec(cost.json()["cost_total"]) == Decimal("43860.00")  # 86 × 510


async def test_second_item_accumulates_not_replaces(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """两行相加 = 59,856 设备费，验证累加而非覆盖。

    （第二行故意用**非甲供**的 devices[2]：甲供行的设备费按 0 计，
    拿它做累加断言虽然也能过，但验不到"累加"这件事。）
    """
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)

    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "86"},
    )
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[2].id), "install_unit_price": str(devices[2].install_unit_price), "name": devices[2].name, "quantity": "2"},
    )
    detail = await _detail(client, admin_headers, q["id"])

    assert dec(detail["equipment_total"]) == Decimal("58480.00") + Decimal("1376.00")
    assert len(detail["items"]) == 2


async def test_owner_supplied_device_adds_no_equipment(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """从设备库加入"甲供"设备时，标记要跟着进来、设备费按 0 计。

    回归：以前 add_item 不回填 device.is_owner_supplied，设备库里标了甲供的设备
    一进报价单就悄悄变成"我方供货"，设备费照算（UI 却说"不计入设备费"）。
    """
    p = await _make_project(db, code="PRJ-OWNER")
    q = await _make_quote(client, admin_headers, p.id, code="QT-OWNER")
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[3].id), "install_unit_price": str(devices[3].install_unit_price), "name": devices[3].name, "quantity": "2"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["is_owner_supplied"] is True
    assert dec(r.json()["equipment_amount"]) == Decimal("0")

    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["equipment_total"]) == Decimal("0")


async def test_add_item_with_eol_device_still_succeeds(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """停产型号允许加入（业务上需要），但不应 500。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    eol = devices[1]  # V1008 status=eol

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(eol.id), "install_unit_price": str(eol.install_unit_price), "name": eol.name, "quantity": "10"},
    )
    assert r.status_code == 201


# ---------------------------------------------------------------------------
# 3. 手工录入行
# ---------------------------------------------------------------------------

async def test_manual_item_uses_supplied_prices(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={
            "name": "临时设备（待询价）",
            "unit": "台",
            "quantity": "3",
            "unit_price": "100.00",
            "install_unit_price": "20.00",
            "source": "manual",
        },
    )
    assert r.status_code == 201
    assert dec(r.json()["equipment_amount"]) == Decimal("300.00")
    assert dec(r.json()["install_amount"]) == Decimal("60.00")


async def test_manual_item_zero_price_is_pending_inquiry(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """未匹配行以 0 价占位，不阻断。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"name": "待询价设备", "quantity": "5", "unit_price": "0",
              "install_unit_price": "0", "source": "manual"},
    )
    assert r.status_code == 201
    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["equipment_total"]) == Decimal("0.00")
    assert dec(detail["total_with_tax"]) == Decimal("0.00")


# ---------------------------------------------------------------------------
# 4. 改数量 / 改单价 / 删行 → 重算
# ---------------------------------------------------------------------------

async def test_update_item_quantity_recomputes(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "10"},
    )
    item_id = add.json()["id"]

    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item_id}",
        headers=admin_headers,
        json={"quantity": "20"},
    )
    assert r.status_code == 200
    assert dec(r.json()["equipment_amount"]) == Decimal("13600.00")

    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["equipment_total"]) == Decimal("13600.00")
    assert dec(detail["install_total"]) == Decimal("2252.00")  # 20 × 112.60


async def test_update_item_price_recomputes(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    item_id = add.json()["id"]

    await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item_id}",
        headers=admin_headers,
        json={"unit_price": "1000.00"},
    )
    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["equipment_total"]) == Decimal("1000.00")


async def test_delete_item_recomputes(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "5"},
    )
    item_id = add.json()["id"]

    r = await client.delete(
        f"/api/v1/quotes/{q['id']}/items/{item_id}", headers=admin_headers
    )
    assert r.status_code == 204
    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["equipment_total"]) == Decimal("0.00")
    assert detail["items"] == []


# ---------------------------------------------------------------------------
# 5. 费率改动 → 重算
# ---------------------------------------------------------------------------

async def test_patch_quote_rate_triggers_recompute(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    before = await _detail(client, admin_headers, q["id"])

    # ① 创建时就能定辅材率（2026-10-05 决策④ ✓：这两项"创建时确定、之后不可改" ✓）
    #    —— 原用例是 PATCH 改辅材率并断言 680×12% ✓；
    #       现在改成"创建时传 12%" ✓，口径断言原样保留 ✓。
    r = await client.post(
        "/api/v1/quotes",
        headers=admin_headers,
        json={"project_id": str(p.id), "name": "辅材率创建验证", "auxiliary_rate": "0.12"},
    )
    assert r.status_code in (200, 201), r.text
    q_created = r.json()
    await client.post(
        f"/api/v1/quotes/{q_created['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "name": devices[0].name,
              "quantity": "1", "unit_price": "680"},
    )
    created = await _detail(client, admin_headers, q_created["id"])
    assert dec(created["auxiliary_total"]) == Decimal("81.60"), (
        f"创建时传的辅材率应生效：680 × 12% = 81.60，实际 {created['auxiliary_total']}"
    )

    # ② 允许的字段（税率）改了要重算 ✓ —— 原用例的"改费率会重算"意图保留 ✓
    before = await _detail(client, admin_headers, q["id"])
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={"tax_rate": "0.12"}
    )
    assert r.status_code == 200
    after = await _detail(client, admin_headers, q["id"])

    assert dec(before["auxiliary_total"]) == Decimal("40.80")   # 680 × 6%（默认辅材率 ✓ 未变 ✓）
    assert float(after["total_with_tax"]) > float(before["total_with_tax"])


# ---------------------------------------------------------------------------
# 5b. 乐观锁（quotes.version）
# ---------------------------------------------------------------------------

async def test_optimistic_lock_rejects_stale_version(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """手上的版本过期就该 409，而不是静默盖掉别人的修改。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    stale = q["version"]

    # 甲先改了一次 → 版本前进
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={"tax_rate": "0.11"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == stale + 1

    # 乙拿着旧版本提交 → 409，并且**费率不能被改掉**
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}",
        headers=admin_headers,
        json={"version": stale, "tax_rate": "0.99"},
    )
    assert r.status_code == 409, r.text
    # 2026-10-07 改：409 的 detail 由**纯字符串**改为**结构化对象** ✓
    #   （前端要拿 remote_changed / current_version 做"可合并"判断 ✗ 靠解析中文句子不可行 ✗）
    detail = r.json()["detail"]
    assert detail["error"] == "version_conflict", detail
    assert detail["your_version"] == stale, detail
    assert detail["current_version"] == stale + 1, detail
    assert "这张单在你编辑期间被" in detail["message"], detail
    assert "自动合并" in detail["message"], detail
    after = await _detail(client, admin_headers, q["id"])
    assert dec(after["tax_rate"]) == Decimal("0.11")

    # 乙刷新拿到最新版本后再提交 → 放行
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}",
        headers=admin_headers,
        json={"version": stale + 1, "tax_rate": "0.12"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == stale + 2


async def test_patch_without_version_is_allowed_but_still_bumps(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """不带 version 的调用方（脚本 / 老客户端）不能被挡住，但版本号照样前进。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={"design_rate": "0.03"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["version"] == q["version"] + 1


async def test_compute_rejects_stale_version(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """「点重算」这个口子也必须比对版本，否则它就是绕过乐观锁的后门。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    stale = q["version"]
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={"tax_rate": "0.06"}
    )
    assert r.status_code == 200, r.text

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/compute",
        headers=admin_headers,
        json={"version": stale, "tax_rate": "0.13"},
    )
    assert r.status_code == 409, r.text
    after = await _detail(client, admin_headers, q["id"])
    assert dec(after["tax_rate"]) == Decimal("0.06"), "409 之后税率仍然被改掉了"


async def test_item_change_also_bumps_version(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """改明细也会推动版本号：明细变了，别人手上的版本就该失效。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    before = q["version"]
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    assert r.status_code == 201, r.text
    after = await _detail(client, admin_headers, q["id"])
    assert after["version"] > before


# ---------------------------------------------------------------------------
# 5c. 未实现 / 重复的口径必须告警
# ---------------------------------------------------------------------------

async def test_unimplemented_auxiliary_base_warns(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """选了「按工程量」实际却按设备费算 —— 必须告警，不能只是换个标签。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/compute",
        headers=admin_headers,
        json={"auxiliary_base": "by_quantity"},
    )
    assert r.status_code == 200, r.text
    warns = r.json()["warnings"]
    assert any("按工程量" in w for w in warns), warns


async def test_direct_fee_base_warns_about_human_plus_material(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """「直接费」按人+材口径，必须提醒口径切换。

    业务拍板：人工费 + 辅材费 = install_total + auxiliary_total，不含设备费。
    与老的「设备费 + 辅材费 + 安装费」口径会差 设备费×design_rate。
    """
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/compute",
        headers=admin_headers,
        json={"fee_base": "direct_fee"},
    )
    assert r.status_code == 200, r.text
    warns = r.json()["warnings"]
    # 必定出现「人+材口径」提示
    assert any("人+材" in w for w in warns), warns
    # 因为直接费基数变小了（不含设备费），与老口径差异 > 0.1% → 必出现审计警告
    assert any("设计费" in w and "差异" in w for w in warns), warns


async def test_explicit_compute_endpoint(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "2"},
    )
    r = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={})
    assert r.status_code == 200
    body = r.json()
    assert dec(body["equipment_total"]) == Decimal("1360.00")
    assert dec(body["install_total"]) == Decimal("225.20")
    # 定额库为空 → 定额口径退化为安装费
    assert dec(body["quota_total"]) == Decimal("225.20")


async def test_compute_with_labor_float_override(
    client: AsyncClient, db: AsyncSession, admin_headers, devices, labor_rates
) -> None:
    """人工浮动只影响定额口径，不影响点位单价口径。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )

    base = (
        await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={})
    ).json()
    floated = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/compute",
            headers=admin_headers,
            json={"labor_float_per_class": {"class_a": "0.5", "class_b": "0.5", "class_c": "0.5"}},
        )
    ).json()

    assert dec(base["equipment_total"]) == dec(floated["equipment_total"])
    assert dec(base["total_with_tax"]) == dec(floated["total_with_tax"])


async def test_overall_labor_float_applies_through_api(
    client: AsyncClient, db: AsyncSession, admin_headers, quota_items, labor_rates
) -> None:
    """回归（2026-10-03）：界面上「整体浮动」以前填了完全不生效。

    失效点在**路由层**：旧代码 `if not float_per_class:` 才把 labor_float_rate
    摊到三类上，而前端永远发送 {class_a:0, class_b:0, class_c:0}（含 0 的字典
    是 truthy）→ 分支永不成立。所以这条测试必须走 HTTP，而不是只测 compute_quote。
    """
    p = await _make_project(db, code="PRJ-FLOAT-A")
    q = await _make_quote(client, admin_headers, p.id, code="QT-FLOAT-A")
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"name": "定额行", "quantity": "10", "unit_price": "1000",
              "notes": "quota:5-6-001"},
    )
    assert r.status_code == 201, r.text

    async def compute(payload: dict) -> dict:
        rr = await client.post(f"/api/v1/quotes/{q['id']}/compute",
                               headers=admin_headers, json=payload)
        assert rr.status_code == 200, rr.text
        return rr.json()

    base = await compute({})
    assert dec(base["quota_total"]) > Decimal("0"), "定额口径没算出来，测试前提不成立"

    ui_shape = await compute({
        "labor_float_rate": "0.10",
        "labor_float_per_class": {"class_a": 0, "class_b": 0, "class_c": 0},
    })
    assert dec(ui_shape["quota_total"]) > dec(base["quota_total"]), \
        "整体浮动没有生效 —— 这正是 2026-10-03 修掉的那个静默失效"

    # 整体 + 分类相加
    both = await compute({
        "labor_float_rate": "0.10",
        "labor_float_per_class": {"class_a": "0.10", "class_b": 0, "class_c": 0},
    })
    assert dec(both["quota_total"]) > dec(ui_shape["quota_total"])

    # 越界/打错键名的分类浮动要被拦成 422（以前字典值无任何校验）
    bad = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers,
                            json={"labor_float_per_class": {"class_A": "0.1"}})
    assert bad.status_code == 422, bad.text


# ---------------------------------------------------------------------------
# 5d. 工艺系数结构化字段（is_shielded / is_cat5e / is_ceiling）
# ---------------------------------------------------------------------------

async def test_create_item_with_structured_craft_coefficients(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """新增明细时勾选工艺字段 → 安装费必须按工艺系数重算。"""
    p = await _make_project(db, code="PRJ-CRAFT-1")
    q = await _make_quote(client, admin_headers, p.id, code="QT-CRAFT-1")
    camera = devices[0]  # 680 / 112.60 / 510
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={
            "device_id": str(camera.id), "install_unit_price": str(camera.install_unit_price),
            "name": camera.name,
            "quantity": "10",
            "is_shielded": True,
            "is_cat5e": True,
            "is_ceiling": False,
        },
    )
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["is_shielded"] is True
    assert item["is_cat5e"] is True
    assert item["is_ceiling"] is False
    # 10 × 112.60 × 1.20 × 1.10 = 1486.32
    assert dec(item["install_amount"]) == Decimal("1486.32"), (
        f"工艺字段应触发 ×1.20 × 1.10，实际 {item['install_amount']}"
    )


async def test_update_item_toggles_craft_coefficients(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """编辑行能把工艺字段勾上/取消。"""
    p = await _make_project(db, code="PRJ-CRAFT-2")
    q = await _make_quote(client, admin_headers, p.id, code="QT-CRAFT-2")
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "10"},
    )
    item_id = add.json()["id"]
    # 默认全 False → 10 × 112.60 = 1126.00
    assert dec(add.json()["install_amount"]) == Decimal("1126.00")

    # 勾上 is_ceiling → × 1.20 = 1351.20
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item_id}",
        headers=admin_headers,
        json={"is_ceiling": True},
    )
    assert r.status_code == 200, r.text
    assert dec(r.json()["install_amount"]) == Decimal("1351.20")
    assert r.json()["is_ceiling"] is True

    # 取消 is_ceiling、勾上 is_shielded → × 1.20 同样 = 1351.20
    r2 = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item_id}",
        headers=admin_headers,
        json={"is_ceiling": False, "is_shielded": True},
    )
    assert r2.status_code == 200, r2.text
    assert dec(r2.json()["install_amount"]) == Decimal("1351.20")
    assert r2.json()["is_shielded"] is True


# ---------------------------------------------------------------------------
# 6. 停产替换
# ---------------------------------------------------------------------------

async def test_replace_eol_item_with_on_sale_device(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """V1008（停产 645）→ V1009（在售 688）。"""
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[1].id), "install_unit_price": str(devices[1].install_unit_price), "name": devices[1].name, "quantity": "10"},
    )
    item_id = add.json()["id"]

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items/{item_id}/replace",
        headers=admin_headers,
        params={"new_device_id": str(devices[2].id)},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["model"] == "DH-IPC-HFW3449M"
    assert Decimal(str(body["unit_price"])) == Decimal("688")

    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["equipment_total"]) == Decimal("6880.00")


async def test_replace_with_eol_device_rejected(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items/{add.json()['id']}/replace",
        headers=admin_headers,
        params={"new_device_id": str(devices[1].id)},  # V1008 已停产
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# 7. 成本与利润视图
# ---------------------------------------------------------------------------

async def test_cost_view_includes_gross_profit(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "10"},
    )
    r = await client.get(f"/api/v1/quotes/{q['id']}/cost", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["cost_total"] == 5100.00  # 10 × 510
    assert body["gross_profit"] > 0
    assert 0 < body["gross_profit_rate"] < 1


async def test_cost_view_denied_for_viewer(
    client: AsyncClient, db: AsyncSession, admin_headers, viewer_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    r = await client.get(f"/api/v1/quotes/{q['id']}/cost", headers=viewer_headers)
    assert r.status_code == 403


async def test_compute_hides_cost_from_sales(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, devices
) -> None:
    """回归：sales 有 quote.compute 权限但没有成本视图权限。

    修之前 compute 会无条件回传 cost_total/gross_profit，
    /cost 端点的 403 形同虚设。
    """
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "10"},
    )

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/compute", headers=sales_headers, json={}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    # 报价口径照常回传
    assert dec(body["equipment_total"]) == Decimal("6800.00")
    # 成本口径必须为 null
    assert body["cost_total"] is None
    assert body["gross_profit"] is None
    assert body["gross_profit_rate"] is None

    # 换个有权限的角色调同一接口，成本就有了
    r2 = await client.post(
        f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={}
    )
    assert r2.status_code == 200
    assert dec(r2.json()["cost_total"]) == Decimal("5100.00")


# ---------------------------------------------------------------------------
# 8. 子系统汇总（客户版）
# ---------------------------------------------------------------------------

async def test_subsystem_summary_with_shares(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    video = Subsystem(project_id=p.id, subsystem_type=SubsystemType.VIDEO_SURVEILLANCE,
                      custom_name="视频监控", sort_order=0)
    net = Subsystem(project_id=p.id, subsystem_type=SubsystemType.NETWORK,
                    custom_name="计算机网络", sort_order=1)
    db.add_all([video, net])
    await db.commit()
    await db.refresh(video)
    await db.refresh(net)

    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name,
              "quantity": "10", "subsystem_id": str(video.id)},
    )
    # devices[3] 是 N5001，设备库里标了 is_owner_supplied=True：
    # 从设备库加行时会**跟着回填甲供标记** → 设备费按 0 计（回归 2026-09-30 的口径统一）
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[3].id), "install_unit_price": str(devices[3].install_unit_price), "name": devices[3].name,
              "quantity": "2", "subsystem_id": str(net.id)},
    )
    # 再给网络加一台普通设备，验证"网络"这一行的小计仍在正常求和
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[2].id), "install_unit_price": str(devices[2].install_unit_price), "name": devices[2].name,
              "quantity": "1", "subsystem_id": str(net.id)},
    )

    r = await client.get(f"/api/v1/quotes/{q['id']}/subsystems", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    subs = {s["name"]: s for s in body["subsystems"]}

    # 视频：10×680 + 10×112.60 = 6800 + 1126 = 7926
    assert subs["视频监控"]["equipment_subtotal"] == 6800.00
    assert subs["视频监控"]["install_subtotal"] == 1126.00
    assert subs["视频监控"]["amount"] == 7926.00
    # 网络：甲供交换机 2 台 → 设备费 0；普通摄像机 1×688 + 112.60 = 800.60
    assert subs["计算机网络"]["equipment_subtotal"] == 688.00
    assert subs["计算机网络"]["install_subtotal"] == 112.60
    assert subs["计算机网络"]["amount"] == 800.60
    assert body["grand_total"] == 8726.60

    # 甲供那一行的设备金额确实被归零了（否则"网络"里会多出 2760）
    items = (await client.get(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers)).json()
    owner_row = [i for i in items if i["device_id"] == str(devices[3].id)][0]
    assert owner_row["is_owner_supplied"] is True
    assert Decimal(str(owner_row["equipment_amount"])) == Decimal("0")

    total_share = sum(s["share"] for s in body["subsystems"])
    assert abs(total_share - 100.0) < 0.1


async def test_subsystem_rollup_covers_every_quote_of_the_project(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """项目里的子系统小计必须覆盖**全部**报价单。

    回归：以前 `_rollup_subsystems` 只按当前这一张报价单求和并覆盖写入，
    而列是项目级的 —— 编辑第二张单会把第一张单的贡献整个抹掉
    （实测：1000 → 500）。同一项目多版报价是本系统的正常用法。
    """
    p = await _make_project(db, code="PRJ-TWOQ")
    video = Subsystem(project_id=p.id, subsystem_type=SubsystemType.VIDEO_SURVEILLANCE,
                      custom_name="视频监控", sort_order=0)
    db.add(video)
    await db.commit()
    await db.refresh(video)

    qa = await _make_quote(client, admin_headers, p.id, code="QT-A")
    qb = await _make_quote(client, admin_headers, p.id, code="QT-B")

    await client.post(
        f"/api/v1/quotes/{qa['id']}/items", headers=admin_headers,
        json={"name": "A 设备", "quantity": "1", "unit_price": "1000.00",
              "subsystem_id": str(video.id)},
    )
    r = await client.get(f"/api/v1/subsystems?project_id={p.id}", headers=admin_headers)
    assert r.json()[0]["equipment_subtotal"] == "1000.00"

    await client.post(
        f"/api/v1/quotes/{qb['id']}/items", headers=admin_headers,
        json={"name": "B 设备", "quantity": "1", "unit_price": "500.00",
              "subsystem_id": str(video.id)},
    )
    r = await client.get(f"/api/v1/subsystems?project_id={p.id}", headers=admin_headers)
    rows = r.json()
    assert len(rows) == 1
    assert Decimal(str(rows[0]["equipment_subtotal"])) == Decimal("1500.00"), (
        "两张报价单的贡献都要算进来（旧实现会被后者覆盖成 500）"
    )
    # 辅材费要真的写进去（以前这一列永远为 0，与导出里"报价汇总"的辅材费自相矛盾）
    assert Decimal(str(rows[0]["auxiliary_subtotal"])) > 0


# ---------------------------------------------------------------------------
# 9. 操作留痕
# ---------------------------------------------------------------------------

async def test_audit_log_records_compute(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )
    r = await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=admin_headers)
    assert r.status_code == 200
    logs = r.json()
    assert any(log["action"] == "compute" for log in logs)


async def test_audit_log_records_device_replacement(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    add = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[1].id), "install_unit_price": str(devices[1].install_unit_price), "name": devices[1].name, "quantity": "1"},
    )
    await client.post(
        f"/api/v1/quotes/{q['id']}/items/{add.json()['id']}/replace",
        headers=admin_headers,
        params={"new_device_id": str(devices[2].id)},
    )
    logs = (
        await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=admin_headers)
    ).json()
    assert any(log["action"] == "replace_device" for log in logs)


# ---------------------------------------------------------------------------
# 10. 项目级联
# ---------------------------------------------------------------------------

async def test_project_subsystems_endpoint(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _make_project(db)
    db.add(Subsystem(project_id=p.id, subsystem_type=SubsystemType.VIDEO_SURVEILLANCE, sort_order=0))
    db.add(Subsystem(project_id=p.id, subsystem_type=SubsystemType.BROADCAST, sort_order=1))
    await db.commit()

    r = await client.get(f"/api/v1/projects/{p.id}/subsystems", headers=admin_headers)
    assert r.status_code == 200
    assert len(r.json()) == 2


async def test_deleting_subsystem_keeps_quote_items(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """回归：删子系统只解除归属，绝不能连带删掉报价明细行。

    QuoteItem 的属主是 Quote；若 Subsystem.items 上误加
    cascade="all, delete-orphan"，定价行会被静默删除。
    """
    p = await _make_project(db)
    sub = Subsystem(project_id=p.id, subsystem_type=SubsystemType.VIDEO_SURVEILLANCE)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    q = await _make_quote(client, admin_headers, p.id)
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name,
              "quantity": "10", "subsystem_id": str(sub.id)},
    )

    before = await _detail(client, admin_headers, q["id"])
    assert len(before["items"]) == 1
    assert dec(before["equipment_total"]) == Decimal("6800.00")

    assert (
        await client.delete(f"/api/v1/subsystems/{sub.id}", headers=admin_headers)
    ).status_code == 204

    after = await _detail(client, admin_headers, q["id"])
    assert len(after["items"]) == 1, "删除子系统不该删掉报价明细行"
    assert dec(after["equipment_total"]) == Decimal("6800.00")


# ---------------------------------------------------------------------------
# 回归：前端契约（这些字段以前被 pydantic 静默丢弃 / 缺字段导致前端崩）
# ---------------------------------------------------------------------------

async def test_create_quote_with_only_project_id(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """「编制报价」只传 project_id 也要能建单。

    QuoteCreate 的 code/title 曾经是必填，于是前端那个只发 {project_id} 的入口
    永远 422，报价编制界面根本进不去。
    """
    p = await _make_project(db, code="PRJ-AUTO-1")
    r = await client.post(
        "/api/v1/quotes", headers=admin_headers, json={"project_id": str(p.id)}
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"], "应自动生成报价单编号"
    assert body["code"].startswith("PRJ-AUTO-1")
    assert body["title"], "应自动生成标题"
    assert body["version"] == 1

    # 同项目再建一单，编号不能撞
    r2 = await client.post(
        "/api/v1/quotes", headers=admin_headers, json={"project_id": str(p.id)}
    )
    assert r2.status_code == 201, r2.text
    assert r2.json()["code"] != body["code"]


async def test_compute_applies_all_rate_overrides(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """重算接口必须真的用上传入的费率。

    ComputeRequest 以前只有 labor_float_rate / labor_float_per_class /
    fee_base / auxiliary_base 四个字段，其余费率被 pydantic 静默忽略：
    界面上改了税率点「重算」，提示成功，实际按旧费率算 —— 钱错了还不报错。
    """
    p = await _make_project(db, code="PRJ-RATE-1")
    q = await _make_quote(client, admin_headers, p.id, code="QT-RATE-1")
    await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
    )

    # 设备 680，安装 112.60 → 设备费 680.00
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/compute",
        headers=admin_headers,
        json={"tax_rate": "0.13", "mgmt_profit_rate": "0.20", "warranty_rate": "0.05"},
    )
    assert r.status_code == 200, r.text
    assert dec(r.json()["warranty_fee"]) == Decimal("34.00")  # 680 × 5%

    detail = await _detail(client, admin_headers, q["id"])
    assert dec(detail["tax_rate"]) == Decimal("0.13")
    assert dec(detail["mgmt_profit_rate"]) == Decimal("0.20")
    assert dec(detail["warranty_rate"]) == Decimal("0.05")


async def test_update_item_sets_name_and_subsystem(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """编辑明细行的名称/型号/单位/所属子系统必须落库。

    QuoteItemUpdate 以前只有数量/单价/备注等字段，改了名称或子系统会提示成功
    但刷新后原样不动。
    """
    p = await _make_project(db, code="PRJ-ITEM-1")
    sub = Subsystem(project_id=p.id, subsystem_type=SubsystemType.VIDEO_SURVEILLANCE)
    db.add(sub)
    await db.commit()
    await db.refresh(sub)

    q = await _make_quote(client, admin_headers, p.id, code="QT-ITEM-1")
    item = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/items",
            headers=admin_headers,
            json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "1"},
        )
    ).json()

    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item['id']}",
        headers=admin_headers,
        json={
            "name": "甲方指定名称",
            "brand": "甲方品牌",
            "model": "MODEL-X",
            "unit": "套",
            "subsystem_id": str(sub.id),
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "甲方指定名称"
    assert body["brand"] == "甲方品牌"
    assert body["model"] == "MODEL-X"
    assert body["unit"] == "套"
    assert body["subsystem_id"] == str(sub.id)

    detail = await _detail(client, admin_headers, q["id"])
    assert detail["items"][0]["name"] == "甲方指定名称"


async def test_bare_quota_note_does_not_break_recompute(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """回归：备注是孤零零一个 "quota:" 时，重算不能 500。

    路由层以前写 it.notes.split("quota:", 1)[1].split()[0]，
    "quota:" 切出来是空串、空串 .split() 是空列表，[0] 直接 IndexError。
    这个列表推导每次重算都跑，所以只要有一行这样的备注，整张报价单的
    加行/改价/重算/询价导入就全部 500，连带别的正常定额行也一起取不到价。
    这里走的是**接口**路径，不是 service 函数。
    """
    p = await _make_project(db, code="PRJ-QUOTA-9")
    q = await _make_quote(client, admin_headers, p.id, code="QT-QUOTA-9")
    item = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/items",
            headers=admin_headers,
            json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "5"},
        )
    ).json()

    # 正常写一个带编号的定额备注，再加一个空壳备注
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item['id']}",
        headers=admin_headers,
        json={"notes": "quota:"},
    )
    assert r.status_code == 200, r.text

    # 关键：下一步的重算以前会 IndexError
    r = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={})
    assert r.status_code == 200, r.text
    assert "equipment_total" in r.json()

    # 同一张单子上再加一行也要能走通（说明取定额那步没被毒死）
    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items",
        headers=admin_headers,
        json={"device_id": str(devices[1].id), "install_unit_price": str(devices[1].install_unit_price), "name": devices[1].name, "quantity": "2"},
    )
    assert r.status_code == 201, r.text


async def test_quota_note_with_code_still_resolves(
    client: AsyncClient, db: AsyncSession, admin_headers, devices, quota_items, labor_rates
) -> None:
    """顺带确认带编号的备注仍然能取到定额子目（别把能用的改坏了）。

    需要 labor_rates：定额口径要求一/二/三类工日当期价齐全，
    缺任何一类都会退化成安装费口径（那时 quota_total 会等于 install_total，
    这个断言就失去意义了）。
    """
    p = await _make_project(db, code="PRJ-QUOTA-10")
    q = await _make_quote(client, admin_headers, p.id, code="QT-QUOTA-10")
    item = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/items",
            headers=admin_headers,
            json={"device_id": str(devices[0].id), "install_unit_price": str(devices[0].install_unit_price), "name": devices[0].name, "quantity": "5"},
        )
    ).json()

    code = quota_items[0].code
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item['id']}",
        headers=admin_headers,
        json={"notes": f"quota:{code} 桥架安装"},
    )
    assert r.status_code == 200, r.text

    r = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={})
    assert r.status_code == 200, r.text
    # 备注尾部还有文字（"桥架安装"），不能因为多写了几个字就解析不出编号
    assert dec(r.json()["quota_total"]) > 0, "带编号的定额备注应当解析出定额子目"

# ---------------------------------------------------------------------------
# 客户版条款与有效期（2026-10-04：让"预览 = 导出 PDF"）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_export_clauses_endpoint_single_source(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """报价说明条款由后端单一出处提供（前端预览与 PDF 导出读同一份）。"""
    p = await _make_project(db, "PRJ-CLAUSE-1")
    await db.commit()
    q = await _make_quote(client, admin_headers, p.id, code="QT-CLAUSE-001")
    r = await client.get(f"/api/v1/quotes/{q['id']}/export-clauses", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["clauses"]) == 6, body
    assert body["valid_days"] == 30
    assert "设备供货" in body["clauses"][0]
    assert any("报价有效期 30 天" in c for c in body["clauses"])


def test_valid_until_is_quote_date_plus_valid_days() -> None:
    """有效期至 = 报价日期 + 30 天（条款第 2 条承诺的 30 天）。"""
    from datetime import UTC, datetime

    from app.services.export_data import QUOTE_VALID_DAYS, _quote_date, _valid_until

    class _Q:
        created_at = datetime(2026, 9, 28, 10, 0, tzinfo=UTC)

    assert _quote_date(_Q()) == "2026-09-28"
    assert QUOTE_VALID_DAYS == 30
    # 30 天后 —— 与时区无关地只比较日期差
    d0 = datetime.fromisoformat(_quote_date(_Q()))
    d1 = datetime.fromisoformat(_valid_until(_Q()))
    assert (d1 - d0).days == 30



# ---------------------------------------------------------------------------
# 阶梯价 / 价格底线（2026-10-04）
# ---------------------------------------------------------------------------

async def _device_with_tiers(client: AsyncClient, headers, *, code: str, tiers: list[dict],
                            min_price: str | None = None) -> dict:
    d = (
        await client.post(
            "/api/v1/devices",
            headers=headers,
            json={"code": code, "name": f"阶梯价测试-{code}", "model": f"M-{code}",
                  "category": "video", "unit": "台",
                  "reference_price": "680.00", "cost_price": "400.00",
                  "install_unit_price": "50.00",
                  **({"min_price": min_price} if min_price else {})},
        )
    ).json()
    if tiers:
        r = await client.put(f"/api/v1/devices/{d['id']}/price-tiers", headers=headers, json=tiers)
        assert r.status_code == 200, r.text
    return d


async def _quote_for(client: AsyncClient, headers, code: str) -> dict:
    p = (
        await client.post(
            "/api/v1/projects", headers=headers,
            json={"code": f"{code}-P", "name": "价格测试项目", "customer_name": "客户",
                  "create_default_subsystems": False},
        )
    ).json()
    q = (
        await client.post(
            "/api/v1/quotes", headers=headers,
            json={"project_id": p["id"], "code": code, "title": "价格测试",
                  "fee_base": "device_aux_install", "auxiliary_base": "device",
                  "auxiliary_rate": "0.06", "design_rate": "0.02", "warranty_rate": "0.03",
                  "mgmt_profit_rate": "0.08", "tax_rate": "0.09"},
        )
    ).json()
    return q


async def test_tier_price_picked_by_quantity(client: AsyncClient, admin_headers) -> None:
    """按数量自动取档：1 台 680 / 10 台 640 / 100 台 600。"""
    d = await _device_with_tiers(
        client, admin_headers, code="VTIER1",
        tiers=[
            {"min_qty": "1", "max_qty": "9", "unit_price": "680.00"},
            {"min_qty": "10", "max_qty": "49", "unit_price": "640.00"},
            {"min_qty": "50", "max_qty": None, "unit_price": "600.00"},
        ],
    )
    q = await _quote_for(client, admin_headers, "QT-TIER-1")
    for qty, expected in (("1", "680.00"), ("10", "640.00"), ("100", "600.00")):
        r = await client.post(
            f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
            json={"device_id": d["id"], "name": "t", "quantity": qty},
        )
        assert r.status_code == 201, r.text
        # 接口返回的是 Decimal 序列化结果，位数不固定 → 按数值比较
        assert Decimal(str(r.json()["unit_price"])) == Decimal(expected), (qty, r.json()["unit_price"])


async def test_tier_replace_rejects_overlap(client: AsyncClient, admin_headers) -> None:
    d = await _device_with_tiers(client, admin_headers, code="VTIER2", tiers=[])
    r = await client.put(
        f"/api/v1/devices/{d['id']}/price-tiers", headers=admin_headers,
        json=[
            {"min_qty": "1", "max_qty": "20", "unit_price": "100"},
            {"min_qty": "10", "max_qty": "30", "unit_price": "90"},
        ],
    )
    assert r.status_code == 422, r.text
    assert "重叠" in r.json()["detail"]


async def test_below_floor_blocked_for_sales(client: AsyncClient, admin_headers) -> None:
    """销售低于底价 → 422（不能靠"少报一点"把单子抢下来）。"""
    d = await _device_with_tiers(
        client, admin_headers, code="VFLOOR1", tiers=[], min_price="500.00"
    )
    q = await _quote_for(client, admin_headers, "QT-FLOOR-1")

    # 建一个销售账号
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "floorsales", "email": "floorsales@example.com",
              "password": "Sales12345!", "role": "sales"},
    )
    tok = (
        await client.post(
            "/api/v1/auth/login/json",
            json={"username": "floorsales", "password": "Sales12345!"},
        )
    ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}

    r = await client.post(
        f"/api/v1/quotes/{q['id']}/items", headers=h,
        json={"device_id": d["id"], "name": "t", "quantity": "1", "unit_price": "380.00"},
    )
    assert r.status_code == 422, r.text
    assert "底价" in r.json()["detail"]


async def test_below_floor_allowed_for_manager_with_reason(
    client: AsyncClient, admin_headers
) -> None:
    """经理/管理员 + 写明理由 → 放行，并且**留痕**。"""
    d = await _device_with_tiers(
        client, admin_headers, code="VFLOOR2", tiers=[], min_price="500.00"
    )
    q = await _quote_for(client, admin_headers, "QT-FLOOR-2")

    # admin 不给理由 → 也要拦（不能凭身份静默放行）
    no_reason = await client.post(
        f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
        json={"device_id": d["id"], "name": "t", "quantity": "1", "unit_price": "380.00"},
    )
    assert no_reason.status_code == 422, no_reason.text
    assert "理由" in no_reason.json()["detail"]

    ok = await client.post(
        f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
        json={"device_id": d["id"], "name": "t", "quantity": "1", "unit_price": "380.00",
              "below_floor_reason": "甲方年度框架协议价，已口头批准"},
    )
    assert ok.status_code == 201, ok.text

    logs = (
        await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=admin_headers)
    ).json()
    assert any(x["action"] == "price_floor_override" for x in logs), logs
    hit = next(x for x in logs if x["action"] == "price_floor_override")
    assert "框架协议价" in str(hit["payload"]), hit


async def test_floor_blocks_item_update(client: AsyncClient, admin_headers) -> None:
    """改单价同样受底线约束（否则"先正常建、再改低"就绕过去了）。"""
    d = await _device_with_tiers(
        client, admin_headers, code="VFLOOR3", tiers=[], min_price="500.00"
    )
    q = await _quote_for(client, admin_headers, "QT-FLOOR-3")
    item = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
            json={"device_id": d["id"], "name": "t", "quantity": "1"},
        )
    ).json()

    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item['id']}", headers=admin_headers,
        json={"unit_price": "300.00"},
    )
    assert r.status_code == 422, r.text
    assert "底价" in r.json()["detail"]

    # 带上理由就放行
    ok = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item['id']}", headers=admin_headers,
        json={"unit_price": "300.00", "below_floor_reason": "清库存"},
    )
    assert ok.status_code == 200, ok.text
    assert Decimal(str(ok.json()["unit_price"])) == Decimal("300.00")


async def test_floor_does_not_block_quantity_only_update(
    client: AsyncClient, admin_headers
) -> None:
    """只改数量不该被底线误拦（单价本来就在底价之上）。"""
    d = await _device_with_tiers(
        client, admin_headers, code="VFLOOR4", tiers=[], min_price="500.00"
    )
    q = await _quote_for(client, admin_headers, "QT-FLOOR-4")
    item = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
            json={"device_id": d["id"], "name": "t", "quantity": "1"},
        )
    ).json()
    r = await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{item['id']}", headers=admin_headers,
        json={"quantity": "5"},
    )
    assert r.status_code == 200, r.text
    assert Decimal(str(r.json()["quantity"])) == Decimal("5.00")


# ---------------------------------------------------------------------------
# 辅材「按工程量」口径（2026-10-04）
# ---------------------------------------------------------------------------

async def test_auxiliary_by_quantity_sums_qty_times_unit(
    client: AsyncClient, admin_headers
) -> None:
    """辅材费 = Σ(数量 × 单位辅材费)；费率不参与，且要明说被忽略。"""
    p = (
        await client.post(
            "/api/v1/projects", headers=admin_headers,
            json={"code": "PRJ-AUXQ-1", "name": "辅材口径", "customer_name": "客户",
                  "create_default_subsystems": False},
        )
    ).json()
    q = (
        await client.post(
            "/api/v1/quotes", headers=admin_headers,
            json={"project_id": p["id"], "code": "QT-AUXQ-1", "title": "按工程量",
                  "fee_base": "device_aux_install",
                  "auxiliary_base": "by_quantity",
                  # 故意给一个非 0 费率：按工程量口径下必须被忽略并提示
                  "auxiliary_rate": "0.06",
                  "design_rate": "0", "warranty_rate": "0",
                  "mgmt_profit_rate": "0", "tax_rate": "0.09"},
        )
    ).json()

    # 桥架 100 米 × 12 元/米 = 1200；网线 3 箱 × 80 = 240 → 辅材 1440
    for name, qty, unit_price, aux in (
        ("桥架", "100", "30", "12"),
        ("网线", "3", "500", "80"),
    ):
        r = await client.post(
            f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
            json={"name": name, "quantity": qty, "unit_price": unit_price,
                  "install_unit_price": "0", "auxiliary_unit_price": aux},
        )
        assert r.status_code == 201, r.text

    calc = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers)
    assert calc.status_code == 200, calc.text
    body = calc.json()

    assert Decimal(str(body["auxiliary_total"])) == Decimal("1440.00"), body["auxiliary_total"]
    joined = " ".join(body.get("warnings") or [])
    assert "费率不参与" in joined, body.get("warnings")


async def test_auxiliary_by_quantity_without_unit_price_warns(
    client: AsyncClient, admin_headers
) -> None:
    """选了按工程量却一行都没填单位辅材费 → 辅材 0，并明确警告（不能悄悄算 0）。"""
    p = (
        await client.post(
            "/api/v1/projects", headers=admin_headers,
            json={"code": "PRJ-AUXQ-2", "name": "空辅材", "customer_name": "客户",
                  "create_default_subsystems": False},
        )
    ).json()
    q = (
        await client.post(
            "/api/v1/quotes", headers=admin_headers,
            json={"project_id": p["id"], "code": "QT-AUXQ-2", "title": "空辅材",
                  "fee_base": "device_aux_install", "auxiliary_base": "by_quantity",
                  "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
                  "mgmt_profit_rate": "0", "tax_rate": "0.09"},
        )
    ).json()
    await client.post(
        f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
        json={"name": "设备", "quantity": "1", "unit_price": "100",
              "install_unit_price": "0"},
    )
    calc = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers)
    body = calc.json()

    assert Decimal(str(body["auxiliary_total"])) == Decimal("0")
    assert "没有任何明细行填了单位辅材费" in " ".join(body.get("warnings") or []), body


async def test_device_auxiliary_price_carried_into_item(
    client: AsyncClient, admin_headers
) -> None:
    """设备库里的单位辅材费要随设备带进报价明细。"""
    d = (
        await client.post(
            "/api/v1/devices", headers=admin_headers,
            json={"code": "VAUX1", "name": "带辅材的设备", "model": "VAUX-M1",
                  "category": "video", "unit": "台", "reference_price": "500",
                  "cost_price": "300", "install_unit_price": "50",
                  "auxiliary_unit_price": "8.5"},
        )
    ).json()
    assert str(d["auxiliary_unit_price"]) != "0", d

    p = (
        await client.post(
            "/api/v1/projects", headers=admin_headers,
            json={"code": "PRJ-AUXQ-3", "name": "带出辅材", "customer_name": "客户",
                  "create_default_subsystems": False},
        )
    ).json()
    q = (
        await client.post(
            "/api/v1/quotes", headers=admin_headers,
            json={"project_id": p["id"], "code": "QT-AUXQ-3", "title": "带出辅材",
                  "fee_base": "device_aux_install", "auxiliary_base": "by_quantity",
                  "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
                  "mgmt_profit_rate": "0", "tax_rate": "0.09"},
        )
    ).json()
    item = (
        await client.post(
            f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
            json={"device_id": d["id"], "name": d["name"], "quantity": "10"},
        )
    ).json()

    assert Decimal(str(item["auxiliary_unit_price"])) == Decimal("8.5"), item

    calc = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers)
    assert Decimal(str(calc.json()["auxiliary_total"])) == Decimal("85.00"), calc.json()["auxiliary_total"]


async def test_device_based_auxiliary_still_uses_rate(
    client: AsyncClient, admin_headers
) -> None:
    """其它口径不受影响：按设备费 × 费率仍然照旧。"""
    p = (
        await client.post(
            "/api/v1/projects", headers=admin_headers,
            json={"code": "PRJ-AUXQ-4", "name": "设备费口径", "customer_name": "客户",
                  "create_default_subsystems": False},
        )
    ).json()
    q = (
        await client.post(
            "/api/v1/quotes", headers=admin_headers,
            json={"project_id": p["id"], "code": "QT-AUXQ-4", "title": "设备费口径",
                  "fee_base": "device_aux_install", "auxiliary_base": "device",
                  "auxiliary_rate": "0.06", "design_rate": "0", "warranty_rate": "0",
                  "mgmt_profit_rate": "0", "tax_rate": "0.09"},
        )
    ).json()
    await client.post(
        f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
        json={"name": "设备", "quantity": "10", "unit_price": "100",
              "install_unit_price": "0", "auxiliary_unit_price": "999"},
    )
    calc = await client.post(f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers)
    body = calc.json()

    # 设备费 1000 × 6% = 60（单位辅材费 999 不该被算进来）
    assert Decimal(str(body["auxiliary_total"])) == Decimal("60.00"), body["auxiliary_total"]
    assert Decimal(str(body["equipment_total"])) == Decimal("1000.00")


# ---------------------------------------------------------------------------
# 版本对比（2026-10-04）
# ---------------------------------------------------------------------------

async def _quote_with_items(client: AsyncClient, headers, code: str, items: list[dict]) -> dict:
    p = (
        await client.post(
            "/api/v1/projects", headers=headers,
            json={"code": f"{code}-P", "name": "版本测试项目", "customer_name": "客户",
                  "create_default_subsystems": False},
        )
    ).json()
    q = (
        await client.post(
            "/api/v1/quotes", headers=headers,
            json={"project_id": p["id"], "code": code, "title": "版本测试",
                  "fee_base": "device_aux_install", "auxiliary_base": "device",
                  "auxiliary_rate": "0.06", "design_rate": "0", "warranty_rate": "0",
                  "mgmt_profit_rate": "0", "tax_rate": "0.09"},
        )
    ).json()
    for it in items:
        r = await client.post(f"/api/v1/quotes/{q['id']}/items", headers=headers, json=it)
        assert r.status_code == 201, r.text
    return q


async def test_submit_and_approve_record_versions(client: AsyncClient, admin_headers) -> None:
    """提交 = 报出去的那一版；批准 = 定下来的那一版。两版都要在。"""
    q = await _quote_with_items(
        client, admin_headers, "QT-REV-1",
        [{"name": "摄像机", "quantity": "10", "unit_price": "500", "install_unit_price": "50"}],
    )
    assert (await client.patch(f"/api/v1/quotes/{q['id']}",
                               headers=admin_headers,
                               json={"status": "submitted"})).status_code == 200
    assert (await client.patch(f"/api/v1/quotes/{q['id']}",
                               headers=admin_headers,
                               json={"status": "reviewing"})).status_code == 200
    assert (await client.patch(f"/api/v1/quotes/{q['id']}",
                               headers=admin_headers,
                               json={"status": "approved"})).status_code == 200

    rows = (await client.get(f"/api/v1/quotes/{q['id']}/revisions", headers=admin_headers)).json()
    reasons = [r["reason"] for r in rows]
    assert "submitted" in reasons, rows
    assert "approved" in reasons, rows
    first = next(r for r in rows if r["reason"] == "submitted")
    assert first["item_count"] == 1
    assert Decimal(str(first["totals"]["total_with_tax"])) > 0, first


async def test_manual_revision_and_diff_shows_price_change(
    client: AsyncClient, admin_headers
) -> None:
    """中途打点 + 对比：改价与新增行都要能看出来，并给出金额差。"""
    q = await _quote_with_items(
        client, admin_headers, "QT-REV-2",
        [{"name": "摄像机", "quantity": "10", "unit_price": "500", "install_unit_price": "0"}],
    )
    r1 = await client.post(f"/api/v1/quotes/{q['id']}/revisions",
                           params={"note": "初稿"}, headers=admin_headers)
    assert r1.status_code == 201, r1.text
    seq1 = r1.json()["seq"]

    # 改价 + 加一行
    items = (await client.get(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers)).json()
    cam = items[0]
    assert (await client.patch(
        f"/api/v1/quotes/{q['id']}/items/{cam['id']}", headers=admin_headers,
        json={"unit_price": "600", "below_floor_reason": "版本测试改价"},
    )).status_code in (200, 422)
    # 上面若被底价拦住也没关系（本用例没设底价，应为 200）
    await client.post(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers,
                      json={"name": "网线", "quantity": "3", "unit_price": "500",
                            "install_unit_price": "0"})
    r2 = await client.post(f"/api/v1/quotes/{q['id']}/revisions",
                           params={"note": "甲方砍价后"}, headers=admin_headers)
    seq2 = r2.json()["seq"]

    diff = (
        await client.get(
            f"/api/v1/quotes/{q['id']}/revisions/diff",
            params={"from_seq": seq1, "to_seq": seq2}, headers=admin_headers,
        )
    ).json()
    assert diff["has_changes"] is True, diff
    assert diff["items_added"] and diff["items_added"][0]["name"] == "网线", diff
    changed = diff["items_changed"][0]
    price_field = next(f for f in changed["fields"] if f["field"] == "unit_price")
    assert Decimal(price_field["from"]) == Decimal("500")
    assert Decimal(price_field["to"]) == Decimal("600")
    totals = {t["field"]: t for t in diff["totals"]}
    assert "total_with_tax" in totals, diff["totals"]
    assert Decimal(totals["total_with_tax"]["delta"]) > 0, totals


async def test_diff_unknown_seq_404(client: AsyncClient, admin_headers) -> None:
    q = await _quote_with_items(client, admin_headers, "QT-REV-3", [])
    await client.post(f"/api/v1/quotes/{q['id']}/revisions", headers=admin_headers)
    r = await client.get(
        f"/api/v1/quotes/{q['id']}/revisions/diff",
        params={"from_seq": 1, "to_seq": 99}, headers=admin_headers,
    )
    assert r.status_code == 404, r.text


async def test_revision_requires_quote_read_permission(
    client: AsyncClient, admin_headers
) -> None:
    """版本历史跟着报价单读权限走（能看单就能看版本）。"""
    q = await _quote_with_items(client, admin_headers, "QT-REV-4", [])
    await client.post(f"/api/v1/quotes/{q['id']}/revisions", headers=admin_headers)
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "revviewer", "email": "revviewer@example.com",
              "password": "Viewer12345!", "role": "viewer"},
    )
    tok = (
        await client.post("/api/v1/auth/login/json",
                          json={"username": "revviewer", "password": "Viewer12345!"})
    ).json()["access_token"]
    r = await client.get(f"/api/v1/quotes/{q['id']}/revisions",
                         headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    # 但 viewer 不能手动存档（需要 quote.update）
    denied = await client.post(f"/api/v1/quotes/{q['id']}/revisions",
                               headers={"Authorization": f"Bearer {tok}"})
    assert denied.status_code == 403, denied.text


# ---------------------------------------------------------------------------
# 分级审批（2026-10-04）
# ---------------------------------------------------------------------------

async def test_approval_disabled_by_default(client: AsyncClient, admin_headers) -> None:
    """默认不开分级审批：批准行为与现状完全一致（避免上线即改变既有流程）。"""
    q = await _quote_with_items(
        client, admin_headers, "QT-APP-1",
        [{"name": "设备", "quantity": "1", "unit_price": "1000", "install_unit_price": "0"}],
    )
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "submitted"})
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "reviewing"})
    r = await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                           json={"status": "approved"})
    assert r.status_code == 200, r.text

    chain = (await client.get(f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers)).json()
    assert chain["enabled"] is False
    assert chain["required_levels"] == 1


async def test_multi_level_approval_flow(client: AsyncClient, admin_headers) -> None:
    """开启后：超过阈值要批多级；未批满时直接批准被拒。"""
    # 开启分级审批，并把阈值调低（10 万 → 2 级），便于测试
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"approval": {
            "enabled": True,
            "tiers": [{"min_amount": 0, "levels": 1}, {"min_amount": 1000, "levels": 2}],
            "reject_requires_comment": True,
        }},
    )
    assert r.status_code == 200, r.text

    q = await _quote_with_items(
        client, admin_headers, "QT-APP-2",
        [{"name": "设备", "quantity": "10", "unit_price": "1000", "install_unit_price": "0"}],
    )
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "submitted"})
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "reviewing"})

    chain = (await client.get(f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers)).json()
    assert chain["enabled"] is True
    assert chain["required_levels"] == 2, chain
    assert chain["can_finalize"] is False

    # 未批满 → 直接批准被拒
    blocked = await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                                 json={"status": "approved"})
    assert blocked.status_code == 409, blocked.text
    assert "分级审批" in blocked.json()["detail"]

    # 第 1 级通过 → 还没完
    a1 = await client.post(f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers,
                           json={"decision": "approved", "comment": "造价复核通过"})
    assert a1.status_code == 201, a1.text
    assert a1.json()["finalized"] is False
    assert a1.json()["status"] == "reviewing"

    # 第 2 级通过 → 自动置为已批准
    a2 = await client.post(f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers,
                           json={"decision": "approved", "comment": "经理批准"})
    assert a2.status_code == 201, a2.text
    assert a2.json()["finalized"] is True
    assert a2.json()["status"] == "approved"

    # 审批记录与版本快照都在
    records = (await client.get(f"/api/v1/quotes/{q['id']}/approvals",
                                headers=admin_headers)).json()["records"]
    assert [x["level"] for x in records] == [1, 2], records
    assert records[0]["comment"] == "造价复核通过"
    revs = (await client.get(f"/api/v1/quotes/{q['id']}/revisions", headers=admin_headers)).json()
    assert any(x["reason"] == "approved" for x in revs), revs


async def test_reject_requires_comment_and_stops_chain(
    client: AsyncClient, admin_headers
) -> None:
    """驳回必须写意见；驳回后单据直接置为已驳回，链条终止。"""
    await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"approval": {"enabled": True, "reject_requires_comment": True,
                           "tiers": [{"min_amount": 0, "levels": 2}]}},
    )
    q = await _quote_with_items(
        client, admin_headers, "QT-APP-3",
        [{"name": "设备", "quantity": "1", "unit_price": "100", "install_unit_price": "0"}],
    )
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "submitted"})
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "reviewing"})

    no_comment = await client.post(f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers,
                                   json={"decision": "rejected"})
    assert no_comment.status_code == 422, no_comment.text

    rejected = await client.post(
        f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers,
        json={"decision": "rejected", "comment": "单价明显低于底价，重报"},
    )
    assert rejected.status_code == 201, rejected.text
    assert rejected.json()["status"] == "rejected"

    # 已驳回的单子不能再审批
    again = await client.post(f"/api/v1/quotes/{q['id']}/approvals", headers=admin_headers,
                              json={"decision": "approved", "comment": "想再批"})
    assert again.status_code == 409, again.text


async def test_approval_requires_approve_permission(client: AsyncClient, admin_headers) -> None:
    """没有 quote:approve 的人不能审批（sales 就没有）。"""
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "appsales", "email": "appsales@example.com",
              "password": "Sales12345!", "role": "sales"},
    )
    tok = (await client.post("/api/v1/auth/login/json",
                             json={"username": "appsales", "password": "Sales12345!"})
           ).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}
    q = await _quote_with_items(client, admin_headers, "QT-APP-4", [])
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "submitted"})
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers,
                       json={"status": "reviewing"})
    r = await client.post(f"/api/v1/quotes/{q['id']}/approvals", headers=h,
                          json={"decision": "approved", "comment": "越权"})
    assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 站内通知（2026-10-04）
# ---------------------------------------------------------------------------

async def _login(client: AsyncClient, username: str, password: str) -> dict:
    tok = (
        await client.post("/api/v1/auth/login/json",
                          json={"username": username, "password": password})
    ).json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


async def test_submit_notifies_approvers_not_submitter(
    client: AsyncClient, admin_headers
) -> None:
    """提交后：有审批权的人收到待审批通知；提交人自己不收（不需要提醒自己）。"""
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "notifmgr", "email": "notifmgr@example.com",
              "password": "Mgr12345!", "role": "manager"},
    )
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "notifsales", "email": "notifsales@example.com",
              "password": "Sales12345!", "role": "sales"},
    )
    sh = await _login(client, "notifsales", "Sales12345!")
    mh = await _login(client, "notifmgr", "Mgr12345!")

    # 销售建项目 → 建单 → 提交（销售有 project/quote 写权限）
    p = (await client.post("/api/v1/projects", headers=sh, json={
        "code": "PRJ-NOTIF-1", "name": "通知测试", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    q = (await client.post("/api/v1/quotes", headers=sh, json={
        "project_id": p["id"], "code": "QT-NOTIF-1", "title": "通知测试",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0.06", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0.09"})).json()
    assert (await client.patch(f"/api/v1/quotes/{q['id']}", headers=sh,
                               json={"status": "submitted"})).status_code == 200

    mgr = (await client.get("/api/v1/notifications", headers=mh)).json()
    assert mgr["unread"] >= 1, mgr
    hit = [n for n in mgr["items"] if n["kind"] == "approval_pending"]
    assert hit, mgr
    assert q["code"] in hit[0]["title"]
    # 2026-10-07 改：链接指向**真实路由** `/projects/<项目id>/workbench/<报价id>` ✓
    #   原先写的是 `/quotes/<报价id>` ✗ —— 前端没有这条路由 ✗，点开是"页面不存在" ✗。
    assert hit[0]["link"] == f"/projects/{p['id']}/workbench/{q['id']}"

    # 提交人自己不该收到"待审批"提醒（他没审批权）
    sales = (await client.get("/api/v1/notifications", headers=sh)).json()
    assert not [n for n in sales["items"] if n["kind"] == "approval_pending"], sales


async def test_approve_notifies_owner(client: AsyncClient, admin_headers) -> None:
    """批准/驳回 → 报单的人收到通知。"""
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "notifmgr2", "email": "notifmgr2@example.com",
              "password": "Mgr12345!", "role": "manager"},
    )
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "notifsales2", "email": "notifsales2@example.com",
              "password": "Sales12345!", "role": "sales"},
    )
    sh = await _login(client, "notifsales2", "Sales12345!")
    mh = await _login(client, "notifmgr2", "Mgr12345!")

    p = (await client.post("/api/v1/projects", headers=sh, json={
        "code": "PRJ-NOTIF-2", "name": "通知测试2", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    q = (await client.post("/api/v1/quotes", headers=sh, json={
        "project_id": p["id"], "code": "QT-NOTIF-2", "title": "通知测试2",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0.06", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0.09"})).json()
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=sh, json={"status": "submitted"})
    # 「接手审核」是审批动作，销售没有 quote:approve → 必须由经理做。
    # 以前这里用销售调，静默 403，后面批准就卡在 submitted（409）。
    assert (await client.patch(f"/api/v1/quotes/{q['id']}", headers=mh,
                               json={"status": "reviewing"})).status_code == 200, "接手审核应由经理做"
    assert (await client.patch(f"/api/v1/quotes/{q['id']}", headers=mh,
                               json={"status": "approved"})).status_code == 200

    sales = (await client.get("/api/v1/notifications", headers=sh)).json()
    approved = [n for n in sales["items"] if n["kind"] == "quote_approved"]
    assert approved, sales
    assert q["code"] in approved[0]["title"]


async def test_notifications_are_per_user_and_markable(
    client: AsyncClient, admin_headers
) -> None:
    """通知只归本人；单条已读、全部已读都要生效；别人的通知标不到。"""
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "notifmgr3", "email": "notifmgr3@example.com",
              "password": "Mgr12345!", "role": "manager"},
    )
    await client.post(
        "/api/v1/users", headers=admin_headers,
        json={"username": "notifsales3", "email": "notifsales3@example.com",
              "password": "Sales12345!", "role": "sales"},
    )
    sh = await _login(client, "notifsales3", "Sales12345!")
    mh = await _login(client, "notifmgr3", "Mgr12345!")

    p = (await client.post("/api/v1/projects", headers=sh, json={
        "code": "PRJ-NOTIF-3", "name": "通知测试3", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    q = (await client.post("/api/v1/quotes", headers=sh, json={
        "project_id": p["id"], "code": "QT-NOTIF-3", "title": "通知测试3",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0.06", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0.09"})).json()
    await client.patch(f"/api/v1/quotes/{q['id']}", headers=sh, json={"status": "submitted"})

    mine = (await client.get("/api/v1/notifications", headers=mh)).json()
    assert mine["items"], mine
    nid = mine["items"][0]["id"]

    # 别人（销售）标不了我的通知
    denied = await client.post(f"/api/v1/notifications/{nid}/read", headers=sh)
    assert denied.status_code == 404, denied.text

    # 本人可以
    assert (await client.post(f"/api/v1/notifications/{nid}/read", headers=mh)).status_code == 204
    after = (await client.get("/api/v1/notifications", headers=mh)).json()
    assert all(n["id"] != nid for n in after["items"]), after

    # 此时已无未读 → read-all 标 0 条（这是对的，不是 bug）
    assert (await client.post("/api/v1/notifications/read-all",
                              headers=mh)).json()["marked"] == 0

    # 再提交一单产生新的未读，然后"全部已读"要能一次清掉
    q2 = (await client.post("/api/v1/quotes", headers=sh, json={
        "project_id": p["id"], "code": "QT-NOTIF-3B", "title": "通知测试3B",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0.06", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0.09"})).json()
    await client.patch(f"/api/v1/quotes/{q2['id']}", headers=sh, json={"status": "submitted"})
    marked = (await client.post("/api/v1/notifications/read-all", headers=mh)).json()
    assert marked["marked"] >= 1, marked
    assert (await client.get("/api/v1/notifications", headers=mh)).json()["unread"] == 0

    # 已读的仍然可以在"全部"里看到
    allrows = (await client.get("/api/v1/notifications",
                                params={"unread_only": False}, headers=mh)).json()
    assert any(n["id"] == nid for n in allrows["items"]), allrows

async def test_strategy_rates_locked_after_creation(client, db, admin_headers):
    """辅材率 / 管理费率：创建报价时确定，之后不可改 ✗（2026-10-05 用户决策④ ✓）。

    只开放 4 项：计税方式 / 税率 / 人工浮动 / 有效期 ✓。
    """
    p = await _make_project(db)
    q = await _make_quote(client, admin_headers, p.id)
    qid = q["id"]

    # ① 被锁的两项：422，且提示里说清"创建时确定"
    for field in ("auxiliary_rate", "mgmt_profit_rate"):
        r = await client.patch(f"/api/v1/quotes/{qid}", headers=admin_headers, json={field: "0.07"})
        assert r.status_code == 422, f"{field} 应被锁 422，实际 {r.status_code}"
        assert "不可修改" in r.text, r.text

    # ② 允许的四项：200 且真的生效
    r = await client.patch(
        f"/api/v1/quotes/{qid}", headers=admin_headers,
        json={"tax_rate": "0.06", "labor_float_rate": "0.10",
              "tax_method": "general_9pct", "valid_days_override": 45},
    )
    assert r.status_code == 200, r.text
    got = (await client.get(f"/api/v1/quotes/{qid}", headers=admin_headers)).json()
    assert abs(float(got["tax_rate"]) - 0.06) < 1e-9, got
    assert abs(float(got["labor_float_rate"]) - 0.10) < 1e-9, got
    assert got["valid_days_override"] == 45, got

async def test_addon_quote_inherits_non_default_rates(client, db, admin_headers) -> None:
    """增项单必须继承原单的**非默认**费率/模式（2026-10-07 回归测试）。

    为什么必须用非默认值 ✓：首版实现用的是 body.model_dump()（没有 exclude_unset）✗
    → Pydantic 的默认值总会进 data → 「调用方没传就沿用原单」的判断永不成立 ✗ →
    增项单的费率被静默重置成默认值 ✗（实测：原单 quota/6%/0.11… → 增项 manual/9%/0.06… ✗）。
    当时用一张**恰好全是默认值**的原单验证，所以没暴露 ✗ —— 这条测试专门钉住非默认值 ✓。
    """
    from uuid import uuid4

    code = f"PRJ-ADDON-{uuid4().hex[:6].upper()}"
    r = await client.post(
        "/api/v1/projects",
        json={"code": code, "name": "增项复制回归", "customer_name": "自检"},
        headers=admin_headers,
    )
    assert r.status_code == 201, r.text
    pid = r.json()["id"]
    src_id = add_id = None
    try:
        # 原单：**非默认**费率 + quota 模式
        r = await client.post(
            "/api/v1/quotes",
            json={
                "project_id": pid,
                "title": "原单（非默认费率）",
                "install_mode": "quota",
                "tax_rate": "0.06",
                "auxiliary_rate": "0.11",
                "design_rate": "0.05",
                "warranty_rate": "0.07",
                "mgmt_profit_rate": "0.13",
                "training_fee": "888",
                "fee_base": "device_only",
            },
            headers=admin_headers,
        )
        assert r.status_code == 201, r.text
        src = r.json()
        src_id = src["id"]
        # 增项单：只给 based_on_quote_id ✓ → 应继承原单的一切
        r = await client.post(
            "/api/v1/quotes",
            json={"project_id": pid, "title": "增项", "based_on_quote_id": src_id},
            headers=admin_headers,
        )
        assert r.status_code == 201, r.text
        add = r.json()
        add_id = add["id"]

        assert add["based_on_quote_id"] == src_id, add
        assert add["install_mode"] == "quota", add  # 若不修 bug，这里会是 manual ✗
        assert Decimal(str(add["tax_rate"])) == Decimal("0.06"), add
        assert Decimal(str(add["auxiliary_rate"])) == Decimal("0.11"), add
        assert Decimal(str(add["design_rate"])) == Decimal("0.05"), add
        assert Decimal(str(add["warranty_rate"])) == Decimal("0.07"), add
        assert Decimal(str(add["mgmt_profit_rate"])) == Decimal("0.13"), add
        assert Decimal(str(add["training_fee"])) == Decimal("888"), add
        assert str(add["fee_base"]) == "device_only", add
    finally:
        for qid in (add_id, src_id):
            if qid:
                await client.delete(f"/api/v1/quotes/{qid}", headers=admin_headers)
        await client.delete(f"/api/v1/projects/{pid}", headers=admin_headers)
