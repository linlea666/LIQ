"""P0.8 · 复盘发现的三处 HIGH/MED bug 修复单测

覆盖：
  - HIGH-1 · §6 orderbook spread 极端异常值（>1%）展示层告警
  - HIGH-2 · ETF 当日无条件 pending（不再受 total_net 是否为 0 约束）
  - M-3    · §四 TP2 "通常参考 ≥ 1:2.5" 残留删除

三处 bug 的根因与修复策略详见 prompts.py / data_meta.py 内部注释
及对应 commit message。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ai.prompts import build_user_prompt
from models.data_meta import DataMeta, infer_etf_daily_status


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HIGH-1 · orderbook spread 异常值告警
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _base_ob_snapshot(**overrides) -> dict:
    snap = {
        "price": 78000.0,
        "coin": "BTC",
        "orderbook_bid_total_usd": 340_000_000,
        "orderbook_ask_total_usd": 510_000_000,
        "orderbook_spread_pct": 0.01,  # 正常情况
    }
    snap.update(overrides)
    return snap


class TestOrderbookSpreadExtremeWarning:
    """HIGH-1 · spread 绝对值 > 1% 必须触发 ⚠ 告警"""

    def test_normal_spread_no_warning(self):
        """spread 0.01% → 无告警（正常值）"""
        snap = _base_ob_snapshot(orderbook_spread_pct=0.01)
        out = build_user_prompt(snap)
        assert "盘口价差 spread:" in out
        assert "极端异常值" not in out

    def test_extreme_positive_spread_warns(self):
        """spread +41.76% → 必须标 ⚠ 极端异常值（复盘观察到的现网场景）"""
        snap = _base_ob_snapshot(orderbook_spread_pct=41.76)
        out = build_user_prompt(snap)
        assert "+41.7600%" in out
        assert "⚠ 极端异常值" in out
        assert "权重降至最低" in out

    def test_extreme_negative_spread_warns(self):
        """spread -5% → 同样告警（对称阈值 |1%|）"""
        snap = _base_ob_snapshot(orderbook_spread_pct=-5.0)
        out = build_user_prompt(snap)
        assert "-5.0000%" in out
        assert "⚠ 极端异常值" in out

    def test_boundary_just_above(self):
        """spread 1.01% 刚刚越过阈值 → 告警"""
        snap = _base_ob_snapshot(orderbook_spread_pct=1.01)
        out = build_user_prompt(snap)
        assert "⚠ 极端异常值" in out

    def test_boundary_just_below(self):
        """spread 0.99% 在阈值内 → 不告警"""
        snap = _base_ob_snapshot(orderbook_spread_pct=0.99)
        out = build_user_prompt(snap)
        assert "⚠ 极端异常值" not in out

    def test_bid_ask_skew_unchanged(self):
        """HIGH-1 修复不影响 P0.3 买卖力差展示（回归保护）"""
        snap = _base_ob_snapshot(
            orderbook_bid_total_usd=340_000_000,
            orderbook_ask_total_usd=510_000_000,
            orderbook_spread_pct=41.76,
        )
        out = build_user_prompt(snap)
        # 买卖力差 = (3.4-5.1)/(3.4+5.1) ≈ -20.00%
        assert "买卖力差 -20.00%" in out
        assert "(买-卖)/(买+卖)" in out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# HIGH-2 · ETF pending 判定放宽
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _today_utc() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")


def _yesterday_utc() -> str:
    import time
    now = int(time.time())
    return datetime.fromtimestamp(now - 86400, tz=timezone.utc).strftime("%Y-%m-%d")


class TestEtfPendingUnconditionalToday:
    """HIGH-2 · date == today → 无条件 pending（无论 total_net 为何值）"""

    def test_today_zero_still_pending(self):
        """当日 $0 → pending（旧行为保留）"""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        meta = infer_etf_daily_status(_today_utc(), 0.0, now)
        assert meta.status == "pending"
        assert "尚未收盘" in meta.pending_reason
        assert "0 可能非真实流入" in meta.pending_reason

    def test_today_positive_amount_still_pending(self):
        """当日 +$1千万（盘中快照） → 必须 pending（HIGH-2 核心场景）"""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        meta = infer_etf_daily_status(_today_utc(), 10_000_000.0, now)
        assert meta.status == "pending", (
            "当日非零金额也必须 pending —— 盘中预估快照不是终值"
        )
        assert "盘中快照非终值" in meta.pending_reason

    def test_today_large_positive_still_pending(self):
        """当日 +$2.4亿（看似巨量）→ 仍必须 pending"""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        meta = infer_etf_daily_status(_today_utc(), 240_000_000.0, now)
        assert meta.status == "pending"

    def test_today_negative_still_pending(self):
        """当日负值（净流出）→ 仍必须 pending"""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        meta = infer_etf_daily_status(_today_utc(), -50_000_000.0, now)
        assert meta.status == "pending"

    def test_yesterday_fresh(self):
        """昨日（已收盘）→ fresh（不受本轮改动影响，回归保护）"""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        meta = infer_etf_daily_status(_yesterday_utc(), 660_000_000.0, now)
        assert meta.status == "fresh"

    def test_describe_cn_renders_reason(self):
        """DataMeta.describe_cn 在 pending 时展示原因（前端/prompt 渲染依赖）"""
        now = int(datetime.now(tz=timezone.utc).timestamp())
        meta = infer_etf_daily_status(_today_utc(), 10_000_000.0, now)
        desc = meta.describe_cn()
        assert "pending" in desc
        assert "尚未收盘" in desc


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M-3 · TP2 "通常参考 ≥ 1:2.5" 残留删除
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class TestTp2ResidualRemoved:
    """M-3 · 删除 P2.1 软化后仍残留的 '通常参考 ≥ 1:X' 表述"""

    def test_tp2_residual_text_gone(self):
        """system_prompt 中不应再出现 '通常参考 ≥ 1:' 句式"""
        import pathlib
        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "ai" / "prompts.py"
        ).read_text(encoding="utf-8")
        assert "通常参考 ≥ 1:" not in src, (
            "M-3 残留未清除：TP2 段仍含 '通常参考 ≥ 1:X'，"
            "该表述会把 AI 拉回旧默认 R:R 值，抵消 P2.1 软化效果"
        )

    def test_tp2_soft_floor_intact(self):
        """核心软底线 rr≥1.0 语义仍在（回归保护）"""
        import pathlib
        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "ai" / "prompts.py"
        ).read_text(encoding="utf-8")
        assert "软底线 R:R ≥ 1.0" in src
        assert "期望值（胜率 × 盈亏比）" in src

    def test_rule_engine_min_rr_still_mentioned(self):
        """规则引擎 min_rr 过滤信息仍保留在别处（用户保护语境）"""
        import pathlib
        src = (
            pathlib.Path(__file__).resolve().parent.parent
            / "ai" / "prompts.py"
        ).read_text(encoding="utf-8")
        # 应仍有"规则引擎侧已预过滤"或"引擎 R:R 已按 ≥ 1:{min_rr}"的表述
        assert "规则引擎" in src and "min_rr" in src
