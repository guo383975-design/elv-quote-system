"""设备库 API 测试：CRUD、统计、精确/模糊匹配 endpoint、替代建议."""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceCategory, DeviceStatus
from app.models.project import Project, ProjectStatus, ProjectType

# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_device(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={
            "code": "V2001",
            "name": "网络高清枪型摄像机 400万",
            "brand": "海康威视",
            "model": "DS-2CD2T46WD-I5",
            "unit": "台",
            "category": "video",
            "reference_price": "680.00",
            "cost_price": "510.00",
            "install_unit_price": "112.60",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "V2001"
    assert body["status"] == "on_sale"


@pytest.mark.asyncio
async def test_create_device_duplicate_code_409(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={"code": "V1001", "name": "重复", "category": "video"},
    )
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_auto_code_generation(client: AsyncClient, admin_headers, devices) -> None:
    """已有 V1001 / V1008 / V1009 → 下一个视频编号 V1010。"""
    r = await client.post(
        "/api/v1/devices/auto-code",
        headers=admin_headers,
        params={"category": "video"},
    )
    assert r.status_code == 200
    assert r.json()["code"] == "V1010"


@pytest.mark.asyncio
async def test_auto_code_for_empty_category(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.post(
        "/api/v1/devices/auto-code",
        headers=admin_headers,
        params={"category": "parking"},
    )
    assert r.json()["code"] == "P0001"


@pytest.mark.asyncio
async def test_list_devices_filter_by_status(
    client: AsyncClient, admin_headers, devices
) -> None:
    r = await client.get(
        "/api/v1/devices", headers=admin_headers, params={"status": "eol"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "V1008"


@pytest.mark.asyncio
async def test_list_devices_keyword_matches_model(
    client: AsyncClient, admin_headers, devices
) -> None:
    r = await client.get(
        "/api/v1/devices", headers=admin_headers, params={"keyword": "S1730S"}
    )
    assert r.json()["total"] == 1
    assert r.json()["items"][0]["code"] == "N5001"


@pytest.mark.asyncio
async def test_list_devices_only_common(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.get(
        "/api/v1/devices", headers=admin_headers, params={"only_common": True}
    )
    assert r.status_code == 200
    assert r.json()["total"] == 0  # fixture 里都没标常用


@pytest.mark.asyncio
async def test_device_stats(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.get("/api/v1/devices/stats", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 4
    assert body["by_status"]["on_sale"] == 3
    assert body["by_status"]["eol"] == 1
    assert body["with_replacement"] == 1


@pytest.mark.asyncio
async def test_update_device_status_and_replacement(
    client: AsyncClient, admin_headers, devices
) -> None:
    """把在售设备标为停产并挂上替代型号。"""
    r = await client.patch(
        f"/api/v1/devices/{devices[0].id}",
        headers=admin_headers,
        json={"status": "eol", "replacement_id": str(devices[2].id)},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "eol"
    assert r.json()["replacement_id"] == str(devices[2].id)


@pytest.mark.asyncio
async def test_device_cannot_be_its_own_replacement(
    client: AsyncClient, admin_headers, devices
) -> None:
    r = await client.patch(
        f"/api/v1/devices/{devices[0].id}",
        headers=admin_headers,
        json={"replacement_id": str(devices[0].id)},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_deactivate_device_is_soft_delete(
    client: AsyncClient, admin_headers, devices
) -> None:
    r = await client.delete(f"/api/v1/devices/{devices[0].id}", headers=admin_headers)
    assert r.status_code == 204

    listing = await client.get("/api/v1/devices", headers=admin_headers)
    assert all(d["code"] != "V1001" for d in listing.json()["items"])

    stats = await client.get("/api/v1/devices/stats", headers=admin_headers)
    assert stats.json()["total"] == 3


@pytest.mark.asyncio
async def test_estimator_can_create_but_viewer_cannot(
    client: AsyncClient, est_headers, viewer_headers
) -> None:
    ok = await client.post(
        "/api/v1/devices",
        headers=est_headers,
        json={"code": "C9001", "name": "测试线缆", "category": "cabling"},
    )
    assert ok.status_code == 201

    denied = await client.post(
        "/api/v1/devices",
        headers=viewer_headers,
        json={"code": "C9002", "name": "测试线缆2", "category": "cabling"},
    )
    assert denied.status_code == 403


# ---------------------------------------------------------------------------
# 字段级权限：DeviceOut.cost_price 仅 admin / estimator / finance 可见
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cost_price_visible_to_cost_role(
    client: AsyncClient, admin_headers, finance_headers, est_headers, devices
) -> None:
    """成本可见性（2026-10-05 新口径）。

      · admin / finance / purchaser / manager 看得到 cost_price；
      · **工程部（estimator）看不到** —— 用户要求"工程部只能看设备清单"。

    这条改动意味着：工程部仍然能看设备清单（型号/规格/参考价），
    但看不到进价；若你希望造价员照旧能看到进价，改
    「系统设置 → 数据可见性」把 estimator 勾进"成本"即可，不用改代码。
    """
    r = await client.get("/api/v1/devices", headers=admin_headers)
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["cost_price"] is not None, "admin 应当看到 cost_price"

    r2 = await client.get("/api/v1/devices", headers=finance_headers)
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["cost_price"] is not None, "finance 应当看到 cost_price"

    # 工程部：设备清单照旧能看，但**看不到进价**
    r3 = await client.get("/api/v1/devices", headers=est_headers)
    assert r3.status_code == 200, r3.text
    items3 = r3.json()["items"]
    assert items3, "工程部必须还能看设备清单本身"
    for item in items3:
        assert item["cost_price"] is None, "工程部不应当看到 cost_price（新口径）"


@pytest.mark.asyncio
async def test_cost_price_hidden_from_sales_and_viewer(
    client: AsyncClient, sales_headers, viewer_headers, devices
) -> None:
    """sales / viewer 调 GET /devices：cost_price 必须为 None。

    以前 list / get / update / replacements / match 全都把 cost_price 透出来，
    同一接口不同角色字段相同。改成裁剪式返回（_device_out_for_view）后必须
    看不到 c 字段。
    """
    r = await client.get("/api/v1/devices", headers=sales_headers)
    assert r.status_code == 200
    items = r.json()["items"]
    assert items, "应当至少返回 fixture 里的设备"
    for item in items:
        assert item["cost_price"] is None, f"sales 看到 cost_price={item['cost_price']}"

    r2 = await client.get("/api/v1/devices", headers=viewer_headers)
    assert r2.status_code == 200
    for item in r2.json()["items"]:
        assert item["cost_price"] is None, f"viewer 看到 cost_price={item['cost_price']}"


@pytest.mark.asyncio
async def test_cost_price_hidden_in_device_detail_and_replacements(
    client: AsyncClient, sales_headers, viewer_headers, devices
) -> None:
    """单条 GET /devices/{id} 和 GET /devices/{id}/replacements 也要裁掉 cost_price。"""
    did = devices[0].id

    detail = await client.get(f"/api/v1/devices/{did}", headers=sales_headers)
    assert detail.status_code == 200
    assert detail.json()["cost_price"] is None

    repl = await client.get(
        f"/api/v1/devices/{devices[1].id}/replacements", headers=viewer_headers
    )
    assert repl.status_code == 200
    # 外层 device 字段也是 DeviceOut → 同样要裁
    assert repl.json()["device"]["cost_price"] is None


@pytest.mark.asyncio
async def test_cost_price_visible_to_finance(
    client: AsyncClient, db: AsyncSession, devices
) -> None:
    """finance 走只读 + 成本视图可见 → 应当能看到 cost_price。"""
    from app.core.security import hash_password
    from app.models.user import User, UserStatus

    user = User(
        username="finance1",
        email="finance1@test.com",
        hashed_password=hash_password("fin12345"),
        role="finance",
        status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.commit()

    login = await client.post(
        "/api/v1/auth/login/json",
        json={"username": "finance1", "password": "fin12345"},
    )
    assert login.status_code == 200, login.text
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    r = await client.get("/api/v1/devices", headers=headers)
    assert r.status_code == 200
    for item in r.json()["items"]:
        assert item["cost_price"] is not None, "finance 应当看到 cost_price"


# ---------------------------------------------------------------------------
# 匹配 endpoint（场景二核心）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_match_exact_via_endpoint(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.post(
        "/api/v1/devices/match",
        headers=admin_headers,
        params={"name": "网络高清枪型摄像机 400万", "model": "DS-2CD2T46WD-I5",
                "brand": "海康威视"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["match_type"] == "exact"
    assert body["best"]["code"] == "V1001"


@pytest.mark.asyncio
async def test_match_fuzzy_name_only(client: AsyncClient, admin_headers, devices) -> None:
    """甲方清单常只有名称 → 应给出候选而非直接失败。"""
    r = await client.post(
        "/api/v1/devices/match",
        headers=admin_headers,
        params={"name": "网络高清枪型摄像机"},
    )
    body = r.json()
    assert body["match_type"] in ("exact", "fuzzy")
    assert len(body["candidates"]) >= 1


@pytest.mark.asyncio
async def test_match_flags_discontinued(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.post(
        "/api/v1/devices/match",
        headers=admin_headers,
        params={"name": "网络高清枪型摄像机", "model": "DH-IPC-HFW3441M", "brand": "大华"},
    )
    body = r.json()
    assert body["best"]["code"] == "V1008"
    assert body["best"]["is_discontinued"] is True


@pytest.mark.asyncio
async def test_match_none_for_unrelated(client: AsyncClient, admin_headers, devices) -> None:
    r = await client.post(
        "/api/v1/devices/match",
        headers=admin_headers,
        params={"name": "电梯五方对讲主机", "model": "ZZZ-0000"},
    )
    assert r.json()["match_type"] == "none"


@pytest.mark.asyncio
async def test_match_respects_threshold(client: AsyncClient, admin_headers, devices) -> None:
    low = await client.post(
        "/api/v1/devices/match",
        headers=admin_headers,
        params={"name": "网络高清枪型", "threshold": 10},
    )
    high = await client.post(
        "/api/v1/devices/match",
        headers=admin_headers,
        params={"name": "网络高清枪型", "threshold": 99},
    )
    assert len(low.json()["candidates"]) >= len(high.json()["candidates"])


# ---------------------------------------------------------------------------
# 替代建议
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replacements_for_eol_with_configured_target(
    client: AsyncClient, admin_headers, devices
) -> None:
    r = await client.get(
        f"/api/v1/devices/{devices[1].id}/replacements", headers=admin_headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "configured"
    assert body["candidates"][0]["code"] == "V1009"
    # 688 - 645 = 43
    assert body["candidates"][0]["price_delta"] == 43.0
    # 回归：两个分支的候选结构必须一致。曾经 configured 回的是 DeviceOut（用 id），
    # similarity 回的是 MatchCandidate（用 device_id），前端得写两套取值逻辑。
    assert body["candidates"][0]["device_id"] == str(devices[2].id)
    assert body["candidates"][0]["source"] == "configured"
    assert "score_detail" in body["candidates"][0]


@pytest.mark.asyncio
async def test_replacements_falls_back_to_similarity(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """无配置替代型号 → 按相似度推荐同分类在售型号。"""
    eol = Device(
        code="V1010", name="网络高清筒机 400万 星光级", brand="海康威视",
        model="DS-2CD3T46WD-I3", unit="台", category=DeviceCategory.VIDEO,
        reference_price=Decimal("698.00"), cost_price=Decimal("524.00"),
        install_unit_price=Decimal("112.60"), status=DeviceStatus.EOL,
    )
    db.add(eol)
    await db.commit()
    await db.refresh(eol)

    r = await client.get(
        f"/api/v1/devices/{eol.id}/replacements", headers=admin_headers
    )
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "similarity"
    assert len(body["candidates"]) >= 1
    for c in body["candidates"]:
        assert c["status"] == "on_sale"
        assert c["code"] != "V1010"
        # 与 configured 分支同构
        assert c["device_id"]
        assert c["source"] == "similarity"
        assert "score_detail" in c


# ---------------------------------------------------------------------------
# 回归：DeviceOut / DeviceUpdate 的字段完整性
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_device_out_exposes_form_fields(
    client: AsyncClient, admin_headers, devices
) -> None:
    """DeviceOut 必须带上编辑表单要回显的字段。

    这几个字段 ORM 里有、界面也在用，但以前没进 DeviceOut：
    「常用」永远是未勾选，质保年限/起订量永远显示默认值，
    用户照着错的默认值一保存，真实配置就被覆盖了。
    """
    r = await client.get(f"/api/v1/devices/{devices[0].id}", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    for field in ("is_common", "moq", "warranty_years", "unit", "category",
                  "subsystem_default", "eol_at", "supplier_id"):
        assert field in body, f"DeviceOut 缺少 {field}"


@pytest.mark.asyncio
async def test_update_device_persists_all_editable_fields(
    client: AsyncClient, admin_headers, devices
) -> None:
    """编辑设备时改的每个字段都要落库。

    DeviceUpdate 以前只有 10 个字段，单位/分类/甲供/常用/质保/起订量/停产日期
    会被 pydantic 静默忽略：提示「设备已更新」，刷新后还是旧值。
    """
    did = devices[0].id
    r = await client.patch(
        f"/api/v1/devices/{did}",
        headers=admin_headers,
        json={
            "unit": "套",
            "category": "network",
            "is_owner_supplied": True,
            "is_common": True,
            "warranty_years": 5,
            "moq": 10,
            "eol_at": "2026-12-31",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unit"] == "套"
    assert body["category"] == "network"
    assert body["is_owner_supplied"] is True
    assert body["is_common"] is True
    assert body["warranty_years"] == 5
    assert body["moq"] == 10
    assert body["eol_at"] == "2026-12-31"

    # 重新拉一次，确认不是响应里的假象
    again = (await client.get(f"/api/v1/devices/{did}", headers=admin_headers)).json()
    assert again["unit"] == "套"
    assert again["is_common"] is True
    assert again["warranty_years"] == 5


def test_supplier_mapper_has_devices_relationship() -> None:
    """回归：Supplier 必须有 devices 属性。

    Device.supplier 声明了 back_populates="devices"，如果 Supplier 上没有这个
    属性，第一次 configure_mappers() 就会抛
        InvalidRequestError: Mapper[Supplier] has no property 'devices'
    后果不是某个接口坏掉，而是**每一个走 ORM 的请求 500、整个测试套全 error**
    （conftest 建用户时就炸）。这条断言是静态的、不依赖执行顺序。
    """
    from app.models.supplier import Supplier

    assert "devices" in Supplier.__mapper__.relationships, (
        "Supplier.devices 缺失：Device.supplier 的 back_populates 会指向不存在的属性，"
        "SQLAlchemy 在 configure_mappers() 阶段直接报错"
    )
    assert Supplier.__mapper__.relationships["devices"].back_populates == "supplier"

# ---------------------------------------------------------------------------
# 原型第 5 页要的两个筛选 + 甲供统计（2026-10-03）
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stats_reports_owner_supplied(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """原型第 5 页统计条要显示「甲供 N」—— 以前这个数拿不到。"""
    db.add_all([
        Device(code="OS-1", name="甲供摄像机", unit="台", category=DeviceCategory.VIDEO,
               is_owner_supplied=True, status=DeviceStatus.ON_SALE),
        Device(code="OS-2", name="自购摄像机", unit="台", category=DeviceCategory.VIDEO,
               is_owner_supplied=False, status=DeviceStatus.ON_SALE),
    ])
    await db.commit()
    r = await client.get("/api/v1/devices/stats", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.json()["owner_supplied"] == 1, r.json()


@pytest.mark.asyncio
async def test_list_filters_for_owner_supplied_and_replacement(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """「仅看甲供设备」「仅看停产有替代」两个开关。"""
    eol = Device(code="FR-1", name="停产有替代", unit="台", category=DeviceCategory.VIDEO,
                 status=DeviceStatus.EOL, is_owner_supplied=True)
    db.add_all([
        eol,
        Device(code="FR-2", name="在售甲供", unit="台", category=DeviceCategory.VIDEO,
               status=DeviceStatus.ON_SALE, is_owner_supplied=True),
        Device(code="FR-3", name="在售自购", unit="台", category=DeviceCategory.VIDEO,
               status=DeviceStatus.ON_SALE, is_owner_supplied=False),
    ])
    await db.flush()
    eol.replacement_id = eol.id  # 有个"替代型号"即可（自指只为测试字段非空）
    await db.commit()

    r1 = await client.get("/api/v1/devices?is_owner_supplied=true&page_size=50",
                          headers=admin_headers)
    assert r1.status_code == 200, r1.text
    assert {d["code"] for d in r1.json()["items"]} >= {"FR-1", "FR-2"}
    assert "FR-3" not in {d["code"] for d in r1.json()["items"]}

    r2 = await client.get("/api/v1/devices?has_replacement=true&page_size=50",
                          headers=admin_headers)
    assert r2.status_code == 200, r2.text
    assert {d["code"] for d in r2.json()["items"]} == {"FR-1"}

    r3 = await client.get("/api/v1/devices?is_owner_supplied=false&page_size=50",
                          headers=admin_headers)
    assert {d["code"] for d in r3.json()["items"]} == {"FR-3"}

@pytest.mark.asyncio
async def test_device_out_exposes_last_inquiry(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """原型第 2 页有「最近询价」列 —— 模型里早有这两列，但一直没在 DeviceOut 暴露。"""
    from datetime import UTC, datetime

    db.add(Device(
        code="INQ-1", name="有询价的设备", unit="台", category=DeviceCategory.VIDEO,
        status=DeviceStatus.ON_SALE, reference_price=100, cost_price=60,
        install_unit_price=0, last_inquiry_at=datetime(2026, 9, 12, 9, 30, tzinfo=UTC),
        last_inquiry_price=98.5,
    ))
    await db.commit()

    r = await client.get("/api/v1/devices?keyword=INQ-1", headers=admin_headers)
    assert r.status_code == 200, r.text
    row = r.json()["items"][0]
    assert row["last_inquiry_at"] is not None, row
    assert row["last_inquiry_at"].startswith("2026-09-12"), row["last_inquiry_at"]
    assert float(row["last_inquiry_price"]) == 98.5, row

@pytest.mark.asyncio
async def test_device_export_xlsx_roundtrip(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """设备库导出：xlsx 头、行数、以及"导出的文件能直接再喂回导入预览"（列与模板一致）。"""
    db.add(Device(
        code="EX-1", name="导出测试摄像机", brand="海康威视", model="DS-EXPORT-1",
        unit="台", category=DeviceCategory.VIDEO, status=DeviceStatus.ON_SALE,
        reference_price=1234.5, cost_price=800, install_unit_price=100,
        is_owner_supplied=False, moq=1, warranty_years=2,
    ))
    await db.commit()

    r = await client.get("/api/v1/devices/export.xlsx", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK", "不是 xlsx"
    assert "spreadsheetml" in r.headers["content-type"]
    assert "attachment" in r.headers["content-disposition"]
    # 中文文件名走 RFC 5987，同时给 ASCII 兜底
    assert "filename*=UTF-8''" in r.headers["content-disposition"]

    from io import BytesIO

    from openpyxl import load_workbook

    wb = load_workbook(BytesIO(r.content))
    ws = wb.active
    header = [c.value for c in ws[1]]
    assert header[0] == "设备名称" and "规格型号" in header, header
    rows = list(ws.iter_rows(min_row=2, values_only=True))
    assert any(row[0] == "导出测试摄像机" for row in rows), rows[:3]

    # 回灌：导出的文件喂给导入预览，应能被识别（列名与模板一致）
    r2 = await client.post(
        "/api/v1/imports/devices/preview",
        files={"file": ("设备库_导出.xlsx", r.content,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"duplicate_policy": "skip", "max_rows": "500"},
        headers=admin_headers,
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    mapped = {m["system_field"] for m in body["mapping"]}
    assert {"name", "model", "unit", "reference_price"} <= mapped, body["mapping"]

# ---------------------------------------------------------------------------
# 批量调价（2026-10-04，原型第 5 页工具栏）
# ---------------------------------------------------------------------------

async def test_bulk_price_dry_run_then_apply(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """默认只预览不落库；确认后才改；百分比与取整都要对。"""
    d1 = Device(code="BP-1", name="调价一", unit="台", category=DeviceCategory.VIDEO,
                status=DeviceStatus.ON_SALE, reference_price=100, cost_price=60,
                install_unit_price=0)
    d2 = Device(code="BP-2", name="调价二", unit="台", category=DeviceCategory.VIDEO,
                status=DeviceStatus.ON_SALE, reference_price=333, cost_price=60,
                install_unit_price=0)
    db.add_all([d1, d2])
    await db.commit()

    payload = {
        "device_ids": [str(d1.id), str(d2.id)],
        "field": "reference_price",
        "mode": "percent",
        "value": "5",
        "round_yuan": True,
        "dry_run": True,
    }
    r = await client.post("/api/v1/devices/bulk-price", json=payload, headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True and body["count"] == 2
    by_code = {c["code"]: c for c in body["changes"]}
    assert by_code["BP-1"]["after"] == 105.0, by_code["BP-1"]      # 100×1.05
    assert by_code["BP-2"]["after"] == 350.0, by_code["BP-2"]      # 349.65 → 取整到元
    # 预览不落库
    r2 = await client.get("/api/v1/devices?keyword=BP-", headers=admin_headers)
    assert {x["code"]: float(x["reference_price"]) for x in r2.json()["items"]} == {
        "BP-1": 100.0, "BP-2": 333.0,
    }

    r3 = await client.post(
        "/api/v1/devices/bulk-price", json={**payload, "dry_run": False}, headers=admin_headers
    )
    assert r3.status_code == 200, r3.text
    r4 = await client.get("/api/v1/devices?keyword=BP-", headers=admin_headers)
    assert {x["code"]: float(x["reference_price"]) for x in r4.json()["items"]} == {
        "BP-1": 105.0, "BP-2": 350.0,
    }


async def test_bulk_price_amount_mode_clamps_at_zero(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """按金额减价时不允许出现负价：夹到 0 并在返回里标注 clamped。"""
    d = Device(code="BP-3", name="调价三", unit="台", category=DeviceCategory.VIDEO,
               status=DeviceStatus.ON_SALE, reference_price=50, cost_price=10,
               install_unit_price=0)
    db.add(d)
    await db.commit()

    r = await client.post(
        "/api/v1/devices/bulk-price",
        json={"device_ids": [str(d.id)], "field": "reference_price", "mode": "amount",
              "value": "-80", "round_yuan": False, "dry_run": True},
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    row = r.json()["changes"][0]
    assert row["before"] == 50.0 and row["after"] == 0.0 and row["clamped"] is True, row


async def test_bulk_price_needs_update_permission(
    client: AsyncClient, viewer_headers, devices
) -> None:
    """只读角色不能批量改价。"""
    r = await client.post(
        "/api/v1/devices/bulk-price",
        json={"device_ids": [str(devices[0].id)], "value": "5", "dry_run": True},
        headers=viewer_headers,
    )
    assert r.status_code == 403, r.text

# ---------------------------------------------------------------------------
# 报价行 → 设备库（原型「复制到系统」，2026-10-04）
# ---------------------------------------------------------------------------

async def test_promote_quote_items_to_library(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """手动录入的行收进设备库：自动编码、带出价格、同型号不重复建。"""
    p = Project(code="PRJ-PROMO-1", name="收进设备库", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT)
    db.add(p)
    await db.flush()
    q = (await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": str(p.id), "code": "QT-PROMO-1", "title": "收库测试",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0.06", "design_rate": "0.02", "warranty_rate": "0.03",
        "mgmt_profit_rate": "0.08", "tax_rate": "0.09",
    })).json()

    # ① 手工录入的可收库行
    manual = (await client.post(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers, json={
        "name": "自制光纤配线箱 12 芯", "model": "ODF-12-CUSTOM", "brand": "自制",
        "unit": "台", "quantity": 2, "unit_price": 480, "install_unit_price": 60,
    })).json()
    # ② 来自设备库的行（不该被收）
    linked = (await client.post(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers, json={
        "device_id": str(devices[0].id), "name": devices[0].name,
        "unit": devices[0].unit, "quantity": 1, "unit_price": 100,
    })).json()
    # ③ 型号与库里已有设备相同（应跳过并报出命中的编码）
    dup = (await client.post(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers, json={
        "name": "重复型号的自制行", "model": devices[1].model, "brand": devices[1].brand,
        "unit": "台", "quantity": 1, "unit_price": 1,
    })).json()

    r = await client.post("/api/v1/devices/from-quote-items", headers=admin_headers,
                          json={"item_ids": [manual["id"], linked["id"], dup["id"]]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created_count"] == 1, body
    new = body["created"][0]
    assert new["code"], new
    assert new["name"] == "自制光纤配线箱 12 芯"

    reasons = " | ".join(s["reason"] for s in body["skipped"])
    assert "本来就来自设备库" in reasons, reasons
    assert devices[1].code in reasons, reasons

    # 真的进库了，且价格带过来
    hit = (await client.get(f"/api/v1/devices?keyword={new['code']}", headers=admin_headers)).json()
    assert hit["total"] == 1
    row = hit["items"][0]
    assert float(row["reference_price"]) == 480.0
    assert float(row["install_unit_price"]) == 60.0


async def test_promote_requires_device_create_permission(
    client: AsyncClient, viewer_headers, devices
) -> None:
    r = await client.post("/api/v1/devices/from-quote-items", headers=viewer_headers,
                          json={"item_ids": [str(devices[0].id)]})
    assert r.status_code == 403, r.text

