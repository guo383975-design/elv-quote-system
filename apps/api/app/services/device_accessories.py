"""配件/授权 的判定与规范化规则（唯一实现处）。

导入（api/v1/imports.py）、配件挂靠、以及后续任何"按型号认配件"的地方都从这里取，
避免同一套规则散落在多处又各写一遍（这几轮分类错、名称丢年限就是这么来的）。
"""

from __future__ import annotations

import re

# 授权型号：LIC-<主机型号>-<类型>-<年限>
LICENSE_RE = re.compile(r"^(LIC-.+?)-(ATP|TP|HQDR|IPS|AV|URL|SDEF|SEC|HIPS)-(\d+)Y$", re.I)

TYPE_CN = {
    "ATP": "高级威胁防护",
    "TP": "威胁防护",
    "HQDR": "乾坤边界防护联动",
    "IPS": "入侵防御",
    "AV": "反病毒",
    "URL": "URL过滤",
    "SDEF": "安全防御",
    "SEC": "安全服务",
    "HIPS": "主机入侵防御",
}

# 授权名里带的"关键词"（型号看不出是授权时，靠名称兜底）
_LICENSE_WORDS = ("功能包", "保卡", "授权", "许可", "订阅")


def is_license_model(model: str, name: str = "") -> bool:
    """是不是授权（软件许可/服务保卡）。

    ⚠ 不看源表的「产品类型」列 —— 那列会把 `LIC-*` 标成"软件"（真实踩坑）。
    """
    if (model or "").upper().startswith("LIC-"):
        return True
    text = f"{model or ''}{name or ''}"
    return any(w in text for w in _LICENSE_WORDS)


def license_display_name(model: str) -> str | None:
    """授权型号 → 「类型 N年（型号）」；不是授权就返回 None。

    为什么要重新生成而不是用源描述：TP 类的年限写在括号**后面**，
    按"括号前的文字"取名会把年限丢掉（真实踩坑）。
    """
    m = LICENSE_RE.match((model or "").strip())
    if not m:
        return None
    typ = TYPE_CN.get(m.group(2).upper(), m.group(2).upper())
    return f"{typ} {m.group(3)}年（{model.strip()}）"


def host_model_of(license_model: str) -> str | None:
    """从授权型号推主机型号：LIC-USG6000F-S112-ATP-3Y → USG6000F-S112。"""
    m = LICENSE_RE.match((license_model or "").strip())
    if m:
        return m.group(1)[len("LIC-"):] or None
    b = re.sub(r"^LIC-", "", (license_model or "").strip())
    b = re.sub(r"-(ATP|TP|HQDR|IPS|AV|URL|SDEF|SEC|HIPS)[-_].*$", "", b, flags=re.I)
    b = re.sub(r"-[0-9]+Y$", "", b, flags=re.I)
    return b.strip("-") or None


def fix_category_leaf(path: str, *, is_license: bool) -> tuple[str, str | None]:
    """把分类路径的叶子在「主机 ↔ 授权」之间纠正。

    返回 (纠正后的路径, 说明或 None)。只改叶子里的"主机/授权"字样，
    不动系列层级 —— 例如
      「网络设备/华为安全设备/USG6000F系列/USG6000F主机」+ 授权 → …/USG6000F授权
    """
    p = (path or "").strip()
    if not p:
        return p, None
    if is_license and "主机" in p:
        return p.replace("主机", "授权"), "该行按型号判定为**授权**，分类已纠正到「授权」节点"
    if not is_license and "授权" in p:
        return p.replace("授权", "主机"), "该行按型号判定为**主机**，分类已纠正到「主机」节点"
    return p, None


def match_host_by_name(license_model: str, license_name: str, hosts) -> object | None:
    """按**名称包含**找主机：授权的型号或名称里出现主机型号即命中。

    为什么需要：不是所有厂商的授权编号都带主机型号（华为是 `LIC-<主机>-ATP-3Y` ✓，
    锐捷是 `RG-WALL 1600-NBW7000-01-S-IA…` —— 主机型号只是它的一段前缀 ✗）。
    用户 2026-10-05 明确的口径就是"按授权名称绑定到同名称主机名下" ✓。

    ⚠ 主机型号之间可能是前缀关系（`…-01-S` 是 `…-01-S-IA` 的前缀），
    因此按型号长度**降序**匹配，取最长的那个 —— 否则会挂到错误的主机上 ✗。
    """
    text = f"{license_model or ''} {license_name or ''}"
    if not text.strip():
        return None
    for h in sorted(hosts, key=lambda x: len(str(getattr(x, "model", "") or "")),
                    reverse=True):
        hm = str(getattr(h, "model", "") or "").strip()
        if len(hm) >= 6 and hm in text:
            return h
    return None


def pick_default_license(models: list[str], prefer: str = "ATP-3Y") -> str | None:
    """从一批授权型号里挑默认项：优先 `prefer`（ATP-3Y），否则第一个。"""
    for m in models:
        if prefer.upper() in (m or "").upper():
            return m
    return models[0] if models else None
