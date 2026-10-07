"""Suppliers router: 供应商 CRUD."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.supplier import Supplier
from app.models.user import User
from app.schemas import Page, SupplierCreate, SupplierOut, SupplierUpdate

router = APIRouter(prefix="/suppliers", tags=["供应商"])
log = get_logger("api.suppliers")


@router.get("", response_model=Page[SupplierOut], summary="供应商列表（分页 + 筛选）")
async def list_suppliers(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("supplier", "read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=500),
    keyword: str | None = Query(None, description="编码/名称 模糊搜索"),
    # 缺省 True：列表默认只看在用供应商，与 devices 一致；
    # 显式传 False 才会把已停用的也带出来。
    is_active: bool = Query(True, description="只看启用中的供应商"),
    # 「在用 + 停用都要」这件事没法用上面那个布尔表达 ——
    # is_active=false 的语义是"只看停用的"，前端把这个当成"含停用的"用过，结果
    # 勾了「显示已停用」却什么都没变。这里给一个显式开关，原来那个参数语义不动。
    include_inactive: bool = Query(False, description="在用与停用都要"),
) -> Page[SupplierOut]:
    stmt = select(Supplier)
    if not include_inactive:
        stmt = stmt.where(Supplier.is_active.is_(is_active))
    # 空字符串按 "无 keyword" 处理，避免 `ilike %%` 把全表当成噪声
    if keyword and keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(
                Supplier.code.ilike(like),
                Supplier.name.ilike(like),
                Supplier.contact_name.ilike(like),
            )
        )

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(
            stmt.order_by(Supplier.code).offset((page - 1) * page_size).limit(page_size)
        )
    ).all()
    return Page[SupplierOut](
        items=[SupplierOut.model_validate(r) for r in rows],
        total=total or 0,
        page=page,
        page_size=page_size,
    )


@router.post(
    "",
    response_model=SupplierOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增供应商",
)
async def create_supplier(
    body: SupplierCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("supplier", "create"))],
) -> Supplier:
    # 同时查两个 unique 字段：code 重了就报 code，name 重了就报 name，
    # 避免用户只看到"编号已存在"但实际是名称撞了。
    if await db.scalar(select(Supplier).where(Supplier.code == body.code)):
        raise HTTPException(status_code=409, detail=f"供应商编号 {body.code} 已存在")
    if await db.scalar(select(Supplier).where(Supplier.name == body.name)):
        raise HTTPException(status_code=409, detail=f"供应商名称 {body.name} 已存在")

    supplier = Supplier(**body.model_dump())
    db.add(supplier)
    await db.flush()
    await db.refresh(supplier)
    log.info(f"新增供应商 {supplier.code} {supplier.name}")
    return supplier


@router.get("/{supplier_id}", response_model=SupplierOut, summary="供应商详情")
async def get_supplier(
    supplier_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("supplier", "read"))],
) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="供应商不存在")
    return supplier


@router.patch("/{supplier_id}", response_model=SupplierOut, summary="更新供应商")
async def update_supplier(
    supplier_id: UUID,
    body: SupplierUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("supplier", "update"))],
) -> Supplier:
    supplier = await db.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="供应商不存在")

    data = body.model_dump(exclude_unset=True)
    # name 改了就得再查一次唯一性。code 这里不允许改（schema 没暴露），
    # 但**重名**只在 name 真改了的情况下检查，否则空 update 会被空报错。
    if "name" in data and data["name"] != supplier.name:
        if await db.scalar(
            select(Supplier).where(
                Supplier.name == data["name"], Supplier.id != supplier.id
            )
        ):
            raise HTTPException(status_code=409, detail=f"供应商名称 {data['name']} 已存在")

    for key, value in data.items():
        setattr(supplier, key, value)
    await db.flush()
    await db.refresh(supplier)
    return supplier


@router.delete(
    "/{supplier_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="停用供应商（软删除）",
)
async def deactivate_supplier(
    supplier_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("supplier", "delete"))],
) -> None:
    supplier = await db.get(Supplier, supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="供应商不存在")
    supplier.is_active = False
    log.warning(f"停用供应商 {supplier.code}")


__all__ = ["router"]

