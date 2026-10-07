"""导入批次的服务层：批次号生成 + 落档（2026-10-04）。

批次号 `IM-<YYYYMMDD>-<NNN>`：按天走序号。生成方式是在同一事务里
`SELECT count(*) FROM import_batches WHERE batch_no LIKE 'IM-20261004-%'` 再 +1 ——
并发下理论上可能撞号，所以 `batch_no` 上有唯一索引，撞了会抛 IntegrityError
（宁可报错也不给两条记录同一个号；真撞了重试一次即可）。
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.import_batch import ImportBatch, ImportKind, ImportStatus

log = get_logger("services.import_batch")

MAX_ERRORS = 50


async def next_batch_no(db: AsyncSession, kind: ImportKind) -> str:
    """生成当天的下一个批次号（kind 只影响日志，编号本身按天全局递增）。"""
    day = datetime.now(tz=UTC).strftime("%Y%m%d")
    prefix = f"IM-{day}-"
    used = (
        await db.scalar(
            select(func.count()).select_from(ImportBatch).where(
                ImportBatch.batch_no.like(f"{prefix}%")
            )
        )
    ) or 0
    return f"{prefix}{used + 1:03d}"


def decide_status(created: int, updated: int, error_rows: int, skipped: int) -> ImportStatus:
    written = created + updated
    if written == 0 and (error_rows or skipped):
        return ImportStatus.FAILED
    if error_rows or skipped:
        return ImportStatus.PARTIAL
    return ImportStatus.SUCCESS


async def record_batch(
    db: AsyncSession,
    *,
    kind: ImportKind,
    user,
    filename: str | None,
    file_size: int,
    total_rows: int,
    created: int = 0,
    updated: int = 0,
    skipped: int = 0,
    errors: list[dict] | None = None,
    summary: dict | None = None,
) -> ImportBatch:
    """落一条批次留档（调用方负责 flush/commit）。"""
    error_list = (errors or [])[:MAX_ERRORS]
    batch = ImportBatch(
        batch_no=await next_batch_no(db, kind),
        kind=kind,
        filename=(filename or "")[:255] or None,
        file_size=int(file_size or 0),
        total_rows=int(total_rows or 0),
        created_rows=int(created or 0),
        updated_rows=int(updated or 0),
        skipped_rows=int(skipped or 0),
        error_rows=len(errors or []),
        status=decide_status(created, updated, len(errors or []), skipped),
        summary=summary or {},
        errors=error_list,
        created_by=getattr(user, "id", None),
        created_by_name=getattr(user, "username", None),
    )
    db.add(batch)
    await db.flush()
    log.info(
        f"导入批次 {batch.batch_no}（{kind}）: 新增 {created} 更新 {updated} "
        f"跳过 {skipped} 失败 {batch.error_rows}"
    )
    return batch
