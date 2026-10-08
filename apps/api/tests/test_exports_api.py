"""导出接口测试：权限边界、客户版 vs 内部版、留痕。

这里是「导出会不会把成本漏给甲方 / 泄露给销售」的唯一防线，
所以两个方向都要断言：该拒的必须 403，该给的必须真的给到。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from httpx import AsyncClient
from openpyxl import load_workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus, ProjectType

pytestmark = pytest.mark.asyncio

XLSX_MEDIA = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# helpers（与 test_quotes_api.py 同构，故意不跨模块 import：测试之间不互相拖累）
# ---------------------------------------------------------------------------

async def _make_project(db: AsyncSession, code: str = "PRJ-EXP-001") -> Project:
    p = Project(
        code=code,
        name="导出测试项目",
        customer_name="导出测试客户",
        project_type=ProjectType.COMMERCIAL,
        status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _make_quote_with_items(client: AsyncClient, headers, project_id, devices, qty: int = 1):
    r = await client.post(
        "/api/v1/quotes",
        headers=headers,
        json={
            "project_id": str(project_id),
            "code": "QT-EXP-001",
            "title": "导出测试报价单",
            "fee_base": "device_aux_install",
            "auxiliary_base": "device",
            "auxiliary_rate": "0.06",
            "design_rate": "0.02",
            "warranty_rate": "0.03",
            "mgmt_profit_rate": "0.08",
            "tax_rate": "0.09",
        },
    )
    assert r.status_code == 201, r.text
    q = r.json()
    for i, d in enumerate(devices[:qty]):
        rr = await client.post(
            f"/api/v1/quotes/{q['id']}/items",
            headers=headers,
            json={"device_id": str(d.id), "name": d.name, "quantity": str(i + 1)},
        )
        assert rr.status_code == 201, rr.text
    return q


def _all_text(data: bytes) -> str:
    """把 xlsx 里所有单元格的文本拼起来，用来断言「有没有出现某个词」。

    不依赖渲染器的行列布局（那是它自己的自由），只依赖表头文字 ——
    表头文字是 export_data.item_headers() 定的契约。
    """
    wb = load_workbook(BytesIO(data))
    parts: list[str] = [ws.title for ws in wb.worksheets]
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if v is not None:
                    parts.append(str(v))
    return "\n".join(parts)


def _pdf_text(data: bytes) -> str:
    """把 PDF 里的文字抽出来（pypdf）。

    reportlab 嵌的中文字体带 ToUnicode 表，`extract_text()` 抽得出中文 ✓
    （实测 "版本" / "V3" 都能抽到），所以导出件上的版本号可以直接断言，
    不用只靠"文件大小 > 2000"这种间接判据。
    """
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


# ---------------------------------------------------------------------------
# 1. 正常导出
# ---------------------------------------------------------------------------

async def test_export_xlsx_admin(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=2)

    r = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith(XLSX_MEDIA)
    # xlsx 是 zip，开头必须是 PK
    assert r.content[:2] == b"PK"
    assert len(r.content) > 3000

    cd = r.headers["content-disposition"]
    assert "attachment" in cd
    assert ".xlsx" in cd
    # 中文文件名走 RFC 5987；同时要有 ASCII 兜底
    assert "filename*=UTF-8''" in cd
    assert 'filename="quote_' in cd


async def test_export_shows_business_version_not_lock_counter(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """导出件上的「版本」必须是**业务版本的时间标记**，不是乐观锁计数器。

    2026-10-08 用户反馈的真因：展示层（含导出件）读的是 `quotes.version` ——
    那是每次写库都 +1 的乐观锁计数器 ✗。生产实证：某单业务上只保存过 2 版，
    界面与 PDF 上却印着 V9 ✓。
    用户随后补充：「版本号以时间为标记」✓。

    两个方向都钉住：
      ① 一次都没保存过 → 导出件里**不出现**「版本」这一项（不印 V0/—）✓；
      ② 保存过 → 印的是**该版的时间标记**（与界面同一时区口径 ✓），
         而不是计数器那个更大的数 ✗。
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    def expected_label(iso: str) -> str:
        """导出件上的版本标记（服务时区 Asia/Shanghai，与界面一致 ✓）。

        ⚠ 开发机（Windows / 精简镜像）没有 tzdata 时，`ZoneInfo` 会抛
        `ZoneInfoNotFoundError` —— 这里与被测代码用**同一套降级口径**（退回本地时区）✓，
        否则这条测试在开发者机器上直接报错 ✗。生产机（有 tzdata）上
        「标记 = Asia/Shanghai 墙钟」由临时库验收脚本另行断言 ✓。
        """
        try:
            tz = ZoneInfo("Asia/Shanghai")
        except Exception:  # noqa: BLE001
            tz = None
        return datetime.fromisoformat(iso).astimezone(tz).strftime("%Y-%m-%d %H:%M")

    p = await _make_project(db, code="PRJ-EXP-VER")
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=1)

    # 再推两次乐观锁计数器（重算 + 改标题）—— 注意：**改标题**不是"保存并重算"，
    # 所以这里不会落版本；而 POST /compute 会落（见下面第 ③ 段前的说明）。
    assert (
        await client.patch(
            f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={"title": "改个标题"}
        )
    ).status_code == 200

    detail = (await client.get(f"/api/v1/quotes/{q['id']}", headers=admin_headers)).json()
    assert detail["version"] > 1, detail
    assert detail["revision_count"] == 0, detail
    assert detail["latest_revision_at"] is None, detail

    x0 = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=admin_headers)
    assert x0.status_code == 200, x0.text
    assert "版本" not in _all_text(x0.content), _all_text(x0.content)[:2000]
    assert f"_v{detail['version']}" not in x0.headers["content-disposition"]

    pdf0 = await client.get(f"/api/v1/quotes/{q['id']}/export.pdf", headers=admin_headers)
    assert pdf0.status_code == 200, pdf0.text
    assert "版本" not in _pdf_text(pdf0.content), _pdf_text(pdf0.content)[:2000]

    # 保存两版 → 导出件上该是第 2 版的时间标记（而乐观锁计数器比它大）
    # ⚠ 两次「保存并重算」中间**必须真的改点东西**：内容没变时快照会被去重跳过 ✓
    #   （用户 2026-10-08 确认的口径：同一次保存且什么都没改 → 不造重复版本 ✓）。
    #   这里改的是**税率** —— 它在快照的表头字段里 ✓；改标题不在快照里 ✗。
    assert (await client.post(
        f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={}
    )).status_code == 200
    assert (await client.patch(
        f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={"tax_rate": "0.12"}
    )).status_code == 200
    assert (await client.post(
        f"/api/v1/quotes/{q['id']}/compute", headers=admin_headers, json={}
    )).status_code == 200

    after = (await client.get(f"/api/v1/quotes/{q['id']}", headers=admin_headers)).json()
    assert after["revision_count"] == 2, after
    label = expected_label(after["latest_revision_at"])
    assert after["version"] > after["latest_revision_seq"], after  # 计数器确实更大 ✓

    x2 = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=admin_headers)
    x2_text = _all_text(x2.content)
    assert "版本" in x2_text and label in x2_text, (label, x2_text[:2000])
    # 文件名后缀也是这个时间标记（紧凑形式）✓
    stamp = label.replace("-", "").replace(":", "").replace(" ", "-")
    assert f"_{stamp}.xlsx" in x2.headers["content-disposition"], x2.headers[
        "content-disposition"
    ]

    pdf2 = await client.get(f"/api/v1/quotes/{q['id']}/export.pdf", headers=admin_headers)
    t2 = _pdf_text(pdf2.content)
    assert "版本" in t2 and label in t2, (label, t2[:2000])
    assert f"V{after['version']}" not in t2, t2[:2000]   # 不许再印计数器那个数 ✗


