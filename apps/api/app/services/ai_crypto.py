"""AI API Key 的加密存储（2026-10-04）。

## 为什么必须加密

API Key 泄露 = 别人拿你的账号刷公网模型（而且账单记在你头上）。
数据库被翻（备份文件、误导出、SQL 注入）时，明文 key 等于直接送出。

## 密钥从哪来

从 `JWT_SECRET` 派生（HKDF 类似的"拼盐再哈希"，不新增必填环境变量）。
代价要写清楚：**换了 JWT_SECRET 就要重新录入 AI Key**（旧密文解不开）——
这比"多一个必须记住配置的环境变量"更不容易在部署时漏掉。

## 对外一律脱敏

`mask()` 只露出前 3 位与后 4 位（`sk-…cdef`），前端只看得到这个。
"""

from __future__ import annotations

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger("services.ai_crypto")

_SALT = "|quote-system-ai-config-v1"


def _fernet() -> Fernet:
    seed = (settings.jwt_secret or "") + _SALT
    key = base64.urlsafe_b64encode(hashlib.sha256(seed.encode("utf-8")).digest())
    return Fernet(key)


def encrypt(plain: str) -> str:
    """明文 → 密文（base64 字符串，可直接进 JSONB）。空串返回空串。"""
    if not plain:
        return ""
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt(token: str) -> str:
    """密文 → 明文。解不开（换过 JWT_SECRET / 数据被改）返回空串并告警，不抛异常。"""
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError) as exc:
        log.warning(f"AI Key 解密失败（换过 JWT_SECRET？需重新录入）：{type(exc).__name__}")
        return ""


def mask(plain: str) -> str:
    """脱敏展示：只留前 3 位与后 4 位。"""
    if not plain:
        return ""
    if len(plain) <= 8:
        return "*" * len(plain)
    return f"{plain[:3]}{'*' * 6}{plain[-4:]}"


def looks_like_key(value: str) -> bool:
    """粗校验：避免把一句话当成 key 存进去。"""
    v = (value or "").strip()
    return len(v) >= 16 and " " not in v
