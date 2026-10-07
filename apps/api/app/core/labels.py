"""中文标签映射的唯一来源。

以前这份映射在三个地方各有一份：`main.py` 的 /meta/labels、`quotes.py` 的
子系统汇总、以及导出模板。改一处漏一处就会出现「同一个枚举在两个页面显示成
不同中文」或「导出的 Excel 里是英文枚举值」。

这里收成一份，其它地方一律 `from app.core.labels import LABELS, label_of`。
枚举的**取值**（小写，和 PG 枚举一致）是 key，中文是 value。
"""

from __future__ import annotations

LABELS: dict[str, dict[str, str]] = {
    "subsystem_type": {
        "video_surveillance": "视频监控",
        "access_control": "门禁一卡通",
        "intrusion": "入侵报警",
        "structured_cabling": "综合布线",
        "network": "计算机网络",
        "broadcast": "公共广播",
        "conference": "会议系统",
        "server_room": "机房工程",
        "parking": "停车管理",
        "info_publish": "信息发布",
        "other": "其他",
    },
    "device_status": {
        "on_sale": "在售",
        "near_eol": "临近停产",
        "eol": "已停产",
        "discontinued": "退市",
    },
    "device_category": {
        "video": "视频监控",
        "access": "门禁一卡通",
        "intrusion": "入侵报警",
        "cabling": "综合布线",
        "network": "计算机网络",
        "broadcast": "公共广播",
        "conference": "会议系统",
        "server_room": "机房",
        "parking": "停车管理",
        "info_publish": "信息发布",
        "power": "电源线缆",
        "accessory": "辅材",
    },
    "role": {
        "admin": "系统管理员",
        "purchaser": "采购",
        "sales": "销售",
        "estimator": "设计",
        "manager": "经理",
        "finance": "财务",
        "viewer": "只读",
    },
    "fee_base": {
        "device_only": "仅设备费",
        "device_aux_install": "设备+辅材+安装费",
        "direct_fee": "直接费",
    },
    "auxiliary_base": {
        "device": "设备费",
        "device_install": "设备费+安装费",
        "by_quantity": "按工程量",
    },
    "project_type": {
        "commercial": "商业综合体",
        "public": "公建（政府/医院/学校）",
        "residential": "住宅小区",
        "data_center": "数据中心",
        "other": "其他",
    },
    "project_status": {
        "draft": "草稿",
        "quoting": "报价中",
        "quoted": "已报价",
        "won": "已中标",
        "lost": "已失单",
        "archived": "已归档",
    },
    "quote_status": {
        "draft": "草稿",
        "reviewing": "审核中",
        "submitted": "已提交",
        "approved": "已批准",
        "rejected": "已驳回",
        "archived": "已归档",
    },
    "item_source": {
        "library": "设备库",
        "manual": "手工录入",
        "imported": "清单导入",
    },
    "labor_class": {
        "class_a": "一类工日",
        "class_b": "二类工日",
        "class_c": "三类工日",
    },
    # 第五册章节名按 2018 版定额真值校正（2026-10-06）：
    # 以前 5-1 记作「综合布线」、5-7 记作「管槽」，与定额书不符；
    # 综合布线与管槽实际都在 5-2，5-7 是电涌保护，且缺 5-2/5-4/5-8。
    "quota_category": {
        "5-1": "计算机、网络设备",
        "5-2": "综合布线",
        "5-3": "楼宇自控",
        "5-4": "广播与有线电视",
        "5-5": "音视频与会议",
        "5-6": "安全防范",
        "5-7": "电涌保护",
        "5-8": "家居智能化",
        "5-9": "时钟系统",       # 2025 版新增章节（母钟/子钟/GPS 接收单元）
        "other": "其他",
    },
    "user_status": {
        "active": "正常",
        "disabled": "已停用",
        "pending": "待激活",
    },
    # tax_method 原来只有前端 QuoteTotalPanel.vue 里有一份，
    # 导出要用，所以上收到后端；标签与前端保持**逐字一致**，避免同一个
    # 计税方式在页面上和导出的 PDF 里写成两种样子。
    # （前端后续可以直接改读 /meta/labels，省掉这份重复。）
    "tax_method": {
        "general_9pct": "一般计税 9%",
        "simple_3pct": "简易计税 3%",
        "general": "一般计税",
        "simple": "简易计税",
    },
}


def label_of(group: str, value: object, *, default: str | None = None) -> str:
    """取中文标签；没有映射就退回原值本身（**不要返回空串**）。

    导出到 Excel/PDF 时这一点尤其重要：宁可显示一个英文枚举值让人一眼看出
    「这里漏了映射」，也不要留空导致甲方拿到一张有空白格子的报价单。
    """
    if value is None:
        return default or ""
    key = str(value)
    return LABELS.get(group, {}).get(key, default if default is not None else key)


__all__ = ["LABELS", "label_of"]
