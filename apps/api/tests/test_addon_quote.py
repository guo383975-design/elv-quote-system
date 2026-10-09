"""增项单三连缺陷的**复现**（改码前先跑红 ✓ 2026-10-09）+ 修复后的回归锁 ✓。

审计已定论（B 线 file:line ✓），本文件先在本机内存库把三个缺陷**各自复现一次** ✓，
再作为修复后的回归锁 ✓。

三连：
  · P1-4 `create_quote` 建单后不调 `_recompute` ✗ → 增项单"有明细、合计 0.00"✗
        生产实例：`PRJ-202610-0002-Q02`（12 行明细 / 含税 0.00 / 设备费 0.00 / 安装费 0.00 ✓）
  · P1-1 增项复制明细**漏拷 `is_shielded`** ✗ → 安装费静默少 1/1.20 = 16.7% ✗（240 → 200 ✓）
  · P2   导出件里的"子系统汇总"取的是**项目级**列 ✗ → 增项单导出时把源单的贡献
        一起算进来 ✗，与同一份导出件的"报价明细 / 报价汇总"自相矛盾 ✗

外加一条**承重墙**回归：定额模式下同一笔安装钱不双计 ✓
（E 线指出把 `quotes.py:162` 的 `quota_enabled=(...)!= "quota"` 改回旧写法 →
 全量 645 条仍全绿 ✗ —— 那条路当年零测试 ✓，这条测试专门拦它 ✓）。

⚠ 样本一律用**非默认值** ✓（我们有过"费率恰好全默认 → 假通过"的血泪 ✓）。
"""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.labor import QuotaCategory, QuotaItem
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote_item import QuoteItem
from app.models.subsystem import Subsystem, SubsystemType

pytestmark = pytest.mark.asyncio


