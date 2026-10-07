"""Excel 批量导入测试：场景一（设备库）+ 场景二（甲方询价清单）."""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest
from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import imports as imports_module
from app.models.project import Project, ProjectStatus, ProjectType
from app.services.excel_io import detect_columns, parse_device_sheet, parse_inquiry_sheet

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def build_xlsx(rows: list[list], sheet_name: str = "设备清单") -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = sheet_name
    for row in rows:
        ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# 解析层（纯函数）
# ---------------------------------------------------------------------------

def test_parse_maps_chinese_headers() -> None:
    data = build_xlsx([
        ["设备名称", "品牌", "规格型号", "所属分类", "计量单位", "参考单价"],
        ["网络高清枪型摄像机", "海康威视", "DS-2CD2T46WD-I5", "视频监控", "台", 680],
    ])
    rows, total = parse_device_sheet(data)
    assert total == 1
    assert rows[0]["name"] == "网络高清枪型摄像机"
    assert rows[0]["model"] == "DS-2CD2T46WD-I5"
    assert rows[0]["reference_price"] == 680


def test_parse_accepts_synonym_headers() -> None:
    """客户表头千奇百怪：型号 / Model / 产品名称 都要认。"""
    data = build_xlsx([
        ["产品名称", "厂牌", "型号", "类别", "数量", "单价(元)"],
        ["24 口交换机", "华为", "S1730S-L24T-A", "网络", 2, 1380],
    ])
    rows, _ = parse_device_sheet(data)
    assert rows[0]["name"] == "24 口交换机"
    assert rows[0]["brand"] == "华为"
    assert rows[0]["model"] == "S1730S-L24T-A"
    assert rows[0]["quantity"] == 2
    assert rows[0]["reference_price"] == 1380


def test_parse_skips_title_row_above_header() -> None:
    """很多甲方清单第 1 行是标题，真实表头在第 2 行。"""
    data = build_xlsx([
        ["宁波XX大厦智能化工程 设备询价清单", None, None, None],
        ["设备名称", "规格型号", "品牌", "数量"],
        ["网络摄像机", "DS-2CD2T46WD-I5", "海康威视", 86],
    ])
    rows, _ = parse_device_sheet(data)
    assert len(rows) == 1
    assert rows[0]["name"] == "网络摄像机"
    assert rows[0]["quantity"] == 86


def test_parse_skips_fully_empty_rows() -> None:
    data = build_xlsx([
        ["设备名称", "规格型号"],
        ["A", "M1"],
        [None, None],
        ["B", "M2"],
        ["", "   "],
    ])
    rows, _ = parse_device_sheet(data)
    assert len(rows) == 2


def test_parse_raises_on_unrecognisable_header() -> None:
    data = build_xlsx([["列一", "列二"], ["x", "y"]])
    with pytest.raises(ValueError, match="表头无法识别"):
        parse_device_sheet(data)


def test_detect_columns_reports_mapping_and_unmapped() -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "QQZZXX", "参考单价"],
        ["摄像机", "M-1", "忽略我", 100],
    ])
    result = detect_columns(data)
    fields = {c["system_field"] for c in result["columns"]}
    assert "name" in fields
    assert "model" in fields
    assert "reference_price" in fields
    assert "QQZZXX" in result["unmapped"]


def test_notes_column_is_matched_by_substring() -> None:
    """「内部备注列」含「备注」→ 应映射到 notes，而不是被忽略。"""
    data = build_xlsx([
        ["设备名称", "规格型号", "内部备注列"],
        ["摄像机", "M-1", "要注意"],
    ])
    fields = {c["system_field"] for c in detect_columns(data)["columns"]}
    assert "notes" in fields


def test_parse_inquiry_minimal_name_and_qty() -> None:
    data = build_xlsx([
        ["设备名称", "数量"],
        ["网络高清枪型摄像机 400万", 86],
        ["24 口千兆交换机", 12],
    ], sheet_name="询价清单")
    rows, total = parse_inquiry_sheet(data)
    assert total == 2
    assert rows[0]["name"] == "网络高清枪型摄像机 400万"
    assert rows[0]["quantity"] == 86


# ---------------------------------------------------------------------------
# 场景一 endpoint：预览
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preview_device_import_ok_rows(
    client: AsyncClient, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "品牌", "规格型号", "所属分类", "计量单位", "参考单价"],
        ["网络高清球机 23 倍", "海康威视", "DS-2DE4A425IW-DE", "视频监控", "台", 4850],
        ["六类非屏蔽双绞线", "一舟", "Y-Z6U305", "综合布线", "箱", 780],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip", "max_rows": "50"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stat"]["ok"] == 2
    assert body["stat"]["error"] == 0
    assert len(body["preview"]) == 2
    assert body["preview"][0]["category"] == "video"


