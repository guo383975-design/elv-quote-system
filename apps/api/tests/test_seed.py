"""种子脚本的回归测试。

`scripts/seed.py` 是部署链路上的一步（`deploy.sh` 第 5 步），而且是在**已有数据的
生产/测试库**上跑，所以"幂等"必须真的成立 —— 以前没有任何测试覆盖它，于是：

1. 供应商按 code 查不到就插入 → 同名旧行（旧编号方案）会让 `ix_suppliers_name`
   唯一键冲突 → 整个 seed 事务回滚 → 部署卡在第 5 步（2026-09-30 在 192.168.3.102
   上实测复现）。
2. 演示用户是"库里只要有一个人就全部跳过" → 任何一次手工建号都会让 5 个演示账号
   永远建不出来。
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.supplier import Supplier
from app.models.user import User
from scripts.seed import SUPPLIERS, seed_suppliers, seed_users


@pytest.mark.asyncio
async def test_seed_suppliers_reuses_row_with_same_name_but_other_code(
    db: AsyncSession,
) -> None:
    """同名不同 code 的旧行必须被复用，而不是插入新行撞唯一键。

    复现路径：库里已有一家「宁波XX安防科技有限公司」但编号是旧方案的
    `SUP-56F42D7A`；种子期望 `SUP-001`。旧实现按 code 查不到就插入 → 唯一键冲突。
    """
    code_seed, name_seed = SUPPLIERS[0]
    old = Supplier(code="SUP-OLDHEX", name=name_seed, is_active=True)
    db.add(old)
    await db.commit()
    await db.refresh(old)

    out = await seed_suppliers(db)

    # 不抛异常，且返回的正是那条既有行（不是新建的另一条）
    assert out[name_seed].id == old.id
    # 同名的没被重复插入：总共 4 家（复用 1 + 新建 3）
    total = await db.scalar(select(func.count()).select_from(Supplier))
    assert total == len(SUPPLIERS)
    codes = set((await db.scalars(select(Supplier.code))).all())
    assert "SUP-OLDHEX" in codes, "既有行的编号不能被擅自改掉（可能已被外键引用）"

    # 幂等：再跑一次仍然不新增
    await seed_suppliers(db)
    total = await db.scalar(select(func.count()).select_from(Supplier))
    assert total == len(SUPPLIERS)


@pytest.mark.asyncio
async def test_seed_users_fills_only_missing_usernames(db: AsyncSession) -> None:
    """已经有一个用户时，缺的演示账号仍要被补齐（旧实现会整体跳过）。"""
    db.add(User(username="someone", email="someone@x.com",
                hashed_password="x", is_superuser=False))
    await db.commit()

    created = await seed_users(db, allow_create=True)
    assert created == 7
    names = set((await db.scalars(select(User.username))).all())
    assert {"admin", "purchaser01", "sales01", "est01", "manager01", "fin01", "view01"} <= names
    assert "someone" in names, "既有用户不能被覆盖或删除"

    # 再跑一次：全部已存在 → 0
    assert await seed_users(db, allow_create=True) == 0


@pytest.mark.asyncio
async def test_seed_users_respects_allow_create_false(db: AsyncSession) -> None:
    """production 下默认不创建演示账号（它们是已知口令）。"""
    created = await seed_users(db, allow_create=False)
    assert created == 0
    total = await db.scalar(select(func.count()).select_from(User))
    assert total == 0
