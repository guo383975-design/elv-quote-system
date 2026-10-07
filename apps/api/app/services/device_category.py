"""设备库分类路径的解析与落树。

Excel/接口收到的"分类"可能是三种形态：

    「视频监控」                      → 一级分类名
    「video」                        → 一级分类代号（枚举值）
    「视频监控/摄像机」               → 多级路径（层级不限）

这个模块把三者统一解析成"根分类 + 一串节点名"，并告诉调用方哪些节点**树上还没有**
（由调用方决定是否自动建 —— 导入时建、接口查错时不建）。纯逻辑、不碰数据库，
所以能单独测。

为什么要把这件事收到一处：分类解析同时被「设备导入预览」「设备导入落库」
「设备表单保存」三条路径使用，各写一遍必然出现"预览说能识别、落库却落到别处"。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from uuid import UUID

# 路径分隔符：斜杠、反斜杠、竖线、大于号、顿号、全角斜杠
_PATH_SPLIT = re.compile(r"[/\\|>、／]+")

# 一级分类的模糊词表（原来写在 api/v1/imports.py 的 _guess_category 里，搬过来共用）
_CATEGORY_KEYWORDS: list[tuple[tuple[str, ...], str]] = [
    (("视频", "监控", "摄像", "camera", "video", "nvr", "录像"), "video"),
    (("门禁", "一卡通", "读卡", "闸机", "access"), "access"),
    (("报警", "入侵", "红外", "intrusion", "alarm"), "intrusion"),
    (("布线", "线缆", "光缆", "网线", "配线", "cabling", "桥架"), "cabling"),
    (("网络", "交换", "路由", "无线", "network", "switch"), "network"),
    (("广播", "扬声", "功放", "broadcast", "音源"), "broadcast"),
    (("会议", "发言", "话筒", "conference"), "conference"),
    (("机房", "机柜", "空调", "server_room", "dcim"), "server_room"),
    (("停车", "车位", "道闸", "parking"), "parking"),
    (("信息发布", "显示屏", "播控", "info_publish"), "info_publish"),
    (("电源", "电线", "电缆", "防雷", "power"), "power"),
    (("辅材", "五金", "耗材", "标签", "accessory"), "accessory"),
]


def split_path(raw: object) -> list[str]:
    """把「视频监控 / 摄像机」拆成 ['视频监控','摄像机']。空值返回 []。"""
    text = str(raw or "").strip()
    if not text:
        return []
    return [p.strip() for p in _PATH_SPLIT.split(text) if p.strip()]


def guess_root_enum(raw: str) -> str | None:
    """把一段文字猜成一级分类枚举值（模糊匹配，大小写不敏感）。"""
    text = str(raw or "").strip().lower()
    if not text:
        return None
    for keywords, enum_value in _CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in text:
                return enum_value
    return None


def auto_node_code(parent_code: str, name: str) -> str:
    """自动创建节点时的稳定代号：`<父代号>.c<内容哈希8位>`。

    用内容哈希（父路径 + 名字）而不是自增序号：同一条 Excel 反复导入时
    必须落到同一个节点上（幂等），自增序号做不到这点。
    """
    digest = hashlib.md5(f"{parent_code}/{name}".encode()).hexdigest()[:8]
    return f"{parent_code}.c{digest}"


@dataclass
class CategoryResolution:
    """一次分类解析的结果。"""

    raw: str
    parts: list[str] = field(default_factory=list)
    root_enum: str | None = None
    # 命中的节点（可能是根，也可能更深；完全没有则为 None）
    node_id: UUID | None = None
    node_path: str = ""
    # 树上还不存在的层级（按顺序；调用方决定建不建）
    missing: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.root_enum is not None

    @property
    def will_create(self) -> bool:
        return bool(self.missing)


def resolve_category(
    raw: object,
    *,
    nodes: dict[UUID, object],
    enum_to_root: dict[str, UUID],
) -> CategoryResolution:
    """解析分类文本。

    Args:
        nodes: {节点id: 节点对象}，节点需要 id / parent_id / name / code / legacy_enum。
        enum_to_root: {一级分类枚举值: 根节点id}

    三种结果：
      · 命中已有节点            → node_id 有值、missing 为空
      · 命中一级分类但树上有缺口  → node_id 是最深的已存在节点、missing 是待建的层级名
      · 树上完全没有这个一级分类  → root_enum 由模糊词表猜出、node_id 为 None、
                                   missing = [根名, *子名]（由调用方决定建不建）
    最后一种很重要：新库（或测试库）里一级节点可能还没建，
    但 `devices.category` 是 NOT NULL，必须仍然能解析出一个一级分类。
    """
    text = str(raw or "").strip()
    res = CategoryResolution(raw=text)
    res.parts = split_path(text)
    if not res.parts:
        res.error = "分类为空"
        return res

    first = res.parts[0]
    rest = res.parts[1:]

    # 1) 一级分类命中（枚举代号 / 根节点名 / 根节点 code / 模糊词表）
    root_id = _match_root(first, nodes=nodes, enum_to_root=enum_to_root)
    if root_id is not None:
        res.root_enum = str(getattr(nodes[root_id], "legacy_enum", "") or "")
        res.node_id = root_id
        res.node_path = _path_of(root_id, nodes)
    else:
        # 2) 不是一级分类，但可能是树上某个**子节点**的名字（用户只写了小类）
        any_node = _match_any_node(first, nodes=nodes)
        if any_node is not None:
            res.node_id = any_node
            res.node_path = _path_of(any_node, nodes)
            res.root_enum = _root_enum_of(any_node, nodes)
            rest = rest  # 命中的子节点之后仍然可以继续往下
        else:
            # 3) 树上没有：猜一个一级分类，记下需要新建的层级
            guessed = guess_root_enum(first)
            if guessed is None:
                res.error = f"分类「{first}」无法识别，请检查是否属于已有的一级分类"
                return res
            res.root_enum = guessed
            res.node_id = None
            res.node_path = ""
            res.missing = [first, *rest]
            return res

    current = res.node_id
    for part in rest:
        child = _match_child(part, current, nodes=nodes)
        if child is None:
            idx = rest.index(part)
            res.missing = rest[idx:]
            break
        current = child
        res.node_id = child
        res.node_path = _path_of(child, nodes)

    return res


def _match_root(
    token: str, *, nodes: dict[UUID, object], enum_to_root: dict[str, UUID]
) -> UUID | None:
    """一级分类匹配：枚举代号 → 节点名 → 节点 code → 模糊词表。"""
    low = token.strip().lower()
    if low in enum_to_root:
        return enum_to_root[low]
    for nid, node in nodes.items():
        if getattr(node, "parent_id", None) is None and low in (
            str(getattr(node, "name", "")).strip().lower(),
            str(getattr(node, "code", "")).strip().lower(),
        ):
            return nid
    guessed = guess_root_enum(token)
    if guessed and guessed in enum_to_root:
        return enum_to_root[guessed]
    return None


def _match_any_node(token: str, *, nodes: dict[UUID, object]) -> UUID | None:
    """在**整棵树**里按名字/code 找节点；只有唯一命中才算数（避免歧义乱挂）。"""
    low = token.strip().lower()
    hits = [
        nid
        for nid, node in nodes.items()
        if low
        in (
            str(getattr(node, "name", "")).strip().lower(),
            str(getattr(node, "code", "")).strip().lower(),
        )
    ]
    return hits[0] if len(hits) == 1 else None


def _root_enum_of(node_id: UUID, nodes: dict[UUID, object]) -> str | None:
    seen: set[UUID] = set()
    cur = nodes.get(node_id)
    while cur is not None and getattr(cur, "id", None) not in seen:
        seen.add(cur.id)
        legacy = getattr(cur, "legacy_enum", None)
        if legacy:
            return str(legacy)
        pid = getattr(cur, "parent_id", None)
        cur = nodes.get(pid) if pid else None
    return None


def _match_child(token: str, parent_id: UUID, *, nodes: dict[UUID, object]) -> UUID | None:
    low = token.strip().lower()
    for nid, node in nodes.items():
        if getattr(node, "parent_id", None) != parent_id:
            continue
        if low in (
            str(getattr(node, "name", "")).strip().lower(),
            str(getattr(node, "code", "")).strip().lower(),
        ):
            return nid
    return None


def _path_of(node_id: UUID, nodes: dict[UUID, object]) -> str:
    parts: list[str] = []
    seen: set[UUID] = set()
    cur = nodes.get(node_id)
    while cur is not None and getattr(cur, "id", None) not in seen:
        seen.add(cur.id)
        parts.append(str(getattr(cur, "name", "")))
        pid = getattr(cur, "parent_id", None)
        cur = nodes.get(pid) if pid else None
    return " / ".join(reversed(parts))


__all__ = [
    "CategoryResolution",
    "auto_node_code",
    "guess_root_enum",
    "resolve_category",
    "split_path",
]
