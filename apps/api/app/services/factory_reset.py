"""恢复出厂设置（2026-10-05）——**高危操作，按"三重门 + 先备份"设计**。

## 为什么这么谨慎

这个按钮点错的代价是"几年的报价数据没了"，而且不可撤销。
所以不是"确认对话框点两下"就够，而是：

1. **权限**：`setting.reset` 只给 admin（前端也对其它角色隐藏）；
2. **重输密码**：验证当前登录账号的口令 —— 防的是"别人趁你没锁屏点一下"；
3. **手打确认短语**：必须原样输入「恢复出厂设置」，防的是"鼠标滑了一下"；
4. **先自动备份**：用 `pg_dump` 存到 `~/quote-backups/factory-reset-<时间>.dump`，
   **备份失败就中止**，绝不无备份清库（这是最后一道保险，也是事后唯一能回到原状的路）；
5. **支持预演**（`dry_run`）：只统计各表行数 + 备份，不删任何东西。

## 三档范围

  · `business`：只清业务数据（项目/报价/设备库/供应商/客户/定额/人工费/点位模板/
    批次/留痕/通知/AI 会话）—— 账号与系统设置保留。相当于 `seed --reset` 的完整版。
  · `business_settings`：再加系统设置（公司抬头、费率、条款、审批、AI 配置）。
  · `all`：再加组织架构与**除当前账号外的所有用户**。

清完之后要恢复"出厂演示数据"，在服务器上跑 `python apps/api/scripts/seed.py`。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as app_settings
from app.core.logging import get_logger
from app.models.department import Department
from app.models.user import User

log = get_logger("services.factory_reset")

CONFIRM_PHRASE = "恢复出厂设置"

SCOPE_BUSINESS = "business"
SCOPE_BUSINESS_SETTINGS = "business_settings"
SCOPE_ALL = "all"
SCOPES = (SCOPE_BUSINESS, SCOPE_BUSINESS_SETTINGS, SCOPE_ALL)

# 删除顺序：**先子后父**（有外键的表先删），顺序错了会撞 FK 约束。
# 表名与模型里的 __tablename__ 一一对应；写错表名会在这里报错而不是静默漏删。
BUSINESS_TABLES: tuple[tuple[str, str], ...] = (
    ("ai_messages", "AI 对话消息"),
    ("ai_conversations", "AI 会话"),
    ("ai_parse_jobs", "AI 报价解析任务"),
    ("ai_usage_logs", "AI 调用台账"),
    ("quote_revisions", "报价单版本快照"),
    ("quote_approvals", "报价单审批记录"),
    ("quote_audit_logs", "报价单操作留痕"),
    ("quote_items", "报价明细行"),
    ("quotes", "报价单"),
    ("project_audit_logs", "项目操作留痕"),
    ("subsystems", "子系统"),
    ("device_price_tiers", "设备阶梯价"),
    ("point_template_items", "点位模板明细"),
    ("point_templates", "点位模板"),
    ("import_batches", "导入批次"),
    ("notifications", "站内通知"),
    ("login_audit_logs", "登录留痕"),
    ("projects", "项目"),
    ("devices", "设备库"),
    ("device_categories", "设备分类树"),
    ("suppliers", "供应商"),
    ("customers", "客户档案"),
    ("quota_items", "定额子目"),
    ("labor_rates", "人工信息价"),
)

SETTINGS_TABLES: tuple[tuple[str, str], ...] = (
    ("app_settings", "系统设置"),
)

# 账号相关：`users` 要排除当前操作者（否则自己也被删，会话立刻失效）
ACCOUNT_TABLES: tuple[tuple[str, str], ...] = (
    ("departments", "组织架构"),
)


class BackupError(RuntimeError):
    """备份没做成 —— 此时**不允许**继续清库。"""


def _sync_dsn() -> str:
    """把异步 URL 换成 psycopg 的同步 URL（pg_dump 用）。"""
    url = app_settings.database_url_sync or app_settings.database_url or ""
    return url.replace("postgresql+psycopg://", "postgresql://").replace(
        "postgresql+asyncpg://", "postgresql://"
    )


def make_backup(*, reason: str = "factory-reset") -> dict[str, Any]:
    """用 pg_dump 做一次自定义格式备份。失败抛 BackupError（调用方必须中止）。

    用子进程而不是库：`pg_dump` 与 PostgreSQL 版本配套，自己拼 SQL 导出是另一套东西。
    """
    dsn = _sync_dsn()
    if not dsn.startswith("postgresql://"):
        raise BackupError(
            "当前数据库不是 PostgreSQL（测试环境用 SQLite），无法用 pg_dump 备份；"
            "生产环境下请确认 apps/api/.env 里的 DATABASE_URL_SYNC"
        )

    backup_dir = pathlib.Path(os.environ.get("BACKUP_DIR") or pathlib.Path.home() / "quote-backups")
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d_%H%M%S")
    out = backup_dir / f"{reason}_{stamp}.dump"

    try:
        proc = subprocess.run(  # noqa: S603 - 参数是拼死的，没有 shell
            ["pg_dump", dsn, "-Fc", "-f", str(out)],
            capture_output=True,
            timeout=600,
            check=False,
        )
    except FileNotFoundError as exc:
        raise BackupError("服务器上没有 pg_dump（缺 postgresql-client）") from exc
    except subprocess.TimeoutExpired as exc:
        raise BackupError("pg_dump 超时（600 秒）") from exc

    if proc.returncode != 0 or not out.exists() or out.stat().st_size < 1024:
        detail = (proc.stderr or b"").decode("utf-8", "replace")[:300]
        raise BackupError(f"pg_dump 失败：{detail or '输出文件异常'}")

    size_mb = round(out.stat().st_size / 1024 / 1024, 2)
    log.warning(f"恢复出厂设置前备份完成：{out}（{size_mb} MB）")
    return {"path": str(out), "filename": out.name, "size_mb": size_mb}


def _tables_for(scope: str) -> list[tuple[str, str]]:
    if scope not in SCOPES:
        raise ValueError(f"未知范围：{scope}")
    tables = list(BUSINESS_TABLES)
    if scope in (SCOPE_BUSINESS_SETTINGS, SCOPE_ALL):
        tables += list(SETTINGS_TABLES)
    if scope == SCOPE_ALL:
        tables += list(ACCOUNT_TABLES)
    return tables


async def preview(db: AsyncSession, *, scope: str) -> dict[str, Any]:
    """各表当前行数（让用户在动手前看清将失去什么）。"""
    items = []
    total = 0
    for table, label in _tables_for(scope):
        try:
            n = int(await db.scalar(text(f"SELECT count(1) FROM {table}")) or 0)  # noqa: S608
        except Exception:  # noqa: BLE001 - 表不存在就当 0（迁移没跑到？）
            n = 0
        items.append({"table": table, "label": label, "rows": n})
        total += n
    if scope == SCOPE_ALL:
        users = int(await db.scalar(select(func.count()).select_from(User)) or 0)
        items.append({"table": "users", "label": "用户账号（保留当前账号）", "rows": users})
        total += users
    return {"scope": scope, "items": items, "total": total}


async def check_credentials(
    user: User, *, password: str, confirm_text: str
) -> None:
    """三门里的两门：确认短语 + 当前账号口令。都是"必须做对"的事。"""
    if (confirm_text or "").strip() != CONFIRM_PHRASE:
        raise PermissionError(f"确认短语不对：请原样输入「{CONFIRM_PHRASE}」")
    if not password:
        raise PermissionError("请输入当前账号的登录密码")
    from app.core.security import verify_password_async

    if not await verify_password_async(password, user.hashed_password):
        raise PermissionError("密码不正确")


async def perform(
    db: AsyncSession,
    *,
    scope: str,
    user: User,
    password: str,
    confirm_text: str,
    dry_run: bool = False,
    require_backup: bool = True,
) -> dict[str, Any]:
    """执行（或预演）恢复出厂设置。

    `dry_run=True`：只统计 + 备份，不删任何行。
    `require_backup=False`：仅测试用（生产路径永远先备份）。
    """
    await check_credentials(user, password=password, confirm_text=confirm_text)

    before = await preview(db, scope=scope)

    backup: dict[str, Any] | None = None
    if require_backup:
        backup = make_backup()
    if dry_run:
        return {
            "scope": scope,
            "dry_run": True,
            "backup": backup,
            "preview": before,
            "deleted": [],
            "total_deleted": 0,
            "note": "预演：只统计与备份，没有删除任何数据",
        }

    deleted: list[dict[str, Any]] = []
    total_deleted = 0
    for table, label in _tables_for(scope):
        try:
            result = await db.execute(text(f"DELETE FROM {table}"))  # noqa: S608
            n = int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001 - 单表失败要报出来，不能静默
            log.error(f"清表 {table} 失败：{type(exc).__name__}: {exc}")
            deleted.append({"table": table, "label": label, "deleted": 0,
                            "error": f"{type(exc).__name__}"})
            continue
        deleted.append({"table": table, "label": label, "deleted": n})
        total_deleted += n

    # 账号范围：删掉除自己以外的用户（自己的会话要能继续用）。
    # ⚠ 必须用 ORM 的带类型比较，不能 `text("... WHERE id <> :keep")` 传字符串：
    # SQLite 里 UUID 存的是无横线十六进制、而 str(uuid) 带横线，两者永不相等，
    # 结果会把**当前账号自己**也删掉（测试就是这么抓到的）。
    if scope == SCOPE_ALL:
        result = await db.execute(delete(User).where(User.id != user.id))
        n = int(result.rowcount or 0)
        deleted.append({"table": "users", "label": "其它用户账号", "deleted": n})
        total_deleted += n
        await db.execute(delete(Department).where(Department.id.is_not(None)))

    await db.flush()
    log.warning(
        f"⚠ 恢复出厂设置已执行：scope={scope} by {user.username}，"
        f"共删除 {total_deleted} 行；备份={backup.get('filename') if backup else '（无）'}"
    )
    return {
        "scope": scope,
        "dry_run": False,
        "backup": backup,
        "preview": before,
        "deleted": deleted,
        "total_deleted": total_deleted,
        "note": (
            "数据已清空。**后续部署不会再把演示数据种回来**"
            "（deploy.sh 第 5 步已改为默认不种；确需演示数据时显式 "
            "`SEED_DEMO=1` 重新部署，或直接跑 scripts/seed.py）。"
            f"如需回滚：用备份 {backup.get('filename') if backup else ''} 恢复。"
        ),
    }
