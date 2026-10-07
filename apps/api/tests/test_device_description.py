"""设备描述 + 报价单"设备参数"栏（2026-10-05）。

用户需求（修正后）：「不需要单独设置参数接口，只需要有一个添加描述的地方就好。
在选择产品的时候鼠标放上去会显示参数或者直接显示在报价单的设备参数一栏。」

所以这里钉住四件事：
  ① 设备能存/改**描述**（多行，性能参数就写在这儿）；
  ② 加设备进报价单时，描述**自动带成**「设备参数」（快照）；
  ③ 显式传了设备参数就不覆盖（手工改的内容说了算）；
  ④ 导出件（Excel）里有「设备参数」这一栏，且写的是那份快照。
"""

from __future__ import annotations

import io

from httpx import AsyncClient
from openpyxl import load_workbook

PARAMS = "吞吐量 10 Gbps；并发连接 200 万；接口 8×GE + 4×SFP+；IPsec 隧道 2000 条"


async def _device(client: AsyncClient, headers, *, code: str, desc: str | None) -> dict:
    body = {"code": code, "name": f"设备{code}", "model": code, "category": "network",
            "unit": "台", "reference_price": "8000", "cost_price": "5000"}
    if desc is not None:
        body["description"] = desc
    r = await client.post("/api/v1/devices", headers=headers, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_device_description_round_trip(
    client: AsyncClient, admin_headers
) -> None:
    """① 描述能存能改，详情里带得回来。"""
    dev = await _device(client, admin_headers, code="DSC1", desc=PARAMS)
    assert dev["description"] == PARAMS, dev

    got = (await client.get(f"/api/v1/devices/{dev['id']}", headers=admin_headers)).json()
    assert got["description"] == PARAMS, got

    r = await client.patch(f"/api/v1/devices/{dev['id']}", headers=admin_headers,
                           json={"description": "改后的参数"})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "改后的参数", r.json()


async def test_item_spec_text_is_filled_from_device(
    client: AsyncClient, admin_headers
) -> None:
    """② 加设备进报价单 → 设备参数自动带上；③ 显式传了就不覆盖。"""
    dev = await _device(client, admin_headers, code="DSC2", desc=PARAMS)
    prj = (await client.post("/api/v1/projects", headers=admin_headers, json={
        "code": "DSC-P", "name": "描述测试项目", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    qt = (await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": prj["id"], "code": "DSC-Q", "title": "描述测试单",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0.15", "tax_rate": "0.09"})).json()

    r = await client.post(f"/api/v1/quotes/{qt['id']}/items", headers=admin_headers,
                          json={"device_id": dev["id"], "name": dev["name"],
                                "quantity": "2", "unit_price": "8000"})
    assert r.status_code == 201, r.text
    assert r.json()["spec_text"] == PARAMS, r.json()

    # 显式给了设备参数 → 保留调用方的（可能是针对本单的定制说明）
    r2 = await client.post(f"/api/v1/quotes/{qt['id']}/items", headers=admin_headers,
                           json={"device_id": dev["id"], "name": dev["name"],
                                 "quantity": "1", "unit_price": "8000",
                                 "spec_text": "本单特殊配置：双电源"})
    assert r2.status_code == 201, r2.text
    assert r2.json()["spec_text"] == "本单特殊配置：双电源", r2.json()

    # 手工录入（无设备）也能填设备参数
    r3 = await client.post(f"/api/v1/quotes/{qt['id']}/items", headers=admin_headers,
                           json={"name": "手动行", "quantity": "1", "unit_price": "100",
                                 "spec_text": "手动写的参数"})
    assert r3.status_code == 201, r3.text
    assert r3.json()["spec_text"] == "手动写的参数", r3.json()


async def test_export_has_device_params_column(
    client: AsyncClient, admin_headers
) -> None:
    """④ 导出的 Excel 里有「设备参数」栏，写的是那份快照。"""
    dev = await _device(client, admin_headers, code="DSC3", desc="吞吐 5 Gbps")
    prj = (await client.post("/api/v1/projects", headers=admin_headers, json={
        "code": "DSC-P3", "name": "导出测试项目", "customer_name": "客户",
        "create_default_subsystems": False})).json()
    qt = (await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": prj["id"], "code": "DSC-Q3", "title": "导出测试单",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0.15", "tax_rate": "0.09"})).json()
    await client.post(f"/api/v1/quotes/{qt['id']}/items", headers=admin_headers,
                      json={"device_id": dev["id"], "name": dev["name"],
                            "quantity": "1", "unit_price": "8000"})
    await client.post(f"/api/v1/quotes/{qt['id']}/compute", headers=admin_headers)

    r = await client.get(f"/api/v1/quotes/{qt['id']}/export.xlsx", headers=admin_headers)
    assert r.status_code == 200, r.text
    wb = load_workbook(io.BytesIO(r.content))
    text = " ".join(
        str(c.value)
        for ws in wb.worksheets for row in ws.iter_rows() for c in row
        if c.value is not None
    )
    assert "设备参数" in text, "导出件里没有「设备参数」栏"
    assert "吞吐 5 Gbps" in text, "导出件里没写设备参数的值"


async def test_spec_column_only_when_present(
    client: AsyncClient, admin_headers
) -> None:
    """没填设备参数的单子，明细表**不该**多出这一列（否则所有单子都变宽）。

    实测过：无条件加列会把客户版 PDF 从 1 页挤到 2 页 —— 多数单子没填参数，
    不该为这个功能付排版代价。表头与单元格必须同条件，否则列数错位。
    """
    from app.services.export_data import ExportContext, ExportItemRow, ExportMeta

    meta = ExportMeta(
        quote_code="Q", title="t", version=1, status_label="草稿", project_code="P",
        project_name="p", customer_name="c", customer_contact="", customer_phone="",
        address="", tax_method_label="", tax_rate_pct="9%", generated_at="now",
        include_cost=False,
    )
    plain = ExportContext(meta=meta, items=[
        ExportItemRow(seq=1, subsystem="", name="设备", brand="", model="M",
                      unit="台", quantity="1", unit_price="1", amount="1",
                      source_label="", note=""),
    ])
    assert "设备参数" not in plain.item_headers(), plain.item_headers()
    assert len(plain.item_headers()) == len(plain.item_row_cells(plain.items[0])), \
        "表头与单元格列数必须一致"

    withspec = ExportContext(meta=meta, items=[
        ExportItemRow(seq=1, subsystem="", name="设备", brand="", model="M",
                      spec_text="吞吐 10Gbps", unit="台", quantity="1",
                      unit_price="1", amount="1", source_label="", note=""),
    ])
    assert "设备参数" in withspec.item_headers(), withspec.item_headers()
    assert "吞吐 10Gbps" in withspec.item_row_cells(withspec.items[0])
    assert len(withspec.item_headers()) == len(
        withspec.item_row_cells(withspec.items[0])
    ), "有参数时表头与单元格列数也必须一致"
