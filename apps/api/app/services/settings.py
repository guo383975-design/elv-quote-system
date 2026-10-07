"""系统设置的默认值与读写（2026-10-04）。

默认值**必须**和以前硬编码的常量一致，否则上线那一刻所有导出文件的抬头/条款会变
—— 这种"静默变化"最难排查。以前写死的地方：

  · `export_data.QUOTE_CLAUSES` / `QUOTE_VALID_DAYS`（报价说明、有效期 30 天）
  · `export_excel.py` / `export_pdf.py` 里的「XX科技有限公司」
"""

from __future__ import annotations

import copy
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.setting import AppSetting

# 设置项的键
K_COMPANY = "company"
K_QUOTE = "quote"
K_PROJECT = "project"
K_APPROVAL = "approval"
K_PERMISSIONS = "permissions"
K_ROLES = "roles"

# 说明：成本/利润的可见性（2026-10-05 起）**不再是独立设置组** ——
# 它们就是权限矩阵里的两行字段级资源（data_cost / data_profit），
# 跟着 `permissions.overrides` 一起存、一起刷新（用户要求把两者合并成一个界面）。

K_AI = "ai"

# 新建项目默认带上的专业（可在「系统设置」里改；清空 = 不自动建）
DEFAULT_SUBSYSTEMS: list[str] = [
    "video_surveillance",   # 视频监控
    "access_control",       # 门禁一卡通
    "intrusion",            # 入侵报警
    "structured_cabling",   # 综合布线
    "network",              # 计算机网络
    "broadcast",            # 公共广播
    "conference",           # 会议系统
    "server_room",          # 机房工程
    "parking",              # 停车管理
    "info_publish",         # 信息发布
]

DEFAULTS: dict[str, Any] = {
    # 自定义角色（2026-10-05）：内置角色不在这里，见 core/roles.py
    K_ROLES: {"custom": []},
    # 权限矩阵覆盖（2026-10-05）：空 = 全部用代码里的默认矩阵。
    # 结构：{role: {resource: [actions]}}，admin 不参与（防自锁）。
    K_PERMISSIONS: {"overrides": {}},
    K_COMPANY: {
        "name": "XX科技有限公司",
        "address": "宁波市鄞州区 XX 路 88 号",
        "phone": "0574-8888 8888",
        "email": "sales@xxzn.com",
    },
    K_PROJECT: {
        # 新建项目时自动创建的专业（值 = SubsystemType 的值，顺序即 sort_order）
        "default_subsystems": list(DEFAULT_SUBSYSTEMS),
    },
    K_AI: {
        # AI 助理（2026-10-04）。默认关闭：不配置 key 就什么都不发出去。
        "enabled": False,
        # 只需要 OpenAI 兼容协议：DeepSeek / 通义 / Moonshot / 本地 vLLM 都吃这一套
        "provider": "openai_compatible",
        "base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-chat",
        # 图片 / 扫描件用的多模态模型（留空 = 用上面的 model）
        "vision_model": "",
        "temperature": 0.1,
        "timeout_seconds": 120,
        # 发出去之前是否隐去客户名/联系人（合规开关）
        "redact_customer": False,
        # ---- 以下两项是密钥相关，**绝不返回前端** ----
        # 加密后的 API Key（见 services/ai_crypto.py）
        "api_key_cipher": "",
        # 掩码提示，只用来显示"配了哪个 key"（如 sk-****cdef）
        "api_key_hint": "",
        # 最近一次连通性测试
        "last_test_ok": None,
        "last_test_at": None,
        "last_test_error": None,
    },
    K_APPROVAL: {
        # 默认**关闭**：开启后"批准"才需要走分级审批链（见 services/approval.py）。
        # 之所以默认关：直接改会让既有审批习惯和既有调用全部 409。
        "enabled": False,
        # 按含税合计分级：min_amount 是下限（含），levels 是需要几级审批
        "tiers": [
            {"min_amount": 0, "levels": 1},
            {"min_amount": 100000, "levels": 2},
            {"min_amount": 500000, "levels": 3},
        ],
        # 驳回是否必须写意见（默认必须 —— 不写意见的打回是最常见的返工来源）
        "reject_requires_comment": True,
    },
    K_QUOTE: {
        # 条款里的数字用变量引用这些结构化字段（见 services/quote_terms.py）——
        # 改这里一处，所有报价单的条款文本自动跟着变，不会再出现
        # "抬头写有效期 45 天、条款还写着 30 天"的自相矛盾。
        "valid_days": 30,
        "prepay_ratio": 30,
        "accept_ratio": 60,
        "warranty_ratio": 10,
        "warranty_months": 12,
        "build_days": 45,
        "clauses": [
            "本报价含设备供货、辅材、安装调试、系统联调及首年免费维保；",
            "报价有效期 {valid_days} 天；设备价格随市场波动，逾期需重新确认；",
            "付款方式：合同签订后预付 {prepay_ratio}%，验收合格付 {accept_ratio}%，"
            "质保金 {warranty_ratio}% 一年后付；",
            "工期：合同签订并收到预付款后 {build_days} 个日历天内完成安装调试；",
            "质保期：整体系统 {warranty_months} 个月，设备按厂家标准执行；",
            "本报价不含：土建配合、桥架施工、强电引至点位、运营商线路费用。",
        ],
    },
}


