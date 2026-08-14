"""
Key Level · Data Freshness (M1) 单元测试

覆盖点：
1. compute_freshness：扫描 state，正确识别 stale / missing / 健康源
2. compute_freshness：overall_freshness_score 公式（核心源 9 个）
3. apply_freshness_to_level：主源 stale → final_score × 0.85 + is_stale=True
4. apply_freshness_to_level：主源 missing → is_stale=True 但不衰减分数
5. apply_freshness_to_level：永久新鲜源（VWAP/EMA）不衰减
6. apply_freshness_to_level：source_tag 关键字匹配（30d / 7d / 1d / Footprint / VWAP）
"""
from __future__ import annotations

import time
import types

import pytest

from models.key_level import DataFreshness, KeyLevelV2
from processors.key_level_freshness import (
    SOURCE_TTL,
    apply_freshness_to_level,
    compute_freshness,
)


# ─────────────────────────────────────────────────────────────────
# 假 state 构造（用 SimpleNamespace + dict 模拟 CoinState）
# ─────────────────────────────────────────────────────────────────

def _fake_state(
    *,
    ticker_age: float | None = 30,
    liq_1d_age: float | None = 60,
    liq_7d_age: float | None = 60,
    liq_30d_age: float | None = None,
    heatmap_24h_age: float | None = 60,
    heatmap_7d_age: float | None = None,
    max_pain_age: float | None = 60,
    fp_age: float | None = 60,
    pressure_age: float | None = 30,
    cvd_age: float | None = 60,
    oi_age: float | None = 60,
):
    """构造 fake state：每个 *_age 是 None 表示该源缺失。"""
    now = time.time()

    def _ts(age):
        return int(now - age) if age is not None else None

    def _stamp(ts):
        if ts is None:
            return None
        return types.SimpleNamespace(ts=ts)

    state = types.SimpleNamespace()
    state.ticker = _stamp(_ts(ticker_age))

    liq_maps = {}
    if liq_1d_age is not None:
        liq_maps["1d"] = types.SimpleNamespace(ts=_ts(liq_1d_age))
    if liq_7d_age is not None:
        liq_maps["7d"] = types.SimpleNamespace(ts=_ts(liq_7d_age))
    if liq_30d_age is not None:
        liq_maps["30d"] = types.SimpleNamespace(ts=_ts(liq_30d_age))
    state.liq_maps = liq_maps

    liq_heatmaps = {}
    if heatmap_24h_age is not None:
        liq_heatmaps["24h"] = types.SimpleNamespace(ts=_ts(heatmap_24h_age))
    if heatmap_7d_age is not None:
        liq_heatmaps["7d"] = types.SimpleNamespace(ts=_ts(heatmap_7d_age))
    state.liq_heatmaps = liq_heatmaps

    liq_max_pain = {}
    if max_pain_age is not None:
        liq_max_pain["24h"] = types.SimpleNamespace(ts=_ts(max_pain_age))
    state.liq_max_pain = liq_max_pain

    if fp_age is not None:
        # footprint_contract = deque-like list；最后一个 bar 的 end_ts 决定年龄
        last_bar = types.SimpleNamespace(end_ts=_ts(fp_age), ts=_ts(fp_age))
        state.footprint_contract = [last_bar]
    else:
        state.footprint_contract = []

    state.orderbook_pressure_snapshot = _stamp(_ts(pressure_age))
    state.cvd_contract = _stamp(_ts(cvd_age))
    state.oi = _stamp(_ts(oi_age))

    return state


# ─────────────────────────────────────────────────────────────────
# 1. 健康场景：所有源新鲜 → score=100, stale=[], missing=[]
# ─────────────────────────────────────────────────────────────────

def test_compute_freshness_default_partial_missing():
    """默认场景：30d / heatmap_7d 缺失（非核心 9 源），只有 30d 影响 score"""
    s = _fake_state()
    f = compute_freshness(s)
    assert isinstance(f, DataFreshness)
    assert f.stale_sources == []
    assert "liq_map_30d" in f.missing_sources
    # core 9 源中，liq_map_30d 缺 → score = 100×(1-1/9) ≈ 88.9
    assert f.overall_freshness_score == round(100.0 * (1 - 1 / 9), 1)


