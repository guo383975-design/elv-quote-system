"""登录失败锁定（H-8）的行为测试（2026-10-09）。

这套锁原来是**一个测试都没有**的：代码写了、审计报告也标了"已修"，
但没有任何东西守着它。而且口径与模型注释不一致 ——
注释写"按 username + IP"，代码只按 username，于是：

    attacker: 对 victim 连打 5 次错口令 → victim 本人在自己的电脑上被锁 15 分钟

    这不是防爆破，是**攻击者零成本把同事挡在门外**。

现在按「账号 + 来源 IP」成对计数，另有单 IP 的兜底上限。这里守四条。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import (
    LOCKOUT_MAX_FAILURES,
    LOCKOUT_MAX_FAILURES_PER_IP,
    LOCKOUT_WINDOW_MINUTES,
)
from app.models.audit import LoginAuditLog


def _ip(addr: str) -> dict[str, str]:
    return {"X-Real-IP": addr}


async def _login(client: AsyncClient, username: str, password: str, addr: str):
    return await client.post(
        "/api/v1/auth/login/json",
        json={"username": username, "password": password},
        headers=_ip(addr),
    )


async def test_failures_from_same_ip_lock_that_pair(
    client: AsyncClient, admin_user
) -> None:
    for _ in range(LOCKOUT_MAX_FAILURES):
        r = await _login(client, "admin", "wrong-password", "10.0.0.9")
        assert r.status_code == 401, r.text

    # 再试（即使口令是对的）→ 429
    r = await _login(client, "admin", "admin12345", "10.0.0.9")
    assert r.status_code == 429, r.text
    assert str(LOCKOUT_WINDOW_MINUTES) in r.json()["detail"]


async def test_lockout_does_not_follow_the_account_to_another_ip(
    client: AsyncClient, admin_user
) -> None:
    """关键回归：别人在别的机器上爆破，**不能**把本人锁在门外。"""
    for _ in range(LOCKOUT_MAX_FAILURES):
        assert (await _login(client, "admin", "wrong-password", "10.0.0.66")).status_code == 401

    # 同账号、但来自本人所在的 IP → 正常登录
    r = await _login(client, "admin", "admin12345", "203.0.113.117")
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]


async def test_single_ip_has_a_higher_backstop(
    client: AsyncClient, admin_user
) -> None:
    """一个 IP 换着用户名撞口令，到上限后这个 IP 一律 429。"""
    for i in range(LOCKOUT_MAX_FAILURES_PER_IP):
        r = await _login(client, f"nobody{i}", "wrong-password", "10.1.2.3")
        assert r.status_code == 401, f"第 {i + 1} 次应当还是 401，得到 {r.status_code}"

    r = await _login(client, "admin", "admin12345", "10.1.2.3")
    assert r.status_code == 429, r.text

    # 别的 IP 不受影响
    assert (await _login(client, "admin", "admin12345", "10.1.2.4")).status_code == 200


async def test_failed_attempt_is_recorded_with_ip(
    client: AsyncClient, admin_user, db: AsyncSession
) -> None:
    await _login(client, "admin", "wrong-password", "10.9.9.9")

    rows = (
        await db.scalars(
            select(LoginAuditLog)
            .where(LoginAuditLog.username == "admin", LoginAuditLog.success.is_(False))
            .order_by(LoginAuditLog.created_at.desc())
        )
    ).all()
    assert rows, "失败尝试必须留痕"
    assert rows[0].ip == "10.9.9.9", "留痕里要能看出是从哪台机器试的"
