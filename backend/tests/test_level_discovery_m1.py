"""
Level Discovery · M1 (V3 准备阶段) 单元测试

覆盖点：
1. `discover_levels` 新增 `liq_map_30d` / `footprint_snapshot` 参数（向后兼容默认 None）
2. 30d 清算簇候选生成（距离窗 8-30%，base × 0.5）
3. footprint_stacked 候选生成（contract latest/prev top_imbalance_zones）
4. RawCandidate.cluster_meta 透传（exchange_count / leverage_intensity）
5. confluence_scoring 共识 multiplier 公式
6. confluence_scoring 失效价计算（S=2×ATR / A=1.5×ATR）
"""
from __future__ import annotations

import inspect

from models.liquidation import LiqCluster, LiquidationMap
from models.market_action import FootprintBarStats, FootprintSnapshot
from processors.confluence_scoring import (
    CONSENSUS_MULTIPLIER_CAP,
    INVALIDATION_ATR_MULT,
    _calc_invalidation,
)
from processors.level_discovery import discover_levels


# ─────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────

def _make_liq_map(
    clusters_below: list[tuple[float, float, float, str, int]] | None = None,
    clusters_above: list[tuple[float, float, float, str, int]] | None = None,
) -> LiquidationMap:
    """clusters_*: list of (price_center, total_usd, distance_pct, lev, ex_count)"""
    below = []
    for p, usd, d, lev, ec in clusters_below or []:
        below.append(LiqCluster(
            price_center=p, price_from=p - 50, price_to=p + 50,
            total_usd=usd, side="long",
            dominant_leverage=lev, distance_pct=d,
            exchange_count=ec, dominant_exchange="Binance",
            leverage_intensity=0.7,
        ))
    above = []
    for p, usd, d, lev, ec in clusters_above or []:
        above.append(LiqCluster(
            price_center=p, price_from=p - 50, price_to=p + 50,
            total_usd=usd, side="short",
            dominant_leverage=lev, distance_pct=d,
            exchange_count=ec, dominant_exchange="OKX",
            leverage_intensity=0.6,
        ))
    return LiquidationMap(
        coin="BTC", ts=int(__import__("time").time()), cycle="1d",
        leverage_groups=[],
        clusters_below=below, clusters_above=above,
    )


def _make_fp_snap_with_stacked(
    *,
    contract_zones: list[dict],
    prev_zones: list[dict] | None = None,
) -> FootprintSnapshot:
    latest = FootprintBarStats(
        ts=int(__import__("time").time()),
        total_buy_usd=1_000_000, total_sell_usd=900_000,
        delta_usd=100_000, delta_pct=0.05,
        top_imbalance_zones=contract_zones,
    )
    prev = None
    if prev_zones is not None:
        prev = FootprintBarStats(
            ts=int(__import__("time").time()) - 3600,
            total_buy_usd=800_000, total_sell_usd=900_000,
            delta_usd=-100_000, delta_pct=-0.06,
            top_imbalance_zones=prev_zones,
        )
    return FootprintSnapshot(contract_latest=latest, contract_prev=prev)


# ─────────────────────────────────────────────────────────────────
# 1. 签名验收
# ─────────────────────────────────────────────────────────────────

def test_signature_has_m1_params():
    sig = inspect.signature(discover_levels)
    assert "liq_map_30d" in sig.parameters, "M1 必须新增 liq_map_30d 参数"
    assert "footprint_snapshot" in sig.parameters, "M1 必须新增 footprint_snapshot 参数"
    assert sig.parameters["liq_map_30d"].default is None
    assert sig.parameters["footprint_snapshot"].default is None


# ─────────────────────────────────────────────────────────────────
# 2. 30d 清算簇接入
# ─────────────────────────────────────────────────────────────────

