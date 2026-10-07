"""调用大模型（2026-10-04）。

## 为什么自己写 HTTP 而不引 SDK

只需要 OpenAI 兼容协议的一个端点（`POST {base_url}/chat/completions`）。
引 `openai` SDK 会多背一个依赖，而且它还在快速换接口；httpx 我们本来就有
（导出/测试都在用）。多模态（图片）也只是把 `image_url` 塞进 content 数组。

## 三件必须做的事

1. **失败要说人话**：401（key 不对）、404（模型名不对）、429（限流）、超时
   分别给不同的中文提示 —— 让管理员一眼知道该改哪里。
2. **超时可配**：大模型慢是常态，默认 120 秒，别用 HTTP 客户端的 5 秒默认值。
3. **每次都记账**：token 与耗时写进 `ai_usage_logs`（用户不设上限，但必须可查）。
"""

from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.models.ai import AiUsageLog
from app.services.ai_crypto import decrypt
from app.services.settings import K_AI, load_settings

log = get_logger("services.ai_client")


class AiNotConfiguredError(RuntimeError):
    """没配好（未启用 / 无 key / 无模型）。接口层翻成 400 并告诉管理员去哪配。"""


class AiTruncatedError(RuntimeError):
    """模型输出被服务商的输出上限截断（不是格式问题，重试同样内容没用）。

    单独一类错误是为了让上层能**换策略**（把这批数据拆小），
    而不是傻等着把同一个超长请求重发一遍。
    """


class AiCallError(RuntimeError):
    """调用失败（网络/鉴权/模型返回不合法），message 已是中文人话。"""


# 真正的"模型名不存在/不可用"特征词。
#
# ⚠ 不要把 `no_healthy_account` 放进来：那是**中转网关的包装码**（网关 handler 的
# "末端错误透传"：code 取本地分类可读名 no_healthy_account，message 才是上游 body
# 原文）。把它当成"模型名问题"，真因就被掩盖成"去改模型名"——真实翻车案例：
# 真因是供应商码 11148（工具调用与结果不配对），管理员照着提示改了模型名，问题照旧。
# 真因一律到 message 里找（见 `_provider_error`）。
_MODEL_NAME_HINTS = (
    "no such model", "service info not found", "model not found",
    "model_not_found", "unknown model", "invalid model",
)

_VENDOR_TOOL_PAIRING = "11148"    # tool calls and tool results do not match
_VENDOR_MODEL_MISSING = "11102"   # model [x] service info not found


def _provider_error(body: str) -> dict[str, str]:
    """把错误体解析成 {code, message, hint, vendor_code, vendor_msg}。

    中转网关（workbuddy/wb2api）的错误是两层的：

        {"error": {"code": "no_healthy_account",              <- 网关分类码
                   "message": "{\\"code\\":11148,\\"msg\\":…}",   <- 上游 body 原文（字符串）
                   "gateway_hint": "…"}}                      <- 网关给的可执行建议

    只看外层码会把真因看丢，所以内层 JSON 也拆开读。取不到一律给空串（不编造）。
    """
    out = {"code": "", "message": "", "hint": "", "vendor_code": "", "vendor_msg": ""}
    text = (body or "").strip()
    if not text:
        return out
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return out
    if not isinstance(data, dict):
        return out
    err = data.get("error") if isinstance(data.get("error"), dict) else data
    if not isinstance(err, dict):
        return out
    out["code"] = str(err.get("code") or "")
    out["message"] = str(err.get("message") or err.get("msg") or "")
    out["hint"] = str(err.get("gateway_hint") or "").strip()

    inner_text = out["message"].strip()
    if inner_text.startswith("{"):
        inner = None
        try:
            inner = json.loads(inner_text)
        except (json.JSONDecodeError, ValueError):
            inner = None
        if isinstance(inner, dict):
            err2 = inner.get("error") if isinstance(inner.get("error"), dict) else inner
            if isinstance(err2, dict):
                out["vendor_code"] = str(err2.get("code") or "")
                out["vendor_msg"] = str(err2.get("msg") or err2.get("message") or "")
    return out


