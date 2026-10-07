"""RBAC permission matrix. Resource × Action → Role sets.

Roles:
    admin      — full
    purchaser  — 采购：设备库/物料导入 + 供应商维护
    estimator  — 设计/造价：设备库+人工费+定额 CRUD + 报价清单
    sales      — 销售：项目+报价单 CRUD；设备库只读
    manager    — 经理：报价审核/批准（只读+审批+重算对齐，不建不改报价）
    finance    — 财务：只读，对内成本视图可见
    viewer     — 只读客户版

Resources:
    user, project, subsystem, device_library, quote, quote_item,
    labor_rate, quota, supplier, point_template, setting, audit_log, customer, ai
"""

from __future__ import annotations

from enum import StrEnum

from app.core.logging import get_logger
from app.core.roles import known_role_keys

log = get_logger("core.permissions")


class Role(StrEnum):
    ADMIN = "admin"
    SALES = "sales"
    ESTIMATOR = "estimator"
    FINANCE = "finance"
    VIEWER = "viewer"
    PURCHASER = "purchaser"
    MANAGER = "manager"


class Action(StrEnum):
    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    EXPORT = "export"
    APPROVE = "approve"
    COMPUTE = "compute"  # 重算报价、跑工艺系数
    CONFIGURE = "configure"  # 系统级配置（AI 的模型与密钥）
    USE = "use"          # 使用某种能力（如调用 AI 助理）
    RESET = "reset"      # 恢复出厂设置（高危，只给 admin）


