"""恢复出厂设置（2026-10-05）——**高危操作，按"四重门 + 先备份 + 必定留痕"设计**。

## 为什么这么谨慎

这个按钮点错的代价是"几年的报价数据没了"，而且不可撤销。
所以不是"确认对话框点两下"就够，而是：

1. **环境闸门**（2026-10-08 新增）：`ALLOW_FACTORY_RESET` 默认 **false** →
   接口直接 403。**默认关着**，要用必须先显式打开再重启服务；
2. **权限**：`setting.reset` 只给 admin（前端也对其它角色隐藏）；
3. **重输密码**：验证当前登录账号的口令 —— 防的是"别人趁你没锁屏点一下"；
4. **手打确认短语**：必须原样输入「恢复出厂设置」，防的是"鼠标滑了一下"；
5. **先自动备份**：用 `pg_dump` 存一份自定义格式备份，**备份失败就中止**，
   绝不无备份清库（这是最后一道保险，也是事后唯一能回到原状的路）；
6. **必定留痕**（2026-10-08 新增）：动手前先把"谁、什么时候、什么范围、
   各表多少行、备份在哪"写成三份**本次操作删不掉**的记录（见下）；
7. **支持预演**（`dry_run`）：只统计各表行数 + 备份，不删任何东西。

## 2026-10-08 事故与这次的修复

生产库的 `projects` / `quotes` / `quote_items` 被清空，`devices` / `users` 完好，
但 `quote_audit_logs` 里**没有任何当天的记录**、备份目录里也没有当天的
`factory-reset_*.dump`。最可能的原因就是走了这个接口 —— 而它的清理范围里
**包含 `quote_audit_logs` / `project_audit_logs` / `login_audit_logs`**，
所以用它清库等于"连作案痕迹一起擦掉"。

因此现在：
  · 真正"谁干的"记在 `factory_reset_logs` 表里，**该表不在任何清理清单中**，
    `scope=all` 也删不掉它（见 `TRACE_TABLE` 与 `_tables_for()`）；
  · 同时写一份同名的 `.json` 到备份文件旁边，再往服务器日志打一条 WARNING ——
    三处互为备份：库连不上看日志，日志滚掉了看 JSON，都还在就查表；
  · 响应与日志里都带**备份文件的完整路径**，事后照它恢复。

## 三档范围

  · `business`：只清业务数据（项目/报价/设备库/供应商/客户/定额/人工费/点位模板/
    批次/留痕/通知/AI 会话）—— 账号与系统设置保留。相当于 `seed --reset` 的完整版。
  · `business_settings`：再加系统设置（公司抬头、费率、条款、审批、AI 配置）。
  · `all`：再加组织架构与**除当前账号外的所有用户**。

清完之后要恢复"出厂演示数据"，在服务器上跑 `python apps/api/scripts/seed.py`。
"""

from __future__ import annotations

import json
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
from app.models.factory_reset_log import (
    STATUS_COMPLETED,
    STATUS_COMPLETED_WITH_ERRORS,
    STATUS_DRY_RUN,
    STATUS_STARTED,
    FactoryResetLog,
)
from app.models.user import User

log = get_logger("services.factory_reset")

CONFIRM_PHRASE = "恢复出厂设置"

SCOPE_BUSINESS = "business"
SCOPE_BUSINESS_SETTINGS = "business_settings"
SCOPE_ALL = "all"
SCOPES = (SCOPE_BUSINESS, SCOPE_BUSINESS_SETTINGS, SCOPE_ALL)

# 与 deploy/backup.sh 的落点保持一致：/var/backups/quote 不在 /home/example 之下，
# postgres 用户可以穿越 —— 恢复时一条 `sudo -u postgres pg_restore` 就够，
# 不必再 `cp /tmp && chmod 644`（2026-10-08 事故里就是这么临时对付的）。
DEFAULT_BACKUP_DIR = pathlib.Path("/var/backups/quote")
LEGACY_BACKUP_DIR = pathlib.Path.home() / "quote-backups"

# 留痕表名。**绝对不允许**出现在下面任何清理清单里：
# 它就是"清库之后还在"的那份记录，混进清单等于把事故证据一起删掉。
TRACE_TABLE = "factory_reset_logs"

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


