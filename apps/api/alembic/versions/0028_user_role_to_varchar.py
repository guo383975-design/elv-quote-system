"""users.role: 枚举 → varchar(32)，支持自定义角色（2026-10-05）。

背景：PG 原生枚举不能删成员、不能改名，做"可增删的自定义角色"必须换类型。
改完由应用层按 core/roles.py 的清单校验。
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return  # SQLite 里本来就是 VARCHAR，无需处理

    # 直接问 PG：这一列现在到底是什么类型。
    # 不用 SQLAlchemy 的类型 repr 做字符串匹配 —— 试过，会在真库上误判成
    # "已经是 varchar" 而跳过 ALTER（临时库验证时就是这么暴露的）。
    row = bind.execute(
        sa.text(
            "SELECT format_type(a.atttypid, a.atttypmod) "
            "FROM pg_attribute a "
            "WHERE a.attrelid = 'users'::regclass AND a.attname = 'role' "
            "AND NOT a.attisdropped"
        )
    ).fetchone()
    current = str(row[0]) if row and row[0] else ""
    if not current:
        return  # 没有这张表/这一列（跑在很老的结构上）
    if current.startswith("character varying") or current == "text":
        return  # 幂等：已经是 varchar

    op.execute(
        "ALTER TABLE users ALTER COLUMN role TYPE varchar(32) USING role::text"
    )
    op.execute("ALTER TABLE users ALTER COLUMN role SET DEFAULT 'sales'")
    # 清掉不再使用的枚举类型。用 DO 块容错：万一还有别的地方引用它，
    # 宁可留着一个没人用的类型，也不要让整条部署因为一句 DROP 而失败。
    op.execute(
        """
        DO $$
        BEGIN
            DROP TYPE IF EXISTS user_role;
        EXCEPTION WHEN dependent_objects_still_exist THEN
            RAISE NOTICE 'user_role 仍被引用，先留着';
        END $$;
        """
    )


def downgrade() -> None:
    # 不回滚：自定义角色的 key 不是枚举成员，回滚会把它们变成非法值。
    pass