# Permission matrix: {resource: {action: set(roles)}}
PERMISSION_MATRIX: dict[str, dict[Action, set[Role]]] = {
    "user": {
        Action.READ: {Role.ADMIN, Role.FINANCE},
        Action.CREATE: {Role.ADMIN},
        Action.UPDATE: {Role.ADMIN},
        Action.DELETE: {Role.ADMIN},
    },
    "project": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.VIEWER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.SALES},
        Action.UPDATE: {Role.ADMIN, Role.SALES},
        Action.DELETE: {Role.ADMIN},
        Action.EXPORT: {Role.ADMIN, Role.SALES, Role.FINANCE},
    },
    "subsystem": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.VIEWER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
        Action.UPDATE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
        Action.DELETE: {Role.ADMIN, Role.SALES},
    },
    "device_library": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE,
            Role.VIEWER, Role.PURCHASER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.ESTIMATOR, Role.PURCHASER},
        Action.UPDATE: {Role.ADMIN, Role.ESTIMATOR, Role.PURCHASER},
        Action.DELETE: {Role.ADMIN},
        Action.EXPORT: {Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.PURCHASER},
    },
    "quote": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.VIEWER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
        Action.UPDATE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
        Action.DELETE: {Role.ADMIN},
        Action.COMPUTE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.MANAGER},
        # 审批人必须能重算：明细被人改过、或口径变更后，审批时看到的数字
        # 如果是旧的，经理既看不准也改不了（他不能改明细）。只读 + 可重算，
        # 不等于可编辑 —— 数据仍然只能由销售/设计改。
        Action.APPROVE: {Role.ADMIN, Role.MANAGER},
        # 造价员需要导出（带工艺系数的核对单常要交付给销售/财务/审计）；
        # viewer 仍然只能读不能导出。
        Action.EXPORT: {Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.MANAGER},
    },
    "quote_item": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.VIEWER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
        Action.UPDATE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
        Action.DELETE: {Role.ADMIN, Role.SALES, Role.ESTIMATOR},
    },
    "labor_rate": {
        Action.READ: {Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.MANAGER},
        Action.CREATE: {Role.ADMIN, Role.ESTIMATOR},
        Action.UPDATE: {Role.ADMIN, Role.ESTIMATOR},
        Action.DELETE: {Role.ADMIN},
    },
    "quota": {
        Action.READ: {Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE, Role.MANAGER},
        Action.CREATE: {Role.ADMIN, Role.ESTIMATOR},
        Action.UPDATE: {Role.ADMIN, Role.ESTIMATOR},
        Action.DELETE: {Role.ADMIN},
    },
    # 点位模板（2026-10-04）：一个点位的物料构成 + 安装单价（元/点）。
    # 与设备库同权：设计/造价要能建模板，采购也能维护；销售与只读角色只能看。
    "point_template": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE,
            Role.VIEWER, Role.PURCHASER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.ESTIMATOR, Role.PURCHASER},
        Action.UPDATE: {Role.ADMIN, Role.ESTIMATOR, Role.PURCHASER},
        Action.DELETE: {Role.ADMIN},
    },
    # 系统设置（2026-10-04）：公司抬头 / 报价有效期 / 报价说明条款 —— 改一次影响所有导出。
    # 读给管理岗（经理也要知道当前口径），写只给 admin。
    "setting": {
        Action.READ: {Role.ADMIN, Role.MANAGER, Role.ESTIMATOR},
        Action.UPDATE: {Role.ADMIN},
        # 高危：清库。只给 admin —— 前端也对其它角色隐藏这个入口。
        Action.RESET: {Role.ADMIN},
    },
    # ---- 字段级资源（2026-10-05）----
    # 只有「查看」一个动作：**进得去模块 ≠ 看得到里面的成本/毛利**。
    # 放进这张表是为了让"所有可见性在同一个界面配、判定只有 can() 一处"，
    # 从此不再有独立的「数据可见性」设置（用户要求合并）。
    #   成本/进价：admin、财务、采购、总经理
    #   利润/毛利：admin、财务、总经理
    "data_cost": {
        Action.READ: {Role.ADMIN, Role.FINANCE, Role.PURCHASER, Role.MANAGER},
    },
    "data_profit": {
        Action.READ: {Role.ADMIN, Role.FINANCE, Role.MANAGER},
    },
    "ai": {
        # `configure` 只给 admin（用户明确要求：API 只有 admin 能设置与管理）——
        # 能改 base_url 的人可以把你们的报价数据导到他自己的服务器上，
        # 所以这条不是"权限洁癖"，是数据出口。
        # `use` 给一线：销售/造价/采购用得上，经理批单时也能用。
        Action.READ: {Role.ADMIN},
        Action.CONFIGURE: {Role.ADMIN},
        Action.USE: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.PURCHASER, Role.MANAGER,
        },
    },
    "customer": {
        # 客户档案全员可读（报价/项目页面要显示客户名），写权限给销售与管理岗：
        # 客户是销售的一手信息，让造价/采购去维护不现实。
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE,
            Role.VIEWER, Role.PURCHASER, Role.MANAGER,
        },
        Action.CREATE: {Role.ADMIN, Role.SALES, Role.MANAGER},
        Action.UPDATE: {Role.ADMIN, Role.SALES, Role.MANAGER},
        Action.DELETE: {Role.ADMIN},
    },
    "supplier": {
        Action.READ: {
            Role.ADMIN, Role.SALES, Role.ESTIMATOR, Role.FINANCE,
            Role.VIEWER, Role.PURCHASER, Role.MANAGER,
        },
        # CREATE/UPDATE 不给 sales —— 销售只查可用供应商；新增/修订由 admin/estimator/purchaser 维护
        Action.CREATE: {Role.ADMIN, Role.ESTIMATOR, Role.PURCHASER},
        Action.UPDATE: {Role.ADMIN, Role.ESTIMATOR, Role.PURCHASER},
        Action.DELETE: {Role.ADMIN},
    },
    "audit_log": {
        Action.READ: {Role.ADMIN, Role.FINANCE},
        Action.CREATE: {Role.ADMIN},
    },
}


# ---------------------------------------------------------------------------
# 权限覆盖层（2026-10-05）：界面上可调，默认矩阵不变
# ---------------------------------------------------------------------------

_OVERRIDES: dict[str, dict[str, frozenset[str]]] = {}


