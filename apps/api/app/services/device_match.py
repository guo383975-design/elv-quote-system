"""Device matching: 精确匹配 → 模糊匹配（名称分词 + 型号特征 + 品牌）.

Scoring (total 100):
    名称相似度  40%
    型号特征    40%
    品牌        20%

Matching tiers:
    score == 100 且 型号完全一致   → exact
    score >= threshold            → fuzzy
    否则                          → none
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

# ---------------------------------------------------------------------------
# 型号特征提取
# ---------------------------------------------------------------------------

# 常见型号结构：字母前缀 + 数字 + 后缀（DS-2CD2T46WD-I5 / S1730S-L24T-A / IPC2A4L-IR5）
_MODEL_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+")

# 单位/修饰词，不参与名称匹配（否则"网络高清枪型摄像机"和"网络摄像机"会被"机"干扰）
_STOPWORDS = {
    "型", "式", "款", "个", "只", "台", "套", "块", "条", "米", "箱", "对", "根",
    "网络", "高清", "数字", "智能", "标准", "通用", "工业级", "商用", "专业",
}


def _tokens(text: str | None) -> list[str]:
    """Split a Chinese/English product name into comparable tokens.

    Chinese has no spaces, so we treat each CJK character as a token and
    Latin/digit runs as their own tokens. 2-gram folding is applied so that
    "摄像机" and "摄像" still overlap partially.
    """
    if not text:
        return []
    text = text.strip().lower()
    raw: list[str] = []
    for chunk in re.findall(r"[\u4e00-\u9fff]+|[a-z0-9]+", text):
        if re.match(r"[\u4e00-\u9fff]", chunk):
            raw.extend(list(chunk))
        else:
            raw.append(chunk)
    return [t for t in raw if t and t not in _STOPWORDS]


def _name_similarity(a: str | None, b: str | None) -> float:
    """Token-overlap (Jaccard-ish) blended with SequenceMatcher. Range 0..1."""
    ta, tb = set(_tokens(a)), set(_tokens(b))
    if not ta or not tb:
        return 0.0
    inter = len(ta & tb)
    union = len(ta | tb)
    jaccard = inter / union if union else 0.0
    # 字符级相似度能救回分词不重合但整体接近的情况
    seq = SequenceMatcher(None, (a or "").lower(), (b or "").lower()).ratio()
    return 0.7 * jaccard + 0.3 * seq


def model_features(model: str | None) -> list[str]:
    """Normalized feature tokens of a model string (uppercased)."""
    if not model:
        return []
    return [t.upper() for t in _MODEL_TOKEN_RE.findall(model.upper())]


def _model_similarity(a: str | None, b: str | None) -> float:
    """Model similarity: exact=1.0; else weighted feature overlap.

    Numeric tokens weigh more than alphabetic ones because a model's
    distinguishing part is almost always the number block (3441 vs 3441M).
    """
    if not a or not b:
        return 0.0
    na, nb = a.strip().upper(), b.strip().upper()
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.92

    fa, fb = model_features(na), model_features(nb)
    if not fa or not fb:
        return 0.0

    sa, sb = set(fa), set(fb)
    inter = sa & sb
    if not inter:
        return 0.0

    def weight(tok: str) -> float:
        return 2.0 if tok.isdigit() else 1.0

    inter_w = sum(weight(t) for t in inter)
    union_w = sum(weight(t) for t in (sa | sb))
    base = inter_w / union_w if union_w else 0.0

    # 型号前缀族（DS-2CD / DH-IPC）一致 → 小幅加成
    pref_a = (fa[0] if fa else "")[:4]
    pref_b = (fb[0] if fb else "")[:4]
    if pref_a and pref_a == pref_b:
        base = min(1.0, base + 0.08)
    return base


def _brand_similarity(a: str | None, b: str | None) -> float:
    """Brands are short; require an exact (case-insensitive) hit for full credit."""
    if not a or not b:
        return 0.0
    na, nb = a.strip(), b.strip()
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        return 0.7
    return SequenceMatcher(None, na, nb).ratio()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class MatchCandidate:
    device: Any
    score: float
    name_score: float
    model_score: float
    brand_score: float

    def to_dict(self) -> dict:
        d = {
            "device_id": str(self.device.id),
            "code": self.device.code,
            "name": self.device.name,
            "brand": self.device.brand,
            "model": self.device.model,
            "unit": self.device.unit,
            "reference_price": float(self.device.reference_price or 0),
            "install_unit_price": float(self.device.install_unit_price or 0),
            "status": str(self.device.status),
            "score": round(self.score, 2),
            "score_detail": {
                "name": round(self.name_score * 100, 2),
                "model": round(self.model_score * 100, 2),
                "brand": round(self.brand_score * 100, 2),
            },
        }
        repl = getattr(self.device, "replacement_id", None)
        if str(self.device.status) in ("eol", "near_eol"):
            d["is_discontinued"] = True
            d["replacement_id"] = str(repl) if repl else None
        return d


WEIGHT_NAME = 0.40
WEIGHT_MODEL = 0.40
WEIGHT_BRAND = 0.20


def score_pair(
    *,
    query_name: str | None,
    query_model: str | None,
    query_brand: str | None,
    device: Any,
) -> MatchCandidate:
    """Score a single device against the query fields.

    权重口径（与设计文档一致）：
        给了型号   → 名称 40% + 型号 40% + 品牌 20%
        没给型号   → 名称 80% + 品牌 20%（型号的权重让给名称）
    然后**只在清单真正提供的维度之间归一化**。

    归一化这一步是必须的：以前缺失的维度照样占着权重、按 0 分计入，
    于是「只有名称」的行（甲方清单最常见的形式）总分上限只有 0.8，
    要过 75 分阈值就得名称相似度 ≥ 0.9375 —— 几乎要求逐字相同，
    结果全是 match_type="none"，与「无型号时把权重让给名称」的设计意图正好相反。
    """
    name_s = _name_similarity(query_name, getattr(device, "name", None))
    model_s = _model_similarity(query_model, getattr(device, "model", None))
    brand_s = _brand_similarity(query_brand, getattr(device, "brand", None))

    base_name = WEIGHT_NAME + (0.0 if query_model else WEIGHT_MODEL)
    parts: list[tuple[float, float]] = []
    if query_name:
        parts.append((base_name, name_s))
    if query_model:
        parts.append((WEIGHT_MODEL, model_s))
    if query_brand:
        parts.append((WEIGHT_BRAND, brand_s))

    if not parts:
        total = 0.0
    else:
        wsum = sum(w for w, _ in parts)
        total = sum(w * s for w, s in parts) / wsum if wsum else 0.0

    return MatchCandidate(
        device=device,
        score=total * 100,
        name_score=name_s,
        model_score=model_s,
        brand_score=brand_s,
    )


def _below_threshold(score: float, threshold: float) -> str:
    """候选分不够「精确」时，决定算模糊命中还是完全没命中。"""
    return "fuzzy" if score >= threshold else "none"


def match_device(
    *,
    query_name: str,
    # candidates 无默认值，必须排在所有带默认值的参数前面。
    # 这是关键字-only 函数，调整顺序对调用方没有任何影响。
    candidates: Sequence[Any],
    query_model: str | None = None,
    query_brand: str | None = None,
    threshold: float = 75.0,
    limit: int = 5,
) -> dict:
    """Match one query row against the device library.

    Returns:
        {
          "query": {...},
          "match_type": "exact" | "fuzzy" | "none",
          "best": {...} | None,
          "candidates": [...],           # top-N above 0, sorted desc
          "discontinued_count": int,      # how many candidates are EOL
        }
    """
    scored: list[MatchCandidate] = [
        score_pair(
            query_name=query_name,
            query_model=query_model,
            query_brand=query_brand,
            device=d,
        )
        for d in candidates
    ]
    scored = [c for c in scored if c.score > 0]
    scored.sort(key=lambda c: c.score, reverse=True)

    best = scored[0] if scored else None

    # exact 判定：型号完全一致（若清单给了型号），否则名称 token 完全一致且≥95分
    match_type = "none"
    if best is not None:
        if query_model:
            same_model = (best.device.model or "").strip().upper() == query_model.strip().upper()
            match_type = "exact" if same_model else _below_threshold(best.score, threshold)
        else:
            match_type = "exact" if best.score >= 95 else _below_threshold(best.score, threshold)

    # 判成 exact 就给满分。
    # 型号是产品的唯一标识，型号逐字相同就是同一款货；目录里的名称写法常带后缀差异
    # （"网络高清枪型摄像机" vs "网络高清枪型摄像机 400万"），字面相似度只有 0.81。
    # 不改的话界面会出现「精确匹配 · 91.79 分」这种自相矛盾的展示。
    # score_detail 保留各维度原始相似度，方便看清满分是怎么来的。
    if match_type == "exact" and best is not None:
        best.score = 100.0

    top = scored[:limit]
    discontinued = sum(
        1 for c in top if str(getattr(c.device, "status", "")) in ("eol", "near_eol")
    )

    return {
        "query": {"name": query_name, "model": query_model, "brand": query_brand},
        "match_type": match_type,
        "best": best.to_dict() if best else None,
        "candidates": [c.to_dict() for c in top],
        "discontinued_count": discontinued,
        "threshold": threshold,
    }


def suggest_replacements(
    device: Any,
    peers: Iterable[Any],
    *,
    limit: int = 5,
) -> list[dict]:
    """Rank in-sale peers as replacement suggestions for an EOL device."""
    out: list[MatchCandidate] = []
    base_price = float(getattr(device, "reference_price", 0) or 0)
    self_id = getattr(device, "id", None)
    self_code = getattr(device, "code", None)

    for p in peers:
        # 必须把自己排除掉：调用方常常直接传整个设备库（含这台停产设备自己），
        # 它和自己比是 100 分，于是「替代型号」的第一条就是它本人 —— 毫无意义，
        # 而且会把真正可用的替代品挤出推荐位。
        # 按 id 和 code 双重判断：id 是首选，但调用方可能传进来的是
        # 另一个 session/另一份对象（id 不同却是同一行数据），code 在库里是唯一键，
        # 用它兜底更稳。
        if p is device:
            continue
        if self_id is not None and getattr(p, "id", None) == self_id:
            continue
        if self_code and getattr(p, "code", None) == self_code:
            continue

        c = score_pair(
            query_name=getattr(device, "name", None),
            query_model=getattr(device, "model", None),
            query_brand=getattr(device, "brand", None),
            device=p,
        )
        out.append(c)

    out.sort(key=lambda c: c.score, reverse=True)

    result: list[dict] = []
    for c in out[:limit]:
        d = c.to_dict()
        d["price_delta"] = round(float(d["reference_price"]) - base_price, 2)
        result.append(d)
    return result


__all__ = [
    "match_device",
    "suggest_replacements",
    "score_pair",
    "model_features",
]