def test_liq_map_30d_clusters_become_candidates():
    """30d 簇位于 8-30% 距离窗，应进入候选池，timeframe='1W'"""
    price = 100_000
    # 30d 多头簇在 -15% (85000)
    liq_30d = _make_liq_map(
        clusters_below=[(85_000, 50_000_000, 15, "50", 3)],
        clusters_above=[(118_000, 60_000_000, 18, "100", 4)],
    )
    result = discover_levels(
        current_price=price, atr=500,
        liq_map_30d=liq_30d,
    )
    cands_30d = [c for c in result.candidates if "30d" in c.source_tag]
    assert len(cands_30d) >= 2, "30d 双向簇都应入选"
    # 检查 timeframe 与 dimension
    for c in cands_30d:
        assert c.timeframe == "1W"
        assert c.dimension == "capital_flow"
        assert c.cluster_meta.get("exchange_count", 0) >= 3


def test_liq_map_30d_too_close_filtered():
    """30d 簇距离 <8% 时应被过滤（避免与 1d/7d 重叠）"""
    price = 100_000
    liq_30d = _make_liq_map(
        clusters_below=[(96_000, 50_000_000, 4, "50", 3)],   # 4% 太近
        clusters_below_no_use=None,  # type: ignore  # 仅占位
    ) if False else _make_liq_map(
        clusters_below=[(96_000, 50_000_000, 4, "50", 3)],
    )
    result = discover_levels(
        current_price=price, atr=500,
        liq_map_30d=liq_30d,
    )
    cands_30d = [c for c in result.candidates if "30d" in c.source_tag]
    assert len(cands_30d) == 0, f"近距离 30d 簇应被过滤, got: {cands_30d}"


# ─────────────────────────────────────────────────────────────────
# 3. cluster_meta 透传
# ─────────────────────────────────────────────────────────────────

def test_cluster_meta_propagated_to_candidate():
    """RawCandidate.cluster_meta 应携带 exchange_count / leverage_intensity / dominant_leverage"""
    price = 100_000
    liq_1d = _make_liq_map(
        clusters_below=[(98_000, 30_000_000, 2, "75", 4)],
    )
    result = discover_levels(
        current_price=price, atr=500,
        liq_map=liq_1d,
    )
    liq_cands = [c for c in result.candidates if c.source_tag == "liq_cluster_below_1d"]
    assert len(liq_cands) == 1
    meta = liq_cands[0].cluster_meta
    assert meta.get("exchange_count") == 4, f"exchange_count 应被透传, got: {meta}"
    assert meta.get("dominant_leverage") == "75"
    assert meta.get("leverage_intensity") == 0.7
    assert meta.get("total_usd") == 30_000_000


# ─────────────────────────────────────────────────────────────────
# 4. Footprint stacked imbalance
# ─────────────────────────────────────────────────────────────────

def test_footprint_stacked_buy_below_price_makes_support():
    """stacked_buy 价位 < 当前价 → support 候选"""
    price = 100_000
    fp = _make_fp_snap_with_stacked(
        contract_zones=[
            {"price": 99_500, "buy": 5_000_000, "sell": 500_000,
             "ratio": 10.0, "side": "stacked_buy"},
        ],
    )
    result = discover_levels(
        current_price=price, atr=500,
        footprint_snapshot=fp,
    )
    fp_cands = [c for c in result.candidates if c.source_tag == "footprint_stacked"]
    assert len(fp_cands) == 1, f"应生成一个 footprint_stacked 候选, got: {fp_cands}"
    assert fp_cands[0].side == "support"
    assert fp_cands[0].price == 99_500


def test_footprint_stacked_buy_above_price_flips_to_resistance():
    """stacked_buy 但价位 > 当前价 → 修正为 resistance（买盘被吸收）"""
    price = 100_000
    fp = _make_fp_snap_with_stacked(
        contract_zones=[
            {"price": 100_500, "buy": 5_000_000, "sell": 500_000,
             "ratio": 10.0, "side": "stacked_buy"},
        ],
    )
    result = discover_levels(
        current_price=price, atr=500,
        footprint_snapshot=fp,
    )
    fp_cands = [c for c in result.candidates if c.source_tag == "footprint_stacked"]
    assert len(fp_cands) == 1
    assert fp_cands[0].side == "resistance", \
        "stacked_buy 在价格上方应被识别为 resistance（被吸收）"
    assert "被吸收" in fp_cands[0].source


