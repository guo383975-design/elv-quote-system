"""AI 对话 / Agent 循环测试（2026-10-04）。

不依赖外网：把 provider 的流式调用换成假的，验证
  ① 多轮上下文与消息落库；
  ② 工具调用循环（模型说要查设备 → 我们执行 → 结果回灌 → 模型给结论）；
  ③ 工具按当前用户鉴权（销售看不到进价、无权看低毛利单）；
  ④ 未配置 AI 时给明确 400。
"""

from __future__ import annotations

import json
from decimal import Decimal

from httpx import AsyncClient

from app.services.ai_tools import run_tool


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


# ---------------------------------------------------------------------------
# 工具层（直接测，不经过模型）
# ---------------------------------------------------------------------------

async def test_tools_hide_cost_from_sales(
    client: AsyncClient, admin_headers, db
) -> None:
    """销售用 search_devices 看不到进价；经理能看到 —— 与界面口径一致。"""
    await client.post("/api/v1/devices", headers=admin_headers, json={
        "code": "VAIT1", "name": "AI 测试摄像机", "model": "AI-TOOL-1",
        "category": "video", "unit": "台", "reference_price": "680",
        "cost_price": "400"})

    from app.core.permissions import Role
    from app.models.user import User, UserStatus

    sales = User(username="aitoolsales", email="aitoolsales@example.com",
                 hashed_password="x", role=Role.SALES, status=UserStatus.ACTIVE)
    manager = User(username="aitoolmgr", email="aitoolmgr@example.com",
                   hashed_password="x", role=Role.MANAGER, status=UserStatus.ACTIVE)
    db.add_all([sales, manager])
    await db.flush()

    got_sales = await run_tool(db, sales, "search_devices", {"keyword": "AI-TOOL-1"})
    assert got_sales["cost_visible"] is False
    assert "cost_price" not in got_sales["devices"][0], got_sales

    got_mgr = await run_tool(db, manager, "search_devices", {"keyword": "AI-TOOL-1"})
    assert got_mgr["cost_visible"] is True
    assert Decimal(str(got_mgr["devices"][0]["cost_price"])) == Decimal("400")


async def test_tool_permission_denied_is_reported_not_faked(
    client: AsyncClient, admin_headers, db
) -> None:
    """无权看的工具要**明确报错**，不能编一个数字出来。"""
    from app.core.permissions import Role
    from app.models.user import User, UserStatus

    sales = User(username="aitool2", email="aitool2@example.com",
                 hashed_password="x", role=Role.SALES, status=UserStatus.ACTIVE)
    db.add(sales)
    await db.flush()

    r = await run_tool(db, sales, "low_margin_quotes", {})
    # 低毛利分析属于**利润**权限（2026-10-05 从成本里拆出来）：
    # 销售既没有成本也没有利润 → 必须明确报错，不能编数字
    assert "error" in r and "利润" in r["error"], r

    unknown = await run_tool(db, sales, "no_such_tool", {})
    assert "error" in unknown


async def test_write_tool_only_proposes(client: AsyncClient, admin_headers, db) -> None:
    """写操作只产出待确认卡片，绝不直接改数据。"""
    dev = (await client.post("/api/v1/devices", headers=admin_headers, json={
        "code": "VAIT2", "name": "AI 待确认设备", "model": "AI-TOOL-2",
        "category": "video", "unit": "台", "reference_price": "500",
        "cost_price": "300"})).json()

    from app.core.permissions import Role
    from app.models.user import User, UserStatus

    # 设备库的写权限是 admin/造价/采购（**不含经理**）——工具必须与这条一致，
    # 否则会出现"界面上改不了、让 AI 改却能改"的漏洞。
    # ⚠ 2026-10-05 起还多一道：**改成本得能看成本**。
    # 工程部（estimator）按新口径看不到进价，所以"提议改进价"对它关闭 ——
    # 让看不到这个数字的人去改它，等于闭着眼睛改价。
    # 采购（purchaser）既有写权限也有成本权限 → 放行。
    estimator = User(username="aitool3", email="aitool3@example.com",
                     hashed_password="x", role=Role.ESTIMATOR, status=UserStatus.ACTIVE)
    manager = User(username="aitool4", email="aitool4@example.com",
                   hashed_password="x", role=Role.MANAGER, status=UserStatus.ACTIVE)
    purchaser = User(username="aitool5", email="aitool5@example.com",
                     hashed_password="x", role=Role.PURCHASER, status=UserStatus.ACTIVE)
    db.add_all([estimator, manager, purchaser])
    await db.flush()

    # 经理没有设备库写权限
    denied = await run_tool(db, manager, "propose_device_cost",
                            {"code_or_model": "AI-TOOL-2", "new_cost": 260})
    assert "error" in denied and "无权" in denied["error"], denied

    # 工程部有写权限但看不到成本 → 拒绝，并说明原因
    blind = await run_tool(db, estimator, "propose_device_cost",
                           {"code_or_model": "AI-TOOL-2", "new_cost": 260})
    assert "error" in blind and "成本" in blind["error"], blind

    # 采购：写权限 + 成本权限都有 → 产出待确认卡片
    out = await run_tool(db, purchaser, "propose_device_cost",
                         {"code_or_model": "AI-TOOL-2", "new_cost": 260, "reason": "供应商降价"})
    assert out["needs_confirmation"] is True, out
    assert out["current_cost"] == 300.0
    assert out["new_cost"] == 260.0

    # 数据没被动过
    got = (await client.get(f"/api/v1/devices/{dev['id']}", headers=admin_headers)).json()
    assert Decimal(str(got["cost_price"])) == Decimal("300"), got


# ---------------------------------------------------------------------------
# Agent 循环（假 provider）
# ---------------------------------------------------------------------------

