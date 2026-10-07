"""删掉 AI 设置里的 max_tokens（2026-10-05）。

用户要求：**不限 token，单次最大 token 这个设置删掉**。
代码侧已经不再发送 `max_tokens`（由服务商用自己的上限）；这里把**已存进库**的
那个字段一并清掉 —— 否则设置页虽不显示，GET /ai/config 仍会吐出它，
给人"还能配"的错觉。
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_settings" not in set(inspector.get_table_names()):
        return
    row = bind.execute(
        sa.text("SELECT value FROM app_settings WHERE key = 'ai'")
    ).fetchone()
    if row is None:
        return
    value = row[0]
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict) or "max_tokens" not in value:
        return
    value.pop("max_tokens", None)
    bind.execute(
        sa.text("UPDATE app_settings SET value = CAST(:v AS jsonb) WHERE key = 'ai'"),
        {"v": json.dumps(value, ensure_ascii=False)},
    )


def downgrade() -> None:
    # 不回滚：把已经删掉的设置项塞回去没有意义（用户明确要求删掉）
    pass
