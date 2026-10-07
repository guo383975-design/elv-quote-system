"""FastAPI dependency: get current user from JWT, check role."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import can
from app.core.security import decode_token
from app.db.session import get_db
from app.models.user import User, UserStatus

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=True)

_credentials_exc = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="无法验证身份",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Decode JWT, fetch user. Raises 401 on bad token or missing user."""
    try:
        payload = decode_token(token)
        subject = payload.get("sub")
        if not subject:
            raise _credentials_exc
        user_id = UUID(str(subject))
    except (JWTError, ValueError):
        raise _credentials_exc from None

    user = await db.scalar(select(User).where(User.id == user_id))
    if not user or user.status != UserStatus.ACTIVE:
        raise _credentials_exc

    # 会话版本校验（2026-10-04，H-8）：强制下线 / 改密码会把 users.token_version +1，
    # 老令牌里的 ver 就对不上了 → 立刻失效，不用等它自然过期。
    token_ver = int(payload.get("ver", 0) or 0)
    if token_ver != int(getattr(user, "token_version", 0) or 0):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="会话已失效（管理员强制下线或口令已变更），请重新登录",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def require_roles(*roles: str):
    """Factory for role-check dependency."""

    async def _checker(
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if str(user.role) not in roles and not user.is_superuser:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"需要角色: {', '.join(roles)}",
            )
        return user

    return _checker


def require_permission(resource: str, action: str):
    """Factory for fine-grained permission check (uses RBAC matrix)."""

    async def _checker(
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if not can(user.role, resource, action):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"权限不足: {resource}.{action}",
            )
        return user

    return _checker


__all__ = [
    "get_current_user",
    "require_roles",
    "require_permission",
    "oauth2_scheme",
]

