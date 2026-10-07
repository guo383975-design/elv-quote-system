"""设备匹配（精确 / 模糊 / 停产替代）测试 — 场景二核心算法."""

from __future__ import annotations

from decimal import Decimal
from uuid import uuid4

import pytest

from app.models.device import Device, DeviceCategory, DeviceStatus
from app.services.device_match import (
    match_device,
    model_features,
    score_pair,
    suggest_replacements,
)


def dev(
    code: str,
    name: str,
    brand: str | None = None,
    model: str | None = None,
    status: DeviceStatus = DeviceStatus.ON_SALE,
    price: str = "100.00",
    category: DeviceCategory = DeviceCategory.VIDEO,
    replacement_id=None,
) -> Device:
    d = Device(
        code=code,
        name=name,
        brand=brand,
        model=model,
        unit="台",
        category=category,
        reference_price=Decimal(price),
        cost_price=Decimal(price) * Decimal("0.75"),
        install_unit_price=Decimal("112.60"),
        status=status,
        replacement_id=replacement_id,
    )
    d.id = uuid4()
    return d


LIBRARY = [
    dev("V1001", "网络高清枪型摄像机 400万", "海康威视", "DS-2CD2T46WD-I5", price="680.00"),
    dev("V1002", "网络高清半球摄像机 400万", "海康威视", "DS-2CD2145FWD-I", price="720.00"),
    dev("V1008", "网络高清枪型摄像机 400万", "大华", "DH-IPC-HFW3441M",
        status=DeviceStatus.EOL, price="645.00"),
    dev("V1010", "网络高清筒机 400万 星光级", "海康威视", "DS-2CD3T46WD-I3",
        status=DeviceStatus.EOL, price="698.00"),
    dev("N5001", "24 口千兆交换机", "华为", "S1730S-L24T-A",
        category=DeviceCategory.NETWORK, price="1380.00"),
]


# ---------------------------------------------------------------------------
# 型号特征提取
# ---------------------------------------------------------------------------

def test_model_features_splits_alpha_and_digit_runs():
    assert model_features("DS-2CD2T46WD-I5") == [
        "DS", "2", "CD", "2", "T", "46", "WD", "I", "5",
    ]


def test_model_features_uppercases():
    assert model_features("s1730s-l24t-a") == ["S", "1730", "S", "L", "24", "T", "A"]


def test_model_features_empty():
    assert model_features(None) == []
    assert model_features("") == []


# ---------------------------------------------------------------------------
# 1. 精确匹配
# ---------------------------------------------------------------------------

def test_exact_match_on_identical_model():
    r = match_device(
        query_name="网络高清枪型摄像机",
        query_model="DS-2CD2T46WD-I5",
        query_brand="海康威视",
        candidates=LIBRARY,
    )
    assert r["match_type"] == "exact"
    assert r["best"]["code"] == "V1001"
    assert r["best"]["score"] == 100.0


def test_exact_match_is_case_insensitive():
    r = match_device(
        query_name="网络高清枪型摄像机",
        query_model="ds-2cd2t46wd-i5",
        query_brand=None,
        candidates=LIBRARY,
    )
    assert r["match_type"] == "exact"
    assert r["best"]["code"] == "V1001"


# ---------------------------------------------------------------------------
# 2. 模糊匹配
# ---------------------------------------------------------------------------

def test_fuzzy_match_when_model_absent_uses_name():
    """甲方清单常只有名称 → 名称权重提升到 80%，应匹到同名类设备。"""
    r = match_device(
        query_name="网络高清枪型摄像机 400万",
        query_model=None,
        query_brand=None,
        candidates=LIBRARY,
    )
    assert r["match_type"] in ("fuzzy", "exact")
    assert r["best"]["code"] in ("V1001", "V1008")
    assert r["best"]["score"] > 75


def test_fuzzy_match_partial_model_number():
    """型号数字块接近（3441 vs 3449）应给出较高分但不必 exact。"""
    r = match_device(
        query_name="网络高清枪型摄像机",
        query_model="DH-IPC-HFW3449M",
        query_brand="大华",
        candidates=LIBRARY,
    )
    assert r["best"]["code"] == "V1008"
    assert 50 < r["best"]["score"] <= 100


def test_no_match_returns_none():
    r = match_device(
        query_name="电梯五方对讲主机",
        query_model="XYZ-9999",
        query_brand="不存在的品牌",
        candidates=LIBRARY,
        threshold=75.0,
    )
    assert r["match_type"] == "none"


def test_threshold_is_respected():
    """把阈值提到 99 → 只有近乎完全一致才算匹配。"""
    r = match_device(
        query_name="网络高清枪型摄像机 400万",
        query_model=None,
        query_brand=None,
        candidates=LIBRARY,
        threshold=99.9,
    )
    assert r["match_type"] in ("none", "exact")


# ---------------------------------------------------------------------------
# 3. 打分构成
# ---------------------------------------------------------------------------

