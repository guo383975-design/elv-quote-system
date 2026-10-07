"""Portable column types.

The app runs on PostgreSQL in production, but the test-suite uses in-memory
SQLite for speed. These aliases give us PG-native storage with a working
fallback everywhere else.
"""

from __future__ import annotations

from enum import Enum as PyEnum

from sqlalchemy import JSON, Uuid
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB

# UUID: native `uuid` on PostgreSQL, CHAR(32) elsewhere. as_uuid=True keeps
# Python-side values as `uuid.UUID` objects on both backends.
UUIDType = Uuid(as_uuid=True)

# JSON: JSONB on PostgreSQL, TEXT-backed JSON elsewhere.
JSONType = JSON().with_variant(JSONB(), "postgresql")


def sa_enum(enum_cls: type[PyEnum], name: str) -> SAEnum:
    """A SQLAlchemy Enum that persists member **values**, not member names.

    SQLAlchemy's default is to store ``.name`` (e.g. ``"ADMIN"``). Our
    migrations create the PostgreSQL types from the lowercase ``.value``
    (``"admin"``), and the API speaks those values too, so we force
    ``values_callable`` here rather than relying on version-specific
    StrEnum detection.
    """
    return SAEnum(
        enum_cls,
        name=name,
        values_callable=lambda cls: [str(e.value) for e in cls],
    )


def drop_none_for_not_null(model: type, data: dict) -> dict:
    """Drop explicit ``None`` values destined for NOT NULL columns.

    Pydantic DTOs commonly default optional numeric fields (``cost_price``,
    ``unit_price``, ...) to ``None``. Assigning ``None`` to a NOT NULL column
    defeats the column's Python-side ``default=0`` and produces an
    IntegrityError, so those keys must be omitted instead. Genuinely nullable
    columns keep ``None`` so callers can still clear them.
    """
    columns = model.__table__.columns
    return {
        key: value
        for key, value in data.items()
        if not (value is None and key in columns and not columns[key].nullable)
    }


__all__ = ["UUIDType", "JSONType", "sa_enum", "drop_none_for_not_null"]
