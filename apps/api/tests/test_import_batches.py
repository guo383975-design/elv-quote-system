"""导入批次留档（#IM-xxx）测试（2026-10-04）。

对应原型里的 `#IM-20260930-001`：每次导入都要能回答"谁、什么时候、导了什么文件、
成功多少、哪几行失败"。以前只有一条 log，界面上查不到。
"""

from __future__ import annotations

import io

from httpx import AsyncClient
from openpyxl import Workbook


def _device_xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.append(["设备名称", "品牌", "规格型号", "单位", "参考单价"])
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


async def _commit_devices(client: AsyncClient, headers, rows: list[list[object]]):
    return await client.post(
        "/api/v1/imports/devices/commit",
        files={
            "file": (
                "批次测试.xlsx",
                _device_xlsx(rows),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
        data={"duplicate_policy": "skip", "skip_errors": "true"},
        headers=headers,
    )


async def test_device_import_records_batch(client: AsyncClient, admin_headers) -> None:
    r = await _commit_devices(
        client, admin_headers,
        [["批次测试摄像机", "海康", "BATCH-CAM-001", "台", 500]],
    )
    assert r.status_code == 201, r.text
    batch_no = r.json().get("batch_no")
    assert batch_no and batch_no.startswith("IM-"), r.json()

    listed = await client.get("/api/v1/imports/batches", headers=admin_headers)
    assert listed.status_code == 200, listed.text
    items = listed.json()["items"]
    assert items, "导入后应该有批次留档"
    top = items[0]
    assert top["batch_no"] == batch_no
    assert top["kind"] == "device"
    assert top["total_rows"] == 1
    assert top["created_rows"] == 1
    assert top["status"] == "success"
    assert top["filename"] and "批次测试" in top["filename"]

    detail = await client.get(f"/api/v1/imports/batches/{top['id']}", headers=admin_headers)
    assert detail.status_code == 200, detail.text
    assert detail.json()["batch_no"] == batch_no


async def test_batch_records_failed_rows_with_line_numbers(
    client: AsyncClient, admin_headers
) -> None:
    """失败行要留下**行号 + 原因**，用户才能回去改表。"""
    r = await _commit_devices(
        client, admin_headers,
        [
            ["正常设备", "品牌", "BATCH-OK-1", "台", 100],
            ["", "品牌", "BATCH-BAD-1", "台", 100],       # 缺名称（第 3 行）
        ],
    )
    assert r.status_code == 201, r.text
    assert r.json()["errors"] == 1

    items = (await client.get("/api/v1/imports/batches", headers=admin_headers)).json()["items"]
    batch = next(b for b in items if b["batch_no"] == r.json()["batch_no"])
    assert batch["status"] == "partial", batch
    assert batch["error_rows"] == 1

    detail = (
        await client.get(f"/api/v1/imports/batches/{batch['id']}", headers=admin_headers)
    ).json()
    assert detail["errors"], detail
    assert detail["errors"][0]["row"] == 3, detail["errors"]
    assert "必填项为空" in detail["errors"][0]["message"]


async def test_batch_filter_by_kind(client: AsyncClient, admin_headers) -> None:
    await _commit_devices(client, admin_headers, [["筛选测试", "牌", "BATCH-FLT-1", "台", 10]])

    only_device = await client.get(
        "/api/v1/imports/batches", params={"kind": "device"}, headers=admin_headers
    )
    assert only_device.status_code == 200
    assert all(b["kind"] == "device" for b in only_device.json()["items"])

    only_inquiry = await client.get(
        "/api/v1/imports/batches", params={"kind": "inquiry"}, headers=admin_headers
    )
    assert only_inquiry.status_code == 200
    assert only_inquiry.json()["items"] == []

    bad = await client.get(
        "/api/v1/imports/batches", params={"kind": "nope"}, headers=admin_headers
    )
    assert bad.status_code == 422


async def test_batch_detail_404_for_unknown_id(client: AsyncClient, admin_headers) -> None:
    r = await client.get(
        "/api/v1/imports/batches/00000000-0000-0000-0000-000000000000", headers=admin_headers
    )
    assert r.status_code == 404
