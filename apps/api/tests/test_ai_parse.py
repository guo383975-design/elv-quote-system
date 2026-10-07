"""AI 供应商报价解析测试（2026-10-04）。

重点验证四条底线：
  1. 规则能读的就不调模型（省钱、快、可复现）；
  2. 解析**不写业务表**；
  3. 落库只认人工确认过的行，且走设备库权限 + #IM 批次；
  4. **模型编造的型号/单价必须被标注出来**（逐字回查）。
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from httpx import AsyncClient
from openpyxl import Workbook


def _quote_xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "报价"
    ws.append(["设备名称", "品牌", "规格型号", "单位", "数量", "单价", "备注"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _device(client: AsyncClient, headers, *, code: str, name: str, model: str,
                  cost: str, ref: str) -> dict:
    r = await client.post("/api/v1/devices", headers=headers, json={
        "code": code, "name": name, "model": model, "category": "video", "unit": "台",
        "reference_price": ref, "cost_price": cost, "install_unit_price": "50"})
    assert r.status_code == 201, r.text
    return r.json()


async def _parse(
    client: AsyncClient, headers, data: bytes, *, force_ai: bool = False,
    background: bool = False,
):
    """默认用**同步**模式（模型已被 monkeypatch 成瞬时返回，断言更直接）。

    生产真实路径是后台模式（默认）；那条路由
    `test_upload_returns_immediately_and_polls_to_ready` 专门覆盖。
    """
    return await client.post(
        "/api/v1/ai/supplier-quote-parse",
        headers=headers,
        files={"file": ("供应商报价.xlsx", data,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"force_ai": str(force_ai).lower(), "background": str(background).lower()},
    )


async def test_rules_engine_parses_without_calling_model(
    client: AsyncClient, admin_headers
) -> None:
    """表头规范的 Excel：走规则，不调用大模型（用量台账里不该出现解析任务）。"""
    dev = await _device(client, admin_headers, code="VAI1", name="网络摄像机",
                        model="DS-2CD2T46", cost="400", ref="680")
    data = _quote_xlsx([["网络摄像机", "海康威视", "DS-2CD2T46", "台", 10, 620, "含税"]])

    r = await _parse(client, admin_headers, data)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "rule", body
    assert len(body["items"]) == 1, body
    item = body["items"][0]
    assert item["model"] == "DS-2CD2T46"
    assert Decimal(str(item["unit_price"])) == Decimal("620")
    # 逐字回查通过（型号与单价都在原文里）
    assert item["verified"] == {"model": True, "unit_price": True}, item
    assert item["needs_review"] is False, item
    # 匹配到了库里的设备
    assert item["match"]["device_id"] == dev["id"], item["match"]
    assert item["match"]["match_type"] == "exact"

    usage = (await client.get("/api/v1/ai/usage", headers=admin_headers)).json()
    assert usage["totals"]["calls"] == 0, "规则解析不该产生模型调用"


async def test_commit_updates_cost_price_and_records_batch(
    client: AsyncClient, admin_headers
) -> None:
    """确认落库：更新进价/最近询价/供货商 + 写 #IM 批次；重复确认被拒。"""
    await _device(client, admin_headers, code="VAI2", name="门禁读卡器",
                  model="AC-100", cost="100", ref="180")
    sup = (await client.post("/api/v1/suppliers", headers=admin_headers,
                             json={"code": "SUP-AI1", "name": "AI 测试供应商"})).json()

    data = _quote_xlsx([["门禁读卡器", "某品牌", "AC-100", "个", 20, 88]])
    parsed = (await _parse(client, admin_headers, data)).json()
    item = parsed["items"][0]
    job_id = parsed["job_id"]

    commit = await client.post(
        f"/api/v1/ai/parse-jobs/{job_id}/commit",
        headers=admin_headers,
        json={
            "supplier_id": sup["id"],
            "rows": [{
                "row": item["row"], "name": item["name"], "brand": item["brand"],
                "model": item["model"], "unit": item["unit"],
                "quantity": item["quantity"], "unit_price": "88",
                "device_id": item["match"]["device_id"], "remark": "含税",
            }],
        },
    )
    assert commit.status_code == 201, commit.text
    result = commit.json()
    assert result["updated"] == 1, result
    assert result["batch_no"].startswith("IM-"), result

    dev = (await client.get(f"/api/v1/devices/{item['match']['device_id']}",
                            headers=admin_headers)).json()
    assert Decimal(str(dev["cost_price"])) == Decimal("88"), dev
    assert dev["supplier_id"] == sup["id"], dev
    assert dev["last_inquiry_price"] is not None, dev

    # 批次留档：类型是 supplier_quote，且写明来自哪次 AI 解析
    batches = (await client.get("/api/v1/imports/batches",
                                params={"kind": "supplier_quote"},
                                headers=admin_headers)).json()["items"]
    assert batches and batches[0]["batch_no"] == result["batch_no"], batches
    assert batches[0]["updated_rows"] == 1
    assert batches[0]["summary"]["ai_job_id"] == job_id
    assert batches[0]["summary"]["source"] == "ai_supplier_quote"

    # 重复确认要拦住（避免同一份报价被计两次账）
    again = await client.post(
        f"/api/v1/ai/parse-jobs/{job_id}/commit", headers=admin_headers,
        json={"rows": [{"name": "门禁读卡器", "unit_price": "88",
                        "device_id": item["match"]["device_id"]}]},
    )
    assert again.status_code == 409, again.text


