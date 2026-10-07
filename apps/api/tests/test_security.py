"""口令哈希 / JWT 的单元测试。

这里的用例都是**真实踩过的坑**，不是凑覆盖率：
  1. passlib 1.7.4 与 bcrypt>=4.1 不兼容 —— 后端初始化就抛 ValueError，
     所有 hash/verify 全废（注册/登录 500）。
  2. `encode("utf-8")[:72].decode("utf-8")` 在多字节字符被劈开时抛
     UnicodeDecodeError —— 混合中英文的长口令必现。
"""

from __future__ import annotations

import bcrypt

from app.core.security import (
    create_access_token,
    decode_token,
    hash_password,
    verify_password,
)


def test_hash_and_verify_roundtrip() -> None:
    h = hash_password("admin12345")
    assert h.startswith("$2b$12$"), f"bcrypt 哈希格式不对: {h[:10]}"
    assert verify_password("admin12345", h) is True
    assert verify_password("admin12346", h) is False


def test_same_password_hashes_differently() -> None:
    """每次都要有独立 salt，否则相同口令在库里是同一个哈希。"""
    assert hash_password("admin12345") != hash_password("admin12345")


def test_password_beyond_72_bytes_is_truncated_not_crashing() -> None:
    """超过 72 字节的口令必须能正常哈希/校验（bcrypt 按字节截断）。"""
    long_a = "A" * 100
    long_b = "A" * 72 + "B" * 28  # 前 72 字节与 long_a 相同
    h = hash_password(long_a)
    assert verify_password(long_a, h) is True
    # 两者前 72 字节一致，按 bcrypt 语义等价 —— 这是已知取舍，不是 bug
    assert verify_password(long_b, h) is True


def test_multibyte_password_at_truncation_boundary() -> None:
    """回归：'a' + 24 个汉字 = 73 字节。

    72 字节的切点落在一个汉字的中间，旧实现 `[:72].decode("utf-8")` 直接抛
    UnicodeDecodeError，注册接口 500。现在在 bytes 层截断，不应抛异常。
    """
    mixed = "a" + "密" * 24
    assert len(mixed.encode("utf-8")) == 73  # 1 + 3*24

    h = hash_password(mixed)  # 不能抛
    assert verify_password(mixed, h) is True
    assert verify_password("a" + "密" * 25, h) is True   # 同样截断到 72 字节
    assert verify_password("b" + "密" * 24, h) is False


def test_pure_chinese_password_shorter_than_limit() -> None:
    h = hash_password("宁波弱电报价系统")
    assert verify_password("宁波弱电报价系统", h) is True


def test_verify_rejects_malformed_hash_without_raising() -> None:
    """脏哈希（历史数据、手工改库）只能返回 False，不能 500。"""
    for bad in ["", "not-a-hash", "$2b$12$tooshort", "x" * 200]:
        assert verify_password("admin12345", bad) is False


def test_hash_is_verifiable_by_bare_bcrypt() -> None:
    """确认产物与 bcrypt 库本身互通（换实现时不能锁死在自家格式里）。"""
    h = hash_password("admin12345")
    assert bcrypt.checkpw(b"admin12345", h.encode("ascii")) is True


def test_jwt_roundtrip_and_tamper() -> None:
    token = create_access_token("user-123", extra_claims={"role": "admin"})
    payload = decode_token(token)
    assert payload["sub"] == "user-123"
    assert payload["role"] == "admin"

    # 改一个字符就应该验签失败
    tampered = token[:-2] + ("aa" if not token.endswith("aa") else "bb")
    try:
        decode_token(tampered)
    except Exception:
        pass
    else:  # pragma: no cover - 篡改必须被发现
        raise AssertionError("被篡改的 token 竟然验签通过了")
