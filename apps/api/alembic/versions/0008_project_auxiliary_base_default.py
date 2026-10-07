"""修正 projects.auxiliary_base 的默认值：device_install → device（2026-10-03）。

## 为什么

`0001_initial.py` 里两张表的 server_default 就不一致：

| 表 | 列 | server_default |
|---|---|---|
| `quotes` | `auxiliary_base` | `device` |
| `projects` | `auxiliary_base` | `device_install` |

而且**已确认口径**（README「已确认的业务口径」第 3 项，2026-09-29 定）是
**按设备金额** = `device`。报价单那边是对的，项目那边是错的。

以前看不出来：`projects` 的这 8 个费率列既没有 API 入口、也没有界面入口（审计 H-12），
值永远是"模型默认"，不影响任何计算。

但只要开始**建报价单时抄项目费率**（H-12），这个不一致就会变成事故：
每张新建的报价单都会拿到 `device_install`，辅材费基数从"设备费"变成"设备费+安装费"，
**金额被静默改变**。

## 为什么可以放心 UPDATE 历史行

该列此前**没有任何写入路径**（无 schema 字段、无界面控件），所以每一行的
`device_install` 都是模型默认值，**不是谁的选择**。把默认值纠正过来，不是改用户意图。

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    cols = {c["name"] for c in inspector.get_columns("projects")}
    if "auxiliary_base" not in cols:
        return

    # 1) 以后新建的项目用正确默认值
    op.execute("ALTER TABLE projects ALTER COLUMN auxiliary_base SET DEFAULT 'device'")
    # 2) 纠正历史行（都是默认值，不是用户选择）
    result = bind.execute(
        sa.text(
            "UPDATE projects SET auxiliary_base = 'device' "
            "WHERE auxiliary_base = 'device_install'"
        )
    )
    print(f"    projects.auxiliary_base: 纠正 {result.rowcount} 行 device_install → device")


def downgrade() -> None:
    op.execute("ALTER TABLE projects ALTER COLUMN auxiliary_base SET DEFAULT 'device_install'")
    # 不把数据改回去：无法区分"默认值"与"确实是 device"的行，
    # 回滚默认值即可，数据保持与已确认口径一致。
