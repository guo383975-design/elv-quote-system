"""把仍是旧默认文本的报价条款升级为变量版（2026-10-05）。

背景：`app_settings` 存过值就盖住代码默认值 —— 用户重置后保存过一次设置，
旧的字面文本被持久化，后来把默认值改成变量版对他们不生效
（上机验证抓到：改了有效期字段，条款里的数字不跟着变）。

分寸：只升级**与旧默认逐字相同**的条款；用户自定义过的保留原文。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "app_settings" not in set(inspector.get_table_names()):
        return

    from app.services.quote_terms_migrate import upgrade_legacy_default_clauses

    row = bind.execute(
        sa.text("SELECT value FROM app_settings WHERE key = 'quote'")
    ).fetchone()
    if row is None:
        return  # 还没存过设置：走代码默认值（已是变量版），无需处理

    value = row[0]
    if isinstance(value, str):
        import json

        value = json.loads(value)
    if not isinstance(value, dict):
        return

    group, changed = upgrade_legacy_default_clauses(value)
    if not changed:
        return
    bind.execute(
        sa.text(
            "UPDATE app_settings SET value = CAST(:v AS jsonb) WHERE key = 'quote'"
        ),
        {"v": __import__("json").dumps(group, ensure_ascii=False)},
    )


def downgrade() -> None:
    # 数据升级不回滚：把变量版退回写死数字的旧文本没有意义，
    # 而且会覆盖用户在这之后做的修改。
    pass
