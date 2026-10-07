"""解析方式三档开关（2026-10-05）。

用户的原话：「我想了解是当前 excl 的解析是内置程序解析的还是大模型分析的？
是不是可以增加一个大模型解析的开关？我可以选择用大模型去分析?」

查库里记录确认：他上传的「华为坤灵…渠道.xlsx」三次都是 `engine=rule`
（内置规则），警告里也写着"表头能直接识别，已用规则解析出 123 行（没有调用大模型）"。
后端一直有 `force_ai`，但**界面没有入口** ✗，所以等于不可选。

三档语义（本文件钉住的是"档位真的决定了走不走模型"，而不只是文案）：

    auto  先规则；表头认不出来才调模型
    rule  只用规则：**绝不调模型**（认不出来也不偷偷调，而是明确提示换档）
    llm   强制模型：即使表头能识别也交给模型
"""

from __future__ import annotations

import io

from httpx import AsyncClient
from openpyxl import Workbook


def _xlsx(headers: list[str], rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(headers)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# 表头故意用规则认不出来的写法。
# ⚠ 第一版用了「厂家型号描述/含税供货价」——实测**规则能认出来**（同义词表比我想的强），
# 于是"只用规则不该调模型"这条根本走不到那个分支。换成真正无语义的列名。
_WEIRD = _xlsx(["第一列", "第二列", "第三列"], [["DS-2CD2T46", 620, "含税"]])
# 表头规范、规则能直接认出来
_NORMAL = _xlsx(["名称", "品牌", "型号", "单位", "数量", "单价"],
                [["网络摄像机", "海康威视", "DS-2CD2T46", "台", 10, 620]])


async def _post(client: AsyncClient, headers, data: bytes, **form):
    return await client.post(
        "/api/v1/ai/supplier-quote-parse",
        headers=headers,
        files={"file": ("供应商报价.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"background": "false", **{k: str(v).lower() if isinstance(v, bool) else str(v)
                                        for k, v in form.items()}},
    )


async def _enable_ai(client: AsyncClient, headers) -> None:
    r = await client.put("/api/v1/ai/config", headers=headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "fake-model", "api_key": "sk-fake-1234567890abcdef"})
    assert r.status_code == 200, r.text


async def test_engine_mode_rule_never_calls_the_model(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """`rule` 档：规则认不出来也**不调模型**，而是明确提示换档。"""
    await _enable_ai(client, admin_headers)
    from app.services import ai_quote_parse as parse_mod

    calls = {"n": 0}

    async def spy(*args, **kwargs):
        calls["n"] += 1
        return {"supplier": {}, "items": [], "notes": []}

    monkeypatch.setattr(parse_mod, "chat_json", spy)

    r = await _post(client, admin_headers, _WEIRD, engine_mode="rule")
    assert r.status_code == 200, r.text
    body = r.json()
    assert calls["n"] == 0, f"选了只用规则却调了 {calls['n']} 次模型 ✗"
    assert body["engine"] == "rule", body
    assert body["items"] == [], body
    assert any("只用规则" in str(w) for w in body["warnings"]), body["warnings"]
    assert body.get("engine_mode") == "rule", body


async def test_engine_mode_llm_forces_model_even_with_recognizable_headers(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """`llm` 档：表头能识别也交给模型（用户想"用大模型去分析"时选它）。"""
    await _enable_ai(client, admin_headers)
    from app.services import ai_quote_parse as parse_mod

    calls = {"n": 0}

    async def spy(*args, **kwargs):
        calls["n"] += 1
        return {
            "supplier": {"name": "某供应商"},
            "items": [{"row": 2, "name": "网络摄像机", "brand": "海康威视",
                       "model": "DS-2CD2T46", "unit": "台", "quantity": 10,
                       "unit_price": 620, "amount": 6200, "remark": None}],
            "notes": [],
        }

    monkeypatch.setattr(parse_mod, "chat_json", spy)

    r = await _post(client, admin_headers, _NORMAL, engine_mode="llm")
    assert r.status_code == 200, r.text
    body = r.json()
    assert calls["n"] >= 1, "选了强制大模型却没调模型 ✗"
    assert body["engine"] == "llm", body
    assert len(body["items"]) == 1, body
    assert body.get("engine_mode") == "llm", body


async def test_engine_mode_auto_keeps_rules_first(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """`auto`（默认）行为不变：表头规范就走规则、不花钱。"""
    from app.services import ai_quote_parse as parse_mod

    calls = {"n": 0}

    async def spy(*args, **kwargs):
        calls["n"] += 1
        return {"supplier": {}, "items": [], "notes": []}

    monkeypatch.setattr(parse_mod, "chat_json", spy)

    r = await _post(client, admin_headers, _NORMAL, engine_mode="auto")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "rule", body
    assert calls["n"] == 0, "自动档在规则可用时不该调模型"
    assert body.get("engine_mode") == "auto", body


async def test_legacy_force_ai_still_means_llm(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """兼容：老客户端传 force_ai=true 仍然等于「强制大模型」。"""
    await _enable_ai(client, admin_headers)
    from app.services import ai_quote_parse as parse_mod

    async def spy(*args, **kwargs):
        return {"supplier": {}, "items": [{"row": 2, "name": "X", "model": "M",
                                           "unit": "台", "quantity": 1,
                                           "unit_price": 1, "amount": 1}], "notes": []}

    monkeypatch.setattr(parse_mod, "chat_json", spy)
    r = await _post(client, admin_headers, _NORMAL, force_ai=True)
    assert r.status_code == 200, r.text
    assert r.json()["engine"] == "llm", r.json()
    assert r.json().get("engine_mode") == "llm", r.json()


async def test_invalid_engine_mode_is_rejected(client: AsyncClient, admin_headers) -> None:
    r = await _post(client, admin_headers, _NORMAL, engine_mode="乱填")
    assert r.status_code == 422, r.text
    assert "engine_mode" in r.text
