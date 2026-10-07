"""FastAPI application entry point."""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.api.v1 import api_router
from app.core.config import DEV_JWT_SECRET, settings
from app.core.labels import LABELS
from app.core.logging import get_logger, setup_logging
from app.core.request_context import client_ip_from_headers, set_request_context
from app.db.session import dispose_engine, init_engine

setup_logging()
log = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 先做配置安全检查：production 下密钥不安全就直接让启动失败，
    # 而不是带着一个公开密钥"看起来正常"地对外提供服务。
    settings.validate_for_runtime()
    log.info(f"启动 {settings.app_name} v{settings.app_version} ({settings.app_env})")
    if not settings.is_production and settings.jwt_secret == DEV_JWT_SECRET:
        log.warning(
            "正在使用公开的开发默认 JWT_SECRET —— 仅限本机开发；"
            "部署到服务器（APP_ENV=production）时服务会拒绝启动，"
            "请按 .env.example 生成随机密钥。"
        )
    init_engine()

    # 多 worker 提醒：权限/角色的缓存是**进程内**的。
    # 单进程时"保存即生效"；多 worker 下只刷新被请求到的那个 worker，
    # 靠下面的定期刷新做到最终一致（默认 10 秒）。
    _workers = int(os.environ.get("WEB_CONCURRENCY") or 0)
    if _workers > 1:
        log.warning(
            f"检测到 {_workers} 个 worker：权限/角色缓存为进程内缓存，"
            "改动会在定期刷新周期（默认 10 秒）内逐步生效。"
        )
    # 启动时加载"运行期配置"：自定义角色 → 权限矩阵覆盖。
    # ⚠ 顺序不能反：覆盖里可能引用自定义角色，角色清单不先就位，那个角色的权限会被丢弃。
    # 2026-10-05 起权限矩阵**也包含字段级**的 data_cost / data_profit 两行，
    # 所以启动只需这一处加载（独立的「数据可见性」设置已合并进来）。
    try:
        from app.db.session import get_session_factory
        from app.services.settings import load_settings, refresh_runtime_caches

        async with get_session_factory()() as _s:
            _all = await load_settings(_s)
        refresh_runtime_caches(_all)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            f"加载角色/权限配置失败，用代码里的默认矩阵：{type(exc).__name__}: {exc}"
        )
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    async def _cache_refresh_loop() -> None:
        """定期把库里的角色/权限设置刷进进程内缓存。

        为什么需要：这些缓存是进程内的 —— 单进程时保存即生效，
        多 worker 时其它 worker 要靠这个循环（默认 10 秒）才能跟上。
        代价是每 10 秒一次设置查询，可以忽略。
        """
        from app.db.session import get_session_factory
        from app.services.settings import load_settings, refresh_runtime_caches

        interval = float(os.environ.get("CACHE_REFRESH_SECONDS") or 10)
        while True:
            await asyncio.sleep(interval)
            try:
                async with get_session_factory()() as _s:
                    refresh_runtime_caches(await load_settings(_s))
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 刷新失败不能拖垮服务
                log.warning(f"定期刷新权限缓存失败：{type(exc).__name__}: {exc}")

    # 预热连接池：把 pool_size 条连接先开出来。
    # 实测（2026-10-05）：冷启动后第一波并发要为建连付 ~20ms × 15 ≈ 0.3s，
    # 表现成"第一个被打的接口特别慢"。这笔钱在启动时付掉，别让第一个用户等。
    try:
        from app.db.session import get_session_factory

        factory = get_session_factory()

        async def _warm_one() -> None:
            async with factory() as _s:
                await _s.execute(text("SELECT 1"))

        await asyncio.gather(*[_warm_one() for _ in range(6)])
        log.info("连接池预热完成（6 条连接）")
    except Exception as exc:  # noqa: BLE001 - 预热失败不影响启动
        log.warning(f"连接池预热失败（忽略）：{type(exc).__name__}: {exc}")

    refresh_task = asyncio.create_task(_cache_refresh_loop())
    try:
        yield
    finally:
        refresh_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await refresh_task
    await dispose_engine()
    log.info("应用已关闭")


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "智能化弱电报价系统后端 API。\n\n"
        "核心能力：设备库管理（含停产/替代闭环）、项目与子系统、"
        "**双行并列报价**（点位单价口径 + 定额计价口径）、"
        "人工费定额与浮动、Excel 双场景批量导入。"
    ),
    openapi_url="/api/openapi.json",
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    lifespan=lifespan,
)

