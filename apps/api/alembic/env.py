"""Alembic env: reads `DATABASE_URL_SYNC` from app settings."""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Import all models so their tables register with Base.metadata
from app import models  # noqa: F401
from app.core.config import settings
from app.db.base import Base

config = context.config

# Override URL with our env-driven sync URL
config.set_main_option("sqlalchemy.url", settings.database_url_sync)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL to stdout — used by `alembic upgrade --sql`."""
    context.configure(
        url=settings.database_url_sync,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations against a live database connection."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # server_default **不比对**（2026-10-04，M-1）：
            # 本项目的分工是"DB 级默认写在迁移里、模型只写 Python 端 default"，
            # 开着比对会让几十个列天天报"要删掉 server_default"的假差异，
            # 把真漂移（列类型不一致）淹没掉。类型比对保持开启。
            compare_server_default=False,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
