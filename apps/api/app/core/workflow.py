"""状态机：项目 / 报价单的合法流转（2026-10-02）。

## 项目状态

项目原来是一个裸 `setattr` —— `draft → won` 这种跳变也允许，没有校验、
没有留痕。项目没有审批链，所以这里管的是"哪些流转在业务上说得通"：

- **正常推进**：草稿 → 报价中 → 已报价 → 已中标 / 已失单 → 已归档
- **允许回退**（填错了、客户改主意）：报价中 → 草稿、已报价 → 报价中、
  已中标 → 已报价、已失单 → 报价中。这些在真实业务里都会发生；
  堵死只会逼人到库里改数据，那还不如让它在系统里留下痕迹。
- **归档是收敛态**：任何状态都能归档，但**只有管理员**能把它改出去。

## 报价单状态

报价单原来**只有权限约束**（有 approve 权限就能批准），于是 `草稿 → 已批准`
是放行的 —— 业务上这是错的：「必须先提交才能批准」。现在补上流转表，
并且明确**每个流转需要哪个动作**（提交是编制者的活，批准/驳回是审批人的活）：

- 提交审核、撤回、改完重新提交 → `quote.update`（销售 / 设计）
- 批准 / 驳回 / 开始审核 / 退回草稿 / 归档已批准的单 → `quote.approve`（经理）

这样"权限"与"方向"两件事都有人管：权限管**谁**能点，流转表管**现在**能不能点。
"""

from __future__ import annotations

from app.models.project import ProjectStatus
from app.models.quote import QuoteStatus

DRAFT = ProjectStatus.DRAFT
QUOTING = ProjectStatus.QUOTING
QUOTED = ProjectStatus.QUOTED
WON = ProjectStatus.WON
LOST = ProjectStatus.LOST
ARCHIVED = ProjectStatus.ARCHIVED

PROJECT_STATUS_TRANSITIONS: dict[ProjectStatus, set[ProjectStatus]] = {
    DRAFT: {QUOTING, ARCHIVED},
    QUOTING: {QUOTED, DRAFT, LOST, ARCHIVED},
    QUOTED: {WON, LOST, QUOTING, ARCHIVED},
    WON: {QUOTED, ARCHIVED},
    LOST: {QUOTING, ARCHIVED},
    # 空集合：离开归档要管理员（见 can_move_project）
    ARCHIVED: set(),
}

# 中文标签只用于错误提示；界面上的正式标签来自 app/core/labels.py
_STATUS_LABEL = {
    DRAFT: "草稿",
    QUOTING: "报价中",
    QUOTED: "已报价",
    WON: "已中标",
    LOST: "已失单",
    ARCHIVED: "已归档",
}


# 全局业务顺序（下拉排序用）
_ORDER = [DRAFT, QUOTING, QUOTED, WON, LOST, ARCHIVED]


def ordered_transitions(states: set[ProjectStatus]) -> list[ProjectStatus]:
    """按全局业务顺序排（草稿 → 报价中 → 已报价 → 已中标/已失单 → 已归档）。

    界面下拉直接用这个顺序；直接输出 set 的话顺序是随机的，用户每次刷新
    看到的选项都在跳。
    """
    return [s for s in _ORDER if s in states]


def ordered_targets(
    current: ProjectStatus | str, *, is_admin: bool = False
) -> list[ProjectStatus]:
    """当前状态可改成的状态，**前进方向在前、回退在后**。

    下拉里第一个应该是"下一步该干的事"（报价中 → 已报价），而不是"退回草稿"。
    两者都对，但顺序会影响用户点错概率。
    """
    cur = ProjectStatus(current)
    allowed = allowed_project_transitions(cur, is_admin=is_admin)
    ci = _ORDER.index(cur)
    forward = [s for s in _ORDER if s in allowed and _ORDER.index(s) > ci]
    backward = [s for s in _ORDER if s in allowed and _ORDER.index(s) < ci]
    return forward + backward


def allowed_project_transitions(
    current: ProjectStatus | str, *, is_admin: bool = False
) -> set[ProjectStatus]:
    """从当前状态可以去的状态集合（界面用它渲染可选值）。"""
    cur = ProjectStatus(current)
    if is_admin and cur is ARCHIVED:
        # 管理员可以把归档改回草稿（结案后发现要返工）
        return {DRAFT}
    return set(PROJECT_STATUS_TRANSITIONS.get(cur, set()))


def can_move_project(
    current: ProjectStatus | str, target: ProjectStatus | str, *, is_admin: bool = False
) -> bool:
    """能否把项目从 current 改到 target。相同状态永远放行（不是变更）。"""
    cur, tgt = ProjectStatus(current), ProjectStatus(target)
    if cur is tgt:
        return True
    # ⚠ 归档的例外要**先**看 is_admin，再交给流转表判断：
    # 第一版把 `if cur is ARCHIVED: return False` 写在前面，于是管理员也退不出去，
    # allowed_project_transitions 里那段 is_admin 分支成了死代码（测试抓到）。
    return tgt in allowed_project_transitions(cur, is_admin=is_admin)


def project_transition_error(
    current: ProjectStatus | str, target: ProjectStatus | str, *, is_admin: bool = False
) -> str:
    """给用户看的 409 文案：说清楚"从这儿能到哪儿"，别只说"不允许"。"""
    cur, tgt = ProjectStatus(current), ProjectStatus(target)
    if cur is ARCHIVED:
        return (
            f"项目已归档，只有管理员能把它改回「{_STATUS_LABEL[DRAFT]}」"
            f"（当前不能改成「{_STATUS_LABEL[tgt]}」）"
        )
    names = "、".join(
        _STATUS_LABEL[s] for s in ordered_targets(cur, is_admin=is_admin)
    ) or "（无）"
    return (
        f"项目状态不能从「{_STATUS_LABEL[cur]}」直接改成「{_STATUS_LABEL[tgt]}」；"
        f"当前可改为：{names}"
    )