@pytest.mark.asyncio
async def test_preview_flags_missing_required_fields(
    client: AsyncClient, admin_headers
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "所属分类"],
        ["缺型号的设备", None, "视频监控"],
        [None, "M-NO-NAME", "视频监控"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    body = r.json()
    assert body["stat"]["error"] == 2
    assert any("规格型号必填" in i for i in body["preview"][0]["issues"])
    assert any("设备名称必填" in i for i in body["preview"][1]["issues"])


@pytest.mark.asyncio
async def test_preview_flags_unrecognised_category_as_warning(
    client: AsyncClient, admin_headers
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "所属分类"],
        ["某设备", "M-1", "未知分类XYZ"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    row = r.json()["preview"][0]
    assert row["level"] == "warn"
    assert any("无法识别" in i for i in row["issues"])


@pytest.mark.asyncio
async def test_preview_flags_non_numeric_price_as_error(
    client: AsyncClient, admin_headers
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "所属分类", "参考单价"],
        ["某设备", "M-1", "视频监控", "面议"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    row = r.json()["preview"][0]
    assert row["level"] == "error"
    assert any("不是数字" in i for i in row["issues"])


@pytest.mark.asyncio
async def test_preview_detects_duplicate_model(
    client: AsyncClient, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "所属分类"],
        ["重复的摄像机", "DS-2CD2T46WD-I5", "视频监控"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    row = r.json()["preview"][0]
    assert row["existing_code"] == "V1001"
    assert any("已存在" in i for i in row["issues"])
    assert r.json()["stat"]["duplicate"] == 1


@pytest.mark.asyncio
async def test_preview_flags_discontinued_model_in_library(
    client: AsyncClient, admin_headers, devices
) -> None:
    """库中该型号已是停产 → 预览要标「停产型号」。"""
    data = build_xlsx([
        ["设备名称", "规格型号", "所属分类"],
        ["大华枪机", "DH-IPC-HFW3441M", "视频监控"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    body = r.json()
    row = body["preview"][0]
    assert row["status_in_db"] == "eol"
    assert row["level"] == "warn"
    assert any("停产型号" in i for i in row["issues"])
    assert body["stat"]["discontinued"] == 1


# ---------------------------------------------------------------------------
# 场景一 endpoint：提交
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_commit_device_import_creates_rows(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "品牌", "规格型号", "所属分类", "计量单位", "参考单价"],
        ["网络高清球机 23 倍", "海康威视", "DS-2DE4A425IW-DE", "视频监控", "台", 4850],
        ["六类非屏蔽双绞线", "一舟", "Y-Z6U305", "综合布线", "箱", 780],
    ])
    r = await client.post(
        "/api/v1/imports/devices/commit",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip", "skip_errors": "true"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 2

    listing = await client.get(
        "/api/v1/devices", headers=admin_headers, params={"keyword": "DS-2DE4A425IW-DE"}
    )
    assert listing.json()["total"] == 1
    created = listing.json()["items"][0]
    assert created["code"].startswith("V")
    assert Decimal(str(created["cost_price"])) == Decimal("3637.50")  # 4850 × 75%


@pytest.mark.asyncio
async def test_commit_device_import_skip_policy(
    client: AsyncClient, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "所属分类"],
        ["已存在的摄像机", "DS-2CD2T46WD-I5", "视频监控"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/commit",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    body = r.json()
    assert body["skipped"] == 1
    assert body["created"] == 0


@pytest.mark.asyncio
async def test_commit_import_assigns_unique_codes_within_same_category(
    client: AsyncClient, admin_headers, devices
) -> None:
    """回归：同分类多行必须拿到递增的唯一编号。

    `_next_code()` 通过 SELECT max(code) 生成编号；若新增行未在下一次查询前
    flush，多行会算出同一个编号并触发唯一约束冲突。
    """
    rows = [["设备名称", "品牌", "规格型号", "所属分类", "参考单价"]]
    for i in range(5):
        rows.append([f"网络高清枪型摄像机 {i}", "海康威视", f"DS-TEST-{i}", "视频监控", 700])
    data = build_xlsx(rows)

    r = await client.post(
        "/api/v1/imports/devices/commit",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    assert r.status_code == 201, r.text
    assert r.json()["created"] == 5

    listing = await client.get(
        "/api/v1/devices", headers=admin_headers,
        params={"category": "video", "page_size": 100},
    )
    codes = [d["code"] for d in listing.json()["items"]]
    assert len(codes) == len(set(codes)), f"编号重复: {codes}"
    # V1001 / V1008 / V1009 已存在 → 新增从 V1010 起
    assert "V1010" in codes and "V1014" in codes


@pytest.mark.asyncio
async def test_commit_device_import_overwrite_policy(
    client: AsyncClient, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "品牌", "规格型号", "所属分类", "参考单价"],
        ["改名后的摄像机", "海康威视", "DS-2CD2T46WD-I5", "视频监控", 999],
    ])
    r = await client.post(
        "/api/v1/imports/devices/commit",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "overwrite"},
    )
    body = r.json()
    assert body["updated"] == 1
    assert body["created"] == 0

    detail = await client.get(f"/api/v1/devices/{devices[0].id}", headers=admin_headers)
    assert detail.json()["name"] == "改名后的摄像机"
    assert Decimal(str(detail.json()["reference_price"])) == Decimal("999")


@pytest.mark.asyncio
async def test_viewer_cannot_import_devices(
    client: AsyncClient, viewer_headers
) -> None:
    data = build_xlsx([["设备名称", "规格型号"], ["x", "y"]])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=viewer_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 场景二 endpoint：甲方询价清单智能匹配
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_preview_inquiry_matches_and_flags(
    client: AsyncClient, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号", "品牌", "数量"],
        ["网络高清枪型摄像机 400万", "DS-2CD2T46WD-I5", "海康威视", 86],
        ["大华枪机", "DH-IPC-HFW3441M", "大华", 10],
        ["电梯五方对讲主机", "ZZZ-0000", "无此品牌", 2],
    ], sheet_name="询价清单")

    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("i.xlsx", data, XLSX_MIME)},
        data={"threshold": "75", "limit": "50"},
    )
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["stat"]["exact"] >= 1
    assert body["stat"]["none"] >= 1

    by_row = {r["row"]: r for r in body["results"]}
    assert by_row[2]["match_type"] == "exact"
    assert by_row[2]["best"]["code"] == "V1001"
    # 第 3 行匹到停产设备
    assert by_row[3]["best"]["is_discontinued"] is True
    # 第 4 行完全不匹配
    assert by_row[4]["match_type"] == "none"

    # 前端预览表要逐行显示「提示」一列；这两个字段以前没返回，
    # 前端 undefined.length 直接把整个匹配确认步骤渲染崩掉。
    for row in body["results"]:
        assert isinstance(row["issues"], list)
        assert row["level"] in ("ok", "warn", "error")
    assert by_row[4]["level"] == "error"
    assert by_row[4]["issues"], "无匹配行必须给出原因"
    assert by_row[2]["level"] == "ok"
    # 数量在顶层（query 只是查询条件回显）—— 前端曾经读 query.quantity，
    # 结果所有导入行都被写成 ×1。
    assert by_row[2]["quantity"] == 86
    assert by_row[3]["quantity"] == 10


@pytest.mark.asyncio
async def test_inquiry_preview_reports_total_rows(
    client: AsyncClient, admin_headers, devices
) -> None:
    """小文件：total_rows == previewed_rows 且 truncated=False。

    这三个字段是判断"有没有丢行"的唯一依据 —— 以前前端只能看 sheet_rows
    （还被错标成"表头行数"），于是 200 行只匹配 50 行也没人发现。
    """
    data = build_xlsx([
        ["设备名称", "规格型号", "数量"],
        ["网络高清枪型摄像机 400万", "DS-2CD2T46WD-I5", 5],
        ["24 口千兆交换机", "S1730S-L24T-A", 2],
    ], sheet_name="询价清单")

    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("i.xlsx", data, XLSX_MIME)},
        data={"threshold": "75"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_rows"] == 2
    assert body["previewed_rows"] == 2
    assert body["truncated"] is False
    assert body["sheet_rows"] == 2


@pytest.mark.asyncio
async def test_inquiry_preview_flags_truncation(
    client: AsyncClient, admin_headers, devices
) -> None:
    """超过 limit 时必须明说 truncated —— 这是"静默丢一半"的回归断言。

    复现原来的事故路径：5 行清单 + limit=2，旧实现返回 2 行且**不提**还有 3 行，
    前端提交时就只写 2 行，界面还提示成功。
    """
    rows = [["设备名称", "规格型号", "数量"]]
    for i in range(5):
        rows.append([f"网络高清枪型摄像机 400万 {i}", "DS-2CD2T46WD-I5", i + 1])
    data = build_xlsx(rows, sheet_name="询价清单")

    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("i.xlsx", data, XLSX_MIME)},
        data={"threshold": "75", "limit": "2"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["total_rows"] == 5, "必须回传文件真实行数，否则前端无法发现丢行"
    assert body["previewed_rows"] == 2
    assert body["truncated"] is True
    assert len(body["results"]) == 2


@pytest.mark.asyncio
async def test_inquiry_preview_rejects_too_many_rows(
    client: AsyncClient, admin_headers, devices, monkeypatch
) -> None:
    """超过 MAX_ROWS 直接 422，而不是"只处理前 N 行"。"""
    monkeypatch.setattr(imports_module, "MAX_ROWS", 3, raising=False)
    rows = [["设备名称", "规格型号", "数量"]]
    for i in range(4):
        rows.append([f"设备{i}", "DS-2CD2T46WD-I5", 1])
    data = build_xlsx(rows, sheet_name="询价清单")

    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("i.xlsx", data, XLSX_MIME)},
        data={"threshold": "75"},
    )
    assert r.status_code == 422, r.text
    assert "上限" in r.json()["detail"]


