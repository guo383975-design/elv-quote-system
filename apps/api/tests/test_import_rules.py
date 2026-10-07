"""导入规则固化（2026-10-05）：新列 + 分类自愈 + 授权命名 + 自动挂靠。

这几条规则都来自真实踩过的坑：
  · 源表「产品类型=软件」的 LIC-* 行其实是授权 → 曾被我放进「主机」节点 ✗
  · TP 类授权的年限写在描述**括号后面** → 取名时丢年限，选择器里分不清 1/3 年 ✗
  · 授权与主机配对、默认项、进价、安装单价 以前全靠导入后手工补 ✗
"""

from __future__ import annotations

import io

from httpx import AsyncClient
from openpyxl import Workbook


def _xlsx(rows: list[list[object]], header: list[str]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "设备清单"
    ws.append(header)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


HDR = ["设备名称", "品牌", "规格型号", "分类路径", "计量单位",
       "参考单价", "进价", "安装单价", "设备描述"]


async def _commit(client: AsyncClient, headers, data: bytes, **form) -> dict:
    payload = {"duplicate_policy": "skip"}
    payload.update({k: str(v).lower() if isinstance(v, bool) else str(v)
                    for k, v in form.items()})
    r = await client.post(
        "/api/v1/imports/devices/commit", headers=headers,
        files={"file": ("t.xlsx", data,
                        "application/vnd.openxmlformats-officedocument."
                        "spreadsheetml.sheet")},
        data=payload)
    assert r.status_code in (200, 201), r.text
    return r.json()


async def test_new_columns_land_on_device(client: AsyncClient, admin_headers) -> None:
    """① 进价 / 安装单价 / 设备描述 三列直接落库（以前模板没有，只能导入后补）。"""
    data = _xlsx([["下一代防火墙", "华为", "FWX-1000", "网络设备", "台",
                   8000, 5000, 300, "吞吐 10Gbps；并发 200 万"]], HDR)
    await _commit(client, admin_headers, data)

    got = (await client.get("/api/v1/devices", headers=admin_headers,
                            params={"keyword": "FWX-1000"})).json()["items"][0]
    assert float(got["reference_price"]) == 8000, got
    assert float(got["cost_price"]) == 5000, got
    assert float(got["install_unit_price"]) == 300, got
    assert got["description"] == "吞吐 10Gbps；并发 200 万", got
    # 设备底价规则（2026-10-05）：导入也按 成本 × 1.35 派生
    from decimal import Decimal

    assert got["min_price"] is not None, got
    assert Decimal(str(got["min_price"])) == Decimal("6750.00"), got  # 5000 × 1.35


async def test_license_row_gets_fixed_category_and_name(
    client: AsyncClient, admin_headers
) -> None:
    """② 授权行就算被写在「主机」分类下、名字只有"威胁防护"，也会被自动纠正。"""
    data = _xlsx([
        ["下一代防火墙", "华为", "FWA-1000", "网络设备/华为安全/A系列/主机", "台",
         8000, 5000, 300, ""],
        # 刻意放错分类 + 名字不带年限（真实源表的形态）
        ["威胁防护", "华为", "LIC-FWA-1000-TP-1Y", "网络设备/华为安全/A系列/主机",
         "套", 1125, 375, 0, "威胁防护(入侵防御+反病毒)1年"],
    ], HDR)
    await _commit(client, admin_headers, data)

    lic = (await client.get("/api/v1/devices", headers=admin_headers,
                            params={"keyword": "LIC-FWA-1000-TP-1Y"})).json()["items"][0]
    assert lic["name"] == "威胁防护 1年（LIC-FWA-1000-TP-1Y）", lic["name"]
    assert "授权" in (lic["category_path"] or ""), lic["category_path"]


async def test_auto_links_license_to_same_named_host(
    client: AsyncClient, admin_headers
) -> None:
    """③ 落库后自动把授权挂到同名主机下，默认项取 ATP-3Y。"""
    data = _xlsx([
        ["下一代防火墙", "华为", "FWB-500", "网络设备/华为安全/B系列/主机", "台",
         8000, 5000, 300, ""],
        ["威胁防护 1年", "华为", "LIC-FWB-500-TP-1Y",
         "网络设备/华为安全/B系列/授权", "套", 1125, 375, 0, ""],
        ["高级威胁防护 3年", "华为", "LIC-FWB-500-ATP-3Y",
         "网络设备/华为安全/B系列/授权", "套", 3780, 1260, 0, ""],
    ], HDR)
    body = await _commit(client, admin_headers, data)
    assert body.get("linked_hosts", 0) >= 1, body
    assert body.get("linked_licenses", 0) >= 2, body

    host = (await client.get("/api/v1/devices", headers=admin_headers,
                             params={"keyword": "FWB-500"})).json()["items"]
    host = next(d for d in host if str(d["model"]) == "FWB-500")
    acc = (await client.get(f"/api/v1/devices/{host['id']}/accessories",
                            headers=admin_headers)).json()
    assert len(acc) == 1 and acc[0]["label"] == "授权", acc
    assert acc[0]["is_required"] is True and acc[0]["select_mode"] == "many", acc
    defaults = [o["name"] for o in acc[0]["options"] if o["is_default"]]
    assert len(defaults) == 1 and "ATP-3Y" in defaults[0], defaults


async def test_install_price_default_for_licenses_is_zero(
    client: AsyncClient, admin_headers
) -> None:
    """④ 文件没写安装单价时：授权=0、其它=参数默认值（300）。"""
    data = _xlsx([
        ["交换机", "华为", "SWX-24", "网络设备", "台", 3000, 2000, 300, ""],
        ["威胁防护 1年", "华为", "LIC-SWX-24-TP-1Y", "网络设备", "套",
         900, 300, 0, ""],
    ], HDR)
    await _commit(client, admin_headers, data)
    devs = (await client.get("/api/v1/devices", headers=admin_headers,
                             params={"page_size": 200})).json()["items"]
    sw = next(d for d in devs if str(d["model"]) == "SWX-24")
    lic = next(d for d in devs if str(d["model"]) == "LIC-SWX-24-TP-1Y")
    assert float(sw["install_unit_price"]) == 300, sw
    assert float(lic["install_unit_price"]) == 0, lic


async def test_ruijie_style_license_links_by_name(
    client: AsyncClient, admin_headers
) -> None:
    """锐捷式命名（授权编号只是主机型号的前缀延伸）也要能自动挂靠。

    华为是 `LIC-<主机>-ATP-3Y`（按型号能推）；锐捷是
    `RG-WALL 1600-NBW7000-01-S-IA…`（主机型号只是它的一段）——
    只按型号推导会全部落空 ✗，必须按**名称包含**兜底（用户 2026-10-05 定的口径）。
    """
    host_model = "RG-WALL 1600-NBW7000-01-S"
    data = _xlsx([
        ["新一代防火墙", "锐捷", host_model, "网络设备/锐捷安全设备/锐捷防火墙主机", "台",
         20000, 12000, 300, ""],
        # 授权：编号以主机型号开头，后面接自己的后缀
        ["三合一特征库授权 1年", "锐捷", f"{host_model}-IAA-LIS-1Y",
         "网络设备/锐捷安全设备/锐捷安全授权", "套", 3000, 1800, 0, ""],
        ["三合一特征库授权 3年", "锐捷", f"{host_model}-IAA-LIS-3Y",
         "网络设备/锐捷安全设备/锐捷安全授权", "套", 7200, 4300, 0, ""],
    ], HDR)
    body = await _commit(client, admin_headers, data)
    assert body.get("linked_licenses", 0) >= 2, body

    devs = (await client.get("/api/v1/devices", headers=admin_headers,
                             params={"page_size": 300})).json()["items"]
    host = next(d for d in devs if str(d["model"]) == host_model)
    acc = (await client.get(f"/api/v1/devices/{host['id']}/accessories",
                            headers=admin_headers)).json()
    assert len(acc) == 1 and acc[0]["label"] == "授权", acc
    assert len(acc[0]["options"]) == 2, acc[0]
    assert acc[0]["is_required"] is True and acc[0]["select_mode"] == "many", acc[0]
