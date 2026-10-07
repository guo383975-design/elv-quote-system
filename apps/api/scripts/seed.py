"""Seed script: 人工信息价 + 定额子目 + 设备库样例 + 演示项目/报价单.

Run:
    python -m scripts.seed            # 全量（幂等，已存在则跳过）
    python -m scripts.seed --reset    # 清空业务数据后重建
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import delete, select

from app.core.config import settings
from app.core.device_tree import CODE_PREFIX_BY_CATEGORY, SEED_CHILDREN
from app.core.labels import label_of
from app.core.logging import get_logger, setup_logging
from app.core.permissions import Role
from app.core.security import hash_password
from app.db.session import dispose_engine, init_engine, session_scope
from app.models.department import Department
from app.models.device import Device, DeviceCategory, DeviceCategoryNode, DeviceStatus
from app.models.labor import LaborClass, LaborRate, QuotaCategory, QuotaItem
from app.models.project import Project, ProjectStatus, ProjectType
from app.models.quote import AuxiliaryBaseType, FeeBaseType, Quote, QuoteStatus
from app.models.quote_item import ItemSource, QuoteItem
from app.models.subsystem import Subsystem, SubsystemType
from app.models.supplier import Supplier
from app.models.user import User, UserStatus
from app.services.quote_compute import (
    compute_quote,
    parse_quota_code,
    write_breakdown_to_quote,
)

setup_logging()
log = get_logger("seed")

# ---------------------------------------------------------------------------
# 1) 人工市场信息价（宁波市 2025-09 期；已由用户确认）
# ---------------------------------------------------------------------------
LABOR_RATES = [
    (LaborClass.A, date(2025, 9, 1), Decimal("150.00")),
    (LaborClass.B, date(2025, 9, 1), Decimal("163.00")),
    (LaborClass.C, date(2025, 9, 1), Decimal("186.00")),
]

# ---------------------------------------------------------------------------
# 2) 定额子目（浙江省通用安装工程预算定额 2018 第五册 建筑智能化工程）
#    ⚠ 人工消耗量数值为结构示例，实际以纸质定额书为准（界面上已标注"待造价员核对"）
#    ⚠ 编号也是**占位编号**（定额书真值是 5-2-13 这种无零填充形式）：
#       真值编号/名称共 843 条，由 scripts/import_quota_zj2018.py 从广联达
#       浙江定额库导入；导入后本表这 35 条会被置为 is_active=False 退役，
#       但**保留在库里**——演示报价单的备注仍引用它们（"quota:5-6-001"）。
#    章节取值按 2018 版真值：综合布线与管槽（线槽/桥架/钢管/机柜）都在 5-2。
# ---------------------------------------------------------------------------
QUOTA_ITEMS = [
    # (code, category, name, unit, A工日, B工日, C工日, 基价)
    ("5-1-001", QuotaCategory.CABLING, "管内穿放双绞线 4 对以内", "100m", 0.42, 1.85, 0.90, 128.50),
    ("5-1-002", QuotaCategory.CABLING, "管内穿放光缆 12 芯以内", "100m", 0.55, 2.40, 1.10, 196.80),
    ("5-1-003", QuotaCategory.CABLING, "安装信息插座 双口", "10个", 0.30, 1.20, 0.55, 96.40),
    ("5-1-004", QuotaCategory.CABLING, "安装配线架 24 口", "个", 0.25, 0.95, 0.45, 78.20),
    ("5-1-005", QuotaCategory.CABLING, "安装光纤配线架 12 口", "个", 0.28, 1.10, 0.50, 88.60),
    ("5-1-006", QuotaCategory.CABLING, "光缆熔接", "芯", 0.08, 0.18, 0.08, 28.50),
    ("5-1-007", QuotaCategory.CABLING, "光缆成端 12 芯", "个", 0.35, 1.40, 0.65, 118.00),
    ("5-1-008", QuotaCategory.CABLING, "安装信息面板 单口", "10个", 0.22, 0.88, 0.40, 72.80),
    ("5-3-001", QuotaCategory.BUILDING_AUTO, "楼宇自控 控制器安装", "台", 0.60, 2.50, 1.15, 210.40),
    ("5-3-002", QuotaCategory.BUILDING_AUTO, "DDC 控制器安装", "台", 0.65, 2.70, 1.25, 228.60),
    ("5-3-003", QuotaCategory.BUILDING_AUTO, "温度传感器安装", "只", 0.18, 0.70, 0.32, 58.40),
    ("5-3-004", QuotaCategory.BUILDING_AUTO, "电动调节阀安装", "台", 0.45, 1.85, 0.85, 156.20),
    ("5-5-001", QuotaCategory.AV, "安装会议扬声器 吸顶式", "只", 0.22, 0.88, 0.40, 72.30),
    ("5-5-002", QuotaCategory.AV, "安装投影机 含吊架", "台", 0.85, 3.40, 1.55, 286.50),
    ("5-5-003", QuotaCategory.AV, "安装音频处理器", "台", 0.35, 1.40, 0.65, 116.80),
    ("5-5-004", QuotaCategory.AV, "安装功放", "台", 0.32, 1.28, 0.58, 106.40),
    ("5-5-005", QuotaCategory.AV, "安装会议话筒", "只", 0.16, 0.62, 0.28, 51.60),
    ("5-5-006", QuotaCategory.AV, "安装信息发布屏 ≤55寸", "台", 0.60, 2.40, 1.10, 202.50),
    ("5-6-001", QuotaCategory.SECURITY, "安装摄像机 枪型/半球（已含调试）", "台", 0.35, 1.40, 0.65, 112.60),
    ("5-6-002", QuotaCategory.SECURITY, "安装门禁读卡器", "台", 0.28, 1.15, 0.52, 92.80),
    ("5-6-003", QuotaCategory.SECURITY, "安装报警探测器 红外", "只", 0.20, 0.82, 0.38, 66.40),
    ("5-6-004", QuotaCategory.SECURITY, "安装道闸 含调试", "台", 1.20, 4.80, 2.20, 396.00),
    ("5-6-005", QuotaCategory.SECURITY, "安装摄像机支架/立杆", "根", 0.30, 1.20, 0.55, 98.60),
    ("5-6-006", QuotaCategory.SECURITY, "安装门禁电锁", "把", 0.22, 0.88, 0.40, 72.60),
    ("5-6-007", QuotaCategory.SECURITY, "安装车牌识别一体机", "台", 0.85, 3.40, 1.55, 288.00),
    ("5-7-001", QuotaCategory.CABLING, "安装金属线槽 宽 100 以内", "10m", 0.48, 1.95, 0.90, 142.60),
    ("5-7-002", QuotaCategory.CABLING, "安装金属线槽 宽 300 以内", "10m", 0.72, 2.90, 1.32, 218.40),
    ("5-7-003", QuotaCategory.CABLING, "安装 JDG 钢管 直径 25 以内", "100m", 1.10, 4.60, 2.10, 356.20),
    ("5-7-004", QuotaCategory.CABLING, "安装 PVC 管 直径 25 以内", "100m", 0.88, 3.70, 1.68, 284.80),
    ("5-7-005", QuotaCategory.CABLING, "安装桥架 宽 400 以内", "10m", 0.85, 3.40, 1.55, 286.00),
    ("5-7-006", QuotaCategory.CABLING, "安装桥架 宽 800 以内", "10m", 1.15, 4.60, 2.10, 398.50),
    ("other-001", QuotaCategory.CABLING, "安装机柜 42U", "台", 0.60, 2.40, 1.10, 208.60),
    ("other-002", QuotaCategory.OTHER, "安装 UPS 主机", "台", 0.90, 3.60, 1.65, 308.40),
    ("other-003", QuotaCategory.NETWORK, "安装网络交换机", "台", 0.35, 1.40, 0.65, 118.60),
    ("other-004", QuotaCategory.NETWORK, "安装路由器/防火墙", "台", 0.45, 1.85, 0.85, 158.20),
]

# ---------------------------------------------------------------------------
# 3) 设备库（与原型页面对齐的编码体系）
# ---------------------------------------------------------------------------
SUPPLIERS = [
    # (code, name) — code 必须唯一；按 SUP-001..004 顺序排，方便后面做报价单里
    # "默认供应商" 的预设。新加 supplier 时手动续号即可，不要在 seed 里自动算。
    ("SUP-001", "宁波XX安防科技有限公司"),
    ("SUP-002", "杭州XX智能设备有限公司"),
    ("SUP-003", "浙江XX网络技术有限公司"),
    ("SUP-004", "宁波XX线缆有限公司"),
]

DEVICES = [
    # code, name, brand, model, unit, category, 参考价, 成本价, 安装单价, 状态, 甲供, 替代型号
    ("V1001", "网络高清枪型摄像机 400万", "海康威视", "DS-2CD2T46WD-I5", "台",
     DeviceCategory.VIDEO, "680.00", "510.00", "112.60", DeviceStatus.ON_SALE, False, None),
    ("V1002", "网络高清半球摄像机 400万", "海康威视", "DS-2CD2145FWD-I", "台",
     DeviceCategory.VIDEO, "720.00", "540.00", "112.60", DeviceStatus.ON_SALE, False, None),
    ("V1003", "网络球机 23 倍变焦", "海康威视", "DS-2DE4A425IW-DE", "台",
     DeviceCategory.VIDEO, "4,850.00", "3,640.00", "286.50", DeviceStatus.ON_SALE, False, None),
    ("V1004", "NVR 网络录像机 32 路", "海康威视", "DS-8632N-K8", "台",
     DeviceCategory.VIDEO, "3,280.00", "2,460.00", "210.40", DeviceStatus.ON_SALE, False, None),
    ("V1005", "监控级硬盘 8TB", "希捷", "ST8000VX004", "块",
     DeviceCategory.VIDEO, "1,180.00", "885.00", "0.00", DeviceStatus.EOL, False, "V1006"),
    ("V1006", "监控级硬盘 8TB（新品）", "希捷", "ST8000VX010", "块",
     DeviceCategory.VIDEO, "1,240.00", "930.00", "0.00", DeviceStatus.ON_SALE, False, None),
    ("V1007", "网络高清枪型摄像机 400万（宇视）", "宇视", "IPC2A4L-IR5-VF40", "台",
     DeviceCategory.VIDEO, "665.00", "498.00", "112.60", DeviceStatus.NEAR_EOL, False, "V1001"),
    ("V1008", "网络高清枪型摄像机 400万（大华）", "大华", "DH-IPC-HFW3441M", "台",
     DeviceCategory.VIDEO, "645.00", "484.00", "112.60", DeviceStatus.EOL, False, "V1009"),
    ("V1009", "网络高清枪型摄像机 400万（大华新品）", "大华", "DH-IPC-HFW3449M", "台",
     DeviceCategory.VIDEO, "688.00", "516.00", "112.60", DeviceStatus.ON_SALE, False, None),
    ("V1010", "网络高清筒机 400万 星光级", "海康威视", "DS-2CD3T46WD-I3", "台",
     DeviceCategory.VIDEO, "698.00", "524.00", "112.60", DeviceStatus.EOL, False, "V1011"),
    ("V1011", "网络高清筒机 400万 星光级（新品）", "海康威视", "DS-2CD2T46WD-I5", "台",
     DeviceCategory.VIDEO, "710.00", "533.00", "112.60", DeviceStatus.ON_SALE, False, None),

    ("A2001", "门禁控制器 双门", "中控智慧", "C3-200", "台",
     DeviceCategory.ACCESS, "1,280.00", "960.00", "92.80", DeviceStatus.ON_SALE, False, None),
    ("A2002", "门禁读卡器 IC 卡", "中控智慧", "KR101", "台",
     DeviceCategory.ACCESS, "320.00", "240.00", "92.80", DeviceStatus.ON_SALE, False, None),
    ("A2003", "电磁锁 280kg", "海康威视", "DS-K4H250", "把",
     DeviceCategory.ACCESS, "285.00", "214.00", "66.40", DeviceStatus.ON_SALE, False, None),
    ("A2004", "一卡通发卡器 USB", "中控智慧", "CR20", "台",
     DeviceCategory.ACCESS, "460.00", "345.00", "66.40", DeviceStatus.EOL, True, None),

    ("I3001", "红外入侵探测器 吸顶", "霍尼韦尔", "IS3050", "只",
     DeviceCategory.INTRUSION, "185.00", "139.00", "66.40", DeviceStatus.ON_SALE, False, None),
    ("I3002", "报警主机 8 防区", "霍尼韦尔", "VISTA-20P", "台",
     DeviceCategory.INTRUSION, "1,460.00", "1,095.00", "142.60", DeviceStatus.ON_SALE, False, None),

    ("C4001", "六类非屏蔽双绞线 305m", "一舟", "Y-Z6U305", "箱",
     DeviceCategory.CABLING, "780.00", "585.00", "0.00", DeviceStatus.ON_SALE, False, None),
    ("C4002", "单模光缆 12 芯", "一舟", "GJFJV-12B1", "米",
     DeviceCategory.CABLING, "6.80", "5.10", "0.00", DeviceStatus.ON_SALE, False, None),
    ("C4003", "六类信息模块 RJ45", "一舟", "MK-6U", "个",
     DeviceCategory.CABLING, "28.00", "21.00", "0.00", DeviceStatus.ON_SALE, False, None),
    ("C4004", "金属线槽 100×50", "国产", "XC-10050", "米",
     DeviceCategory.CABLING, "32.00", "24.00", "0.00", DeviceStatus.ON_SALE, False, None),
    ("C4005", "JDG 钢管 Φ25", "国产", "JDG25", "米",
     DeviceCategory.CABLING, "9.60", "7.20", "0.00", DeviceStatus.ON_SALE, False, None),

    ("N5001", "24 口千兆交换机", "华为", "S1730S-L24T-A", "台",
     DeviceCategory.NETWORK, "1,380.00", "1,035.00", "0.00", DeviceStatus.ON_SALE, True, None),
    ("N5002", "48 口千兆交换机", "华为", "S1730S-L48T-A", "台",
     DeviceCategory.NETWORK, "2,860.00", "2,145.00", "0.00", DeviceStatus.ON_SALE, True, None),
    ("N5003", "千兆光模块 单模 20km", "华为", "eSFP-GE-LX-SM1310", "个",
     DeviceCategory.NETWORK, "420.00", "315.00", "0.00", DeviceStatus.ON_SALE, True, None),
    ("N5004", "无线 AP 吸顶式 WiFi6", "华为", "AirEngine 5761-11", "台",
     DeviceCategory.NETWORK, "1,180.00", "885.00", "0.00", DeviceStatus.ON_SALE, True, None),

    ("B6001", "网络广播功放 350W", "ITC", "T-350S", "台",
     DeviceCategory.BROADCAST, "3,280.00", "2,460.00", "142.60", DeviceStatus.ON_SALE, False, None),
    ("B6002", "吸顶扬声器 6W", "ITC", "T-105C", "只",
     DeviceCategory.BROADCAST, "128.00", "96.00", "72.30", DeviceStatus.ON_SALE, False, None),

    ("M7001", "会议吸顶扬声器", "BOSCH", "LBC 3090/01", "只",
     DeviceCategory.CONFERENCE, "1,860.00", "1,395.00", "72.30", DeviceStatus.ON_SALE, False, None),
    ("M7002", "数字会议主机", "BOSCH", "CCS 1000 D", "台",
     DeviceCategory.CONFERENCE, "18,600.00", "13,950.00", "286.50", DeviceStatus.ON_SALE, False, None),

    ("S8001", "42U 服务器机柜 600×1000", "图腾", "G26642", "台",
     DeviceCategory.SERVER_ROOM, "2,180.00", "1,635.00", "210.40", DeviceStatus.ON_SALE, False, None),
    ("S8002", "机房精密空调 12.5kW", "艾默生", "DME12MHP1", "台",
     DeviceCategory.SERVER_ROOM, "68,000.00", "51,000.00", "1,860.00", DeviceStatus.ON_SALE, False, None),

    ("P9001", "车牌识别一体机", "臻识", "C5", "台",
     DeviceCategory.PARKING, "6,800.00", "5,100.00", "396.00", DeviceStatus.ON_SALE, False, None),
    ("P9002", "道闸 直杆 4m", "捷顺", "JSDZ-4", "台",
     DeviceCategory.PARKING, "5,600.00", "4,200.00", "396.00", DeviceStatus.ON_SALE, False, None),

    ("D1101", "55 寸液晶拼接屏 3.5mm", "三星", "LTI550HN", "台",
     DeviceCategory.INFO_PUBLISH, "9,200.00", "6,900.00", "396.00", DeviceStatus.ON_SALE, False, None),
    ("D1102", "信息发布一体机 43 寸", "国产", "XX43", "台",
     DeviceCategory.INFO_PUBLISH, "4,800.00", "3,600.00", "210.40", DeviceStatus.ON_SALE, False, None),
]


async def seed_labor_rates(session) -> int:
    n = 0
    for labor_class, eff, price in LABOR_RATES:
        exists = await session.scalar(
            select(LaborRate).where(
                LaborRate.region == "ningbo",
                LaborRate.labor_class == labor_class,
                LaborRate.effective_date == eff,
            )
        )
        if exists:
            continue
        session.add(
            LaborRate(
                region="ningbo",
                labor_class=labor_class,
                effective_date=eff,
                price_per_day=price,
                source="宁波市建设工程造价信息 2025-09",
                is_current=True,
                notes="用户确认采用期",
            )
        )
        n += 1
    await session.flush()
    return n


async def seed_quota(session) -> int:
    n = 0
    for code, cat, name, unit, a, b, c, base in QUOTA_ITEMS:
        if await session.scalar(select(QuotaItem).where(QuotaItem.code == code)):
            continue
        session.add(
            QuotaItem(
                code=code, category=cat, name=name, unit=unit,
                labor_a_day=Decimal(str(a)),
                labor_b_day=Decimal(str(b)),
                labor_c_day=Decimal(str(c)),
                base_price=Decimal(str(base)),
                notes="人工消耗量为结构示例，以纸质定额书为准",
            )
        )
        n += 1
    await session.flush()
    return n


async def seed_suppliers(session) -> dict[str, Supplier]:
    """按 code 定位供应商；**同名不同 code 的旧行必须复用**，不能硬插。

    为什么不能只按 code 查：`suppliers.name` 上也有唯一索引，而历史库里同一家
    供应商可能是用**旧编号方案**建出来的（实测 192.168.3.102 的库里是
    `SUP-56F42D7A` 这种，而当前种子里是 `SUP-001`）。此时"按 code 查不到 → 插入"
    会撞 `ix_suppliers_name` → 整个 seed 事务回滚 → `deploy.sh` 第 5 步失败。
    2026-09-30 在本机服务器上实测复现（部署在同一个库上再也跑不完）。

    复用而不是改编号：`code` 可能已经被 devices / subsystems 的外键引用，
    擅自改编号会把引用打断；编号与种子不一致只记一条 warning 说明。
    """
    out: dict[str, Supplier] = {}
    for code, name in SUPPLIERS:
        s = await session.scalar(select(Supplier).where(Supplier.code == code))
        if not s:
            s = await session.scalar(select(Supplier).where(Supplier.name == name))
            if s is not None:
                log.warning(
                    f"供应商「{name}」已存在但编号是 {s.code}（种子期望 {code}）—— 复用既有行"
                )
        if not s:
            s = Supplier(
                code=code,
                name=name,
                contact_name="待补充",
                contact_phone=None,
                contact_email=None,
                is_active=True,
            )
            session.add(s)
            await session.flush()
        out[name] = s
    return out


async def seed_device_categories(session) -> dict[str, DeviceCategoryNode]:
    """建/补齐设备库分类树，返回 {节点code: 节点}。

    · 一级节点：迁移 0004 已经建过（12 个），这里兜底补上缺的 —— 测试库和
      手工建的库没有跑迁移，不补的话设备就没有分类可挂。
    · 二级节点：来自 `core/device_tree.py` 的 SEED_CHILDREN，是**业务数据的初值**，
      使用方之后随便改（改名/加层/停用都行）。
    幂等：按 code 找，找不到才建。改了名字不会被覆盖回去。
    """
    nodes: dict[str, DeviceCategoryNode] = {
        n.code: n for n in (await session.scalars(select(DeviceCategoryNode))).all()
    }
    created = 0

    # 一级节点
    for i, member in enumerate(DeviceCategory):
        value = str(member)
        if value in nodes:
            continue
        node = DeviceCategoryNode(
            code=value,
            name=label_of("device_category", value),
            sort_order=i,
            is_active=True,
            legacy_enum=value,
            code_prefix=CODE_PREFIX_BY_CATEGORY.get(value),
        )
        session.add(node)
        await session.flush()
        nodes[value] = node
        created += 1

    # 二级节点
    for root_enum, children in SEED_CHILDREN.items():
        parent = nodes.get(root_enum)
        if parent is None:  # pragma: no cover - 上面刚补齐
            continue
        for order, (code, name) in enumerate(children):
            if code in nodes:
                continue
            node = DeviceCategoryNode(
                code=code,
                name=name,
                parent_id=parent.id,
                sort_order=order,
                is_active=True,
            )
            session.add(node)
            await session.flush()
            nodes[code] = node
            created += 1

    if created:
        log.info(f"设备分类树：新建 {created} 个节点（一级 + 二级）")
    return nodes


# 演示设备的二级分类：设备编号 → 分类树节点 code（见 core/device_tree.py 的 SEED_CHILDREN）。
# 刻意做成"编号 → 叶子"的显式映射，而不是按名字做关键词猜测：
# 演示库要一眼能核对"每台设备落在哪一格"，猜错一个关键词会让整棵树的示例失真。
DEVICE_LEAF: dict[str, str] = {
    # 视频监控
    "V1001": "video.camera", "V1002": "video.camera", "V1003": "video.camera",
    "V1007": "video.camera", "V1008": "video.camera", "V1009": "video.camera",
    "V1010": "video.camera", "V1011": "video.camera",
    "V1004": "video.storage", "V1005": "video.storage", "V1006": "video.storage",
    # 门禁一卡通
    "A2001": "access.controller", "A2002": "access.reader",
    "A2003": "access.lock", "A2004": "access.attendance",
    # 入侵报警
    "I3001": "intrusion.detector", "I3002": "intrusion.panel",
    # 综合布线
    "C4001": "cabling.copper", "C4002": "cabling.fiber",
    "C4003": "cabling.patch", "C4004": "cabling.tray", "C4005": "cabling.tray",
    # 计算机网络
    "N5001": "network.switch", "N5002": "network.switch",
    "N5003": "network.switch", "N5004": "network.wireless",
    # 公共广播
    "B6001": "broadcast.source", "B6002": "broadcast.speaker",
    # 会议系统
    "M7001": "conference.av", "M7002": "conference.mic",
    # 机房
    "S8001": "server_room.cabinet", "S8002": "server_room.ac",
    # 停车管理
    "P9001": "parking.pay", "P9002": "parking.gate",
    # 信息发布
    "D1101": "info_publish.screen", "D1102": "info_publish.terminal",
}


async def seed_devices(
    session,
    suppliers: dict[str, Supplier],
    categories: dict[str, DeviceCategoryNode] | None = None,
) -> int:
    supplier_names = list(suppliers.keys())
    created: dict[str, Device] = {}
    n = 0
    categories = categories or {}

    for i, row in enumerate(DEVICES):
        (code, name, brand, model, unit, cat, price, cost,
         install, st, owner_supplied, repl_code) = row

        d = await session.scalar(select(Device).where(Device.code == code))
        if d is None:
            d = Device(code=code)
            session.add(d)
            n += 1
        d.name = name
        d.brand = brand
        d.model = model
        d.unit = unit
        d.category = cat
        # 挂到分类树的二级节点上；DEVICE_LEAF 没覆盖到的设备退化为挂一级节点，
        # 与迁移回填的结果一致，不会出现"未分类"。
        leaf = categories.get(DEVICE_LEAF.get(code, ""))
        if leaf is None:
            leaf = categories.get(str(cat))
        d.category_id = leaf.id if leaf is not None else None
        d.reference_price = Decimal(price.replace(",", ""))
        d.cost_price = Decimal(cost.replace(",", ""))
        d.install_unit_price = Decimal(install.replace(",", ""))
        d.status = st
        d.is_owner_supplied = owner_supplied
        d.is_active = True
        d.is_common = i < 12
        d.supplier_id = suppliers[supplier_names[i % len(supplier_names)]].id
        d.moq = 1
        d.warranty_years = 2
        # 最后询价（原型第 2 页的「最近询价」列）：演示口径 —— 最近一次询价价 = 参考价，
        # 日期按设备序号在近 4 个询价日之间错开，模拟"不同型号询价节奏不同"。
        d.last_inquiry_at = datetime(2026, 9, 12, 9, 30, tzinfo=UTC) - timedelta(days=(i % 4) * 15)
        d.last_inquiry_price = Decimal(price.replace(",", ""))
        created[code] = d

    await session.flush()

    # 第二遍：解析替代型号引用
    for row in DEVICES:
        code, _, _, _, _, _, _, _, _, _, _, repl_code = row
        if repl_code and code in created and repl_code in created:
            created[code].replacement_id = created[repl_code].id
    await session.flush()
    return n


# 组织架构（演示用的规范树；生产环境由管理员在「用户管理」页面维护）。
# (code, name, parent_code, sort_order)
DEPARTMENTS = [
    ("dept", "XX科技", None, 0),
    ("dept.gm", "总经办", "dept", 0),
    ("dept.purchase", "采购部", "dept", 1),
    ("dept.design", "设计部", "dept", 2),
    ("dept.design.smart", "智能化设计组", "dept.design", 0),
    ("dept.design.elv", "弱电设计组", "dept.design", 1),
    ("dept.sales", "销售部", "dept", 3),
    ("dept.finance", "财务部", "dept", 4),
    ("dept.eng", "工程部", "dept", 5),
]

# 演示用户 → 部门（填在 users.department_id，自由文本的 department 同步）
USER_DEPARTMENT = {
    "admin": "dept.gm",
    "manager01": "dept.gm",
    "purchaser01": "dept.purchase",
    "est01": "dept.design.smart",
    "sales01": "dept.sales",
    "fin01": "dept.finance",
    "view01": "dept.eng",
}


async def seed_departments(session) -> dict[str, Department]:
    """建/补齐组织架构树（幂等：按 code 找，找到就跳过）。

    ⚠ 这里**不跟演示账号共用一个开关**（第一版写错了，已在 .102 上验证过后果）：
    组织架构是**结构**，与设备分类树同类 —— 设备分类树一直是无条件建的，
    部门树却被 `allow_demo` 拦掉，结果 production 模式部署出来是一棵空树，
    用户管理页面看上去像坏了。真正的敏感物是**账号凭据**（已知口令），
    那个仍然由 `allow_create` 控制。

    这是宁波XX科技自用的内部系统，所以默认给的就是本公司的一套部门；
    使用方之后在「用户管理」页面随便改（按 code 查找，改过的不覆盖）。
    """
    nodes: dict[str, Department] = {
        d.code: d for d in (await session.scalars(select(Department))).all()
    }
    created = 0
    for code, name, parent_code, order in DEPARTMENTS:
        if code in nodes:
            continue
        parent = nodes.get(parent_code) if parent_code else None
        node = Department(
            code=code,
            name=name,
            parent_id=parent.id if parent else None,
            sort_order=order,
            is_active=True,
        )
        session.add(node)
        await session.flush()
        nodes[code] = node
        created += 1
    if created:
        log.info(f"组织架构：新建 {created} 个部门节点")
    return nodes


async def seed_users(
    session, *, allow_create: bool = True, departments: dict[str, Department] | None = None
) -> int:
    """按用户名补齐演示账号，返回新建数量。

    以前是「库里只要**有一个人**就整体跳过」（`select(User).limit(1)`），
    于是任何一次手工建号都会让这 5 个演示账号永远建不出来；反过来，
    在生产库里跑一次 seed 就会凭空多出 5 个已知口令的账号。
    现在按用户名逐个补齐，并把「要不要建演示账号」交给调用方决定：
    production 下默认不建（要建得显式 --demo-users）。
    """
    demo = [
        ("admin", "admin@example.com", "admin12345", "系统管理员", Role.ADMIN, True),
        ("purchaser01", "purchaser01@example.com", "purchaser12345", "采购员", Role.PURCHASER, False),
        ("sales01", "sales01@example.com", "sales12345", "张销售", Role.SALES, False),
        ("est01", "est01@example.com", "est12345", "李设计", Role.ESTIMATOR, False),
        ("manager01", "manager01@example.com", "manager12345", "陈经理", Role.MANAGER, False),
        ("fin01", "fin01@example.com", "fin12345", "王财务", Role.FINANCE, False),
        ("view01", "view01@example.com", "view12345", "只读账号", Role.VIEWER, False),
    ]

    if not allow_create:
        log.warning(
            "production 环境默认不创建演示账号（已知口令，等同于弱口令后门）。"
            "确实需要时用：python -m scripts.seed --demo-users"
        )
        return 0

    created = 0
    for username, email, pwd, full_name, role, su in demo:
        existing = await session.scalar(select(User).where(User.username == username))
        if existing is not None:
            # 已存在的演示账号只做**部门补空**：管理员手工调整过的不覆盖。
            if departments and existing.department_id is None:
                code = USER_DEPARTMENT.get(username)
                node = departments.get(code) if code else None
                if node is not None:
                    existing.department_id = node.id
                    existing.department = node.name
            continue
        # 邮箱也可能已被占用（同名邮箱的另一个账号）→ 跳过并说清楚，
        # 否则整条 seed 会因为唯一键冲突整体失败（连带后面的业务数据都建不出来）。
        if await session.scalar(select(User).where(User.email == email)):
            log.warning(f"演示账号 {username} 未创建：邮箱 {email} 已被占用")
            continue
        code = USER_DEPARTMENT.get(username)
        node = departments.get(code) if (departments and code) else None
        session.add(
            User(
                username=username, email=email,
                hashed_password=hash_password(pwd),
                full_name=full_name, role=role,
                status=UserStatus.ACTIVE, is_superuser=su,
                department=node.name if node else None,
                department_id=node.id if node else None,
            )
        )
        created += 1
    await session.flush()
    return created


async def seed_demo_project(session) -> None:
    """Create 宁波XX大厦智能化工程 with 10 subsystems and a computed quote.

    Numbers mirror the UI prototype so the prototype and the API agree.
    """
    if await session.scalar(select(Project).where(Project.code == "PRJ-202609-0001")):
        return

    owner = await session.scalar(select(User).where(User.username == "sales01"))

    project = Project(
        code="PRJ-202609-0001",
        name="宁波XX大厦智能化弱电工程",
        customer_name="宁波XX置业有限公司",
        customer_contact="陈工",
        customer_phone="0574-8888XXXX",
        address="浙江省宁波市鄞州区XX路 88 号",
        project_type=ProjectType.COMMERCIAL,
        status=ProjectStatus.QUOTED,
        total_area_sqm=Decimal("38000.00"),
        owner_id=owner.id if owner else None,
    )
    session.add(project)
    await session.flush()

    sub_specs = [
        (SubsystemType.VIDEO_SURVEILLANCE, "视频监控", 0),
        (SubsystemType.ACCESS_CONTROL, "门禁一卡通", 1),
        (SubsystemType.INTRUSION, "入侵报警", 2),
        (SubsystemType.STRUCTURED_CABLING, "综合布线", 3),
        (SubsystemType.NETWORK, "计算机网络", 4),
        (SubsystemType.BROADCAST, "公共广播", 5),
        (SubsystemType.CONFERENCE, "会议系统", 6),
        (SubsystemType.SERVER_ROOM, "机房工程", 7),
        (SubsystemType.PARKING, "停车管理", 8),
        (SubsystemType.INFO_PUBLISH, "信息发布", 9),
    ]
    subs: dict[str, Subsystem] = {}
    for st, name, order in sub_specs:
        s = Subsystem(project_id=project.id, subsystem_type=st, custom_name=name, sort_order=order)
        session.add(s)
        subs[str(st)] = s
    await session.flush()

    quote = Quote(
        project_id=project.id,
        code="QT-202609-0001",
        version=1,
        title="宁波XX大厦智能化弱电工程报价单（第一版）",
        status=QuoteStatus.APPROVED,
        customer_name=project.customer_name,
        customer_contact=project.customer_contact,
        customer_phone=project.customer_phone,
        fee_base=FeeBaseType.DEVICE_AUX_INSTALL,
        auxiliary_base=AuxiliaryBaseType.DEVICE,
        auxiliary_rate=Decimal("0.06"),
        design_rate=Decimal("0.02"),
        warranty_rate=Decimal("0.03"),
        mgmt_profit_rate=Decimal("0.08"),
        tax_rate=Decimal("0.09"),
        labor_float_per_class={"class_a": 0, "class_b": 0, "class_c": 0},
    )
    session.add(quote)
    await session.flush()

    devices = {
        d.code: d
        for d in (await session.scalars(select(Device))).all()
    }

    # (设备编码, 子系统, 数量, 定额编号)
    item_specs = [
        ("V1001", SubsystemType.VIDEO_SURVEILLANCE, 86, "5-6-001"),
        ("V1002", SubsystemType.VIDEO_SURVEILLANCE, 42, "5-6-001"),
        ("V1004", SubsystemType.VIDEO_SURVEILLANCE, 3, "5-6-001"),
        ("V1006", SubsystemType.VIDEO_SURVEILLANCE, 24, None),
        ("A2001", SubsystemType.ACCESS_CONTROL, 8, "5-6-002"),
        ("A2002", SubsystemType.ACCESS_CONTROL, 16, "5-6-002"),
        ("A2003", SubsystemType.ACCESS_CONTROL, 12, "5-6-002"),
        ("I3001", SubsystemType.INTRUSION, 24, "5-6-003"),
        ("I3002", SubsystemType.INTRUSION, 2, "5-6-003"),
        ("C4001", SubsystemType.STRUCTURED_CABLING, 60, "5-1-001"),
        ("C4002", SubsystemType.STRUCTURED_CABLING, 1200, "5-1-002"),
        ("C4003", SubsystemType.STRUCTURED_CABLING, 240, "5-1-003"),
        ("C4004", SubsystemType.STRUCTURED_CABLING, 860, "5-7-001"),
        ("N5001", SubsystemType.NETWORK, 12, None),
        ("N5002", SubsystemType.NETWORK, 3, None),
        ("N5004", SubsystemType.NETWORK, 18, None),
        ("B6001", SubsystemType.BROADCAST, 2, None),
        ("B6002", SubsystemType.BROADCAST, 36, "5-5-001"),
        ("M7002", SubsystemType.CONFERENCE, 1, "5-5-002"),
        ("S8001", SubsystemType.SERVER_ROOM, 6, None),
        ("P9001", SubsystemType.PARKING, 2, "5-6-004"),
        ("P9002", SubsystemType.PARKING, 2, "5-6-004"),
        ("D1101", SubsystemType.INFO_PUBLISH, 4, None),
    ]

    for order, (code, sub_type, qty, quota_code) in enumerate(item_specs):
        d = devices.get(code)
        if not d:
            continue
        notes = f"quota:{quota_code}" if quota_code else None
        item = QuoteItem(
            quote_id=quote.id,
            subsystem_id=subs[str(sub_type)].id,
            device_id=d.id,
            name=d.name,
            brand=d.brand,
            model=d.model,
            unit=d.unit,
            quantity=Decimal(str(qty)),
            unit_price=d.reference_price,
            install_unit_price=d.install_unit_price,
            cost_price=d.cost_price,
            source=ItemSource.LIBRARY,
            is_owner_supplied=d.is_owner_supplied,
            sort_order=order,
            notes=notes,
        )
        session.add(item)

    await session.flush()

    # 重算（走真实业务逻辑）
    from sqlalchemy.orm import selectinload

    quote = await session.scalar(
        select(Quote).options(selectinload(Quote.items)).where(Quote.id == quote.id)
    )
    labor_rates = {
        r.labor_class: r
        for r in (
            await session.scalars(
                select(LaborRate).where(LaborRate.is_current.is_(True))
            )
        ).all()
    }
    # 明细行 → 定额编号，与 api/v1/quotes.py 的 _recompute 用同一份解析逻辑
    quota_map: dict[str, str] = {}
    for it in quote.items:
        item_quota_code = parse_quota_code(it.notes)
        if item_quota_code:
            quota_map[str(it.id)] = item_quota_code

    quota_items = {
        q.code: q
        for q in (
            await session.scalars(
                select(QuotaItem).where(QuotaItem.code.in_(list(quota_map.values())))
            )
        ).all()
    }

    b = compute_quote(
        items=list(quote.items),
        auxiliary_base=AuxiliaryBaseType.DEVICE,
        auxiliary_rate=Decimal("0.06"),
        fee_base=FeeBaseType.DEVICE_AUX_INSTALL,
        design_rate=Decimal("0.02"),
        warranty_rate=Decimal("0.03"),
        training_fee=Decimal("0"),
        mgmt_profit_rate=Decimal("0.08"),
        tax_rate=Decimal("0.09"),
        labor_float_per_class={"class_a": Decimal("0"), "class_b": Decimal("0"), "class_c": Decimal("0")},
        labor_rates=labor_rates,
        quota_items=quota_items,
        quote_items_quota_map=quota_map,
    )
    write_breakdown_to_quote(quote, b)

    # 子系统小计
    for s in subs.values():
        rows = [it for it in quote.items if it.subsystem_id == s.id]
        s.equipment_subtotal = sum((Decimal(str(r.equipment_amount)) for r in rows), Decimal("0"))
        s.install_subtotal = sum((Decimal(str(r.install_amount)) for r in rows), Decimal("0"))
        s.point_count = int(sum(Decimal(str(r.quantity)) for r in rows))

    await session.flush()
    log.info(
        f"演示报价单: 设备费 ¥{b.equipment_total} 安装费 ¥{b.install_total} "
        f"含税合计 ¥{b.total_with_tax} 定额口径 ¥{b.quota_total_with_tax}"
    )


async def seed_audit_logs_stub(session) -> None:
    """Placeholder for future audit seeds."""


async def reset_business_data(session) -> None:
    for model in (QuoteItem, Quote, Subsystem, Project, Device, QuotaItem, LaborRate, Supplier):
        await session.execute(delete(model))
    await session.flush()
    log.warning("业务数据已清空（用户保留）")


async def seed_point_templates(session) -> None:
    """点位模板（原型顶栏「点位模板」/ 报价页「套用点位模板」）。

    数值来自原型第 1 页的提示：「安装单价按【点位模板】自动带出
    （摄像机 80 元/点、球机 180 元/点、信息点 60 元/点…）」。
    每个模板指定一台**主设备** —— 安装单价（元/点）只计在主设备行上，
    否则一个点位里的支架/电源会各收一次安装费。
    """
    from app.models.point_template import PointTemplate, PointTemplateItem

    if await session.scalar(select(PointTemplate).limit(1)):
        return

    devices = {d.code: d for d in (await session.scalars(select(Device))).all()}
    existing = {t.code for t in (await session.scalars(select(PointTemplate))).all()}

    # (编码, 名称, 子系统, 安装单价元/点, [(设备编码, 数量, 是否主设备)])
    # 注意：演示设备库里视频专业只有 摄像机/NVR/硬盘，没有支架、电源适配器，
    # 所以演示模板比原型示意（球机 + 壁装支架 + 电源适配器）简单；
    # 真实使用时在模板里把支架/电源一起挂上即可。
    specs = [
        ("PT-DOME", "球机点位模板", SubsystemType.VIDEO_SURVEILLANCE, "180", [
            ("V1002", 1, True),
        ]),
        ("PT-BULLET", "枪机点位模板", SubsystemType.VIDEO_SURVEILLANCE, "80", [
            ("V1001", 1, True),
        ]),
        # 信息点：一个点位 = 1 个 RJ45 信息模块 + 约 15m 双绞线（0.05 箱 = 15.25m）
        ("PT-DATA", "信息点点位模板", SubsystemType.STRUCTURED_CABLING, "60", [
            ("C4003", 1, True), ("C4001", Decimal("0.05"), False),
        ]),
    ]
    made = 0
    for order, (code, name, sub_type, install, item_specs) in enumerate(specs):
        if code in existing:
            continue
        tpl = PointTemplate(
            code=code, name=name, subsystem_type=sub_type,
            install_unit_price=Decimal(install), sort_order=order,
        )
        session.add(tpl)
        await session.flush()
        for i, (dev_code, qty, primary) in enumerate(item_specs):
            d = devices.get(dev_code)
            if d is None:
                continue
            session.add(
                PointTemplateItem(
                    template_id=tpl.id, device_id=d.id, quantity=Decimal(str(qty)),
                    is_primary=primary, sort_order=i,
                )
            )
        made += 1
    await session.flush()
    if made:
        log.info("seeded {} point templates", made)


async def main(reset: bool = False, demo_users: bool = False) -> None:
    # 与 app.main 的 lifespan 同一道闸：production 下密钥不安全就别往下做，
    # 否则 deploy.sh 会在这一步"成功"，服务却在重启后拒绝启动。
    settings.validate_for_runtime()
    init_engine()
    try:
        async with session_scope() as session:
            if reset:
                await reset_business_data(session)

            # production 下默认不建演示账号：它们是已知口令（admin/admin12345 …），
            # 在生产库上跑一次 seed 就等于开了 5 个弱口令账号。
            # 注意：组织架构树**不**受这个开关影响（它是结构，不是凭据）。
            allow_demo = demo_users or not settings.is_production
            departments = await seed_departments(session)
            n_users = await seed_users(
                session, allow_create=allow_demo, departments=departments
            )
            n_rates = await seed_labor_rates(session)
            n_quota = await seed_quota(session)
            suppliers = await seed_suppliers(session)
            categories = await seed_device_categories(session)
            n_devices = await seed_devices(session, suppliers, categories)
            await seed_demo_project(session)
            await seed_point_templates(session)

            log.info(
                f"Seed 完成: 用户 +{n_users}, 人工信息价 +{n_rates}, "
                f"定额子目 +{n_quota}, 设备 +{n_devices}, "
                f"设备分类 {len(categories)} 个节点"
            )
    finally:
        await dispose_engine()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Seed the quote system database")
    parser.add_argument("--reset", action="store_true", help="清空业务数据后重建")
    parser.add_argument(
        "--demo-users",
        action="store_true",
        help="即使 APP_ENV=production 也创建 5 个已知口令的演示账号（不推荐）",
    )
    args = parser.parse_args()
    asyncio.run(main(reset=args.reset, demo_users=args.demo_users))




