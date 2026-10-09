"""系统设置路由（2026-10-04）：公司抬头 / 报价有效期 / 报价说明条款。

写权限只给 admin（`setting:update`）：这些值一改，所有导出的 PDF、Excel、
客户版预览的抬头与条款都会跟着变，影响面比单张报价单大得多。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import require_permission
from app.core.logging import get_logger
from app.db.session import get_db
from app.models.user import User
from app.services.settings import (
    LETTERHEAD_MAXLEN,
    MAX_LETTERHEADS,
    load_settings,
    normalize_letterhead,
    redact_settings,
    save_settings,
    strip_ai_secrets,
)


def role_label_str(key: str) -> str:
    """角色 key → 显示名（错误提示里给人看）。"""
    from app.core.roles import role_label

    return role_label(key)


def _clause_variables() -> dict[str, str]:
    """条款里可用的 {变量} 清单（前端设置页显示提示用）。"""
    from app.services.quote_terms import CLAUSE_VARIABLES

    return CLAUSE_VARIABLES


router = APIRouter(prefix="/settings", tags=["系统设置"])
log = get_logger("api.settings")


@router.get("", summary="读取系统设置（缺项用默认值补齐）")
async def get_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "read"))],
) -> dict:
    # 脱敏：AI Key 的密文也不能出服务端。
    # `clause_variables` 是条款里可用变量的定义（前端设置页显示提示用）。
    return {
        **redact_settings(await load_settings(db)),
        "clause_variables": _clause_variables(),
    }


# ---------------------------------------------------------------------------
# 保存 / 校验 / 缓存刷新（2026-10-05 抽成助手：PATCH 与「导入配置」共用同一套）
# ---------------------------------------------------------------------------

async def _validate_settings_patch(db: AsyncSession, body: dict) -> None:
    """保存前的全部校验。**PATCH 与导入都走这里** —— 两条路各写一套迟早漂移。

    校验不过就抛 422，并且**不改动 body**（除了把 roles.custom / company.letterheads
    换成净化后的版本）。
    """
    # 条款里的 {变量} 必须都在定义表里 —— 写错了直接 422，
    # 而不是把 `{valid_dayss}` 原样发给客户（这种错人工几乎不可能发现）。
    from app.services.quote_terms import CLAUSE_VARIABLES, unknown_variables

    quote_patch = body.get("quote") if isinstance(body.get("quote"), dict) else {}
    bad_vars = unknown_variables(quote_patch.get("clauses") or [])
    if bad_vars:
        raise HTTPException(
            status_code=422,
            detail=(
                f"条款里有未定义的变量：{'、'.join('{' + v + '}' for v in bad_vars)}。"
                f"可用变量：{'、'.join('{' + k + '}' for k in CLAUSE_VARIABLES)}"
            ),
        )

    # 权限覆盖：只接受默认矩阵里存在的资源/动作；admin 不接受覆盖。
    # 在接口层就拦：这些值直接决定"谁能进哪个模块"，
    # 一个错别字不该让人手里多出或少掉一整块权限。
    from app.core.permissions import Role as _Role
    from app.core.permissions import permission_catalog
    from app.core.roles import known_role_keys

    # 本次请求里**正在创建**的自定义角色也算"已知"——
    # 否则"新建角色 + 同时给它配权限"会被拒（校验顺序导致，调用方不该关心顺序）。
    _incoming_roles = {
        str(item.get("key") or "").strip().lower()
        for item in ((body.get("roles") or {}).get("custom") or [])
        if isinstance(item, dict)
    }
    _known = known_role_keys() | _incoming_roles

    perm_patch = body.get("permissions")
    if isinstance(perm_patch, dict):
        overrides = perm_patch.get("overrides")
        if overrides is not None and not isinstance(overrides, dict):
            raise HTTPException(status_code=422, detail="permissions.overrides 必须是对象")
        catalog = permission_catalog()
        for role_name, res_map in (overrides or {}).items():
            if str(role_name) == str(_Role.ADMIN):
                raise HTTPException(
                    status_code=422,
                    detail="admin 权限不可修改（恒定全部权限，防止把自己锁在门外）",
                )
            if str(role_name) not in _known:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"未知角色：{role_name}"
                        "（自定义角色请先在「角色管理」里建好，或与角色定义一起提交）"
                    ),
                )
            if not isinstance(res_map, dict):
                raise HTTPException(
                    status_code=422, detail=f"{role_name} 的权限必须是「资源→动作数组」"
                )
            for res, actions in res_map.items():
                if res not in catalog:
                    raise HTTPException(status_code=422, detail=f"未知资源：{res}")
                bad = [str(a) for a in (actions or []) if str(a) not in catalog[res]]
                if bad:
                    raise HTTPException(
                        status_code=422,
                        detail=f"{role_name}.{res} 含有该资源不支持的动作：{'、'.join(bad)}",
                    )

    # 自定义角色：校验定义；**不允许删掉还有人在用的角色** ——
    # 否则那些人会变成"角色不存在"，登录后一片空白却没人知道为什么。
    from sqlalchemy import func as _func
    from sqlalchemy import select as _select

    from app.core.roles import custom_roles_snapshot, validate_custom_role
    from app.models.user import User as _User

    roles_patch = body.get("roles")
    if isinstance(roles_patch, dict) and "custom" in roles_patch:
        incoming = roles_patch.get("custom") or []
        if not isinstance(incoming, list):
            raise HTTPException(status_code=422, detail="roles.custom 必须是数组")
        cleaned, seen = [], set()
        for item in incoming:
            role, err = validate_custom_role(item, existing_keys=seen)
            if role is None:
                raise HTTPException(status_code=422, detail=err)
            seen.add(role["key"])
            cleaned.append(role)
        removed = {r["key"] for r in custom_roles_snapshot()} - seen
        if removed:
            counts = dict(
                (
                    await db.execute(
                        _select(_User.role, _func.count())
                        .where(_User.role.in_(removed))
                        .group_by(_User.role)
                    )
                ).all()
            )
            if counts:
                detail = "、".join(f"{role_label_str(k)}（{v} 人）" for k, v in counts.items())
                raise HTTPException(
                    status_code=422,
                    detail=f"这些角色还有人在用，先把他们改成别的角色：{detail}",
                )
        body["roles"] = {**roles_patch, "custom": cleaned}

    # 公司抬头库（2026-10-09）：报价单可选用哪家公司抬头报价。
    # 这里只维护**候选名单**。单据上存的是当时选定那一项的**值本身**
    # （天然快照），所以改/删这份名单**不会**追溯改写已开出的报价单 ——
    # 这正是"历史单据绝不跟着变"的实现方式 ✓。
    # 结构（2026-10-09 扩展 ✓，用户补充「地址和联系电话和邮箱」）：
    #   `[{name, address, phone, email}, …]`，`name` 必填、其余三项可空 ✓。
    # 净化动作：按字段去空白、**按名称去重**、限长（每字段各自上限）、限量；
    #   `name` 为空的那条直接丢掉（前端多留一行空输入框不该报错 ✓）；
    #   但**类型错**要拦（数组里混进数字、或一项不是字符串/对象 = 接口误用 ✗）。
    # ⚠ **兼容旧格式** ✓：以前是字符串数组 `["甲公司"]` —— 归一成
    #   `{"name": "甲公司"}` ✓（normalize_letterhead ✓），读到旧格式不能崩 ✗。
    company_patch = body.get("company")
    if isinstance(company_patch, dict) and "letterheads" in company_patch:
        raw = company_patch.get("letterheads")
        if raw is None:
            raw = []
        if not isinstance(raw, list):
            raise HTTPException(status_code=422, detail="company.letterheads 必须是数组")
        cleaned_letterheads: list[dict[str, str]] = []
        seen_names: set[str] = set()
        for item in raw:
            if not isinstance(item, (str, dict)):
                raise HTTPException(
                    status_code=422,
                    detail="company.letterheads 里每一项必须是公司名，或含 name 的对象",
                )
            if isinstance(item, str) and not item.strip():
                continue  # 旧格式的空串：丢掉（前端多留一行空输入框不该报错 ✓）
            entry = normalize_letterhead(item)
            if entry is None:
                # 对象但 name 为空 → 当作"还没填完的一行"丢掉 ✓（不报错 ✓）
                continue
            for field, limit in LETTERHEAD_MAXLEN.items():
                if len(entry[field]) > limit:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"公司抬头的「{field}」过长（最多 {limit} 字）："
                            f"{entry[field][:20]}…"
                        ),
                    )
            if entry["name"] in seen_names:
                continue  # 同名去重（保留首次出现的顺序 ✓）
            seen_names.add(entry["name"])
            cleaned_letterheads.append(entry)
        if len(cleaned_letterheads) > MAX_LETTERHEADS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"公司抬头最多 {MAX_LETTERHEADS} 个"
                    f"（当前 {len(cleaned_letterheads)} 个）"
                ),
            )
        body["company"] = {**company_patch, "letterheads": cleaned_letterheads}


def _refresh_runtime_caches(saved: dict) -> None:
    """把刚保存的设置推进进程内缓存 —— 判定立刻生效，不用重启。

    不做这一步的话，界面显示"已保存"，而判定还是旧值：那就是"配置没生效"，
    比没有这个设置更糟。
    """
    from app.core.permissions import set_permission_overrides
    from app.core.roles import set_custom_roles

    # 角色先于权限覆盖：覆盖里可能引用自定义角色
    set_custom_roles((saved.get("roles") or {}).get("custom") or [])
    # 权限矩阵覆盖（2026-10-05 起**也包含字段级**的 data_cost / data_profit 两行，
    # 所以"数据可见性"不再是独立设置，这里一次刷新就够）
    set_permission_overrides((saved.get("permissions") or {}).get("overrides") or {})


@router.patch("", summary="保存系统设置（只覆盖传入的键）")
async def patch_settings(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "update"))],
) -> dict:
    await _validate_settings_patch(db, body)
    # AI 密钥字段只能走 /ai/config（加密 + 审计）：通用入口把这几项剔除，
    # 否则有人可以直接把 api_key_cipher 覆盖成明文（等于绕过加密）。
    payload = strip_ai_secrets(body)

    # 留痕（2026-10-05）：先算出"会改什么"，再落库。
    # 这类改动影响面最大（动一下权限矩阵，某个部门就进不去某个模块了），
    # 而事后没有界面可查就只能靠翻日志、或者问"是不是你改的"。
    current = await load_settings(db)
    diff = _settings_diff(current, payload)

    saved = await save_settings(db, payload, actor_id=user.id)
    _refresh_runtime_caches(saved)

    if diff:
        from app.models.audit import SettingAuditAction
        from app.services.audit import setting_audit

        db.add(
            setting_audit(
                user_id=user.id,
                username=user.username,
                action=SettingAuditAction.SETTINGS_UPDATE,
                summary="保存系统设置：" + "、".join(g["group"] for g in diff),
                diff=diff,
            )
        )
        await db.commit()
    log.info(
        "settings updated by {}: keys={}",
        user.username, sorted(k for k, v in payload.items() if v is not None),
    )
    return redact_settings(saved)


# ---------------------------------------------------------------------------
# 配置导出 / 导入（2026-10-05）
# ---------------------------------------------------------------------------

EXPORT_SCHEMA_VERSION = 1
_SECRET_KEYS = ("api_key", "api_key_cipher", "api_key_hint", "last_test_at",
                "last_test_ok", "last_test_error")


def _brief(value: object, limit: int = 120) -> str:
    """把值压成一行短文本（预演差异给人看，不需要全文）。"""
    import json as _json

    try:
        text = _json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = str(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _settings_diff(current: dict, incoming: dict) -> list[dict]:
    """按组列出"导入后会改什么"。"""
    out: list[dict] = []
    for group, new in incoming.items():
        if new is None:
            continue
        old = current.get(group)
        changes: list[dict] = []
        if isinstance(new, dict) and isinstance(old, dict):
            for field, value in new.items():
                if old.get(field) != value:
                    changes.append(
                        {"field": field, "from": _brief(old.get(field)),
                         "to": _brief(value)}
                    )
        elif old != new:
            changes.append({"field": group, "from": _brief(old), "to": _brief(new)})
        if changes:
            out.append({"group": group, "changes": changes})
    return out


def _export_payload(user: User, saved: dict) -> dict:
    """导出内容：脱敏后的设置（**永不含密钥**）。"""
    from app.core.config import settings as app_conf

    data = redact_settings(saved)
    ai = dict(data.get("ai") or {})
    for key in _SECRET_KEYS:
        ai.pop(key, None)
    ai["api_key_set"] = bool(ai.get("api_key_set"))
    data["ai"] = ai
    return {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "exported_at": datetime.now(tz=UTC).isoformat(),
        "exported_by": user.username,
        "app_version": getattr(app_conf, "app_version", ""),
        "note": "本文件不含任何密钥；导入后 AI Key 需要重新填写。",
        "settings": data,
    }


@router.get("/audit-logs", summary="配置/权限变更留痕（最近 N 条）")
async def setting_audit_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "update"))],
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    """谁在什么时候把设置/权限改成了什么。

    留痕里**不含密钥**（AI 配置只记"改了哪些字段、有没有重设 Key"）。
    """
    from sqlalchemy import select as _select

    from app.core.roles import role_label
    from app.models.audit import SettingAuditLog

    rows = (
        await db.scalars(
            _select(SettingAuditLog)
            .order_by(SettingAuditLog.created_at.desc())
            .limit(limit)
        )
    ).all()
    action_label = {
        "settings_update": "保存设置",
        "config_import": "导入配置",
        "ai_config": "改 AI 配置",
    }
    return {
        "total": len(rows),
        "items": [
            {
                "id": str(r.id),
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "username": r.username,
                "action": str(r.action),
                "action_label": action_label.get(str(r.action), str(r.action)),
                "summary": r.summary,
                "diff": (r.payload or {}).get("diff") or [],
                "ip": r.ip,
            }
            for r in rows
        ],
        # 前端显示角色名要用（差异里可能是角色 key）
        "role_labels": {k: role_label(k) for k in ("admin", "manager", "finance",
                                                   "estimator", "purchaser", "sales",
                                                   "viewer")},
    }


@router.get("/export", summary="导出配置（设置 + 权限 + 角色；不含密钥）")
async def export_settings(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "update"))],
) -> dict:
    saved = await load_settings(db)
    log.info(f"配置导出 by {user.username}")
    return _export_payload(user, saved)


@router.post(
    "/import",
    summary="导入配置（默认只预演差异；confirm=true 才落库）",
)
async def import_settings(
    body: dict,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "update"))],
) -> dict:
    """导入配置。

    · 默认 `dry_run=true` —— 先看"会改什么"，人确认了再 `dry_run=false`；
    · 文件里的密钥字段一律剥掉（文件可能来自别处，不能让密钥跟着进来）；
    · 校验与 PATCH /settings 完全一致（`_validate_settings_patch`）。
    """
    doc = body.get("document") if isinstance(body.get("document"), dict) else body
    dry_run = bool(body.get("dry_run", True))

    version = doc.get("schema_version")
    if version != EXPORT_SCHEMA_VERSION:
        raise HTTPException(
            status_code=422,
            detail=(
                f"配置版本不支持：{version!r}（当前支持 {EXPORT_SCHEMA_VERSION}）。"
                "可能来自更新版本的系统，请用同版本的导出文件。"
            ),
        )
    incoming = doc.get("settings")
    if not isinstance(incoming, dict):
        raise HTTPException(status_code=422, detail="文件里没有 settings")

    # 永不接受密钥
    incoming = strip_ai_secrets(incoming)
    ai_in = incoming.get("ai")
    if isinstance(ai_in, dict):
        for key in _SECRET_KEYS:
            ai_in.pop(key, None)

    await _validate_settings_patch(db, incoming)

    current = await load_settings(db)
    diff = _settings_diff(current, incoming)
    if dry_run:
        return {
            "dry_run": True,
            "group_count": len(diff),
            "diff": diff,
            "note": "这是预演：没有写入任何东西。确认无误后用 dry_run=false 应用。",
        }

    saved = await save_settings(db, incoming, actor_id=user.id)
    _refresh_runtime_caches(saved)

    from app.models.audit import SettingAuditAction
    from app.services.audit import setting_audit

    db.add(
        setting_audit(
            user_id=user.id,
            username=user.username,
            action=SettingAuditAction.CONFIG_IMPORT,
            summary=f"导入配置档：涉及 {len(diff)} 个设置组",
            diff=diff,
        )
    )
    await db.commit()

    log.warning(
        f"⚠ 配置导入已应用 by {user.username}：涉及 {len(diff)} 个设置组"
    )
    return {
        "dry_run": False,
        "group_count": len(diff),
        "diff": diff,
        "settings": redact_settings(saved),
        "note": "已应用。AI Key 不在导出文件里，需要到「AI 助理」重新填写。",
    }


# ---------------------------------------------------------------------------
# 恢复出厂设置（2026-10-05）—— 高危，只给 admin
# ---------------------------------------------------------------------------

class FactoryResetRequest(BaseModel):
    scope: str = Field("business", description="business / business_settings / all")
    confirm_text: str = Field("", description="必须原样输入「恢复出厂设置」")
    password: str = Field("", description="当前登录账号的密码")
    dry_run: bool = Field(False, description="预演：只统计与备份，不删除")


def _require_admin(user: User) -> None:
    """除了权限矩阵，再显式卡一道：必须是 admin 角色或超级管理员。

    写成两道是因为这个操作**不可撤销**：矩阵将来被人改宽了，这里还拦得住。
    """
    if not (user.is_superuser or str(user.role) == "admin"):
        raise HTTPException(status_code=403, detail="恢复出厂设置仅限管理员账号")


@router.get(
    "/permission-matrix",
    summary="权限矩阵：可配置清单 + 默认值 + 当前生效值",
)
async def permission_matrix(
    user: Annotated[User, Depends(require_permission("setting", "update"))],
) -> dict:
    """给「系统设置 → 权限矩阵」渲染勾选表。

      · `catalog`   —— 资源 → 该资源支持的动作（决定每个资源有几个勾选框）；
      · `defaults`  —— 代码里的默认矩阵（「恢复默认」按钮要用）；
      · `effective` —— 当前实际生效值（默认 + 覆盖）。

    `admin` 恒定全权且不可改，所以不在可编辑角色里。
    """
    from app.core.permissions import (
        Role,
        default_matrix_for,
        effective_matrix,
        permission_catalog,
        permission_overrides_snapshot,
    )
    from app.core.roles import all_roles

    # 可编辑角色 = 内置（除 admin）+ 自定义（2026-10-05）：
    # admin 恒定全权不可改；自定义角色一建好就能在这里配权限。
    roles = [r["key"] for r in all_roles() if r["key"] != str(Role.ADMIN)]
    return {
        "editable_roles": roles,
        "role_labels": {r["key"]: r["name"] for r in all_roles()},
        "custom_roles": [r["key"] for r in all_roles() if not r.get("builtin")],
        "catalog": permission_catalog(),
        "defaults": {role: default_matrix_for(role) for role in roles},
        "effective": {
            role: {res: sorted(acts) for res, acts in effective_matrix(role).items()}
            for role in roles
        },
        "overrides": permission_overrides_snapshot(),
        "admin_note": "admin 恒定拥有全部权限且不可修改（防止把自己锁在门外）",
    }


@router.get("/factory-reset/preview", summary="恢复出厂设置 · 预演（各表行数）")
async def factory_reset_preview(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "reset"))],
    scope: str = Query("business"),
) -> dict:
    from app.services.factory_reset import SCOPES, preview

    _require_admin(user)
    if scope not in SCOPES:
        raise HTTPException(status_code=422, detail=f"未知范围：{scope}")
    return await preview(db, scope=scope)


@router.post(
    "/factory-reset",
    summary="恢复出厂设置（先自动备份；支持 dry_run 预演）",
)
async def factory_reset(
    body: FactoryResetRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(require_permission("setting", "reset"))],
) -> dict:
    from app.services.factory_reset import (
        SCOPES,
        BackupError,
        FactoryResetDisabledError,
        ensure_enabled,
        perform,
    )

    _require_admin(user)
    # 环境闸门（2026-10-08 事故后新增，默认关闭）：
    # 原设计只有"admin + 手打短语 + 密码"，任何拿到 admin 令牌的人/脚本都能
    # 一键清库 —— 而该接口的清理范围连 *_audit_logs 一起删，事后查无可查。
    # 现在未显式开启 ALLOW_FACTORY_RESET 时直接 403，**连密码都不校验**。
    try:
        ensure_enabled()
    except FactoryResetDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    if body.scope not in SCOPES:
        raise HTTPException(status_code=422, detail=f"未知范围：{body.scope}")
    try:
        return await perform(
            db,
            scope=body.scope,
            user=user,
            password=body.password,
            confirm_text=body.confirm_text,
            dry_run=body.dry_run,
        )
    except FactoryResetDisabledError as exc:
        # perform() 里再查一次（防止有人绕过路由直接调服务）
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except PermissionError as exc:
        # 短语/密码不对：401 更贴切（是"没通过校验"），并把原因说清楚
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except BackupError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"备份失败，已中止（不会在没有备份的情况下清库）：{exc}",
        ) from exc