class FactoryResetDisabledError(RuntimeError):
    """生产闸门没开 —— 接口一律 403，不做任何事。

    名字带 Error 后缀是为了跟同文件的 `BackupError` 一致（ruff N818）。
    """


# 403 的说明文案：要让人一眼看懂"为什么不让用"以及"真要用怎么办"。
FACTORY_RESET_DISABLED_DETAIL = (
    "「恢复出厂设置」已被环境闸门关闭（ALLOW_FACTORY_RESET=false，默认值）。"
    "这是 2026-10-08 生产库被清空、且未留任何痕迹之后加的第三道门："
    "光有 admin 权限 + 确认短语还不够，任何拿到 admin 令牌的人或脚本都不该能一键清库。"
    "确需执行时：① 在 apps/api/.env 里加一行 ALLOW_FACTORY_RESET=true；"
    "② 重启 quote-api；③ 先 dry_run 预演核对各表行数；④ 执行完**立刻**把该行改回 false 并再次重启。"
)


def ensure_enabled() -> None:
    """闸门没开就抛 FactoryResetDisabledError（路由映射成 403）。

    注意：**preview 不受这道闸门限制** —— 它只读、不删任何东西，
    恰恰是"我到底会失去什么"的诊断入口，关掉它只会让人更盲目。
    """
    if not app_settings.allow_factory_reset:
        raise FactoryResetDisabledError(FACTORY_RESET_DISABLED_DETAIL)


def _backup_dir() -> pathlib.Path:
    """备份落点：`$BACKUP_DIR` > `/var/backups/quote`（可写时）> `~/quote-backups`。

    回退时**必须说出来** —— 悄悄换目录正是这次事故里"以为有备份/以为拉到了"
    那一类问题的根源。
    """
    override = os.environ.get("BACKUP_DIR", "").strip()
    if override:
        return pathlib.Path(override)
    if DEFAULT_BACKUP_DIR.is_dir() and os.access(DEFAULT_BACKUP_DIR, os.W_OK):
        return DEFAULT_BACKUP_DIR
    log.warning(
        f"备份目录 {DEFAULT_BACKUP_DIR} 不存在或不可写，回退到 {LEGACY_BACKUP_DIR}；"
        f"该目录在 2026-10-08 事故中导致 postgres 用户穿不过去、恢复时要手工 cp+chmod。"
        f"建议跑一次 deploy/deploy.sh 把它建好。"
    )
    return LEGACY_BACKUP_DIR


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

    backup_dir = _backup_dir()
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

    # 0640 + 目录 setgid（deploy.sh 建的 /var/backups/quote 是 example:postgres 2750）
    # ⇒ postgres 用户读得到，别人读不到。恢复时不必再 chmod。
    try:
        os.chmod(out, 0o640)
    except OSError as exc:  # pragma: no cover - 只在怪异文件系统上发生
        log.warning(f"备份文件 chmod 0640 失败（不影响备份本身）：{out}：{exc}")

    size_mb = round(out.stat().st_size / 1024 / 1024, 2)
    log.warning(f"恢复出厂设置前备份完成：{out}（{size_mb} MB）")
    return {"path": str(out), "filename": out.name, "size_mb": size_mb}


