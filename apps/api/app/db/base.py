"""SQLAlchemy declarative base + common mixins."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import DateTime
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import UUIDType


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Single declarative base for the whole app."""


class UUIDPKMixin:
    """UUID primary key.

    The Python-side `uuid4` default keeps SQLite tests working; PostgreSQL
    additionally has `DEFAULT gen_random_uuid()` from the migration for
    bulk inserts that bypass the ORM.
    """

    id: Mapped[UUID] = mapped_column(
        UUIDType,
        primary_key=True,
        default=uuid4,
    )


class TimestampMixin:
    """Created/updated timestamps."""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=_utcnow,
        onupdate=_utcnow,
        nullable=False,
    )


__all__ = ["Base", "UUIDPKMixin", "TimestampMixin", "uuid4"]
