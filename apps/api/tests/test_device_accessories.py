"""配件/授权（2026-10-05）：产品库配置 + 配单提示。

用户需求原文：「产品库产品需要增加一个配件功能。比如说买防火墙需要购买授权，
里面可以选择不同的授权，在销售配单的时候也要提示需要添加授权。」

覆盖：
  ① 产品库给设备配"槽 + 候选"（不同档授权），带参考价返回；
  ② 校验：不能把自己当配件、同槽不能重复候选、单选槽只能一个默认、槽名不能空；
  ③ 配单检查：加了防火墙没配授权 → 提示；补上授权（带 parent_item_id）→ 不再提示；
  ④ 配件行不能跨单挂靠（parent_item_id 必须属于本单）。
"""

from __future__ import annotations

from httpx import AsyncClient


async def _device(client: AsyncClient, headers, *, code: str, name: str,
                  model: str, cost: str = "100", ref: str = "200") -> dict:
    r = await client.post("/api/v1/devices", headers=headers, json={
        "code": code, "name": name, "model": model, "category": "network",
        "unit": "台", "reference_price": ref, "cost_price": cost})
    assert r.status_code == 201, r.text
    return r.json()


async def _quote(client: AsyncClient, headers, *, code: str) -> dict:
    prj = (await client.post("/api/v1/projects", headers=headers, json={
        "code": f"ACC-{code}", "name": "配件测试项目", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    r = await client.post("/api/v1/quotes", headers=headers, json={
        "project_id": prj["id"], "code": f"ACC-Q-{code}", "title": "配件测试单",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0.15", "tax_rate": "0.09"})
    assert r.status_code == 201, r.text
    return r.json()


async def _add_item(client: AsyncClient, headers, quote_id: str, **body) -> dict:
    r = await client.post(f"/api/v1/quotes/{quote_id}/items", headers=headers, json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def test_configure_and_read_accessories(client: AsyncClient, admin_headers) -> None:
    """① 给防火墙配「授权」槽，两种授权可选（一种默认），能读回来且带参考价。"""
    fw = await _device(client, admin_headers, code="FW-ACC1", name="下一代防火墙",
                       model="FW-1000", cost="5000", ref="8000")
    lic1 = await _device(client, admin_headers, code="LIC-1Y", name="防火墙授权（1年）",
                         model="LIC-1Y", cost="800", ref="1500")
    lic3 = await _device(client, admin_headers, code="LIC-3Y", name="防火墙授权（3年）",
                         model="LIC-3Y", cost="1800", ref="3200")

    r = await client.put(f"/api/v1/devices/{fw['id']}/accessories", headers=admin_headers,
                         json={"groups": [{
                             "label": "授权", "is_required": True, "select_mode": "one",
                             "quantity_mode": "per_unit", "default_quantity": 1,
                             "options": [
                                 {"accessory_id": lic1["id"], "is_default": True},
                                 {"accessory_id": lic3["id"]},
                             ]}]})
    assert r.status_code == 200, r.text
    groups = r.json()
    assert len(groups) == 1 and groups[0]["label"] == "授权", groups
    assert groups[0]["is_required"] is True, groups
    names = {o["name"] for o in groups[0]["options"]}
    assert names == {"防火墙授权（1年）", "防火墙授权（3年）"}, names
    # 候选要带参考价（界面要显示"选哪种、多少钱"）
    assert all(o["reference_price"] for o in groups[0]["options"]), groups

    # 读回来一致
    got = (await client.get(f"/api/v1/devices/{fw['id']}/accessories",
                            headers=admin_headers)).json()
    assert len(got) == 1 and len(got[0]["options"]) == 2, got

    # 整体替换：改成只有一个候选 → 旧的应被替换掉（不是追加）
    r2 = await client.put(f"/api/v1/devices/{fw['id']}/accessories", headers=admin_headers,
                          json={"groups": [{"label": "授权", "options": [
                              {"accessory_id": lic3["id"]}]}]})
    assert r2.status_code == 200, r2.text
    assert len(r2.json()) == 1 and len(r2.json()[0]["options"]) == 1, r2.json()


async def test_accessory_validations(client: AsyncClient, admin_headers) -> None:
    """② 脏数据必须在接口挡住。"""
    fw = await _device(client, admin_headers, code="FW-ACC2", name="防火墙2", model="FW-2")
    lic = await _device(client, admin_headers, code="LIC-A", name="授权A", model="A")

    async def put(groups: list[dict]) -> int:
        r = await client.put(f"/api/v1/devices/{fw['id']}/accessories",
                             headers=admin_headers, json={"groups": groups})
        return r.status_code

    # 不能把自己当配件
    assert await put([{"label": "授权",
                       "options": [{"accessory_id": fw["id"]}]}]) == 422
    # 同一个槽里重复候选
    assert await put([{"label": "授权", "options": [
        {"accessory_id": lic["id"]}, {"accessory_id": lic["id"]}]}]) == 422
    # 单选槽只能一个默认
    lic2 = await _device(client, admin_headers, code="LIC-B", name="授权B", model="B")
    assert await put([{"label": "授权", "select_mode": "one", "options": [
        {"accessory_id": lic["id"], "is_default": True},
        {"accessory_id": lic2["id"], "is_default": True}]}]) == 422
    # 槽名不能空 / 不能重名
    assert await put([{"label": "  ", "options": []}]) == 422
    assert await put([{"label": "授权", "options": []},
                      {"label": "授权", "options": []}]) == 422
    # 候选不存在
    assert await put([{"label": "授权", "options": [
        {"accessory_id": "00000000-0000-0000-0000-000000000000"}]}]) == 422


async def test_quote_prompts_missing_accessory(
    client: AsyncClient, admin_headers
) -> None:
    """③ 配单提示：加了防火墙没配授权 → 提示；补上 → 不再提示。"""
    fw = await _device(client, admin_headers, code="FW-ACC3", name="防火墙3",
                       model="FW-3", cost="5000", ref="8000")
    lic = await _device(client, admin_headers, code="LIC-C", name="授权C", model="C",
                        cost="900", ref="1600")
    await client.put(f"/api/v1/devices/{fw['id']}/accessories", headers=admin_headers,
                     json={"groups": [{"label": "授权", "is_required": True,
                                       "quantity_mode": "per_unit",
                                       "options": [{"accessory_id": lic["id"],
                                                    "is_default": True}]}]})
    qt = await _quote(client, admin_headers, code="P")

    # 只加防火墙 → 检查应提示缺授权
    fw_item = await _add_item(client, admin_headers, qt["id"], device_id=fw["id"],
                              name=fw["name"], quantity="3", unit_price="8000")
    check = (await client.get(f"/api/v1/quotes/{qt['id']}/accessory-check",
                              headers=admin_headers)).json()
    assert len(check["missing"]) == 1, check
    m = check["missing"][0]
    assert m["group_label"] == "授权" and m["item_id"] == fw_item["id"], m
    assert m["quantity"] == "3.000", m          # 数量随主设备（per_unit）
    assert len(m["options"]) == 1, m
    assert "还没选" in check["summary"], check
    assert m["chosen"] == [], m

    # 补上授权（挂在这行下面）→ 不再提示
    await _add_item(client, admin_headers, qt["id"], device_id=lic["id"],
                    name=lic["name"], quantity="3", unit_price="1600",
                    parent_item_id=fw_item["id"])
    check2 = (await client.get(f"/api/v1/quotes/{qt['id']}/accessory-check",
                               headers=admin_headers)).json()
    assert check2["missing"] == [], check2
    assert "齐全" in check2["summary"], check2


async def test_parent_item_must_belong_to_same_quote(
    client: AsyncClient, admin_headers
) -> None:
    """④ 配件行不能跨单挂靠。"""
    qt1 = await _quote(client, admin_headers, code="X1")
    qt2 = await _quote(client, admin_headers, code="X2")
    parent = await _add_item(client, admin_headers, qt1["id"], name="主设备",
                             quantity="1", unit_price="100")
    r = await client.post(f"/api/v1/quotes/{qt2['id']}/items", headers=admin_headers,
                          json={"name": "配件", "quantity": "1", "unit_price": "10",
                                "parent_item_id": parent["id"]})
    assert r.status_code == 422, r.text
    assert "本报价单" in r.text


async def test_accessories_require_update_permission(
    client: AsyncClient, admin_headers, sales_headers
) -> None:
    """配件的写接口要 device_library.update —— 销售不该能改产品库。"""
    fw = await _device(client, admin_headers, code="FW-ACC4", name="防火墙4", model="FW-4")
    r = await client.put(f"/api/v1/devices/{fw['id']}/accessories", headers=sales_headers,
                         json={"groups": []})
    # 销售对设备库只有 read（无 update）
    assert r.status_code == 403, r.text


def test_match_host_by_name_prefers_longest() -> None:
    """按名称匹配主机时**取最长的那个**。

    主机型号之间可能是前缀关系（锐捷就是：`RG-WALL 1600-NBW7000-01-S` 是
    `RG-WALL 1600-NBW7000-01-S-IA` 的前缀）。不按长度降序取，就会把
    `-IA` 的授权挂到基础款主机上 —— 报价时选错型号、钱就错了 ✗。
    """
    from app.services.device_accessories import match_host_by_name

    class _H:
        def __init__(self, model: str) -> None:
            self.model = model

    hosts = [_H("RG-WALL 1600-NBW7000-01-S"), _H("RG-WALL 1600-NBW7000-01-S-IA")]
    hit = match_host_by_name("RG-WALL 1600-NBW7000-01-S-IA-LIS-1Y", "", hosts)
    assert hit is not None and hit.model == "RG-WALL 1600-NBW7000-01-S-IA", hit

    # 华为那种"编号里带主机型号"的也要命中
    hosts2 = [_H("USG6000F-S112")]
    hit2 = match_host_by_name("LIC-USG6000F-S112-ATP-3Y", "高级威胁防护 3年", hosts2)
    assert hit2 is not None and hit2.model == "USG6000F-S112", hit2

    # 毫不相干的授权不该乱挂
    assert match_host_by_name("ABC-123", "无关授权", hosts2) is None