def dec(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


async def _make_project(db: AsyncSession, code: str = "PRJ-ZZADDON-REPRO") -> Project:
    p = Project(
        code=code,
        name="增项复现项目（临时）",
        customer_name="复现客户",
        project_type=ProjectType.COMMERCIAL,
        status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _make_subsystem(db: AsyncSession, pid) -> Subsystem:
    sub = Subsystem(
        project_id=pid,
        subsystem_type=SubsystemType.VIDEO_SURVEILLANCE,
        custom_name="视频监控",
        sort_order=0,
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


# 原单：**非默认**费率（辅材 7% / 设计 3% / 维保 4% / 管理 12% / 税 13% / 培训 666）
_SRC_PAYLOAD = {
    "title": "原单（非默认费率）",
    "fee_base": "device_only",
    "auxiliary_base": "device",
    "auxiliary_rate": "0.07",
    "design_rate": "0.03",
    "warranty_rate": "0.04",
    "mgmt_profit_rate": "0.12",
    "tax_rate": "0.13",
    "training_fee": "666",
    "labor_float_rate": "0.15",
}


async def _seed_source(client: AsyncClient, headers, pid, sub_id) -> dict:
    """造原单：1 行**屏蔽**线缆，数量 2 / 设备单价 500 / 安装单价 100 / 进价 300。

    安装费 = 2 × 100 × 1.20（屏蔽系数 by 报价级设置缺省 1.20）= 240.00 ✓
    —— 正是审计实测的那个数 ✓。
    """
    r = await client.post(
        "/api/v1/quotes", headers=headers, json={"project_id": str(pid), **_SRC_PAYLOAD}
    )
    assert r.status_code == 201, r.text
    src = r.json()
    r = await client.post(
        f"/api/v1/quotes/{src['id']}/items",
        headers=headers,
        json={
            "name": "屏蔽双绞线",
            "brand": "海康",
            "model": "DS-SHIELD-1",
            "spec_text": "屏蔽双绞线 2×1.0",
            "unit": "箱",
            "quantity": "2",
            "unit_price": "500",
            "install_unit_price": "100",
            "cost_price": "300",
            "auxiliary_unit_price": "5",
            "is_shielded": True,
            "is_cat5e": False,
            "is_ceiling": False,
            "source": "manual",
            "notes": "屏蔽线 quota:5-1-001",
            "subsystem_id": str(sub_id),
            "sort_order": 7,
        },
    )
    assert r.status_code == 201, r.text
    return src


async def _make_addon(client: AsyncClient, headers, pid, src_id) -> dict:
    r = await client.post(
        "/api/v1/quotes",
        headers=headers,
        json={"project_id": str(pid), "title": "增项单", "based_on_quote_id": src_id},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _items(client: AsyncClient, headers, qid) -> list[dict]:
    r = await client.get(f"/api/v1/quotes/{qid}/items", headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------------------
# P1-4：建单（含增项单）后要带上正确合计
# ---------------------------------------------------------------------------

async def test_new_quote_has_totals_immediately_after_creation(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """建完就有合计：普通单（先建单后加行）与增项单（建单即复制明细）都要 ✓。

    复现（改码前）：增项单建完 = 明细已复制 **但** 设备费/安装费/含税合计全是 0.00 ✗
    （生产 `PRJ-202610-0002-Q02` 就是这个形态 ✓）。
    """
    p = await _make_project(db)
    sub = await _make_subsystem(db, p.id)

    # ① 普通空单：建完的合计 = 「保存并重算」算出来的值 ✓
    #    （**不能**断言 0.00 ✗：设计费/维保按基数为 0 算 ✓，但培训费 666 是**报价单级**
    #      的一笔固定费用 ✓、与有没有明细无关 ✓ —— 空单带培训费时合计本来就不是 0 ✓。
    #      以前建单后不算 ✗ → 界面显示 0.00 ✗，用户得点一次「保存并重算」才对上 ✗。）
    r = await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": str(p.id), "title": "空单", **_SRC_PAYLOAD},
    )
    assert r.status_code == 201, r.text
    empty = r.json()
    empty_computed = (
        await client.post(f"/api/v1/quotes/{empty['id']}/compute", json={}, headers=admin_headers)
    ).json()
    assert dec(empty["total_with_tax"]) == dec(empty_computed["total_with_tax"]), (
        empty, empty_computed,
    )
    assert dec(empty["equipment_total"]) == Decimal("0.00"), empty
    assert dec(empty["install_total"]) == Decimal("0.00"), empty
    # 建单后**不落版本** ✓（建单不是"人保存出来的一版"✓ 版本快照只在保存/提交/批准时落 ✓）
    assert empty["revision_count"] == 0, empty

    # ② 原单 + 明细（明细写入路径照旧会重算 ✓）
    src = await _seed_source(client, admin_headers, p.id, sub.id)
    src_detail = (await client.get(f"/api/v1/quotes/{src['id']}", headers=admin_headers)).json()
    assert dec(src_detail["equipment_total"]) == Decimal("1000.00"), src_detail
    assert dec(src_detail["install_total"]) == Decimal("240.00"), src_detail
    src_total = dec(src_detail["total_with_tax"])
    assert src_total > Decimal("0.00"), src_detail

    # ③ 增项单：建单响应里就必须带正确合计 ✓（改码前这里是 0.00 ✗）
    add = await _make_addon(client, admin_headers, p.id, src["id"])
    assert dec(add["equipment_total"]) == Decimal("1000.00"), add
    assert dec(add["install_total"]) == Decimal("240.00"), add
    assert dec(add["total_with_tax"]) == src_total, (
        "增项单建完即应带上正确合计（改码前是 0.00）"
    )

    # ④ 再核对一遍持久化后的值（响应体 ≠ 库里的值 这种事必须排掉 ✓）
    add_detail = (await client.get(f"/api/v1/quotes/{add['id']}", headers=admin_headers)).json()
    assert dec(add_detail["total_with_tax"]) == src_total, add_detail
    assert dec(add_detail["subtotal_before_tax"]) == dec(src_detail["subtotal_before_tax"])
    # 成本合计不在单详情里（成本是独立权限 ✓，走 GET /quotes/{id}/cost ✓）→ 直接查库比 ✓
    from app.models.quote import Quote

    src_row = await db.get(Quote, UUID(src["id"]))
    add_row = await db.get(Quote, UUID(add["id"]))
    assert dec(add_row.cost_total) == dec(src_row.cost_total), (add_row.cost_total,
                                                                src_row.cost_total)

    # ⑤ 三条路径的合计不重复 ✓：再改一次明细 → 合计只跟着明细走（不是累加 ✓）
    rows = await _items(client, admin_headers, add["id"])
    assert len(rows) == 1, rows
    r = await client.patch(
        f"/api/v1/quotes/{add['id']}/items/{rows[0]['id']}",
        headers=admin_headers, json={"quantity": "3"},
    )
    assert r.status_code == 200, r.text
    add2 = (await client.get(f"/api/v1/quotes/{add['id']}", headers=admin_headers)).json()
    # 设备费 3×500 = 1500（不是 1000+1500 ✗）；安装费 3×100×1.20 = 360
    assert dec(add2["equipment_total"]) == Decimal("1500.00"), add2
    assert dec(add2["install_total"]) == Decimal("360.00"), add2
    # 再点一次 compute（幂等 ✓）→ 不得把合计翻倍 ✗
    r = await client.post(f"/api/v1/quotes/{add['id']}/compute", json={}, headers=admin_headers)
    assert r.status_code == 200, r.text
    add3 = (await client.get(f"/api/v1/quotes/{add['id']}", headers=admin_headers)).json()
    assert dec(add3["total_with_tax"]) == dec(add2["total_with_tax"]), add3


# ---------------------------------------------------------------------------
# P1-1：增项复制明细 —— 该拷的字段逐个拷齐（含 is_shielded）
# ---------------------------------------------------------------------------

async def test_addon_item_copy_is_field_complete(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """增项行的**业务列**必须逐个拷贝 ✓ —— 尤其 `is_shielded` ✗（改码前漏拷 ✗）。

    漏拷的代价是**静默**的：安装费少 1/1.20 = 16.7%（实测 240 → 200 ✓），
    没有任何报错、没有任何提示 ✓ —— 所以必须逐字段钉住 ✓。
    """
    p = await _make_project(db)
    sub = await _make_subsystem(db, p.id)
    src = await _seed_source(client, admin_headers, p.id, sub.id)
    add = await _make_addon(client, admin_headers, p.id, src["id"])

    rows = await _items(client, admin_headers, add["id"])
    assert len(rows) == 1, rows
    got = rows[0]

    # ---- 本单特有：quote_id 指向新单；parent_item_id 这里为 None（源行就是主行）----
    assert got["quote_id"] == add["id"], got
    assert got["parent_item_id"] is None, got
    # ---- 关联 ----
    assert got["subsystem_id"] == str(sub.id), got
    assert got["device_id"] is None, got          # 源行是手工行，没有设备库引用 ✓
    # ---- 业务文本 ----
    assert got["name"] == "屏蔽双绞线", got
    assert got["brand"] == "海康", got
    assert got["model"] == "DS-SHIELD-1", got
    assert got["spec_text"] == "屏蔽双绞线 2×1.0", got
    assert got["unit"] == "箱", got
    assert got["notes"] == "屏蔽线 quota:5-1-001", got
    assert got["sort_order"] == 7, got
    assert got["source"] == "manual", got
    # ---- 数量与单价（金额的**输入**，必须原样拷 ✓）----
    assert dec(got["quantity"]) == Decimal("2.00"), got
    assert dec(got["unit_price"]) == Decimal("500.00"), got
    assert dec(got["install_unit_price"]) == Decimal("100.00"), got
    assert dec(got["auxiliary_unit_price"]) == Decimal("5.00"), got
    # ---- 布尔标记：甲供 + 三个工艺系数（**is_shielded 就是这里漏的** ✗）----
    assert got["is_owner_supplied"] is False, got
    assert got["is_shielded"] is True, "增项复制漏拷 is_shielded（P1-1）"
    assert got["is_cat5e"] is False, got
    assert got["is_ceiling"] is False, got
    # ---- 派生金额：由 _recompute 从上面重算 ✓（值必须与源单一致 ✓）----
    assert dec(got["equipment_amount"]) == Decimal("1000.00"), got
    assert dec(got["install_amount"]) == Decimal("240.00"), (
        "is_shielded 丢了 → 安装费会变成 200.00（少 16.7%）"
    )
    # ---- 成本（不在 QuoteItemOut 里 → 直接查库 ✓ 也是"该拷字段"之一 ✓）----
    item = await db.get(QuoteItem, UUID(got["id"]))
    assert dec(item.cost_price) == Decimal("300.00"), item.cost_price

    # 再验一遍"工艺系数真的参与了计算"：源单/增项单的安装费必须相同 ✓
    src_detail = (await client.get(f"/api/v1/quotes/{src['id']}", headers=admin_headers)).json()
    add_detail = (await client.get(f"/api/v1/quotes/{add['id']}", headers=admin_headers)).json()
    assert dec(add_detail["install_total"]) == dec(src_detail["install_total"]), (
        add_detail, src_detail,
    )


# ---------------------------------------------------------------------------
# P2：导出件的"子系统汇总"必须只描述**这一张单**
# ---------------------------------------------------------------------------

async def test_export_subsystem_summary_is_per_quote_not_project(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """增项单的导出件：三张表都必须是**这一张单**的数字 ✓。

    复现（改码前）：`_load_for_export` 把**项目全部**子系统交给 `build_subsystem_rows` ✗
    → 它读的是项目级列 ✗（`_rollup_subsystems` 按项目全部报价单聚合 ✓）
    → 增项单导出时"子系统汇总"= 原单 + 增项 = 翻倍 ✗，
      而同页"报价明细 / 报价汇总"只有这一张单 ✗ —— 文件自己跟自己打架 ✓。
    """
    from app.api.v1.exports import _load_for_export
    from app.services.export_data import build_context, build_quote_subsystem_rows

    p = await _make_project(db)
    sub = await _make_subsystem(db, p.id)
    src = await _seed_source(client, admin_headers, p.id, sub.id)
    add = await _make_addon(client, admin_headers, p.id, src["id"])

    quote, project, subsystems = await _load_for_export(db, UUID(add["id"]))
    items = sorted(quote.items, key=lambda it: (it.sort_order or 0, str(it.id)))
    assert len(items) == 1, items

    # ★ 修复后的正确取数（导出路由现在走的就是这一句 ✓）
    per_quote = build_quote_subsystem_rows(
        items, subsystems, auxiliary_total=quote.auxiliary_total
    )
    # MUTANT-M3 已跑完（证明：还原 P2 后本测试变红 ✓），此处恢复修复后的取数 ✓
    per_quote = build_quote_subsystem_rows(
        items, subsystems, auxiliary_total=quote.auxiliary_total
    )
    ctx = build_context(
        quote=quote, project=project, items=items, subsystems=subsystems,
        include_cost=False, subsystem_rows=per_quote,
    )

    assert len(ctx.subsystems) == 1, ctx.subsystems
    row = ctx.subsystems[0]
    # 本单：设备 1000 / 安装 240 / 辅材 = 1000 × 7% = 70 / 小计 1240
    assert row.equipment == "1,000.00", row
    assert row.install == "240.00", row
    assert row.auxiliary == "70.00", row
    assert row.amount == "1,240.00", row
    assert row.point_count == "2", row

    # 与同一份导出件的"报价汇总"口径一致 ✓（不再自相矛盾 ✓）
    assert dec(quote.equipment_total) == Decimal("1000.00"), quote.equipment_total
    assert dec(quote.install_total) == Decimal("240.00"), quote.install_total
    assert dec(quote.auxiliary_total) == Decimal("70.00"), quote.auxiliary_total

    # 项目级列仍然是"两张单的聚合" ✓ —— 修复**没有**去动它 ✗（项目视图还要它 ✓）
    fresh = await db.get(Subsystem, sub.id)
    await db.refresh(fresh)
    assert dec(fresh.equipment_subtotal) == Decimal("2000.00"), fresh.equipment_subtotal
    assert dec(fresh.install_subtotal) == Decimal("480.00"), fresh.install_subtotal

    # 反证：老写法（直接用项目级列）在增项单上就是翻倍 ✗ —— 这条断言钉住"为什么必须改"
    from app.services.export_data import build_subsystem_rows

    old = build_subsystem_rows(subsystems)
    assert old[0].equipment == "2,000.00", old[0]
    assert per_quote[0].equipment != old[0].equipment, (per_quote, old)


# ---------------------------------------------------------------------------
# 承重墙：定额模式下同一笔安装钱不双计（E 线点名的"变异存活"那条）
# ---------------------------------------------------------------------------

async def test_quota_mode_does_not_double_count_install_money(
    client: AsyncClient, db: AsyncSession, admin_headers, labor_rates
) -> None:
    """**定额模式**（`install_mode="quota"`）下安装费 = 定额基价，定额人工费不再叠加 ✓。

    为什么必须补这条（E 线结论 ✓）：`quotes.py:162` 的
    `quota_enabled=(...)!= "quota"` 改回旧写法（恒 True）后，
    **全量 645 条测试仍然全绿** ✗ —— 也就是这条钱路当年**零测试** ✗。
    这条用同一批明细造两张单（手工 / 定额）对比 ✓：
      · 手工单：合计 = 明细安装费 + 定额人工费（两条口径**都**要算 ✓）
      · 定额单：合计 = 明细安装费（定额人工费**不再叠加** ✓）
    两边**不含税小计必须相等** ✓（钱一分不多一分不少 ✓）；
    若把 `quota_enabled` 改回恒 True ✗，定额单会多出一笔定额人工费 ✗ → 立刻红 ✓。
    """
    # 人工信息价由 conftest 的 labor_rates fixture 提供（一类 150 / 二类 163 / 三类 186 ✓）。
    # 定额子目用**三类工日都非零**的一条 ✓ —— 少一类就会被 `_compute_quota_total`
    # 整条跳过、定额人工费静默变 0 ✗，那样这条测试就"看起来通过其实没测到"✗。
    db.add(QuotaItem(code="5-1-001", category=QuotaCategory.CABLING,
                     name="管内穿放双绞线", unit="100m",
                     labor_a_day=Decimal("0.42"), labor_b_day=Decimal("1.85"),
                     labor_c_day=Decimal("0.31"), base_price=Decimal("128.50")))
    await db.commit()

    p = await _make_project(db, code="PRJ-ZZQUOTA-REPRO")
    sub = await _make_subsystem(db, p.id)

    item_payload = {
        "name": "管内穿放双绞线",
        "quantity": "1.7",
        "unit_price": "320.11",
        "install_unit_price": "128.50",
        "cost_price": "201.03",
        "notes": "quota:5-1-001",
        "subsystem_id": str(sub.id),
    }
    rates = {"fee_base": "device_only", "auxiliary_rate": "0.05", "design_rate": "0.03",
             "warranty_rate": "0.02", "mgmt_profit_rate": "0.11", "tax_rate": "0.13",
             "labor_float_rate": "0.10"}

    totals: dict[str, Decimal] = {}
    details: dict[str, dict] = {}
    for mode in ("manual", "quota"):
        r = await client.post(
            "/api/v1/quotes", headers=admin_headers,
            json={"project_id": str(p.id), "title": f"{mode} 单",
                  "install_mode": mode, **rates},
        )
        assert r.status_code == 201, r.text
        q = r.json()
        r = await client.post(
            f"/api/v1/quotes/{q['id']}/items", headers=admin_headers, json=item_payload
        )
        assert r.status_code == 201, r.text
        d = (await client.get(f"/api/v1/quotes/{q['id']}", headers=admin_headers)).json()
        assert d["install_mode"] == mode, d
        details[mode] = d
        totals[mode] = dec(d["subtotal_before_tax"])

    manual, quota = details["manual"], details["quota"]

    # ① 两条路径的**明细安装费**是同一笔钱 ✓
    assert dec(manual["install_total"]) == dec(quota["install_total"]), (manual, quota)
    assert dec(quota["install_total"]) == Decimal("218.45"), quota  # 1.7 × 128.50

    # ② 手工单：定额人工费**要**单列（这是"另一笔钱"✓ 不是重复 ✓）
    assert dec(manual["quota_total"]) > Decimal("0.00"), manual

    # ③ 定额单：定额人工费**不再叠加** ✓（改码前/被改回旧写法时这里就是 1 倍以上）
    assert dec(quota["quota_total"]) == Decimal("0.00"), (
        "定额模式下定额口径必须为 0（人工费已含在安装费基价内）"
    )

    # ④ ★ 承重墙：不含税小计**相等** ✓ —— 把 quota_enabled 改回恒 True → 这里变红 ✓
    assert totals["manual"] == totals["quota"], (
        f"定额模式下同一笔安装钱被算了两次：manual={totals['manual']} "
        f"quota={totals['quota']}"
    )
    # ⑤ 顺带钉住"不是两单都算错成一样"：手工单 = 安装 + 定额人工费 ✓ 关系式成立
    assert totals["manual"] == dec(
        Decimal(str(manual["equipment_total"]))
        + Decimal(str(manual["install_total"]))
        + Decimal(str(manual["auxiliary_total"]))
        + Decimal(str(manual["design_fee"]))
        + Decimal(str(manual["warranty_fee"]))
        + Decimal(str(manual["training_fee"]))
    ), manual


# ---------------------------------------------------------------------------
# 归档单不可删除（2026-10-09 补，审计 E 线 P1-2）
# ---------------------------------------------------------------------------

async def test_archived_quote_cannot_be_deleted(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """归档即定稿 ✓ → 归档单 DELETE 必须 409 ✗（改码前是 204 ✗）。

    为什么这是钱/责任的问题 ✓：归档后还能整张删掉 ✗，
    `quote_revisions`（业务版本快照）随 0018 的 ON DELETE CASCADE 一起消失 ✗
    —— "定稿"就成了一个状态字 ✓，出了争议连它当时长什么样都查不回来 ✗。
    未被归档的单仍然可删 ✓（下面同一条测试里钉住，避免守卫被写成"谁都不能删"✗）。
    """
    p = await _make_project(db, code="PRJ-ZZARCH-DEL")
    # ⚠ 先把 id 取成普通值 ✓：建单接口里有过 422（归档单重算）→ 请求会 rollback ✓
    #   → ORM 实例被 expire ✓，之后再访问 p.id 会触发同步懒加载 → MissingGreenlet ✗。
    pid = p.id
    sub_id = (await _make_subsystem(db, pid)).id
    src = await _seed_source(client, admin_headers, pid, sub_id)

    # 走完整流转到 archived（submitted → reviewing → approved → archived ✓）
    for st in ("submitted", "reviewing", "approved", "archived"):
        d = (await client.get(f"/api/v1/quotes/{src['id']}", headers=admin_headers)).json()
        r = await client.patch(
            f"/api/v1/quotes/{src['id']}", headers=admin_headers,
            json={"status": st, "version": d["version"]},
        )
        assert r.status_code == 200, (st, r.text)
    d = (await client.get(f"/api/v1/quotes/{src['id']}", headers=admin_headers)).json()
    assert d["status"] == "archived", d
    # 归档前先落一版快照，才能证明"删了就把定稿也删了"这件事
    r = await client.post(f"/api/v1/quotes/{src['id']}/compute", json={}, headers=admin_headers)
    assert r.status_code == 409, r.text   # 归档单连重算都不许 ✓（既有守卫）

    # ★ 归档单：DELETE 必须 409
    r = await client.delete(f"/api/v1/quotes/{src['id']}", headers=admin_headers)
    assert r.status_code == 409, r.text
    assert "归档" in r.text and "定稿" in r.text, r.text
    # 单还在 ✓ 状态还是 archived ✓
    after = (await client.get(f"/api/v1/quotes/{src['id']}", headers=admin_headers)).json()
    assert after["status"] == "archived", after

    # 反证：同一张单回退到 draft 之前**不许**绕过（归档不可逆 → 422 ✓ 既有守卫）
    r = await client.patch(
        f"/api/v1/quotes/{src['id']}", headers=admin_headers,
        json={"status": "draft", "version": after["version"]},
    )
    assert r.status_code == 422, r.text

    # 同项目另建一张**未归档**的单 → 仍然可删 ✓（守卫是"窄"的 ✓）
    draft = await client.post(
        "/api/v1/quotes", headers=admin_headers,
        json={"project_id": str(pid), "title": "草稿单", **_SRC_PAYLOAD},
    )
    assert draft.status_code == 201, draft.text
    draft_id = draft.json()["id"]
    r = await client.delete(f"/api/v1/quotes/{draft_id}", headers=admin_headers)
    assert r.status_code == 204, r.text
    assert (
        await client.get(f"/api/v1/quotes/{draft_id}", headers=admin_headers)
    ).status_code == 404
    # 删掉之后那条删除留痕**仍然查得到** ✓（"谁把哪张单删了"答得上来 ✓）——
    # 即"删除留痕要活过对象删除"这条口径 ✓（外键置空 + subject_code 存单号 ✓）。
    r = await client.get(f"/api/v1/quotes/{draft_id}/audit-logs", headers=admin_headers)
    # 单已不存在 → 该单的留痕路由 404 ✓（属主查询），但审计表里必须留着这条 ✓
    from sqlalchemy import select

    from app.models.audit import AuditAction, QuoteAuditLog

    rows = list(
        (
            await db.scalars(
                select(QuoteAuditLog).where(QuoteAuditLog.action == AuditAction.DELETE)
            )
        ).all()
    )
    hits = [x for x in rows if x.subject_code == draft.json()["code"]]
    assert hits, [x.subject_code for x in rows]
    assert hits[-1].quote_id is None, hits[-1].quote_id   # 外键已置空 ✓ 记录还在 ✓
    assert r.status_code in (200, 404), r.status_code
