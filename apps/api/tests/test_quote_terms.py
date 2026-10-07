"""条款模板变量 + 单据级覆盖 测试（2026-10-05）。

核心要证的四件事：
  ① **改一处，条款跟着变** —— 以前有效期字段改了、条款文本还写着旧数字，
     给客户的报价单自相矛盾；
  ② 变量名写错 → 保存就被拦（422），不会带着 `{错别字}` 发出去；
  ③ 每张单可覆盖，且**不污染**其它单；
  ④ 传 null 能恢复"用系统设置"。
"""

from __future__ import annotations

from httpx import AsyncClient

from app.services.quote_terms import render_clauses, unknown_variables


def test_render_and_typo_detection() -> None:
    """纯函数层：渲染替换 + 错别字识别。"""
    out = render_clauses(
        ["有效期 {valid_days} 天，预付 {prepay_ratio}%", "没有变量的条款"],
        {"valid_days": 45, "prepay_ratio": 30},
    )
    assert out[0] == "有效期 45 天，预付 30%"
    assert out[1] == "没有变量的条款"
    # 未定义变量原样保留（保存时已被拦，这里只是兜底不静默吃掉）
    assert render_clauses(["{nope}"], {})[0] == "{nope}"
    assert unknown_variables(["有效期 {valid_dayss} 天"]) == ["valid_dayss"]
    assert unknown_variables(["有效期 {valid_days} 天"]) == []


