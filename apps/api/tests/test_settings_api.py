"""系统设置测试（2026-10-04）。

重点验证"设置真的会影响输出"：改完公司抬头/有效期/条款后，
条款接口（客户版预览读它）与 PDF 导出都跟着变。
"""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus, ProjectType


async def _quote(client: AsyncClient, headers, project_id, code="QT-SET-1") -> dict:
    r = await client.post(
        "/api/v1/quotes",
        headers=headers,
        json={
            "project_id": str(project_id), "code": code, "title": "设置测试单",
            "fee_base": "device_aux_install", "auxiliary_base": "device",
            "auxiliary_rate": "0.06", "design_rate": "0.02", "warranty_rate": "0.03",
            "mgmt_profit_rate": "0.08", "tax_rate": "0.09",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_settings_defaults_then_roundtrip(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """没写过库时返回默认值；写过之后按新值返回。"""
    r = await client.get("/api/v1/settings", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["company"]["name"], body
    assert len(body["quote"]["clauses"]) == 6
    assert body["quote"]["valid_days"] == 30

    r = await client.patch(
        "/api/v1/settings",
        headers=admin_headers,
        json={
            "company": {"name": "测试科技有限公司", "phone": "0574-0000 0000"},
            "quote": {"valid_days": 45, "clauses": ["一、测试条款"]},
        },
    )
    assert r.status_code == 200, r.text
    saved = r.json()
    assert saved["company"]["name"] == "测试科技有限公司"
    # 未传的键要保留原值，不能被整体覆盖掉
    assert saved["company"]["address"], "没传 address 时应保留原值"
    assert saved["quote"]["valid_days"] == 45
    assert saved["quote"]["clauses"] == ["一、测试条款"]

    again = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert again["company"]["name"] == "测试科技有限公司"
    assert again["quote"]["valid_days"] == 45


async def test_settings_change_flows_into_export_meta(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """设置 → 条款接口（客户版预览）→ PDF 导出，整条链路跟着变。"""
    p = Project(
        code="PRJ-SET-1", name="设置测试项目", customer_name="客户",
        project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.commit()
    q = await _quote(client, admin_headers, p.id)

    await client.patch(
        "/api/v1/settings",
        headers=admin_headers,
        json={
            "company": {"name": "杭州某某智能科技有限公司"},
            "quote": {"valid_days": 15, "clauses": ["1. 自定义条款甲", "2. 自定义条款乙"]},
        },
    )

    r = await client.get(f"/api/v1/quotes/{q['id']}/export-clauses", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["clauses"] == ["1. 自定义条款甲", "2. 自定义条款乙"], body
    assert body["valid_days"] == 15, body
    assert body["company"]["name"] == "杭州某某智能科技有限公司", body

    # PDF 导出必须仍然能渲染（设置进的是渲染路径，不能把导出搞崩）
    r = await client.get(f"/api/v1/quotes/{q['id']}/export.pdf", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.content[:5] == b"%PDF-"
    r = await client.get(f"/api/v1/quotes/{q['id']}/export.xlsx", headers=admin_headers)
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK"


async def test_settings_update_requires_admin(
    client: AsyncClient, viewer_headers, admin_headers
) -> None:
    """设置改一次影响所有导出 —— 只有 admin 能写；只读角色连读都不该给。"""
    r = await client.patch(
        "/api/v1/settings", headers=viewer_headers, json={"quote": {"valid_days": 1}}
    )
    assert r.status_code == 403, r.text

    r = await client.get("/api/v1/settings", headers=viewer_headers)
    assert r.status_code == 403, r.text

    # admin 可以读
    assert (await client.get("/api/v1/settings", headers=admin_headers)).status_code == 200
