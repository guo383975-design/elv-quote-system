"""Projects router: CRUD + auto-generated codes."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.core.workflow import (
    can_move_project,
    ordered_targets,
    project_transition_error,
)
from app.db.session import get_db
from app.models.audit import AuditAction, ProjectAuditLog
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote import Quote, QuoteStatus
from app.models.subsystem import Subsystem
from app.models.user import User
from app.schemas import (
    Page,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    SubsystemOut,
)
from app.services.audit import project_audit

router = APIRouter(prefix="/projects", tags=["项目"])
log = get_logger("api.projects")

# 待审批 = 已提交/审核中。列表上给经理一个"这一单有几张在等我"的徽标。
_PENDING_QUOTE_STATUSES = (QuoteStatus.SUBMITTED, QuoteStatus.REVIEWING)


async def _quote_stats(db: AsyncSession, project_ids: list[UUID]) -> dict[UUID, tuple[int, int]]:
    """每个项目的（报价单数, 待审批数）——一次聚合查完，不要 N+1。"""
    if not project_ids:
        return {}
    rows = await db.execute(
        select(
            Quote.project_id,
            func.count(),
            func.count().filter(Quote.status.in_(_PENDING_QUOTE_STATUSES)),
        )
        .where(Quote.project_id.in_(project_ids))
        .group_by(Quote.project_id)
    )
    return {pid: (total, pending) for pid, total, pending in rows.all()}


async def _next_project_code(db: AsyncSession) -> str:
    """生成 PRJ-YYYYMM-NNNN。

    ⚠ 必须用「本月已存在的**最大**编号 + 1」，不能用「本月行数 + 1」。
    projects.code 上有唯一索引，而 DELETE /projects/{id} 是**硬删除**：
    删掉任意一单都会让 count 回退，下一次建单算出的编号就会撞上仍然存在的单
    （例如已有 0002/0003，删掉 0001 后 count=2 → 又算出 0003）→ IntegrityError。
    末尾再探一次空位，兜住并发和手工编号造成的空洞。
    """
    from datetime import date

    ym = date.today().strftime("%Y%m")
    prefix = f"PRJ-{ym}-"
    last = await db.scalar(
        select(func.max(Project.code)).where(Project.code.like(f"{prefix}%"))
    )
    n = 1
    if last:
        try:
            n = int(str(last).rsplit("-", 1)[1]) + 1
        except (ValueError, IndexError):
            n = 1
    code = f"{prefix}{n:04d}"
    while await db.scalar(select(Project.id).where(Project.code == code)):
        n += 1
        code = f"{prefix}{n:04d}"
    return code


@router.post(
    "/auto-code",
    summary="生成下一个项目编号（前端新建表单预填，与设备库 /devices/auto-code 对齐）",
)
async def gen_project_code(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "create"))],
) -> dict:
    return {"code": await _next_project_code(db)}


@router.get("", response_model=Page[ProjectOut], summary="项目列表（分页 + 关键字）")
async def list_projects(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "read"))],
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    keyword: str | None = Query(None, description="按项目名/客户名/编号模糊搜索"),
    status_filter: ProjectStatus | None = Query(None, alias="status"),
    project_type: ProjectType | None = Query(None),
) -> Page[ProjectOut]:
    stmt = select(Project)
    if keyword:
        like = f"%{keyword}%"
        stmt = stmt.where(
            Project.name.ilike(like)
            | Project.customer_name.ilike(like)
            | Project.code.ilike(like)
        )
    if status_filter:
        stmt = stmt.where(Project.status == status_filter)
    if project_type:
        stmt = stmt.where(Project.project_type == project_type)

    total = await db.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (
        await db.scalars(
            stmt.order_by(Project.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    stats = await _quote_stats(db, [r.id for r in rows])
    items: list[ProjectOut] = []
    for r in rows:
        out = ProjectOut.model_validate(r)
        out.quote_count, out.pending_quote_count = stats.get(r.id, (0, 0))
        items.append(out)
    return Page[ProjectOut](items=items, total=total or 0, page=page, page_size=page_size)


async def _apply_customer(
    db: AsyncSession, data: dict, *, only_if_empty: bool = True
) -> None:
    """按 customer_id 把客户档案里的名称/联系人/电话填进 data（原地修改）。

    `only_if_empty=True`：只在调用方没显式给值时才填 —— 项目允许有专属联系人，
    不能被客户档案里的总机号覆盖掉。
    """
    cid = data.get("customer_id")
    if not cid:
        return
    from app.models.customer import Customer

    cust = await db.get(Customer, cid)
    if cust is None:
        raise HTTPException(status_code=422, detail="客户不存在")
    pairs = (
        ("customer_name", cust.name),
        ("customer_contact", cust.contact_name),
        ("customer_phone", cust.contact_phone),
    )
    for key, value in pairs:
        if value and (not only_if_empty or not data.get(key)):
            data[key] = value




async def _ensure_default_subsystems(db: AsyncSession, project: Project) -> int:
    """按「系统设置 → project.default_subsystems」补齐项目缺的专业（幂等）。

    返回新建数量。清单为空 = 什么都不建（用户明确关掉了自动创建）。
    """
    from app.models.subsystem import Subsystem, SubsystemType
    from app.services.settings import load_settings

    settings = await load_settings(db)
    wanted = (settings.get("project") or {}).get("default_subsystems") or []
    if not wanted:
        return 0

    existing = {
        str(s.subsystem_type)
        for s in (
            await db.scalars(select(Subsystem).where(Subsystem.project_id == project.id))
        ).all()
    }
    created = 0
    for order, raw in enumerate(wanted):
        if raw in existing:
            continue
        try:
            stype = SubsystemType(raw)
        except ValueError:
            log.warning(f"系统设置里的默认子系统取值不认识，跳过：{raw}")
            continue
        db.add(
            Subsystem(
                project_id=project.id,
                subsystem_type=stype,
                sort_order=order,
            )
        )
        created += 1
    if created:
        await db.flush()
        log.info(f"项目 {project.code} 自动创建 {created} 个默认子系统")
    return created


@router.post(
    "",
    response_model=ProjectOut,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目",
)
async def create_project(
    body: ProjectCreate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "create"))],
) -> Project:
    if await db.scalar(select(Project).where(Project.code == body.code)):
        raise HTTPException(status_code=409, detail=f"项目编号 {body.code} 已存在")

    data = body.model_dump()
    # 这个开关不是 Project 的字段：它控制"要不要自动建默认专业"，
    # 必须在构造 Project 之前剔除，否则 Project(**data) 直接 TypeError。
    auto_subsystems = bool(data.pop("create_default_subsystems", True))
    # 给了客户 → 用它兜底填客户名/联系人/电话（显式传的值优先）
    await _apply_customer(db, data)
    # 二选一校验：既没选档案、也没手填名称 → 明确报错，
    # 而不是让 NOT NULL 约束在 flush 时抛 500
    if not (data.get("customer_name") or "").strip():
        raise HTTPException(
            status_code=422, detail="必须填写客户名称，或从客户档案里选择一家客户"
        )
    # expected_start 用 date，但 schema 是 datetime；取日期部分
    if data.get("expected_start"):
        data["expected_start"] = data["expected_start"].date()

    # 新建时就允许选「报价中」（很多项目是边谈边做，建单即进入报价）；
    # 但要走同一张状态机 —— 不能新建时直接落成「已中标」。
    new_status = data.get("status", ProjectStatus.DRAFT)
    if new_status is not None and not can_move_project(
        ProjectStatus.DRAFT, new_status, is_admin=user.is_superuser
    ):
        raise HTTPException(
            status_code=409,
            detail=project_transition_error(
                ProjectStatus.DRAFT, new_status, is_admin=user.is_superuser
            ),
        )

    project = Project(**data, owner_id=user.id)
    db.add(project)
    await db.flush()
    # 自动建默认专业（2026-10-04）：不建的话，新建项目里一个子系统都没有，
    # 用户会以为"加设备加不进去"。清单来自「系统设置」，可用请求参数关掉。
    if auto_subsystems:
        await _ensure_default_subsystems(db, project)
    if ProjectStatus(new_status) is not ProjectStatus.DRAFT:
        # 建单时不是草稿 → 也留一条痕（否则"这单怎么一出生就是报价中"没人知道）
        db.add(
            project_audit(
                project_id=project.id,
                project_code=project.code,
                user_id=user.id,
                action=AuditAction.CREATE,
                payload={"from": None, "to": str(project.status)},
            )
        )
        await db.flush()
    await db.refresh(project)
    log.info(f"创建项目 {project.code} {project.name}")
    return project


@router.get(
    "/status-options",
    response_model=dict[str, list[str]],
    summary="各状态下可改成的状态（界面渲染下拉用，一次拉全表）",
)
async def status_options(
    user: Annotated[User, Depends(require_permission("project", "read"))],
) -> dict[str, list[str]]:
    """把状态机的"唯一真相"放在后端。

    前端如果自己抄一份流转表，早晚会漂移：改了一处忘了另一处，用户看到的下拉里
    有选项、点下去却 409。这里一次返回 {当前状态: [可改成的状态…]}，
    新建表单和编辑表单都用它 —— 不必每个项目再请求一次，也不会抄错。
    """
    is_admin = bool(user.is_superuser or str(user.role) == "admin")
    return {
        str(s): [str(t) for t in ordered_targets(s, is_admin=is_admin)]
        for s in ProjectStatus
    }


@router.get("/{project_id}", response_model=ProjectOut, summary="项目详情")
async def get_project(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "read"))],
) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


@router.patch("/{project_id}", response_model=ProjectOut, summary="更新项目")
async def update_project(
    project_id: UUID,
    body: ProjectUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "update"))],
) -> Project:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")

    data = body.model_dump(exclude_unset=True)
    if data.get("customer_id"):
        # only_if_empty 恒为 True：用户显式填的客户名/联系人**永远优先**，
        # 档案里的值只用来补空。反过来写的话，改一次客户就会把项目上
        # 手工维护的专属联系人覆盖成档案里的总机号。
        await _apply_customer(db, data)
    new_status = data.get("status")
    if new_status is not None and ProjectStatus(new_status) is not ProjectStatus(project.status):
        # 状态机校验：`draft → won` 这类跳变在业务上是错的（见 core/workflow.py）
        if not can_move_project(project.status, new_status, is_admin=user.is_superuser):
            raise HTTPException(
                status_code=409,
                detail=project_transition_error(
                    project.status, new_status, is_admin=user.is_superuser
                ),
            )
        db.add(
            project_audit(
                project_id=project.id,
                project_code=project.code,
                user_id=user.id,
                action=AuditAction.UPDATE,
                payload={"from": str(project.status), "to": str(new_status)},
            )
        )

    for key, value in data.items():
        if key == "expected_start" and value is not None:
            value = value.date()
        setattr(project, key, value)
    await db.flush()
    await db.refresh(project)
    return project


@router.get("/{project_id}/audit-logs", summary="项目操作留痕")
async def project_audit_logs(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("audit_log", "read"))],
    limit: int = Query(50, ge=1, le=200),
) -> list[dict]:
    """与 /quotes/{id}/audit-logs 同构：谁能看审计谁能看（管理员 · 财务）。"""
    rows = await db.scalars(
        select(ProjectAuditLog)
        .where(ProjectAuditLog.project_id == project_id)
        .order_by(ProjectAuditLog.created_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": str(r.id),
            "user_id": str(r.user_id) if r.user_id else None,
            "action": str(r.action),
            "payload": r.payload,
            "note": r.note,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows.all()
    ]


@router.get(
    "/{project_id}/allowed-statuses",
    response_model=list[str],
    summary="某个项目当前可以改成哪些状态（含当前状态本身）",
)
async def allowed_statuses(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "read"))],
) -> list[str]:
    """与 /projects/status-options 同源，只是绑到具体项目上（编辑时更方便）。"""
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    current = ProjectStatus(project.status)
    is_admin = bool(user.is_superuser or str(user.role) == "admin")
    # 当前状态自己也返回（界面下拉里要能显示"没改"）
    return [str(current)] + [str(s) for s in ordered_targets(current, is_admin=is_admin)]


@router.delete(
    "/{project_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除项目（级联删除子系统与报价单）",
)
async def delete_project(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("project", "delete"))],
) -> None:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    await db.delete(project)
    log.warning(f"删除项目 {project.code} by {user.username}")


@router.get(
    "/{project_id}/subsystems",
    response_model=list[SubsystemOut],
    summary="项目的子系统清单",
)
async def list_project_subsystems(
    project_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("subsystem", "read"))],
) -> list[Subsystem]:
    project = await db.get(Project, project_id)
    if not project:
        raise HTTPException(status_code=404, detail="项目不存在")
    rows = await db.scalars(
        select(Subsystem)
        .where(Subsystem.project_id == project_id)
        .order_by(Subsystem.sort_order)
    )
    return list(rows.all())




