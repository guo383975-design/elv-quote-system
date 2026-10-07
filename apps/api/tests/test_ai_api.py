from httpx import AsyncClient

# ---------------------------------------------------------------------------
# AI 助理 P0：配置与密钥安全（2026-10-04）
# ---------------------------------------------------------------------------

async def test_ai_config_requires_admin(client: AsyncClient, admin_headers) -> None:
    """只有 admin 能读写 AI 配置（能改 base_url 的人可以把数据导走）。"""
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "aimgr", "email": "aimgr@example.com",
        "password": "Mgr12345!", "role": "manager"})
    tok = (await client.post("/api/v1/auth/login/json", json={
        "username": "aimgr", "password": "Mgr12345!"})).json()["access_token"]
    h = {"Authorization": f"Bearer {tok}"}

    assert (await client.get("/api/v1/ai/config", headers=admin_headers)).status_code == 200
    denied_read = await client.get("/api/v1/ai/config", headers=h)
    assert denied_read.status_code == 403, denied_read.text
    denied_write = await client.put("/api/v1/ai/config", headers=h, json={"enabled": True})
    assert denied_write.status_code == 403, denied_write.text


async def test_ai_api_key_never_returned(client: AsyncClient, admin_headers) -> None:
    """Key 只在请求体里出现一次：存库加密、接口只回掩码、通用设置出口也拿不到。"""
    secret = "sk-live-abcdefghijklmnopqrstuvwxyz"
    r = await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "test-model", "api_key": secret,
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert "api_key_cipher" not in body, body
    assert body["api_key_set"] is True
    assert body["api_key_hint"].startswith("sk-") and "abcdefghij" not in body["api_key_hint"]
    assert secret not in r.text

    # 读一次也拿不到
    got = await client.get("/api/v1/ai/config", headers=admin_headers)
    assert secret not in got.text and "api_key_cipher" not in got.json(), got.text

    # 通用设置出口（/settings）同样不能泄露
    allset = await client.get("/api/v1/settings", headers=admin_headers)
    assert secret not in allset.text, "通用设置接口泄露了 API Key！"
    assert "api_key_cipher" not in allset.json().get("ai", {}), allset.json().get("ai")

    # 通用写入也不能覆盖密钥字段（否则等于绕过加密）
    tampered = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "ai": {"api_key_cipher": "明文假冒", "model": "another-model"}})
    assert tampered.status_code == 200, tampered.text
    assert tampered.json()["ai"]["api_key_set"] is True, "密钥被通用接口改掉了"
    after = await client.get("/api/v1/ai/config", headers=admin_headers)
    assert after.json()["api_key_set"] is True
    assert after.json()["model"] == "another-model", "非密钥字段应能通过通用接口改"


async def test_ai_key_clear_and_keep(client: AsyncClient, admin_headers) -> None:
    """空字符串 = 不改（表单回显掩码常见）；__CLEAR__ = 明确清空。"""
    await client.put("/api/v1/ai/config", headers=admin_headers,
                     json={"api_key": "sk-keep-1234567890abcdef"})
    # 传空 → 保持不变
    await client.put("/api/v1/ai/config", headers=admin_headers, json={"api_key": ""})
    assert (await client.get("/api/v1/ai/config", headers=admin_headers)).json()["api_key_set"] is True

    # 显式清空
    await client.put("/api/v1/ai/config", headers=admin_headers, json={"api_key": "__CLEAR__"})
    cleared = (await client.get("/api/v1/ai/config", headers=admin_headers)).json()
    assert cleared["api_key_set"] is False and cleared["api_key_hint"] == ""


async def test_ai_config_rejects_garbage_key(client: AsyncClient, admin_headers) -> None:
    bad = await client.put("/api/v1/ai/config", headers=admin_headers,
                           json={"api_key": "这不是一个 key"})
    assert bad.status_code == 422, bad.text


async def test_provider_request_has_no_max_tokens(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """发给服务商的请求体里**不许**再带 max_tokens。

    用户要求"不限 token、把这个设置删掉" —— 由服务商用自己的输出上限。
    这条断言防止哪天有人又把它加回去（加了就等于悄悄给输出设了上限，
    大文件解析会被截断，而这个坑我们刚踩过）。
    """
    import httpx

    from app.services import ai_client

    captured: dict = {}

    class FakeResponse:
        status_code = 200
        text = "{}"

        def json(self):
            return {
                "choices": [{"message": {"content": '{"ok": true}'},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, headers=None, json=None):
            captured["payload"] = json
            return FakeResponse()

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    cfg = {
        "enabled": True,               # require_ready 会检查开关
        "provider": "openai_compatible",
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test-1234567890",
        "model": "fake-model",
        "temperature": 0.1,
        "timeout_seconds": 30,
    }
    out = await ai_client.chat_json(
        None, cfg=cfg, system="s", user_text="u", task="test",
    )
    assert out == {"ok": True}, out
    payload = captured.get("payload") or {}
    assert "max_tokens" not in payload, payload
    assert "max_completion_tokens" not in payload, payload
    assert payload.get("model") == "fake-model"