def test_score_components_are_reported():
    r = match_device(
        query_name="网络高清枪型摄像机 400万",
        query_model="DS-2CD2T46WD-I5",
        query_brand="海康威视",
        candidates=LIBRARY,
    )
    detail = r["best"]["score_detail"]
    assert set(detail.keys()) == {"name", "model", "brand"}
    assert detail["model"] == 100.0
    assert detail["brand"] == 100.0


def test_weights_sum_to_100_when_model_given():
    c = score_pair(
        query_name="网络高清枪型摄像机 400万",
        query_model="DS-2CD2T46WD-I5",
        query_brand="海康威视",
        device=LIBRARY[0],
    )
    assert c.score == pytest.approx(100.0, abs=0.01)


def test_wrong_brand_lowers_score_but_not_to_zero():
    with_brand = score_pair(
        query_name="网络高清枪型摄像机 400万",
        query_model="DS-2CD2T46WD-I5",
        query_brand="海康威视",
        device=LIBRARY[0],
    )
    wrong_brand = score_pair(
        query_name="网络高清枪型摄像机 400万",
        query_model="DS-2CD2T46WD-I5",
        query_brand="大华",
        device=LIBRARY[0],
    )
    assert wrong_brand.score < with_brand.score
    assert wrong_brand.score > 70  # 型号对了，仍应高分


# ---------------------------------------------------------------------------
# 4. 停产标记
# ---------------------------------------------------------------------------

def test_discontinued_candidate_is_flagged():
    r = match_device(
        query_name="网络高清枪型摄像机",
        query_model="DH-IPC-HFW3441M",
        query_brand="大华",
        candidates=LIBRARY,
    )
    assert r["best"]["code"] == "V1008"
    assert r["best"]["is_discontinued"] is True


def test_discontinued_count_tallies_eol_candidates():
    r = match_device(
        query_name="摄像机",
        query_model=None,
        query_brand=None,
        candidates=LIBRARY,
        limit=10,
    )
    assert r["discontinued_count"] >= 1


def test_on_sale_candidate_has_no_discontinued_flag():
    r = match_device(
        query_name="24 口千兆交换机",
        query_model="S1730S-L24T-A",
        query_brand="华为",
        candidates=LIBRARY,
    )
    assert "is_discontinued" not in r["best"]


def test_near_eol_is_also_treated_as_discontinued():
    near = dev("V1007", "网络高清枪型摄像机 400万", "宇视", "IPC2A4L-IR5-VF40",
               status=DeviceStatus.NEAR_EOL)
    r = match_device(
        query_name="网络高清枪型摄像机",
        query_model="IPC2A4L-IR5-VF40",
        query_brand="宇视",
        candidates=[near],
    )
    assert r["best"]["is_discontinued"] is True


# ---------------------------------------------------------------------------
# 5. 候选排序与 limit
# ---------------------------------------------------------------------------

def test_candidates_sorted_desc_and_limited():
    r = match_device(
        query_name="网络高清枪型摄像机 400万",
        query_model=None,
        query_brand="海康威视",
        candidates=LIBRARY,
        limit=3,
    )
    scores = [c["score"] for c in r["candidates"]]
    assert scores == sorted(scores, reverse=True)
    assert len(r["candidates"]) <= 3


def test_zero_score_candidates_are_dropped():
    r = match_device(
        query_name="电梯五方对讲主机",
        query_model=None,
        query_brand=None,
        candidates=LIBRARY,
        limit=10,
    )
    assert all(c["score"] > 0 for c in r["candidates"])


# ---------------------------------------------------------------------------
# 6. 替代型号建议
# ---------------------------------------------------------------------------

def test_configured_replacement_is_preferred():
    target = dev("V1011", "网络高清筒机 400万 星光级（新品）", "海康威视",
                 "DS-2CD2T46WD-I5", price="710.00")
    eol = dev("V1010", "网络高清筒机 400万 星光级", "海康威视", "DS-2CD3T46WD-I3",
              status=DeviceStatus.EOL, price="698.00", replacement_id=target.id)
    cands = suggest_replacements(eol, [target], limit=5)
    assert len(cands) == 1
    assert cands[0]["code"] == "V1011"


def test_price_delta_computed_against_eol_device():
    target = dev("V1011", "网络高清筒机 400万 星光级（新品）", "海康威视",
                 "DS-2CD2T46WD-I5", price="710.00")
    eol = dev("V1010", "网络高清筒机 400万 星光级", "海康威视", "DS-2CD3T46WD-I3",
              status=DeviceStatus.EOL, price="698.00", replacement_id=target.id)
    cands = suggest_replacements(eol, [target])
    assert cands[0]["price_delta"] == 12.0


def test_similarity_suggestions_when_no_configured_replacement():
    eol = dev("V1008", "网络高清枪型摄像机 400万", "大华", "DH-IPC-HFW3441M",
              status=DeviceStatus.EOL, price="645.00")
    cands = suggest_replacements(eol, LIBRARY, limit=3)
    assert len(cands) >= 1
    assert cands[0]["code"] in ("V1001", "V1002")

    def score_key(c):
        return -c["score"]

    # 已排序
    assert [c["score"] for c in cands] == sorted(
        [c["score"] for c in cands], reverse=True
    )
