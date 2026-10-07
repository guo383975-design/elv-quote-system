"""配置/权限变更留痕 测试（2026-10-05）。

证五件事：
  ① 保存设置会留下"改了哪个字段、从什么变成什么"；
  ② 导入配置单独记一类（config_import）；**预演不留痕**（什么都没写）；
  ③ 改 AI 配置会留痕，但**留痕里不含 Key**（连密文也不含）；
  ④ 只有能改设置的人能看留痕（销售 403）；
  ⑤ 留痕带上是谁、从哪个 IP 改的。
"""

from __future__ import annotations

from httpx import AsyncClient


async def _logs(client: AsyncClient, admin_headers, limit: int = 20) -> list[dict]:
    r = await client.get("/api/v1/settings/audit-logs", headers=admin_headers,
                         params={"limit": limit})
    assert r.status_code == 200, r.text
    return r.json()["items"]


async def test_settings_change_is_audited(client: AsyncClient, admin_headers) -> None:
    """① 保存设置留痕，且差异看得出前后值。"""
    r = await client.patch("/api/v1/settings", headers=admin_headers,
                           json={"quote": {"valid_days": 42}})
    assert r.status_code == 200, r.text

    logs = await _logs(client, admin_headers)
    hit = next((x for x in logs if x["action"] == "settings_update"), None)
    assert hit is not None, logs
    assert hit["username"] == "admin", hit
    groups = [g["group"] for g in hit["diff"]]
    assert "quote" in groups, hit["diff"]
    change = next(
        c for g in hit["diff"] if g["group"] == "quote" for c in g["changes"]
        if c["field"] == "valid_days"
    )
    assert change["to"] == "42", change
    assert change["from"] != "42", change
    assert hit["ip"], "留痕里没有 IP"
    assert "保存系统设置" in (hit["summary"] or ""), hit["summary"]


async def test_import_is_audited_but_dry_run_is_not(
    client: AsyncClient, admin_headers
) -> None:
    """② 导入单独记一类；预演不留痕。"""
    before = len(await _logs(client, admin_headers, limit=200))

    doc = {"schema_version": 1, "settings": {"quote": {"valid_days": 44}}}
    await client.post("/api/v1/settings/import", headers=admin_headers,
                      json={"document": doc, "dry_run": True})
    mid = len(await _logs(client, admin_headers, limit=200))
    assert mid == before, "预演竟然留痕了（什么都没写就不该有记录）"

    await client.post("/api/v1/settings/import", headers=admin_headers,
                      json={"document": doc, "dry_run": False})
    logs = await _logs(client, admin_headers)
    assert any(x["action"] == "config_import" for x in logs), logs
    assert len(logs) > before


async def test_ai_config_audited_without_key(
    client: AsyncClient, admin_headers
) -> None:
    """③ 改 AI 配置留痕，但**不记 Key**。"""
    secret = "sk-audit-secret-abcdef1234567890"
    r = await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "audit-model", "api_key": secret})
    assert r.status_code == 200, r.text

    raw = (await client.get("/api/v1/settings/audit-logs", headers=admin_headers,
                            params={"limit": 20})).text
    assert secret not in raw, "留痕里出现了明文密钥！"
    assert "api_key_cipher" not in raw, "留痕里出现了密钥密文！"

    logs = await _logs(client, admin_headers)
    hit = next((x for x in logs if x["action"] == "ai_config"), None)
    assert hit is not None, logs
    assert "重设了 API Key" in (hit["summary"] or ""), hit["summary"]
    fields = [c["field"] for g in hit["diff"] for c in g["changes"]]
    assert "model" in fields and "base_url" in fields, fields

    # 收尾：关掉 AI，别把测试配置留在库里
    await client.put("/api/v1/ai/config", headers=admin_headers,
                     json={"enabled": False, "api_key": "__CLEAR__"})


async def test_audit_logs_require_setting_update(
    client: AsyncClient, sales_headers
) -> None:
    """④ 销售看不到配置留痕（能改设置的人才该看到）。"""
    r = await client.get("/api/v1/settings/audit-logs", headers=sales_headers)
    assert r.status_code == 403, r.text