# ===========================================================================
# 报价单状态机
# ===========================================================================

QD = QuoteStatus.DRAFT
QREV = QuoteStatus.REVIEWING
QSUB = QuoteStatus.SUBMITTED
QAPP = QuoteStatus.APPROVED
QREJ = QuoteStatus.REJECTED
QARC = QuoteStatus.ARCHIVED

QUOTE_ORDER = [QD, QSUB, QREV, QAPP, QREJ, QARC]

_QUOTE_LABEL = {
    QD: "草稿",
    QSUB: "已提交",
    QREV: "审核中",
    QAPP: "已批准",
    QREJ: "已驳回",
    QARC: "已归档",
}

# 每个流转需要哪个动作。**这是"业务要求必须先提交才能批准"的落点**：
# 没有 (DRAFT, APPROVED) 这一项，所以草稿直接批准会 409，与有没有权限无关。
#
# 2026-10-02 收紧：「审核中」成为**必经步骤**。
# 原本 (SUBMITTED → APPROVED/REJECTED) 也在表里，经理可以跳过「开始审核」
# 直接拍板 —— 那个状态就成了没有强制力的装饰。现在提交之后必须先「开始审核」
# （= 接手），才能批准或驳回：审批链固定为 提交 → 审核中 → 批准/驳回。
# 好处：留痕里能看出"谁在什么时候接手、隔了多久拍板"。
# 若业务上希望"驳回可以直接打回、不必先接手"，把 (QSUB, QREJ): "approve"
# 加回本表即可（一行）。
QUOTE_TRANSITION_ACTION: dict[tuple[QuoteStatus, QuoteStatus], str] = {
    # 编制者（quote.update）
    (QD, QSUB): "update",        # 提交审核
    (QD, QARC): "update",        # 作废归档（草稿不用了）
    (QSUB, QD): "update",        # 提交人撤回
    (QREV, QD): "update",        # 提交人撤回
    (QREJ, QD): "update",        # 驳回后收回草稿
    (QREJ, QSUB): "update",      # 改完重新提交
    # 审批人（quote.approve）
    (QSUB, QREV): "approve",     # 开始审核（接手）—— 批准/驳回的**前置步骤**
    (QREV, QAPP): "approve",
    (QREV, QREJ): "approve",
    (QAPP, QARC): "approve",
    (QAPP, QD): "approve",       # 退回草稿 = 解锁
    (QARC, QD): "approve",       # 归档退回
}


def quote_transitions(current: QuoteStatus | str) -> list[QuoteStatus]:
    """当前状态下允许去的状态（按业务顺序，前进在前、回退在后）。"""
    cur = QuoteStatus(current)
    ci = QUOTE_ORDER.index(cur)
    targets = [t for (f, t) in QUOTE_TRANSITION_ACTION if f is cur]
    forward = [t for t in QUOTE_ORDER if t in targets and QUOTE_ORDER.index(t) > ci]
    backward = [t for t in QUOTE_ORDER if t in targets and QUOTE_ORDER.index(t) < ci]
    return forward + backward


def can_move_quote(current: QuoteStatus | str, target: QuoteStatus | str) -> bool:
    """能否把报价单从 current 改到 target。相同状态永远放行（不是变更）。"""
    cur, tgt = QuoteStatus(current), QuoteStatus(target)
    if cur is tgt:
        return True
    return (cur, tgt) in QUOTE_TRANSITION_ACTION


def required_quote_action(current: QuoteStatus | str, target: QuoteStatus | str) -> str:
    """该流转需要哪个动作：`update` 或 `approve`。非法流转返回空串。"""
    return QUOTE_TRANSITION_ACTION.get((QuoteStatus(current), QuoteStatus(target)), "")


def quote_transition_error(current: QuoteStatus | str, target: QuoteStatus | str) -> str:
    """409 文案：说清楚"为什么不行 + 现在能干什么"。"""
    cur, tgt = QuoteStatus(current), QuoteStatus(target)
    if cur is QD and tgt in (QAPP, QREJ):
        return (
            "报价单必须先「提交审核」才能批准 —— 草稿直接批准会绕过审批环节。"
            "请先提交审核，再由经理批准。"
        )
    if cur is QSUB and tgt in (QAPP, QREJ):
        verb = "批准" if tgt is QAPP else "驳回"
        return (
            f"报价单已提交，但还没有人接手审核；请先点「开始审核」再{verb}"
            "（业务要求：进入审核中才能批准/驳回，这样能看出谁接手、隔了多久拍板）。"
        )
    if cur is QREJ:
        return "已驳回的报价单需要修改后「重新提交」，再由经理批准。"
    if cur in (QAPP, QARC):
        return (
            f"报价单已{'批准' if cur is QAPP else '归档'}，不能再改成"
            f"「{_QUOTE_LABEL[tgt]}」；如需修改请先「退回草稿」。"
        )
    allowed = quote_transitions(cur)
    names = "、".join(_QUOTE_LABEL[s] for s in allowed) or "（无）"
    return (
        f"报价单状态不能从「{_QUOTE_LABEL[cur]}」改成「{_QUOTE_LABEL[tgt]}」；"
        f"当前可改为：{names}"
    )


__all__ = [
    "PROJECT_STATUS_TRANSITIONS",
    "QUOTE_TRANSITION_ACTION",
    "allowed_project_transitions",
    "can_move_project",
    "can_move_quote",
    "ordered_targets",
    "ordered_transitions",
    "project_transition_error",
    "quote_transition_error",
    "quote_transitions",
    "required_quote_action",
]
