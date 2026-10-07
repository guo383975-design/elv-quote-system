"""把 visibility 设置折进权限矩阵（2026-10-05）。

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# 与 core/permissions.PERMISSION_MATRIX 里 data_cost / data_profit 的默认一致
DEFAULT_COST = {"admin", "finance", "purchaser", "manager"}
DEFAULT_PROFIT = {"admin", "finance", "manager"}


def _load(bind, key: str):
    row = bind.execute(
        sa.text("SELECT value FROM app_settings WHERE key = :k"), {"k": key}
    ).fetchone()
    if row is None or row[0] is None:
        return None
    value = row[0]
    if isinstance(value, str):
        value = json.loads(value)
    return value if isinstance(value, dict) else None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_settings" not in set(inspector.get_table_names()):
        return

    vis = _load(bind, "visibility")
    if vis is None:
        return  # 没配过（或已迁过）

    cost = {str(r) for r in (vis.get("cost_roles") or [])} or set(DEFAULT_COST)
    profit = {str(r) for r in (vis.get("profit_roles") or [])} or set(DEFAULT_PROFIT)

    perms = _load(bind, "permissions") or {}
    overrides = dict(perms.get("overrides") or {})

    def mark(role: str, row: str, allow: bool) -> None:
        per_role = dict(overrides.get(role) or {})
        per_role[row] = ["read"] if allow else []
        overrides[role] = per_role

    for role in cost | DEFAULT_COST:
        mark(role, "data_cost", role in cost)
    for role in profit | DEFAULT_PROFIT:
        mark(role, "data_profit", role in profit)

    perms["overrides"] = overrides
    bind.execute(
        sa.text("UPDATE app_settings SET value = CAST(:v AS jsonb) WHERE key = 'permissions'"),
        {"v": json.dumps(perms, ensure_ascii=False)},
    )
    bind.execute(sa.text("DELETE FROM app_settings WHERE key = 'visibility'"))


def downgrade() -> None:
    # 不回滚：合并之后矩阵才是唯一事实源，退回独立组会立刻出现两份口径。
    pass
