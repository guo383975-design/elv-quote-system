"""流式对话 + 工具调用（2026-10-04）。

OpenAI 兼容的流式协议里，工具调用是**分片**下发的：
`tool_calls[0].function.arguments` 会一段一段来（`{"key":` / `"value"}`），
必须按 `index` 累积再拼接 —— 直接取每一片的 arguments 会拿到半截 JSON，
表现为"工具参数解析失败"。

同时这里不做"边流边存"：先完整收完一轮，再落库。否则中断时会留下半条消息。
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.services.ai_client import (
    AiCallError,
    _endpoint,
    _explain_http_error,
    _log_usage,
    require_ready,
)

log = get_logger("services.ai_client")


async def stream_chat_with_tools(
    db: AsyncSession,
    *,
    cfg: dict[str, Any],
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    task: str,
    user=None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    model_override: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """流式要一轮回复。产出事件：

      {"type": "delta", "text": "..."}                     文本增量
      {"type": "tool_calls", "tool_calls": [...]}           本轮要调的工具（已累积完整）
      {"type": "usage", "usage": {...}}                     token 用量
    """
    require_ready(cfg)
    model = model_override or cfg.get("model") or ""
    timeout = float(cfg.get("timeout_seconds") or 120)

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": float(cfg.get("temperature") or 0.1),
        "stream": True,
        # 让少数支持该参数的厂商把用量也带在流里（不支持也不影响）
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"

    started = time.monotonic()
    collected_text: list[str] = []
    tool_acc: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream(
                "POST",
                _endpoint(cfg.get("base_url") or ""),
                headers={
                    "Authorization": f"Bearer {cfg.get('api_key')}",
                    "Content-Type": "application/json",
                },
                json=payload,
            ) as resp:
                if resp.status_code >= 400:
                    body = (await resp.aread()).decode("utf-8", "replace")
                    msg = _explain_http_error(resp.status_code, body)
                    await _log_usage(
                        db, task=task, cfg=cfg, model=model, usage={},
                        duration_ms=int((time.monotonic() - started) * 1000), ok=False,
                        error=msg, user=user, ref_type=ref_type, ref_id=ref_id,
                    )
                    raise AiCallError(msg)

                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if chunk.get("usage"):
                        usage = chunk["usage"]
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = delta.get("content")
                        if text:
                            collected_text.append(text)
                            yield {"type": "delta", "text": text}
                        for frag in delta.get("tool_calls") or []:
                            idx = int(frag.get("index") or 0)
                            slot = tool_acc.setdefault(
                                idx, {"id": None, "name": None, "arguments": ""}
                            )
                            if frag.get("id"):
                                slot["id"] = frag["id"]
                            fn = frag.get("function") or {}
                            if fn.get("name"):
                                slot["name"] = fn["name"]
                            if fn.get("arguments"):
                                slot["arguments"] += fn["arguments"]
    except httpx.TimeoutException as exc:
        msg = (
            f"对话超时（{int(timeout)} 秒）。可以到「系统设置 → AI 助理」把超时调大，"
            "或让我把问题拆小一点。"
        )
        await _log_usage(
            db, task=task, cfg=cfg, model=model, usage={},
            duration_ms=int((time.monotonic() - started) * 1000), ok=False, error=msg,
            user=user, ref_type=ref_type, ref_id=ref_id,
        )
        raise AiCallError(msg) from exc
    except httpx.HTTPError as exc:
        msg = f"网络错误：{type(exc).__name__}。请检查 Base URL 与本机出网。"
        await _log_usage(
            db, task=task, cfg=cfg, model=model, usage={},
            duration_ms=int((time.monotonic() - started) * 1000), ok=False, error=msg,
            user=user, ref_type=ref_type, ref_id=ref_id,
        )
        raise AiCallError(msg) from exc

    duration_ms = int((time.monotonic() - started) * 1000)
    calls = []
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot.get("name"):
            continue
        raw_args = slot.get("arguments") or "{}"
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            args = {"__raw__": raw_args}
        calls.append({
            "id": slot.get("id") or f"call_{idx}",
            "name": slot["name"],
            "arguments": args,
            "arguments_raw": raw_args,
        })

    await _log_usage(
        db, task=task, cfg=cfg, model=model, usage=usage, duration_ms=duration_ms,
        ok=True, error=None, user=user, ref_type=ref_type, ref_id=ref_id,
    )
    yield {"type": "usage", "usage": usage, "text": "".join(collected_text)}
    if calls:
        yield {"type": "tool_calls", "tool_calls": calls}
