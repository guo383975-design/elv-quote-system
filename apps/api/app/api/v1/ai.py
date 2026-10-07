"""AI 助理路由（2026-10-04）。

P0 只做**配置与管理**（用户明确要求：API 只有 admin 能设置与管理）：
  · 读配置：**永远返回脱敏版**（`api_key_cipher` 不出服务端）
  · 写配置：key 单独处理（新值→加密+掩码；空→保持不变；`__CLEAR__`→清空）
  · 测试连接：由后端发起，前端不接触 key
  · 用量台账：谁、什么任务、多少 token、成败
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.api.v1.imports import MAX_UPLOAD_BYTES
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.ai import AiUsageLog
from app.models.device import Device
from app.models.project import Project
from app.models.supplier import Supplier
from app.models.user import User
from app.services import ai_client
from app.services.ai_crypto import encrypt, looks_like_key, mask
from app.services.settings import K_AI, load_settings, redact_settings, save_settings

router = APIRouter(prefix="/ai", tags=["AI 助理"])
log = get_logger("api.ai")

# 想清空 key 时传这个（比"传空字符串"明确 —— 空字符串在表单里太容易是误操作）
CLEAR_TOKEN = "__CLEAR__"


class AiConfigUpdate(BaseModel):
    enabled: bool | None = None
    provider: str | None = Field(None, max_length=32)
    base_url: str | None = Field(None, max_length=255)
    model: str | None = Field(None, max_length=64)
    vision_model: str | None = Field(None, max_length=64)
    temperature: float | None = Field(None, ge=0, le=2)
    timeout_seconds: int | None = Field(None, ge=5, le=600)
    redact_customer: bool | None = None
    # 新 key（明文，只在这一次请求体里出现，存库即加密）；
    # 传空/不传 = 不改；传 __CLEAR__ = 清空
    api_key: str | None = Field(None, max_length=512)


@router.get("/config", summary="读取 AI 配置（脱敏：不含 API Key 明文/密文）")
async def get_ai_config(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "configure"))],
) -> dict:
    return redact_settings(await load_settings(db))[K_AI]


@router.put("/config", summary="保存 AI 配置（仅 admin；API Key 加密存储）")
async def update_ai_config(
    body: AiConfigUpdate,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "configure"))],
) -> dict:
    patch: dict = {}
    data = body.model_dump(exclude_unset=True)

    raw_key = data.pop("api_key", None)
    if raw_key is not None:
        key = raw_key.strip()
        if key == CLEAR_TOKEN:
            patch["api_key_cipher"] = ""
            patch["api_key_hint"] = ""
            log.warning(f"AI API Key 被清空 by {user.username}")
        elif key:
            if not looks_like_key(key):
                raise HTTPException(
                    status_code=422,
                    detail="这看起来不是一个 API Key（太短或含空格）。请粘贴完整密钥。",
                )
            patch["api_key_cipher"] = encrypt(key)
            patch["api_key_hint"] = mask(key)
            log.warning(f"AI API Key 已更新 by {user.username}（{mask(key)}）")
        # key 为空字符串 = 不改（表单回显的是掩码，用户没动就不该覆盖）

    patch.update(data)
    # 改了地址/模型 → 之前那次测试结果就不算数了
    if any(k in patch for k in ("base_url", "model", "api_key_cipher")):
        patch["last_test_ok"] = None
        patch["last_test_at"] = None
        patch["last_test_error"] = None

    saved = await save_settings(db, {K_AI: patch}, actor_id=user.id)

    # 留痕（2026-10-05）：改 AI 配置是**敏感操作**（能改 base_url 的人
    # 可以把报价数据导到自己的服务器上）。这里只记字段与"有没有重设 Key"，
    # **不记 Key 本身**（连密文也不记）。
    from app.models.audit import SettingAuditAction
    from app.services.audit import setting_audit

    redacted = redact_settings(saved)[K_AI]
    # 密钥相关字段**连字段名都不进差异** —— 语义由摘要里的 key_note 承担
    # （否则留痕里会出现 api_key_cipher 这种字段名，虽然没泄露值，也没必要）。
    _never_in_diff = ("last_test_ok", "last_test_at", "last_test_error",
                      "api_key_cipher", "api_key_hint")
    touched = [k for k in patch if k not in _never_in_diff]
    key_note = (
        "重设了 API Key" if patch.get("api_key_cipher")
        else ("清空了 API Key" if "api_key_cipher" in patch else "未动 Key")
    )
    db.add(
        setting_audit(
            user_id=user.id,
            username=user.username,
            action=SettingAuditAction.AI_CONFIG,
            summary=f"改 AI 配置：{key_note}；字段={'、'.join(sorted(touched)) or '无'}",
            diff=[{
                "group": "ai",
                "changes": [
                    {"field": k, "from": "", "to": str(redacted.get(k))}
                    for k in sorted(touched)
                ],
            }],
        )
    )
    await db.commit()
    return redacted


@router.post("/test-connection", summary="测试连通性（后端发起，前端不接触 Key）")
async def ai_test_connection(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "configure"))],
) -> dict:
    cfg = await ai_client.load_ai_config(db)
    now = datetime.now(tz=UTC).isoformat()
    try:
        result = await ai_client.test_connection(db, cfg=cfg, user=user)
    except ai_client.AiNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ai_client.AiCallError as exc:
        await save_settings(
            db,
            {K_AI: {"last_test_ok": False, "last_test_at": now, "last_test_error": str(exc)}},
            actor_id=user.id,
        )
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    await save_settings(
        db,
        {K_AI: {
            "last_test_ok": True, "last_test_at": now, "last_test_error": None,
        }},
        actor_id=user.id,
    )
    return result


@router.get("/usage", summary="AI 用量台账（近 N 条 + 汇总）")
async def ai_usage(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "configure"))],
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    rows = (
        await db.scalars(
            select(AiUsageLog).order_by(AiUsageLog.created_at.desc()).limit(limit)
        )
    ).all()
    totals = (
        await db.execute(
            select(
                func.count(AiUsageLog.id),
                func.coalesce(func.sum(AiUsageLog.total_tokens), 0),
                func.coalesce(func.sum(AiUsageLog.duration_ms), 0),
            )
        )
    ).one()
    failed = await db.scalar(
        select(func.count(AiUsageLog.id)).where(AiUsageLog.ok.is_(False))
    )
    return {
        "totals": {
            "calls": int(totals[0] or 0),
            "total_tokens": int(totals[1] or 0),
            "duration_ms": int(totals[2] or 0),
            "failed": int(failed or 0),
        },
        "items": [
            {
                "id": str(r.id),
                "username": r.username,
                "task": r.task,
                "model": r.model,
                "total_tokens": r.total_tokens,
                "duration_ms": r.duration_ms,
                "ok": r.ok,
                "error": r.error,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# P1：供应商报价解析（AI 产出候选行，人工确认后才落库）
# ---------------------------------------------------------------------------

class ParseCommitRow(BaseModel):
    """一行确认后的数据。**只认这里传来的字段** —— 不回头看任务里的原始结果，
    避免"用户改了界面上的数字、落库却用了 AI 原值"这种错位。
    """

    row: int | None = None
    name: str = Field(..., min_length=1, max_length=255)
    brand: str | None = Field(None, max_length=64)
    model: str | None = Field(None, max_length=128)
    unit: str | None = Field(None, max_length=16)
    quantity: Decimal | None = None
    unit_price: Decimal = Field(..., description="供应商报价（作为进价）")
    device_id: UUID | None = Field(None, description="匹配到的设备库 id；为空则该行跳过")
    remark: str | None = Field(None, max_length=255)


class ParseCommitRequest(BaseModel):
    rows: list[ParseCommitRow] = Field(..., min_length=1)
    supplier_id: UUID | None = None
    # 供应商报价的用途：目前只支持更新设备库进价（新建设备走设备库导入那条路）
    target: Literal["device_library"] = "device_library"


def _extract_plain_text(text: str):
    """用户直接粘贴的报价内容：当成文本类抽取结果（同样给行号，便于逐字核对）。"""
    from app.services.ai_extract import ExtractResult

    lines = list((text or "").splitlines())
    numbered = "\n".join(f"{i}\t{ln}" for i, ln in enumerate(lines, start=1))
    rows = [ln.split("\t") for ln in lines]
    return ExtractResult(engine="text", text=numbered, rows=rows, preview=numbered[:2000])

@router.post("/supplier-quote-parse", summary="解析供应商报价（PDF/图片/Excel/文本；不落库）")
async def ai_parse_supplier_quote(
    background_tasks: BackgroundTasks,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
    file: Annotated[UploadFile | None, File()] = None,
    text: Annotated[str | None, Form()] = None,
    supplier_id: Annotated[UUID | None, Form()] = None,
    project_id: Annotated[UUID | None, Form()] = None,
    force_ai: Annotated[bool, Form()] = False,
    # 解析方式三档（2026-10-05）：auto=先规则后模型 / rule=只用规则 / llm=强制大模型。
    # 用户明确问过"当前 Excel 是内置程序解析还是大模型解析？能不能自己选？"
    # —— 后端一直有 force_ai，但界面上没入口，所以等于不可选。
    engine_mode: Annotated[str, Form()] = "auto",
    background: Annotated[bool, Form()] = True,
) -> dict:
    """把供应商发来的报价变成**候选行**（含匹配结果与警告），一行都不写业务表。

    **默认异步**：立刻返回 `job_id` 与 `status=pending`，模型调用在后台跑，
    前端轮询 `/ai/parse-jobs/{id}`。传 `background=false` 则同步等结果
    （单元测试与脚本用；模型慢时接口会长时间挂着）。
    """
    from app.models.ai import AiParseJob, AiParseStatus
    from app.services.ai_extract import ExtractError, extract_document
    from app.services.ai_quote_parse import parse_supplier_quote

    # 解析方式归一化（三档 → 两个开关）。保留旧的 force_ai 语义：
    # 老客户端传 force_ai=true 仍然等于"强制大模型"，不会因为升级接口而行为变化。
    mode = (engine_mode or "auto").strip().lower()
    if mode not in ("auto", "rule", "llm"):
        raise HTTPException(
            status_code=422,
            detail=f"engine_mode 只能是 auto / rule / llm，收到 {engine_mode!r}",
        )
    if force_ai and mode == "auto":
        mode = "llm"
    force_ai = mode == "llm"        # 强制用模型
    allow_ai = mode != "rule"       # 是否允许用模型（rule 档绝不调模型）

    if file is None and not (text or "").strip():
        raise HTTPException(status_code=422, detail="请上传文件，或直接把报价内容粘贴到文本框")

    filename = "粘贴的文本"
    data = b""
    mime = None
    if file is not None:
        data = await file.read()
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413, detail=f"文件超过 {MAX_UPLOAD_BYTES // 1024 // 1024} MB"
            )
        filename = file.filename or filename
        mime = file.content_type

    supplier_name = None
    if supplier_id:
        supplier = await db.get(Supplier, supplier_id)
        if supplier is None:
            raise HTTPException(status_code=404, detail="供应商不存在")
        supplier_name = supplier.name
    project_name = None
    if project_id:
        project = await db.get(Project, project_id)
        if project is not None:
            project_name = f"{project.code} {project.name}"

    try:
        extract = (
            extract_document(filename=filename, data=data, mime=mime)
            if file is not None
            else _extract_plain_text(text or "")
        )
    except ExtractError as exc:
        # 读不了的文件给 422 + 人话；以前是 500「服务器内部错误」
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    job = AiParseJob(
        kind="supplier_quote",
        status=str(AiParseStatus.PENDING),
        filename=filename[:255],
        file_sha256=hashlib.sha256(data).hexdigest() if data else None,
        file_size=len(data),
        mime=(mime or "")[:128] or None,
        supplier_id=supplier_id,
        project_id=project_id,
        extracted_text=(extract.text or "")[:20000],
        created_by=user.id,
        created_by_name=user.username,
    )
    db.add(job)
    await db.flush()

    # 同一份文件解析过就给个提示（不拦，可能是故意重试）
    if job.file_sha256:
        prev = await db.scalar(
            select(AiParseJob)
            .where(AiParseJob.file_sha256 == job.file_sha256, AiParseJob.id != job.id)
            .order_by(AiParseJob.created_at.desc())
            .limit(1)
        )
        if prev is not None:
            extract.warnings.append(
                f"这份文件在 {prev.created_at:%Y-%m-%d %H:%M} 解析过一次"
                f"（任务 {str(prev.id)[:8]}），"
                "请确认是否重复"
            )

    if background:
        # 先提交任务行，再排后台解析：前端马上拿到 job_id 就能把它挂进对话
        await db.commit()
        background_tasks.add_task(
            _run_parse_job_bg,
            str(job.id),
            extract,
            supplier_name,
            project_name,
            str(user.id),
            user.username,
            force_ai,
            allow_ai=allow_ai,
        )
        log.info(f"AI 解析入队 by {user.username}：任务 {job.id}（{extract.engine}）")
        return {
            "job_id": str(job.id),
            "engine": "",
            "items": [],
            "warnings": list(extract.warnings),
            "supplier": {},
            "notes": [],
            "filename": filename,
            "extract_engine": extract.engine,
            "preview": extract.preview[:1000],
            "status": str(AiParseStatus.PENDING),
        }

    cfg = await ai_client.load_ai_config(db)
    try:
        result = await parse_supplier_quote(
            db,
            extract=extract,
            cfg=cfg,
            supplier_hint=supplier_name,
            project_hint=project_name,
            user=user,
            force_ai=force_ai,
            allow_ai=allow_ai,
            ref_id=str(job.id),
        )
    except ai_client.AiNotConfiguredError as exc:
        job.status = str(AiParseStatus.FAILED)
        job.result = {"warnings": [str(exc)]}
        await db.flush()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ai_client.AiCallError as exc:
        job.status = str(AiParseStatus.FAILED)
        job.result = {"warnings": [str(exc)]}
        await db.flush()
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    job.result = {**result, "engine_mode": mode}   # 记下用户选的方式，事后可查
    job.status = (
        str(AiParseStatus.READY) if result.get("items") else str(AiParseStatus.FAILED)
    )
    await db.flush()
    log.info(
        f"AI 解析供应商报价 by {user.username}（方式 {mode}／引擎 {result.get('engine')}）："
        f"{len(result.get('items') or [])} 行，任务 {job.id}"
    )
    return {"job_id": str(job.id), **result, "engine_mode": mode, "filename": filename,
            "extract_engine": extract.engine, "preview": extract.preview[:1000],
            "status": job.status}


async def _run_parse_job_bg(
    job_id: str,
    extract,
    supplier_hint: str | None,
    project_hint: str | None,
    user_id: str,
    username: str,
    force_ai: bool,
    allow_ai: bool = True,
) -> None:
    """后台跑模型解析，并把结果写回任务行。

    **必须用新的数据库会话**：请求会话在响应发出后就关闭了，
    蹭它会在异步上下文里报错（或静默丢数据）。
    """
    from uuid import UUID as _UUID

    from app.db.session import get_session_factory
    from app.models.ai import AiParseJob, AiParseStatus
    from app.models.user import User
    from app.services.ai_quote_parse import parse_supplier_quote

    factory = get_session_factory()
    async with factory() as session:
        job = await session.get(AiParseJob, _UUID(job_id))
        if job is None:
            log.warning(f"后台解析找不到任务 {job_id}")
            return
        user = await session.get(User, _UUID(user_id))
        try:
            cfg = await ai_client.load_ai_config(session)
            result = await parse_supplier_quote(
                session,
                extract=extract,
                cfg=cfg,
                supplier_hint=supplier_hint,
                project_hint=project_hint,
                user=user,
                force_ai=force_ai,
                allow_ai=allow_ai,
                ref_id=job_id,
            )
            job.result = result
            job.status = (
                str(AiParseStatus.READY) if result.get("items")
                else str(AiParseStatus.FAILED)
            )
            log.info(
                f"AI 解析完成（后台）任务 {job_id}：{len(result.get('items') or [])} 行"
            )
        except (ai_client.AiNotConfiguredError, ai_client.AiCallError) as exc:
            job.status = str(AiParseStatus.FAILED)
            job.result = {"warnings": [str(exc)], "items": []}
            log.warning(f"AI 解析失败（后台）任务 {job_id}：{exc}")
        except Exception as exc:  # noqa: BLE001
            job.status = str(AiParseStatus.FAILED)
            job.result = {
                "warnings": [f"解析异常（{type(exc).__name__}），请重试或改用手工录入"],
                "items": [],
            }
            log.warning(f"AI 解析异常（后台）任务 {job_id}：{type(exc).__name__}: {exc}")
        await session.commit()


@router.get("/parse-jobs", summary="解析任务历史")
async def list_parse_jobs(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
    limit: int = Query(20, ge=1, le=100),
) -> dict:
    from app.models.ai import AiParseJob

    rows = (
        await db.scalars(
            select(AiParseJob).order_by(AiParseJob.created_at.desc()).limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(j.id),
                "status": j.status,
                "filename": j.filename,
                "engine": (j.result or {}).get("engine"),
                "item_count": len((j.result or {}).get("items") or []),
                "commit_info": j.commit_info or {},
                "created_by_name": j.created_by_name,
                "created_at": j.created_at.isoformat() if j.created_at else None,
            }
            for j in rows
        ]
    }


@router.get("/parse-jobs/{job_id}", summary="解析任务详情（候选行 + 匹配 + 警告）")
async def get_parse_job(
    job_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
) -> dict:
    from app.models.ai import AiParseJob

    job = await db.get(AiParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="解析任务不存在")
    return {
        "id": str(job.id),
        "status": job.status,
        "filename": job.filename,
        "supplier_id": str(job.supplier_id) if job.supplier_id else None,
        "project_id": str(job.project_id) if job.project_id else None,
        "result": job.result or {},
        "commit_info": job.commit_info or {},
        "created_by_name": job.created_by_name,
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }


@router.post(
    "/parse-jobs/{job_id}/commit",
    status_code=status.HTTP_201_CREATED,
    summary="人工确认后落库（更新设备库进价 + #IM 批次留档）",
)
async def commit_parse_job(
    job_id: UUID,
    body: ParseCommitRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("device_library", "update"))],
) -> dict:
    """**只有走到这里才写业务表**，而且用的是请求体里确认过的行。"""
    from app.models.ai import AiParseJob, AiParseStatus
    from app.models.import_batch import ImportKind
    from app.services.import_batch import record_batch

    job = await db.get(AiParseJob, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="解析任务不存在")
    if job.status == str(AiParseStatus.COMMITTED):
        raise HTTPException(status_code=409, detail="这个任务已经确认落库过了（避免重复计账）")

    now = datetime.now(tz=UTC)
    updated = 0
    skipped: list[dict] = []
    touched: list[str] = []
    for row in body.rows:
        if row.device_id is None:
            skipped.append({
                "row": row.row, "name": row.name,
                "message": "未匹配到设备库（可先在设备库建好，或用「设备库导入」批量建）",
            })
            continue
        device = await db.get(Device, row.device_id)
        if device is None:
            skipped.append({"row": row.row, "name": row.name, "message": "设备不存在"})
            continue
        price = Decimal(str(row.unit_price))
        if price <= 0:
            skipped.append({"row": row.row, "name": row.name, "message": "单价格式不对（<=0）"})
            continue

        old_cost = Decimal(str(device.cost_price or 0))
        device.cost_price = price
        device.last_inquiry_price = price
        device.last_inquiry_at = now
        if body.supplier_id:
            device.supplier_id = body.supplier_id
        updated += 1
        touched.append(f"{device.code} 进价 {old_cost} → {price}")

    batch = await record_batch(
        db,
        kind=ImportKind.SUPPLIER_QUOTE,
        user=user,
        filename=job.filename,
        file_size=job.file_size,
        total_rows=len(body.rows),
        created=0,
        updated=updated,
        skipped=len(skipped),
        errors=skipped[:50],
        summary={
            "source": "ai_supplier_quote",
            "ai_job_id": str(job.id),
            "engine": (job.result or {}).get("engine"),
            "supplier_id": str(body.supplier_id) if body.supplier_id else None,
            "confirmed_by": user.username,
            "changes": touched[:50],
        },
    )
    job.status = str(AiParseStatus.COMMITTED)
    job.commit_info = {
        "batch_no": batch.batch_no,
        "updated": updated,
        "skipped": len(skipped),
        "confirmed_by": user.username,
        "confirmed_at": now.isoformat(),
    }
    await db.flush()
    log.warning(
        f"供应商报价确认落库 by {user.username}：{updated} 条进价更新，"
        f"跳过 {len(skipped)}，批次 {batch.batch_no}"
    )
    return {
        "batch_no": batch.batch_no,
        "updated": updated,
        "skipped": len(skipped),
        "skipped_detail": skipped[:50],
        "changes": touched[:50],
    }


# ---------------------------------------------------------------------------
# 对话（SSE 流式 + 工具调用，2026-10-04）
# ---------------------------------------------------------------------------

class ChatAttachment(BaseModel):
    kind: str = Field("parse_job", max_length=32)
    job_id: str | None = None
    filename: str | None = Field(None, max_length=255)
    item_count: int | None = None


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    conversation_id: UUID | None = None
    attachments: list[ChatAttachment] = []


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"


@router.post("/chat", summary="对话（SSE 流式：文本增量 + 工具执行过程）")
async def ai_chat(
    body: ChatRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
):
    """流式的两个坑都在这段代码里处理了：
      · `X-Accel-Buffering: no` —— 否则 nginx 会把 SSE 攒成一坨，用户看不到"边打边出"；
      · `finally` 里显式 commit —— 否则客户端中途断开时，半段对话还在事务里丢掉。
    """
    from app.models.ai import AiConversation
    from app.services.ai_chat import run_agent_turn

    cfg = await ai_client.load_ai_config(db)
    try:
        ai_client.require_ready(cfg)
    except ai_client.AiNotConfiguredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    if body.conversation_id:
        conv = await db.get(AiConversation, body.conversation_id)
        if conv is None or (conv.user_id is not None and conv.user_id != user.id):
            raise HTTPException(status_code=404, detail="会话不存在")
    else:
        conv = AiConversation(title="新对话", user_id=user.id, user_name=user.username)
        db.add(conv)
        await db.flush()

    attachments = [a.model_dump() for a in body.attachments]
    conv_id = str(conv.id)

    async def gen():
        yield _sse({"type": "conversation", "conversation_id": conv_id})
        try:
            async for ev in run_agent_turn(
                db, cfg=cfg, conversation=conv, user=user,
                user_text=body.message, attachments=attachments,
            ):
                yield _sse(ev)
        except Exception as exc:  # noqa: BLE001 - 流里出错只能以事件形式告知
            log.warning(f"对话流异常：{type(exc).__name__}: {exc}")
            yield _sse({"type": "error", "message": f"对话中断（{type(exc).__name__}）"})
        finally:
            try:
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                log.warning(f"对话落库失败：{type(exc).__name__}")

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/conversations", summary="我的会话列表")
async def list_conversations(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
    limit: int = Query(30, ge=1, le=200),
) -> dict:
    from app.models.ai import AiConversation

    rows = (
        await db.scalars(
            select(AiConversation)
            .where(AiConversation.user_id == user.id, AiConversation.archived.is_(False))
            .order_by(AiConversation.last_message_at.desc().nullslast())
            .limit(limit)
        )
    ).all()
    return {
        "items": [
            {
                "id": str(c.id),
                "title": c.title,
                "message_count": c.message_count,
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            }
            for c in rows
        ]
    }


@router.get("/conversations/{conversation_id}", summary="会话消息（回看历史）")
async def get_conversation(
    conversation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
) -> dict:
    from app.models.ai import AiConversation, AiMessage

    conv = await db.get(AiConversation, conversation_id)
    if conv is None or (conv.user_id is not None and conv.user_id != user.id):
        raise HTTPException(status_code=404, detail="会话不存在")
    rows = (
        await db.scalars(
            select(AiMessage)
            .where(AiMessage.conversation_id == conv.id)
            .order_by(AiMessage.created_at)
        )
    ).all()
    return {
        "id": str(conv.id),
        "title": conv.title,
        "messages": [
            {
                "id": str(m.id),
                "role": m.role,
                "content": m.content,
                "tool_calls": m.tool_calls or [],
                "tool_name": m.tool_name,
                "attachments": m.attachments or [],
                "meta": m.meta or {},
                "created_at": m.created_at.isoformat() if m.created_at else None,
            }
            for m in rows
        ],
    }


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话",
)
async def delete_conversation(
    conversation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "use"))],
) -> None:
    from app.models.ai import AiConversation

    conv = await db.get(AiConversation, conversation_id)
    if conv is None or (conv.user_id is not None and conv.user_id != user.id):
        raise HTTPException(status_code=404, detail="会话不存在")
    await db.delete(conv)
    await db.flush()


@router.get("/models", summary="拉取服务商可用模型列表（免得猜模型名）")
async def ai_models(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("ai", "configure"))],
) -> dict:
    """代理服务商的 `GET {base_url}/models`。

    为什么值得做：OpenAI 兼容网关的模型名五花八门（`deepseek-chat` / `minimax-m3` /
    `qwen-max`…），填错就是 503/404，用户根本不知道去哪查。多数网关实现了 /models，
    直接列出来让他选，比猜名字靠谱。
    """
    import httpx

    cfg = await ai_client.load_ai_config(db)
    if not (cfg.get("api_key") or "").strip():
        raise HTTPException(status_code=400, detail="还没有配置 API Key，无法拉取模型列表")
    base = (cfg.get("base_url") or "").rstrip("/")
    if not base:
        raise HTTPException(status_code=400, detail="还没有配置接口地址")

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{base}/models",
                headers={"Authorization": f"Bearer {cfg.get('api_key')}"},
            )
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"连不上服务商（{type(exc).__name__}）。检查接口地址与本机出网。",
        ) from exc

    if resp.status_code >= 400:
        # 统一走同一个解释器：否则同一类上游错误在不同入口说法不同，
        # 用户得到的解释取决于他点了哪个按钮（真实事故：真因是供应商码 11148
        # "工具调用与结果不配对"，却被讲成"模型名不对，去改模型名"）。
        from app.services.ai_client import _explain_http_error

        detail = _explain_http_error(resp.status_code, resp.text)
        raise HTTPException(
            status_code=502,
            detail=(
                "服务商没有返回模型列表。"
                "有些网关不实现 /models，这种情况请直接照服务商控制台填写模型名。\n"
                f"{detail}"
            ),
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="服务商返回的不是 JSON") from exc

    rows = body.get("data") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="服务商的响应里没有 data 列表")

    names = sorted(
        str(r.get("id")) for r in rows if isinstance(r, dict) and r.get("id")
    )
    return {"count": len(names), "models": names}
