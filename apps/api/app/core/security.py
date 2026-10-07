"""Security primitives: password hashing and JWT encode/decode."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import settings

# 直接用 bcrypt，不再经过 passlib。
#
# 为什么把 passlib 换掉：passlib 1.7.4（2020 年后再没发版）与 bcrypt>=4.1 不兼容。
# 它在初始化 bcrypt 后端时会跑一个 detect_wrap_bug() 自检，而这个自检会拿一个
# 超过 72 字节的测试口令去调 bcrypt.hashpw；bcrypt 4.1 起不再静默截断，直接抛
#     ValueError: password cannot be longer than 72 bytes
# 于是**后端初始化本身就失败**，hash_password / verify_password 全部不可用
# ——表现为注册 500、登录 500。这不是边界情况，是必现。
# 产物格式两边都是 $2b$<rounds>$...，所以历史上 passlib 生成的哈希仍然能校验。
_BCRYPT_ROUNDS = 12
# bcrypt 的硬上限是 72 **字节**（不是字符）。
_BCRYPT_MAX_BYTES = 72


def _password_bytes(plain: str) -> bytes:
    """把口令编码成 bcrypt 能吃的字节，并按 72 字节截断。

    必须在 bytes 层截断，不能 `encode()[:72].decode()`：
    中文一个字 3 字节，像 "a" + 24 个汉字 共 73 字节，切在第 72 字节会把最后一个
    汉字劈成两半，`.decode("utf-8")` 直接抛 UnicodeDecodeError → 注册接口 500。
    bcrypt 本身只接受 bytes，按字节截断也正是它历史上的语义。
    """
    return plain.encode("utf-8")[:_BCRYPT_MAX_BYTES]


def hash_password(plain: str) -> str:
    """Hash a plain-text password with bcrypt (rounds=12)."""
    return bcrypt.hashpw(_password_bytes(plain), bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode(
        "ascii"
    )


async def verify_password_async(plain: str, hashed: str) -> bool:
    """bcrypt 校验的**线程池版本**（接口必须用这个）。

    bcrypt 故意慢（每次 ~300ms），直接在事件循环里跑会独占整个进程：
    压测实测 20 路并发登录 p50 = 6.3 秒，期间所有请求都被卡住。
    """
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(verify_password, plain, hashed)


async def hash_password_async(plain: str) -> str:
    """bcrypt 加密的线程池版本（建号 / 改密 / 初始化管理员都用它）。"""
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(hash_password, plain)


def verify_password(plain: str, hashed: str) -> bool:
    """Constant-time verify. Returns False on any malformed input rather than 500ing."""
    try:
        return bcrypt.checkpw(_password_bytes(plain), hashed.encode("ascii"))
    except (ValueError, TypeError, UnicodeError):
        return False


def create_access_token(
    subject: str | int,
    *,
    extra_claims: dict[str, Any] | None = None,
    expires_minutes: int | None = None,
) -> str:
    """Mint a JWT.

    Args:
        subject: User id (stored as `sub` claim).
        extra_claims: Additional claims such as `roles`, `tenant_id`.
        expires_minutes: Override default expiry.
    """
    minutes = expires_minutes or settings.jwt_expire_minutes
    now = datetime.now(tz=UTC)
    payload: dict[str, Any] = {
        "sub": str(subject),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=minutes)).timestamp()),
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict[str, Any]:
    """Decode + verify. Raises `jose.JWTError` on bad/expired tokens."""
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


__all__ = [
    "hash_password",
    "verify_password",
    "create_access_token",
    "decode_token",
    "JWTError",
]