def set_permission_overrides(overrides: dict | None) -> None:
    """设置/刷新覆盖层。传 None 或空 dict = 全部回到默认矩阵。

    只接受**合法资源与动作**（以默认矩阵为准）：写错的一律丢弃并记日志 ——
    权限这种东西，宁可少给也不能因为一个错别字多给。
    """
    global _OVERRIDES
    if not overrides:
        _OVERRIDES = {}
        log.info("权限覆盖已清空，回到默认矩阵")
        return

    cleaned: dict[str, dict[str, frozenset[str]]] = {}
    for role_name, res_map in overrides.items():
        if str(role_name) == str(Role.ADMIN):
            # admin 恒定全权：不参与覆盖（防自锁）
            log.warning("忽略对 admin 的权限覆盖请求（admin 恒定拥有全部权限）")
            continue
        if str(role_name) not in known_role_keys():
            log.warning(f"忽略未知角色的权限覆盖：{role_name}")
            continue
        if not isinstance(res_map, dict):
            continue
        per_res: dict[str, frozenset[str]] = {}
        for res, actions in res_map.items():
            valid = PERMISSION_MATRIX.get(str(res))
            if not valid:
                log.warning(f"忽略未知资源的权限覆盖：{res}")
                continue
            allowed_actions = {str(a) for a in valid}
            picked = {str(a) for a in (actions or [])} & allowed_actions
            dropped = {str(a) for a in (actions or [])} - allowed_actions
            if dropped:
                log.warning(f"忽略 {role_name}.{res} 的非法动作：{sorted(dropped)}")
            per_res[str(res)] = frozenset(picked)
        cleaned[str(role_name)] = per_res
    _OVERRIDES = cleaned
    log.info(f"权限覆盖已更新，涉及角色：{sorted(cleaned)}")


def permission_overrides_snapshot() -> dict[str, dict[str, list[str]]]:
    """当前覆盖（只读，给接口与前端用）。"""
    return {
        role: {res: sorted(acts) for res, acts in res_map.items()}
        for role, res_map in _OVERRIDES.items()
    }


def effective_matrix(role: Role | str) -> dict[str, set[str]]:
    """某角色**实际生效**的 资源 → 动作集合（默认矩阵 + 覆盖）。

    自定义角色没有默认矩阵，所以没配过覆盖时返回空（要显式勾）。
    """
    role_name = str(getattr(role, "value", role))
    if role_name not in known_role_keys():
        return {}
    # 默认矩阵 + 覆盖（**按资源合并**：覆盖的行以覆盖为准，其余仍走默认）
    merged: dict[str, set[str]] = {}
    for res, actions in PERMISSION_MATRIX.items():
        picked = {
            str(a)
            for a, allowed in actions.items()
            if role_name in {str(x) for x in allowed}
        }
        if picked:
            merged[res] = picked
    override = _OVERRIDES.get(role_name)
    if override:
        for res, acts in override.items():
            if acts:
                merged[res] = set(acts)
            else:
                merged.pop(res, None)   # 显式清空 = 这一行什么都不给
    return merged


def default_matrix_for(role: Role | str) -> dict[str, list[str]]:
    """某角色在**代码默认矩阵**里的权限（给前端渲染"默认值/恢复默认"用）。"""
    role_key = str(getattr(role, "value", role))
    out: dict[str, list[str]] = {}
    for res, actions in PERMISSION_MATRIX.items():
        picked = sorted(
            str(a)
            for a, allowed in actions.items()
            if role_key in {str(x) for x in allowed}
        )
        if picked:
            out[res] = picked
    return out


def permission_catalog() -> dict[str, list[str]]:
    """可配置的清单：资源 → 该资源支持的动作（前端渲染勾选表用）。"""
    return {
        res: [str(a) for a in actions]
        for res, actions in PERMISSION_MATRIX.items()
    }


