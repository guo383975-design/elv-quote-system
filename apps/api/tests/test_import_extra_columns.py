"""导入器此前**忽略的三列**（2026-10-09 修）。

问题现场（设备库导入，场景一）：
  · 「设备状态」列被**硬编码 `status=ON_SALE`** 覆盖 ✗ —— 源表写"停产"也照当在售 ✗
    （列名早就被 `excel_io._SYNONYMS` 认出来了 ✓，只是没人用 ✗）；
  · 「供应商」列完全不落 ✗（`devices.supplier_id` 一直空着 ✗）；
  · 「替代型号」列完全不落 ✗（`devices.replacement_id` 一直空着 ✗）。

修复口径（用户 2026-10-09 明确）：
  ① 设备状态 → 查词表映射 ✓；**认不出的值按"在售"并在报告里报出来** ✓（不许静默 ✗）；
  ② 供应商 → **只按名称匹配已有供应商** ✓，没有就报告 ✓（**不自动新建** ✗ 免得脏数据 ✗）；
  ③ 替代型号 → 设备表**有** `replacement_id` 字段 ✓ → 按型号匹配后落库 ✓；
     匹配不到就报告并跳过 ✓（**不塞进描述** ✗）。

⚠ 本文件只动**导入逻辑** ✓：已入库的存量设备（那 454 台大华）一台都不碰 ✓
（用例里用的是同型号覆盖才生效的 overwrite 分支，默认 skip 分支不写任何已有行 ✓）。
"""

from __future__ import annotations

import io

from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceStatus
from app.models.supplier import Supplier

# 官方模板的 15 列（**必须**按它来：这 15 列就是用户实际拿到的表 ✓）
TEMPLATE_HEADER = [
    "设备名称", "品牌", "规格型号", "分类路径", "计量单位",
    "参考单价", "进价", "安装单价", "设备描述", "供应商", "起订量",
    "质保年限", "是否甲供", "设备状态", "替代型号",
]


def _xlsx(rows: list[list[object]]) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "设备清单"
    ws.append(TEMPLATE_HEADER)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _form(**over: object) -> dict[str, str]:
    payload = {"duplicate_policy": "skip", "skip_errors": "true", "link_accessories": "false"}
    payload.update({k: str(v) for k, v in over.items()})
    return payload


