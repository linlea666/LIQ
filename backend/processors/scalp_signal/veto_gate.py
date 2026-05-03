"""信号否决闸（Veto Gate） · 信号产出前的最终硬规则检查

设计原则：
  - 与 RegimeGate 互补：RegimeGate 是 Stage 1（regime 级粗筛），
    VetoGate 是 Stage 5（candidate 级细审）
  - 任何一项 veto 触发 → 信号被丢弃（不生成）
  - 通过的检查项写入 ScalpSignal.veto_check_passed（可观测性）

当前 6 项检查：
  1. bar_closed         15m K 线必须已封闭（避免实时跳动数据）
  2. data_freshness     ticker 不能太旧（< 60s）
  3. cooldown           同 strategy × 同 direction 在 cooldown_min 内不重复发
  4. blackswan          news_brief.update_trigger=blackswan 且 < 30min 内 → 拒绝
  5. bias_opposite      candidate.direction 与 bias_score 强烈反向 → 拒绝
  6. regime_changed     regime 在最近 5min 刚切换 → 拒绝（不稳定期）

为什么不复用 OpportunityEngine 的 setup veto？
  - OpportunityEngine veto 是基于 RR 的（如 ATR 不足、距离过近）
  - 短线事件合约不需要 RR 检查（无止损止盈）
  - 故 dev-constraints #3 选择"独立新写"，针对二元方向预测优化
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from processors.scalp_signal.base_strategy import StrategyCandidate, StrategyContext

logger = logging.getLogger(__name__)


# 各项检查阈值
TICKER_FRESH_SEC = 60                  # ticker 必须 < 60s
CANDLE_15M_FRESH_SEC = 16 * 60         # 15m K 线必须 < 16min（已封闭）
BLACKSWAN_WINDOW_SEC = 30 * 60         # blackswan 简报 30min 内 → 拒绝
REGIME_STABLE_SEC = 5 * 60             # regime 必须稳定 ≥ 5min
BIAS_OPPOSITE_THRESHOLD = 0.5          # |bias_score| ≥ 0.5 且方向相反 → 拒绝


@dataclass
class VetoResult:
    """否决检查结果"""
    passed: bool
    reasons_passed: list[str] = field(default_factory=list)
    reason_blocked: Optional[str] = None
    block_detail: Optional[str] = None    # 人类可读详情（用于日志/UI）


class VetoGate:
    """否决闸 · 无状态 evaluate(candidate, ctx, ...) → VetoResult"""

    def evaluate(
        self,
        candidate: StrategyCandidate,
        ctx: StrategyContext,
        *,
        last_signal_ts: Optional[int] = None,
        cooldown_sec: int = 60 * 60,           # 默认 60min（与 StrategyConfig.cooldown_min 对齐）
        blackswan_active: bool = False,
        blackswan_age_sec: Optional[int] = None,
        now_ts: Optional[int] = None,
    ) -> VetoResult:
        """逐项检查并短路返回

        任一项失败立即返回 passed=False，不继续后续检查（first-fail 模式）

        Args:
            blackswan_active: 当前是否处于黑天鹅期（由调用方判断 news_brief 状态）
            blackswan_age_sec: 距离黑天鹅触发的秒数（若 active=True 时必填）
            cooldown_sec: 该策略 × 该方向的 cooldown 秒数
            last_signal_ts: 同 strategy × 同 direction 上次信号的 created_at
        """
        now = now_ts if now_ts is not None else int(time.time())
        passed: list[str] = []

        # 1) bar_closed：15m K 线必须已封闭
        ok, detail = self._check_bar_closed(ctx, now=now)
        if not ok:
            return VetoResult(False, passed, "bar_not_closed", detail)
        passed.append("bar_closed")

        # 2) data_freshness：ticker 不能太旧
        ok, detail = self._check_ticker_fresh(ctx, now=now)
        if not ok:
            return VetoResult(False, passed, "ticker_stale", detail)
        passed.append("ticker_fresh")

        # 3) cooldown：同 strategy × 同 direction 在 cooldown 内不重复发
        ok, detail = self._check_cooldown(last_signal_ts, cooldown_sec, now=now)
        if not ok:
            return VetoResult(False, passed, "cooldown", detail)
        passed.append("cooldown_clear")

        # 4) blackswan：黑天鹅时段 → 拒绝
        ok, detail = self._check_blackswan(blackswan_active, blackswan_age_sec)
        if not ok:
            return VetoResult(False, passed, "blackswan_active", detail)
        passed.append("no_blackswan")

        # 5) bias_opposite：与多周期偏置强烈反向
        ok, detail = self._check_bias_opposite(candidate, ctx)
        if not ok:
            return VetoResult(False, passed, "bias_opposite", detail)
        passed.append("bias_consistent")

        # 6) regime_stable：regime 不能刚切换
        ok, detail = self._check_regime_stable(ctx, now=now)
        if not ok:
            return VetoResult(False, passed, "regime_unstable", detail)
        passed.append("regime_stable")

        return VetoResult(True, passed, None, None)

    # ── 单项检查 ──────────────────────────────────────────────

    def _check_bar_closed(
        self,
        ctx: StrategyContext,
        *,
        now: int,
    ) -> tuple[bool, str]:
        candles = getattr(ctx.state, "candles_15m", None) or []
        if not candles:
            return False, "no 15m candles"
        last = candles[-1]
        ts = getattr(last, "ts", 0)
        if not ts:
            return False, "last candle no ts"
        age = now - int(ts)
        if age >= CANDLE_15M_FRESH_SEC:
            return False, f"15m candle too old: {age}s ≥ {CANDLE_15M_FRESH_SEC}s"
        # 同时要求至少有 5s 的"安全垫"：避免在 K 线刚开始的极早期就出信号
        # （刚开盘的 K 线还在剧烈跳动，不可信）
        if age < 5:
            return False, f"15m candle too fresh: {age}s < 5s（K 线刚开盘不稳定）"
        return True, ""

    def _check_ticker_fresh(
        self,
        ctx: StrategyContext,
        *,
        now: int,
    ) -> tuple[bool, str]:
        ticker = getattr(ctx.state, "ticker", None)
        if ticker is None:
            return False, "no ticker"
        ts = getattr(ticker, "ts", 0)
        if not ts:
            return False, "ticker no ts"
        age = now - int(ts)
        if age >= TICKER_FRESH_SEC:
            return False, f"ticker too old: {age}s"
        return True, ""

    def _check_cooldown(
        self,
        last_signal_ts: Optional[int],
        cooldown_sec: int,
        *,
        now: int,
    ) -> tuple[bool, str]:
        if last_signal_ts is None or last_signal_ts <= 0:
            return True, ""
        elapsed = now - int(last_signal_ts)
        if elapsed < cooldown_sec:
            return False, f"cooldown active: {elapsed}s / {cooldown_sec}s"
        return True, ""

    def _check_blackswan(
        self,
        active: bool,
        age_sec: Optional[int],
    ) -> tuple[bool, str]:
        if not active:
            return True, ""
        if age_sec is None:
            # 调用方说 active 但没给 age → 保守拒绝
            return False, "blackswan active (age unknown)"
        if age_sec < BLACKSWAN_WINDOW_SEC:
            return False, f"blackswan {age_sec // 60}min ago, within {BLACKSWAN_WINDOW_SEC // 60}min window"
        return True, ""

    def _check_bias_opposite(
        self,
        candidate: StrategyCandidate,
        ctx: StrategyContext,
    ) -> tuple[bool, str]:
        if candidate.direction == "up" and ctx.bias_score <= -BIAS_OPPOSITE_THRESHOLD:
            return False, f"up signal but bias={ctx.bias_score:+.2f} (≤ -{BIAS_OPPOSITE_THRESHOLD})"
        if candidate.direction == "down" and ctx.bias_score >= BIAS_OPPOSITE_THRESHOLD:
            return False, f"down signal but bias={ctx.bias_score:+.2f} (≥ +{BIAS_OPPOSITE_THRESHOLD})"
        return True, ""

    def _check_regime_stable(
        self,
        ctx: StrategyContext,
        *,
        now: int,
    ) -> tuple[bool, str]:
        snap = getattr(ctx.state, "regime_snapshot", None)
        if snap is None:
            return False, "no regime_snapshot"
        changed_at = getattr(snap, "regime_changed_at", 0) or 0
        if changed_at <= 0:
            # 没有切换记录 → 视为稳定（首次启动）
            return True, ""
        age = now - int(changed_at)
        if age < REGIME_STABLE_SEC:
            return False, f"regime just changed {age}s ago, need ≥ {REGIME_STABLE_SEC}s"
        return True, ""
