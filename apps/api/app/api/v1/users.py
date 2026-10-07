"""Users router: 用户管理（仅管理员）."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.core.permissions import Role
from app.core.security import hash_password_async
from app.db.session import get_db
from app.models.department import Department
from app.models.user import User, UserStatus
from app.schemas import (
    DepartmentIn,
    DepartmentOut,
    DepartmentTreeOut,
    DepartmentUpdate,
    Page,
    UserCreate,
    UserOut,
    UserUpdate,
)

router = APIRouter(prefix="/users", tags=["用户管理"])
log = get_logger("api.users")


# ---------------------------------------------------------------------------
# 组织架构树（2026-10-02：用户管理从平铺列表改成"左树右列表"）
# ---------------------------------------------------------------------------

async def _load_dept_nodes(db: AsyncSession) -> dict[UUID, Department]:
    rows = await db.scalars(select(Department))
    return {d.id: d for d in rows.all()}


def _dept_path(node_id: UUID | None, by_id: dict[UUID, Department]) -> str:
    """「公司 / 设计部 / 智能化设计组」。找不到节点返回空串。"""
    if node_id is None:
        return ""
    parts: list[str] = []
    seen: set[UUID] = set()
    cur = by_id.get(node_id)
    while cur is not None and cur.id not in seen:
        seen.add(cur.id)
        parts.append(cur.name)
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return " / ".join(reversed(parts))


def _dept_descendant_ids(node_id: UUID, by_id: dict[UUID, Department]) -> set[UUID]:
    """节点自身 + 全部后代：点父部门要能看到下面所有组里的人。"""
    out = {node_id}
    changed = True
    while changed:
        changed = False
        for nid, node in by_id.items():
            if nid not in out and node.parent_id in out:
                out.add(nid)
                changed = True
    return out


def _dept_out(
    node: Department,
    by_id: dict[UUID, Department],
    *,
    user_count: int = 0,
    total_count: int = 0,
    children: list[DepartmentOut] | None = None,
) -> DepartmentOut:
    """显式构造输出模型。

    ⚠ 不能写 `DepartmentOut.model_validate(node)`：ORM 上有个 `children` 关系，
    pydantic 会去读它 → 在 async 上下文里触发懒加载 → MissingGreenlet（500）。
    设备分类树踩过同一个坑。
    """
    return DepartmentOut(
        id=node.id,
        parent_id=node.parent_id,
        code=node.code,
        name=node.name,
        sort_order=node.sort_order,
        is_active=node.is_active,
        notes=node.notes,
        path=_dept_path(node.id, by_id),
        user_count=user_count,
        total_count=total_count,
        children=children or [],
    )


def _user_out(user: User, by_id: dict[UUID, Department] | None = None) -> UserOut:
    out = UserOut.model_validate(user)
    if by_id is not None:
        out.department_path = _dept_path(user.department_id, by_id)
    return out


async def _counts_by_dept(db: AsyncSession) -> dict[UUID, int]:
    rows = await db.execute(
        select(User.department_id, func.count())
        .where(User.department_id.is_not(None))
        .group_by(User.department_id)
    )
    return {d: n for d, n in rows.all() if d is not None}


@router.get(
    "/departments/tree",
    response_model=DepartmentTreeOut,
    summary="组织架构树（含每个部门的人数）",
)
async def department_tree(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "read"))],
) -> DepartmentTreeOut:
    by_id = await _load_dept_nodes(db)
    counts = await _counts_by_dept(db)
    unassigned = await db.scalar(
        select(func.count()).select_from(User).where(User.department_id.is_(None))
    )

    out_by_id: dict[UUID, DepartmentOut] = {
        nid: _dept_out(n, by_id, user_count=counts.get(nid, 0)) for nid, n in by_id.items()
    }
    roots: list[DepartmentOut] = []
    for node in out_by_id.values():
        parent = out_by_id.get(node.parent_id) if node.parent_id else None
        if parent is not None:
            parent.children.append(node)
        else:
            roots.append(node)

    def _finalize(node: DepartmentOut) -> int:
        node.children.sort(key=lambda c: (c.sort_order, c.name))
        total = node.user_count
        for child in node.children:
            total += _finalize(child)
        node.total_count = total
        return total

    for root in roots:
        _finalize(root)
    roots.sort(key=lambda c: (c.sort_order, c.name))
    return DepartmentTreeOut(
        nodes=roots,
        unassigned=unassigned or 0,
        total=sum(counts.values()) + (unassigned or 0),
    )


@router.post(
    "/departments",
    response_model=DepartmentOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增部门/组（层级不限）",
)
async def create_department(
    body: DepartmentIn,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "create"))],
) -> DepartmentOut:
    if await db.scalar(select(Department).where(Department.code == body.code)):
        raise HTTPException(status_code=409, detail=f"部门代号 {body.code} 已存在")
    by_id = await _load_dept_nodes(db)
    if body.parent_id is not None and body.parent_id not in by_id:
        raise HTTPException(status_code=400, detail="上级部门不存在")

    node = Department(
        code=body.code,
        name=body.name,
        parent_id=body.parent_id,
        sort_order=body.sort_order,
        is_active=body.is_active,
        notes=body.notes,
    )
    db.add(node)
    await db.flush()
    await db.refresh(node)
    log.info(f"新增部门 {node.code} {node.name} by {user.username}")
    return _dept_out(node, {**by_id, node.id: node})


@router.patch(
    "/departments/{department_id}",
    response_model=DepartmentOut,
    summary="修改部门（改名/挪位置/排序/停用）",
)
async def update_department(
    department_id: UUID,
    body: DepartmentUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "update"))],
) -> DepartmentOut:
    node = await db.get(Department, department_id)
    if not node:
        raise HTTPException(status_code=404, detail="部门不存在")
    by_id = await _load_dept_nodes(db)
    data = body.model_dump(exclude_unset=True)

    if "parent_id" in data:
        new_parent = data["parent_id"]
        if new_parent is not None:
            if new_parent not in by_id:
                raise HTTPException(status_code=400, detail="上级部门不存在")
            if new_parent == node.id:
                raise HTTPException(status_code=400, detail="不能把自己设成上级部门")
            if new_parent in _dept_descendant_ids(node.id, by_id):
                raise HTTPException(
                    status_code=400, detail="不能把部门移动到它自己的下级里"
                )
        node.parent_id = new_parent

    for field in ("name", "sort_order", "is_active", "notes"):
        if field in data:
            setattr(node, field, data[field])
    await db.flush()

    # 改名之后，挂在它（及子树）下面用户的自由文本 department 要跟着对齐，
    # 否则报表里会出现新旧两个名字。
    if "name" in data:
        names = {nid: n.name for nid, n in by_id.items()}
        names[node.id] = node.name
        for uid in _dept_descendant_ids(node.id, by_id):
            await db.execute(
                User.__table__.update()
                .where(User.department_id == uid)
                .values(department=names.get(uid))
            )

    await db.refresh(node)
    fresh = await _load_dept_nodes(db)
    counts = await _counts_by_dept(db)
    return _dept_out(node, fresh, user_count=counts.get(node.id, 0))


@router.delete(
    "/departments/{department_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除部门（有下级或有人时拒绝）",
)
async def delete_department(
    department_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "delete"))],
) -> None:
    node = await db.get(Department, department_id)
    if not node:
        raise HTTPException(status_code=404, detail="部门不存在")
    kids = await db.scalar(
        select(func.count())
        .select_from(Department)
        .where(Department.parent_id == node.id)
    )
    if kids:
        raise HTTPException(
            status_code=409, detail=f"该部门下还有 {kids} 个下级，请先删除或移走"
        )
    used = await db.scalar(
        select(func.count()).select_from(User).where(User.department_id == node.id)
    )
    if used:
        raise HTTPException(
            status_code=409, detail=f"该部门下还有 {used} 个用户，请先把人调到别的部门"
        )
    await db.delete(node)
    log.warning(f"删除部门 {node.code} {node.name} by {user.username}")


@router.get("/roles", summary="可选角色清单（内置 + 自定义，供下拉与标签用）")
async def list_roles(
    user: Annotated[User, Depends(require_permission("user", "read"))],
) -> list[dict]:
    """角色清单：任何能看用户管理的人都能读 —— 建号/改号的下拉、界面上显示角色名都要它。"""
    from app.core.roles import all_roles

    return all_roles()


@router.get("", response_model=Page[UserOut], summary="用户列表")
async def list_users(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    role: Role | None = None,
    keyword: str | None = None,
    department_id: UUID | None = Query(None, description="组织架构节点"),
    include_descendants: bool = Query(True, description="是否含下级部门的人"),
    unassigned: bool = Query(False, description="只看没分配部门的用户"),
) -> Page[UserOut]:
    stmt = select(User)
    if role:
        stmt = stmt.where(User.role == role)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(User.username.ilike(like) | User.full_name.ilike(like))
    by_id = await _load_dept_nodes(db)
    if unassigned:
        stmt = stmt.where(User.department_id.is_(None))
    elif department_id is not None:
        if department_id not in by_id:
            raise HTTPException(status_code=404, detail="部门不存在")
        ids = (
            _dept_descendant_ids(department_id, by_id)
            if include_descendants
            else {department_id}
        )
        stmt = stmt.where(User.department_id.in_(ids))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(
            stmt.order_by(User.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return Page[UserOut](
        items=[_user_out(r, by_id) for r in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="管理员创建用户",
)
async def create_user(
    body: UserCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "create"))],
) -> User:
    """管理员建号。以前收的是裸 dict：非法 role/status 会抛 ValueError → 500
    （本该 422），而且 body 里还能塞 is_superuser —— 那是绕过 require_roles
    与 can_see_cost 的开关，不该由请求体决定。现在统一走 UserCreate 校验。
    """
    if await db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if await db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="邮箱已注册")

    # 挂了部门节点就以节点为准：自由文本的 department 用节点名回填，
    # 保证报表里同一个部门永远同一个写法。
    department_name = body.department
    if body.department_id is not None:
        node = await db.get(Department, body.department_id)
        if node is None:
            raise HTTPException(status_code=400, detail="部门不存在")
        department_name = node.name

    new_user = User(
        username=body.username,
        email=body.email,
        hashed_password=await hash_password_async(body.password),
        full_name=body.full_name,
        phone=body.phone,
        department=department_name,
        department_id=body.department_id,
        role=body.role,
        status=body.status,
        # 普通建号永远是 False：superuser 只能由另一个 superuser 在
        # 库里直接改（DBA 操作），不经这个接口。
        is_superuser=False,
    )
    db.add(new_user)
    await db.flush()
    await db.refresh(new_user)
    log.info(f"管理员 {user.username} 创建用户 {new_user.username} ({new_user.role})")
    by_id = await _load_dept_nodes(db)
    return _user_out(new_user, by_id)


@router.post("/{user_id}/force-logout", summary="强制下线（递增会话版本，旧令牌立刻失效）")
async def force_logout(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "update"))],
) -> dict:
    """把该用户的 `token_version` +1 —— 他手上所有令牌立刻 401（H-8）。

    比"改密码"温和：不用告诉对方新口令，也不会打断他下次登录。
    """
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    target.token_version = int(target.token_version or 0) + 1
    await db.commit()
    log.warning(f"强制下线：{target.username} by {user.username}")
    return {"ok": True, "username": target.username, "token_version": target.token_version}


@router.patch("/{user_id}", response_model=UserOut, summary="修改用户（角色/状态/资料）")
async def update_user(
    user_id: UUID,
    body: UserUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "update"))],
) -> User:
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 走 UserUpdate 之后非法 role/status 由 pydantic 拦成 422，
    # 不再需要手工 try/except（以前是裸 dict，Role("bogus") 会冒到全局 → 500）。
    data = body.model_dump(exclude_unset=True)
    if data.get("password"):
        target.hashed_password = await hash_password_async(data.pop("password"))
        # 改密码 = 让旧令牌立刻失效（H-8）：版本 +1，别的设备上还开着的会话直接掉线
        target.token_version = int(target.token_version or 0) + 1
    # 改部门节点时同步自由文本字段（与 create 同一口径）
    if "department_id" in data:
        if data["department_id"] is None:
            data["department"] = None
        else:
            node = await db.get(Department, data["department_id"])
            if node is None:
                raise HTTPException(status_code=400, detail="部门不存在")
            data["department"] = node.name
    for field, value in data.items():
        # 不能把自己降权/停用：改完自己就再也进不来了（没人是 superuser 时尤其致命）
        if field == "role" and target.id == user.id and value != target.role:
            raise HTTPException(status_code=400, detail="不能修改自己的角色")
        if field == "status" and target.id == user.id and str(value) != str(target.status):
            raise HTTPException(status_code=400, detail="不能停用自己")
        if field == "email" and value != target.email:
            if await db.scalar(select(User).where(User.email == value, User.id != target.id)):
                raise HTTPException(status_code=409, detail="邮箱已注册")
        setattr(target, field, value)

    await db.flush()
    await db.refresh(target)
    by_id = await _load_dept_nodes(db)
    return _user_out(target, by_id)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, summary="停用用户（软删除）")
async def disable_user(
    user_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("user", "delete"))],
) -> None:
    if user_id == user.id:
        raise HTTPException(status_code=400, detail="不能停用自己")
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    target.status = UserStatus.DISABLED
    log.warning(f"停用用户 {target.username} by {user.username}")

