"""设备库留痕（2026-10-09）。

问题现场：报价单 / 项目 / 登录 / 设置都有留痕 ✓，**设备库没有** ✗ ——
改一台设备的进价、把型号标成停产、停用一台设备，事后答不上
"是谁、什么时候改的、改前是什么" ✗（而这些值直接决定报价的成本与毛利 ✗）。

本文件钉住五件事：
  1. 新增 / 改名 / 改价 / 停用 → 每条都有痕，谁、何时、before→after 都在 ✓；
  2. 空改动不写痕 ✓（留痕记的是变更，不是点击次数 ✓）；
  3. 批量调价**也是改价** ✓（dry_run 预览不记 ✗，落库才记 ✓）；
  4. 分类节点增删改同样留痕 ✓（设备归属与编号前缀的来源 ✓）；
  5. 读取入口能查得到 ✓，且留痕**不会被"恢复出厂设置"清掉** ✓。

⚠ 这里**不按插入顺序**断言：`created_at` 是库侧的 CURRENT_TIMESTAMP ✓，
SQLite 上只到秒 ✗ —— 同一秒里插的多条排序不稳 ✓。断言一律按"动作 + 变更字段"
定位到具体那一条 ✓，这样在 SQLite 与 PG 上结论一致 ✓。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import DeviceAuditAction, DeviceAuditEntity, DeviceAuditLog


async def _trail(db: AsyncSession, code: str) -> list[DeviceAuditLog]:
    rows = (
        await db.scalars(
            select(DeviceAuditLog).where(DeviceAuditLog.subject_code == code)
        )
    ).all()
    return list(rows)


def _changes(row: DeviceAuditLog) -> dict[str, tuple[object, object]]:
    return {
        str(c["field"]): (c["from"], c["to"]) for c in (row.changes or {}).get("fields", [])
    }


def _one(rows: list[DeviceAuditLog], *, action: str, field: str) -> DeviceAuditLog:
    """按「动作 + 变更里出现的字段」唯一定位一条痕 —— 不依赖先后顺序 ✓。"""
    hits = [
        r for r in rows
        if str(r.action) == action and field in _changes(r)
    ]
    assert len(hits) == 1, (
        f"按 action={action} field={field} 期望恰好一条，实际 {len(hits)}："
        f"{[(str(r.action), _changes(r)) for r in rows]}"
    )
    return hits[0]


async def test_device_create_rename_repricing_deactivate_all_leave_traces(
    client: AsyncClient, db: AsyncSession, admin_headers, admin_user
) -> None:
    """验收主线：一台设备的 新增 → 改名 → 改价 → 停用，四条痕一条不少 ✓。

    每条都要答得出：**谁**（operator_id + 用户名快照 ✓）、**什么时候**（created_at ✓）、
    **改了什么**（changes 里的 field/from/to ✓）。
    """
    created = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={
            "code": "ZZ-AUDIT-001",
            "name": "留痕测试摄像机",
            "brand": "测试品牌",
            "model": "ZZ-AUDIT-MODEL",
            "unit": "台",
            "category": "video",
            "reference_price": "680.00",
            "cost_price": "510.00",
        },
    )
    assert created.status_code == 201, created.text
    dev_id = created.json()["id"]

    # ① 改名
    r = await client.patch(
        f"/api/v1/devices/{dev_id}", headers=admin_headers, json={"name": "留痕测试摄像机（改名）"}
    )
    assert r.status_code == 200, r.text
    # ② 改价（参考价 + 进价一起改 → 仍是一条痕、两个字段 ✓）
    r = await client.patch(
        f"/api/v1/devices/{dev_id}",
        headers=admin_headers,
        json={"reference_price": "720.50", "cost_price": "540.00"},
    )
    assert r.status_code == 200, r.text
    # ③ 停用（DELETE = 软删 ✓）
    r = await client.delete(f"/api/v1/devices/{dev_id}", headers=admin_headers)
    assert r.status_code == 204, r.text

    rows = await _trail(db, "ZZ-AUDIT-001")
    assert len(rows) == 4, [(str(x.action), x.summary) for x in rows]

    create = _one(rows, action="create", field="name")
    rename = _one(rows, action="update", field="name")
    repricing = _one(rows, action="update", field="reference_price")
    deactivate = _one(rows, action="delete", field="is_active")

    # 谁 / 什么时候 / 什么对象：每一条都要有 ✓
    for row in rows:
        assert row.operator_id == admin_user.id, (str(row.action), row.operator_id)
        assert row.operator_username == "admin", (str(row.action), row.operator_username)
        assert row.created_at is not None, str(row.action)
        assert row.entity == DeviceAuditEntity.DEVICE, row.entity
        assert row.entity_id == UUID(dev_id), row.entity_id
        assert row.ip or row.ip is None  # 带 IP 列（测试里可能没中间件 → 允许 None ✓）
        assert row.summary, str(row.action)

    # 新增：记的是**初始值** ✓（不是一堆 None→None ✗）
    init = _changes(create)
    assert init["name"] == (None, "留痕测试摄像机"), init
    assert init["reference_price"] == (None, "680.00"), init
    assert init["status"] == (None, "on_sale"), init

    # 改名：只记 name 一个字段 ✓（别的字段没变就不该出现 ✗）
    assert _changes(rename) == {"name": ("留痕测试摄像机", "留痕测试摄像机（改名）")}, _changes(rename)

    # 改价：金额保精度（Decimal → 字符串 ✓，不是 float 的 720.4999…）
    price_changes = _changes(repricing)
    assert price_changes["reference_price"] == ("680.00", "720.50"), price_changes
    assert price_changes["cost_price"] == ("510.00", "540.00"), price_changes

    # 停用：action=delete ✓（接口就是 DELETE ✓ / 软删 ✓），is_active true→false ✓
    assert _changes(deactivate) == {"is_active": (True, False)}, _changes(deactivate)

    # 留痕说改了什么，库里就得是什么 ✓
    detail = await client.get(f"/api/v1/devices/{dev_id}", headers=admin_headers)
    assert detail.json()["is_active"] is False, detail.json()

    # ④ 重新启用：走 PATCH is_active=true → **同样留痕** ✓（与上面的停用配对 ✓）
    r = await client.patch(
        f"/api/v1/devices/{dev_id}", headers=admin_headers, json={"is_active": True}
    )
    assert r.status_code == 200, r.text
    rows = await _trail(db, "ZZ-AUDIT-001")
    assert len(rows) == 5, [(str(x.action), x.summary) for x in rows]
    reactivate = _one(rows, action="update", field="is_active")
    assert _changes(reactivate) == {"is_active": (False, True)}, _changes(reactivate)


async def test_device_update_without_real_change_writes_no_trace(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """空改动不写痕 ✓（界面上"保存"很轻，连点两次不该长出两条一样的记录 ✗）。

    ⚠ 这是**有意的**取舍：留痕记的是"变更"，不是"点击次数" ✓。
    """
    created = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={"code": "ZZ-AUDIT-002", "name": "空改动设备", "category": "video"},
    )
    assert created.status_code == 201, created.text
    dev_id = created.json()["id"]

    # 把 name 改成**同样的值**：pydantic 收到字段了，但值没变 → 不该记 ✓
    r = await client.patch(
        f"/api/v1/devices/{dev_id}", headers=admin_headers, json={"name": "空改动设备"}
    )
    assert r.status_code == 200, r.text

    rows = await _trail(db, "ZZ-AUDIT-002")
    assert [str(x.action) for x in rows] == ["create"], [
        (str(x.action), x.summary) for x in rows
    ]


async def test_bulk_price_audits_only_when_applied(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """批量调价：**预览不记** ✗（没落库的动作不是变更 ✓），落库才记 ✓，且逐台记 before→after ✓。

    为什么必须覆盖它：不记的话"把 500 台设备整体上调 5%"就是留痕里最大的黑洞 ✗
    —— 比逐台改价还危险 ✗。
    """
    created = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={"code": "ZZ-AUDIT-003", "name": "批量调价设备", "category": "video",
              "reference_price": "1000.00"},
    )
    assert created.status_code == 201, created.text
    dev_id = created.json()["id"]

    preview = await client.post(
        "/api/v1/devices/bulk-price",
        headers=admin_headers,
        json={"device_ids": [dev_id], "field": "reference_price", "mode": "percent",
              "value": "5", "dry_run": True},
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["dry_run"] is True
    rows = await _trail(db, "ZZ-AUDIT-003")
    assert [str(x.action) for x in rows] == ["create"], "dry_run 预览不该写留痕"

    applied = await client.post(
        "/api/v1/devices/bulk-price",
        headers=admin_headers,
        json={"device_ids": [dev_id], "field": "reference_price", "mode": "percent",
              "value": "5", "round_yuan": True, "dry_run": False},
    )
    assert applied.status_code == 200, applied.text

    rows = await _trail(db, "ZZ-AUDIT-003")
    assert len(rows) == 2, [(str(x.action), x.summary) for x in rows]
    applied_row = _one(rows, action="update", field="reference_price")
    # 1000 × 1.05 = 1050（round_yuan=True → 取整到元 ✓）
    assert _changes(applied_row) == {"reference_price": ("1000.00", "1050")}, _changes(applied_row)
    assert "批量调价" in (applied_row.summary or ""), applied_row.summary
    assert applied_row.operator_username == "admin", applied_row.operator_username


async def test_category_create_update_delete_leave_traces(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """分类节点增/改/删各留一条 ✓（分类树是设备归属与编号前缀的来源 ✓，同样要可追溯 ✓）。"""
    created = await client.post(
        "/api/v1/devices/categories",
        headers=admin_headers,
        json={"code": "zz.audit.root", "name": "留痕测试一级分类", "legacy_enum": "video",
              "code_prefix": "V"},
    )
    assert created.status_code == 201, created.text
    node = created.json()
    node_id = UUID(node["id"])

    r = await client.patch(
        f"/api/v1/devices/categories/{node['id']}",
        headers=admin_headers,
        json={"name": "留痕测试一级分类（改名）", "notes": "留痕用"},
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"/api/v1/devices/categories/{node['id']}", headers=admin_headers)
    assert r.status_code == 204, r.text

    rows = await _trail(db, "zz.audit.root")
    assert sorted(str(x.action) for x in rows) == [
        "category_create", "category_delete", "category_update",
    ], [(str(x.action), x.summary) for x in rows]

    created_row = _one(rows, action="category_create", field="code")
    updated_row = _one(rows, action="category_update", field="notes")
    deleted_row = _one(rows, action="category_delete", field="deleted")

    for row in rows:
        assert row.entity == DeviceAuditEntity.CATEGORY, row.entity
        assert row.entity_id == node_id, row.entity_id
        assert row.subject_name, row.summary
        assert row.operator_username == "admin", row.operator_username

    assert _changes(updated_row) == {
        "name": ("留痕测试一级分类", "留痕测试一级分类（改名）"),
        "notes": (None, "留痕用"),
    }, _changes(updated_row)
    # 新增那条记的是初始值 ✓（legacy_enum 是"vide"还是"video"？这里只钉住关键两项）
    init = _changes(created_row)
    assert init["name"] == (None, "留痕测试一级分类"), init
    assert init["legacy_enum"] == (None, "video"), init

    # 分类被**硬删**了 ✓ —— 留痕照样在 ✓（entity_id 刻意没有外键 ✓）
    still = (
        await db.scalars(select(DeviceAuditLog).where(DeviceAuditLog.entity_id == node_id))
    ).all()
    assert len(list(still)) == 3, "分类删掉之后留痕必须还在（否则这条痕没有意义）"
    assert deleted_row.summary and "删除设备分类" in deleted_row.summary, deleted_row.summary


async def test_global_audit_endpoint_includes_device_traces(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """读取入口：`GET /audit-logs/recent` 要能把设备库留痕带出来 ✓（kind=device ✓），
    并且能按设备编号 / 动作筛 ✓ —— 否则有留痕也查不到，等于没留 ✓。"""
    created = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={"code": "ZZ-AUDIT-004", "name": "留痕查询设备", "category": "video"},
    )
    assert created.status_code == 201, created.text
    dev_id = created.json()["id"]
    assert (
        await client.delete(f"/api/v1/devices/{dev_id}", headers=admin_headers)
    ).status_code == 204

    r = await client.get(
        "/api/v1/audit-logs/recent",
        headers=admin_headers,
        params={"subject_code": "ZZ-AUDIT-004"},
    )
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert [i["kind"] for i in items] == ["device", "device"], items
    assert {i["action"] for i in items} == {"create", "delete"}, items
    delete_row = [i for i in items if i["action"] == "delete"][0]
    assert delete_row["subject_name"] == "留痕查询设备", delete_row
    assert delete_row["username"] == "admin", delete_row
    assert delete_row["payload"]["fields"][0]["field"] == "is_active", delete_row
    # 设备库留痕刻意没有外键 → "对象是否已删"**未知** ✓（不许猜 ✓）
    assert delete_row["subject_deleted"] is None, delete_row

    # 按动作筛：`action=delete` 跨表统一 ✓（设备停用和删单同一个取值 ✓）
    r2 = await client.get(
        "/api/v1/audit-logs/recent",
        headers=admin_headers,
        params={"action": "delete", "subject_code": "ZZ-AUDIT-004"},
    )
    assert [i["action"] for i in r2.json()["items"]] == ["delete"], r2.json()

    # 关掉设备库留痕 → 老调用方（只要报价单/项目）行为不变 ✓
    r3 = await client.get(
        "/api/v1/audit-logs/recent",
        headers=admin_headers,
        params={"subject_code": "ZZ-AUDIT-004", "include_device": False},
    )
    assert r3.json()["items"] == [], r3.json()


def test_device_audit_table_is_not_in_factory_reset_scope() -> None:
    """留痕必须**活过**「恢复出厂设置」✓（那是它唯一的用处 ✓）。

    做法与 `factory_reset_logs` 一致 ✓：本表**不在**任何清理清单里 ✓，
    而 `scope=all` 只会清那三张清单里的表 ✓（清库之后要能查到"清之前谁改过设备" ✓）。
    `entity_id` / `operator_id` 也因此**不加外键** ✓ —— 加 FK 就等于给
    "级联带走留痕"开后门 ✗（见 models/audit.py 的 DeviceAuditLog docstring ✓）。
    """
    from app.db.base import Base
    from app.services.factory_reset import (
        ACCOUNT_TABLES,
        BUSINESS_TABLES,
        SETTINGS_TABLES,
    )

    # 先证明这张表**真实存在**（否则下面的断言是空转 ✓）
    assert "device_audit_logs" in set(Base.metadata.tables), "device_audit_logs 没注册进 metadata？"

    cleaned = {name for name, _label in (*BUSINESS_TABLES, *SETTINGS_TABLES, *ACCOUNT_TABLES)}
    assert "device_audit_logs" not in cleaned, (
        "设备库留痕混进了 factory_reset 的清理清单 —— 清库会把事故证据一起删掉"
    )
    # 顺带钉住"设备库本身会被清" ✓：正因为会被清，留痕才必须独立存活 ✓
    assert "devices" in cleaned and "device_categories" in cleaned, sorted(cleaned)

    # 两张 id 列都不带外键 ✓（SQLAlchemy 层面也确认一次，防止以后有人"顺手"加上）
    cols = Base.metadata.tables["device_audit_logs"].columns
    assert not cols["entity_id"].foreign_keys, "entity_id 不能有外键"
    assert not cols["operator_id"].foreign_keys, "operator_id 不能有外键"


def test_audit_action_values_are_plain_strings() -> None:
    """动作取值与既有留痕**逐字对齐** ✓（`create`/`update`/`delete` ✓），
    这样 `?action=delete` 能一次捞出"删单"和"停用设备" ✓（不必记两套词表 ✓）。"""
    assert DeviceAuditAction.CREATE.value == "create"
    assert DeviceAuditAction.UPDATE.value == "update"
    assert DeviceAuditAction.DELETE.value == "delete"
    # 分类那三个没有对应动作 → 新造名字 ✓（不硬塞进 create/update/delete ✗）
    assert DeviceAuditAction.CATEGORY_CREATE.value == "category_create"
    assert DeviceAuditAction.CATEGORY_UPDATE.value == "category_update"
    assert DeviceAuditAction.CATEGORY_DELETE.value == "category_delete"

    from app.models.audit import AuditAction

    assert DeviceAuditAction.CREATE.value == AuditAction.CREATE.value
    assert DeviceAuditAction.UPDATE.value == AuditAction.UPDATE.value
    assert DeviceAuditAction.DELETE.value == AuditAction.DELETE.value


def test_price_changes_keep_decimal_precision() -> None:
    """留痕里的钱必须是**字符串**而不是 float ✓ —— 680.10 变 680.099999… 这条痕就废了 ✗。"""
    from app.services.audit import field_changes, plain

    assert plain(Decimal("680.10")) == "680.10"
    assert plain(Decimal("0.1")) == "0.1"
    changes = field_changes(
        {"reference_price": Decimal("680.10")}, {"reference_price": Decimal("595.00")}
    )
    assert changes == [{"field": "reference_price", "from": "680.10", "to": "595.00"}], changes
    # 没变就不出现 ✓
    assert field_changes({"name": "A"}, {"name": "A"}) == []
