"""体检：AI 会话历史里的工具调用／结果配对（**只读，不改任何数据**）。

## 为什么需要这个脚本

上游（供应商错误码 **11148**：`tool calls and tool results do not match`）要求
`assistant.tool_calls` 的每个 id 都有对应的 `role:tool` 结果，且结果紧跟其后。
一旦历史里出现半截配对或顺序错乱，这条会话**之后每一轮请求都会被 400 拒掉**
（坏历史被原样重放），而且会把中转网关账号的连败计数喂高 —— 网关把这个 400
当 `ErrClient` 罚号（`NoteFailures`），账号被降权/出池后，所有客户端都会看到
"No healthy account available in pool"，最后被前端翻译成"模型名不存在"，
把管理员引到完全无关的方向。

## 用法

    python -m scripts.check_ai_tool_pairing                 # 体检最近 200 个会话
    python -m scripts.check_ai_tool_pairing --limit 0       # 不限会话数（全量）
    python -m scripts.check_ai_tool_pairing --fix-sql       # 额外打印可手工执行的修复 SQL
    python -m scripts.check_ai_tool_pairing --quiet          # 只打印有问题的会话

退出码：发现问题 = 1，全部干净 = 0（可挂到巡检/CI 上当门禁）。

> 注意：api 侧从 V0.1.1 起在**出站前**做对称裁剪 + 按声明重排，历史里的坏形态
> 已经不会再打挂会话；本脚本用于**确认历史现状**与评估是否需要清理数据。
"""

from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import select

from app.core.logging import setup_logging
from app.db.session import dispose_engine, init_engine, session_scope
from app.models.ai import AiConversation, AiMessage

setup_logging()


def _scan(messages: list[AiMessage]) -> dict[str, list[str]]:
    """扫一个会话的消息，返回 {'问题类型': [说明, ...]}。"""
    problems: dict[str, list[str]] = defaultdict(list)

    declared: dict[str, int] = {}        # call id -> 声明它的消息下标
    seen_result: dict[str, int] = {}     # call id -> 结果消息下标
    for idx, m in enumerate(messages):
        if m.role == "assistant":
            for c in (m.tool_calls or []):
                if not isinstance(c, dict):
                    problems["调用不是对象"].append(f"#{idx} {c!r}")
                    continue
                cid, name = c.get("id"), c.get("name")
                if not cid:
                    problems["调用缺 id"].append(f"#{idx} {name or '(无名)'}")
                elif not name:
                    problems["调用缺 name"].append(f"#{idx} id={cid}")
                else:
                    if cid in declared:
                        problems["调用 id 重复"].append(f"#{idx} id={cid}")
                    declared[cid] = idx
        elif m.role == "tool":
            cid = (m.tool_call_id or "").strip()
            if not cid:
                problems["结果缺 tool_call_id"].append(f"#{idx} {m.tool_name or '(无名)'}")
                continue
            if cid in seen_result:
                problems["结果 id 重复"].append(f"#{idx} id={cid}")
            else:
                # 只记第一次出现：下面判"结果排在调用之前"要用它，
                # 被后来的重复结果覆盖就会漏报顺序问题。
                seen_result[cid] = idx

    for cid, idx in declared.items():
        first = seen_result.get(cid)
        if first is None:
            problems["有调用没结果"].append(f"#{idx} id={cid}")
        elif first < idx:
            # 顺序错乱要独立判定：不能写成 duplicate 分支的 elif，
            # 否则同一 id 有多条结果时这条会被吞掉（结果重复 + 顺序错乱同时存在）。
            problems["结果排在调用之前（顺序错乱）"].append(
                f"call#{idx} → result#{first} id={cid}"
            )
    for cid, idx in seen_result.items():
        if cid not in declared:
            problems["有结果没调用"].append(f"#{idx} id={cid}")
    return problems


async def main(limit: int, show_sql: bool, quiet: bool) -> int:
    init_engine()
    bad_conversations = 0
    scanned = 0
    total_messages = 0
    try:
        async with session_scope() as session:
            stmt = select(AiConversation).order_by(AiConversation.last_message_at.desc().nullslast())
            if limit > 0:
                stmt = stmt.limit(limit)
            conversations = list((await session.scalars(stmt)).all())

            for conv in conversations:
                rows = list(
                    (
                        await session.scalars(
                            select(AiMessage)
                            .where(AiMessage.conversation_id == conv.id)
                            .order_by(AiMessage.created_at, AiMessage.id)
                        )
                    ).all()
                )
                scanned += 1
                total_messages += len(rows)
                problems = _scan(rows)
                if not problems:
                    if not quiet:
                        print(f"[OK]   {conv.id}  {conv.title!r}  {len(rows)} 条消息")
                    continue

                bad_conversations += 1
                print(f"\n[BAD]  {conv.id}  {conv.title!r}  "
                      f"({len(rows)} 条消息, 用户={conv.user_name or '-'}, "
                      f"最后活动={conv.last_message_at})")
                for kind, items in problems.items():
                    print(f"       · {kind} ×{len(items)}")
                    for it in items[:6]:
                        print(f"           - {it}")
                    if len(items) > 6:
                        print(f"           … 其余 {len(items) - 6} 条")
                if show_sql:
                    cid = _sql_str(str(conv.id))
                    print("       -- 可选手工修复（请先备份数据库）：")
                    print("       -- 1) 只清掉这条会话里配不上对的工具行：")
                    print(f"       --    DELETE FROM ai_messages WHERE conversation_id = {cid}"
                          " AND role = 'tool' AND (tool_call_id IS NULL OR tool_call_id = '');")
                    print("       -- 2) 或整条会话归档/删除，让用户重开：")
                    print(f"       --    UPDATE ai_conversations SET archived = TRUE WHERE id = {cid};")
    finally:
        await dispose_engine()

    print("\n" + "=" * 68)
    print(f"扫描会话 {scanned} 个 / 消息 {total_messages} 条；"
          f"有配对问题的会话 {bad_conversations} 个")
    if bad_conversations:
        print("说明：这些会话的坏历史会被每一轮请求重放并让上游返回 11148。")
        print("      api 侧已在出站前自动裁剪（不影响继续使用）；")
        print("      若要清理数据，先备份再执行上面 --fix-sql 给出的语句。")
        return 1
    print("全部干净：没有发现工具调用/结果配对问题。")
    return 0


def _sql_str(value: str) -> str:
    """单引号转义（只用于打印，不执行）。"""
    return "'" + value.replace("'", "''") + "'"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="体检 AI 会话的工具调用/结果配对（只读）")
    parser.add_argument("--limit", type=int, default=200,
                        help="只体检最近 N 个会话；0 = 全量（默认 200）")
    parser.add_argument("--fix-sql", action="store_true",
                        help="对每条有问题的会话额外打印可手工执行的修复 SQL（不执行）")
    parser.add_argument("--quiet", action="store_true", help="只打印有问题的会话")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.limit, args.fix_sql, args.quiet)))