def _looks_like_model_name_error(text: str) -> bool:
    low = (text or "").lower()
    return any(h in low for h in _MODEL_NAME_HINTS)


def _looks_like_tool_pairing_error(low_body: str, vendor_code: str) -> bool:
    if vendor_code == _VENDOR_TOOL_PAIRING:
        return True
    return (
        "tool calls and tool results do not match" in low_body
        or "tool_call_sequence_broken" in low_body
    )


def _explain_http_error(status: int, body: str, *, model: str | None = None) -> str:
    info = _provider_error(body)
    text = (body or "")[:300]
    low = (body or "").lower()
    hint = f"\n服务商建议：{info['hint']}" if info["hint"] else ""

    # ① 工具调用与结果不配对（供应商 11148）：坏历史被每轮请求原样重放，
    #    换模型/重试都不会好 —— 唯一出路是新开会话。
    if _looks_like_tool_pairing_error(low, info["vendor_code"]):
        return (
            "这次请求被服务商拒绝了：**会话历史里的工具调用与结果不配对**"
            "（供应商错误码 11148，tool_call_sequence_broken）。\n"
            "坏历史会被每一轮请求原样重放，所以换模型、重试都没用 —— "
            "请**新开一个会话**继续，这条会话已经报废。"
            "系统已修复产生这种形态的代码，历史会话会在下次发请求时自动修正。"
            f"{hint}\n服务商原始提示：{text}"
        )

    # ② 模型名/模型可用性（11102 service info not found 等）——这才是"去改模型名"
    if _looks_like_model_name_error(low) or info["vendor_code"] == _VENDOR_MODEL_MISSING:
        name = f"「{model}」" if model else ""
        return (
            f"模型名{name}在服务端不存在或不可用。"
            "请到「系统设置 → AI 助理」，把「对话/解析模型」改成该服务商控制台里"
            "真实存在的模型名（图片识别另填「多模态模型」）。"
            f"{hint}\n服务商原始提示：{text}"
        )

    # ③ 网关本身没有可用账号（本地调度失败）：与模型名无关，别去改模型名
    if info["code"].lower() == "no_healthy_account" or \
            "all accounts are temporarily unavailable" in low:
        return (
            "服务商暂时没有可用账号/容量（网关码 no_healthy_account），稍后重试即可。"
            "若原始提示里出现 11148（工具调用与结果不配对），"
            "说明是某个会话的坏历史把网关账号拖垮了，请新开一个会话。"
            f"{hint}\n服务商原始提示：{text}"
        )

    if status == 401:
        return "API Key 无效或已过期（401）。请到「系统设置 → AI 助理」重新填写。"
    if status == 403:
        return f"该 Key 无权访问此模型（403）。{hint}{text}"
    if status == 404:
        return (
            "接口地址或模型名不对（404）。请确认 Base URL 是否以 /v1 结尾、"
            f"模型名是否存在。{hint}{text}"
        )
    if status == 429:
        return f"被限流了（429）。稍后重试，或换用额度更充足的账号。{hint}{text}"
    if status >= 500:
        return f"模型服务端错误（{status}）。{hint}{text}"
    return f"调用失败（{status}）。{hint}{text}"


async def load_ai_config(db: AsyncSession) -> dict[str, Any]:
    """取 AI 配置（**含解密后的 key**，只在服务端使用）。"""
    raw = (await load_settings(db)).get(K_AI) or {}
    cfg = dict(raw)
    cfg["api_key"] = decrypt(raw.get("api_key_cipher") or "")
    return cfg


def require_ready(cfg: dict[str, Any]) -> None:
    if not cfg.get("enabled"):
        raise AiNotConfiguredError(
            "AI 助理未启用：请到「系统设置 → AI 助理」打开开关并填写模型信息。"
        )
    if not (cfg.get("api_key") or "").strip():
        raise AiNotConfiguredError(
            "还没有配置 API Key：请到「系统设置 → AI 助理」填写。"
        )
    if not (cfg.get("model") or "").strip():
        raise AiNotConfiguredError(
            "还没有配置模型名：请到「系统设置 → AI 助理」填写。"
        )