def test_compute_freshness_full_healthy_with_all_sources():
    s = _fake_state(liq_30d_age=300, heatmap_7d_age=300)
    f = compute_freshness(s)
    assert f.overall_freshness_score == 100.0
    assert f.stale_sources == []
    assert f.missing_sources == []


# ─────────────────────────────────────────────────────────────────
# 2. stale 识别（age > TTL）
# ─────────────────────────────────────────────────────────────────

def test_compute_freshness_detects_stale_liq_1d():
    """liq_1d TTL=600，超过 → stale"""
    s = _fake_state(liq_1d_age=SOURCE_TTL["liq_map_1d"] + 100)
    f = compute_freshness(s)
    assert "liq_map_1d" in f.stale_sources


def test_compute_freshness_detects_stale_footprint():
    """footprint TTL=300"""
    s = _fake_state(fp_age=SOURCE_TTL["footprint_contract"] + 50)
    f = compute_freshness(s)
    assert "footprint_contract" in f.stale_sources


def test_safe_age_supports_ts_sec_field():
    """OrderbookPressureSnapshot 字段名为 ts_sec，_safe_age 必须兼容（修复前永远 missing）。"""
    now = time.time()
    s = _fake_state(pressure_age=None)  # 先把 .ts 版去掉
    s.orderbook_pressure_snapshot = types.SimpleNamespace(ts_sec=int(now - 30))
    f = compute_freshness(s)
    assert "orderbook_pressure" not in f.missing_sources
    assert "orderbook_pressure" in f.sources_age_seconds
    assert 25 <= f.sources_age_seconds["orderbook_pressure"] <= 35


def test_safe_age_supports_timestamp_field():
    """通用兜底：obj.timestamp 也支持。"""
    now = time.time()
    s = _fake_state(pressure_age=None)
    s.orderbook_pressure_snapshot = types.SimpleNamespace(timestamp=int(now - 45))
    f = compute_freshness(s)
    assert 40 <= f.sources_age_seconds["orderbook_pressure"] <= 50


def test_safe_age_dict_with_ts_sec():
    """dict 形态也兼容 ts_sec。"""
    now = time.time()
    s = _fake_state(pressure_age=None)
    s.orderbook_pressure_snapshot = {"ts_sec": int(now - 20)}
    f = compute_freshness(s)
    assert "orderbook_pressure" not in f.missing_sources


def test_safe_age_reads_last_series_point_for_legacy_cvd():
    """旧 CVDData 无顶层 ts 时，最后一根 series.ts 仍是权威 as_of。"""
    now = time.time()
    s = _fake_state(cvd_age=None)
    s.cvd_contract = types.SimpleNamespace(
        ts=0,
        series=[types.SimpleNamespace(ts=int(now - 40))],
    )
    f = compute_freshness(s)
    assert "cvd" not in f.missing_sources
    assert 35 <= f.sources_age_seconds["cvd"] <= 45


def test_compute_freshness_score_decreases_with_stale_count():
    """每多一个 stale/missing core → score 降一档（共 9 个核心源）"""
    s_stale_2 = _fake_state(
        liq_1d_age=SOURCE_TTL["liq_map_1d"] + 50,
        liq_7d_age=SOURCE_TTL["liq_map_7d"] + 50,
    )
    f = compute_freshness(s_stale_2)
    expected = round(100.0 * (1 - 3 / 9), 1)  # 30d 缺失 + 1d/7d stale = 3
    assert f.overall_freshness_score == expected


# ─────────────────────────────────────────────────────────────────
# 3. missing 识别（state 上无该字段或 ts 为 0）
# ─────────────────────────────────────────────────────────────────

def test_compute_freshness_detects_missing_max_pain():
    s = _fake_state(max_pain_age=None)
    f = compute_freshness(s)
    assert "liq_max_pain" in f.missing_sources


def test_compute_freshness_detects_missing_footprint():
    s = _fake_state(fp_age=None)
    f = compute_freshness(s)
    assert "footprint_contract" in f.missing_sources


def test_compute_freshness_footprint_dict_form():
    """生产形态：state.footprint_contract = deque[dict({'ts': int(秒), 'buckets': [...]})]"""
    s = _fake_state(fp_age=None)
    bar_ts = int(time.time()) - 120
    s.footprint_contract = [{"ts": bar_ts, "buckets": [{"price_lo": 1, "price_hi": 2}]}]
    f = compute_freshness(s)
    assert "footprint_contract" not in f.missing_sources
    assert "footprint_contract" not in f.stale_sources
    assert f.sources_age_seconds.get("footprint_contract") == pytest.approx(120, abs=2)