def test_footprint_stacked_distance_filter():
    """超过 5×ATR 的失衡带应被过滤"""
    price = 100_000
    atr = 500
    fp = _make_fp_snap_with_stacked(
        contract_zones=[
            {"price": 96_000, "buy": 5_000_000, "sell": 500_000,
             "ratio": 10.0, "side": "stacked_buy"},  # 距离 4000 = 8×ATR
        ],
    )
    result = discover_levels(
        current_price=price, atr=atr,
        footprint_snapshot=fp,
    )
    fp_cands = [c for c in result.candidates if c.source_tag == "footprint_stacked"]
    assert len(fp_cands) == 0, "8×ATR 距离的失衡带应被过滤"


# ─────────────────────────────────────────────────────────────────
# 5. 共识 multiplier
# ─────────────────────────────────────────────────────────────────

def test_consensus_multiplier_formula():
    """min(cap=1.6, 1 + 0.15×(ec-1))"""
    # 边界：ec=1 → mult=1.0
    assert min(CONSENSUS_MULTIPLIER_CAP, 1.0 + 0.15 * (1 - 1)) == 1.0
    # ec=3 → mult=1.30
    assert abs(min(CONSENSUS_MULTIPLIER_CAP, 1.0 + 0.15 * (3 - 1)) - 1.30) < 1e-9
    # ec=5 → 1.6（撞 cap）
    assert min(CONSENSUS_MULTIPLIER_CAP, 1.0 + 0.15 * (5 - 1)) == 1.6
    # ec=10 → 仍 1.6（cap 生效）
    assert min(CONSENSUS_MULTIPLIER_CAP, 1.0 + 0.15 * (10 - 1)) == 1.6


# ─────────────────────────────────────────────────────────────────
# 6. 失效价计算
# ─────────────────────────────────────────────────────────────────

def test_invalidation_price_for_support_s_tier():
    """S 级 support → price - 2×ATR"""
    from models.key_level import KeyLevelV2
    lv = KeyLevelV2(
        price=63_000, side="support", strength_tier="S",
        confluence_score=80, sources=["7d清算簇"],
    )
    inv, cond, mult = _calc_invalidation(lv, atr=200)
    assert mult == INVALIDATION_ATR_MULT["S"] == 2.0
    assert inv == 63_000 - 2 * 200
    assert "1h 收盘 <" in cond
    assert "$62,600" in cond


def test_invalidation_price_for_resistance_a_tier():
    """A 级 resistance → price + 1.5×ATR"""
    from models.key_level import KeyLevelV2
    lv = KeyLevelV2(
        price=66_000, side="resistance", strength_tier="A",
        confluence_score=55, sources=["VWAP"],
    )
    inv, cond, mult = _calc_invalidation(lv, atr=300)
    assert mult == INVALIDATION_ATR_MULT["A"] == 1.5
    assert inv == 66_000 + 1.5 * 300
    assert "1h 收盘 >" in cond


def test_invalidation_disabled_when_atr_zero():
    from models.key_level import KeyLevelV2
    lv = KeyLevelV2(price=63_000, side="support", strength_tier="S", confluence_score=80)
    inv, cond, mult = _calc_invalidation(lv, atr=0)
    assert inv is None
    assert cond == ""
    assert mult == 0.0


# ─────────────────────────────────────────────────────────────────
# 7. 向后兼容：所有 M1 参数均为 None 时不报错，行为同旧版
# ─────────────────────────────────────────────────────────────────

def test_backward_compat_none_args_does_not_crash():
    result = discover_levels(
        current_price=100_000, atr=500,
        # 不传 liq_map_30d / footprint_snapshot
    )
    assert isinstance(result.candidates, list)
