"""配置导出 / 导入 测试（2026-10-05）。

证五件事：
  ① 导出**不含任何密钥**（这是最要紧的一条：导出文件会被随手发给人）；
  ② 导入默认只预演（dry_run），不落库；
  ③ 确认应用后生效，且**走与 PATCH 相同的校验**（条款变量、角色名、权限矩阵）；
  ④ 版本不对 / 结构不对 → 422；
  ⑤ 文件里塞的密钥字段会被剥掉（文件可能来自别处）。
"""

from __future__ import annotations

from httpx import AsyncClient


async def test_export_never_contains_secrets(
    client: AsyncClient, admin_headers
) -> None:
    """① 导出文件里不许出现密钥或其密文。"""
    # 先配一个 Key（密文只存服务端）
    await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "fake-model", "api_key": "sk-secret-abcdef1234567890"})

    r = await client.get("/api/v1/settings/export", headers=admin_headers)
    assert r.status_code == 200, r.text
    doc = r.json()
    assert doc["schema_version"] == 1, doc
    assert doc["exported_by"] == "admin", doc

    raw = r.text
    for leak in ("sk-secret", "api_key_cipher", "api_key_hint"):
        assert leak not in raw, f"导出文件里出现了 {leak}"
    assert doc["settings"]["ai"]["api_key_set"] is True, doc["settings"]["ai"]

    # 导出内容应带上权限与角色（换机器要还原的就是这些）
    # 字段可见性已并入 permissions（2026-10-05）：导出里不再有独立的 visibility 组
    assert "permissions" in doc["settings"]
    assert "roles" in doc["settings"]
    assert "visibility" not in doc["settings"], "旧的 visibility 组不该再出现"


async def test_import_dry_run_then_apply(
    client: AsyncClient, admin_headers
) -> None:
    """②③ 预演看得见差异，确认后才落库并生效。"""
    before = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert before["quote"]["valid_days"] == 30

    doc = {
        "schema_version": 1,
        "settings": {
            "quote": {"valid_days": 45},
            # 字段级可见性（成本/利润）2026-10-05 起就是矩阵里的两行，
            # 不再是独立的 visibility 组
            "permissions": {"overrides": {"purchaser": {"data_profit": ["read"]}}},
        },
    }
    dry = await client.post("/api/v1/settings/import", headers=admin_headers,
                            json={"document": doc, "dry_run": True})
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True and body["group_count"] >= 1, body
    assert any(g["group"] == "quote" for g in body["diff"]), body["diff"]
    # 预演不落库
    mid = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert mid["quote"]["valid_days"] == 30, "预演竟然写进去了"

    real = await client.post("/api/v1/settings/import", headers=admin_headers,
                             json={"document": doc, "dry_run": False})
    assert real.status_code == 200, real.text
    assert real.json()["dry_run"] is False
    after = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert after["quote"]["valid_days"] == 45, after["quote"]

    # 条款文本也跟着变（导入的是 quote 组，渲染同源）
    # 还原，别影响其它用例
    await client.patch("/api/v1/settings", headers=admin_headers,
                       json={"quote": {"valid_days": 30}})


async def test_import_validates_like_patch(client: AsyncClient, admin_headers) -> None:
    """③ 导入走同一套校验：条款变量、未知角色都要被拦。"""
    bad_clause = await client.post("/api/v1/settings/import", headers=admin_headers, json={
        "document": {"schema_version": 1,
                     "settings": {"quote": {"clauses": ["有效期 {valid_dayss} 天"]}}},
        "dry_run": True})
    assert bad_clause.status_code == 422 and "valid_dayss" in bad_clause.text

    bad_role = await client.post("/api/v1/settings/import", headers=admin_headers, json={
        "document": {"schema_version": 1,
                     "settings": {"permissions": {
                         "overrides": {"engineer": {"quote": ["read"]}}}}},
        "dry_run": True})
    assert bad_role.status_code == 422 and "engineer" in bad_role.text

    bad_admin = await client.post("/api/v1/settings/import", headers=admin_headers, json={
        "document": {"schema_version": 1,
                     "settings": {"permissions": {"overrides": {"admin": {"quote": []}}}}},
        "dry_run": True})
    assert bad_admin.status_code == 422 and "admin" in bad_admin.text


async def test_import_rejects_bad_document(client: AsyncClient, admin_headers) -> None:
    """④ 版本/结构不对要 422（而不是悄悄导入一半）。"""
    v = await client.post("/api/v1/settings/import", headers=admin_headers,
                          json={"document": {"schema_version": 99, "settings": {}}})
    assert v.status_code == 422 and "版本" in v.json()["detail"]

    s = await client.post("/api/v1/settings/import", headers=admin_headers,
                          json={"document": {"schema_version": 1}})
    assert s.status_code == 422 and "settings" in s.json()["detail"]


async def test_import_strips_keys_from_file(client: AsyncClient, admin_headers) -> None:
    """⑤ 文件里带的密钥字段会被剥掉（文件可能来自别处）。"""
    r = await client.post("/api/v1/settings/import", headers=admin_headers, json={
        "document": {"schema_version": 1, "settings": {
            "ai": {"enabled": True, "base_url": "https://evil.example.com/v1",
                   "model": "x", "api_key_cipher": "明文塞进来的密钥",
                   "api_key_hint": "sk-****"}}},
        "dry_run": False})
    assert r.status_code == 200, r.text
    cfg = (await client.get("/api/v1/ai/config", headers=admin_headers)).json()
    assert cfg.get("api_key_set") is False, "文件里的密钥竟然被写进去了"
    # base_url 属于正常配置，允许跟着导入（用户自己的选择）
    assert cfg["base_url"] == "https://evil.example.com/v1", cfg
    # 收尾：不要让这台的 AI 配置停在这个地址上
    await client.put("/api/v1/ai/config", headers=admin_headers,
                     json={"enabled": False, "base_url": "https://api.deepseek.com/v1"})
