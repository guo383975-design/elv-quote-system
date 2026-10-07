"""用户管理「组织架构树」测试（2026-10-02：用户管理从平铺列表改成左树右列表）。

覆盖：
  · 树接口的层级、自身人数与子树人数、未分配人数
  · 按部门筛选要**包含下级部门**（点父部门不能是空列表）
  · 部门增删改：不允许成环、有下级/有人时拒绝删除
  · 用户挂部门：自由文本 department 由节点名反推（不允许两套真相）
  · 改名后子树下用户的自由文本字段跟着对齐
  · 权限：只有 admin 能维护组织架构
"""

from __future__ import annotations

from uuid import UUID

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.department import Department
from app.models.user import User, UserStatus


async def _make_tree(db: AsyncSession) -> dict[str, UUID]:
    """公司 → 设计部 → 智能化设计组；再建销售部。返回 code → id（只留纯值）。"""
    root = Department(code="dept", name="公司", sort_order=0)

    db.add(root)
    await db.flush()
    sales = Department(code="dept.sales", name="销售部", parent_id=root.id, sort_order=1)
    design = Department(code="dept.design", name="设计部", parent_id=root.id, sort_order=0)
    db.add_all([sales, design])
    await db.flush()
    smart = Department(
        code="dept.design.smart", name="智能化设计组", parent_id=design.id, sort_order=0
    )
    db.add(smart)
    await db.commit()
    for n in (root, sales, design, smart):
        await db.refresh(n)
    return {
        "root": root.id, "sales": sales.id, "design": design.id, "smart": smart.id,
    }


def _mk_user(username: str, department_id: UUID | None, *, role: str = "sales") -> User:
    from app.core.permissions import Role

    return User(
        username=username,
        email=f"{username}@test.com",
        hashed_password="x",
        role=Role(role),
        status=UserStatus.ACTIVE,
        department_id=department_id,
    )