def _merge(default: Any, stored: Any) -> Any:
    """字典逐键合并（只覆盖出现过的键），其余类型直接取存储值。"""
    if isinstance(default, dict) and isinstance(stored, dict):
        out = dict(default)
        out.update(stored)
        return out
    return stored if stored is not None else default


async def load_settings(db: AsyncSession) -> dict[str, Any]:
    """读全部设置（缺的用默认值补齐）。任何调用点都不会拿到 None。"""
    rows = (await db.scalars(select(AppSetting))).all()
    # deepcopy 而不是 dict()：`dict(DEFAULTS)` 是浅拷贝，未落库的组会和 DEFAULTS
    # **共享同一个嵌套字典** —— 调用方原地改一下就把全局默认值改了（跨请求串味）。
    out = copy.deepcopy(DEFAULTS)
    for row in rows:
        if row.key in out:
            out[row.key] = _merge(out[row.key], row.value)
        else:
            out[row.key] = row.value
    return out


async def load_export_settings(db: AsyncSession) -> dict[str, Any]:
    """导出/预览用的一小撮（公司抬头 + 有效期 + 条款）。"""
    s = await load_settings(db)
    company = dict(DEFAULTS[K_COMPANY])
    company.update(s.get(K_COMPANY) or {})
    quote = dict(DEFAULTS[K_QUOTE])
    quote.update(s.get(K_QUOTE) or {})
    return {"company": company, "quote": quote}


def refresh_runtime_caches(saved: dict[str, Any]) -> None:
    """把设置推进进程内缓存（角色清单 + 权限矩阵覆盖）。

    单独抽出来是为了让**三个地方**共用：保存设置、导入配置、以及
    `main.py` 里的**定期刷新任务**。

    ⚠ 为什么需要定期刷新：这些缓存是**进程内**的。单进程时"保存即生效"成立；
    一旦起多个 worker，改权限只刷新了被请求到的那个 worker，
    其它 worker 会继续按旧权限判 —— 撤销权限不生效就是安全问题。
    定期从库里重读（默认 10 秒）能让多 worker 部署也**最终一致**。
    """
    from app.core.permissions import set_permission_overrides
    from app.core.roles import set_custom_roles

    set_custom_roles((saved.get("roles") or {}).get("custom") or [])
    set_permission_overrides((saved.get("permissions") or {}).get("overrides") or {})


async def save_settings(
    db: AsyncSession, patch: dict[str, Any], *, actor_id: UUID | None = None
) -> dict[str, Any]:
    """按组保存（只覆盖传入的键）。返回保存后的全量设置。"""
    current = await load_settings(db)
    for key, value in patch.items():
        if value is None:
            continue
        merged = _merge(current.get(key, DEFAULTS.get(key)), value)
        row = await db.scalar(select(AppSetting).where(AppSetting.key == key))
        if row is None:
            db.add(AppSetting(key=key, value=merged, updated_by=actor_id))
        else:
            row.value = merged
            row.updated_by = actor_id
        current[key] = merged
    await db.commit()
    return current



# 绝不返回前端的字段（AI Key 的密文本身也不能给：拿到密文等于拿到口令的一半）
_SECRET_FIELDS = ("api_key_cipher",)


def redact_settings(settings: dict) -> dict:
    """给客户端的版本：剥掉密钥，只留"配没配、配的是哪个"。

    为什么放在服务层而不是接口层：**任何**把设置吐给前端的地方都得过这一关，
    少写一处就是一次泄露。所以做成默认动作，接口层调用它。
    """
    out = copy.deepcopy(settings)
    ai = out.get(K_AI)
    if isinstance(ai, dict):
        cipher = ai.pop("api_key_cipher", "") or ""
        ai["api_key_set"] = bool(cipher)
        ai["api_key_hint"] = ai.get("api_key_hint") or ""
        for field in _SECRET_FIELDS:
            ai.pop(field, None)
    return out

# AI 组里**只能**通过 `/ai/config` 写的字段（走加密 + 审计）。
# 通用 `PATCH /settings` 能写的话，密文字段会被直接覆盖成明文，
# 或者有人传个假密文让 key 失效 —— 两种都不是我们想要的。
_AI_SECRET_FIELDS = ("api_key_cipher", "api_key_hint", "last_test_ok", "last_test_at",
                     "last_test_error")


def strip_ai_secrets(patch: dict[str, Any]) -> dict[str, Any]:
    """从通用设置补丁里剔除 AI 的密钥/测试字段（保留其它 AI 配置）。"""
    out = dict(patch)
    ai = out.get(K_AI)
    if isinstance(ai, dict):
        cleaned = {k: v for k, v in ai.items() if k not in _AI_SECRET_FIELDS}
        if cleaned:
            out[K_AI] = cleaned
        else:
            out.pop(K_AI, None)
    return out