async def _mk_quote(client: AsyncClient, headers) -> dict:
    prj = (await client.post("/api/v1/projects", headers=headers, json={
        "code": "PRJ-TERMS-1", "name": "条款测试项目", "customer_name": "某客户",
        "create_default_subsystems": False})).json()
    return (await client.post("/api/v1/quotes", headers=headers, json={
        "project_id": prj["id"], "code": "QT-TERMS-1", "title": "条款测试单",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0"})).json()


async def test_changing_number_updates_clause_text(
    client: AsyncClient, admin_headers
) -> None:
    """① 把有效期改成 60 天，条款第 2 条必须跟着写 60 天（不再自相矛盾）。"""
    q = await _mk_quote(client, admin_headers)

    before = (await client.get(f"/api/v1/quotes/{q['id']}/export-clauses",
                               headers=admin_headers)).json()
    assert before["valid_days"] == 30, before
    assert "报价有效期 30 天" in before["clauses"][1], before["clauses"]

    r = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "quote": {"valid_days": 60, "prepay_ratio": 50}})
    assert r.status_code == 200, r.text

    after = (await client.get(f"/api/v1/quotes/{q['id']}/export-clauses",
                              headers=admin_headers)).json()
    assert after["valid_days"] == 60, after
    assert "报价有效期 60 天" in after["clauses"][1], after["clauses"]
    assert "预付 50%" in after["clauses"][2], after["clauses"]
    # 模板里保留的是变量本身（说明"数字从哪来"可追溯）
    assert "{valid_days}" in after["clause_template"][1], after["clause_template"]
    assert after["available_variables"]["valid_days"], after["available_variables"]


async def test_unknown_variable_is_rejected(
    client: AsyncClient, admin_headers
) -> None:
    """② 变量名写错：保存设置、改单据，都要 422。"""
    bad = await client.patch("/api/v1/settings", headers=admin_headers, json={
        "quote": {"clauses": ["有效期 {valid_dayss} 天"]}})
    assert bad.status_code == 422, bad.text
    assert "valid_dayss" in bad.json()["detail"], bad.json()

    q = await _mk_quote(client, admin_headers)
    bad2 = await client.patch(f"/api/v1/quotes/{q['id']}", headers=admin_headers, json={
        "clauses_override": ["本单特批：有效期 {validdays} 天"]})
    assert bad2.status_code == 422, bad2.text


async def test_per_quote_override_does_not_leak(
    client: AsyncClient, admin_headers
) -> None:
    """③ 每张单可覆盖；④ 传 null 恢复全局。"""
    q1 = await _mk_quote(client, admin_headers)
    prj2 = (await client.post("/api/v1/projects", headers=admin_headers, json={
        "code": "PRJ-TERMS-2", "name": "另一个项目", "customer_name": "另一客户",
        "create_default_subsystems": False})).json()
    q2 = (await client.post("/api/v1/quotes", headers=admin_headers, json={
        "project_id": prj2["id"], "code": "QT-TERMS-2", "title": "不受影响的单",
        "fee_base": "device_aux_install", "auxiliary_base": "device",
        "auxiliary_rate": "0", "design_rate": "0", "warranty_rate": "0",
        "mgmt_profit_rate": "0", "tax_rate": "0"})).json()

    # 只给 q1 设覆盖（含变量，验证覆盖条款里的变量也会被替换）
    r = await client.patch(f"/api/v1/quotes/{q1['id']}", headers=admin_headers, json={
        "clauses_override": ["本单特批：预付 {prepay_ratio}%，有效期 {valid_days} 天"],
        "valid_days_override": 90})
    assert r.status_code == 200, r.text

    a1 = (await client.get(f"/api/v1/quotes/{q1['id']}/export-clauses",
                           headers=admin_headers)).json()
    assert a1["clauses_overridden"] is True and a1["valid_days_overridden"] is True, a1
    assert a1["valid_days"] == 90, a1
    assert a1["clauses"] == ["本单特批：预付 30%，有效期 90 天"], a1["clauses"]

    # q2 完全不受影响
    a2 = (await client.get(f"/api/v1/quotes/{q2['id']}/export-clauses",
                           headers=admin_headers)).json()
    assert a2["clauses_overridden"] is False and a2["valid_days_overridden"] is False, a2
    assert a2["valid_days"] == 30, a2
    assert len(a2["clauses"]) >= 6, a2["clauses"]
    assert "本单特批" not in " ".join(a2["clauses"]), a2["clauses"]

    # ④ 传 null 恢复全局
    r2 = await client.patch(f"/api/v1/quotes/{q1['id']}", headers=admin_headers, json={
        "clauses_override": None, "valid_days_override": None})
    assert r2.status_code == 200, r2.text
    a1b = (await client.get(f"/api/v1/quotes/{q1['id']}/export-clauses",
                            headers=admin_headers)).json()
    assert a1b["clauses_overridden"] is False and a1b["valid_days_overridden"] is False, a1b
    assert a1b["valid_days"] == 30, a1b
    assert len(a1b["clauses"]) >= 6, a1b["clauses"]


async def test_pdf_context_uses_rendered_clauses(
    client: AsyncClient, admin_headers, db
) -> None:
    """导出上下文拿到的必须是**渲染后**的条款（PDF/Excel 与预览同源）。

    用真实 ORM 对象（手搓假对象要补齐一堆金额字段，很容易假通过）。
    """
    from uuid import UUID

    from app.models.project import Project
    from app.models.quote import Quote
    from app.services.export_data import build_context
    from app.services.settings import load_settings

    q = await _mk_quote(client, admin_headers)
    r = await client.patch("/api/v1/settings", headers=admin_headers,
                           json={"quote": {"build_days": 30}})
    assert r.status_code == 200, r.text

    quote = await db.get(Quote, UUID(q["id"]))
    project = await db.get(Project, UUID(q["project_id"]))
    settings = await load_settings(db)

    ctx = build_context(
        quote=quote, project=project, items=[], subsystems=[],
        include_cost=False, settings=settings,
    )
    joined = " ".join(ctx.meta.clauses)
    assert "30 个日历天" in joined, ctx.meta.clauses      # 设置改了 → 条款跟着变
    assert "{build_days}" not in joined, ctx.meta.clauses  # 变量必须已替换


def test_legacy_default_clauses_are_upgraded() -> None:
    """老库升级：仍是旧默认文本的条款要换成变量版，用户改写过的不许碰。

    背景：`app_settings` 存过值就盖住代码默认值 —— 用户重置后保存过一次设置，
    旧的字面文本被持久化，后来改成变量版对他们不生效（上机验证抓到）。
    """
    from app.services.quote_terms_migrate import (
        LEGACY_DEFAULT_CLAUSES,
        upgrade_legacy_default_clauses,
    )

    # ① 还是旧默认 → 升级，并补齐结构化数字
    old = {"valid_days": 30, "clauses": list(LEGACY_DEFAULT_CLAUSES)}
    new_group, changed = upgrade_legacy_default_clauses(old)
    assert changed is True
    assert "{valid_days}" in new_group["clauses"][1], new_group["clauses"]
    assert new_group["prepay_ratio"] == 30 and new_group["build_days"] == 45

    # ② 用户改写过 → 一个字都不动
    mine = {"clauses": ["本公司特别约定：预付 50%，余款验收后结清。"]}
    kept, changed2 = upgrade_legacy_default_clauses(mine)
    assert changed2 is True or kept["clauses"] == mine["clauses"], kept
    assert kept["clauses"] == mine["clauses"], "用户自定义的条款被覆盖了！"

    # ③ 已经升级过 → 幂等（第二次不再改）
    again, changed3 = upgrade_legacy_default_clauses(new_group)
    assert again["clauses"] == new_group["clauses"]
