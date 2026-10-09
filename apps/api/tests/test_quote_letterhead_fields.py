"""「公司抬头」第二批：抬头扩成「名称 + 地址 + 电话 + 邮箱」（2026-10-09 用户补充）。

用户原话：「地址和联系电话和邮箱还是要修改一下的」✓
（第一批只做了公司名称 —— 用户当时说"只要公司名称"，现在是**补充** ✗ 不是推翻 ✓）。

本文件守住的两条硬约束与第一批相同，但覆盖**四个字段**：
  ① **老单不许变**：四项都为 NULL 的老单，出口必须仍是「XX科技」那一整套；
  ② **快照性**：选完抬头后改掉抬头库里那一家的地址/电话/邮箱，
     那张单**四项都仍是原值** ✓✓（存的是值本身，不是指向抬头库的 id）；
  ③ **逐字段**回落：只填了名称的抬头，地址/电话/邮箱要**各自**回落到系统设置 ✓
     —— 不是"本单有任意一项就整组用本单"✗（那样联系方式会整块变空白 ✗）。

出口仍是**真读渲染产物**：
  · `export-clauses` 接口 → 前端「客户版预览页」的唯一数据源（返回 company 四项）
  · 客户版 PDF → pypdf 抽正文（抬头块把四项都印了）
  · Excel → openpyxl 读「报价汇总」A2=名称、A3=地址·电话·邮箱一行
（内部版 PDF = 同一路由 `?internal=true` ✓ 同一 meta ✓。）

另有 ⑤：用户要求把预览页那个**写死的「XX科技」徽标**换成中性品牌字「智能化弱电」——
本仓有"用 pytest 断言前端源码"的既有做法（见 test_settings_view_structure.py ✓），
这里沿用：既断言**源码**里不再有旧徽标、又断言**渲染产物**里不再出现「XX科技」字样 ✓。
"""

from __future__ import annotations

import io
import pathlib

from httpx import AsyncClient
from openpyxl import load_workbook
from pypdf import PdfReader
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectStatus, ProjectType

ROOT = pathlib.Path(__file__).resolve().parents[3]
PREVIEW_VIEW = ROOT / "apps/web/src/views/QuotePreviewView.vue"

# 系统设置里 company 的出厂默认=「XX科技」那一整套（= 加这个功能之前所有单据印的）
DEFAULT_COMPANY = "XX科技有限公司"
DEFAULT_ADDRESS = "宁波市鄞州区 XX 路 88 号"
DEFAULT_PHONE = "0574-8888 8888"
DEFAULT_EMAIL = "sales@xxzn.com"

# 用户确认的中性品牌字（替换掉预览页写死的「XX科技」徽标）
NEUTRAL_BRAND = "智能化弱电"


# ---------------------------------------------------------------------------
# 助手
# ---------------------------------------------------------------------------

def _pdf_text(content: bytes) -> str:
    reader = PdfReader(io.BytesIO(content))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


