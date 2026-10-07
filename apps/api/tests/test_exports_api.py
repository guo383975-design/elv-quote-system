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
