"""角色注册表：内置角色 + 自定义角色（2026-10-05）。

## 为什么单独一个模块

角色 key 以前只存在于 `Role` 枚举里，于是"加一个角色"必须改代码。
现在内置角色仍在枚举里（代码里到处在用），**自定义角色**放在设置里
（`roles.custom`），由本模块合成一份**已知角色清单**：

  · 权限矩阵覆盖、数据可见性、菜单可见资源都按角色 **key（字符串）** 工作，
    所以自定义角色一建好就能在「系统设置」里配权限；
  · 校验只认这份清单 —— 未知角色一律拒绝（用户表里塞个错别字角色，
    等于给了一个没有任何权限、也永远登录不了任何页面的账号）。

与 permissions/visibility 一样用**进程内缓存**：启动加载、保存设置时刷新。
⚠ 单进程部署；将来多 worker 需要共享缓存。
"""
from __future__ import annotations

import re
from typing import Any

from app.core.logging import get_logger

log = get_logger("core.roles")

# 内置角色：key 与 core.permissions.Role 一致，不可删除/改名
BUILTIN_ROLES: tuple[dict[str, Any], ...] = (
    {"key": "admin", "name": "管理员", "builtin": True},
    {"key": "manager", "name": "总经理", "builtin": True},
    {"key": "finance", "name": "财务", "builtin": True},
    {"key": "estimator", "name": "工程/造价", "builtin": True},
    {"key": "purchaser", "name": "采购", "builtin": True},
    {"key": "sales", "name": "销售", "builtin": True},
    {"key": "viewer", "name": "只读", "builtin": True},
)

_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,23}$")
_custom: list[dict[str, Any]] = []


def validate_custom_role(item: Any, *, existing_keys: set[str]) -> tuple[dict | None, str]:
    """校验一条自定义角色定义，返回 (净化后的定义, 错误信息)。"""
    if not isinstance(item, dict):
        return None, "角色定义必须是对象"
    key = str(item.get("key") or "").strip().lower()
    name = str(item.get("name") or "").strip()
    if not _KEY_RE.match(key):
        return None, f"角色标识「{key}」不合法：只能用小写字母/数字/下划线，2~24 位，字母开头"
    if key in {r["key"] for r in BUILTIN_ROLES}:
        return None, f"「{key}」是内置角色，不能重复定义"
    if key in existing_keys:
        return None, f"角色标识「{key}」重复"
    if not name or len(name) > 16:
        return None, "角色名称必填且不超过 16 个字"
    return {"key": key, "name": name, "builtin": False}, ""


def set_custom_roles(items: Any) -> None:
    """刷新自定义角色缓存（启动时 & 保存设置时调用）。非法项丢弃并记日志。"""
    global _custom
    cleaned: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items or []:
        role, err = validate_custom_role(item, existing_keys=seen)
        if role is None:
            log.warning(f"忽略自定义角色定义：{err}")
            continue
        seen.add(role["key"])
        cleaned.append(role)
    _custom = cleaned
    log.info(f"自定义角色已更新：{[r['key'] for r in _custom] or '（无）'}")


def custom_roles_snapshot() -> list[dict[str, Any]]:
    return [dict(r) for r in _custom]


def all_roles() -> list[dict[str, Any]]:
    """内置 + 自定义（给前端下拉、标签用）。"""
    return [dict(r) for r in BUILTIN_ROLES] + [dict(r) for r in _custom]


def known_role_keys() -> set[str]:
    return {r["key"] for r in BUILTIN_ROLES} | {r["key"] for r in _custom}


def is_builtin(key: str) -> bool:
    return any(r["key"] == str(key) for r in BUILTIN_ROLES)


def role_label(key: str) -> str:
    for r in all_roles():
        if r["key"] == str(key):
            return str(r["name"])
    return str(key)


__all__ = [
    "BUILTIN_ROLES",
    "all_roles",
    "custom_roles_snapshot",
    "is_builtin",
    "known_role_keys",
    "role_label",
    "set_custom_roles",
    "validate_custom_role",
]