def _endpoint(base_url: str) -> str:
    return f"{(base_url or '').rstrip('/')}/chat/completions"


def _image_part(data: bytes, mime: str | None) -> dict[str, Any]:
    b64 = base64.b64encode(data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime or 'image/png'};base64,{b64}"},
    }


async def chat_json(
    db: AsyncSession,
    *,
    cfg: dict[str, Any],
    system: str,
    user_text: str,
    images: list[tuple[bytes, str | None]] | None = None,
    task: str,
    user=None,
    ref_type: str | None = None,
    ref_id: str | None = None,
    model_override: str | None = None,
    max_retries: int = 2,
) -> dict[str, Any]:
    """要一次**结构化 JSON** 回复。

    `images` 非空时走多模态（扫描件/截图）。返回解析好的 dict；
    模型返回的不是合法 JSON 时把校验错误回灌重试（最多 `max_retries` 次）。
    """
    require_ready(cfg)
    model = model_override or cfg.get("model") or ""
    timeout = float(cfg.get("timeout_seconds") or 120)

    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for data, mime in images or []:
        content.append(_image_part(data, mime))

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": content if images else user_text},
    ]

    last_error = ""
    for attempt in range(max_retries + 1):
        started = time.monotonic()
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": float(cfg.get("temperature") or 0.1),
            # 让模型尽量只吐 JSON（支持该参数的供应商会严格遵守，不支持的会忽略）
            "response_format": {"type": "json_object"},
        }
        raw_text = ""
        usage: dict[str, Any] = {}
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(
                    _endpoint(cfg.get("base_url") or ""),
                    headers={
                        "Authorization": f"Bearer {cfg.get('api_key')}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
            if resp.status_code >= 400:
                msg = _explain_http_error(resp.status_code, resp.text, model=model)
                await _log_usage(
                    db, task=task, cfg=cfg, model=model, usage={}, duration_ms=int(
                        (time.monotonic() - started) * 1000
                    ), ok=False, error=msg, user=user, ref_type=ref_type, ref_id=ref_id,
                )
                raise AiCallError(msg)
            body = resp.json()
            raw_text = (
                (body.get("choices") or [{}])[0].get("message", {}).get("content") or ""
            )
            usage = body.get("usage") or {}
        except httpx.TimeoutException as exc:
            msg = (
                f"调用超时（{int(timeout)} 秒）。可在「系统设置 → AI 助理」把超时调大，"
                "或换更快的模型。"
            )
            await _log_usage(
                db, task=task, cfg=cfg, model=model, usage={},
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=False, error=msg, user=user, ref_type=ref_type, ref_id=ref_id,
            )
            raise AiCallError(msg) from exc
        except httpx.HTTPError as exc:
            msg = f"网络错误：{type(exc).__name__}。请检查 Base URL 与本机出网是否正常。"
            await _log_usage(
                db, task=task, cfg=cfg, model=model, usage={},
                duration_ms=int((time.monotonic() - started) * 1000),
                ok=False, error=msg, user=user, ref_type=ref_type, ref_id=ref_id,
            )
            raise AiCallError(msg) from exc

        duration_ms = int((time.monotonic() - started) * 1000)
        # 先看是不是被截断了：截断的 JSON 怎么修都修不出来，
        # 报"不是合法 JSON"会让人以为是模型不会写格式（实际是装不下）。
        finish = ((resp.json().get("choices") or [{}])[0] or {}).get("finish_reason")
        if finish == "length":
            await _log_usage(
                db, task=task, cfg=cfg, model=model, usage=usage,
                duration_ms=duration_ms, ok=False,
                error="模型输出被截断（超出服务商输出上限）", user=user,
                ref_type=ref_type, ref_id=ref_id,
            )
            raise AiTruncatedError(
                "模型输出被截断（这一批内容超出服务商的输出上限）。"
                "系统已自动把它拆成更小的批次重试；若仍失败，请把文件按工作表或分页拆开再传。"
            )

        parsed = _parse_json(raw_text)
        if parsed is not None:
            await _log_usage(
                db, task=task, cfg=cfg, model=model, usage=usage,
                duration_ms=duration_ms, ok=True, error=None, user=user,
                ref_type=ref_type, ref_id=ref_id,
            )
            return parsed

        # 只有在**模型确实回了内容**、只是格式不对时才重试。
        # 空回复通常是超时/被截断，再试一遍只会让用户多等一个超时周期
        # （真事：视觉解析 + 3 次 × 120 秒 = 6 分钟，前端一直转圈）。
        if not raw_text.strip():
            msg = (
                "模型没有返回内容（可能超时或被截断）。"
                "可以把文件拆小、只传关键页，或在「系统设置 → AI 助理」把超时时间调大。"
            )
            await _log_usage(
                db, task=task, cfg=cfg, model=model, usage=usage,
                duration_ms=duration_ms, ok=False, error=msg, user=user,
                ref_type=ref_type, ref_id=ref_id,
            )
            raise AiCallError(msg)

        # 不是合法 JSON：把错误回灌，让模型自己修
        last_error = "模型返回的不是合法 JSON"
        await _log_usage(
            db, task=task, cfg=cfg, model=model, usage=usage,
            duration_ms=duration_ms, ok=False,
            error=f"{last_error}（第 {attempt + 1} 次）", user=user,
            ref_type=ref_type, ref_id=ref_id,
        )
        messages.append({"role": "assistant", "content": raw_text[:2000]})
        messages.append(
            {
                "role": "user",
                "content": (
                    "上面的回复不是合法 JSON。请**只**输出一个 JSON 对象，"
                    "不要任何解释、不要 markdown 代码块。"
                ),
            }
        )

    raise AiCallError(
        f"{last_error}（已重试 {max_retries} 次）。可以换一个更擅长 JSON 的模型再试。"
    )


def _parse_json(text: str) -> dict[str, Any] | None:
    """从模型回复里抠出 JSON：容忍 ```json 包裹与前后废话。"""
    if not text:
        return None
    cleaned = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)\s*```", cleaned, re.S)
    if fence:
        cleaned = fence.group(1).strip()
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        # 再试一次：截取第一个 { 到最后一个 }
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            data = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


async def _log_usage(
    db: AsyncSession | None,
    *,
    task: str,
    cfg: dict[str, Any],
    model: str,
    usage: dict[str, Any],
    duration_ms: int,
    ok: bool,
    error: str | None,
    user=None,
    ref_type: str | None = None,
    ref_id: str | None = None,
) -> None:
    if db is None:
        # 没有会话（单测/脚本直连）时跳过台账，不影响主流程
        return
    db.add(
        AiUsageLog(
            user_id=getattr(user, "id", None),
            username=getattr(user, "username", None),
            task=task,
            model=model,
            provider=cfg.get("provider"),
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            total_tokens=int(usage.get("total_tokens") or 0),
            duration_ms=duration_ms,
            ok=ok,
            error=error,
            ref_type=ref_type,
            ref_id=ref_id,
        )
    )
    await db.flush()


async def test_connection(db: AsyncSession, *, cfg: dict[str, Any], user=None) -> dict[str, Any]:
    """连通性测试：发一句最小请求，验证 key/模型/地址三件事都对。"""
    require_ready(cfg)
    started = time.monotonic()
    out = await chat_json(
        db,
        cfg=cfg,
        system="你是一个连通性测试助手。",
        user_text='请只回复这个 JSON：{"ok": true}',
        task="test",
        user=user,
        ref_type="ai_config",
        max_retries=0,
    )
    return {
        "ok": True,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "model": cfg.get("model"),
        "reply": out,
    }
