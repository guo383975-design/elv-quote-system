"""供应商 API 测试：CRUD + 角色权限 + 软删除 + keyword 筛选."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

# ---------------------------------------------------------------------------
# 基础 CRUD：admin 全套
# ---------------------------------------------------------------------------

async def test_admin_create_supplier(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["code"] == "SUP-001"
    assert body["name"] == "宁波XX安防科技有限公司"
    assert body["is_active"] is True


async def test_admin_list_suppliers_pagination(client: AsyncClient, admin_headers) -> None:
    # 先建两条
    for code, name in [
        ("SUP-001", "宁波XX安防科技有限公司"),
        ("SUP-002", "杭州XX智能设备有限公司"),
    ]:
        r = await client.post(
            "/api/v1/suppliers",
            headers=admin_headers,
            json={"code": code, "name": name},
        )
        assert r.status_code == 201, r.text

    r = await client.get("/api/v1/suppliers", headers=admin_headers)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 2
    assert {item["code"] for item in body["items"]} == {"SUP-001", "SUP-002"}


async def test_admin_get_supplier_detail(client: AsyncClient, admin_headers) -> None:
    r = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={
            "code": "SUP-001",
            "name": "宁波XX安防科技有限公司",
            "contact_name": "张三",
            "contact_phone": "0574-88880000",
            "contact_email": "sales@example.com",
            "address": "浙江省宁波市鄞州区XX路 88 号",
        },
    )
    sid = r.json()["id"]

    detail = await client.get(f"/api/v1/suppliers/{sid}", headers=admin_headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["contact_name"] == "张三"
    assert body["contact_email"] == "sales@example.com"


async def test_admin_update_supplier(client: AsyncClient, admin_headers) -> None:
    create = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    sid = create.json()["id"]

    r = await client.patch(
        f"/api/v1/suppliers/{sid}",
        headers=admin_headers,
        json={"contact_name": "李四", "contact_phone": "0574-9999"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["contact_name"] == "李四"
    assert r.json()["contact_phone"] == "0574-9999"


# ---------------------------------------------------------------------------
# 角色权限
# ---------------------------------------------------------------------------

async def test_sales_can_list_and_get_but_cannot_create_update_or_delete(
    client: AsyncClient, admin_headers, sales_headers
) -> None:
    """sales 在 supplier 矩阵里只有 READ —— 不能增删改。

    这是 2026-09-29 用户拍板的口径：「销售只查可用供应商；新增/修订由
    admin/estimator 维护」。矩阵在 `permissions.py` 已经调整过；
    本测试钉死矩阵 + 路由器层都正确返回 403。
    """
    # admin 先建一个，sales 读得到
    create = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    sid = create.json()["id"]

    listing = await client.get("/api/v1/suppliers", headers=sales_headers)
    assert listing.status_code == 200
    assert listing.json()["total"] == 1

    detail = await client.get(f"/api/v1/suppliers/{sid}", headers=sales_headers)
    assert detail.status_code == 200

    # sales 不能 create
    denied_create = await client.post(
        "/api/v1/suppliers",
        headers=sales_headers,
        json={"code": "SUP-099", "name": "销售想新增的供应商"},
    )
    assert denied_create.status_code == 403, denied_create.text

    # sales 不能 update
    denied_update = await client.patch(
        f"/api/v1/suppliers/{sid}",
        headers=sales_headers,
        json={"contact_name": "销售改的"},
    )
    assert denied_update.status_code == 403, denied_update.text

    # sales 不能 delete（这条之前就过，无需改）
    denied_delete = await client.delete(
        f"/api/v1/suppliers/{sid}", headers=sales_headers
    )
    assert denied_delete.status_code == 403, denied_delete.text


async def test_viewer_cannot_access_suppliers(
    client: AsyncClient, admin_headers, viewer_headers
) -> None:
    create = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    sid = create.json()["id"]

    # viewer 在 supplier.read 里有，所以列表 / 详情可以
    listing = await client.get("/api/v1/suppliers", headers=viewer_headers)
    assert listing.status_code == 200

    detail = await client.get(f"/api/v1/suppliers/{sid}", headers=viewer_headers)
    assert detail.status_code == 200

    # create / update / delete 全部 403
    for r in (
        await client.post(
            "/api/v1/suppliers",
            headers=viewer_headers,
            json={"code": "SUP-099", "name": "viewer 想新增"},
        ),
        await client.patch(
            f"/api/v1/suppliers/{sid}",
            headers=viewer_headers,
            json={"contact_name": "viewer 想改"},
        ),
        await client.delete(f"/api/v1/suppliers/{sid}", headers=viewer_headers),
    ):
        assert r.status_code == 403, r.text


# ---------------------------------------------------------------------------
# 唯一性冲突
# ---------------------------------------------------------------------------

async def test_duplicate_code_returns_409(client: AsyncClient, admin_headers) -> None:
    first = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    assert first.status_code == 201

    dup_code = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "另一家供应商"},
    )
    assert dup_code.status_code == 409, dup_code.text
    assert "编号" in dup_code.json()["detail"]


async def test_duplicate_name_returns_409(client: AsyncClient, admin_headers) -> None:
    first = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    assert first.status_code == 201

    dup_name = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-002", "name": "宁波XX安防科技有限公司"},
    )
    assert dup_name.status_code == 409, dup_name.text
    assert "名称" in dup_name.json()["detail"]


# ---------------------------------------------------------------------------
# 软删除
# ---------------------------------------------------------------------------

async def test_soft_delete_filters_from_listing_and_marks_inactive(
    client: AsyncClient, admin_headers
) -> None:
    """软删除与 devices 行为对齐：list 默认按 is_active=True 过滤，被停用的不再出现；
    但 GET /{id} 仍然 200（保留历史记录，is_active=False）。前端通过 is_active 渲染。

    之所以不复用「软删除后 GET → 404」是因为 devices 也是这样设计的，硬改成 404
    会让两个资源的语义不一致，而且历史报价单里的 supplier_id 反查就会断。
    """
    create = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    sid = create.json()["id"]

    # 默认列表 is_active=True：删除前能看到
    listing_before = await client.get("/api/v1/suppliers", headers=admin_headers)
    assert listing_before.json()["total"] == 1

    delete = await client.delete(
        f"/api/v1/suppliers/{sid}", headers=admin_headers
    )
    assert delete.status_code == 204

    # 软删除后默认列表（is_active=True）应该为 0 —— 这就是用户说的
    # 「软删除后再 GET → 看不到」（list 端点按 is_active 过滤）
    listing_after = await client.get("/api/v1/suppliers", headers=admin_headers)
    assert listing_after.json()["total"] == 0

    # 显式传 is_active=False 能看到
    listing_inactive = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"is_active": "false"}
    )
    assert listing_inactive.json()["total"] == 1
    assert listing_inactive.json()["items"][0]["is_active"] is False

    # 详情仍然可读（与 devices 一致），is_active=False 暴露给前端判断渲染
    detail = await client.get(f"/api/v1/suppliers/{sid}", headers=admin_headers)
    assert detail.status_code == 200
    assert detail.json()["is_active"] is False


async def test_get_missing_supplier_returns_404(client: AsyncClient, admin_headers) -> None:
    """真不存在的 UUID → 404，与 devices 行为一致。"""
    fake = "00000000-0000-0000-0000-000000000000"
    r = await client.get(f"/api/v1/suppliers/{fake}", headers=admin_headers)
    assert r.status_code == 404


async def test_update_nonexistent_supplier_returns_404(
    client: AsyncClient, admin_headers
) -> None:
    fake = "00000000-0000-0000-0000-000000000000"
    r = await client.patch(
        f"/api/v1/suppliers/{fake}",
        headers=admin_headers,
        json={"contact_name": "x"},
    )
    assert r.status_code == 404


async def test_delete_nonexistent_supplier_returns_404(
    client: AsyncClient, admin_headers
) -> None:
    fake = "00000000-0000-0000-0000-000000000000"
    r = await client.delete(f"/api/v1/suppliers/{fake}", headers=admin_headers)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 关键字筛选
# ---------------------------------------------------------------------------

async def test_empty_keyword_lists_all(client: AsyncClient, admin_headers) -> None:
    for code, name in [
        ("SUP-001", "宁波XX安防科技有限公司"),
        ("SUP-002", "杭州XX智能设备有限公司"),
        ("SUP-003", "浙江XX网络技术有限公司"),
    ]:
        await client.post(
            "/api/v1/suppliers",
            headers=admin_headers,
            json={"code": code, "name": name},
        )

    # 空字符串 = 无 keyword
    r = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": ""}
    )
    assert r.status_code == 200
    assert r.json()["total"] == 3

    # 不传 keyword
    r2 = await client.get("/api/v1/suppliers", headers=admin_headers)
    assert r2.json()["total"] == 3


async def test_keyword_matches_name_partial(
    client: AsyncClient, admin_headers
) -> None:
    await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-001", "name": "宁波XX安防科技有限公司"},
    )
    await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-002", "name": "杭州XX智能设备有限公司"},
    )
    await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={"code": "SUP-003", "name": "浙江XX网络技术有限公司"},
    )

    # "宁波" 部分匹配 name
    r = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": "宁波"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["code"] == "SUP-001"

    # "智能" 部分匹配 SUP-002
    r2 = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": "智能"}
    )
    assert r2.json()["total"] == 1
    assert r2.json()["items"][0]["code"] == "SUP-002"

    # "技术" 只出现在 SUP-003 的 name 里（"科技" ≠ "技术"）
    r3 = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": "技术"}
    )
    assert r3.json()["total"] == 1
    assert r3.json()["items"][0]["code"] == "SUP-003"

    # "有限公司" 是三个供应商 name 的共同后缀 → 命中 3
    r4 = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": "有限公司"}
    )
    assert r4.json()["total"] == 3

    # "网络" 部分匹配 SUP-003
    r5 = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": "网络"}
    )
    assert r5.json()["total"] == 1
    assert r5.json()["items"][0]["code"] == "SUP-003"

    # code 也能搜（SUP-002 这种精确前缀也能匹配）
    r6 = await client.get(
        "/api/v1/suppliers", headers=admin_headers, params={"keyword": "SUP-002"}
    )
    assert r6.json()["total"] == 1
    assert r6.json()["items"][0]["code"] == "SUP-002"


# ---------------------------------------------------------------------------
# 回归：Supplier 模型在迁移后的字段都暴露在响应里
# ---------------------------------------------------------------------------

async def test_supplier_out_exposes_all_fields(
    client: AsyncClient, admin_headers
) -> None:
    """前端表单要回显的字段都得在 SupplierOut 里。

    之前只有 name / contact_person / contact_phone，新增的 contact_name /
    contact_email 不暴露会导致前端编辑表单字段一片空白。
    """
    create = await client.post(
        "/api/v1/suppliers",
        headers=admin_headers,
        json={
            "code": "SUP-001",
            "name": "宁波XX安防科技有限公司",
            "contact_name": "张三",
            "contact_phone": "0574-88880000",
            "contact_email": "zhang@example.com",
            "address": "浙江省宁波市鄞州区XX路 88 号",
        },
    )
    sid = create.json()["id"]

    detail = await client.get(
        f"/api/v1/suppliers/{sid}", headers=admin_headers
    )
    body = detail.json()
    for field in (
        "id", "code", "name",
        "contact_name", "contact_phone", "contact_email", "address",
        "is_active", "created_at", "updated_at",
    ):
        assert field in body, f"SupplierOut 缺少 {field}"

    assert body["contact_email"] == "zhang@example.com"
    assert body["contact_name"] == "张三"

async def test_supplier_include_inactive_filter(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """在用/停用的过滤语义（2026-10-04 修）。

    原先只有一个 `is_active` 参数，语义是"只看启用中 / 只看停用"，
    前端却把它当成"是否含停用"用 —— 结果勾了「显示已停用」什么都没变。
    现在 `include_inactive=true` 表示两者都要，`is_active=false` 仍表示只看停用。
    """
    from app.models.supplier import Supplier

    db.add_all([
        Supplier(code="SUP-T-ACT", name="在用供应商", is_active=True),
        Supplier(code="SUP-T-OFF", name="停用供应商", is_active=False),
    ])
    await db.commit()

    r1 = await client.get("/api/v1/suppliers?keyword=SUP-T-", headers=admin_headers)
    assert r1.status_code == 200, r1.text
    assert {x["code"] for x in r1.json()["items"]} == {"SUP-T-ACT"}, r1.json()

    r2 = await client.get("/api/v1/suppliers?keyword=SUP-T-&is_active=false", headers=admin_headers)
    assert {x["code"] for x in r2.json()["items"]} == {"SUP-T-OFF"}, r2.json()

    r3 = await client.get(
        "/api/v1/suppliers?keyword=SUP-T-&include_inactive=true", headers=admin_headers
    )
    assert {x["code"] for x in r3.json()["items"]} == {"SUP-T-ACT", "SUP-T-OFF"}, r3.json()

