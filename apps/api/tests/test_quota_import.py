"""2018 版第五册真值定额库：数据集校验、导入幂等性、占位值退役、接口可检索。

背景：843 条真值子目从广联达浙江定额库安装包解析得到（编号/名称/章节），
人工消耗量与基价尚未回填。本文件守住三件事：
  1. 数据集本身干净（编号唯一、章节都在枚举里）；
  2. 导入可重复执行，且**绝不覆盖**造价员回填的工日/基价；
  3. 导入后接口真的能检索到（否则"写入系统"没有意义）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.labor import QuotaCategory, QuotaItem
from scripts.import_quota_zj2018 import (
    IMPORT_NOTE,
    UNIT_PLACEHOLDER,
    import_quota_items,
    is_placeholder_note,
    load_rows,
    retire_placeholders,
)

REAL_COUNT = 843
SEED_PLACEHOLDER_CODE = "5-6-001"


def test_chapter_enum_matches_2018_truth() -> None:
    """章节枚举必须与定额书真值一致 —— 这是真值子目能落位的前提。

    回归点：以前 5-1 是「综合布线」、5-7 是「管槽」，且缺 5-2/5-4/5-8，
    导致 843 条真值子目（如 5-2-13 双绞线缆管内穿放）根本存不进去。
    """
    values = {c.value for c in QuotaCategory}
    assert {"5-1", "5-2", "5-3", "5-4", "5-5", "5-6", "5-7", "5-8", "other"} <= values
    assert QuotaCategory.CABLING.value == "5-2", "综合布线在 5-2，不是 5-1"
    assert QuotaCategory.NETWORK.value == "5-1"
    assert QuotaCategory.SPD.value == "5-7", "5-7 是电涌保护，不是管槽"


def test_dataset_is_complete_and_clean() -> None:
    """数据集：843 条、编号唯一、章节合法、抽样名称与定额书一致。"""
    rows = load_rows()
    assert len(rows) == REAL_COUNT
    assert len({r["code"] for r in rows}) == REAL_COUNT
    assert all(r["code"].startswith("5-") for r in rows)
    assert all(r["category"].startswith("5-") for r in rows)
    assert min(int(r["seq"]) for r in rows) == 5925, "第五册在通用安装索引里从第 5925 条开始"

    by_code = {r["code"]: r for r in rows}
    assert "双绞线" in by_code["5-2-13"]["name"]
    assert "入侵探测器" in by_code["5-6-1"]["name"]
    assert by_code["5-7-1"]["category"] == "5-7"


def test_dataset_rejects_bad_rows(tmp_path) -> None:
    """章节非法 / 编号重复 / 名称缺失必须在入库前就报错，而不是写进去再发现。"""
    import json

    def dump(items) -> object:
        p = tmp_path / "bad.json"
        p.write_text(json.dumps({"items": items}, ensure_ascii=False), encoding="utf-8")
        return p

    with pytest.raises(ValueError, match="不在 QuotaCategory"):
        load_rows(dump([{"code": "5-2-1", "name": "x", "category": "9-9"}]))
    with pytest.raises(ValueError, match="编号重复"):
        load_rows(
            dump(
                [
                    {"code": "5-2-1", "name": "x", "category": "5-2"},
                    {"code": "5-2-1", "name": "y", "category": "5-2"},
                ]
            )
        )
    with pytest.raises(ValueError, match="名称为空"):
        load_rows(dump([{"code": "5-2-1", "name": "", "category": "5-2"}]))
    with pytest.raises(ValueError, match="没有 items"):
        load_rows(dump([]))


@pytest.mark.asyncio
async def test_import_is_idempotent(db: AsyncSession) -> None:
    """首次全量新增，重复执行零新增、零改动（幂等）。"""
    rows = load_rows()

    first = await import_quota_items(db, rows)
    assert first["inserted"] == REAL_COUNT
    assert first["updated"] == 0

    total = await db.scalar(select(func.count()).select_from(QuotaItem))
    assert total == REAL_COUNT

    second = await import_quota_items(db, rows)
    assert second["inserted"] == 0
    assert second["updated"] == 0
    assert second["unchanged"] == REAL_COUNT

    sample = await db.scalar(select(QuotaItem).where(QuotaItem.code == "5-2-13"))
    assert sample is not None
    assert sample.unit == UNIT_PLACEHOLDER, "单位未取到时不许让默认值 100m 冒充"
    assert sample.labor_a_day == Decimal("0.000")
    assert sample.base_price == Decimal("0.00")
    assert IMPORT_NOTE in (sample.notes or "")


@pytest.mark.asyncio
async def test_reimport_never_clobbers_backfilled_values(db: AsyncSession) -> None:
    """造价员回填的工日/基价/系数是成果，重跑导入脚本不许覆盖。"""
    rows = load_rows()
    await import_quota_items(db, rows)

    target = await db.scalar(select(QuotaItem).where(QuotaItem.code == "5-2-13"))
    assert target is not None
    target.labor_a_day = Decimal("1.234")
    target.labor_b_day = Decimal("2.345")
    target.labor_c_day = Decimal("0.567")
    target.base_price = Decimal("88.88")
    target.shielded_coef = Decimal("1.35")
    target.unit = "100m"
    await db.flush()

    # 同时改一个名称，模拟"数据集更新"：名称该更新，工日不该被动
    idx = next(i for i, r in enumerate(rows) if r["code"] == "5-2-13")
    rows[idx] = {**rows[idx], "name": rows[idx]["name"] + "（校订）"}
    stats = await import_quota_items(db, rows)
    assert stats["updated"] == 1

    await db.refresh(target)
    assert target.labor_a_day == Decimal("1.234")
    assert target.labor_b_day == Decimal("2.345")
    assert target.labor_c_day == Decimal("0.567")
    assert target.base_price == Decimal("88.88")
    assert target.shielded_coef == Decimal("1.35")
    assert target.unit == "100m", "回填的单位也不许被占位符改回去"
    assert target.name.endswith("（校订）")


@pytest.mark.asyncio
async def test_retire_placeholders_hides_seed_rows_but_keeps_them(db: AsyncSession) -> None:
    """seed 的占位示意值退役而非删除 —— 演示报价单还引用着它们的编号。"""
    db.add(
        QuotaItem(
            code=SEED_PLACEHOLDER_CODE,
            category=QuotaCategory.SECURITY,
            name="安装摄像机 枪型/半球（已含调试）",
            unit="台",
            labor_a_day=Decimal("0.35"),
            labor_b_day=Decimal("1.40"),
            labor_c_day=Decimal("0.65"),
            base_price=Decimal("112.60"),
            notes="人工消耗量为结构示例，以纸质定额书为准",
        )
    )
    await db.flush()
    assert is_placeholder_note("人工消耗量为结构示例，以纸质定额书为准") is True
    assert is_placeholder_note(IMPORT_NOTE) is False

    await import_quota_items(db, load_rows())
    retired = await retire_placeholders(db)
    assert retired == [SEED_PLACEHOLDER_CODE]

    row = await db.scalar(select(QuotaItem).where(QuotaItem.code == SEED_PLACEHOLDER_CODE))
    assert row is not None, "退役不能删行：演示单备注仍按编号查它"
    assert row.is_active is False
    assert "已退役" in (row.notes or "")
    assert row.labor_a_day == Decimal("0.35"), "退役不是清数据"

    # 再跑一次不应重复退役（只处理 is_active=True 的行）
    assert await retire_placeholders(db) == []


@pytest.mark.asyncio
async def test_imported_library_is_searchable_via_api(
    db: AsyncSession, client, admin_headers
) -> None:
    """"写进系统"的验收点：接口能列出、能按章节筛、能按关键字检索。"""
    await import_quota_items(db, load_rows())

    r = await client.get("/api/v1/quota-items", headers=admin_headers)
    assert r.status_code == 200
    assert len(r.json()) == REAL_COUNT

    r = await client.get(
        "/api/v1/quota-items", headers=admin_headers, params={"category": "5-2"}
    )
    body = r.json()
    assert body, "5-2 综合布线应当有子目"
    assert {row["category"] for row in body} == {"5-2"}
    assert {row["code"] for row in body} >= {"5-2-13"}

    r = await client.get(
        "/api/v1/quota-items", headers=admin_headers, params={"keyword": "双绞线"}
    )
    codes = {row["code"] for row in r.json()}
    assert "5-2-13" in codes

    r = await client.get("/api/v1/quota-items/5-2-13", headers=admin_headers)
    assert r.status_code == 200
    assert r.json()["unit"] == UNIT_PLACEHOLDER
