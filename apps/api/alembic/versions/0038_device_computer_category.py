"""device_category 枚举新增 computer（电脑与办公设备）+ 建对应的一级节点。

Revision ID: 0038
Revises: 0037

背景（2026-10-07）：中天电脑那本分销报价册带来 1109 台电脑/办公设备
（CPU / 主板 / 显卡 / 内存 / 显示器 / 机箱 / 电源 / 键鼠 …）。现有 12 个一级
分类全是弱电专业，没有一格装得下，因此新增第 13 个一级分类「电脑与办公设备」。

⚠ 这次改动**必须四处一起**，少任何一处都会出问题：

  1. `app/models/device.py`    DeviceCategory 加 COMPUTER = "computer"
  2. `app/core/device_tree.py` CODE_PREFIX_BY_CATEGORY 加 "computer": "T"
  3. `app/core/labels.py`      device_category 加 "computer": "电脑与办公设备"
                               ← 本迁移建根节点时用 label_of() 取名字，漏了会建成无名节点
  4. 本迁移                    ALTER TYPE + 建根节点

为什么必须有这一条迁移、不能只靠界面：
  · `POST /devices/categories` 建一级分类时校验 legacy_enum ∈ DeviceCategory
    （api/v1/devices.py 的 `_validate_legacy_enum`），枚举里没有的值一律 422；
  · 导入侧的 `resolve_category()` 认不出一级分类时**不报错**，而是把设备落成
    `DeviceCategory.ACCESSORY` 且 `category_id = NULL`（也就是全进「辅材」），
    所以漏建节点的后果是静默的、事后要一条条挪回来。

PG 的限制（本迁移为什么要用 autocommit_block）：
  `ALTER TYPE ... ADD VALUE` 在 PG12+ 可以写在事务里，但**同一个事务中不能使用
  这个新值**（会报 `unsafe use of new value "computer" of enum type`）。
  所以必须先把这个 ALTER 单独提交掉，后面的 INSERT 才用得上它。
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None

ROOT_ENUM = "computer"


def upgrade() -> None:
    # ----- 1) 枚举加值：必须在独立事务里提交 -----
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE device_category ADD VALUE IF NOT EXISTS '{ROOT_ENUM}'")

    bind = op.get_bind()

    # ----- 2) 一级节点（幂等：已经有这个 legacy_enum 的根就跳过）-----
    exists = bind.execute(
        sa.text("select 1 from device_categories where legacy_enum = :e"),
        {"e": ROOT_ENUM},
    ).first()
    if exists:
        return

    # 名字必须走 labels，与 0004 建那 12 个根时同一口径
    from app.core.device_tree import CODE_PREFIX_BY_CATEGORY
    from app.core.labels import label_of

    name = label_of("device_category", ROOT_ENUM, default="电脑与办公设备")
    code_prefix = CODE_PREFIX_BY_CATEGORY.get(ROOT_ENUM)

    # 排到最后：0004 给那 12 个根分配的 sort_order 是 0..11
    sort_order = bind.execute(
        sa.text(
            "select coalesce(max(sort_order), -1) + 1 from device_categories "
            "where parent_id is null"
        )
    ).scalar_one()

    bind.execute(
        sa.text(
            "insert into device_categories "
            "(id, parent_id, code, name, sort_order, is_active, legacy_enum, "
            " code_prefix, notes, created_at, updated_at) "
            "values (gen_random_uuid(), null, :code, :name, :sort_order, true, "
            "        :legacy_enum, :code_prefix, null, now(), now())"
        ),
        {
            "code": ROOT_ENUM,
            "name": name,
            "sort_order": sort_order,
            "legacy_enum": ROOT_ENUM,
            "code_prefix": code_prefix,
        },
    )

    # 二级节点不在这里建：与 0004 的口径一致（二级是业务数据，由使用方维护），
    # 这次会由「设备库导入」按分类路径自动建出来（imports.py 的
    # _ensure_category_path 按 父+名 幂等，重复导入不会长出重复节点）。


def downgrade() -> None:
    """只撤根节点。

    PG 没有 `ALTER TYPE ... DROP VALUE`，所以 computer 这个枚举值会留在
    device_category 类型里。它无害：没有根节点时不会有设备用到它，
    重新升级也只会把它再挂上。
    """
    bind = op.get_bind()
    used = bind.execute(
        sa.text(
            "select count(*) from devices d "
            "join device_categories c on d.category_id = c.id "
            "where c.legacy_enum = :e"
        ),
        {"e": ROOT_ENUM},
    ).scalar_one()
    if used:
        raise RuntimeError(
            f"还有 {used} 台设备挂在「电脑与办公设备」下，先挪到别的分类或删掉，再回滚这条迁移"
        )
    bind.execute(
        sa.text("delete from device_categories where legacy_enum = :e"),
        {"e": ROOT_ENUM},
    )
