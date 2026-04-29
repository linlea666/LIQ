"""M4 · AI Snapshot 注入流动性墙引擎数据 单元测试

覆盖：
1. _build_liquidity_wall_block：高可信筛选（dual_source / trust_score >= 0.65）
2. wall_events 30min 过滤 + 远价位 5% 过滤
3. crowding_global 注入（OI delta / Funding / LS / 推断仓位）
4. data_quality 透传（warming / partial / stale / missing / ok）
5. 暖机期：pressure_snapshot 为 None 时全部降级为空
6. AISnapshot 字段端到端集成（liquidity_walls / liquidity_wall_events / liquidity_crowding）
7. prompt §8d 段落渲染（含警告 + 数据质量降级提示）
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ai.snapshot import _build_liquidity_wall_block, build_ai_snapshot
from models.orderbook_pressure import (
    OrderbookPressureSnapshot,
    PositionCrowdingSnapshot,
    SweepTarget,
    WallEvent,
    WallZone,
)


# ─────────────────────────────────────────────────────────────────
# 工具：构造 OP snapshot
# ─────────────────────────────────────────────────────────────────

def _make_zone(
    side: str,
    price_mid: float,
    *,
    trust_score: float = 0.5,
    dual_source: bool = False,
    has_spot_confluence: bool = False,
    source: str = "depth_only",
    distance_pct: float = 0.0,
    current_usd: float = 2_000_000.0,
    visible_minutes: float = 30.0,
    exchange_count: int = 1,
    break_through_risk: float = 0.0,
    next_magnet_price: float | None = None,
    sweep_target: SweepTarget | None = None,
) -> WallZone:
    return WallZone(
        side=side,  # type: ignore[arg-type]
        price_low=price_mid * 0.998,
        price_high=price_mid * 1.002,
        price_mid=price_mid,
        peak_price=price_mid,
        distance_pct=distance_pct,
        current_usd=current_usd,
        max_usd_1h=current_usd * 1.2,
        avg_usd_1h=current_usd,
        bin_count=2,
        seen_count=10,
        visible_minutes=visible_minutes,
        persistence_score=0.7,
        source=source,  # type: ignore[arg-type]
        exchange_count=exchange_count,
        trust_score=trust_score,
        dual_source=dual_source,
        has_spot_confluence=has_spot_confluence,
        break_through_risk=break_through_risk,
        next_magnet_price=next_magnet_price,
        sweep_target=sweep_target,
    )


def _make_event(side: str, price_mid: float, event_type: str, age_sec: int,
                snap_ts: int, confidence: float = 0.7,
                size_before: float | None = None,
                size_after: float | None = None,
                executed: float | None = None) -> WallEvent:
    return WallEvent(
        ts_sec=snap_ts - age_sec,
        side=side,  # type: ignore[arg-type]
        price_mid=price_mid,
        event_type=event_type,  # type: ignore[arg-type]
        confidence=confidence,
        size_before_usd=size_before,
        size_after_usd=size_after,
        executed_usd_value=executed,
    )


def _make_op_snap(
    *,
    walls_above: list[WallZone] | None = None,
    walls_below: list[WallZone] | None = None,
    events: list[WallEvent] | None = None,
    crowding: PositionCrowdingSnapshot | None = None,
    quality: str = "ok",
    ts_sec: int | None = None,
) -> OrderbookPressureSnapshot:
    return OrderbookPressureSnapshot(
        coin="BTC",
        ts_sec=ts_sec if ts_sec is not None else int(time.time()),
        last_price=63000.0,
        atr=300.0,
        walls_above=walls_above or [],
        walls_below=walls_below or [],
        wall_events=events or [],
        crowding_global=crowding,
        data_quality=quality,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────────
# 1. 高可信墙筛选（dual_source / trust_score >= 0.65）
# ─────────────────────────────────────────────────────────────────

class TestHighTrustWallFiltering:
    def test_dual_source_zone_included(self):
        zone = _make_zone("bid", 62500.0, dual_source=True, trust_score=0.7)
        snap = _make_op_snap(walls_below=[zone])
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["walls"]) == 1
        assert block["walls"][0]["trust_tier"] == "双源高可信"

    def test_trust_score_above_065_included(self):
        zone = _make_zone("ask", 64000.0, trust_score=0.7)
        snap = _make_op_snap(walls_above=[zone])
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["walls"]) == 1
        assert block["walls"][0]["trust_tier"] == "较可信合约"

    def test_low_trust_zone_filtered_out(self):
        zone = _make_zone("bid", 62500.0, trust_score=0.5, dual_source=False)
        snap = _make_op_snap(walls_below=[zone])
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["walls"] == []

    def test_top5_limit_applied(self):
        zones = [
            _make_zone("bid", 62500.0 - i * 100, dual_source=True, trust_score=0.7 + i * 0.01)
            for i in range(8)
        ]
        snap = _make_op_snap(walls_below=zones)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["walls"]) == 5

    def test_wall_with_break_through_risk_includes_magnet(self):
        sweep = SweepTarget(
            direction="below",
            magnet_price=61000.0,
            magnet_amount_usd=50_000_000.0,
            distance_pct=-3.2,
            vacuum_gap_pct=2.5,
        )
        zone = _make_zone(
            "bid", 62500.0,
            dual_source=True, trust_score=0.7,
            break_through_risk=0.75,
            next_magnet_price=61000.0,
            sweep_target=sweep,
        )
        snap = _make_op_snap(walls_below=[zone])
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        wall = block["walls"][0]
        assert wall["break_through_risk"] == 0.75
        assert wall["next_magnet_price"] == 61000.0
        assert wall["vacuum_gap_pct"] == 2.5

    def test_spot_only_tier_label(self):
        zone = _make_zone(
            "bid", 62500.0, source="spot_only",
            trust_score=0.7,
        )
        snap = _make_op_snap(walls_below=[zone])
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["walls"][0]["trust_tier"] == "仅现货"


# ─────────────────────────────────────────────────────────────────
# 2. wall_events 时间窗 + 距离过滤
# ─────────────────────────────────────────────────────────────────

class TestWallEventsFiltering:
    def test_consumed_event_within_30min_included(self):
        snap_ts = int(time.time())
        ev = _make_event("bid", 62500.0, "wall_consumed", 600, snap_ts,
                         executed=500_000.0)
        snap = _make_op_snap(events=[ev], ts_sec=snap_ts)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["events"]) == 1
        assert block["events"][0]["kind"] == "被吃"
        assert block["events"][0]["min_ago"] == 10
        assert block["events"][0]["executed_usd_value"] == 500_000.0

    def test_event_older_than_30min_excluded(self):
        snap_ts = int(time.time())
        ev = _make_event("bid", 62500.0, "wall_consumed", 1900, snap_ts)
        snap = _make_op_snap(events=[ev], ts_sec=snap_ts)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["events"] == []

    def test_event_far_from_price_excluded(self):
        """事件价位距当前价 > 5% → 过滤。"""
        snap_ts = int(time.time())
        ev = _make_event("ask", 70_000.0, "wall_consumed", 300, snap_ts)
        snap = _make_op_snap(events=[ev], ts_sec=snap_ts)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["events"] == []

    def test_only_three_event_types_included(self):
        """非 consumed/strengthened/removed 事件被忽略。"""
        snap_ts = int(time.time())
        evs = [
            _make_event("bid", 62500.0, "wall_appeared", 300, snap_ts),
            _make_event("bid", 62500.0, "wall_consumed", 300, snap_ts),
        ]
        snap = _make_op_snap(events=evs, ts_sec=snap_ts)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["events"]) == 1
        assert block["events"][0]["kind"] == "被吃"

    def test_events_sorted_by_recency(self):
        snap_ts = int(time.time())
        evs = [
            _make_event("bid", 62500.0, "wall_consumed", 1500, snap_ts),
            _make_event("bid", 62700.0, "wall_strengthened", 200, snap_ts),
            _make_event("bid", 62300.0, "wall_removed", 800, snap_ts),
        ]
        snap = _make_op_snap(events=evs, ts_sec=snap_ts)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["events"]) == 3
        assert block["events"][0]["kind"] == "增厚"
        assert block["events"][1]["kind"] == "撤单"
        assert block["events"][2]["kind"] == "被吃"

    def test_events_capped_at_8(self):
        snap_ts = int(time.time())
        evs = [
            _make_event("bid", 62500.0, "wall_strengthened", 100 + i * 50, snap_ts)
            for i in range(15)
        ]
        snap = _make_op_snap(events=evs, ts_sec=snap_ts)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert len(block["events"]) == 8


# ─────────────────────────────────────────────────────────────────
# 3. crowding_global 注入
# ─────────────────────────────────────────────────────────────────

class TestCrowdingInjection:
    def test_crowding_with_full_data(self):
        cg = PositionCrowdingSnapshot(
            oi_delta_1h_pct=2.5,
            oi_delta_24h_pct=8.0,
            oi_margin_split="stable_dominant",
            funding_now_pct=0.0001,
            funding_percentile_30d=0.85,
            top_position_ls_ratio=1.85,
            inferred_position_state="long_opening",
            long_crowding_risk=0.7,
            short_crowding_risk=0.2,
        )
        snap = _make_op_snap(crowding=cg)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["crowding"] is not None
        assert block["crowding"]["oi_delta_1h_pct"] == 2.5
        assert block["crowding"]["oi_margin_split"] == "stable_dominant"
        assert block["crowding"]["inferred_position_state"] == "long_opening"
        assert block["crowding"]["long_crowding_risk"] == 0.7

    def test_crowding_drops_none_fields(self):
        cg = PositionCrowdingSnapshot(
            oi_delta_1h_pct=1.0,
        )
        snap = _make_op_snap(crowding=cg)
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert "oi_delta_1h_pct" in block["crowding"]
        assert "funding_now_pct" not in block["crowding"]


# ─────────────────────────────────────────────────────────────────
# 4. 暖机期 / 数据质量
# ─────────────────────────────────────────────────────────────────

class TestQualityAndFallback:
    def test_none_snapshot_returns_empty(self):
        block = _build_liquidity_wall_block(None, last_price=63000.0)
        assert block["walls"] == []
        assert block["events"] == []
        assert block["crowding"] is None
        assert block["quality"] == ""

    def test_warming_quality_passed_through(self):
        snap = _make_op_snap(quality="warming")
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["quality"] == "warming"

    def test_empty_snapshot_returns_clean_block(self):
        snap = _make_op_snap()
        block = _build_liquidity_wall_block(snap, last_price=63000.0)
        assert block["walls"] == []
        assert block["events"] == []
        assert block["crowding"] is None


# ─────────────────────────────────────────────────────────────────
# 5. AISnapshot 端到端集成
# ─────────────────────────────────────────────────────────────────

def _make_base_kwargs():
    return dict(
        coin="BTC",
        price=63_000.0,
        high_24h=64_000.0,
        low_24h=62_000.0,
        liq_map=None,
        cvd_contract=None,
        cvd_spot=None,
        oi=None,
        funding=None,
        basis=None,
        orderbook=None,
        liq_stats=None,
        vp=None,
        atr=300.0,
        market_temp_score=50.0,
        pin_risk_level="low",
    )


class TestAISnapshotIntegration:
    def test_snapshot_injects_liquidity_block_when_present(self):
        cg = PositionCrowdingSnapshot(
            oi_delta_1h_pct=1.5,
            inferred_position_state="mixed",
            long_crowding_risk=0.3,
            short_crowding_risk=0.3,
        )
        zone = _make_zone(
            "bid", 62800.0,
            dual_source=True, trust_score=0.85,
        )
        op = _make_op_snap(walls_below=[zone], crowding=cg)

        snap = build_ai_snapshot(**_make_base_kwargs(), pressure_snapshot=op)

        assert len(snap.liquidity_walls) == 1
        assert snap.liquidity_walls[0]["trust_tier"] == "双源高可信"
        assert snap.liquidity_crowding is not None
        assert snap.liquidity_wall_quality == "ok"

    def test_snapshot_without_pressure_keeps_empty_defaults(self):
        snap = build_ai_snapshot(**_make_base_kwargs())
        assert snap.liquidity_walls == []
        assert snap.liquidity_wall_events == []
        assert snap.liquidity_crowding is None
        assert snap.liquidity_wall_quality == ""


# ─────────────────────────────────────────────────────────────────
# 6. prompt §8d 段落渲染
# ─────────────────────────────────────────────────────────────────

class TestPromptRendering:
    def _build_user_prompt_section(self, snap_dict: dict) -> str:
        """直接调 prompts.build_user_prompt 拿完整渲染文本。"""
        from ai.prompts import build_user_prompt
        return build_user_prompt(snap_dict)

    def _base_snap_dict(self) -> dict:
        """构造最小可渲染的 snapshot dict（避免 prompt 各段 KeyError）。"""
        return {
            "coin": "BTC", "ts": int(time.time()), "price": 63000.0,
            "high_24h": 64000.0, "low_24h": 62000.0,
            "atr_14": 300.0,
            "market_temperature": 50.0, "pin_risk_level": "low",
            "cvd_contract_trend": "", "cvd_contract_delta_1h": 0,
            "cvd_spot_trend": "", "cvd_spot_delta_1h": 0,
            "cvd_divergence": "",
            "oi_current_usd": 0, "oi_change_1h_pct": 0,
            "oi_change_5m_pct": 0, "oi_trend": "",
            "funding_interpretation": "",
            "basis_pct": 0,
            "orderbook_bid_total_usd": 0, "orderbook_ask_total_usd": 0,
            "orderbook_spread_pct": 0,
            "recent_liq_24h_long_usd": 0, "recent_liq_24h_short_usd": 0,
            "volume_profile_poc": 0, "value_area_high": 0,
            "value_area_low": 0, "vwap": 0,
            "liq_clusters_above": [], "liq_clusters_below": [],
            "vacuum_zones": [], "liq_imbalance_ratio": 0,
            "liquidity_walls": [],
            "liquidity_wall_events": [],
            "liquidity_crowding": None,
            "liquidity_wall_quality": "",
        }

    def test_section_8d_skipped_when_block_empty(self):
        text = self._build_user_prompt_section(self._base_snap_dict())
        assert "8d. 流动性墙引擎" not in text

    def test_section_8d_renders_with_walls_and_warning(self):
        snap_dict = self._base_snap_dict()
        snap_dict["liquidity_walls"] = [{
            "side": "买墙",
            "price_mid": 62800.0,
            "distance_pct": -0.32,
            "current_usd": 5_000_000.0,
            "trust_tier": "双源高可信",
            "trust_score": 0.85,
            "persistence_min": 45.0,
            "exchange_count": 3,
            "break_through_risk": 0.3,
        }]
        text = self._build_user_prompt_section(snap_dict)
        assert "8d. 流动性墙引擎" in text
        assert "双源高可信" in text
        # 警告语必现
        assert "spoof" in text or "意图" in text
        assert "trust_score" in text

    def test_section_8d_break_through_warning_visible(self):
        snap_dict = self._base_snap_dict()
        snap_dict["liquidity_walls"] = [{
            "side": "买墙",
            "price_mid": 62800.0,
            "distance_pct": -0.32,
            "current_usd": 5_000_000.0,
            "trust_tier": "双源高可信",
            "trust_score": 0.85,
            "persistence_min": 45.0,
            "exchange_count": 3,
            "break_through_risk": 0.75,
            "next_magnet_price": 61000.0,
            "vacuum_gap_pct": 2.5,
        }]
        text = self._build_user_prompt_section(snap_dict)
        # W1-T3：打穿风险渲染必须明确为"评分"而非概率
        # W3-T3：评分以 0.XX 浮点展示（不再 X%）
        assert "打穿风险评分" in text
        assert "打穿风险评分0.75" in text
        # 反向断言：不应再出现 X% 形式
        assert "打穿风险评分75%" not in text
        assert "打穿风险75%" not in text
        assert "磁铁" in text
        assert "真空跨度" in text

    def test_section_8d_probability_disclaimer_present(self):
        """W1-T3：性质提示必须显式声明"打穿风险≠统计概率"，防止 AI 误读。"""
        snap_dict = self._base_snap_dict()
        snap_dict["liquidity_walls"] = [{
            "side": "买墙", "price_mid": 62800.0, "distance_pct": -0.32,
            "current_usd": 5_000_000.0, "trust_tier": "双源高可信",
            "trust_score": 0.85, "persistence_min": 45.0,
            "exchange_count": 3, "break_through_risk": 0.3,
        }]
        text = self._build_user_prompt_section(snap_dict)
        # 必须出现的关键短语（语义判定，不依赖具体措辞）
        assert "不是统计概率" in text
        # 强化提示：禁止表述 X% 概率
        assert "概率被打穿" in text  # "X% 概率被打穿" 子串
        # trust_score 也要免责
        assert "不代表" in text and "不会被打穿" in text

    def test_section_8d_warming_quality_label(self):
        snap_dict = self._base_snap_dict()
        snap_dict["liquidity_walls"] = [{
            "side": "买墙",
            "price_mid": 62800.0,
            "distance_pct": -0.32,
            "current_usd": 5_000_000.0,
            "trust_tier": "较可信合约",
            "trust_score": 0.7,
            "persistence_min": 20.0,
            "exchange_count": 1,
            "break_through_risk": 0.0,
        }]
        snap_dict["liquidity_wall_quality"] = "warming"
        text = self._build_user_prompt_section(snap_dict)
        assert "暖机期" in text or "数据 < 30min" in text


# ─────────────────────────────────────────────────────────────────
# W3-T3 文案统一：prompt §8d 风险评分 X% → 0.XX
# ─────────────────────────────────────────────────────────────────

class TestW3T3PromptScoreFormat:
    """W3-T3：prompt §8d 中的风险评分一律以 0.XX 浮点展示。

    动机：与 trust_score / confidence / SR/SA 同口径，避免 AI 把 X% 误读为统计概率。
    """

    def _build(self, snap_dict: dict) -> str:
        from ai.prompts import build_user_prompt
        return build_user_prompt(snap_dict)

    def _wall_dict(self, btr: float = 0.78, **extra) -> dict:
        base = {
            "side": "买墙",
            "price_mid": 62800.0,
            "distance_pct": -0.32,
            "current_usd": 5_000_000.0,
            "trust_tier": "双源高可信",
            "trust_score": 0.85,
            "persistence_min": 45.0,
            "exchange_count": 3,
            "break_through_risk": btr,
        }
        base.update(extra)
        return base

    def _base_snap(self) -> dict:
        return {
            "coin": "BTC", "ts": int(time.time()), "price": 63000.0,
            "high_24h": 64000.0, "low_24h": 62000.0,
            "atr_14": 300.0,
            "market_temperature": 50.0, "pin_risk_level": "low",
            "cvd_contract_trend": "", "cvd_contract_delta_1h": 0,
            "cvd_spot_trend": "", "cvd_spot_delta_1h": 0,
            "cvd_divergence": "",
            "oi_current_usd": 0, "oi_change_1h_pct": 0,
            "oi_change_5m_pct": 0, "oi_trend": "",
            "funding_interpretation": "",
            "basis_pct": 0,
            "orderbook_bid_total_usd": 0, "orderbook_ask_total_usd": 0,
            "orderbook_spread_pct": 0,
            "recent_liq_24h_long_usd": 0, "recent_liq_24h_short_usd": 0,
            "volume_profile_poc": 0, "value_area_high": 0,
            "value_area_low": 0, "vwap": 0,
            "liq_clusters_above": [], "liq_clusters_below": [],
            "vacuum_zones": [], "liq_imbalance_ratio": 0,
            "liquidity_walls": [],
            "liquidity_wall_events": [],
            "liquidity_crowding": None,
            "liquidity_wall_quality": "",
        }

    def test_risk_score_two_decimal_float_format(self):
        snap = self._base_snap()
        snap["liquidity_walls"] = [self._wall_dict(btr=0.78)]
        text = self._build(snap)
        assert "打穿风险评分0.78" in text
        # 反向断言：禁止 X% 形式
        assert "打穿风险评分78%" not in text
        assert "打穿风险78%" not in text

    def test_risk_score_round_value_keeps_two_decimals(self):
        """0.6 边界 → 0.60（保留两位小数）。"""
        snap = self._base_snap()
        snap["liquidity_walls"] = [self._wall_dict(btr=0.6)]
        text = self._build(snap)
        assert "打穿风险评分0.60" in text
        assert "打穿风险评分60%" not in text

    def test_risk_score_max_value(self):
        """评分 = 1.0 → 1.00。"""
        snap = self._base_snap()
        snap["liquidity_walls"] = [self._wall_dict(btr=1.0)]
        text = self._build(snap)
        assert "打穿风险评分1.00" in text
        assert "打穿风险评分100%" not in text

    def test_risk_score_below_threshold_no_render(self):
        """break_through_risk < 0.6 → 不渲染风险段，自然不会有 X% / 0.XX。"""
        snap = self._base_snap()
        snap["liquidity_walls"] = [self._wall_dict(btr=0.3)]
        text = self._build(snap)
        # ⚠ 风险段不应出现
        assert "⚠打穿风险评分" not in text

    def test_disclaimer_mentions_float_format(self):
        """性质提示明确指出"评分以 0.00–1.00 浮点展示"，统一口径。"""
        snap = self._base_snap()
        snap["liquidity_walls"] = [self._wall_dict(btr=0.3)]
        text = self._build(snap)
        # W3-T3 强化：免责声明须显式说明评分采用浮点形式
        assert "0.00" in text and "1.00" in text  # "0.00–1.00 浮点"
        assert "浮点" in text
        # 与 trust_score / confidence 同口径的语义提示
        assert "同口径" in text or "trust_score" in text or "confidence" in text

    def test_no_score_pct_anywhere_in_section(self):
        """整个 §8d 段落中：「评分」字样后不得紧跟数字+%。"""
        import re

        snap = self._base_snap()
        snap["liquidity_walls"] = [self._wall_dict(btr=0.85)]
        text = self._build(snap)
        # 抓出整个 §8d 段
        m = re.search(r"### 8d\..*?(?=\n### |\Z)", text, re.DOTALL)
        assert m is not None
        section = m.group(0)
        # 风险评分必须以 0.XX 或 1.00 浮点展示
        assert re.search(r"评分(0\.\d{2}|1\.00)", section)
        # 禁止 评分\d+% 形式
        assert not re.search(r"评分\d+(\.\d+)?%", section), (
            f"§8d 不得出现「评分 X%」形式: {section[:500]}"
        )