@pytest.mark.asyncio
async def test_inquiry_preview_rejects_oversized_upload(
    client: AsyncClient, admin_headers, devices, monkeypatch
) -> None:
    """询价清单以前没有体积校验（设备库那条链路有），补上并断言 413。"""
    monkeypatch.setattr(imports_module, "MAX_UPLOAD_BYTES", 16, raising=False)
    data = build_xlsx(
        [["设备名称", "规格型号", "数量"], ["摄像机", "DS-2CD2T46WD-I5", 1]],
        sheet_name="询价清单",
    )
    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("i.xlsx", data, XLSX_MIME)},
        data={"threshold": "75"},
    )
    assert r.status_code == 413, r.text


@pytest.mark.asyncio
async def test_commit_inquiry_rejects_empty_payload(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """空数组不再"成功写入 0 行"，直接 422（前端可据此提示）。"""
    p = Project(code="PRJ-IMP-EMPTY", name="空载荷项目", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    q = (await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": str(p.id), "code": "QT-IMP-EMPTY", "title": "空载荷"},
    )).json()

    r = await client.post(
        "/api/v1/imports/inquiry/commit",
        headers=admin_headers,
        data={"quote_id": q["id"], "payload": json.dumps([])},
    )
    assert r.status_code == 422, r.text
    assert "没有任何要写入的行" in r.json()["detail"]


@pytest.mark.asyncio
async def test_inquiry_preview_tolerates_dirty_quantity_cells(
    client: AsyncClient, admin_headers, devices
) -> None:
    """数量写成「86 台」「1,200」这类脏数据不能让预览 500。

    造价员手填的清单里带单位、带千分位很常见。float("86 台") 抛 ValueError，
    冒到全局 handler 就是 500，用户只看到「服务器错误」，整批匹配都做不成。
    """
    data = build_xlsx([
        ["设备名称", "规格型号", "数量"],
        ["网络高清枪型摄像机 400万", "DS-2CD2T46WD-I5", "86 台"],
        ["大华枪机", "DH-IPC-HFW3441M", "1,200"],
        ["电梯五方对讲主机", "ZZZ-0000", "无"],
    ], sheet_name="询价清单")

    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("dirty.xlsx", data, XLSX_MIME)},
        data={"threshold": "75", "limit": "50"},
    )
    assert r.status_code == 200, r.text
    by_row = {x["row"]: x for x in r.json()["results"]}
    assert by_row[2]["quantity"] == 86.0
    assert by_row[3]["quantity"] == 1200.0
    assert by_row[4]["quantity"] == 0.0  # 抠不出数字 → 退回默认值，而不是崩