def can(role: Role | str, resource: str, action: Action | str) -> bool:
    """Check whether a role can perform an action on a resource.

    Lookup is strict: unknown role/action/resource → False.
    Admin wins on any action of a **known** resource.
    """
    # 角色可以是**自定义角色**（core/roles.py 的清单），所以按字符串校验，
    # 不再要求必须命中 Role 枚举 —— 否则新建的角色永远拿不到权限。
    role_key = str(getattr(role, "value", role))
    if role_key not in known_role_keys():
        return False
    try:
        a = Action(action)
    except ValueError:
        return False
    r = role_key
    # 资源必须先存在，再给 admin 放行。
    # 之前 `if r == ADMIN: return True` 写在资源校验之前，于是
    # can(ADMIN, "拼错的资源名", READ) 也返回 True —— 与本文档字符串
    # 「unknown resource → False」自相矛盾，而且前端用 can() 控制菜单/按钮时，
    # 一个拼错的资源名会静默变成「可见」，把 bug 藏起来。
    matrix = PERMISSION_MATRIX.get(resource)
    if not matrix:
        return False
    if r == str(Role.ADMIN):
        # admin 恒定全权，且**不可被覆盖** —— 这是防自锁的底线
        return True
    override = _OVERRIDES.get(r)
    if override is not None and resource in override:
        # **按资源合并**（2026-10-05 起）：只覆盖勾过的资源行，没勾过的仍走默认矩阵。
        # 以前是"整角色替换" —— 那样"只想给财务开利润"会连带冻结它的功能权限。
        return str(a) in override[resource]
    try:
        enum_role = Role(r)
    except ValueError:
        # 自定义角色且没配过这一行 → 什么都不给（要显式勾）
        return False
    allowed = matrix.get(a, set())
    return enum_role in allowed


def visible_resources(role: Role | str) -> set[str]:
    """All resources a role can READ. Useful for menu generation.

    **字段级资源（data_cost / data_profit）不在这里** —— 它们不是能导航的模块，
    出现在菜单/资源清单里只会让人困惑。
    """
    role_key = str(getattr(role, "value", role))
    if role_key not in known_role_keys():
        return set()
    if role_key == str(Role.ADMIN):
        return set(PERMISSION_MATRIX.keys()) - FIELD_RESOURCES
    override = _OVERRIDES.get(role_key)
    if override is not None:
        # 被覆盖的角色：菜单 = 覆盖里给了 read 的行 + 没被覆盖但默认有 read 的行
        visible = {
            res
            for res, acts in override.items()
            if str(Action.READ) in acts and res not in FIELD_RESOURCES
        }
        try:
            enum_role = Role(role_key)
        except ValueError:
            return visible
        for res, actions in PERMISSION_MATRIX.items():
            if res in FIELD_RESOURCES or res in override:
                continue
            if enum_role in actions.get(Action.READ, set()):
                visible.add(res)
        return visible
    try:
        enum_role = Role(role_key)
    except ValueError:
        # 自定义角色且没配过权限 → 菜单里什么都不出现
        return set()
    return {
        res
        for res, actions in PERMISSION_MATRIX.items()
        if res not in FIELD_RESOURCES and enum_role in actions.get(Action.READ, set())
    }


# ---------------------------------------------------------------------------
# 字段级可见性（2026-10-05 起就是矩阵里的 data_cost / data_profit 两行）
# ---------------------------------------------------------------------------
FIELD_RESOURCES: frozenset[str] = frozenset({"data_cost", "data_profit"})


def can_see_cost(*, role: Role | str, is_superuser: bool = False) -> bool:
    """能否看到**成本 / 进价**（内部版导出的成本列也走这一档）。

    与「系统设置 → 权限矩阵」里 `成本/进价` 那一行**完全同一处判定** ——
    以前有独立的「数据可见性」设置，两处口径容易漂移，现已合并。
    """
    if is_superuser:
        return True
    return can(role, "data_cost", Action.READ)


def can_see_profit(*, role: Role | str, is_superuser: bool = False) -> bool:
    """能否看到**利润**（毛利额/毛利率/利润看板/低毛利分析）。"""
    if is_superuser:
        return True
    return can(role, "data_profit", Action.READ)


__all__ = [
    "FIELD_RESOURCES", "PERMISSION_MATRIX", "Action", "Role",
    "can", "can_see_cost", "can_see_profit", "visible_resources",
    "effective_matrix", "permission_catalog", "permission_overrides_snapshot",
    "set_permission_overrides",
]


