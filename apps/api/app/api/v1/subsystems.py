"""Subsystems router: 项目的子系统增删改."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.project import Project
from app.models.subsystem import Subsystem, SubsystemType
from app.models.supplier import Supplier
from app.models.user import User
from app.schemas import SubsystemCreate, SubsystemOut

router = APIRouter(prefix="/subsystems", tags=["子系统"])
log = get_logger("api.subsystems")


def _as_uuid(value: object, field: str) -> UUID | None:
    """route 层 Pydantic 已经把 dict[str, Any] 里的 UUID 当成字符串收了，
    这里统一再转一次。空串 / None → None；非法 → 400。"""
    if value is None or value == "":
        return None
    try:
        return UUID(str(value))
    except (TypeError, ValueError) as err:
        raise HTTPException(
            status_code=400, detail=f"{field} 不是合法的 UUID"
        ) from err


@router.get("", response_model=list[SubsystemOut], summary="子系统列表（按项目筛选）")
async def list_subsystems(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("subsystem", "read"))],
    project_id: UUID | None = None,
    subsystem_type: SubsystemType | None = None,
) -> list[Subsystem]:
    stmt = select(Subsystem)
    if project_id:
        stmt = stmt.where(Subsystem.project_id == project_id)
    if subsystem_type:
        stmt = stmt.where(Subsystem.subsystem_type == subsystem_type)
    rows = await db.scalars(stmt.order_by(Subsystem.sort_order))
    return list(rows.all())


@router.post(
    "",
    response_model=SubsystemOut,
    status_code=status.HTTP_201_CREATED,
    summary="新增子系统",
)
async def create_subsystem(
    body: SubsystemCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("subsystem", "create"))],
) -> Subsystem:
    if not await db.get(Project, body.project_id):
        raise HTTPException(status_code=404, detail="项目不存在")

    # 重复判定：类型 **且** 名称都相同才算重复（2026-10-05）
    # 以前只比类型 ✗ → "同一项目再加一套视频监控（名字不同）"被 409 挡住，
    # 用户表现为"新增子系统无法增加" ✗。现在可以同类多套、各自命名 ✓。
    same_name = (body.custom_name or "").strip()
    exists = await db.scalar(
        select(Subsystem).where(
            Subsystem.project_id == body.project_id,
            Subsystem.subsystem_type == body.subsystem_type,
            func.coalesce(Subsystem.custom_name, "") == same_name,
        )
    )
    if exists:
        raise HTTPException(
            status_code=409,
            detail=(
                f"该项目已存在「{same_name or body.subsystem_type}」这个子系统；"
                "要再加一套同类子系统，请换一个名称"
            ),
        )

    # supplier_id 可选 —— 传了但该供应商不存在/已停用都报 400，比静默丢更安全
    if body.supplier_id is not None:
        supplier = await db.get(Supplier, body.supplier_id)
        if supplier is None or not supplier.is_active:
            raise HTTPException(status_code=400, detail="供应商不存在或已停用")

    sub = Subsystem(**body.model_dump())
    db.add(sub)
    await db.flush()
    await db.refresh(sub)
    return sub


@router.get("/{subsystem_id}", response_model=SubsystemOut, summary="子系统详情")
async def get_subsystem(
    subsystem_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("subsystem", "read"))],
) -> Subsystem:
    sub = await db.get(Subsystem, subsystem_id)
    if not sub:
        raise HTTPException(status_code=404, detail="子系统不存在")
    return sub


@router.patch("/{subsystem_id}", response_model=SubsystemOut, summary="更新子系统")
async def update_subsystem(
    subsystem_id: UUID,
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("subsystem", "update"))],
) -> Subsystem:
    sub = await db.get(Subsystem, subsystem_id)
    if not sub:
        raise HTTPException(status_code=404, detail="子系统不存在")
    for field in ("custom_name", "sort_order", "notes"):
        if field in body:
            setattr(sub, field, body[field])
    # supplier_id 允许三种语义：传 null → 解除；传 UUID → 校验后挂上；不传 → 不动
    if "supplier_id" in body:
        new_sid = _as_uuid(body["supplier_id"], "supplier_id")
        if new_sid is not None:
            supplier = await db.get(Supplier, new_sid)
            if supplier is None or not supplier.is_active:
                raise HTTPException(status_code=400, detail="供应商不存在或已停用")
        sub.supplier_id = new_sid
    await db.flush()
    await db.refresh(sub)
    return sub


@router.delete("/{subsystem_id}", status_code=status.HTTP_204_NO_CONTENT, summary="删除子系统")
async def delete_subsystem(
    subsystem_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("subsystem", "delete"))],
) -> None:
    sub = await db.get(Subsystem, subsystem_id)
    if not sub:
        raise HTTPException(status_code=404, detail="子系统不存在")
    await db.delete(sub)