async def _project(db: AsyncSession, code: str = "PRJ-LHF-1") -> Project:
    p = Project(
        code=code, name="抬头四项测试项目", customer_name="客户",
        project_type=ProjectType.COMMERCIAL, status=ProjectStatus.DRAFT,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _quote(client: AsyncClient, headers, project_id, code: str = "QT-LHF-1") -> dict:
    r = await client.post(
        "/api/v1/quotes",
        headers=headers,
        json={
            "project_id": str(project_id), "code": code, "title": "抬头四项测试单",
            "fee_base": "device_aux_install", "auxiliary_base": "device",
            "auxiliary_rate": "0.06", "design_rate": "0.02", "warranty_rate": "0.03",
            "mgmt_profit_rate": "0.08", "tax_rate": "0.09",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _patch_quote(client: AsyncClient, headers, quote_id, **fields) -> dict:
    r = await client.patch(f"/api/v1/quotes/{quote_id}", headers=headers, json=fields)
    assert r.status_code == 200, r.text
    return r.json()


async def _four_everywhere(client: AsyncClient, headers, quote_id) -> dict:
    """三处出口各取一次「名称/地址/电话/邮箱」✓（返回可供断言的实际值 ✓）。"""
    r = await client.get(f"/api/v1/quotes/{quote_id}/export-clauses", headers=headers)
    assert r.status_code == 200, r.text
    c = r.json()["company"]

    r = await client.get(f"/api/v1/quotes/{quote_id}/export.pdf", headers=headers)
    assert r.status_code == 200, r.text
    assert r.content[:5] == b"%PDF-", r.content[:5]
    pdf = _pdf_text(r.content)

    r = await client.get(f"/api/v1/quotes/{quote_id}/export.xlsx", headers=headers)
    assert r.status_code == 200, r.text
    assert r.content[:2] == b"PK", r.content[:2]
    ws = load_workbook(io.BytesIO(r.content))["报价汇总"]

    return {
        "preview": {
            "name": c["name"], "address": c["address"],
            "phone": c["phone"], "email": c["email"],
        },
        "pdf": pdf,
        "xlsx_name": str(ws.cell(row=2, column=1).value or ""),
        "xlsx_contact": str(ws.cell(row=3, column=1).value or ""),
    }


def _assert_four(found: dict, *, name: str, address: str, phone: str, email: str) -> None:
    """三处出口必须都是这四项 ✓；对不上就把**实际值**打出来 ✓。"""
    assert found["preview"] == {
        "name": name, "address": address, "phone": phone, "email": email,
    }, f"预览接口返回的 company 不是这四项：{found['preview']}"
    for label, value in (("名称", name), ("地址", address), ("电话", phone), ("邮箱", email)):
        assert value in found["pdf"], f"客户版 PDF 正文里没有{label} {value!r}\n{found['pdf'][:400]}"
    assert found["xlsx_name"] == name, f"Excel A2 应为名称，实际 {found['xlsx_name']!r}"
    for label, value in (("地址", address), ("电话", phone), ("邮箱", email)):
        assert value in found["xlsx_contact"], (
            f"Excel A3（联系方式行）里没有{label} {value!r}，实际 {found['xlsx_contact']!r}"
        )


# ---------------------------------------------------------------------------
# ① 老单：四项仍是「XX科技」那一整套，且四列仍为 NULL
# ---------------------------------------------------------------------------

async def test_legacy_quote_prints_default_name_and_contact(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _project(db)
    q = await _quote(client, admin_headers, p.id)

    # 新建的单四项都没写 = NULL ✓（不是被写成"XX科技"那套字面量 ✗ ——
    # 那样以后改系统设置里的公司信息，老单就不会跟着变了 = 改了老单行为 ✗）。
    assert q["company_name"] is None, q
    assert q["company_address"] is None, q
    assert q["company_phone"] is None, q
    assert q["company_email"] is None, q

    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(
        found, name=DEFAULT_COMPANY, address=DEFAULT_ADDRESS,
        phone=DEFAULT_PHONE, email=DEFAULT_EMAIL,
    )


# ---------------------------------------------------------------------------
# ② 选自定义抬头 → 四项一起变，且系统设置不被污染
# ---------------------------------------------------------------------------

async def test_custom_letterhead_changes_all_four_fields(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _project(db)
    q = await _quote(client, admin_headers, p.id)
    lh = {
        "name": "浙江某某科技有限公司", "address": "杭州市西湖区某某路 1 号",
        "phone": "0571-1111 2222", "email": "abc@example.invalid",
    }
    await _patch_quote(
        client, admin_headers, q["id"],
        company_name=lh["name"], company_address=lh["address"],
        company_phone=lh["phone"], company_email=lh["email"],
    )

    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(found, **lh)

    # 系统设置（全局那套）**没被本单覆盖污染** ✓
    s = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert s["company"]["name"] == DEFAULT_COMPANY, s["company"]
    assert s["company"]["address"] == DEFAULT_ADDRESS, s["company"]
    assert s["company"]["phone"] == DEFAULT_PHONE, s["company"]
    assert s["company"]["email"] == DEFAULT_EMAIL, s["company"]


# ---------------------------------------------------------------------------
# ③ ★快照性：改掉抬头库里那一家的三项 → 那张单四项都仍是原值
# ---------------------------------------------------------------------------

async def test_four_field_snapshot_survives_library_change(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _project(db)
    q = await _quote(client, admin_headers, p.id)
    lh = {
        "name": "甲抬头有限公司", "address": "宁波市甲路 1 号",
        "phone": "0574-0000 0001", "email": "jia@example.invalid",
    }

    # 抬头库里先有这一家
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": [lh]}},
    )
    assert r.status_code == 200, r.text

    # 本单选用它 —— 界面上选一下就会把四项**一起快照**进本单 ✓（这里照做 ✓）
    await _patch_quote(
        client, admin_headers, q["id"],
        company_name=lh["name"], company_address=lh["address"],
        company_phone=lh["phone"], company_email=lh["email"],
    )

    # ★ 之后把库里那一家的**四项全改掉**（连名称也改）✗
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": [{
            "name": "甲抬头（已改名）", "address": "已改的地址",
            "phone": "已改的电话", "email": "已改的邮箱",
        }]}},
    )
    assert r.status_code == 200, r.text
    lib = (await client.get("/api/v1/settings", headers=admin_headers)).json()
    assert lib["company"]["letterheads"][0]["address"] == "已改的地址", lib["company"]

    # 那张单**四项必须仍是原值** ✓✓ —— 这是本功能最容易做漏的一条
    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(found, **lh)
    assert "已改的地址" not in found["pdf"], "抬头库的改动被追溯写进了历史单据 ✗"