@pytest.mark.asyncio
async def test_preview_inquiry_reports_discontinued_count(
    client: AsyncClient, admin_headers, devices
) -> None:
    data = build_xlsx([
        ["设备名称", "规格型号"],
        ["大华枪机", "DH-IPC-HFW3441M"],
    ], sheet_name="询价清单")
    r = await client.post(
        "/api/v1/imports/inquiry/preview",
        headers=admin_headers,
        files={"file": ("i.xlsx", data, XLSX_MIME)},
        data={"threshold": "50"},
    )
    assert r.json()["stat"]["discontinued"] >= 1


@pytest.mark.asyncio
async def test_commit_inquiry_adds_items_to_quote(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """确认后的匹配行写入报价单；未匹配行以 0 价占位。"""
    p = Project(code="PRJ-IMP-1", name="导入项目", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT)
    db.add(p)
    await db.commit()
    await db.refresh(p)

    q = (await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": str(p.id), "code": "QT-IMP-1", "title": "导入报价"},
    )).json()

    payload = [
        {"row": 2, "device_id": str(devices[0].id), "name": devices[0].name, "quantity": 86},
        {"row": 3, "name": "待询价设备", "quantity": 5},
    ]
    r = await client.post(
        "/api/v1/imports/inquiry/commit",
        headers=admin_headers,
        data={"quote_id": q["id"], "payload": json.dumps(payload)},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["added"] == 1
    assert body["pending_inquiry"] == 1
    assert body["total_with_tax"] > 0

    items = (await client.get(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers)).json()
    assert len(items) == 2
    pending = [i for i in items if i["name"] == "待询价设备"][0]
    assert Decimal(str(pending["unit_price"])) == Decimal("0")
    assert pending["source"] == "manual"


@pytest.mark.asyncio
async def test_commit_inquiry_auto_replaces_eol_device(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """清单选中了停产型号且该型号配了替代 → 自动换替代并计数。"""
    p = Project(code="PRJ-IMP-2", name="导入项目2", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT)
    db.add(p)
    await db.commit()
    await db.refresh(p)

    q = (await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": str(p.id), "code": "QT-IMP-2", "title": "导入报价2"},
    )).json()

    payload = [
        {"row": 2, "device_id": str(devices[1].id), "name": devices[1].name, "quantity": 10},
    ]
    r = await client.post(
        "/api/v1/imports/inquiry/commit",
        headers=admin_headers,
        data={"quote_id": q["id"], "payload": json.dumps(payload)},
    )
    assert r.status_code == 201, r.text
    assert r.json()["auto_replaced"] == 1

    items = (await client.get(f"/api/v1/quotes/{q['id']}/items", headers=admin_headers)).json()
    assert items[0]["model"] == "DH-IPC-HFW3449M"  # 替代型号
    assert items[0]["is_replaced"] is True


@pytest.mark.asyncio
async def test_commit_inquiry_unknown_quote_404(
    client: AsyncClient, admin_headers
) -> None:
    import uuid

    r = await client.post(
        "/api/v1/imports/inquiry/commit",
        headers=admin_headers,
        data={"quote_id": str(uuid.uuid4()), "payload": "[]"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_commit_inquiry_bad_json_422(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = Project(code="PRJ-IMP-3", name="导入项目3", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    q = (await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": str(p.id), "code": "QT-IMP-3", "title": "t"},
    )).json()

    r = await client.post(
        "/api/v1/imports/inquiry/commit",
        headers=admin_headers,
        data={"quote_id": q["id"], "payload": "{not json"},
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_templates_endpoints(client: AsyncClient, admin_headers) -> None:
    d = await client.get("/api/v1/imports/device-template", headers=admin_headers)
    i = await client.get("/api/v1/imports/inquiry-template", headers=admin_headers)
    assert d.status_code == 200 and "设备名称" in d.json()["columns"]
    assert i.status_code == 200 and "数量" in i.json()["columns"]
    # columns 是表头字符串数组（不是 {field, header} 对象）；
    # 前端曾经按 c.header 读，下载下来的模板表头全是 undefined。
    assert isinstance(d.json()["columns"], list)
    assert all(isinstance(c, str) for c in d.json()["columns"])


@pytest.mark.asyncio
async def test_device_preview_returns_unmapped_columns(
    client: AsyncClient, admin_headers
) -> None:
    """未识别的列要回给前端做提示；以前只算不返回，前端 .length 直接崩。"""
    data = build_xlsx([
        ["设备名称", "规格型号", "无关列A", "备注说明"],
        ["网络高清枪型摄像机 400万", "DS-2CD2T46WD-I5", "x", "y"],
    ])
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", data, XLSX_MIME)},
        data={"duplicate_policy": "skip", "max_rows": "100"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert "unmapped" in body, "缺少 unmapped，前端预览会 undefined.length 崩掉"
    assert "无关列A" in body["unmapped"]
    # 有表头就算认出来了，不该出现在 unmapped 里
    assert "设备名称" not in body["unmapped"]
