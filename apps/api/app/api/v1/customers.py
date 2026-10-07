"""客户档案路由（2026-10-04）。

权限：全员可读、admin/sales/manager 可写、只有 admin 能删（见 permissions.py）。
删除用**停用**（is_active=False）而不是物理删除 —— 历史项目还引用着它，
物理删除会让老项目的客户名变成空白。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.customer import Customer
from app.models.project import Project
from app.models.user import User
from app.schemas import CustomerCreate, CustomerOut, CustomerUpdate, Page

router = APIRouter(prefix="/customers", tags=["客户档案"])
log = get_logger("api.customers")


async def _next_code(db: AsyncSession) -> str:
    """CUS-0001 递增（与设备编号同一套思路：取当前最大值 +1）。"""
    last = await db.scalar(
        select(func.max(Customer.code)).where(Customer.code.like("CUS-%"))
    )
    n = 1
    if last:
        try:
            n = int(last.split("-")[-1]) + 1
        except ValueError:
            n = 1
    return f"CUS-{n:04d}"


@router.get("", response_model=Page[CustomerOut], summary="客户列表（分页 + 关键词）")
async def list_customers(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("customer", "read"))],
    keyword: str | None = None,
    is_active: bool | None = None,
    include_inactive: bool = Query(
        False, description="true = 连停用的一起返回（is_active 仍可进一步过滤）"
    ),
    page: int = 1,
    page_size: int = 50,
) -> Page[CustomerOut]:
    stmt = select(Customer)
    if keyword:
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Customer.name.ilike(like),
                Customer.short_name.ilike(like),
                Customer.code.ilike(like),
                Customer.contact_name.ilike(like),
            )
        )
    # is_active 是"只看启用的 / 只看停用的"；要两种都要得传 include_inactive=true
    if is_active is not None:
        stmt = stmt.where(Customer.is_active.is_(is_active))
    elif not include_inactive:
        stmt = stmt.where(Customer.is_active.is_(True))

    total = await db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = (
        await db.scalars(
            stmt.order_by(Customer.code).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return Page[CustomerOut](
        items=[CustomerOut.model_validate(r) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "", response_model=CustomerOut, status_code=status.HTTP_201_CREATED,
    summary="新建客户",
)
async def create_customer(
    body: CustomerCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("customer", "create"))],
) -> Customer:
    if await db.scalar(select(Customer).where(Customer.name == body.name)):
        raise HTTPException(status_code=409, detail=f"客户「{body.name}」已存在")
    data = body.model_dump()
    code = (data.pop("code", None) or "").strip() or await _next_code(db)
    if await db.scalar(select(Customer).where(Customer.code == code)):
        raise HTTPException(status_code=409, detail=f"客户编号 {code} 已被占用")
    c = Customer(**data, code=code, created_by=user.id)
    db.add(c)
    await db.flush()
    await db.refresh(c)
    log.info(f"新建客户 {c.code} {c.name} by {user.username}")
    return c


@router.get("/{customer_id}", response_model=CustomerOut, summary="客户详情")
async def get_customer(
    customer_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("customer", "read"))],
) -> Customer:
    c = await db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    return c


@router.patch("/{customer_id}", response_model=CustomerOut, summary="更新客户")
async def update_customer(
    customer_id: UUID,
    body: CustomerUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("customer", "update"))],
) -> Customer:
    c = await db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    data = body.model_dump(exclude_unset=True)
    if "name" in data and data["name"] != c.name:
        if await db.scalar(select(Customer).where(Customer.name == data["name"])):
            raise HTTPException(status_code=409, detail=f"客户「{data['name']}」已存在")
        # 改名要同步到还引用着它的项目上（项目列表显示的是快照文本）
        await db.execute(
            Project.__table__.update()
            .where(Project.customer_id == c.id)
            .values(customer_name=data["name"])
        )
    for k, v in data.items():
        setattr(c, k, v)
    await db.flush()
    await db.refresh(c)
    log.info(f"更新客户 {c.code} by {user.username}: {sorted(data)}")
    return c


@router.delete(
    "/{customer_id}", status_code=status.HTTP_204_NO_CONTENT, summary="停用客户（不物理删除）"
)
async def deactivate_customer(
    customer_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("customer", "delete"))],
) -> None:
    """停用而不是删除：历史项目还引用着它，删掉老项目的客户名就空了。"""
    c = await db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(status_code=404, detail="客户不存在")
    c.is_active = False
    await db.flush()
    log.warning(f"停用客户 {c.code} {c.name} by {user.username}")
