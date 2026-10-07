"""设备库分类树的**唯一数据源**（一级节点的编号前缀 + 随种子交付的二级分类）。

为什么单独一个模块：
  · 设备自动编号的前缀（V/A/I/…）原来写死在 `api/v1/devices.py` 里，而分类树
    把"分类"变成了数据，前缀就该跟着分类走（节点上的 `code_prefix`，
    子节点留空表示继承父节点）。这份映射既给迁移用（建一级节点），也给
    运行时用（没挂节点的设备按一级分类兜底），所以必须是同一份。
  · 二级分类是**业务数据**（使用方自己维护），这里只是"随种子交付的一套合理初值"：
    它让第一次打开设备库就不是"都写在一起"，之后随便改。
"""

from __future__ import annotations

# 一级分类（= DeviceCategory 的取值）→ 设备编号前缀。
# 前缀参与 `_next_device_code()` 的编号生成（V1001 / A2001 / …），改了会影响新编号。
CODE_PREFIX_BY_CATEGORY: dict[str, str] = {
    "video": "V",
    "access": "A",
    "intrusion": "I",
    "cabling": "C",
    "network": "N",
    "broadcast": "B",
    "conference": "M",
    "server_room": "S",
    "parking": "P",
    "info_publish": "D",
    "power": "W",
    "accessory": "X",
}

# 随种子交付的二级分类：一级分类取值 → [(节点 code, 中文名), …]
# code 用 `<一级>.<英文短名>`，保证全局唯一且稳定（改名不动 code）。
SEED_CHILDREN: dict[str, list[tuple[str, str]]] = {
    "video": [
        ("video.camera", "摄像机"),
        ("video.storage", "录像与存储"),
        ("video.display", "显示设备"),
        ("video.transport", "传输设备"),
    ],
    "access": [
        ("access.reader", "读卡器与识别"),
        ("access.controller", "门禁控制器"),
        ("access.lock", "电锁与开门按钮"),
        ("access.attendance", "消费与考勤"),
    ],
    "intrusion": [
        ("intrusion.detector", "探测器"),
        ("intrusion.panel", "报警主机"),
        ("intrusion.siren", "声光与紧急按钮"),
    ],
    "cabling": [
        ("cabling.copper", "铜缆"),
        ("cabling.fiber", "光缆与光模块"),
        ("cabling.patch", "配线架与跳线"),
        ("cabling.tray", "管槽与桥架"),
    ],
    "network": [
        ("network.switch", "交换机"),
        ("network.router", "路由与安全"),
        ("network.wireless", "无线网络"),
        ("network.manage", "网络管理"),
    ],
    "broadcast": [
        ("broadcast.source", "音源与功放"),
        ("broadcast.speaker", "扬声器"),
        ("broadcast.peripheral", "广播周边"),
    ],
    "conference": [
        ("conference.mic", "会议发言"),
        ("conference.av", "扩声与显示"),
        ("conference.control", "中控与矩阵"),
    ],
    "server_room": [
        ("server_room.cabinet", "机柜与配电"),
        ("server_room.ac", "精密空调"),
        ("server_room.dcim", "动力环境监控"),
        ("server_room.grounding", "防雷接地"),
    ],
    "parking": [
        ("parking.gate", "出入口控制"),
        ("parking.guide", "车位引导"),
        ("parking.pay", "收费与识别"),
    ],
    "info_publish": [
        ("info_publish.screen", "显示屏"),
        ("info_publish.terminal", "发布终端"),
        ("info_publish.software", "播控软件"),
    ],
    "power": [
        ("power.supply", "电源设备"),
        ("power.cable", "电线电缆"),
        ("power.protection", "防雷与接地"),
    ],
    "accessory": [
        ("accessory.install", "安装辅材"),
        ("accessory.hardware", "五金件"),
        ("accessory.label", "标签与耗材"),
    ],
}


__all__ = ["CODE_PREFIX_BY_CATEGORY", "SEED_CHILDREN"]