@app.middleware("http")
async def _request_context(request, call_next):  # type: ignore[no-untyped-def]
    """把客户端 IP / User-Agent 存进上下文，供留痕使用（H-10）。

    必须读转发头：前面是 nginx，不加这个所有人都会被记成 127.0.0.1。
    """
    set_request_context(
        client_ip_from_headers(
            request.headers.get("x-forwarded-for"),
            request.headers.get("x-real-ip"),
            request.client.host if request.client else None,
        ),
        request.headers.get("user-agent"),
    )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(api_router, prefix="/api/v1")


# ---------------------------------------------------------------------------
# exception handlers — uniform error envelope
# ---------------------------------------------------------------------------

@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    # exc.errors() 里带 pydantic v2 的 "input" 原始值。上传类接口收到错的
    # Content-Type 时 input 是 bytes/UploadFile，直接塞进 JSONResponse 会在
    # 序列化阶段再抛一次，客户端看到的是 500 而不是 422。
    # FastAPI 自己的 handler 就是先 jsonable_encoder 再返回。
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=jsonable_encoder(
            {
                "error": "validation_error",
                "detail": exc.errors(),
                "path": str(request.url.path),
            }
        ),
    )


@app.exception_handler(IntegrityError)
async def integrity_handler(request: Request, exc: IntegrityError) -> JSONResponse:
    log.error(f"数据库约束冲突: {exc}")
    return JSONResponse(
        status_code=status.HTTP_409_CONFLICT,
        content={
            "error": "integrity_error",
            "detail": "数据约束冲突（可能是唯一键重复或外键引用无效）",
        },
    )


@app.exception_handler(Exception)
async def unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
    log.exception(f"未处理异常 {request.method} {request.url.path}: {exc}")
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_error", "detail": "服务器内部错误，请查看日志"},
    )


# ---------------------------------------------------------------------------
# health / meta
# ---------------------------------------------------------------------------

@app.get("/api/health", tags=["系统"], summary="健康检查")
async def health() -> dict:
    return {"status": "ok", "app": settings.app_name, "version": settings.app_version}


@app.get("/api/v1/meta/enums", tags=["系统"], summary="所有枚举字典（前端下拉用）")
async def enums() -> dict:
    from app.core.permissions import Action
    from app.core.roles import all_roles
    from app.models.device import DeviceCategory, DeviceStatus
    from app.models.labor import LaborClass, QuotaCategory
    from app.models.project import ProjectStatus, ProjectType
    from app.models.quote import AuxiliaryBaseType, FeeBaseType, QuoteStatus
    from app.models.quote_item import ItemSource
    from app.models.subsystem import SubsystemType

    def vals(enum_cls) -> list[dict]:
        return [{"value": str(m), "label": getattr(m, "label", str(m))} for m in enum_cls]

    return {
        "project_type": vals(ProjectType),
        "project_status": vals(ProjectStatus),
        "subsystem_type": vals(SubsystemType),
        "device_category": vals(DeviceCategory),
        "device_status": vals(DeviceStatus),
        "quote_status": vals(QuoteStatus),
        "fee_base": vals(FeeBaseType),
        "auxiliary_base": vals(AuxiliaryBaseType),
        "item_source": vals(ItemSource),
        "labor_class": vals(LaborClass),
        "quota_category": vals(QuotaCategory),
        # 角色用 core/roles 的清单（内置 + **自定义**）——
        # 这样建完新角色就能在用户管理的下拉里直接选到它。
        "role": [{"value": r["key"], "label": r["name"]} for r in all_roles()],
        "action": vals(Action),
    }


@app.get("/api/v1/meta/labels", tags=["系统"], summary="中文标签映射")
async def labels() -> dict:
    # 映射本身搬到了 app/core/labels.py 的唯一来源：
    # 导出 Excel/PDF 也要用同一份中文，不能再各存一套。
    return LABELS


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        reload=settings.app_env == "development",
    )