async def test_export_pdf_admin(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=2)

    r = await client.get(f"/api/v1/quotes/{q['id']}/export.pdf", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("application/pdf")
    assert r.content[:5] == b"%PDF-"
    assert len(r.content) > 2000
    assert "filename*=UTF-8''" in r.headers["content-disposition"]
    # 中文字体必须真嵌进去了，否则甲方打开是一堆方块（而且不会报错）
    assert b"FontFile2" in r.content or b"WenQuanYi" in r.content


# ---------------------------------------------------------------------------
# 2. 成本泄露边界（本文件存在的理由）
# ---------------------------------------------------------------------------

async def test_sales_gets_customer_version_without_cost(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, devices
) -> None:
    """sales 有 quote.export 但没有成本权限 → 拿到的必须是**客户版**。"""
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=2)

    r = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=sales_headers)
    assert r.status_code == 200, r.text
    text = _all_text(r.content)
    assert "进价" not in text, "客户版里出现了进价列"
    assert "毛利" not in text, "客户版里出现了毛利"


async def test_admin_internal_export_has_cost(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """显式 internal=true 且有权 → 内部版必须真的带上成本列。"""
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=2)

    r = await client.get(
        f"/api/v1/quotes/{q['id']}/export.xlsx?internal=true", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    text = _all_text(r.content)
    assert "进价" in text
    assert "毛利" in text


async def test_sales_cannot_export_internal(
    client: AsyncClient, db: AsyncSession, admin_headers, sales_headers, devices
) -> None:
    """**最关键的一条**：sales 显式要内部版时必须 403。

    不能"悄悄给一个没有成本列的客户版" —— 那会让人以为成本是 0 或以为导出坏了。
    """
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=1)

    for ext in ("xlsx", "pdf"):
        r = await client.get(
            f"/api/v1/quotes/{q['id']}/export.{ext}?internal=true", headers=sales_headers
        )
        assert r.status_code == 403, f"{ext}: 期望 403，实际 {r.status_code}"
        assert "成本" in r.json()["detail"]