# ─────────────────────────────────────────────────────────────────
# 4. apply_freshness_to_level：主源 stale → 软衰减 0.85x
# ─────────────────────────────────────────────────────────────────

def test_apply_freshness_decays_stale_liq_7d_level():
    f = DataFreshness(
        ts=int(time.time()),
        sources_age_seconds={"liq_map_7d": 7200},
        stale_sources=["liq_map_7d"],
        overall_freshness_score=80.0,
    )
    lv = KeyLevelV2(
        price=63_000, side="support",
        sources=["7d清算簇$50M", "VWAP"],
        confluence_score=60, final_score=70.0,
        strength_tier="A",
    )
    apply_freshness_to_level(lv, f)
    assert lv.is_stale is True
    assert lv.final_score == round(70.0 * 0.85, 1)
    assert lv.primary_source_age_hours == round(7200 / 3600, 2)


def test_apply_freshness_missing_marks_stale_no_decay():
    """主源 missing → is_stale=True 但不衰减（数据消失，不知道旧不旧）"""
    f = DataFreshness(
        ts=int(time.time()),
        sources_age_seconds={},
        missing_sources=["liq_map_7d"],
        overall_freshness_score=80.0,
    )
    lv = KeyLevelV2(
        price=63_000, side="support",
        sources=["7d清算簇$50M"],
        final_score=70.0, strength_tier="A",
    )
    apply_freshness_to_level(lv, f)
    assert lv.is_stale is True
    assert lv.final_score == 70.0  # 不衰减


# ─────────────────────────────────────────────────────────────────
# 5. 永久新鲜源不衰减
# ─────────────────────────────────────────────────────────────────

def test_apply_freshness_skips_pure_indicator_levels():
    """主源是 VWAP/EMA/Fib（永久新鲜）→ 不应识别为 stale"""
    f = DataFreshness(
        ts=int(time.time()),
        sources_age_seconds={"liq_map_7d": 7200},
        stale_sources=["liq_map_7d"],
    )
    # 这个 level 的 sources 都是非清算/非 footprint
    lv = KeyLevelV2(
        price=63_000, side="support",
        sources=["VWAP", "EMA200", "Fib0.618"],
        final_score=70.0, strength_tier="A",
    )
    apply_freshness_to_level(lv, f)
    # 主源被识别为 vwap（永久新鲜分类，但 vwap 不在 stale_sources）→ 不衰减
    assert lv.is_stale is False
    assert lv.final_score == 70.0


def test_apply_freshness_30d_keyword_match():
    """30d 关键字应优先匹配 liq_map_30d 源"""
    f = DataFreshness(
        ts=int(time.time()),
        sources_age_seconds={"liq_map_30d": 14400},
        stale_sources=["liq_map_30d"],
    )
    lv = KeyLevelV2(
        price=58_000, side="support",
        sources=["30d清算簇$30M"],
        final_score=50.0, strength_tier="B",
    )
    apply_freshness_to_level(lv, f)
    assert lv.is_stale is True
    assert lv.final_score == round(50.0 * 0.85, 1)


def test_apply_freshness_footprint_keyword_match():
    """Footprint 关键字应匹配 footprint_contract"""
    f = DataFreshness(
        ts=int(time.time()),
        sources_age_seconds={"footprint_contract": 600},
        stale_sources=["footprint_contract"],
    )
    lv = KeyLevelV2(
        price=63_500, side="resistance",
        sources=["Footprint卖盘失衡(×8.5)"],
        final_score=40.0, strength_tier="B",
    )
    apply_freshness_to_level(lv, f)
    assert lv.is_stale is True
    assert lv.final_score == round(40.0 * 0.85, 1)


# ─────────────────────────────────────────────────────────────────
# 6. apply_freshness_to_level：None freshness 不报错
# ─────────────────────────────────────────────────────────────────

def test_apply_freshness_with_none_no_op():
    lv = KeyLevelV2(
        price=63_000, side="support",
        sources=["7d清算簇"], final_score=70.0,
    )
    apply_freshness_to_level(lv, None)  # type: ignore[arg-type]
    # 不抛错，不修改
    assert lv.is_stale is False
    assert lv.final_score == 70.0
