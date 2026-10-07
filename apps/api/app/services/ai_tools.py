"""AI 助理能用的工具（2026-10-04）。

## 设计原则

1. **工具就是"我的手"**：每个工具都复用现有服务/查询，不在 AI 这条线上另写一套业务逻辑
   —— 否则会出现"界面上按规则拦住了、AI 却能绕过去"。
2. **按当前用户鉴权**：工具执行时带 `user`，该用户看不到的成本/毛利，工具也不返回
   （`can_see_cost`），权限不足就明确回"你无权查看"，而不是编一个数字。
3. **只读工具直接执行；写操作只产出"待确认"提议**（`propose_*`），
   由前端渲染成确认卡片，人点了才调既有接口落库 —— 沿用"AI 产出一律人工确认"。
4. **返回值要能塞进模型上下文**：一律是紧凑的 dict/list，并且截断（别把 500 条设备灌进去）。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.permissions import can, can_see_cost, can_see_profit
from app.models.customer import Customer
from app.models.device import Device, DevicePriceTier
from app.models.project import Project
from app.models.quote import Quote, QuoteStatus
from app.models.user import User

log = get_logger("services.ai_tools")

MAX_ROWS = 20


def _money(v: Any) -> float:
    try:
        return float(Decimal(str(v or 0)).quantize(Decimal("0.01")))
    except Exception:  # noqa: BLE001
        return 0.0


def _cost_visible(user: User) -> bool:
    return can_see_cost(role=user.role, is_superuser=bool(user.is_superuser))


def _see_profit(user: Any) -> bool:
    """利润（毛利额/毛利率/低毛利单）是**另一档**权限：财务看得到，采购看不到。"""
    return can_see_profit(role=user.role, is_superuser=bool(user.is_superuser))


def _device_brief(d: Device, *, with_cost: bool) -> dict[str, Any]:
    out = {
        "code": d.code,
        "name": d.name,
        "brand": d.brand,
        "model": d.model,
        "unit": d.unit,
        "reference_price": _money(d.reference_price),
        "status": str(d.status),
        "min_price": _money(d.min_price) if d.min_price is not None else None,
    }
    if with_cost:
        out["cost_price"] = _money(d.cost_price)
        out["last_inquiry_price"] = (
            _money(d.last_inquiry_price) if d.last_inquiry_price is not None else None
        )
    return out


# ---------------------------------------------------------------------------
# 只读工具
# ---------------------------------------------------------------------------

async def tool_search_devices(
    db: AsyncSession, user: User, *, keyword: str = "", limit: int = 10
) -> dict[str, Any]:
    """按名称/型号/品牌模糊搜设备库。"""
    if not can(user.role, "device_library", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看设备库"}
    stmt = select(Device)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(Device.name.ilike(like), Device.model.ilike(like),
                Device.brand.ilike(like), Device.code.ilike(like))
        )
    rows = (await db.scalars(stmt.order_by(Device.code).limit(min(limit, MAX_ROWS)))).all()
    with_cost = _cost_visible(user)
    return {
        "count": len(rows),
        "cost_visible": with_cost,
        "devices": [_device_brief(d, with_cost=with_cost) for d in rows],
        "hint": None if rows else "没找到；可以换个关键词，或让我用型号搜",
    }


async def tool_get_device(
    db: AsyncSession, user: User, *, code_or_model: str
) -> dict[str, Any]:
    """查一台设备的详情（含阶梯价）。"""
    if not can(user.role, "device_library", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看设备库"}
    key = (code_or_model or "").strip()
    d = await db.scalar(
        select(Device).where(
            or_(Device.code == key, Device.model == key, Device.model.ilike(f"%{key}%"))
        ).limit(1)
    )
    if d is None:
        return {"error": f"设备库里没有「{key}」"}
    info = _device_brief(d, with_cost=_cost_visible(user))
    tiers = (
        await db.scalars(
            select(DevicePriceTier)
            .where(DevicePriceTier.device_id == d.id)
            .order_by(DevicePriceTier.min_qty)
        )
    ).all()
    info["price_tiers"] = [
        {
            "min_qty": float(t.min_qty),
            "max_qty": float(t.max_qty) if t.max_qty is not None else None,
            "unit_price": _money(t.unit_price),
        }
        for t in tiers
    ]
    return info


async def tool_search_projects(
    db: AsyncSession, user: User, *, keyword: str = "", limit: int = 10
) -> dict[str, Any]:
    """按项目号/名称/客户搜项目。"""
    if not can(user.role, "project", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看项目"}
    stmt = select(Project)
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(Project.code.ilike(like), Project.name.ilike(like),
                Project.customer_name.ilike(like))
        )
    rows = (await db.scalars(stmt.order_by(Project.code.desc()).limit(min(limit, MAX_ROWS)))).all()
    return {
        "count": len(rows),
        "projects": [
            {
                "code": p.code,
                "name": p.name,
                "customer": p.customer_name,
                "status": str(p.status),
                "type": str(p.project_type),
            }
            for p in rows
        ],
    }


async def tool_search_customers(
    db: AsyncSession, user: User, *, keyword: str = "", limit: int = 10
) -> dict[str, Any]:
    """搜客户档案。"""
    if not can(user.role, "customer", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看客户档案"}
    stmt = select(Customer).where(Customer.is_active.is_(True))
    if keyword.strip():
        like = f"%{keyword.strip()}%"
        stmt = stmt.where(
            or_(Customer.name.ilike(like), Customer.short_name.ilike(like),
                Customer.contact_name.ilike(like))
        )
    rows = (await db.scalars(stmt.order_by(Customer.code).limit(min(limit, MAX_ROWS)))).all()
    return {
        "count": len(rows),
        "customers": [
            {
                "code": c.code,
                "name": c.name,
                "contact": c.contact_name,
                "phone": c.contact_phone,
            }
            for c in rows
        ],
    }


async def tool_get_quote(db: AsyncSession, user: User, *, code: str) -> dict[str, Any]:
    """按单号看报价单摘要（状态、金额、行数）。"""
    if not can(user.role, "quote", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看报价单"}
    q = await db.scalar(select(Quote).where(Quote.code == (code or "").strip()))
    if q is None:
        return {"error": f"没有单号为「{code}」的报价单"}
    item_count = await db.scalar(
        select(func.count()).select_from(Quote).where(Quote.id == q.id)
    )
    out = {
        "code": q.code,
        "title": q.title,
        "status": str(q.status),
        "project_id": str(q.project_id),
        "equipment_total": _money(q.equipment_total),
        "install_total": _money(q.install_total),
        "auxiliary_total": _money(q.auxiliary_total),
        "total_with_tax": _money(q.total_with_tax),
        "approved_at": q.approved_at.isoformat() if getattr(q, "approved_at", None) else None,
        "item_count": int(item_count or 0),
    }
    if _cost_visible(user):
        out["cost_total"] = _money(q.cost_total)
    # 毛利是**另一档**权限：财务/总经理看得到，采购只看进价，工程与销售都不看
    if _see_profit(user):
        out["gross_profit"] = _money(q.gross_profit)
        out["gross_profit_rate"] = float(q.gross_profit_rate or 0)
    elif _cost_visible(user):
        out["profit_note"] = "当前账号看不到毛利（利润权限由「系统设置 → 数据可见性」配置）"
    return out


async def tool_list_quotes(
    db: AsyncSession, user: User, *, status: str | None = None, limit: int = 10
) -> dict[str, Any]:
    """列最近的报价单（可按状态过滤：draft/submitted/reviewing/approved/rejected）。"""
    if not can(user.role, "quote", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看报价单"}
    stmt = select(Quote).order_by(Quote.created_at.desc())
    if status:
        try:
            stmt = stmt.where(Quote.status == QuoteStatus(status))
        except ValueError:
            return {
                "error": f"状态「{status}」不认识"
                "（可用：draft/submitted/reviewing/approved/rejected）"
            }
    rows = (await db.scalars(stmt.limit(min(limit, MAX_ROWS)))).all()
    return {
        "count": len(rows),
        "cost_visible": _cost_visible(user),
        "quotes": [
            {
                "code": q.code,
                "title": q.title,
                "status": str(q.status),
                "total_with_tax": _money(q.total_with_tax),
                **(
                    {"gross_profit": _money(q.gross_profit),
                     "gross_profit_rate": float(q.gross_profit_rate or 0)}
                    if _see_profit(user)
                    else {}
                ),
            }
            for q in rows
        ],
    }


async def tool_business_overview(
    db: AsyncSession, user: User, *, months: int = 6
) -> dict[str, Any]:
    """经营概况：已批准报价的项目数/金额/成本/毛利/毛利率。无成本权限时不含毛利。"""
    if not can(user.role, "quote", "read") and not user.is_superuser:
        return {"error": "当前账号无权查看报价数据"}
    rows = (
        await db.scalars(select(Quote).where(Quote.status == QuoteStatus.APPROVED))
    ).all()
    latest: dict[str, Quote] = {}
    for q in rows:
        cur = latest.get(str(q.project_id))
        if cur is None or (q.approved_at or q.created_at) > (cur.approved_at or cur.created_at):
            latest[str(q.project_id)] = q
    amount = sum((Decimal(str(q.total_with_tax or 0)) for q in latest.values()), Decimal("0"))
    out: dict[str, Any] = {
        "approved_projects": len(latest),
        "amount": _money(amount),
        "note": "每个项目只取最近一次已批准的报价（改过价不会重复计业绩）",
    }
    if _cost_visible(user):
        cost = sum((Decimal(str(q.cost_total or 0)) for q in latest.values()), Decimal("0"))
        out["cost"] = _money(cost)
    if _see_profit(user):
        profit = sum((Decimal(str(q.gross_profit or 0)) for q in latest.values()), Decimal("0"))
        out.update({
            "gross_profit": _money(profit),
            "gross_profit_rate": float(profit / amount) if amount else 0.0,
        })
    if not _cost_visible(user) and not _see_profit(user):
        out["note"] += "；当前账号看不到成本与毛利"
    elif not _see_profit(user):
        out["note"] += "；当前账号看不到毛利（利润权限未开放）"
    elif not _cost_visible(user):
        out["note"] += "；当前账号看不到成本明细"
    return out


async def tool_low_margin_quotes(
    db: AsyncSession, user: User, *, max_rate: float = 0.15, limit: int = 10
) -> dict[str, Any]:
    """找毛利偏低或亏损的已批准报价（需要**利润**权限）。"""
    if not _see_profit(user):
        return {
            "error": "当前账号没有利润权限，无法做低毛利分析"
            "（利润可见性由「系统设置 → 数据可见性」按角色配置）"
        }
    rows = (
        await db.scalars(
            select(Quote)
            .where(Quote.status == QuoteStatus.APPROVED)
            .order_by(Quote.gross_profit_rate)
            .limit(200)
        )
    ).all()
    hit = [q for q in rows if float(q.gross_profit_rate or 0) < max_rate]
    return {
        "threshold_rate": max_rate,
        "count": len(hit[: min(limit, MAX_ROWS)]),
        "quotes": [
            {
                "code": q.code,
                "title": q.title,
                "total_with_tax": _money(q.total_with_tax),
                **({"cost_total": _money(q.cost_total)} if _cost_visible(user) else {}),
                "gross_profit": _money(q.gross_profit),
                "gross_profit_rate": float(q.gross_profit_rate or 0),
            }
            for q in hit[: min(limit, MAX_ROWS)]
        ],
    }


async def tool_check_price(
    db: AsyncSession, user: User, *, code_or_model: str, unit_price: float
) -> dict[str, Any]:
    """给定报价单价，检查是否低于该设备底价（低于则说明需要经理授权+理由）。"""
    base = await tool_get_device(db, user, code_or_model=code_or_model)
    if base.get("error"):
        return base
    floor = base.get("min_price")
    price = Decimal(str(unit_price))
    if floor is None:
        return {"device": base.get("code"), "floor": None,
                "verdict": "该设备没设底价，不拦截", "unit_price": float(price)}
    floor_d = Decimal(str(floor))
    return {
        "device": base.get("code"),
        "name": base.get("name"),
        "floor": float(floor_d),
        "unit_price": float(price),
        "below_floor": price < floor_d,
        "verdict": (
            f"低于底价 {float(floor_d - price):.2f}，需要经理/管理员授权并写明理由"
            if price < floor_d
            else "不低于底价，可以直接报"
        ),
    }


# ---------------------------------------------------------------------------
# 写操作：只产出"待确认"提议，绝不直接改数据
# ---------------------------------------------------------------------------

async def tool_propose_device_cost(
    db: AsyncSession, user: User, *, code_or_model: str, new_cost: float, reason: str = ""
) -> dict[str, Any]:
    """提议更新某设备的进价（不会执行，需要用户在卡片上确认）。

    两道门（2026-10-05）：
      ① 有设备库写权限（与界面一致，避免"界面改不了、让 AI 改却能改"）；
      ② **能看成本** —— 看不到进价的人不该去改进价（闭着眼睛改价）。
    """
    if not can(user.role, "device_library", "update") and not user.is_superuser:
        return {"error": "当前账号无权修改设备库"}
    if not _cost_visible(user):
        return {
            "error": "当前账号看不到成本/进价，无法提议改进价"
            "（成本可见性由「系统设置 → 数据可见性」按角色配置）"
        }
    base = await tool_get_device(db, user, code_or_model=code_or_model)
    if base.get("error"):
        return base
    return {
        "kind": "confirm_update_device_cost",
        "needs_confirmation": True,
        "device": {"code": base.get("code"), "name": base.get("name"),
                   "model": base.get("model")},
        "current_cost": base.get("cost_price"),
        "new_cost": float(new_cost),
        "reason": reason,
        "how_to_apply": "用户在卡片上点「确认应用」后，前端调用 PATCH /devices/{id}",
        "note": "我没有直接改数据；确认前设备库保持原样",
    }


async def tool_get_parse_candidates(
    db: AsyncSession, user: User, *, job_id: str, limit: int = 20
) -> dict[str, Any]:
    """读一次"供应商报价解析"的候选行（用户在对话里传了文件时会用到）。

    只读候选，不落库 —— 落库仍然要用户在界面上确认。
    """
    from uuid import UUID

    from app.models.ai import AiParseJob

    try:
        jid = UUID(str(job_id))
    except ValueError:
        return {"error": "解析任务 id 不合法"}
    job = await db.get(AiParseJob, jid)
    if job is None:
        return {"error": "找不到这个解析任务"}
    # 解析是后台跑的：还没完就如实说"还在解析中"，别让模型拿空结果编内容
    if job.status not in ("ready", "committed"):
        note = {
            "pending": "这份文件还在解析中（后台跑着），稍等一下再问我要候选行。",
            "failed": "这份文件解析失败了，看看 warnings 里的原因，或让用户重传一份。",
        }.get(job.status, f"任务状态是 {job.status}，暂时读不到候选行。")
        return {
            "job_id": str(job.id),
            "status": job.status,
            "warnings": ((job.result or {}).get("warnings") or [])[:5],
            "note": note,
            "items": [],
        }
    # 别人的解析任务不给看（里面是供应商报价原文）
    if job.created_by and job.created_by != user.id and not user.is_superuser:
        return {"error": "这是别人上传的解析任务，无权查看"}
    result = job.result or {}
    items = (result.get("items") or [])[: min(limit, MAX_ROWS)]
    with_cost = _cost_visible(user)
    return {
        "job_id": str(job.id),
        "status": job.status,
        "engine": result.get("engine"),
        "filename": job.filename,
        "warnings": (result.get("warnings") or [])[:5],
        "items": [
            {
                "row": it.get("row"),
                "name": it.get("name"),
                "brand": it.get("brand"),
                "model": it.get("model"),
                "unit": it.get("unit"),
                "quantity": it.get("quantity"),
                "unit_price": it.get("unit_price"),
                "verified": it.get("verified"),
                "needs_review": it.get("needs_review"),
                "matched_device": (it.get("match") or {}).get("code"),
                "match_type": (it.get("match") or {}).get("match_type"),
                **(
                    {"reference_price": (it.get("match") or {}).get("reference_price")}
                    if with_cost
                    else {}
                ),
            }
            for it in items
        ],
        "note": "这些是候选行；要真正改进价，需要用户在解析卡片上点确认",
    }


# ---------------------------------------------------------------------------
# 注册表 + OpenAI tools 定义
# ---------------------------------------------------------------------------

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

TOOLS: dict[str, dict[str, Any]] = {
    "search_devices": {
        "handler": tool_search_devices,
        "kind": "read",
        "label": "搜索设备库",
        "description": (
            "按名称/型号/品牌/编号模糊搜索企业设备库，返回参考价"
            "（有成本权限时还返回进价、最近询价、底价）。"
            "用户问某型号、某品牌、要找设备时用它。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "keyword": {"type": "string", "description": "关键词，如型号或名称片段"},
                "limit": {"type": "integer", "description": "返回条数，默认 10，最多 20"},
            },
            "required": ["keyword"],
        },
    },
    "get_device": {
        "handler": tool_get_device,
        "kind": "read",
        "label": "查设备详情",
        "description": "按设备编号或型号查一台设备的详情，包含阶梯价档位。",
        "parameters": {
            "type": "object",
            "properties": {"code_or_model": {"type": "string"}},
            "required": ["code_or_model"],
        },
    },
    "search_projects": {
        "handler": tool_search_projects,
        "kind": "read",
        "label": "搜索项目",
        "description": "按项目号/项目名/客户名搜索项目。",
        "parameters": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["keyword"],
        },
    },
    "search_customers": {
        "handler": tool_search_customers,
        "kind": "read",
        "label": "搜索客户",
        "description": "搜索客户档案（名称/简称/联系人）。",
        "parameters": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["keyword"],
        },
    },
    "get_quote": {
        "handler": tool_get_quote,
        "kind": "read",
        "label": "查报价单",
        "description": "按单号（如 QT-202609-0001）查看报价单状态与金额；有成本权限时含毛利。",
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string"}},
            "required": ["code"],
        },
    },
    "list_quotes": {
        "handler": tool_list_quotes,
        "kind": "read",
        "label": "列报价单",
        "description": (
            "列最近的报价单，可按状态过滤"
            "（draft/submitted/reviewing/approved/rejected）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "status": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    "business_overview": {
        "handler": tool_business_overview,
        "kind": "read",
        "label": "经营概况",
        "description": "已批准报价的项目数/金额（有成本权限时含成本、毛利、毛利率）。",
        "parameters": {"type": "object", "properties": {"months": {"type": "integer"}}},
    },
    "low_margin_quotes": {
        "handler": tool_low_margin_quotes,
        "kind": "read",
        "label": "查低毛利单",
        "description": "找出毛利低于给定比率的已批准报价（默认 15%），需要成本权限。",
        "parameters": {
            "type": "object",
            "properties": {"max_rate": {"type": "number"}, "limit": {"type": "integer"}},
        },
    },
    "check_price": {
        "handler": tool_check_price,
        "kind": "read",
        "label": "核价（对底价）",
        "description": "给定设备与报价单价，判断是否低于底价、是否需要授权。",
        "parameters": {
            "type": "object",
            "properties": {
                "code_or_model": {"type": "string"},
                "unit_price": {"type": "number"},
            },
            "required": ["code_or_model", "unit_price"],
        },
    },
    "get_parse_candidates": {
        "handler": tool_get_parse_candidates,
        "kind": "read",
        "label": "读解析候选",
        "description": (
            "读取一次供应商报价解析任务的候选行（用户在对话里上传文件后会产生）。"
            "当用户说「这份报价怎么样」「哪些行有问题」时用它。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "解析任务 id"},
                "limit": {"type": "integer"},
            },
            "required": ["job_id"],
        },
    },
    "propose_device_cost": {
        "handler": tool_propose_device_cost,
        "kind": "write",
        "label": "提议改进价",
        "description": (
            "当用户要求把某设备进价改成某个值（例如按供应商报价更新）时用它。"
            "**它不会真的改数据**，只产出一张待确认卡片，用户确认后才生效。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "code_or_model": {"type": "string"},
                "new_cost": {"type": "number"},
                "reason": {"type": "string", "description": "为什么改（如：XX 供应商 10 月报价）"},
            },
            "required": ["code_or_model", "new_cost"],
        },
    },
}


def openai_tool_specs(names: list[str] | None = None) -> list[dict[str, Any]]:
    """转成 OpenAI 兼容的 tools 参数。"""
    out = []
    for name, spec in TOOLS.items():
        if names is not None and name not in names:
            continue
        out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec["description"],
                "parameters": spec["parameters"],
            },
        })
    return out


async def run_tool(
    db: AsyncSession, user: User, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """执行一个工具。异常一律转成 {error}，不让它炸掉整段对话。"""
    spec = TOOLS.get(name)
    if spec is None:
        return {"error": f"没有名为「{name}」的工具"}
    try:
        return await spec["handler"](db, user, **(arguments or {}))
    except TypeError as exc:
        return {"error": f"工具参数不对：{exc}"}
    except Exception as exc:  # noqa: BLE001
        log.warning(f"工具 {name} 执行失败：{type(exc).__name__}: {exc}")
        return {"error": f"工具执行失败（{type(exc).__name__}）"}