# ---------------------------------------------------------------------------
# ④ 逐字段回落：只填名称 → 其余三项各自用系统设置
# ---------------------------------------------------------------------------

async def test_empty_fields_fall_back_one_by_one(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    p = await _project(db)
    q = await _quote(client, admin_headers, p.id)

    # (a) 只给名称 → 地址/电话/邮箱**各自**回落 ✓（不是整组变空白 ✗）
    await _patch_quote(client, admin_headers, q["id"], company_name="只有名字的公司")
    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(
        found, name="只有名字的公司", address=DEFAULT_ADDRESS,
        phone=DEFAULT_PHONE, email=DEFAULT_EMAIL,
    )

    # (b) 再单独补一个电话 → 名称/地址/邮箱保持不变、各自独立 ✓
    await _patch_quote(client, admin_headers, q["id"], company_phone="0574-9999 9999")
    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(
        found, name="只有名字的公司", address=DEFAULT_ADDRESS,
        phone="0574-9999 9999", email=DEFAULT_EMAIL,
    )

    # (c) 显式传 null = 这一项恢复用系统设置 ✓（前端选回「默认」就是这条路 ✓）
    await _patch_quote(client, admin_headers, q["id"], company_phone=None)
    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(
        found, name="只有名字的公司", address=DEFAULT_ADDRESS,
        phone=DEFAULT_PHONE, email=DEFAULT_EMAIL,
    )


# ---------------------------------------------------------------------------
# ⑤ 徽标中性化：预览页不再有「XX科技」徽标；自定义抬头的产物里也没有「XX科技」痕迹
# ---------------------------------------------------------------------------

def test_preview_badge_is_neutralised() -> None:
    """预览页那个写死的「XX科技」徽标已换成中性品牌字 ✓（两处模板必须一致 ✓）。"""
    src = PREVIEW_VIEW.read_text(encoding="utf-8")
    # 旧的写死徽标必须**一处都不剩** ✓
    assert '<div class="doc-brand">XX科技</div>' not in src, "预览页还留着旧的「XX科技」徽标 ✗"
    # 中性品牌字要出现在**恰好两处**（量尺纸 + 分页渲染，代码注释要求两者逐字一致 ✓）
    assert src.count(f'<div class="doc-brand">{NEUTRAL_BRAND}</div>') == 2, (
        "预览页两处 doc-brand 必须都是中性品牌字，且逐字一致 ✗"
    )


async def test_no_legacy_brand_trace_when_custom_letterhead(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """选了别家抬头 → 产物里**不该再出现「XX科技」**（含 PDF 正文与 Excel ✓）。"""
    p = await _project(db)
    q = await _quote(client, admin_headers, p.id)
    await _patch_quote(
        client, admin_headers, q["id"],
        company_name="浙江某某科技有限公司", company_address="杭州市西湖区某某路 1 号",
        company_phone="0571-1111 2222", company_email="abc@example.invalid",
    )
    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(
        found, name="浙江某某科技有限公司", address="杭州市西湖区某某路 1 号",
        phone="0571-1111 2222", email="abc@example.invalid",
    )
    assert "XX科技" not in found["pdf"], f"自定义抬头的 PDF 里仍有「XX科技」痕迹：\n{found['pdf'][:400]}"
    assert "XX科技" not in found["xlsx_name"], found["xlsx_name"]
    assert "XX科技" not in found["xlsx_contact"], found["xlsx_contact"]
    assert found["preview"]["name"] != DEFAULT_COMPANY, found["preview"]


# ---------------------------------------------------------------------------
# 抬头库的结构：对象数组 + 兼容旧字符串数组
# ---------------------------------------------------------------------------

async def test_letterheads_store_four_fields_and_normalise_legacy_strings(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    # 存对象数组 ✓：逐字段去空白 ✓、按名称去重 ✓
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": [
            {"name": " 甲公司 ", "address": " 甲地址 ", "phone": " 111 ", "email": " a@x.invalid "},
            {"name": "甲公司", "address": "重复项应被丢掉"},       # 同名去重 ✓
            {"name": "乙公司"},                                     # 只填名称 ✓（其余三项可空 ✓）
        ]}},
    )
    assert r.status_code == 200, r.text
    heads = r.json()["company"]["letterheads"]
    assert heads == [
        {"name": "甲公司", "address": "甲地址", "phone": "111", "email": "a@x.invalid"},
        {"name": "乙公司", "address": "", "phone": "", "email": ""},
    ], heads

    # ⚠ 兼容**旧格式**（字符串数组）✓ —— 读到不能崩 ✗（旧配置档/旧前端可能这么传 ✓）
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": ["老格式公司", "  ", "另一家"]}},
    )
    assert r.status_code == 200, r.text
    heads = r.json()["company"]["letterheads"]
    assert heads == [
        {"name": "老格式公司", "address": "", "phone": "", "email": ""},
        {"name": "另一家", "address": "", "phone": "", "email": ""},
    ], heads

    # 类型错仍要拦 ✓（数组里混进数字 = 接口误用 ✗）
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": [123]}},
    )
    assert r.status_code == 422, r.text

    # 单字段超长 → 422 ✓（电话上限 64 与列宽一致 ✓）
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": [{"name": "甲公司", "phone": "9" * 65}]}},
    )
    assert r.status_code == 422, r.text


async def test_name_only_letterhead_is_usable(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """抬头库里有"只填了名称"的那条 → 本单选它，三项各自回落 ✓ 不崩 ✓。"""
    p = await _project(db)
    q = await _quote(client, admin_headers, p.id)
    r = await client.patch(
        "/api/v1/settings", headers=admin_headers,
        json={"company": {"letterheads": [{"name": "只填名称的公司"}]}},
    )
    assert r.status_code == 200, r.text

    await _patch_quote(client, admin_headers, q["id"], company_name="只填名称的公司")
    found = await _four_everywhere(client, admin_headers, q["id"])
    _assert_four(
        found, name="只填名称的公司", address=DEFAULT_ADDRESS,
        phone=DEFAULT_PHONE, email=DEFAULT_EMAIL,
    )