async def test_agent_loop_runs_tool_then_answers(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """一轮对话里：模型先要查设备 → 我们执行 → 结果回灌 → 模型给结论。"""
    await _enable_ai(client, admin_headers)
    await client.post("/api/v1/devices", headers=admin_headers, json={
        "code": "VAIT3", "name": "AI 循环摄像机", "model": "AI-LOOP-1",
        "category": "video", "unit": "台", "reference_price": "680", "cost_price": "400"})

    from app.services import ai_chat as chat_mod

    calls: list[list[dict]] = []

    async def fake_stream(db, *, cfg, messages, tools=None, **kwargs):
        calls.append(messages)
        if len(calls) == 1:
            # 第一轮：模型要求调工具
            yield {"type": "delta", "text": "我查一下设备库。"}
            yield {"type": "usage", "usage": {"total_tokens": 11}}
            yield {"type": "tool_calls", "tool_calls": [{
                "id": "call_1", "name": "search_devices",
                "arguments": {"keyword": "AI-LOOP-1"}, "arguments_raw": '{"keyword":"AI-LOOP-1"}',
            }]}
        else:
            # 第二轮：拿到工具结果后给结论
            assert any(m.get("role") == "tool" for m in messages), "工具结果没有回灌给模型"
            yield {"type": "delta", "text": "查到了：AI-LOOP-1，参考价 680，进价 400。"}
            yield {"type": "usage", "usage": {"total_tokens": 22}}

    monkeypatch.setattr(chat_mod, "stream_chat_with_tools", fake_stream)

    events = await _read_sse(client, admin_headers, {"message": "AI-LOOP-1 多少钱？"})
    kinds = [e["type"] for e in events]
    assert "conversation" in kinds and "user_saved" in kinds, kinds
    assert "delta" in kinds and "tool_start" in kinds and "tool_result" in kinds, kinds
    assert kinds[-1] == "done", kinds

    tool_start = next(e for e in events if e["type"] == "tool_start")
    assert tool_start["name"] == "search_devices"
    assert tool_start["label"] == "搜索设备库"
    tool_result = next(e for e in events if e["type"] == "tool_result")
    assert tool_result["ok"] is True
    assert "1 台设备" in tool_result["summary"], tool_result

    text = "".join(e.get("text") or "" for e in events if e["type"] == "delta")
    assert "680" in text and "400" in text, text

    # 消息落库：user / assistant(带 tool_calls) / tool / assistant(结论)
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation")
    detail = (await client.get(f"/api/v1/ai/conversations/{conv_id}",
                               headers=admin_headers)).json()
    roles = [m["role"] for m in detail["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"], roles
    assert detail["messages"][1]["tool_calls"][0]["name"] == "search_devices"
    assert "680" in (detail["messages"][-1]["content"] or "")
    assert len(calls) == 2, "应该恰好两轮模型调用"


async def test_chat_continues_conversation(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """带上会话 id 继续聊：历史消息要进上下文（多轮对话）。"""
    await _enable_ai(client, admin_headers)

    from app.services import ai_chat as chat_mod

    seen: list[int] = []

    async def fake_stream(db, *, cfg, messages, tools=None, **kwargs):
        seen.append(len([m for m in messages if m.get("role") in ("user", "assistant")]))
        yield {"type": "delta", "text": "好的。"}
        yield {"type": "usage", "usage": {"total_tokens": 5}}

    monkeypatch.setattr(chat_mod, "stream_chat_with_tools", fake_stream)

    first = await _read_sse(client, admin_headers, {"message": "第一句"})
    conv_id = next(e["conversation_id"] for e in first if e["type"] == "conversation")

    await _read_sse(client, admin_headers, {"message": "第二句", "conversation_id": conv_id})
    assert seen[0] == 1, seen        # 第一轮只有 1 条用户消息
    assert seen[1] >= 3, seen        # 第二轮应带上历史（user/assistant/user）

    detail = (await client.get(f"/api/v1/ai/conversations/{conv_id}",
                               headers=admin_headers)).json()
    assert len(detail["messages"]) == 4, detail["messages"]
    convs = (await client.get("/api/v1/ai/conversations", headers=admin_headers)).json()
    assert any(c["id"] == conv_id and c["message_count"] == 4 for c in convs["items"])


async def test_chat_requires_ai_configured(client: AsyncClient, admin_headers) -> None:
    """没配 AI（或没 Key）时给明确 400，而不是 500。"""
    await client.put("/api/v1/ai/config", headers=admin_headers,
                     json={"enabled": False, "api_key": "__CLEAR__"})
    r = await client.post("/api/v1/ai/chat", headers=admin_headers, json={"message": "在吗"})
    assert r.status_code == 400, r.text
    assert "AI" in r.json()["detail"]


async def test_conversation_is_private(client: AsyncClient, admin_headers, monkeypatch) -> None:
    """会话是私人的：别人拿 id 也读不到。"""
    await _enable_ai(client, admin_headers)

    from app.services import ai_chat as chat_mod

    async def fake_stream(db, *, cfg, messages, tools=None, **kwargs):
        yield {"type": "delta", "text": "嗯。"}
        yield {"type": "usage", "usage": {}}

    monkeypatch.setattr(chat_mod, "stream_chat_with_tools", fake_stream)
    events = await _read_sse(client, admin_headers, {"message": "机密问题"})
    conv_id = next(e["conversation_id"] for e in events if e["type"] == "conversation")

    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "ainosey", "email": "ainosey@example.com",
        "password": "Viewer12345!", "role": "manager"})
    tok = (await client.post("/api/v1/auth/login/json", json={
        "username": "ainosey", "password": "Viewer12345!"})).json()["access_token"]
    r = await client.get(f"/api/v1/ai/conversations/{conv_id}",
                         headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 404, r.text