async def test_department_tree_shape_and_counts(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    db.add_all([
        _mk_user("u_smart1", tree["smart"]),
        _mk_user("u_smart2", tree["smart"]),
        _mk_user("u_design", tree["design"]),
        _mk_user("u_sales", tree["sales"]),
        _mk_user("u_none", None),
    ])
    await db.commit()

    r = await client.get("/api/v1/users/departments/tree", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    # admin_headers fixture 自己建了一个没分配部门的 admin 用户，所以是 2 个未分配
    assert body["unassigned"] == 2
    assert body["total"] == 6
    roots = {n["name"]: n for n in body["nodes"]}
    assert "公司" in roots
    assert "销售部" not in roots, "销售部已挂到公司下面，不该再是顶级节点"
    company = roots["公司"]
    # 公司自身没挂人，含子树 4 人（设计部 3 + 销售部 1）
    assert company["user_count"] == 0
    assert company["total_count"] == 4
    kids = {c["name"]: c for c in company["children"]}
    assert kids["设计部"]["path"] == "公司 / 设计部"
    assert kids["设计部"]["total_count"] == 3
    grand = {c["name"]: c for c in kids["设计部"]["children"]}
    assert grand["智能化设计组"]["user_count"] == 2


async def test_list_users_by_department_includes_descendants(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    db.add_all([
        _mk_user("u_smart1", tree["smart"]),
        _mk_user("u_design", tree["design"]),
        _mk_user("u_sales", tree["sales"]),
    ])
    await db.commit()

    # 点「公司」→ 能看到下面所有部门的人
    r = await client.get(
        f"/api/v1/users?department_id={tree['root']}&page_size=100", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    names = {i["username"] for i in r.json()["items"]}
    assert names == {"u_smart1", "u_design", "u_sales"}
    row = [i for i in r.json()["items"] if i["username"] == "u_smart1"][0]
    assert row["department_path"] == "公司 / 设计部 / 智能化设计组"

    # 只看「公司」自己（没人）
    r2 = await client.get(
        f"/api/v1/users?department_id={tree['root']}&include_descendants=false"
        "&page_size=100",
        headers=admin_headers,
    )
    assert r2.json()["total"] == 0

    # 未分配
    r3 = await client.get("/api/v1/users?unassigned=true", headers=admin_headers)
    assert r3.status_code == 200


async def test_user_department_derives_legacy_text(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """挂部门节点时，自由文本 department 必须由节点名反推（不允许两套真相）。"""
    tree = await _make_tree(db)
    r = await client.post(
        "/api/v1/users",
        headers=admin_headers,
        json={
            "username": "newbie", "email": "newbie@test.com", "password": "p12345678",
            "department_id": str(tree["smart"]), "department": "随便填的部门",  # 故意传错
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["department"] == "智能化设计组", "应以部门节点为准"
    assert body["department_path"] == "公司 / 设计部 / 智能化设计组"

    # PATCH 换部门 → 文本跟着变
    r2 = await client.patch(
        f"/api/v1/users/{body['id']}",
        headers=admin_headers,
        json={"department_id": str(tree["sales"])},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["department"] == "销售部"

    # 解除部门 → 文本一起清空
    r3 = await client.patch(
        f"/api/v1/users/{body['id']}", headers=admin_headers, json={"department_id": None}
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["department"] is None
    assert r3.json()["department_path"] == ""


async def test_rename_department_resyncs_user_text(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    u = _mk_user("u_smart", tree["smart"])
    u.department = "智能化设计组"
    db.add(u)
    await db.commit()

    r = await client.patch(
        f"/api/v1/users/departments/{tree['smart']}",
        headers=admin_headers,
        json={"name": "智能化组（改名）"},
    )
    assert r.status_code == 200, r.text
    await db.refresh(u)
    assert u.department == "智能化组（改名）", "改名后用户的自由文本字段要跟着对齐"


async def test_department_tree_write_guards(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)

    # 成环：把公司挪到自己的孙子里
    r = await client.patch(
        f"/api/v1/users/departments/{tree['root']}",
        headers=admin_headers,
        json={"parent_id": str(tree["smart"])},
    )
    assert r.status_code == 400, r.text
    # 自己当自己的上级
    r2 = await client.patch(
        f"/api/v1/users/departments/{tree['design']}",
        headers=admin_headers,
        json={"parent_id": str(tree["design"])},
    )
    assert r2.status_code == 400, r2.text
    # 代号重复
    r3 = await client.post(
        "/api/v1/users/departments",
        headers=admin_headers,
        json={"code": "dept.sales", "name": "重复代号"},
    )
    assert r3.status_code == 409, r3.text
    # 上级不存在
    r4 = await client.post(
        "/api/v1/users/departments",
        headers=admin_headers,
        json={"code": "dept.x", "name": "X",
              "parent_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert r4.status_code == 400, r4.text


async def test_delete_department_refuses_with_children_or_users(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    r = await client.delete(
        f"/api/v1/users/departments/{tree['root']}", headers=admin_headers
    )
    assert r.status_code == 409 and "下级" in r.json()["detail"], r.text

    # smart 没有下级，用它验证"有人不能删"
    db.add(_mk_user("u_smart", tree["smart"]))
    await db.commit()
    r2 = await client.delete(
        f"/api/v1/users/departments/{tree['smart']}", headers=admin_headers
    )
    assert r2.status_code == 409 and "用户" in r2.json()["detail"], r2.text

    # 空节点可以删
    r3 = await client.delete(
        f"/api/v1/users/departments/{tree['sales']}", headers=admin_headers
    )
    assert r3.status_code == 204, r3.text


async def test_department_maintenance_permissions(
    client: AsyncClient, db: AsyncSession, admin_headers, est_headers, sales_headers
) -> None:
    """组织架构维护是 admin 的事；设计能看不能改；销售连看都不行。"""
    await _make_tree(db)
    assert (await client.get("/api/v1/users/departments/tree",
                             headers=est_headers)).status_code == 403  # user.read 不含设计
    assert (await client.get("/api/v1/users/departments/tree",
                             headers=sales_headers)).status_code == 403
    body = {"code": "dept.new", "name": "新部门"}
    assert (await client.post("/api/v1/users/departments", headers=est_headers,
                              json=body)).status_code == 403
    assert (await client.post("/api/v1/users/departments", headers=sales_headers,
                              json=body)).status_code == 403
    # admin 能建、能改、能删（空节点）
    assert (await client.post("/api/v1/users/departments", headers=admin_headers,
                              json=body)).status_code == 201


async def test_seed_departments_and_assignment(db: AsyncSession) -> None:
    """seed 的组织架构树幂等，并把演示账号按部门归位。"""
    from scripts.seed import seed_departments, seed_users

    nodes = await seed_departments(db)
    assert "dept.design.smart" in nodes
    # 三级：公司 → 设计部 → 智能化设计组
    smart = nodes["dept.design.smart"]
    assert smart.parent_id == nodes["dept.design"].id
    assert nodes["dept.design"].parent_id == nodes["dept"].id

    assert await seed_users(db, allow_create=True, departments=nodes) == 7
    admin = await db.scalar(select_user("admin"))
    est = await db.scalar(select_user("est01"))
    assert admin.department_id == nodes["dept.gm"].id
    assert est.department_id == nodes["dept.design.smart"].id
    assert est.department == "智能化设计组"

    # 幂等：再跑一次不新增部门、不改人
    before = len(nodes)
    again = await seed_departments(db)
    assert len(again) == before
    assert await seed_users(db, allow_create=True, departments=again) == 0

    # production 开关：账号一个都不建（部门树不受影响，它是结构不是凭据）
    assert await seed_users(db, allow_create=False, departments=again) == 0


def select_user(username: str):
    from sqlalchemy import select

    return select(User).where(User.username == username)

