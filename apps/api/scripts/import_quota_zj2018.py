"""把《浙江省通用安装工程预算定额（2018版）》第五册（建筑智能化工程）真值子目
导入 `quota_items`，让系统能检索、能"套定额"。

数据来源
    `scripts/data/quota_zj2018_vol5.json`（843 条）
    ← 广联达浙江定额库安装包 5.2.30.348
    → `浙江省通用安装工程预算定额(2018).g7z` → `数据/子目索引.Index`（GBK 明文）
    解析过程与脚本留档见仓库 `定额库提取/` 目录。

⚠ 本脚本只导入 **编号 / 名称 / 章节**，不导入人工消耗量与基价
    （那部分在广联达私有二进制格式里，需用正版计价软件导出或按纸质定额书回填）。
    因此导入后的子目：
      - 界面可检索、可套用（编号真值：5-2-13 这种，无零填充）；
      - `labor_a/b/c` 全为 0，重算时定额口径不会把它们当成"算出来的 0 元"，
        而是跳过并给出 warning / 退化为安装费口径（见 quote_compute 的
        未回填保护，2026-10-06 加）。

用法
    python -m scripts.import_quota_zj2018                    # 导入 + 退役占位示意值
    python -m scripts.import_quota_zj2018 --keep-placeholders # 保留占位行可选
    python -m scripts.import_quota_zj2018 --dry-run           # 只看统计，不写库
    python -m scripts.import_quota_zj2018 --file 其它数据集.json

幂等性
    按 `code` upsert：重复执行只更新名称/章节，**绝不覆盖已回填的工日、基价与
    工艺系数**；已退役的占位行不会被重新激活（它们的编号与真值编号不重叠）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.logging import get_logger, setup_logging
from app.db.session import dispose_engine, init_engine, session_scope
from app.models.labor import QuotaCategory, QuotaItem

setup_logging()
log = get_logger("import-quota")

DATA_FILE = Path(__file__).resolve().parent / "data" / "quota_zj2018_vol5.json"

# 单位列是 NOT NULL：真值单位还没拿到，先写一个显式的占位，
# 而不是让列默认值 "100m" 冒充一个具体单位（那会让导出的定额表看起来是对的）。
UNIT_PLACEHOLDER = "待补"

IMPORT_NOTE = (
    "编号/名称来源：广联达浙江定额库（2018版第五册 建筑智能化工程，"
    "解析自 子目索引.Index）；人工消耗量与基价待造价员按纸质定额书或"
    "计价软件导出值回填"
)

# 占位示意值（seed.py 那 35 条）的识别标记。用备注特征而不是硬编码编号，
# 这样换环境、手工录入的行都不会被误伤。
PLACEHOLDER_MARKERS = ("结构示例", "示意值", "占位")
RETIRE_SUFFIX = "｜已退役：本行为结构示意值，编号与名称已由 2018 版第五册真值库取代"


def load_rows(path: Path = DATA_FILE) -> list[dict[str, Any]]:
    """读数据集并做**入库前校验**（纯函数，可单测）。"""
    payload = json.loads(path.read_text(encoding="utf-8"))
    items = payload.get("items") or []
    if not items:
        raise ValueError(f"数据文件没有 items：{path}")

    valid = {str(c.value) for c in QuotaCategory}
    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for raw in items:
        code = str(raw.get("code") or "").strip()
        name = str(raw.get("name") or "").strip()
        category = str(raw.get("category") or "").strip()
        if not code or not name:
            raise ValueError(f"编号或名称为空：{raw}")
        if code in seen:
            raise ValueError(f"编号重复：{code}")
        if category not in valid:
            raise ValueError(f"章节 {category} 不在 QuotaCategory 枚举内：{code}")
        seen.add(code)
        rows.append(
            {"code": code, "name": name, "category": category, "seq": raw.get("seq")}
        )
    return rows


def is_placeholder_note(notes: str | None) -> bool:
    text = notes or ""
    return any(marker in text for marker in PLACEHOLDER_MARKERS)


def _category_value(item: QuotaItem) -> str:
    return str(getattr(item.category, "value", item.category))


async def import_quota_items(
    session: AsyncSession,
    rows: list[dict[str, Any]],
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """按 code upsert。返回 {inserted, updated, unchanged, reactivated}。"""
    existing = {q.code: q for q in (await session.scalars(select(QuotaItem))).all()}
    stats = {"inserted": 0, "updated": 0, "unchanged": 0, "reactivated": 0}

    for row in rows:
        item = existing.get(row["code"])
        if item is None:
            stats["inserted"] += 1
            if dry_run:
                continue
            session.add(
                QuotaItem(
                    code=row["code"],
                    category=QuotaCategory(row["category"]),
                    name=row["name"],
                    unit=UNIT_PLACEHOLDER,
                    labor_a_day=Decimal("0"),
                    labor_b_day=Decimal("0"),
                    labor_c_day=Decimal("0"),
                    base_price=Decimal("0"),
                    notes=IMPORT_NOTE,
                )
            )
            continue

        changed = (
            item.name != row["name"]
            or _category_value(item) != row["category"]
            or not item.is_active
        )
        if not changed:
            stats["unchanged"] += 1
            continue

        stats["updated"] += 1
        if not item.is_active:
            stats["reactivated"] += 1
        if dry_run:
            continue
        # 只动"身份"字段：工日、基价、工艺系数是造价员回填的成果，绝不覆盖。
        item.name = row["name"]
        item.category = QuotaCategory(row["category"])
        item.is_active = True
        if is_placeholder_note(item.notes):
            item.notes = IMPORT_NOTE

    try:
        await session.flush()
    except Exception as exc:  # noqa: BLE001
        # 部署路径上最容易踩的坑：迁移 0033 没跑，quota_category 还是老枚举，
        # 插 5-2-13 直接抛 invalid input value for enum。把根因写在异常里，
        # 免得下游只看到一句 "invalid input value"，跑去查数据文件。
        text = str(exc)
        if "quota_category" in text:
            raise RuntimeError(
                "数据库的 quota_category 枚举里没有 5-2 / 5-4 / 5-8 —— "
                "请先执行 `alembic upgrade head`（迁移 0033）再导入。原始错误：" + text
            ) from exc
        raise
    return stats


async def retire_placeholders(
    session: AsyncSession, *, dry_run: bool = False
) -> list[str]:
    """把 seed 的占位示意值退役（is_active=False），但不删除。

    为什么保留：演示报价单的明细备注仍写着 `quota:5-6-001`，而
    `quotes._recompute` 是按编号查库、**不看 is_active** 的 —— 删掉就会让
    演示单的定额口径无声退化。退役只是让它们不再出现在定额库检索里。
    """
    rows = list(
        (await session.scalars(select(QuotaItem).where(QuotaItem.is_active.is_(True)))).all()
    )
    targets = [q for q in rows if is_placeholder_note(q.notes)]
    if not dry_run:
        for q in targets:
            q.notes = (q.notes or "") + RETIRE_SUFFIX
            q.is_active = False
        await session.flush()
    return [q.code for q in targets]


async def main(
    *,
    data_file: Path,
    dry_run: bool,
    keep_placeholders: bool,
) -> None:
    # 与 app.main / seed 同一道闸：production 下配置不安全就别写库。
    settings.validate_for_runtime()
    rows = load_rows(data_file)
    init_engine()
    try:
        async with session_scope() as session:
            stats = await import_quota_items(session, rows, dry_run=dry_run)
            retired = (
                []
                if keep_placeholders
                else await retire_placeholders(session, dry_run=dry_run)
            )

        by_cat = Counter(r["category"] for r in rows)
        log.info(
            f"定额库导入{'（预演）' if dry_run else ''}：新增 {stats['inserted']}、"
            f"更新 {stats['updated']}、未变 {stats['unchanged']}，"
            f"退役占位示意值 {len(retired)} 条"
        )
        print(f"数据文件: {data_file}")
        print(f"真值子目: {len(rows)} 条，章节分布 {dict(sorted(by_cat.items()))}")
        print(
            f"入库结果: 新增 {stats['inserted']} / 更新 {stats['updated']}"
            f"（其中重新启用 {stats['reactivated']}）/ 未变 {stats['unchanged']}"
        )
        if retired:
            preview = "、".join(retired[:5]) + ("…" if len(retired) > 5 else "")
            print(f"已退役占位示意值 {len(retired)} 条：{preview}")
        if dry_run:
            print("（--dry-run：以上统计未写入数据库）")
        else:
            print("⚠ 这些子目的『人工消耗量 / 基价』仍是空的，正式报价前必须回填。")
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入 2018 版第五册真值定额子目")
    parser.add_argument("--file", type=Path, default=DATA_FILE, help="数据集 JSON 路径")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    parser.add_argument(
        "--keep-placeholders",
        action="store_true",
        help="不退役 seed 的 35 条占位示意值（默认退役）",
    )
    args = parser.parse_args()
    asyncio.run(
        main(
            data_file=args.file,
            dry_run=args.dry_run,
            keep_placeholders=args.keep_placeholders,
        )
    )
