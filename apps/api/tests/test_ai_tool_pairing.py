"""工具调用/结果配对（11148）回归测试（2026-10-06）。

背景（真实事故）：`ai_messages.created_at` 用 `server_default=func.now()`，PostgreSQL
的 now() 是**事务时间** —— 同一轮对话（user → assistant(tool_calls) → N 条 tool 结果）
所有行时间戳完全相同，`ORDER BY created_at` 的平局顺序由数据库决定。取回的历史
可能出现"结果排在调用之前／两个结果互换"，而 id 全都配得上对，所以"按 id 裁剪"救不了；
上游（供应商码 11148 `tool calls and tool results do not match`）直接 400 拒掉整条请求，
并且这条坏历史被每轮请求原样重放 —— 会话永久报废，还会把中转网关账号的连败计数喂高，
最终表现为"No healthy account available in pool + 让用户去改模型名"。

这里锁住四件事：
  ① 出站前按**声明顺序**重建工具结果（对入库顺序不敏感）；
  ② 装不上对的调用/结果一律裁掉，不留半截配对；
  ③ assistant 一旦带了 tool_calls，就必定给每个 call 落一条结果（工具抛异常也算）；
  ④ 错误文案不再把 no_healthy_account 一律解释成"模型名不对"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from httpx import AsyncClient
from sqlalchemy import select

from app.models.ai import AiConversation, AiMessage
from app.services import ai_chat as chat_mod
from app.services.ai_chat import (
    _history_to_messages,
    _rebuild_tool_order,
    _sanitize_tool_pairs,
)
from app.services.ai_client import _explain_http_error, _provider_error

# 用户实际贴回来的报文（外层网关码 + 内层供应商码）
REAL_BODY = json.dumps({
    "error": {
        "code": "no_healthy_account",
        "message": json.dumps({
            "code": 11148,
            "msg": "tool calls and tool results do not match, please start a new conversation and retry",
            "requestId": "2d896f7899b2a8aa4c81da218402fa46",
            "extError": {"code": "400001", "message": "tool calls and tool results do not match"},
        }),
    }
}, ensure_ascii=False)

MODEL_MISSING_BODY = json.dumps({
    "error": {
        "code": "no_healthy_account",
        "gateway_hint": "upstream has no such model on this backend; switch model or retry on another account",
        "message": json.dumps({
            "code": 11102,
            "msg": "model [Deepseek-V4.1-Flash] service info not found",
            "requestId": "0617a7fea2225b08baefbcc647ab1cee",
        }),
    }
}, ensure_ascii=False)

NO_ACCOUNT_BODY = json.dumps({
    "error": {
        "code": "no_healthy_account",
        "gateway_hint": "no healthy account available in pool; check /status or retry later",
        "message": "all accounts are temporarily unavailable, please retry later",
        "type": "api_error",
    }
}, ensure_ascii=False)


def _msg(role: str, **kw) -> dict:
    base = {"role": role, "content": ""}
    base.update(kw)
    return base


def _call(cid: str, name: str = "search_devices") -> dict:
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": "{}"}}


# ---------------------------------------------------------------------------
# ① 顺序重建：结果挂到声明它的调用上（入库顺序怎么乱都不影响出站）
# ---------------------------------------------------------------------------


def test_rebuild_attaches_results_to_the_declaring_call() -> None:
    """结果排在调用之前（顺序错乱）→ 出站时被挪回调用后面，且按声明顺序。"""
    dirty = [
        _msg("user", content="查两个设备"),
        _msg("tool", tool_call_id="call_B", name="search_devices", content="B"),
        _msg("assistant", tool_calls=[_call("call_A"), _call("call_B")]),
        _msg("tool", tool_call_id="call_A", name="search_devices", content="A"),
        _msg("assistant", content="结论"),
    ]
    out, stats = _rebuild_tool_order(dirty)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool", "tool", "assistant"], roles

    idx = roles.index("assistant")           # 声明两个调用的那条
    assert out[idx]["tool_calls"][0]["id"] == "call_A"
    assert out[idx + 1]["tool_call_id"] == "call_A", "结果必须按声明顺序紧跟其后"
    assert out[idx + 2]["tool_call_id"] == "call_B"
    assert stats["orphan_results"] == 0


def test_rebuild_drops_orphan_and_duplicate_results() -> None:
    dirty = [
        _msg("assistant", tool_calls=[_call("call_A")]),
        _msg("tool", tool_call_id="call_A", content="第一次"),
        _msg("tool", tool_call_id="call_A", content="重复"),
        _msg("tool", tool_call_id="call_ZZZ", content="没人声明"),
    ]
    out, stats = _rebuild_tool_order(dirty)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[1]["content"] == "第一次"
    assert stats == {"orphan_results": 1, "dup_results": 1}, stats


# ---------------------------------------------------------------------------
# ② 对称裁剪：不留半截配对
# ---------------------------------------------------------------------------


def test_sanitize_keeps_only_complete_pairs() -> None:
    """批里 [A,B] 只回了 A：B 从调用侧裁掉，孤儿结果也裁掉。"""
    dirty = [
        _msg("assistant", tool_calls=[_call("call_A"), _call("call_B")]),
        _msg("tool", tool_call_id="call_A", content="A"),
        _msg("tool", tool_call_id="call_C", content="孤儿"),
        _msg("assistant", tool_calls=[_call("call_D")]),
    ]
    clean, stats = _sanitize_tool_pairs(dirty)
    kept = clean[0]["tool_calls"]
    assert [c["id"] for c in kept] == ["call_A"], kept
    assert [m["role"] for m in clean] == ["assistant", "tool"]
    # 丢掉两个调用（批里的 call_B + 完全没结果的 call_D）、一个孤儿结果、
    # 以及 call_D 那条只剩空壳的 assistant 消息
    assert stats == {"calls": 2, "results": 1, "messages": 1}, stats
    # 完全得不到结果的 assistant 只剩空壳 → 整条丢掉（不留空 content 的 assistant）
    assert all(m["role"] != "assistant" or m.get("tool_calls") or m.get("content")
               for m in clean)


def test_sanitize_noop_when_history_is_clean() -> None:
    clean_in = [
        _msg("user", content="hi"),
        _msg("assistant", tool_calls=[_call("call_A")]),
        _msg("tool", tool_call_id="call_A", content="A"),
    ]
    out, stats = _sanitize_tool_pairs(clean_in)
    assert out == clean_in and not any(stats.values()), stats


# ---------------------------------------------------------------------------
# 落库行 → messages：脏行（缺 name / 空 id）不再制造半截配对
# ---------------------------------------------------------------------------


def test_history_to_messages_repairs_dirty_rows() -> None:
    rows = [
        AiMessage(role="user", content="查设备"),
        # 调用缺 name：发出去上游判非法参数；它的结果必须一起丢掉，否则成孤儿
        AiMessage(role="assistant", content=None,
                  tool_calls=[{"id": "call_A", "name": "", "arguments": {}},
                              {"id": "call_B", "name": "search_devices", "arguments": {}}]),
        AiMessage(role="tool", tool_call_id="call_A", tool_name="", content="A"),
        AiMessage(role="tool", tool_call_id="call_B", tool_name="search_devices", content="B"),
        AiMessage(role="tool", tool_call_id="", tool_name="x", content="空 id"),
    ]
    out = _history_to_messages(rows)
    roles = [m["role"] for m in out]
    assert roles == ["user", "assistant", "tool"], roles
    assert [c["id"] for c in out[1]["tool_calls"]] == ["call_B"]
    assert out[2]["tool_call_id"] == "call_B"
    assert all(m.get("tool_call_id") for m in out if m["role"] == "tool")


# ---------------------------------------------------------------------------
# ③ Agent 循环：带 tool_calls 的 assistant 必须配齐结果
# ---------------------------------------------------------------------------


async def _enable_ai(client: AsyncClient, admin_headers) -> None:
    r = await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "fake-model", "api_key": "sk-fake-1234567890abcdef"})
    assert r.status_code == 200, r.text


async def _read_sse(client: AsyncClient, headers, payload: dict) -> list[dict]:
    events: list[dict] = []
    async with client.stream("POST", "/api/v1/ai/chat", headers=headers, json=payload) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for line in resp.aiter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:].strip()))
    return events


async def test_every_call_gets_a_result_even_when_tool_raises(
    client: AsyncClient, admin_headers, db, monkeypatch
) -> None:
    """工具抛异常时也必须落一条结果 —— 否则这条会话以后每轮请求都被判 11148。"""
    await _enable_ai(client, admin_headers)

    async def fake_stream(db_, *, cfg, messages, tools=None, **kwargs):
        if not any(m.get("role") == "tool" for m in messages):
            yield {"type": "tool_calls", "tool_calls": [
                {"id": "call_ok", "name": "search_devices", "arguments": {"keyword": "x"},
                 "arguments_raw": '{"keyword":"x"}'},
                {"id": "call_boom", "name": "search_devices", "arguments": {"keyword": "y"},
                 "arguments_raw": '{"keyword":"y"}'},
            ]}
            return
        yield {"type": "delta", "text": "两个都查完了。"}

    async def fake_run_tool(db_, user, name, args):
        if args.get("keyword") == "y":
            raise RuntimeError("模拟工具崩溃")
        return {"devices": [], "count": 0, "cost_visible": True}

    monkeypatch.setattr(chat_mod, "stream_chat_with_tools", fake_stream)
    monkeypatch.setattr(chat_mod, "run_tool", fake_run_tool)

    events = await _read_sse(client, admin_headers, {"message": "查两个"})
    assert events[-1]["type"] == "done", events[-1]
    failed = [e for e in events if e["type"] == "tool_result" and not e["ok"]]
    assert failed and "工具执行异常" in failed[0]["summary"], failed

    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    rows = list((await db.scalars(
        select(AiMessage).where(AiMessage.conversation_id == UUID(conv_id))
    )).all())
    assistant = [m for m in rows if m.role == "assistant" and m.tool_calls]
    assert len(assistant) == 1
    declared = [c["id"] for c in assistant[0].tool_calls]
    assert declared == ["call_ok", "call_boom"], declared
    results = {m.tool_call_id for m in rows if m.role == "tool"}
    assert results == set(declared), f"每个调用都必须有结果，实际 {results}"
    boom = next(m for m in rows if m.tool_call_id == "call_boom")
    assert "工具执行异常" in (boom.content or ""), boom.content


async def test_dirty_history_is_repaired_before_being_sent(
    client: AsyncClient, admin_headers, db, monkeypatch
) -> None:
    """端到端：库里躺着顺序错乱 + 半截配对 + 空 id 的历史，出站 payload 必须是干净的。

    关键点：这些行的时间戳**故意完全相同**，复现 `func.now()` 事务时间造成的平局。
    """
    await _enable_ai(client, admin_headers)

    conv = AiConversation(title="脏历史会话", user_id=None)
    db.add(conv)
    await db.flush()
    same = datetime.now(tz=UTC)          # ← 同一事务同一时间戳（平局）

    db.add_all([
        AiMessage(conversation_id=conv.id, role="user", content="帮我查设备", created_at=same),
        # 入库顺序故意打乱：结果排在声明它的 assistant 之前
        AiMessage(conversation_id=conv.id, role="tool", tool_call_id="call_A",
                  tool_name="search_devices", content='{"n":1}', created_at=same),
        AiMessage(conversation_id=conv.id, role="assistant", content=None, created_at=same,
                  tool_calls=[{"id": "call_A", "name": "search_devices", "arguments": {"k": "x"}},
                              {"id": "call_B", "name": "search_devices", "arguments": {"k": "y"}}]),
        AiMessage(conversation_id=conv.id, role="tool", tool_call_id="call_B",
                  tool_name="search_devices", content='{"n":2}', created_at=same),
        # 空 id 的孤儿结果 + 没人声明的孤儿结果
        AiMessage(conversation_id=conv.id, role="tool", tool_call_id="",
                  tool_name="search_devices", content="空 id", created_at=same),
        AiMessage(conversation_id=conv.id, role="tool", tool_call_id="call_ZZZ",
                  tool_name="search_devices", content="孤儿", created_at=same),
    ])
    await db.flush()

    seen: list[list[dict]] = []

    async def fake_stream(db_, *, cfg, messages, tools=None, **kwargs):
        seen.append(messages)
        yield {"type": "delta", "text": "好的。"}

    monkeypatch.setattr(chat_mod, "stream_chat_with_tools", fake_stream)
    await _read_sse(client, admin_headers,
                    {"message": "继续", "conversation_id": str(conv.id)})

    assert seen, "模型没被调用"
    sent = seen[0]

    # 1) 每个 tool 结果都必须紧跟在声明它的 assistant 之后、id 非空
    for i, m in enumerate(sent):
        if m.get("role") != "tool":
            continue
        assert m["tool_call_id"], "空 tool_call_id 不允许出现在 payload 里"
        prev = sent[i - 1]
        assert prev.get("role") in ("assistant", "tool"), f"结果前面不是 assistant/tool: {prev}"
        declared_ids = {c["id"] for c in (sent[i - 1].get("tool_calls") or [])}
        if prev.get("role") == "assistant":
            assert m["tool_call_id"] in declared_ids, (m, prev)

    # 2) 每个声明的调用都有结果，且结果顺序 = 声明顺序
    pushed_idx = next(
        i for i, m in enumerate(sent)
        if m.get("role") == "assistant" and m.get("tool_calls")
    )
    pushed = sent[pushed_idx]
    ids = [c["id"] for c in pushed["tool_calls"]]
    assert ids == ["call_A", "call_B"], ids
    after = sent[pushed_idx + 1: pushed_idx + 3]
    assert [m["tool_call_id"] for m in after] == ["call_A", "call_B"], after

    # 3) 孤儿一律不进 payload
    flat = json.dumps(sent, ensure_ascii=False)
    assert "call_ZZZ" not in flat and "空 id" not in flat


# ---------------------------------------------------------------------------
# ④ 错误文案：不再把 no_healthy_account 一律说成"模型名不对"
# ---------------------------------------------------------------------------


def test_provider_error_reads_both_layers() -> None:
    info = _provider_error(REAL_BODY)
    assert info["code"] == "no_healthy_account"
    assert info["vendor_code"] == "11148"
    assert "tool calls and tool results do not match" in info["vendor_msg"]


def test_explain_tool_pairing_error_says_start_new_conversation() -> None:
    msg = _explain_http_error(503, REAL_BODY, model="cn:deepseek-v4.1-flash")
    assert "11148" in msg and "新开一个会话" in msg, msg
    assert "模型名" not in msg, "工具配对问题不该再让用户去改模型名"


def test_explain_model_missing_still_points_at_model_name() -> None:
    msg = _explain_http_error(503, MODEL_MISSING_BODY, model="Deepseek-V4.1-Flash")
    assert "模型名" in msg and "系统设置" in msg, msg
    assert "upstream has no such model" in msg, "网关建议要透出来"


def test_explain_no_account_is_not_a_model_name_problem() -> None:
    msg = _explain_http_error(503, NO_ACCOUNT_BODY, model="cn:deepseek-v4.1-flash")
    assert "no_healthy_account" in msg and "账号" in msg, msg
    assert "模型名" not in msg, msg
    assert "no healthy account available in pool" in msg, "网关建议要透出来"


# ---------------------------------------------------------------------------
# 2026-10-05：同一 call id 被复用（真实事故的根因）
# ---------------------------------------------------------------------------


def _assert_protocol_valid(messages: list[dict]) -> None:
    """按 OpenAI 协议逐条检查（顺序 + 配对 + 类型）。

    这是"上游到底会不会 400"的判据 —— 断言它等价于断言"不会被罚号"。
    """
    import json as _json

    pending: list[str] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "assistant":
            assert not pending, f"第 {i} 条 assistant 之前有未闭环的调用：{pending}"
            pending = [c.get("id") for c in (m.get("tool_calls") or [])]
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                assert c.get("id"), "tool_call 缺 id"
                assert c.get("type") == "function", c.get("type")
                assert fn.get("name"), "tool_call 缺 function.name"
                assert isinstance(fn.get("arguments"), str), "arguments 必须是字符串"
                _json.loads(fn.get("arguments") or "{}")
        elif role == "tool":
            cid = m.get("tool_call_id")
            assert cid in pending, f"第 {i} 条 tool({cid}) 对不上紧邻的调用 {pending}"
            pending.remove(cid)
            assert isinstance(m.get("content"), str), "tool 的 content 必须是字符串"
    assert not pending, f"结尾有没等到结果的调用：{pending}"


def test_repeated_call_id_with_single_result() -> None:
    """同一 id 出现两次、只有一条结果 —— 集合语义会漏，次数语义能修。

    这正是真实事故的形态：模型/网关复用了同一个 tool_call_id，
    集合判定"配对成功"，报文却非法。
    """
    from app.services.ai_chat import (
        _enforce_tool_protocol,
        _rebuild_tool_order,
        _sanitize_tool_pairs,
    )

    call = {
        "id": "call_X",
        "type": "function",
        "function": {"name": "search_device", "arguments": "{}"},
    }
    msgs = [
        {"role": "user", "content": "找一下设备"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "call_X", "name": "search_device",
         "content": "[]"},
        {"role": "user", "content": "再找一次"},
        # ⚠ 复用同一个 id，但这次没有结果
        {"role": "assistant", "content": "我再查一下", "tool_calls": [call]},
        {"role": "user", "content": "算了"},
    ]

    ordered, _ = _rebuild_tool_order(msgs)
    cleaned, _ = _sanitize_tool_pairs(ordered)
    final, gate = _enforce_tool_protocol(cleaned)

    _assert_protocol_valid(final)
    # 第二次调用必须被丢弃（它拿不到结果）
    calls = [c for m in final if m.get("role") == "assistant" for c in (m.get("tool_calls") or [])]
    assert len(calls) == 1, f"应当只保留配得上结果的那次调用，实际 {len(calls)}"
    assert gate["calls"] >= 0


def test_gate_drops_dangling_call_and_orphan_result() -> None:
    """闸门要能独立兜住：结尾悬挂调用 + 孤立结果，都不会发给上游。"""
    from app.services.ai_chat import _enforce_tool_protocol

    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "来了",
         "tool_calls": [{"id": "A", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "打断"},
        {"role": "tool", "tool_call_id": "B", "name": "f", "content": "{}"},
    ]
    out, stats = _enforce_tool_protocol(msgs)
    _assert_protocol_valid(out)
    assert stats["calls"] == 1, stats        # 悬挂的 A 被摘掉
    assert stats["results"] == 1, stats      # 孤儿结果 B 被丢掉
    for m in out:
        assert not m.get("tool_calls")


def test_gate_coerces_non_string_tool_content() -> None:
    """tool 的 content 必须是字符串：上游对非字符串一样拒。"""
    from app.services.ai_chat import _enforce_tool_protocol

    msgs = [
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "A", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "A", "name": "f", "content": {"k": 1}},
    ]
    out, _ = _enforce_tool_protocol(msgs)
    _assert_protocol_valid(out)
    assert isinstance(out[1]["content"], str)