async def _preview(client: AsyncClient, headers, data: bytes, **over) -> dict:
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=headers,
        files={"file": ("t.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data={**_form(**over), "max_rows": "100"},
    )
    assert r.status_code == 200, r.text
    return r.json()


async def _commit(client: AsyncClient, headers, data: bytes, **over) -> dict:
    r = await client.post(
        "/api/v1/imports/devices/commit",
        headers=headers,
        files={"file": ("t.xlsx", data, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
        data=_form(**over),
    )
    assert r.status_code in (200, 201), r.text
    return r.json()


async def _device(client: AsyncClient, headers, model: str) -> dict:
    r = await client.get("/api/v1/devices", headers=headers, params={"keyword": model})
    assert r.status_code == 200, r.text
    items = [d for d in r.json()["items"] if d["model"] == model]
    assert len(items) == 1, (model, r.json())
    return items[0]


async def test_status_supplier_replacement_land_on_commit(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """三条主线：状态按词表落 ✓ / 供应商按名称挂 ✓ / 替代型号按型号挂 ✓（含前向引用未命中）。"""
    sup = Supplier(code="SUP-ZZ-1", name="宁波测试供应商", is_active=True)
    stopped = Supplier(code="SUP-ZZ-2", name="已停用供应商", is_active=False)
    db.add_all([sup, stopped])
    await db.commit()
    await db.refresh(sup)

    data = _xlsx([
        # A：在售 + 命中供应商（无替代型号）
        ["留痕摄像机A", "海康", "ZZ-IMP-A", "视频监控/摄像机", "台",
         680, 510, 112, "", "宁波测试供应商", 1, 2, "否", "在售", ""],
        # B：停产 + 替代型号指向**同一文件里前面那一行**（A 已落库 → 能匹配上 ✓）
        ["留痕录像机B", "海康", "ZZ-IMP-B", "视频监控/录像机", "台",
         1200, 900, 150, "", "", 1, 2, "否", "停产", "ZZ-IMP-A"],
        # C：退市（词表里另一个枚举值 ✓）
        ["留痕交换机C", "华为", "ZZ-IMP-C", "计算机网络/交换机", "台",
         800, 600, 0, "", "", 1, 2, "否", "退市", ""],
        # D：状态认不出（→在售 + 报告 ✓）、供应商不存在（→报告、不新建 ✓）、
        #    替代型号不存在（→报告、跳过、**不写进描述** ✓）
        ["留痕不明D", "其他", "ZZ-IMP-D", "视频监控/摄像机", "台",
         300, 200, 50, "原样描述", "不存在的供应商", 1, 2, "否", "库存充足", "NO-SUCH-MODEL"],
        # E：供应商同名但**已停用** → 不挂接，报告里单独一类 ✓
        ["留痕停用供应商E", "其他", "ZZ-IMP-E", "视频监控/摄像机", "台",
         300, 200, 50, "", "已停用供应商", 1, 2, "否", "在售", ""],
    ])

    preview = await _preview(client, admin_headers, data)
    report = preview["column_report"]
    by_model = {row["model"]: row for row in preview["preview"]}

    # ① 状态：识别成功的进 mapped ✓、认不出的进 unrecognized ✓、最终落库分布进 applied ✓
    assert report["status"]["mapped"] == {"在售": 2, "停产": 1, "退市": 1}, report
    assert report["status"]["unrecognized"] == {"库存充足": 1}, report
    assert report["status"]["applied"] == {"在售": 3, "已停产": 1, "退市": 1}, report
    assert by_model["ZZ-IMP-D"]["status"] == "on_sale"
    assert by_model["ZZ-IMP-D"]["status_recognized"] is False
    assert by_model["ZZ-IMP-B"]["status"] == "eol"
    assert any("无法识别" in i for i in by_model["ZZ-IMP-D"]["issues"]), by_model["ZZ-IMP-D"]
    # ② 供应商：命中的 1 条 ✓、不存在的 1 条 ✓、已停用的 1 条 ✓
    assert report["supplier"]["matched"] == {"宁波测试供应商": 1}, report
    assert report["supplier"]["missing"] == {"不存在的供应商": 1}, report
    assert report["supplier"]["inactive"] == {"已停用供应商": 1}, report
    assert by_model["ZZ-IMP-A"]["supplier_name"] == "宁波测试供应商"
    assert by_model["ZZ-IMP-D"]["supplier_id"] is None
    assert any("不会自动新建" in i for i in by_model["ZZ-IMP-D"]["issues"]), by_model["ZZ-IMP-D"]
    assert any("已停用" in i for i in by_model["ZZ-IMP-E"]["issues"]), by_model["ZZ-IMP-E"]
    # ③ 替代型号：B 指向 A（同一文件内、且 A 在 B 前面 → 预览阶段查的是**库**）
    #    预览时 A 还没落库 → 属于"前向引用"，如实报告为未命中 ✓（不静默 ✗）
    assert report["replacement"]["missing"] == {"ZZ-IMP-A": 1, "NO-SUCH-MODEL": 1}, report
    assert by_model["ZZ-IMP-B"]["replacement_id"] is None
    assert any("不在设备库中" in i for i in by_model["ZZ-IMP-B"]["issues"]), by_model["ZZ-IMP-B"]

    suppliers_before = await db.scalar(select(func.count()).select_from(Supplier))

    result = await _commit(client, admin_headers, data)
    assert result["created"] == 5, result
    creport = result["column_report"]
    # 落库阶段：A 已经在库里了 → B 的替代型号**能**匹配上 ✓
    # （这正是"报告要说实话"的意义：预览与落库的差别必须看得见 ✓）
    assert creport["replacement"]["matched"] == 1, creport
    assert creport["replacement"]["missing"] == {"NO-SUCH-MODEL": 1}, creport

    a = await _device(client, admin_headers, "ZZ-IMP-A")
    b = await _device(client, admin_headers, "ZZ-IMP-B")
    c = await _device(client, admin_headers, "ZZ-IMP-C")
    d = await _device(client, admin_headers, "ZZ-IMP-D")
    e = await _device(client, admin_headers, "ZZ-IMP-E")

    # ① 状态真的落了（改码前全是 on_sale ✗）
    assert a["status"] == DeviceStatus.ON_SALE, a
    assert b["status"] == DeviceStatus.EOL, b
    assert c["status"] == DeviceStatus.DISCONTINUED, c
    # ② 供应商真的挂了 ✓（改码前恒为 None ✗）
    assert a["supplier_id"] == str(sup.id), a
    assert d["supplier_id"] is None, d
    assert e["supplier_id"] is None, e
    # ③ 替代型号真的挂了 ✓（改码前恒为 None ✗），且指向 A 那台设备 ✓
    assert b["replacement_id"] == a["id"], (b, a)
    assert d["replacement_id"] is None, d

    # ③ 匹配不到的替代型号**不许塞进描述** ✓（描述只能原样是源表那一列 ✓）
    assert d["description"] == "原样描述", d
    assert "NO-SUCH-MODEL" not in (d["description"] or ""), d

    # ② **绝不新建供应商** ✓（名称近似/不存在的都只报告 ✓）
    suppliers_after = await db.scalar(select(func.count()).select_from(Supplier))
    assert suppliers_after == suppliers_before, "导入不该新建供应商"

    # 报告写进了导入批次 ✓（事后能查"这一批的状态是怎么来的" ✓）
    batch = (
        await client.get(
            f"/api/v1/imports/batches", headers=admin_headers, params={"kind": "device"}
        )
    ).json()["items"][0]
    assert batch["batch_no"] == result["batch_no"], batch
    assert batch["summary"]["column_report"]["status"]["unrecognized"] == {"库存充足": 1}, batch


async def test_blank_status_column_defaults_to_on_sale_without_noise(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """源表**没填**状态 → 按在售 ✓，但**不算"认不出"** ✓（没说 ≠ 说错了 ✓）。

    这一区分很重要：把"没填"也算成"认不出"，报告里就会出现几百条假警报 ✗，
    用户从此不看报告 ✗ —— 报告一旦不可信，就等于没有 ✗。
    """
    data = _xlsx([
        ["无状态列值", "海康", "ZZ-IMP-BLANK", "视频监控/摄像机", "台",
         100, 80, 0, "", "", 1, 2, "否", "", ""],
    ])
    preview = await _preview(client, admin_headers, data)
    report = preview["column_report"]
    assert report["status"]["blank"] == 1, report
    assert report["status"]["unrecognized"] == {}, report
    assert report["status"]["mapped"] == {}, report

    await _commit(client, admin_headers, data)
    dev = await _device(client, admin_headers, "ZZ-IMP-BLANK")
    assert dev["status"] == DeviceStatus.ON_SALE, dev


async def test_overwrite_applies_status_and_supplier_to_existing_device(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """`duplicate_policy=overwrite` 时三列**也要生效** ✓
    （"改了状态/供应商重导一次"必须真的变 ✓，否则用户以为改好了其实没变 ✗）。

    ⚠ 这是唯一会写**已有**设备的路径 ✓，且只有用户显式选「覆盖」才会走到 ✓
    （默认 skip ✓）；默认路径对存量设备一行都不碰 ✓。
    """
    sup = Supplier(code="SUP-ZZ-3", name="覆盖用供应商", is_active=True)
    db.add(sup)
    await db.commit()
    await db.refresh(sup)

    first = _xlsx([
        ["覆盖前", "海康", "ZZ-IMP-OVR", "视频监控/摄像机", "台",
         100, 80, 0, "", "", 1, 2, "否", "在售", ""],
    ])
    await _commit(client, admin_headers, first)
    dev = await _device(client, admin_headers, "ZZ-IMP-OVR")
    assert dev["status"] == DeviceStatus.ON_SALE and dev["supplier_id"] is None, dev

    second = _xlsx([
        ["覆盖后", "海康", "ZZ-IMP-OVR", "视频监控/摄像机", "台",
         100, 80, 0, "", "覆盖用供应商", 1, 2, "否", "已停产", ""],
    ])
    result = await _commit(client, admin_headers, second, duplicate_policy="overwrite")
    assert result["updated"] == 1 and result["created"] == 0, result

    dev = await _device(client, admin_headers, "ZZ-IMP-OVR")
    assert dev["status"] == DeviceStatus.EOL, dev
    assert dev["supplier_id"] == str(sup.id), dev


async def test_skipped_duplicate_rows_are_not_counted_in_column_report(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """被跳过的重复行**不进**映射报告 ✓ —— 报告说的是"这一批**落库**的行怎么处理的" ✓。

    把没写进去的行也统计成"按在售导入了"，报告就是在撒谎 ✗。
    """
    rows = [
        ["第一台", "海康", "ZZ-IMP-SKIP", "视频监控/摄像机", "台",
         100, 80, 0, "", "", 1, 2, "否", "在售", ""],
    ]
    await _commit(client, admin_headers, _xlsx(rows))

    # 同一个型号再导一次（默认 skip）→ 0 新增 1 跳过 ✓，报告里不该出现它 ✓
    result = await _commit(client, admin_headers, _xlsx([
        ["第二台", "海康", "ZZ-IMP-SKIP", "视频监控/摄像机", "台",
         100, 80, 0, "", "", 1, 2, "否", "库存充足", ""],
    ]))
    assert result["skipped"] == 1 and result["created"] == 0, result
    report = result["column_report"]
    assert report["status"]["unrecognized"] == {}, report
    assert report["status"]["applied"] == {}, report


async def test_inactive_replacement_target_is_reported_and_skipped(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """替代型号指向一台**已停用**的设备 → 不挂接 ✓ 并报告 ✓（挂上去等于让配单换到不能买的型号 ✗）。"""
    db.add(Device(code="ZZ-OLD-1", name="已停用老型号", model="ZZ-IMP-OLD",
                  category="video", reference_price=100, cost_price=80,
                  install_unit_price=0, is_active=False))
    await db.commit()

    result = await _commit(client, admin_headers, _xlsx([
        ["新型号", "海康", "ZZ-IMP-NEW", "视频监控/摄像机", "台",
         200, 160, 0, "", "", 1, 2, "否", "在售", "ZZ-IMP-OLD"],
    ]))
    assert result["column_report"]["replacement"]["missing"] == {"ZZ-IMP-OLD": 1}, result

    dev = await _device(client, admin_headers, "ZZ-IMP-NEW")
    assert dev["replacement_id"] is None, dev