async def test_viewer_cannot_export(
    client: AsyncClient, db: AsyncSession, admin_headers, viewer_headers, devices
) -> None:
    """viewer 连导出权限都没有。"""
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=1)
    r = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=viewer_headers)
    assert r.status_code == 403


async def test_estimator_can_export_quote(
    client: AsyncClient, db: AsyncSession, admin_headers, est_headers,
    finance_headers, devices
) -> None:
    """造价员**能导出**报价单，但拿到的是**客户版**（2026-10-05 新口径）。

    导出权限（quote.export）与"能否看成本"是两件事：estimator 有导出权限，
    但按用户要求（工程部只能看设备清单）**没有成本权限** ——
    所以显式要 internal=true 会被 403，客户版照常能导。
    """
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=1)

    # 1) 默认（不带 internal）走客户版 → estimator 能下载
    r = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=est_headers)
    assert r.status_code == 200, r.text

    # 2) estimator 没有成本权限 → 显式 internal=true 必须被拒
    r2 = await client.get(
        f"/api/v1/quotes/{q['id']}/export.xlsx?internal=true", headers=est_headers
    )
    assert r2.status_code == 403, r2.text
    assert "成本" in r2.json()["detail"]

    # 3) 财务有成本权限 → 内部版放行，且确实带成本/毛利
    r3 = await client.get(
        f"/api/v1/quotes/{q['id']}/export.xlsx?internal=true", headers=finance_headers
    )
    assert r3.status_code == 200, r3.text
    text = _all_text(r3.content)
    assert "毛利" in text or "成本" in text, "内部版必须带成本/毛利字段"


async def test_viewer_still_cannot_export_after_estimator_opened_up(
    client: AsyncClient, db: AsyncSession, admin_headers, viewer_headers, devices
) -> None:
    """反向断言：放开 estimator 不能顺手把 viewer 也放进 export 名单。

    一旦 quote.EXPORT 矩阵回归性地把 viewer 漏进去，这条会立刻报 200。
    """
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=1)
    r = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=viewer_headers)
    assert r.status_code == 403, (
        f"viewer 不该能导出（实际 {r.status_code}）；"
        "放开 estimator 后请确认 PERMISSION_MATRIX['quote']['EXPORT'] 没把 viewer 也加进去"
    )


# ---------------------------------------------------------------------------
# 3. 留痕与边界
# ---------------------------------------------------------------------------

async def test_export_writes_audit_log(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=1)
    await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=admin_headers)

    r = await client.get(f"/api/v1/quotes/{q['id']}/audit-logs", headers=admin_headers)
    assert r.status_code == 200, r.text
    logs = r.json() if isinstance(r.json(), list) else r.json().get("items", [])
    exports = [row for row in logs if row.get("action") == "export"]
    assert exports, f"没有留下 export 留痕: {[row.get('action') for row in logs]}"
    assert exports[0]["payload"]["format"] == "xlsx"
    assert exports[0]["payload"]["include_cost"] is False


async def test_export_missing_quote_is_404(client: AsyncClient, admin_headers) -> None:
    r = await client.get(
        "/api/v1/quotes/00000000-0000-0000-0000-000000000000/export.xlsx",
        headers=admin_headers,
    )
    assert r.status_code == 404


async def test_export_empty_quote_does_not_crash(
    client: AsyncClient, db: AsyncSession, admin_headers, devices
) -> None:
    """没有任何明细的报价单也要能导出（空单是真实存在的中间状态）。"""
    p = await _make_project(db)
    q = await _make_quote_with_items(client, admin_headers, p.id, devices, qty=0)
    for ext, magic in (("xlsx", b"PK"), ("pdf", b"%PDF-")):
        r = await client.get(f"/api/v1/quotes/{q['id']}/export.{ext}", headers=admin_headers)
        assert r.status_code == 200, f"{ext}: {r.text}"
        assert r.content[: len(magic)] == magic
