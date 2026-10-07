"""Auth router: login (OAuth2 password flow), register, me."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user
from app.core.config import settings
from app.core.logging import get_logger
from app.core.permissions import Role, visible_resources
from app.core.security import (
    create_access_token,
    hash_password,
    verify_password_async,
)
from app.db.session import get_db
from app.models.audit import LoginAuditLog
from app.models.user import User, UserStatus
from app.schemas import LoginRequest, RegisterRequest, TokenResponse, UserOut
from app.services.audit import login_audit

router = APIRouter(prefix="/auth", tags=["认证"])
log = get_logger("api.auth")


# 登录失败锁定（H-8）：窗口 + 次数上限，超限锁一个窗口。
# 计数依据是 `login_audit_logs`，不另建内存计数器 —— 重启不丢、多进程也准。
LOCKOUT_WINDOW_MINUTES = 15
LOCKOUT_MAX_FAILURES = 5


async def _recent_failures(db: AsyncSession, username: str) -> int:
    since = datetime.now(tz=UTC) - timedelta(minutes=LOCKOUT_WINDOW_MINUTES)
    return (
        await db.scalar(
            select(func.count())
            .select_from(LoginAuditLog)
            .where(
                LoginAuditLog.username == (username or "").strip()[:64],
                LoginAuditLog.success.is_(False),
                LoginAuditLog.created_at >= since,
            )
        )
    ) or 0


def _record_login(
    db: AsyncSession, *, username: str, user: User | None, success: bool, reason: str | None = None
) -> None:
    db.add(
        login_audit(
            username=username,
            user_id=user.id if user else None,
            success=success,
            reason=reason,
        )
    )


async def _authenticate(db: AsyncSession, username: str, password: str) -> User:
    """Look up by username OR email, then verify. Raises 401 uniformly.

    失败会写一条登录留痕（含 IP/UA），超过阈值改成 429 锁定。
    """
    failures = await _recent_failures(db, username)
    if failures >= LOCKOUT_MAX_FAILURES:
        _record_login(
            db, username=username, user=None, success=False,
            reason=f"锁定中（{LOCKOUT_WINDOW_MINUTES} 分钟内失败 {failures} 次）",
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"登录失败次数过多，账号已被临时锁定 {LOCKOUT_WINDOW_MINUTES} 分钟；"
                "请稍后再试或联系管理员"
            ),
        )

    stmt = select(User).where((User.username == username) | (User.email == username))
    user = await db.scalar(stmt)
    if not user or not await verify_password_async(password, user.hashed_password):
        _record_login(db, username=username, user=None, success=False, reason="用户名或密码错误")
        # 显式提交：401 是抛异常返回的，不能让"失败计数"跟着响应一起被丢掉
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if user.status != UserStatus.ACTIVE:
        _record_login(
            db, username=username, user=user, success=False, reason=f"账号状态 {user.status}"
        )
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"账号状态为 {user.status}，无法登录",
        )
    return user


async def _issue_token(db: AsyncSession, user: User) -> TokenResponse:
    user.last_login_at = datetime.now(tz=UTC)
    await db.flush()
    token = create_access_token(
        user.id,
        extra_claims={
            "role": str(user.role),
            "username": user.username,
            # 会话版本：强制下线 / 改密码时 +1，已发出的令牌立刻失效
            "ver": int(getattr(user, "token_version", 0) or 0),
        },
    )
    return TokenResponse(
        must_change_password=bool(getattr(user, "must_change_password", False)),
        access_token=token,
        token_type="bearer",
        expires_in=settings.jwt_expire_minutes * 60,
    )


class ChangePasswordBody(BaseModel):
    """改自己的口令（2026-10-05 ✓）。"""

    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/change-password", summary="修改自己的口令")
async def change_password(
    body: ChangePasswordBody,
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """校验旧口令 → 写新口令 → 清掉"必须改口令"标记 → 让其它会话失效 ✓。

    为什么让其它会话失效 ✓：改口令的常见动机就是"怀疑泄露" ✓，
    旧 token 还能用就没意义了 ✓（token_version+1 即可 ✓）。
    """
    if not await verify_password_async(body.old_password, user.hashed_password):
        raise HTTPException(status_code=400, detail="原口令不正确")
    if body.new_password == body.old_password:
        raise HTTPException(status_code=400, detail="新口令不能与原口令相同")
    user.hashed_password = await hash_password_async(body.new_password)
    user.must_change_password = False
    user.token_version = int(user.token_version or 0) + 1
    await db.commit()
    return {"ok": True, "must_change_password": False}


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="OAuth2 表单登录（Swagger 授权按钮用）",
)
async def login_form(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    user = await _authenticate(db, form.username, form.password)
    log.info(f"用户登录 {user.username} ({user.role})")
    _record_login(db, username=user.username, user=user, success=True, reason="表单登录")
    return await _issue_token(db, user)


@router.post("/login/json", response_model=TokenResponse, summary="JSON 登录（前端用）")
async def login_json(
    body: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    user = await _authenticate(db, body.username, body.password)
    log.info(f"用户登录 {user.username} ({user.role})")
    _record_login(db, username=user.username, user=user, success=True, reason="JSON 登录")
    return await _issue_token(db, user)


@router.post(
    "/register",
    response_model=UserOut,
    status_code=status.HTTP_201_CREATED,
    summary="引导式注册（默认关闭；仅空库 + BOOTSTRAP_TOKEN 时可用）",
)
async def register(
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """创建**第一个**管理员。默认关闭，不是一个开放接口。

    为什么收紧：这里原来是完全匿名的开放注册，而"第一个用户"直接是
    `admin` + `is_superuser`。内网里任何人先调一次就能接管系统；即便库里
    已有用户，匿名者也能自建一个 active 的 `sales` 账号（可读全部项目/客户/
    报价单并导出客户版）。设计口径是明确的 —— 报价系统设计方案.md §9.1：
    "内网通常**不开放自助注册**，由管理员创建/停用"。

    现在的准入条件是**三个都满足**：

    1. 后端 `.env` 里配了非空 `BOOTSTRAP_TOKEN`（不配 = 这个接口一律 403）；
    2. 请求体里的 `bootstrap_token` 与之常量时间相等；
    3. 库里**一个用户都没有**（纯引导场景）。

    已有用户后本接口永久 403，建号走管理员接口 `POST /api/v1/users`。
    注册成功后应当立刻把 `.env` 里的 BOOTSTRAP_TOKEN 清空并重启。
    """
    if not settings.bootstrap_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="自助注册已关闭；账号请由管理员创建（POST /api/v1/users）",
        )
    # 常量时间比较：不给"逐字符猜令牌"留下计时侧信道，也避免日志里出现令牌。
    if not secrets.compare_digest(body.bootstrap_token or "", settings.bootstrap_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="引导令牌不正确，或自助注册已关闭",
        )
    if (await db.scalar(select(User).limit(1))) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="系统已有用户，注册通道已关闭；请由管理员创建账号",
        )

    if await db.scalar(select(User).where(User.username == body.username)):
        raise HTTPException(status_code=409, detail="用户名已存在")
    if await db.scalar(select(User).where(User.email == body.email)):
        raise HTTPException(status_code=409, detail="邮箱已注册")

    # 走到这里说明是空库 + 拿着引导令牌 → 第一个用户就是管理员
    user = User(
        username=body.username,
        email=body.email,
        hashed_password=hash_password(body.password),
        full_name=body.full_name,
        department=body.department,
        role=Role.ADMIN,
        status=UserStatus.ACTIVE,
        is_superuser=True,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    log.warning(
        f"引导注册：{user.username} 已成为管理员（请立刻清空 .env 里的 BOOTSTRAP_TOKEN）"
    )
    return user


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def me(user: Annotated[User, Depends(get_current_user)]) -> User:
    return user


@router.get("/me/permissions", summary="当前用户可见资源与动作矩阵")
async def my_permissions(
    user: Annotated[User, Depends(get_current_user)],
) -> dict:
    """Used by the frontend to build menus and hide buttons."""
    from app.core.permissions import (
        PERMISSION_MATRIX,
        can_see_cost,
        can_see_profit,
    )

    actions: dict[str, list[str]] = {}
    for res in visible_resources(user.role):
        allowed = [
            str(a)
            for a, roles in PERMISSION_MATRIX.get(res, {}).items()
            if user.is_superuser or user.role in roles
        ]
        actions[res] = allowed
    return {
        "role": str(user.role),
        "is_superuser": user.is_superuser,
        "resources": sorted(actions.keys()),
        "actions": actions,
        # 字段级可见性（2026-10-05）：前端据此隐藏成本/利润入口与列，
        # 免得"点进去只看到一片 null"或者以为是 bug。
        # 与后端判定同一处（core/visibility），不会出现前后端两套口径。
        "can_see_cost": can_see_cost(
            role=user.role, is_superuser=bool(user.is_superuser)
        ),
        "can_see_profit": can_see_profit(
            role=user.role, is_superuser=bool(user.is_superuser)
        ),
    }

@router.post("/refresh", response_model=TokenResponse, summary="用当前令牌换一张新令牌")
async def refresh_token(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TokenResponse:
    """滑动续期：仍然有效的令牌可以换一张新的。

    不是无状态刷新令牌 —— 要拿得出**当前有效**的令牌才给新的，
    所以令牌一旦过期、或版本被强制下线顶掉，刷新这条路也一起断。
    """
    return await _issue_token(db, user)


@router.post("/logout", summary="登出（记一条会话留痕）")
async def logout(
    user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """登出本身不改令牌版本，只记录一次会话结束。

    要"立刻让所有令牌失效"用管理端强制下线（`POST /users/{id}/force-logout`）。
    """
    _record_login(db, username=user.username, user=user, success=True, reason="登出")
    await db.commit()
    return {"ok": True}


@router.get("/login-logs", summary="登录尝试留痕（管理员）")
async def login_logs(
    db: Annotated[AsyncSession, Depends(get_db)],
    user: Annotated[User, Depends(get_current_user)],
    username: str | None = None,
    only_failed: bool = False,
    limit: int = 100,
) -> list[dict]:
    if not (user.is_superuser or user.role == Role.ADMIN):
        raise HTTPException(status_code=403, detail="只有管理员能查看登录留痕")
    stmt = (
        select(LoginAuditLog)
        .order_by(LoginAuditLog.created_at.desc())
        .limit(max(1, min(limit, 500)))
    )
    if username:
        stmt = stmt.where(LoginAuditLog.username == username)
    if only_failed:
        stmt = stmt.where(LoginAuditLog.success.is_(False))
    rows = (await db.scalars(stmt)).all()
    return [
        {
            "username": r.username,
            "success": r.success,
            "reason": r.reason,
            "ip": r.ip,
            "user_agent": r.user_agent,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]
