"""项目级费率 → 报价单快照（审计 H-12，2026-10-03）。

**改动前**：项目的 8 个费率列是彻头彻尾的死列 —— 模型里有、`ProjectCreate` 里没有、
界面上也没有；既配不了、读不到，更不会影响报价单。审计的结论是"要么接进
`create_quote`，要么删列"，本轮选择**接上**。

**语义（快照）**：

1. 建报价单时，把项目上的 8 个费率字段**抄一份**到报价单上；
2. 调用方在 `QuoteCreate` 里**显式指定**的字段以调用方为准（用 `exclude_unset`
   判断"显式"，而不是"值不等于默认值"）；
3. 抄完就是**副本**：之后改项目费率**不会**回改已建的报价单 —— 否则已发出去的
   清单会被静默改数，那等于篡改。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus, ProjectType

pytestmark = pytest.mark.asyncio

CUSTOM_RATES = {
    "tax_rate": "0.13",
    "fee_base": "direct_fee",
    "auxiliary_base": "device_install",
    "auxiliary_rate": "0.08",
    "design_rate": "0.05",
    "warranty_rate": "0.04",
    "training_fee": "2000",
    "mgmt_profit_rate": "0.12",
}


async def _project(client: AsyncClient, headers, code: str, **overrides) -> dict:
    payload = {"code": code, "name": "费率项目", "customer_name": "客户", **overrides}
    r = await client.post("/api/v1/projects", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


def dec(v) -> Decimal:
    return Decimal(str(v)).quantize(Decimal("0.0001"))


async def test_project_exposes_and_accepts_rates(client: AsyncClient, admin_headers) -> None:
    """项目费率必须能配、也能读回（以前两样都不行）。"""
    p = await _project(client, admin_headers, "PRJ-RATE-1", **CUSTOM_RATES)
    for field, want in CUSTOM_RATES.items():
        if field in ("fee_base", "auxiliary_base"):
            assert p[field] == want, (field, p[field])
        else:
            # Numeric 列按精度回显（0.13 → 0.1300），所以按数值比、不按字符串比
            assert dec(p[field]) == dec(want), (field, p[field])

    r = await client.get(f"/api/v1/projects/{p['id']}", headers=admin_headers)
    assert r.status_code == 200
    assert dec(r.json()["design_rate"]) == Decimal("0.05")
    assert r.json()["fee_base"] == "direct_fee"


async def test_create_quote_snapshots_project_rates(
    client: AsyncClient, admin_headers
) -> None:
    """核心：建报价单时抄项目费率（前端「编制报价」只传 project_id）。"""
    p = await _project(client, admin_headers, "PRJ-RATE-2", **CUSTOM_RATES)
    r = await client.post("/api/v1/quotes", headers=admin_headers,
                          json={"project_id": p["id"]})
    assert r.status_code == 201, r.text
    q = r.json()

    assert dec(q["tax_rate"]) == Decimal("0.13")
    assert dec(q["auxiliary_rate"]) == Decimal("0.08")
    assert dec(q["design_rate"]) == Decimal("0.05")
    assert dec(q["warranty_rate"]) == Decimal("0.04")
    assert dec(q["training_fee"]) == Decimal("2000")
    assert dec(q["mgmt_profit_rate"]) == Decimal("0.12")
    assert q["fee_base"] == "direct_fee"
    assert q["auxiliary_base"] == "device_install"


async def test_explicit_quote_rates_win_over_project(
    client: AsyncClient, admin_headers
) -> None:
    """调用方显式给的费率不能被项目覆盖（判"显式"而不是"不等于默认值"）。"""
    p = await _project(client, admin_headers, "PRJ-RATE-3", **CUSTOM_RATES)
    r = await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": p["id"],
        "design_rate": "0.01",      # 显式指定
        "tax_rate": "0.06",         # 显式指定
    })
    assert r.status_code == 201, r.text
    q = r.json()
    assert dec(q["design_rate"]) == Decimal("0.01"), "显式指定被项目费率覆盖了"
    assert dec(q["tax_rate"]) == Decimal("0.06")
    # 没显式指定的仍然抄项目
    assert dec(q["auxiliary_rate"]) == Decimal("0.08")
    assert q["fee_base"] == "direct_fee"


async def test_project_rate_change_does_not_touch_existing_quotes(
    client: AsyncClient, admin_headers
) -> None:
    """快照不可变：改项目费率只影响**之后**新建的报价单。"""
    p = await _project(client, admin_headers, "PRJ-RATE-4", design_rate="0.05")
    r1 = await client.post("/api/v1/quotes", headers=admin_headers,
                           json={"project_id": p["id"]})
    old_quote = r1.json()
    assert dec(old_quote["design_rate"]) == Decimal("0.05")

    # 项目费率改成 0.09
    rp = await client.patch(f"/api/v1/projects/{p['id']}", headers=admin_headers,
                            json={"design_rate": "0.09"})
    assert rp.status_code == 200, rp.text

    # 老单还是 0.05（快照），新单才是 0.09
    rq = await client.get(f"/api/v1/quotes/{old_quote['id']}", headers=admin_headers)
    assert dec(rq.json()["design_rate"]) == Decimal("0.05"), "已建报价单被回改了"

    r2 = await client.post("/api/v1/quotes", headers=admin_headers,
                           json={"project_id": p["id"]})
    assert dec(r2.json()["design_rate"]) == Decimal("0.09")


async def test_default_project_rates_do_not_change_new_quotes(
    client: AsyncClient, admin_headers
) -> None:
    """**防止静默改数**：项目用默认费率时，抄过去必须与报价单原默认值**完全一致**。

    这条测试的由来：`projects.auxiliary_base` 的 server_default 是 `device_install`，
    而 `quotes.auxiliary_base` 是 `device`（0001 起就不一致）。一旦开始抄项目费率，
    每张新建报价单的辅材费基数就会被静默换掉 —— 金额变化而没人知道。
    已确认口径是"按设备金额"，所以两边都必须是 `device`。
    """
    p = await _project(client, admin_headers, "PRJ-RATE-DEFAULT")
    r = await client.post("/api/v1/quotes", headers=admin_headers,
                          json={"project_id": p["id"]})
    assert r.status_code == 201, r.text
    q = r.json()

    # 与 Quote 模型的默认值逐项对照（改任何一边都会在这里报警）
    assert q["auxiliary_base"] == "device", "辅材费基数被静默改掉了！"
    assert q["fee_base"] == "device_aux_install"
    assert dec(q["tax_rate"]) == Decimal("0.09")
    assert dec(q["auxiliary_rate"]) == Decimal("0.06")
    assert dec(q["design_rate"]) == Decimal("0.02")
    assert dec(q["warranty_rate"]) == Decimal("0.03")
    assert dec(q["training_fee"]) == Decimal("0")
    assert dec(q["mgmt_profit_rate"]) == Decimal("0.08")


async def test_explicit_device_install_choice_is_honored(
    client: AsyncClient, admin_headers
) -> None:
    """项目**显式**选了 `device_install` 就要照抄（默认值对齐不等于取消这个选项）。"""
    p = await _project(client, admin_headers, "PRJ-RATE-5",
                       auxiliary_base="device_install", design_rate="0.05")
    r = await client.post("/api/v1/quotes", headers=admin_headers,
                          json={"project_id": p["id"]})
    assert r.status_code == 201, r.text
    assert r.json()["auxiliary_base"] == "device_install"
    assert dec(r.json()["design_rate"]) == Decimal("0.05")


async def test_project_rate_bounds_are_enforced(client: AsyncClient, admin_headers) -> None:
    """费率边界与报价单一致（比例 ≤ 1，金额非负），否则 422。"""
    for bad in ({"tax_rate": "1.5"}, {"design_rate": "-0.1"}, {"training_fee": "-1"}):
        r = await client.post("/api/v1/projects", headers=admin_headers,
                              json={"code": f"PRJ-BAD-{list(bad)[0]}", "name": "x",
                                    "customer_name": "y", **bad})
        assert r.status_code == 422, f"{bad} 竟然通过了：{r.status_code} {r.text}"


async def test_bogus_legacy_project_enum_does_not_break_quote_creation(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """历史脏数据（项目上存了非法的 fee_base 字符串）不该让建单 500 —— 跳过该字段。"""
    p = Project(code="PRJ-LEGACY-1", name="脏数据项目", customer_name="客户",
                project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT,
                design_rate=Decimal("0.07"))
    db.add(p)
    await db.commit()
    await db.refresh(p)
    # 绕过 schema 直接写一个非法枚举值（模拟早期手工改库）
    await db.execute(
        Project.__table__.update().where(Project.id == p.id).values(fee_base="bogus_value")
    )
    await db.commit()

    r = await client.post("/api/v1/quotes", headers=admin_headers,
                          json={"project_id": str(p.id)})
    assert r.status_code == 201, r.text
    q = r.json()
    # 非法字段被跳过 → 用报价单自己的默认值；其它字段照常抄
    assert q["fee_base"] == "device_aux_install"
    assert dec(q["design_rate"]) == Decimal("0.07")