async def test_unmatched_rows_are_skipped_with_reason(
    client: AsyncClient, admin_headers
) -> None:
    """没匹配到设备的行不落库，但要说清为什么（不静默丢）。"""
    data = _quote_xlsx([["库里没有的奇怪设备", "杂牌", "NO-SUCH-MODEL-999", "台", 1, 10]])
    parsed = (await _parse(client, admin_headers, data)).json()
    item = parsed["items"][0]
    assert item["match"] is None, item

    commit = await client.post(
        f"/api/v1/ai/parse-jobs/{parsed['job_id']}/commit", headers=admin_headers,
        json={"rows": [{"row": item["row"], "name": item["name"],
                        "model": item["model"], "unit_price": "10",
                        "device_id": None}]},
    )
    assert commit.status_code == 201, commit.text
    body = commit.json()
    assert body["updated"] == 0 and body["skipped"] == 1, body
    assert "未匹配到设备库" in body["skipped_detail"][0]["message"]


async def test_fabricated_values_are_flagged(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """**关键**：模型编出来的型号/单价必须被逐字回查抓出来并标注。"""
    await _device(client, admin_headers, code="VAI3", name="交换机", model="SW-24",
                  cost="500", ref="900")
    # 配一个假 key（走 LLM 路径需要 enabled + key），然后把模型调用换成"编造结果"
    cfg = await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "fake-model", "api_key": "sk-fake-1234567890abcdef"})
    assert cfg.status_code == 200, cfg.text

    from app.services import ai_quote_parse as parse_mod

    async def fake_chat_json(*args, **kwargs):
        # 原文里没有 1234.56 这个价，型号也是编的
        return {
            "supplier": {"name": "某供应商"},
            "items": [{
                "row": 2, "name": "交换机", "brand": "编造品牌",
                "model": "FABRICATED-XYZ", "unit": "台",
                "quantity": 2, "unit_price": 1234.56, "amount": 2469.12,
                "remark": None,
            }],
            "notes": [],
        }

    monkeypatch.setattr(parse_mod, "chat_json", fake_chat_json)

    data = _quote_xlsx([["交换机", "华为", "SW-24", "台", 2, 800]])
    r = await _parse(client, admin_headers, data, force_ai=True)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["engine"] == "llm", body
    item = body["items"][0]
    assert item["verified"] == {"model": False, "unit_price": False}, item
    assert item["needs_review"] is True, item
    assert any("未能与原文逐字核对" in w for w in item["warnings"]), item["warnings"]
    assert any("未能在原文中逐字找到" in w for w in body["warnings"]), body["warnings"]


async def test_parse_requires_use_permission(client: AsyncClient, admin_headers) -> None:
    """没有 ai.use 的角色（如 viewer）不能用解析功能。"""
    await client.post("/api/v1/users", headers=admin_headers, json={
        "username": "aiviewer", "email": "aiviewer@example.com",
        "password": "Viewer12345!", "role": "viewer"})
    tok = (await client.post("/api/v1/auth/login/json", json={
        "username": "aiviewer", "password": "Viewer12345!"})).json()["access_token"]
    r = await _parse(client, {"Authorization": f"Bearer {tok}"},
                     _quote_xlsx([["x", "y", "z", "台", 1, 1]]))
    assert r.status_code == 403, r.text


