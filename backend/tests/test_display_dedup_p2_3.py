"""P2.3 · 展示层去冗余单测

验证：
  1. §9g 活跃关键位展示限制为 10 条（旧 15）
  2. §1 清算簇 24h/7d 展示限制为 10/8 条（旧无限/无限）
  3. 数据采集侧未被影响（传入 20 条应被截断展示，但不抛错）
"""
from __future__ import annotations

import pytest
from ai.prompts import build_user_prompt as _build_user_prompt


def _mk_level(price: float, tier: str = "A", state: str = "idle", i: int = 0) -> dict:
    """生成一条合法 key level"""
    return {
        "price": price,
        "side": "support",
        "state": state,
        "strength_tier": tier,
        "confluence_score": 60 - i,
        "source_count": 2,
        "cascade_risk": 0.1,
        "sweep_usd": 0,
        "sources": ["S1"],
        "bounce_quality": "",
        "breakout_stage": 0,
        "distance_pct": -0.5 * (i + 1),
        "test_count": 0,
    }


def _mk_cluster(price_from: float, price_to: float, i: int = 0) -> dict:
    return {
        "price_from": price_from,
        "price_to": price_to,
        "total_usd": 1_000_000 * (20 - i),
        "dominant_leverage": "25x",
        "distance_pct": 0.5 * (i + 1),
    }


def _base_snapshot(**overrides) -> dict:
    snap = {
        "price": 72000.0,
        "coin": "BTC",
        "liq_clusters_above": [],
        "liq_clusters_below": [],
        "vacuum_zones": [],
    }
    snap.update(overrides)
    return snap


class TestKeyLevelsTruncation:
    """§9g 关键位展示限制为 10 条"""

    def test_20_levels_display_10(self):
        levels = [_mk_level(72000 + 100 * i, i=i) for i in range(20)]
        snap = _base_snapshot(
            key_levels={
                "active_count": 20,
                "levels": levels,
                "signals": [],
                "structure_summary": "",
                "daily_strong_support": None,
                "daily_strong_resistance": None,
                "weekly_strong_support": None,
                "weekly_strong_resistance": None,
                "bull_bear_line": None,
                "breakout_zone": None,
            }
        )
        out = _build_user_prompt(snap)
        # 每行表格行形如 "| $72100 | A | ..."；点算表格内数据行
        table_rows = [
            l for l in out.split("\n")
            if l.startswith("| $") and " | 支撑 |" in l
        ]
        assert len(table_rows) == 10, (
            f"应展示 10 条活跃位，实际 {len(table_rows)}"
        )

    def test_5_levels_display_all(self):
        """少于 10 条：全部展示"""
        levels = [_mk_level(72000 + 100 * i, i=i) for i in range(5)]
        snap = _base_snapshot(
            key_levels={
                "active_count": 5, "levels": levels, "signals": [],
                "structure_summary": "", "daily_strong_support": None,
                "daily_strong_resistance": None, "weekly_strong_support": None,
                "weekly_strong_resistance": None, "bull_bear_line": None,
                "breakout_zone": None,
            }
        )
        out = _build_user_prompt(snap)
        rows = [
            l for l in out.split("\n")
            if l.startswith("| $") and " | 支撑 |" in l
        ]
        assert len(rows) == 5


class TestLiqCluster24hTruncation:
    """§1 24h 清算簇展示限制为 10 条"""

    def test_20_above_clusters_display_10(self):
        clusters = [_mk_cluster(73000 + 100 * i, 73000 + 100 * (i + 1), i=i)
                    for i in range(20)]
        snap = _base_snapshot(liq_clusters_above=clusters)
        out = _build_user_prompt(snap)
        # 上方清算密集区下的 "  - $X-$Y" 行
        section_start = out.find("上方清算密集区(空头清算):")
        section_end = out.find("\n下方清算密集区(多头清算):", section_start)
        if section_end == -1:
            section_end = out.find("\n清算真空区", section_start)
        section = out[section_start:section_end]
        rows = [l for l in section.split("\n")
                if l.strip().startswith("- $")]
        assert len(rows) == 10, f"应展示 10 条，实际 {len(rows)}"

    def test_5_below_clusters_display_all(self):
        clusters = [_mk_cluster(71000 - 100 * i, 71000 - 100 * (i - 1), i=i)
                    for i in range(5)]
        snap = _base_snapshot(liq_clusters_below=clusters)
        out = _build_user_prompt(snap)
        section_start = out.find("下方清算密集区(多头清算):")
        section_end = out.find("\n清算真空区", section_start)
        if section_end == -1:
            section_end = out.find("\n24h流动性视角", section_start)
        section = out[section_start:section_end]
        rows = [l for l in section.split("\n")
                if l.strip().startswith("- $")]
        assert len(rows) == 5


class TestLiqCluster7dTruncation:
    """§1b 7d 清算簇展示限制为 8 条（与 30d 对齐）"""

    def test_15_above_7d_display_8(self):
        clusters = [_mk_cluster(75000 + 100 * i, 75000 + 100 * (i + 1), i=i)
                    for i in range(15)]
        snap = _base_snapshot(
            liq_clusters_above_7d=clusters,
            liq_imbalance_ratio_7d=1.2,
        )
        out = _build_user_prompt(snap)
        section_start = out.find("7天上方清算密集区(空头清算):")
        # 7d 下方或其他后续段
        section_end = out.find("\n7天下方", section_start)
        if section_end == -1:
            section_end = out.find("\n7天清算真空区", section_start)
        if section_end == -1:
            section_end = out.find("\n7天流动性视角", section_start)
        section = out[section_start:section_end] if section_end > 0 else out[section_start:]
        rows = [l for l in section.split("\n")
                if l.strip().startswith("- $")]
        assert len(rows) == 8, f"应展示 8 条，实际 {len(rows)}"

    def test_no_error_when_empty(self):
        """空列表不报错（smoke）"""
        snap = _base_snapshot()
        out = _build_user_prompt(snap)
        assert "上方清算密集区" in out