def write_trace_file(
    payload: dict[str, Any], *, backup: dict[str, Any] | None
) -> pathlib.Path | None:
    """把留痕写一份 JSON 到**备份文件旁边**（同名不同后缀）。

    只在真的做了备份时写：一来"备份在哪、留痕就在哪"最好找，
    二来测试里 `require_backup=False`（SQLite 没有 pg_dump）就不会往开发机写文件。

    失败**不中止**流程：留痕的主副本是数据库里的 `factory_reset_logs` 行
    与服务器日志，这份 JSON 是第三份保险。
    """
    if not backup or not backup.get("path"):
        return None
    try:
        target = pathlib.Path(str(backup["path"])).with_suffix(".json")
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n",
            encoding="utf-8",
        )
        try:
            os.chmod(target, 0o640)
        except OSError:  # pragma: no cover
            pass
        return target
    except OSError as exc:  # pragma: no cover - 磁盘满/权限问题才走到
        log.error(f"写留痕 JSON 失败（不影响本次操作的其它留痕）：{exc}")
        return None


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

    `dry_run=True`：只统计 + 备份 + 留痕，不删任何行。
    `require_backup=False`：仅测试用（生产路径永远先备份）。
    """
    # 第一道门：环境闸门。放在最前面 —— 没开闸时连密码都不该被检验。
    ensure_enabled()
    await check_credentials(user, password=password, confirm_text=confirm_text)

    before = await preview(db, scope=scope)
    started_at = datetime.now(tz=UTC)

    backup: dict[str, Any] | None = None
    if require_backup:
        backup = make_backup()

    # ---- 留痕 ①：服务器日志 + 备份旁的 JSON（都不依赖数据库事务）----
    trace: dict[str, Any] = {
        "operator_id": str(user.id),
        "operator_username": user.username,
        "scope": scope,
        "dry_run": dry_run,
        "started_at": started_at.isoformat(timespec="seconds"),
        "backup_path": (backup or {}).get("path"),
        "backup_filename": (backup or {}).get("filename"),
        "counts": before,
    }
    trace_file = write_trace_file(trace, backup=backup)
    log.warning(
        f"⚠ 恢复出厂设置「{'预演' if dry_run else '开始'}」：scope={scope} "
        f"操作者={user.username}({user.id}) 备份="
        f"{(backup or {}).get('path') or '（无）'} 留痕="
        f"{trace_file or '（仅数据库）'}"
    )

    # ---- 留痕 ②：库里的行。与下面的 DELETE **同一个事务** ——
    # 要么"删了并且记下了"，要么两者一起回滚，不存在"删了但没记"。
    row = FactoryResetLog(
        operator_id=user.id,
        operator_username=user.username,
        scope=scope,
        dry_run=dry_run,
        status=STATUS_DRY_RUN if dry_run else STATUS_STARTED,
        backup_path=(backup or {}).get("path"),
        backup_filename=(backup or {}).get("filename"),
        counts=before,
        deleted={},
        total_deleted=0,
    )
    db.add(row)
    await db.flush()
    trace_id = str(row.id)

    if dry_run:
        row.status = STATUS_DRY_RUN
        await db.flush()
        return {
            "scope": scope,
            "dry_run": True,
            "backup": backup,
            "backup_path": (backup or {}).get("path"),
            "trace_id": trace_id,
            "trace_file": str(trace_file) if trace_file else None,
            "preview": before,
            "deleted": [],
            "total_deleted": 0,
            "note": "预演：只统计与备份，没有删除任何数据",
        }

    deleted: list[dict[str, Any]] = []
    total_deleted = 0
    errors: list[str] = []
    for table, label in _tables_for(scope):
        try:
            result = await db.execute(text(f"DELETE FROM {table}"))  # noqa: S608
            n = int(result.rowcount or 0)
        except Exception as exc:  # noqa: BLE001 - 单表失败要报出来，不能静默
            log.error(f"清表 {table} 失败：{type(exc).__name__}: {exc}")
            errors.append(f"{table}: {type(exc).__name__}")
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

    # ---- 留痕收尾：真实删除结果写回同一行（该表不在清理清单里，所以它还在）----
    row.deleted = {"items": deleted, "total": total_deleted}
    row.total_deleted = total_deleted
    row.status = STATUS_COMPLETED_WITH_ERRORS if errors else STATUS_COMPLETED
    row.error = "; ".join(errors) if errors else None

    await db.flush()
    log.warning(
        f"⚠ 恢复出厂设置已执行：scope={scope} by {user.username}，"
        f"共删除 {total_deleted} 行；备份={(backup or {}).get('path') or '（无）'}；"
        f"留痕 id={trace_id}"
        + (f"；有 {len(errors)} 张表失败：{'; '.join(errors)}" if errors else "")
    )
    return {
        "scope": scope,
        "dry_run": False,
        "backup": backup,
        "backup_path": (backup or {}).get("path"),
        "trace_id": trace_id,
        "trace_file": str(trace_file) if trace_file else None,
        "preview": before,
        "deleted": deleted,
        "total_deleted": total_deleted,
        "note": (
            "数据已清空。**后续部署不会再把演示数据种回来**"
            "（deploy.sh 第 5 步已改为默认不种；确需演示数据时显式 "
            "`SEED_DEMO=1` 重新部署，或直接跑 scripts/seed.py）。"
            f"如需回滚：用备份 {(backup or {}).get('path') or ''} 恢复。"
        ),
    }
