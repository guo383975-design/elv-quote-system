"""留痕构造助手（2026-10-04，H-10）。

原来各处以 `QuoteAuditLog(...)` 直接构造，**没有人填 IP/UA，也没有单号快照**
→ 一旦报价单被删（外键 CASCADE）整条留痕跟着消失，事后无法回答"谁在什么时候删了哪张单"。

现在统一走这里：
  · 自动带上当前请求的 IP / User-Agent（`request_context`）；
  · 自动把 `quote_code` / `project_code` 快照进 `subject_code` ——
    外键改成 `ON DELETE SET NULL` 之后，删单留下的留痕仍然看得出是哪张单。

2026-10-09 起多一类：`device_audit()` 给**设备库**留痕 ✓（此前只有报价单/项目/
登录/设置有留痕 ✗，改设备的价格与停用状态查不到人 ✗）。设备库的对象是
`devices` / `device_categories`，所以快照字段落在 `subject_code`（编号/代号）
与 `subject_name`（名称）上，并且 `operator_id` / `entity_id` **刻意不加外键** ✓
（`factory_reset scope=all` 会清空设备与账号，留痕必须活下来 ✓）。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import Enum as PyEnum
from typing import Any
from uuid import UUID

from app.core.request_context import current_request_context
from app.models.audit import (
    AuditAction,
    DeviceAuditAction,
    DeviceAuditLog,
    LoginAuditLog,
    ProjectAuditLog,
    QuoteAuditLog,
    SettingAuditAction,
    SettingAuditLog,
)


def _ctx() -> tuple[str | None, str | None]:
    c = current_request_context()
    return c.ip, c.user_agent


def plain(value: Any) -> Any:
    """把 ORM 值转成能进 JSON 列的**纯**值。

    留痕的 payload 会被 `json.dumps`/JSONB 写入，Decimal / UUID / 枚举 /
    datetime 直接塞进去要么抛 TypeError，要么依赖各驱动自己的兜底编码器
    （同一份数据在 PG 与 SQLite 上落成两种形状 ✗）。所以统一在这里转一次：

      · Decimal → 定长字符串（**不能用 float** ✗：金额 680.10 变 680.099999… ✗
        留痕的第一用途就是"对得上钱"，精度丢了这条痕就废了 ✗）；
      · UUID → 字符串（与 subject_code / 接口返回一致，人眼可读 ✓）；
      · 枚举 → `.value`（与库里的取值一致，不是成员名 ✓）；
      · datetime → ISO 字符串（带时区 ✓）。
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, PyEnum):
        return str(value.value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return str(value)


def field_changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    """逐字段比 before/after，只留下**真的变了**的字段。

    口径与 `SettingAuditLog` 的 diff 一致 ✓（`[{field, from, to}]` ✓）：
    事后要能一眼看出"改的是哪几个字段、从什么变成什么"，
    而不是把整行前后端两份糊在一起让人自己找 ✗。
    值都过 `plain()` ✓（Decimal 保精度 ✓）。
    """
    out: list[dict[str, Any]] = []
    for field, old in before.items():
        new = after.get(field)
        a, b = plain(old), plain(new)
        if a != b:
            out.append({"field": field, "from": a, "to": b})
    return out


def device_audit(
    *,
    entity: str,
    action: DeviceAuditAction | str,
    entity_id: UUID | None = None,
    code: str | None = None,
    name: str | None = None,
    user_id: UUID | None = None,
    username: str | None = None,
    changes: list[dict[str, Any]] | None = None,
    summary: str | None = None,
) -> DeviceAuditLog:
    """设备库留痕（新增 / 修改 / 停用 / 分类增删改）。

    调用方负责：① 与业务写操作**同一个事务**（`db.add` 后一起 flush/commit ✓
    —— "改了但没记"和"记了但没改"都不允许 ✓）；
    ② `changes` 用 `field_changes()` 算好 ✓（本函数只做搬运 ✓）。
    """
    ip, ua = _ctx()
    return DeviceAuditLog(
        entity=str(entity),
        entity_id=entity_id,
        subject_code=(code or None),
        subject_name=(name or None),
        operator_id=user_id,
        operator_username=(username or None),
        action=str(action),
        summary=(summary or "")[:512] or None,
        changes={"fields": changes or []},
        ip=ip,
        user_agent=ua,
    )


def quote_audit(
    *,
    quote_id: UUID | None,
    quote_code: str | None = None,
    user_id: UUID | None,
    action: AuditAction,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> QuoteAuditLog:
    ip, ua = _ctx()
    return QuoteAuditLog(
        quote_id=quote_id,
        subject_code=quote_code,
        user_id=user_id,
        action=action,
        payload=payload or {},
        note=note,
        ip=ip,
        user_agent=ua,
    )


def project_audit(
    *,
    project_id: UUID | None,
    project_code: str | None = None,
    user_id: UUID | None,
    action: AuditAction,
    payload: dict[str, Any] | None = None,
    note: str | None = None,
) -> ProjectAuditLog:
    ip, ua = _ctx()
    return ProjectAuditLog(
        project_id=project_id,
        subject_code=project_code,
        user_id=user_id,
        action=action,
        payload=payload or {},
        note=note,
        ip=ip,
        user_agent=ua,
    )


def login_audit(
    *,
    username: str,
    user_id: UUID | None,
    success: bool,
    reason: str | None = None,
) -> LoginAuditLog:
    """登录尝试留痕。既是审计，也是**登录失败锁定**的计数依据（见 auth.py）。"""
    ip, ua = _ctx()
    return LoginAuditLog(
        username=(username or "").strip()[:64],
        user_id=user_id,
        success=success,
        reason=(reason or None),
        ip=ip,
        user_agent=ua,
    )


def setting_audit(
    *,
    user_id: UUID | None,
    username: str | None,
    action: SettingAuditAction,
    summary: str | None = None,
    diff: list[dict[str, Any]] | None = None,
) -> SettingAuditLog:
    """设置/权限变更留痕。

    `diff` 用 `_settings_diff` 算好的结构（组 + 字段 + 前后值）。
    **调用方负责不要把密钥塞进来** —— 这里只做搬运。
    """
    ip, ua = _ctx()
    return SettingAuditLog(
        user_id=user_id,
        username=(username or "")[:64] or None,
        action=str(action),
        summary=(summary or "")[:512] or None,
        payload={"diff": diff or []},
        ip=ip,
        user_agent=ua,
    )
