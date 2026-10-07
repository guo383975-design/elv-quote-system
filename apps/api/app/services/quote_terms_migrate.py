"""老数据升级：把"还是旧默认文本"的条款换成变量版（2026-10-05）。

## 为什么需要

`app_settings` 一旦存过值，就**盖住**了代码里的默认值 —— 这是"默认值 vs 已存值"
的经典坑。真实情况：用户重置后（当时默认条款还是写死数字的版本）保存过一次设置，
于是那批字面文本被持久化；之后我把默认值改成变量版，**对他们不生效**，
条款里的数字依旧不会跟着字段变（上机验证就是这么抓到的）。

## 分寸

只替换**与旧默认逐字相同**的那几条 —— 用户自己改写过的条款一个字都不碰
（那种情况会保留原文，并在返回里报告"未升级"，而不是偷偷覆盖）。
"""

from __future__ import annotations

from typing import Any

# 旧版默认条款（写死数字）。**必须与历史默认逐字一致**，否则匹配不上、不会升级。
LEGACY_DEFAULT_CLAUSES: tuple[str, ...] = (
    "本报价含设备供货、辅材、安装调试、系统联调及首年免费维保；",
    "报价有效期 30 天；设备价格随市场波动，逾期需重新确认；",
    "付款方式：合同签订后预付 30%，验收合格付 60%，质保金 10% 一年后付；",
    "工期：合同签订并收到预付款后 45 个日历天内完成安装调试；",
    "质保期：整体系统 12 个月，设备按厂家标准执行；",
    "本报价不含：土建配合、桥架施工、强电引至点位、运营商线路费用。",
)

# 升级后的变量版（与 settings.DEFAULTS 同源；这里直接引用，避免再抄一份）
def _new_default_clauses() -> list[str]:
    from app.services.settings import DEFAULTS, K_QUOTE

    return list(DEFAULTS[K_QUOTE]["clauses"])


def upgrade_legacy_default_clauses(quote_group: dict[str, Any] | None) -> tuple[dict, bool]:
    """把「仍是旧默认」的条款升级成变量版，并补齐缺失的结构化数字。

    返回 `(新的 quote 组, 是否改动)`。**用户改写过就不动**。
    """
    group = dict(quote_group or {})
    changed = False

    stored = group.get("clauses")
    if isinstance(stored, list) and [str(c) for c in stored] == list(LEGACY_DEFAULT_CLAUSES):
        group["clauses"] = _new_default_clauses()
        changed = True

    # 结构化数字：缺就补默认（只补不改：用户设过的保持原值）
    from app.services.quote_terms import DEFAULT_TERMS

    for name, value in DEFAULT_TERMS.items():
        if group.get(name) is None:
            group[name] = value
            changed = True

    return group, changed
