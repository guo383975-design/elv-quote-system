"""报价单导出：Excel（.xlsx）与 PDF。

## 为什么导出要单独一个模块

导出是「客户版」和「内部版」的唯一分叉点：`sales` **有** `quote.export` 权限
但**没有**看成本/进价的权限（见 core/permissions.can_see_cost）。所以：

    include_cost = internal and can_see_cost(role=..., is_superuser=...)

默认（`internal` 不传）一律是**客户版**，不带任何成本/毛利列。要内部版必须
显式 `?internal=true`，且必须真的有权 —— 无权时返回 **403**，而不是"悄悄给你
一个没有成本列的客户版"。后者会让人以为成本是 0 或以为导出坏了，最坑。

## 关于「GET 却写库」

这两个接口会写一条 `action=export` 的审计日志（谁、在什么时候、导出了哪种格式、
是不是内部版）。这确实让 GET 带上了副作用，是个有意识的取舍：
「谁把报价单导出去给甲方了」在这个业务里是必须留痕的事，而 `audit_action`
枚举里本来就有 `export`。写入是**尽力而为**：任何异常都不会影响文件下载。
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote as urlquote
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.core.permissions import can_see_cost, can_see_profit
from app.db.session import get_db
from app.models.audit import AuditAction
from app.models.project import Project
from app.models.quote import Quote
from app.models.quote_item import QuoteItem
from app.models.subsystem import Subsystem
from app.models.user import User
from app.services.audit import quote_audit
from app.services.export_data import ExportContext, build_context
from app.services.settings import load_export_settings

router = APIRouter(prefix="/quotes", tags=["报价单导出"])
log = get_logger("api.exports")

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF_MEDIA = "application/pdf"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

async def _load_for_export(
    db: AsyncSession, quote_id: UUID
) -> tuple[Quote, Project, list[Subsystem]]:
    """一次把导出要用的东西全查出来（避免异步里的懒加载 → MissingGreenlet）。"""
    quote = await db.scalar(
        select(Quote)
        .options(selectinload(Quote.items).selectinload(QuoteItem.device))
        .where(Quote.id == quote_id)
    )
    if not quote:
        raise HTTPException(status_code=404, detail="报价单不存在")
    project = await db.get(Project, quote.project_id)
    if not project:
        # 外键是 CASCADE，正常不会发生；真发生了说明数据被外部改坏了
        raise HTTPException(status_code=404, detail="报价单所属项目不存在")
    subsystems = list(
        await db.scalars(
            select(Subsystem)
            .where(Subsystem.project_id == quote.project_id)
            .order_by(Subsystem.sort_order, Subsystem.id)
        )
    )
    return quote, project, subsystems


def _ordered_items(quote: Quote) -> list[QuoteItem]:
    """明细按 sort_order 排；同序时用 id 兜底，保证两次导出顺序一致。

    顺序不稳定会让甲方逐版对单时找不到行，也会让 diff 变成噪声。
    """
    return sorted(quote.items, key=lambda it: (it.sort_order or 0, str(it.id)))


def _resolve_include_profit(user: User) -> bool:
    """能否在导出件里带**毛利**行（2026-10-05 审计补上的一档）。

    与"能否带成本列"分开：成本可见 ≠ 利润可见。
    """
    return can_see_profit(role=user.role, is_superuser=bool(user.is_superuser))


def _resolve_include_cost(user: User, internal: bool) -> bool:
    """客户版 / 内部版的唯一判定处。"""
    if not internal:
        return False
    if not can_see_cost(role=user.role, is_superuser=bool(user.is_superuser)):
        raise HTTPException(
            status_code=403,
            detail=(
                "你没有查看成本/进价的权限，无法导出内部版报价单。"
                "去掉 internal=true 可以导出客户版（不含成本与毛利）。"
            ),
        )
    return True


def _content_disposition(filename: str, ascii_fallback: str) -> str:
    """构造 Content-Disposition。

    中文文件名必须走 RFC 5987 的 `filename*=UTF-8''…`；同时给一个纯 ASCII 的
    `filename=` 兜底，因为少数老客户端不认 `filename*`，会退化成
    `%E6%8A%A5...xlsx` 这种乱码文件名，甚至直接丢掉文件名。
    """
    return (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{urlquote(filename, safe='')}"
    )


async def _audit_export(
    db: AsyncSession, *, quote: Quote, user: User, fmt: str, include_cost: bool, rows: int
) -> None:
    """记一条导出留痕。**任何失败都不能影响文件下载**。"""
    try:
        db.add(
            quote_audit(
                quote_id=quote.id,
                quote_code=quote.code,
                user_id=user.id,
                action=AuditAction.EXPORT,
                payload={
                    "format": fmt,
                    "include_cost": include_cost,
                    "item_count": rows,
                    "total_with_tax": str(quote.total_with_tax),
                },
            )
        )
        await db.flush()
    except Exception as exc:  # pragma: no cover - 只在库异常时走到
        log.warning(f"导出审计日志写入失败（不影响下载）: {exc!r}")


def _base_name(ctx: ExportContext) -> str:
    code = ctx.meta.quote_code or "quote"
    return f"报价单_{code}_v{ctx.meta.version}"


def _renderer(kind: str):
    """延迟导入渲染器。

    故意**不**在模块顶层 import：渲染器各自的依赖（openpyxl / reportlab + 中文字体）
    如果缺了，顶层 import 会让**整个应用起不来**——所有接口一起 500，
    而实际上只有导出这一个功能不可用。放在这里能把故障限制在导出口子上，
    并且返回 503 + 说清缺什么，而不是一个没头绪的 500。
    """
    try:
        if kind == "xlsx":
            from app.services.export_excel import render
        else:
            from app.services.export_pdf import render
    except ImportError as exc:  # pragma: no cover - 只在依赖缺失时走到
        log.error(f"导出组件不可用（{kind}）: {exc!r}")
        raise HTTPException(
            status_code=503,
            detail=(
                f"导出组件不可用（{kind}）：{exc}。"
                "请确认服务端已装齐依赖（xlsx 需要 openpyxl；pdf 需要 reportlab 与中文字体）。"
            ),
        ) from exc
    return render


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

@router.get(
    "/{quote_id}/export.xlsx",
    summary="导出报价单 Excel（默认客户版，不含成本；?internal=true 出内部版）",
    response_class=Response,
    responses={200: {"content": {XLSX_MEDIA: {}}}},
)
async def export_quote_xlsx(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "export"))],
    internal: bool = Query(False, description="导出内部版（含成本/毛利），需要成本查看权限"),
) -> Response:
    include_cost = _resolve_include_cost(user, internal)
    quote, project, subsystems = await _load_for_export(db, quote_id)
    items = _ordered_items(quote)
    ctx = build_context(
        quote=quote, project=project, items=items, subsystems=subsystems,
        include_cost=include_cost,
        include_profit=_resolve_include_profit(user),
        settings=await load_export_settings(db),
    )
    # 渲染是几百毫秒的 CPU 独占，放线程池里跑，别卡住事件循环
    from starlette.concurrency import run_in_threadpool

    data = await run_in_threadpool(_renderer("xlsx"), ctx)
    await _audit_export(
        db, quote=quote, user=user, fmt="xlsx", include_cost=include_cost, rows=len(items)
    )
    return Response(
        content=data,
        media_type=XLSX_MEDIA,
        headers={
            "Content-Disposition": _content_disposition(
                f"{_base_name(ctx)}.xlsx", f"quote_{ctx.meta.quote_code or 'quote'}.xlsx"
            ),
            "Content-Length": str(len(data)),
        },
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

@router.get(
    "/{quote_id}/export.pdf",
    summary="导出报价单 PDF（默认客户版，不含成本；?internal=true 出内部版）",
    response_class=Response,
    responses={200: {"content": {PDF_MEDIA: {}}}},
)
async def export_quote_pdf(
    quote_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("quote", "export"))],
    internal: bool = Query(False, description="导出内部版（含成本/毛利），需要成本查看权限"),
) -> Response:
    include_cost = _resolve_include_cost(user, internal)
    quote, project, subsystems = await _load_for_export(db, quote_id)
    items = _ordered_items(quote)
    ctx = build_context(
        quote=quote, project=project, items=items, subsystems=subsystems,
        include_cost=include_cost,
        # 客户版 PDF 不出参数列 ✓（内部版 True 时出 ✓）——2026-10-05 用户要求 C
        show_spec_column=include_cost,
        settings=await load_export_settings(db),
    )
    from starlette.concurrency import run_in_threadpool as _rt

    data = await _rt(_renderer("pdf"), ctx)
    await _audit_export(
        db, quote=quote, user=user, fmt="pdf", include_cost=include_cost, rows=len(items)
    )
    return Response(
        content=data,
        media_type=PDF_MEDIA,
        headers={
            "Content-Disposition": _content_disposition(
                f"{_base_name(ctx)}.pdf", f"quote_{ctx.meta.quote_code or 'quote'}.pdf"
            ),
            "Content-Length": str(len(data)),
        },
    )


__all__ = ["router"]