@pytest.mark.parametrize(
    ("name", "ok"),
    [("DS-2CD2T46", True), ("编造的型号", False)],
)
def test_verbatim_check(name: str, ok: bool) -> None:
    """逐字回查函数的单元行为（不依赖 HTTP）。"""
    from app.services.ai_quote_parse import verify_row_against_source

    src = "摄像机 DS-2CD2T46 单价 ￥680.50"
    from app.services.ai_quote_parse import _norm_for_check

    checked = verify_row_against_source({"model": name, "unit_price": 680.5},
                                        _norm_for_check(src))
    assert checked["model"] is ok


async def test_upload_returns_immediately_and_polls_to_ready(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """**后台模式**（真实默认路径）：上传立刻返回 pending，轮询后拿到候选行。

    这条是用户报的「一直在上传」的回归用例：以前同步等模型，最坏
    3 次 × 120 秒超时 = 6 分钟；现在上传立即返回 job_id，前端轮询进度。
    """
    import asyncio

    await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "fake-model", "api_key": "sk-fake-1234567890abcdef"})

    from app.services import ai_quote_parse as parse_mod

    async def fake_chat_json(*args, **kwargs):
        return {
            "supplier": {"name": "某供应商"},
            "items": [{
                "row": 2, "name": "后台解析摄像机", "brand": "海康",
                "model": "ASYNC-1", "unit": "台", "quantity": 5,
                "unit_price": 700, "amount": 3500, "remark": None,
            }],
            "notes": [],
        }

    # 后台任务跑的也是这个模块级函数，替换它对两边都生效
    monkeypatch.setattr(parse_mod, "chat_json", fake_chat_json)

    r = await client.post(
        "/api/v1/ai/supplier-quote-parse",
        headers=admin_headers,
        files={"file": ("异步.xlsx",
                        _quote_xlsx([["后台解析摄像机", "海康", "ASYNC-1", "台", 5, 700]]),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={"force_ai": "true", "background": "true"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    job_id = body["job_id"]
    assert body["status"] == "pending", body        # 立刻返回，不等模型
    assert body["items"] == []

    detail: dict = {}
    for _ in range(60):
        detail = (await client.get(f"/api/v1/ai/parse-jobs/{job_id}",
                                   headers=admin_headers)).json()
        if detail["status"] in ("ready", "failed"):
            break
        await asyncio.sleep(0.05)
    assert detail["status"] == "ready", detail
    items = detail["result"]["items"]
    assert len(items) == 1 and items[0]["model"] == "ASYNC-1", items
    assert items[0]["verified"] == {"model": True, "unit_price": True}, items


async def test_parse_candidates_tool_reports_pending_honestly(db) -> None:
    """任务还没解析完时，工具要如实说「还在解析中」，**不能拿空结果编内容**。"""
    from app.core.permissions import Role
    from app.models.ai import AiParseJob, AiParseStatus
    from app.models.user import User, UserStatus
    from app.services.ai_tools import run_tool

    # 这个用例直接用 db fixture（没走登录），所以自己建一个操作者
    user = User(
        username="toolpending", email="toolpending@example.com",
        hashed_password="x", role=Role.ADMIN, status=UserStatus.ACTIVE,
    )
    db.add(user)
    await db.flush()
    job = AiParseJob(
        kind="supplier_quote", status=str(AiParseStatus.PENDING),
        filename="还在跑.xlsx", created_by=user.id, created_by_name=user.username,
    )
    db.add(job)
    await db.flush()

    out = await run_tool(db, user, "get_parse_candidates", {"job_id": str(job.id)})
    assert out["items"] == [], out
    assert "还在解析中" in out["note"], out


def test_pricelist_headers_parse_without_model() -> None:
    """**渠道价目表**（型号 + 京东控价 + 工程提货价，没有数量/名称列）必须走规则解析。

    用户真实文件（华为坤灵安全报价单2026 渠道.xlsx）就是这种结构：
    以前规则认不出列名 → 交给模型 → 模型输出又被截断 → 整个"解析失败"。
    """
    from app.services.ai_extract import ExtractResult
    from app.services.ai_quote_parse import parse_with_rules

    rows = [
        ["品牌", "产品类型", "产品型号", "产品描述", "京东控价", "工程提货价", "保卡类型", "备注"],
        ["华为", "防火墙", "USG6000F-S112", "交流主机(10*GE RJ45)", "3999", "1600", "1类", ""],
        ["华为", "交换机", "S5735-L24T4S", "24口千兆", "2680", "1180", "1类", ""],
    ]
    items = parse_with_rules(ExtractResult(engine="xlsx", text="", rows=rows))
    assert len(items) == 2, items

    first = items[0]
    assert first["model"] == "USG6000F-S112"       # 型号列没被"产品"抢去当名称
    assert first["name"] == "USG6000F-S112"        # 没有名称列 → 用型号当名称
    assert first["unit_price"] == "1600"           # 工程提货价 = 我们的进价
    assert first["reference_price"] == "3999"      # 京东控价 = 对外参考价（不混一起）
    assert first["brand"] == "华为"
    assert first["quantity"] == "1"                # 价目表没有数量列 → 按 1
    assert first["quantity_defaulted"] is True     # 并且标记"这个 1 是补的"
    assert first["spec"] == "交流主机(10*GE RJ45)"


def test_long_price_list_is_split_into_batches() -> None:
    """行数多要分批：整份 JSON 装不下会被截断（这就是用户那次失败的根因）。"""
    from app.services.ai_extract import ExtractResult
    from app.services.ai_quote_parse import _split_extract

    rows = [["型号", "单价"], *[[f"M{i}", str(i * 10)] for i in range(1, 57)]]
    batches = _split_extract(ExtractResult(engine="xlsx", text="", rows=rows))
    assert len(batches) == 3, [len(b.rows) - 1 for b in batches]
    assert [len(b.rows) - 1 for b in batches] == [25, 25, 6]   # 56 行数据
    # 每批都带表头，且**行号与原文一致**（人工核对要靠它）
    assert "型号" in batches[0].text.splitlines()[1]   # 第 0 行是 "### 表头" 标题
    # 第 3 批的第一条数据行号 = 1(表头) + 50 + 1 = 52
    assert batches[2].text.splitlines()[3].startswith("52"), batches[2].text[:120]
    # 行数不多时不分批（行为与以前一致）
    small = _split_extract(ExtractResult(engine="xlsx", text="", rows=rows[:6]))
    assert len(small) == 1


async def test_truncated_output_is_split_instead_of_failing(
    client: AsyncClient, admin_headers, monkeypatch
) -> None:
    """模型输出被截断时，要把这批**拆小重试**，而不是直接失败。

    截断是"装不下"不是"不会写格式"，重发同样的请求毫无意义 ——
    这条用例模拟"一批超过 3 行就截断"，验证系统会自己拆到能装下为止。
    """
    await client.put("/api/v1/ai/config", headers=admin_headers, json={
        "enabled": True, "base_url": "https://api.example.com/v1",
        "model": "fake-model", "api_key": "sk-fake-1234567890abcdef"})

    from app.services import ai_quote_parse as parse_mod
    from app.services.ai_client import AiTruncatedError

    calls: list[int] = []

    async def fake_chat_json(*args, **kwargs):
        text = kwargs.get("user_text") or (args[3] if len(args) > 3 else "")
        # 只数"数据行"（表头行在另一个小节里）
        data_part = str(text).split("### 数据行（行号同原文）")[-1]
        # 未分批时原文第 1 行是**表头**（真模型不会把它当成一条明细），这里跳过
        data_rows = [
            ln for ln in data_part.splitlines()
            if "\t" in ln and "型号" not in ln
        ]
        calls.append(len(data_rows))
        # 模拟"超过 6 行就装不下"。阈值必须大于算法能拆到的最小粒度
        # （`_split_extract` 最少 3 行一批），否则这道题无解。
        if len(data_rows) > 6:
            raise AiTruncatedError("模型输出被截断（测试模拟）")
        return {
            "supplier": {}, "notes": [],
            "items": [
                {"row": int(ln.split("\t")[0]), "name": ln.split("\t")[1],
                 "model": ln.split("\t")[1], "unit_price": ln.split("\t")[2].strip()}
                for ln in data_rows
            ],
        }

    monkeypatch.setattr(parse_mod, "chat_json", fake_chat_json)

    rows = [["型号", "单价"], *[[f"T{i}", str(i)] for i in range(1, 9)]]   # 8 行数据
    data = _quote_xlsx(rows)
    r = await client.post("/api/v1/ai/supplier-quote-parse", headers=admin_headers,
                          files={"file": ("长价目表.xlsx", data,
                                          "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
                          data={"force_ai": "true", "background": "false"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 8, body          # 拆小之后一行都没丢
    assert max(calls) > 6, calls                  # 确实撞上过截断
    assert min(calls) <= 4, calls                 # 也确实拆到能装下的粒度
