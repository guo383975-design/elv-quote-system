"""「数据范围 = 全员可见」口径的行为锁（2026-10-09 用户决策 A ✓）。

背景：设计文档 §3.1/§3.2 原来写的是「SELF / DEPT / ALL 三级数据范围 + 所有报价单查询强制
拼接 WHERE 条件」✗ —— 而代码里**从来没有过**行级过滤 ✗：
`app/api/deps.py::require_permission` 只按角色判 RBAC ✓，
`list_quotes` / `get_quote` 里没有任何 `owner_id/department` 条件 ✓。
用户 2026-10-09 定调：**不做数据隔离，改成"全员可见/可协作"口径** ✓
（设计文档 §3.2 已按**实际行为**改写 ✓）。

为什么必须补这两条（E 线指出：全套测试**零跨用户用例** ✗）：
  · 正向：`sales_b` 能读到 `sales_a` 建的单 ✓ —— 把"全员可见"的语义**钉住** ✓，
    避免以后有人出于"安全"直觉**误加过滤**、把多角色接力协作这条主流程弄坏 ✗。
  · 边界：`viewer` 对**别人的**单能不能写 ✓ —— 照当前**真实行为**钉住 ✓
    （不是照文档愿望钉 ✓）。

⚠ 这是行为锁 ✗ 不是需求背书 ✓：若哪天要加真正的隔离，这两条会**故意变红** ✓
（那时请连同设计文档 §3.2 一起改 ✓ 别只改测试 ✗）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import Role
from app.core.security import hash_password
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.user import User, UserStatus

pytestmark = pytest.mark.asyncio


def dec(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def _mk_user(db: AsyncSession, username: str, role: Role) -> User:
    u = User(
        username=username,
        email=f"{username}@vis.test",
        hashed_password=hash_password(f"{username}-pw-12345"),
        full_name=username,
        role=role,
        status=UserStatus.ACTIVE,
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


async def _login(client: AsyncClient, username: str) -> dict[str, str]:
    r = await client.post(
        "/api/v1/auth/login/json",
        json={"username": username, "password": f"{username}-pw-12345"},
    )
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def _setup(client, db, admin_headers) -> dict:
    """造：项目 + sales_a 建的一张带明细的单 + sales_b / viewer 两个账号 ✓。"""
    await _mk_user(db, "sales_a", Role.SALES)
    await _mk_user(db, "sales_b", Role.SALES)
    await _mk_user(db, "watcher", Role.VIEWER)
    a_headers = await _login(client, "sales_a")
    b_headers = await _login(client, "sales_b")
    v_headers = await _login(client, "watcher")

    p = Project(
        code="PRJ-ZZVIS-0001",
        name="全员可见口径验证项目（临时）",
        customer_name="可见性客户",
        project_type=ProjectType.COMMERCIAL,
        status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)

    r = await client.post(
        "/api/v1/quotes", headers=a_headers,
        json={"project_id": str(p.id), "title": "sales_a 建的单", "code": "QT-ZZVIS-A",
              "tax_rate": "0.13", "auxiliary_rate": "0.07", "mgmt_profit_rate": "0.12"},
    )
    assert r.status_code == 201, r.text
    quote = r.json()
    r = await client.post(
        f"/api/v1/quotes/{quote['id']}/items", headers=a_headers,
        json={"name": "A 的设备", "quantity": "2", "unit_price": "1000",
              "install_unit_price": "100"},
    )
    assert r.status_code == 201, r.text
    return {
        "project_id": str(p.id), "quote": quote,
        "a": a_headers, "b": b_headers, "viewer": v_headers,
    }


# ---------------------------------------------------------------------------
# 正向：全员可见（sales_b 读到 sales_a 的单）
# ---------------------------------------------------------------------------

async def test_all_users_can_read_each_others_quotes(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """**全员可见** ✓：`sales_b` 能读到 `sales_a` 建的单（详情 / 列表 / 明细 / 重算视图 ✓）。

    这条测试是**故意的行为锁** ✓：它会把"按归属过滤"这种改动**测红** ✓ ——
    因为本系统的正常用法就是多角色在同一张单上接力（销售建 → 造价套定额 →
    经理审核 → 财务复核 → 导出）✓，加了归属过滤就把主流程弄坏了 ✗。
    """
    ctx = await _setup(client, db, admin_headers)
    qid = ctx["quote"]["id"]
    b = ctx["b"]

    # ① 详情（含明细行）
    r = await client.get(f"/api/v1/quotes/{qid}", headers=b)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "QT-ZZVIS-A", body
    assert len(body["items"]) == 1, body
    assert dec(body["equipment_total"]) == Decimal("2000.00"), body
    # 别人的单，金额一分数不少 ✓（不是"看得见但脱敏"）
    assert dec(body["total_with_tax"]) > Decimal("0.00"), body

    # ② 列表里也看得到（关键词直接命中别人的单号 ✓）
    r = await client.get("/api/v1/quotes", headers=b, params={"keyword": "QT-ZZVIS-A"})
    assert r.status_code == 200, r.text
    assert [x["id"] for x in r.json()["items"]] == [qid], r.json()

    # ③ 明细子资源
    r = await client.get(f"/api/v1/quotes/{qid}/items", headers=b)
    assert r.status_code == 200, r.text
    assert len(r.json()) == 1, r.json()

    # ④ 「保存并重算」的视图也能读（经理角色的主用途 ✓）
    r = await client.post(f"/api/v1/quotes/{qid}/compute", json={}, headers=b)
    assert r.status_code == 200, r.text

    # ⑤ 只读角色（viewer）同样能读 ✓ —— "全员可见"就是字面意思 ✓
    r = await client.get(f"/api/v1/quotes/{qid}", headers=ctx["viewer"])
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# 边界：viewer 能不能写**别人的**单（照当前真实行为钉住）
# ---------------------------------------------------------------------------

async def test_viewer_cannot_write_other_users_quote(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """边界钉住：**viewer 对别人的单不能写** ✓ —— 拒绝依据是**角色**（403 ✓），
    不是**归属** ✓。这是当前真实行为 ✓（先把实测结果写在这里 ✓）：

      实测（2026-10-09，本机内存库）：
        · viewer PATCH /quotes/{别人的单}      → **403**（`quote.update` 不含 viewer ✓）
        · viewer POST  /quotes/{别人的单}/items → **403**（`quote_item.create` ✓）
        · viewer POST  /quotes/{别人的单}/compute → **403**（`quote.compute` ✓）
        · viewer DELETE /quotes/{别人的单}      → **403**（`quote.delete` 只给 admin ✓）

    为什么这是**边界**而不是正向 ✓：改单的判定完全不看"这张单是谁的" ✓ ——
    所以从**同一个角色**出发，改**自己的**单与改**别人的**单**返回完全相同** ✓
    （下一段就是这条对照 ✓）。
    """
    ctx = await _setup(client, db, admin_headers)
    qid = ctx["quote"]["id"]
    v = ctx["viewer"]

    r = await client.patch(
        f"/api/v1/quotes/{qid}", headers=v, json={"title": "viewer 想改标题"}
    )
    assert r.status_code == 403, ("viewer 改别人的单", r.status_code, r.text)

    r = await client.post(
        f"/api/v1/quotes/{qid}/items", headers=v,
        json={"name": "viewer 想加行", "quantity": "1", "unit_price": "1"},
    )
    assert r.status_code == 403, ("viewer 加行", r.status_code, r.text)

    r = await client.post(f"/api/v1/quotes/{qid}/compute", json={}, headers=v)
    assert r.status_code == 403, ("viewer 重算", r.status_code, r.text)

    r = await client.delete(f"/api/v1/quotes/{qid}", headers=v)
    assert r.status_code == 403, ("viewer 删单", r.status_code, r.text)

    # ★ 关键对照：viewer 写**自己的**单同样是 403 ✓ → 拒绝依据是角色，不是归属 ✓
    r = await client.post(
        "/api/v1/quotes", headers=v,
        json={"project_id": ctx["project_id"], "title": "viewer 自己的单"},
    )
    assert r.status_code == 403, ("viewer 建单", r.status_code, r.text)

    # 而**同一张别人的单**，换成 sales（有 quote.update 的角色）→ 200 ✓
    #   —— 这条是"判定不看归属"的**直接反证** ✓：
    #   若哪天有人给报价单加了归属过滤（只让建单人改 ✗），这里会变成 403 → 测试红 ✓。
    r = await client.patch(
        f"/api/v1/quotes/{qid}", headers=ctx["b"], json={"title": "sales_b 接手改的"}
    )
    assert r.status_code == 200, ("sales 改别人的单应放行", r.status_code, r.text)
    got = (await client.get(f"/api/v1/quotes/{qid}", headers=ctx["b"])).json()
    assert got["title"] == "sales_b 接手改的", got
