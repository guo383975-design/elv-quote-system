"""Async SQLAlchemy engine + session factory + FastAPI dependency."""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("db")

# Module-level singletons (initialised at startup, closed at shutdown)
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine() -> AsyncEngine:
    """Create the global async engine. Idempotent."""
    global _engine, _session_factory
    if _engine is not None:
        return _engine
    # 连接池按 worker 数分配：每个 worker 一份池子，
    # 加起来不能超过 PG 的 max_connections（默认 100）。
    # 单 worker 时 10+20=30 很宽裕；4 worker 时改成 5+10=15（合计 60）更稳妥。
    # 压测（2026-10-05）结论：瓶颈不在连接数（观察期活跃连接只有 1~2），
    # 而在单进程只能吃一个 CPU 核 —— 加 worker 才是扩吞吐的手段。
    _workers = max(1, int(os.environ.get("WEB_CONCURRENCY") or 1))
    _pool_size = int(os.environ.get("DB_POOL_SIZE") or max(3, 10 // _workers))
    _max_overflow = int(os.environ.get("DB_MAX_OVERFLOW") or max(5, 20 // _workers))
    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=_pool_size,
        max_overflow=_max_overflow,
        pool_pre_ping=True,
    )
    _session_factory = async_sessionmaker(
        bind=_engine,
        expire_on_commit=False,
        # Keep SQLAlchemy's default autoflush ON. Several code paths read back
        # rows they just added in the same request — e.g. bulk device import
        # calls `_next_code()` per row, so a pending row must be visible to the
        # next `SELECT max(code)` or two rows would collide on the same code.
        autoflush=True,
    )
    log.info("Async DB engine initialised")
    return _engine


async def dispose_engine() -> None:
    """Tear-down: close all pooled connections. Call on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        log.info("Async DB engine disposed")
    _engine = None
    _session_factory = None


def set_session_factory(factory: async_sessionmaker[AsyncSession] | None) -> None:
    """显式注入会话工厂（**给测试与后台任务用**）。

    生产环境由 `init_engine()` 设置；测试里用的是 conftest 自建的引擎，
    后台任务（如 AI 解析）需要拿到同一个工厂才能看到测试数据 ——
    否则它会去连一个从未初始化的引擎，直接报 "DB engine not initialised"。
    """
    global _session_factory
    _session_factory = factory


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory. Raises if engine not initialised yet."""
    if _session_factory is None:
        raise RuntimeError("DB engine not initialised — call init_engine() first")
    return _session_factory


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency: yields a session, rolls back on exception.

    Usage:
        @router.get("/")
        async def endpoint(db: AsyncSession = Depends(get_db)): ...
    """
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Manual context manager — for scripts/workers, not FastAPI routes."""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


__all__ = [
    "init_engine",
    "dispose_engine",
    "get_session_factory",
    "set_session_factory",
    "get_db",
    "session_scope",
]
