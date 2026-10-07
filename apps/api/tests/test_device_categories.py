"""设备库**分类树**测试（2026-09-30 起设备库不再是一层扁平分类）。

覆盖：
  · 树接口的层级、自身数量与子树数量、未分类数量
  · 按节点筛选要**包含子树**（点父节点不能是空列表）
  · 节点增删改：一级必须带 legacy_enum、不允许成环、有子/有设备时拒绝删除
  · 设备写库时一级分类枚举由树推导（不允许两套真相）
  · 权限：只有 admin/estimator 能维护分类
  · Excel 导入按「视频监控/摄像机」这样的路径落到树上，并按需新建分类
"""

from __future__ import annotations

import io
from uuid import UUID

from httpx import AsyncClient
from openpyxl import Workbook
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.device import Device, DeviceCategory, DeviceCategoryNode, DeviceStatus
from app.services.device_category import (
    auto_node_code,
    guess_root_enum,
    resolve_category,
    split_path,
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ---------------------------------------------------------------------------
# 解析层（纯函数，不碰数据库）
# ---------------------------------------------------------------------------

def test_split_path_handles_separators() -> None:
    assert split_path("视频监控/摄像机") == ["视频监控", "摄像机"]
    assert split_path("视频监控 / 摄像机") == ["视频监控", "摄像机"]
    assert split_path("视频监控\\摄像机") == ["视频监控", "摄像机"]
    assert split_path("视频监控>摄像机") == ["视频监控", "摄像机"]
    assert split_path("视频监控、摄像机") == ["视频监控", "摄像机"]
    assert split_path("") == []
    assert split_path(None) == []


def test_guess_root_enum_keywords() -> None:
    assert guess_root_enum("视频监控") == "video"
    assert guess_root_enum("综合布线") == "cabling"
    assert guess_root_enum("门禁一卡通") == "access"
    assert guess_root_enum("交换机") == "network"
    assert guess_root_enum("完全不相关") is None


class _Node:
    """给解析器用的假节点（它只按属性访问，不需要真 ORM 对象）。"""

    def __init__(self, nid, parent, name, code, legacy=None):
        self.id = nid
        self.parent_id = parent
        self.name = name
        self.code = code
        self.legacy_enum = legacy


def _fake_tree():
    from uuid import UUID

    video = UUID(int=1)
    camera = UUID(int=2)
    gun = UUID(int=3)
    nodes = {
        video: _Node(video, None, "视频监控", "video", "video"),
        camera: _Node(camera, video, "摄像机", "video.camera"),
        gun: _Node(gun, camera, "枪机", "video.camera.gun"),
    }
    return nodes, {"video": video}


def test_resolve_hits_existing_node() -> None:
    nodes, enum_to_root = _fake_tree()
    r = resolve_category("视频监控/摄像机", nodes=nodes, enum_to_root=enum_to_root)
    assert r.ok and not r.will_create
    assert r.root_enum == "video"
    assert r.node_path == "视频监控 / 摄像机"

    # 三层也能走到底
    r3 = resolve_category("视频监控/摄像机/枪机", nodes=nodes, enum_to_root=enum_to_root)
    assert r3.node_path == "视频监控 / 摄像机 / 枪机"

    # 只写小类名：树上唯一命中 → 直接用那个节点
    rl = resolve_category("枪机", nodes=nodes, enum_to_root=enum_to_root)
    assert rl.node_path == "视频监控 / 摄像机 / 枪机"

    # 英文代号
    rc = resolve_category("video", nodes=nodes, enum_to_root=enum_to_root)
    assert rc.root_enum == "video"


def test_resolve_reports_missing_levels() -> None:
    nodes, enum_to_root = _fake_tree()
    r = resolve_category("视频监控/球机", nodes=nodes, enum_to_root=enum_to_root)
    assert r.ok and r.will_create
    assert r.missing == ["球机"]
    assert r.node_id == next(iter([n.id for n in nodes.values() if n.name == "视频监控"]))


def test_resolve_without_any_tree_still_yields_root_enum() -> None:
    """新库/测试库还没有分类节点时，也要能解析出一级分类。

    `devices.category` 是 NOT NULL：解析不出来就没有归属，这是硬要求。
    """
    r = resolve_category("视频监控/摄像机", nodes={}, enum_to_root={})
    assert r.ok and r.root_enum == "video"
    assert r.missing == ["视频监控", "摄像机"]  # 根也要建


def test_auto_node_code_is_stable() -> None:
    a = auto_node_code("video", "球机")
    b = auto_node_code("video", "球机")
    c = auto_node_code("video", "半球")
    assert a == b, "同父同名必须得到同一个代号（导入要幂等）"
    assert a != c
    assert a.startswith("video.")


# ---------------------------------------------------------------------------
# 接口层
# ---------------------------------------------------------------------------

async def _make_tree(db: AsyncSession) -> dict[str, UUID]:
    """video 根 + 摄像机/球机 两个子节点；再建 access 根。

    返回**id 字符串**而不是 ORM 对象：conftest 的共享 session 在任何一次返回
    错误的请求之后会 rollback 并 expire 掉所有实例，之后再读 tree["x"].id
    就会触发懒加载 → MissingGreenlet（假故障，与产品无关）。
    """
    video = DeviceCategoryNode(
        code="video", name="视频监控", sort_order=0, legacy_enum="video", code_prefix="V"
    )
    access = DeviceCategoryNode(
        code="access", name="门禁一卡通", sort_order=1, legacy_enum="access", code_prefix="A"
    )
    db.add_all([video, access])
    await db.flush()
    camera = DeviceCategoryNode(
        code="video.camera", name="摄像机", parent_id=video.id, sort_order=0
    )
    ball = DeviceCategoryNode(code="video.ball", name="球机", parent_id=video.id, sort_order=1)
    db.add_all([camera, ball])
    await db.commit()
    for n in (video, access, camera, ball):
        await db.refresh(n)
    return {
        "video": video.id, "access": access.id,
        "camera": camera.id, "ball": ball.id,
    }


async def test_category_tree_shape_and_counts(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    db.add_all([
        Device(code="V9001", name="枪机", category=DeviceCategory.VIDEO,
               category_id=tree["camera"], status=DeviceStatus.ON_SALE),
        Device(code="V9002", name="球机", category=DeviceCategory.VIDEO,
               category_id=tree["ball"], status=DeviceStatus.ON_SALE),
        Device(code="V9003", name="挂在根上的", category=DeviceCategory.VIDEO,
               category_id=tree["video"], status=DeviceStatus.ON_SALE),
        # 未挂分类
        Device(code="V9004", name="未分类", category=DeviceCategory.VIDEO,
               status=DeviceStatus.ON_SALE),
    ])
    await db.commit()

    r = await client.get("/api/v1/devices/category-tree", headers=admin_headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["unassigned"] == 1
    assert body["total"] == 4
    by_name = {n["name"]: n for n in body["nodes"]}
    assert "视频监控" in by_name
    video = by_name["视频监控"]
    assert video["device_count"] == 1, "自身只挂了 1 台"
    assert video["total_count"] == 3, "含子节点共 3 台"
    kids = {c["name"]: c for c in video["children"]}
    assert kids["摄像机"]["path"] == "视频监控 / 摄像机"
    assert kids["摄像机"]["device_count"] == 1


async def test_list_by_node_includes_descendants(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    db.add_all([
        Device(code="V9101", name="枪机", category=DeviceCategory.VIDEO,
               category_id=tree["camera"], status=DeviceStatus.ON_SALE),
        Device(code="V9102", name="球机", category=DeviceCategory.VIDEO,
               category_id=tree["ball"], status=DeviceStatus.ON_SALE),
        Device(code="V9103", name="门禁", category=DeviceCategory.ACCESS,
               category_id=tree["access"], status=DeviceStatus.ON_SALE),
    ])
    await db.commit()

    # 点父节点 → 子树里的设备都要出来（否则树就是个装饰）
    r = await client.get(
        f"/api/v1/devices?category_id={tree['video']}", headers=admin_headers
    )
    assert r.status_code == 200, r.text
    codes = {i["code"] for i in r.json()["items"]}
    assert codes == {"V9101", "V9102"}
    # 回显分类路径
    row = [i for i in r.json()["items"] if i["code"] == "V9101"][0]
    assert row["category_path"] == "视频监控 / 摄像机"

    # 只要节点自身
    r2 = await client.get(
        f"/api/v1/devices?category_id={tree['video']}&include_descendants=false",
        headers=admin_headers,
    )
    assert {i["code"] for i in r2.json()["items"]} == set()

    # 未分类
    r3 = await client.get("/api/v1/devices?uncategorized=true", headers=admin_headers)
    assert r3.status_code == 200
    assert r3.json()["total"] == 0


async def test_device_category_enum_derived_from_tree(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """挂在「视频监控/摄像机」下的设备，category 必须是 video（不允许两套真相）。"""
    tree = await _make_tree(db)
    r = await client.post(
        "/api/v1/devices",
        headers=admin_headers,
        json={
            "code": "V9201", "name": "测试枪机", "category": "access",  # 故意传错
            "category_id": str(tree["camera"]),
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["category"] == "video", "应以分类树节点为准"
    assert r.json()["category_path"] == "视频监控 / 摄像机"

    # PATCH 改节点 → category 跟着变
    r2 = await client.patch(
        f"/api/v1/devices/{r.json()['id']}",
        headers=admin_headers,
        json={"category_id": str(tree["access"])},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["category"] == "access"


async def test_create_root_requires_legacy_enum_and_is_unique(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    await _make_tree(db)
    r = await client.post(
        "/api/v1/devices/categories",
        headers=admin_headers,
        json={"code": "fire", "name": "消防"},
    )
    assert r.status_code == 422, r.text

    r2 = await client.post(
        "/api/v1/devices/categories",
        headers=admin_headers,
        json={"code": "video2", "name": "又一个视频", "legacy_enum": "video"},
    )
    assert r2.status_code == 409, r2.text

    r3 = await client.post(
        "/api/v1/devices/categories",
        headers=admin_headers,
        json={"code": "power", "name": "电源线缆", "legacy_enum": "power",
              "code_prefix": "W"},
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["path"] == "电源线缆"


async def test_child_node_ignores_explicit_legacy_enum(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    r = await client.post(
        "/api/v1/devices/categories",
        headers=admin_headers,
        json={"code": "video.nvr", "name": "录像机", "parent_id": str(tree["video"]),
              "legacy_enum": "access"},   # 故意传一个别的
    )
    assert r.status_code == 201, r.text
    assert r.json()["legacy_enum"] is None, "子节点的 legacy_enum 必须由父链推导"
    assert r.json()["path"] == "视频监控 / 录像机"


async def test_move_node_rejects_cycle_and_root_under_child(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    # 把根挪到自己的子节点下面 → 400
    r = await client.patch(
        f"/api/v1/devices/categories/{tree['video']}",
        headers=admin_headers,
        json={"parent_id": str(tree["camera"])},
    )
    assert r.status_code == 400, r.text
    # 自己当自己的父 → 400
    r2 = await client.patch(
        f"/api/v1/devices/categories/{tree['camera']}",
        headers=admin_headers,
        json={"parent_id": str(tree["camera"])},
    )
    assert r2.status_code == 400, r2.text
    # 一级分类不能挪到别人下面
    r3 = await client.patch(
        f"/api/v1/devices/categories/{tree['access']}",
        headers=admin_headers,
        json={"parent_id": str(tree["video"])},
    )
    assert r3.status_code == 400, r3.text


async def test_move_node_resyncs_device_enum(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """把子树挪到另一个一级分类下 → 子树里设备的 category 也要跟着改。"""
    tree = await _make_tree(db)
    other_root = DeviceCategoryNode(
        code="network", name="计算机网络", sort_order=2, legacy_enum="network", code_prefix="N"
    )
    db.add(other_root)
    await db.flush()
    dev = Device(code="V9301", name="枪机", category=DeviceCategory.VIDEO,
                 category_id=tree["camera"], status=DeviceStatus.ON_SALE)
    db.add(dev)
    await db.commit()
    other_id = other_root.id   # 请求失败会回滚并 expire ORM 实例，先取值

    r = await client.patch(
        f"/api/v1/devices/categories/{tree['camera']}",
        headers=admin_headers,
        json={"parent_id": str(other_id)},
    )
    assert r.status_code == 200, r.text
    await db.refresh(dev)
    assert str(dev.category) == "network", "挪了子树，设备的一级分类必须重新对齐"


async def test_delete_node_refuses_with_children_or_devices(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    tree = await _make_tree(db)
    r = await client.delete(
        f"/api/v1/devices/categories/{tree['video']}", headers=admin_headers
    )
    assert r.status_code == 409 and "子分类" in r.json()["detail"], r.text

    db.add(Device(code="V9401", name="枪机", category=DeviceCategory.VIDEO,
                  category_id=tree["ball"], status=DeviceStatus.ON_SALE))
    await db.commit()
    r2 = await client.delete(
        f"/api/v1/devices/categories/{tree['ball']}", headers=admin_headers
    )
    assert r2.status_code == 409 and "设备" in r2.json()["detail"], r2.text

    # 空节点可以删
    empty = DeviceCategoryNode(code="video.empty", name="空节点", parent_id=tree["video"])
    db.add(empty)
    await db.commit()
    await db.refresh(empty)
    empty_id = empty.id
    r3 = await client.delete(
        f"/api/v1/devices/categories/{empty_id}", headers=admin_headers
    )
    assert r3.status_code == 204, r3.text


async def test_category_maintenance_permissions(
    client: AsyncClient, db: AsyncSession, viewer_headers, sales_headers, est_headers
) -> None:
    tree = await _make_tree(db)
    body = {"code": "video.monitor", "name": "监视器", "parent_id": str(tree["video"])}
    # viewer / sales 只能看
    assert (await client.post("/api/v1/devices/categories", headers=viewer_headers,
                              json=body)).status_code == 403
    assert (await client.post("/api/v1/devices/categories", headers=sales_headers,
                              json=body)).status_code == 403
    # 造价员可以维护（device_library.create = admin/estimator）
    assert (await client.post("/api/v1/devices/categories", headers=est_headers,
                              json=body)).status_code == 201
    # 但删分类只有 admin（device_library.delete）
    r = await client.delete(
        f"/api/v1/devices/categories/{tree['camera']}", headers=est_headers
    )
    assert r.status_code == 403, r.text


async def test_import_device_creates_missing_category_path(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    """Excel 里写「视频监控/摄像机」而树上没有这些节点 → 自动建好并落到那格。

    这就是"不要都写在一起"的落地点：导入时按用户写的层级建树，
    而不是把一堆二级分类压回一级分类里。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "设备清单"
    ws.append(["设备名称", "规格型号", "分类路径", "计量单位", "参考单价"])
    ws.append(["网络高清枪机", "DS-TEST-1", "视频监控/摄像机", "台", 680])
    ws.append(["24 口交换机", "S-TEST-1", "计算机网络/交换机", "台", 1380])
    buf = io.BytesIO()
    wb.save(buf)

    r = await client.post(
        "/api/v1/imports/devices/commit",
        headers=admin_headers,
        files={"file": ("d.xlsx", buf.getvalue(), XLSX_MIME)},
        data={"duplicate_policy": "skip", "skip_errors": "true"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["created"] == 2
    assert len(body["created_categories"]) == 4, body["created_categories"]  # 2 根 + 2 子

    # 设备落到二级节点上，且一级分类正确
    r2 = await client.get("/api/v1/devices?keyword=DS-TEST-1", headers=admin_headers)
    row = r2.json()["items"][0]
    assert row["category"] == "video"
    assert row["category_path"] == "视频监控 / 摄像机"

    # 再导一次：不该重复建分类（幂等）
    r3 = await client.post(
        "/api/v1/imports/devices/commit",
        headers=admin_headers,
        files={"file": ("d.xlsx", buf.getvalue(), XLSX_MIME)},
        data={"duplicate_policy": "overwrite", "skip_errors": "true"},
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["created_categories"] == [], "分类不该重复创建"

    total = len((await client.get("/api/v1/devices/category-tree",
                                  headers=admin_headers)).json()["nodes"])
    assert total == 2, "树上应该只有 2 个一级节点"


async def test_import_preview_reports_category_path(
    client: AsyncClient, db: AsyncSession, admin_headers
) -> None:
    await _make_tree(db)
    wb = Workbook()
    ws = wb.active
    ws.append(["设备名称", "规格型号", "所属分类"])
    ws.append(["球机", "DS-X", "视频监控/球机"])
    buf = io.BytesIO()
    wb.save(buf)
    r = await client.post(
        "/api/v1/imports/devices/preview",
        headers=admin_headers,
        files={"file": ("d.xlsx", buf.getvalue(), XLSX_MIME)},
        data={"duplicate_policy": "skip", "max_rows": "50"},
    )
    assert r.status_code == 200, r.text
    row = r.json()["preview"][0]
    assert row["category"] == "video"
    assert row["category_path"] == "视频监控 / 球机"
    assert row["category_new_nodes"] == []
    # "命中已有分类"不该把整行降级成 warn
    assert row["level"] == "ok"



