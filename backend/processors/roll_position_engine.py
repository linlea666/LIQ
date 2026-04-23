"""滚仓引擎主体 —— 针对单个活跃计划的实时评估

数据流：
    engine.py _run_loop() 每 N 秒调用 RollPositionEngine.evaluate_all()
      → 对每个 active plan 调用 evaluate(position, plan, market_ctx, settings)
      → 返回 RollSignal（WS 推送 + 重要事件入 events.jsonl）

评估流程（严格 phase 化，顺序不可改）：

    Phase 0 · 数据健康 & 系统级护栏
        - 必要字段齐全？否则 hold + insufficient
        - safety_gate=block → hold + urgent（黑天鹅/API异常/清算潮）

    Phase 1 · 离场扫描（最高优先级）
        - 爆仓距离 < 5% → close + urgent
        - 止损已被击穿 → close + urgent
        - 结构 CHoCH 反向 + trend_exhaustion=exhaustion_confirmed → close + urgent

    Phase 2 · 减仓扫描（独立 reduce_confidence_score）
        - 累加命中的 reduce_signals
        - score ≥ full_reduce → reduce (step_size_pct)
        - score ≥ half_reduce → reduce (step_size_pct × 0.5)

    Phase 3 · 止损上移
        - count_add_events ≥ trail_sl_after_add_n 且新止损优于当前 → move_sl

    Phase 4 · 加仓评估（confidence scoring）
        - 先验过滤：浮盈阈值 / 最大加仓次数 / 距上次 ATR 间距
        - 加权累加 add_confidence_score（regime/结构/动能/关键位/蜡烛/CVD/BB）
        - intensity_from_score() → full/half/small/reject
        - 应用烈度乘数 → 过三道闸门 → 二分缩量
        - accepted 则 action=add，否则 hold（必要时 gate_blocked 事件）

    Phase 5 · 前瞻扫描（独立于主动作，合并到同一个 RollSignal）

稳态约束：
    add_intensity 发生等级跳变时，需要连续 3 分钟保持目标烈度才真正切换；
    由 IntensityStabilizer 维护 per-position 的时间窗口状态。

可测性约定：
    - evaluate() 是"纯函数" + 一个可注入的 IntensityStabilizer
    - 不直接读磁盘 / 发网络请求；所有市场数据由 MarketContext 传入
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional

from models.common_enums import MarketRegimeLabel
from models.roll_position import (
    AddIntensity,
    RollPlan,
    Side,
    UserPosition,
)
from models.roll_signal import (
    AddPreview,
    ForwardWindow,
    GatesStatus,
    PreviewMetrics,
    RollSignal,
    SignalRef,
)
from processors.roll_risk import (
    INTENSITY_MULTIPLIER,
    IdealAddContext,
    SimulatedMetrics,
    bars_since_last_add_in_atr,
    binary_search_safe_margin,
    compute_ideal_add_margin,
    compute_trail_sl,
    count_add_events,
    effective_leverage,
    estimate_liq_price,
    intensity_from_score,
    simulate_after_add,
    unrealized_pnl_pct,
    unrealized_pnl_usd,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# MarketContext —— 引擎输入 DTO（上层 engine.py 负责填充）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

MarketStructureDirection = Literal["bullish", "bearish", "ranging", "transitioning"]
LastStructureEvent = Literal["BOS", "CHoCH", "none"]
TrendExhaustionState = Literal[
    "momentum_continuation",
    "healthy_continuation",
    "neutral",
    "exhaustion_warn",
    "exhaustion_confirmed",
]
KeyLevelState = Literal[
    "approaching",
    "tested",
    "bounced",
    "broken",
    "fake_break",
    "retest_done",
]
SafetyGateState = Literal["pass", "warn", "block"]
SqueezeReleaseState = Literal["pending", "released_up", "released_down", "none"]
CVDState = Literal["bull_div", "bear_div", "none"]
ReversalPatternName = Literal[
    "pin_bar_support",
    "pin_bar_resistance",
    "engulfing_bullish",
    "engulfing_bearish",
    "doji_reversal",
    "none",
]


@dataclass
class KeyLevelRef:
    """距现价最近的关键位引用（只取评估所需的扁平字段，避免耦合 KeyLevelV2 全模型）。"""
    price: float
    kind: Literal["support", "resistance"]
    state: KeyLevelState = "approaching"
    confluence_score: float = 50.0
    distance_pct: float = 0.0               # 距现价百分比（绝对值）


@dataclass
class MarketContext:
    """滚仓引擎的全部市场输入。

    上层需要提供的字段：
      - 必需：ts, current_price, atr
      - 可选（缺失时对应信号不计分，但不中断评估）：其余所有字段

    data_quality 对应字段完整度，缺失关键字段时标 partial，缺失到无法计算时标 insufficient。
    """

    ts: int
    current_price: float
    atr: float = 0.0

    regime: Optional[MarketRegimeLabel] = None
    regime_confidence: float = 0.0                 # 0~1

    ms_direction_4h: Optional[MarketStructureDirection] = None
    ms_direction_1h: Optional[MarketStructureDirection] = None
    ms_last_event_4h: LastStructureEvent = "none"
    ms_last_event_side_4h: Optional[Side] = None  # BOS/CHoCH 的方向性含义：bullish/bearish → long/short

    te_overall_state: Optional[TrendExhaustionState] = None
    te_overall_score: float = 0.0                  # -1~1（正=延续强，负=衰竭强）

    nearest_level: Optional[KeyLevelRef] = None

    reversal_pattern: ReversalPatternName = "none"

    cvd_divergence: CVDState = "none"
    funding_rate: Optional[float] = None           # 八小时 funding（持多不利=正，持空不利=负）

    squeeze_state: SqueezeReleaseState = "none"    # 针对 add_trigger=squeeze_release

    safety_gate: SafetyGateState = "pass"
    safety_gate_reason: str = ""

    # 数据健康
    data_quality: Literal["ok", "partial", "insufficient"] = "ok"
    missing_inputs: list[str] = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 权重表 —— confidence score 的可调校中枢
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 加仓权重（正=支持加仓，负=反对加仓）
ADD_WEIGHTS = {
    # regime
    "regime_trend_align":   25.0,     # trend_up+long / trend_down+short
    "regime_trend_oppose":  -30.0,    # trend_up+short / trend_down+long
    "regime_range":         -15.0,
    "regime_squeeze":         0.0,    # 中性，等方向
    "regime_high_vol_chop": -25.0,
    "regime_extreme":      -100.0,    # 硬红线

    # market structure
    "ms_align_4h":           20.0,
    "ms_oppose_4h":         -25.0,
    "ms_ranging_4h":        -10.0,
    "ms_bos_bonus":          10.0,
    "ms_align_1h":            8.0,
    "ms_oppose_1h":         -10.0,

    # trend exhaustion
    "te_momentum":           15.0,
    "te_healthy":            10.0,
    "te_neutral":             0.0,
    "te_warn":              -20.0,
    "te_confirmed":         -40.0,

    # key level
    "level_bounced_align":   15.0,   # bounced / retest_done 方向与加仓方向一致
    "level_confluence_bonus": 5.0,   # confluence ≥ 70 时额外
    "level_fake_break":     -25.0,
    "level_broken_oppose":  -15.0,   # 被反向击穿

    # candle patterns
    "pattern_reversal_align":  10.0,
    "pattern_reversal_oppose": -15.0,

    # cvd
    "cvd_align":              10.0,
    "cvd_oppose":            -10.0,

    # funding
    "funding_favorable":       5.0,   # funding 极端不利持仓方 → 对反向持仓是助攻
    "funding_unfavorable":    -5.0,

    # squeeze
    "squeeze_release_align":  10.0,
    "squeeze_release_oppose": -15.0,
}

# 减仓权重（正=支持减仓）
REDUCE_WEIGHTS = {
    "long_upper_wick":         20.0,   # 持多时长上影
    "long_lower_wick":         20.0,   # 持空时长下影
    "cvd_bear_div":            20.0,   # 持多时顶背离
    "cvd_bull_div":            20.0,   # 持空时底背离
    "sweep_fail_to_hold":      25.0,
    "exhaustion_warn":         25.0,
    "exhaustion_confirmed":    40.0,
    "volume_stall_at_extreme": 15.0,
    "fake_break":              30.0,
    "structure_choch_against": 35.0,
    "funding_extreme":         10.0,
    "reversal_pattern":        15.0,
}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Intensity 稳定器 —— 3 分钟滞后机制
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

INTENSITY_STABILIZATION_SEC = 180   # 3 分钟


@dataclass
class _IntensityWindow:
    """单个 position 的 intensity 变化窗口。"""
    current_intensity: AddIntensity = "reject"
    pending_intensity: Optional[AddIntensity] = None
    pending_since_ts: int = 0


class IntensityStabilizer:
    """跨调用维持 per-position 的 add_intensity 状态，实现"连续 3 分钟确认才切换"。

    线程不安全（单 asyncio 事件循环内顺序调用，不需要锁）。
    测试时可注入自定义实例观察内部状态。
    """

    def __init__(self, stabilization_sec: int = INTENSITY_STABILIZATION_SEC):
        self._windows: dict[str, _IntensityWindow] = {}
        self.stabilization_sec = stabilization_sec

    def get(self, position_id: str) -> _IntensityWindow:
        return self._windows.setdefault(position_id, _IntensityWindow())

    def observe(
        self,
        position_id: str,
        raw_intensity: AddIntensity,
        ts: int,
    ) -> AddIntensity:
        """接收当次的 raw 建议，返回"稳态"后的实际输出 intensity。

        规则：
          - 若 raw == current → 清空 pending，直接返回 current
          - 若 raw != current：
              - pending 为空 或 与 raw 不同 → 记录新 pending，返回 current（不切换）
              - pending == raw 且已持续 ≥ stabilization_sec → 切换，返回 raw
              - pending == raw 但未满时间 → 返回 current（继续等）
        """
        win = self.get(position_id)
        if raw_intensity == win.current_intensity:
            win.pending_intensity = None
            win.pending_since_ts = 0
            return win.current_intensity

        if win.pending_intensity != raw_intensity:
            # 启动新 pending 窗口
            win.pending_intensity = raw_intensity
            win.pending_since_ts = ts
            return win.current_intensity

        # pending 已在进行，检查是否到点
        if ts - win.pending_since_ts >= self.stabilization_sec:
            win.current_intensity = raw_intensity
            win.pending_intensity = None
            win.pending_since_ts = 0
            return win.current_intensity

        return win.current_intensity

    def reset(self, position_id: str) -> None:
        self._windows.pop(position_id, None)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 加仓 confidence scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _is_regime_align(regime: Optional[MarketRegimeLabel], side: Side) -> Optional[bool]:
    """regime 与持仓方向是否一致。None 表示 regime 未知或为中性。"""
    if regime is None:
        return None
    if regime == "trend_up":
        return side == "long"
    if regime == "trend_down":
        return side == "short"
    return None


def _is_ms_align(direction: Optional[MarketStructureDirection], side: Side) -> Optional[bool]:
    if direction is None or direction in ("transitioning",):
        return None
    if direction == "ranging":
        return None
    if direction == "bullish":
        return side == "long"
    return side == "short"


@dataclass
class ConfidenceResult:
    """confidence score 聚合结果。"""
    score: float
    breakdown: dict[str, float] = field(default_factory=dict)
    supporting: list[SignalRef] = field(default_factory=list)
    blocking: list[SignalRef] = field(default_factory=list)
    hard_block: bool = False                   # regime=extreme 等硬红线


def compute_add_confidence(
    position: UserPosition,
    market: MarketContext,
) -> ConfidenceResult:
    """计算加仓置信度分数（累加权重 + 收集支持/反对信号）。"""
    result = ConfidenceResult(score=0.0)
    side = position.side

    # ── regime ────────────────────────────────────────
    if market.regime:
        regime_align = _is_regime_align(market.regime, side)
        if market.regime == "extreme":
            w = ADD_WEIGHTS["regime_extreme"]
            result.hard_block = True
            result.blocking.append(SignalRef(
                source="market_regime", read="extreme", weight=w,
                detail="市场处于极端状态，禁止加仓（硬红线）",
            ))
            result.breakdown["regime"] = w
            result.score += w
        elif market.regime == "high_vol_chop":
            w = ADD_WEIGHTS["regime_high_vol_chop"]
            result.blocking.append(SignalRef(
                source="market_regime", read="high_vol_chop", weight=w,
                detail="高波动无序，加仓风险高",
            ))
            result.breakdown["regime"] = w
            result.score += w
        elif market.regime == "range":
            w = ADD_WEIGHTS["regime_range"]
            result.blocking.append(SignalRef(
                source="market_regime", read="range", weight=w,
                detail="箱体震荡，不利于加仓趋势",
            ))
            result.breakdown["regime"] = w
            result.score += w
        elif regime_align is True:
            w = ADD_WEIGHTS["regime_trend_align"]
            result.supporting.append(SignalRef(
                source="market_regime", read=market.regime, weight=w,
                detail=f"市场趋势与持仓方向一致（{market.regime}）",
            ))
            result.breakdown["regime"] = w
            result.score += w
        elif regime_align is False:
            w = ADD_WEIGHTS["regime_trend_oppose"]
            result.blocking.append(SignalRef(
                source="market_regime", read=market.regime, weight=w,
                detail=f"市场趋势与持仓方向相反（{market.regime}）",
            ))
            result.breakdown["regime"] = w
            result.score += w

    # ── market structure 4H ───────────────────────────
    if market.ms_direction_4h:
        align4 = _is_ms_align(market.ms_direction_4h, side)
        if align4 is True:
            w = ADD_WEIGHTS["ms_align_4h"]
            result.supporting.append(SignalRef(
                source="market_structure_4h", read=market.ms_direction_4h, weight=w,
                detail="4H 结构与持仓方向一致",
            ))
            result.breakdown["ms_4h"] = w
            result.score += w
        elif align4 is False:
            w = ADD_WEIGHTS["ms_oppose_4h"]
            result.blocking.append(SignalRef(
                source="market_structure_4h", read=market.ms_direction_4h, weight=w,
                detail="4H 结构与持仓方向相反",
            ))
            result.breakdown["ms_4h"] = w
            result.score += w
        elif market.ms_direction_4h == "ranging":
            w = ADD_WEIGHTS["ms_ranging_4h"]
            result.blocking.append(SignalRef(
                source="market_structure_4h", read="ranging", weight=w,
                detail="4H 结构横盘",
            ))
            result.breakdown["ms_4h"] = w
            result.score += w

        # BOS 同向加成
        if (
            market.ms_last_event_4h == "BOS"
            and market.ms_last_event_side_4h == side
        ):
            w = ADD_WEIGHTS["ms_bos_bonus"]
            result.supporting.append(SignalRef(
                source="market_structure_4h", read="BOS_align", weight=w,
                detail="4H 刚发生同向 BOS",
            ))
            result.breakdown["ms_bos"] = w
            result.score += w

    # ── market structure 1H（次权重） ──────────────────
    if market.ms_direction_1h:
        align1 = _is_ms_align(market.ms_direction_1h, side)
        if align1 is True:
            w = ADD_WEIGHTS["ms_align_1h"]
            result.supporting.append(SignalRef(
                source="market_structure_1h", read=market.ms_direction_1h, weight=w,
                detail="1H 结构同向",
            ))
            result.breakdown["ms_1h"] = w
            result.score += w
        elif align1 is False:
            w = ADD_WEIGHTS["ms_oppose_1h"]
            result.blocking.append(SignalRef(
                source="market_structure_1h", read=market.ms_direction_1h, weight=w,
                detail="1H 结构反向",
            ))
            result.breakdown["ms_1h"] = w
            result.score += w

    # ── trend exhaustion ─────────────────────────────
    te = market.te_overall_state
    if te:
        w_map = {
            "momentum_continuation": ADD_WEIGHTS["te_momentum"],
            "healthy_continuation": ADD_WEIGHTS["te_healthy"],
            "neutral": ADD_WEIGHTS["te_neutral"],
            "exhaustion_warn": ADD_WEIGHTS["te_warn"],
            "exhaustion_confirmed": ADD_WEIGHTS["te_confirmed"],
        }
        w = w_map.get(te, 0.0)
        if w > 0:
            result.supporting.append(SignalRef(
                source="trend_exhaustion", read=te, weight=w,
                detail=f"动能评估：{te}",
            ))
        elif w < 0:
            result.blocking.append(SignalRef(
                source="trend_exhaustion", read=te, weight=w,
                detail=f"动能评估：{te}",
            ))
        result.breakdown["te"] = w
        result.score += w

    # ── key level ─────────────────────────────────────
    if market.nearest_level:
        lv = market.nearest_level
        # 判定加仓方向与关键位"支撑 bounced → long / 阻力 bounced → short"是否一致
        bounced_aligned = (
            lv.state in ("bounced", "retest_done")
            and ((lv.kind == "support" and side == "long")
                 or (lv.kind == "resistance" and side == "short"))
        )
        if bounced_aligned:
            w = ADD_WEIGHTS["level_bounced_align"]
            detail = f"关键位 {lv.price:.2f} 反弹确认"
            if lv.confluence_score >= 70:
                w += ADD_WEIGHTS["level_confluence_bonus"]
                detail += f"（共振 {lv.confluence_score:.0f}）"
            result.supporting.append(SignalRef(
                source=f"key_level_v2#{lv.price:.2f}", read=lv.state,
                weight=w, detail=detail,
            ))
            result.breakdown["level"] = result.breakdown.get("level", 0.0) + w
            result.score += w
        elif lv.state == "fake_break":
            w = ADD_WEIGHTS["level_fake_break"]
            result.blocking.append(SignalRef(
                source=f"key_level_v2#{lv.price:.2f}", read="fake_break",
                weight=w, detail="关键位假突破",
            ))
            result.breakdown["level"] = w
            result.score += w
        elif (
            lv.state == "broken"
            and ((lv.kind == "support" and side == "long")
                 or (lv.kind == "resistance" and side == "short"))
        ):
            # 反向击穿（空头击穿支撑 + 我们持多）
            w = ADD_WEIGHTS["level_broken_oppose"]
            result.blocking.append(SignalRef(
                source=f"key_level_v2#{lv.price:.2f}", read="broken",
                weight=w, detail="关键位被反向击穿",
            ))
            result.breakdown["level"] = w
            result.score += w

    # ── candle pattern ────────────────────────────────
    if market.reversal_pattern != "none":
        pat = market.reversal_pattern
        bullish_patterns = {"pin_bar_support", "engulfing_bullish"}
        bearish_patterns = {"pin_bar_resistance", "engulfing_bearish"}
        if (pat in bullish_patterns and side == "long") or (pat in bearish_patterns and side == "short"):
            w = ADD_WEIGHTS["pattern_reversal_align"]
            result.supporting.append(SignalRef(
                source="candlestick_patterns", read=pat, weight=w,
                detail=f"K线反转形态同向（{pat}）",
            ))
            result.breakdown["pattern"] = w
            result.score += w
        elif (pat in bullish_patterns and side == "short") or (pat in bearish_patterns and side == "long"):
            w = ADD_WEIGHTS["pattern_reversal_oppose"]
            result.blocking.append(SignalRef(
                source="candlestick_patterns", read=pat, weight=w,
                detail=f"K线反转形态反向（{pat}）",
            ))
            result.breakdown["pattern"] = w
            result.score += w

    # ── CVD 背离 ──────────────────────────────────────
    cvd = market.cvd_divergence
    if cvd == "bull_div" and side == "long":
        w = ADD_WEIGHTS["cvd_align"]
        result.supporting.append(SignalRef(
            source="cvd", read="bull_div", weight=w, detail="CVD 底背离支持多",
        ))
        result.breakdown["cvd"] = w
        result.score += w
    elif cvd == "bear_div" and side == "short":
        w = ADD_WEIGHTS["cvd_align"]
        result.supporting.append(SignalRef(
            source="cvd", read="bear_div", weight=w, detail="CVD 顶背离支持空",
        ))
        result.breakdown["cvd"] = w
        result.score += w
    elif (cvd == "bull_div" and side == "short") or (cvd == "bear_div" and side == "long"):
        w = ADD_WEIGHTS["cvd_oppose"]
        result.blocking.append(SignalRef(
            source="cvd", read=cvd, weight=w, detail="CVD 背离与持仓方向相反",
        ))
        result.breakdown["cvd"] = w
        result.score += w

    # ── funding rate ──────────────────────────────────
    if market.funding_rate is not None:
        extreme_threshold = 0.001   # 0.1% 每 8h
        if side == "long" and market.funding_rate < -extreme_threshold:
            # 多头收费，对持多不利（空头太疯）→ 助攻反转，反对加仓
            w = ADD_WEIGHTS["funding_unfavorable"]
            result.blocking.append(SignalRef(
                source="funding_rate", read=f"{market.funding_rate*100:.3f}%",
                weight=w, detail="funding 极端负值，持多不利",
            ))
            result.breakdown["funding"] = w
            result.score += w
        elif side == "short" and market.funding_rate > extreme_threshold:
            w = ADD_WEIGHTS["funding_unfavorable"]
            result.blocking.append(SignalRef(
                source="funding_rate", read=f"{market.funding_rate*100:.3f}%",
                weight=w, detail="funding 极端正值，持空不利",
            ))
            result.breakdown["funding"] = w
            result.score += w
        elif side == "long" and market.funding_rate > extreme_threshold:
            w = ADD_WEIGHTS["funding_favorable"]
            result.supporting.append(SignalRef(
                source="funding_rate", read=f"{market.funding_rate*100:.3f}%",
                weight=w, detail="funding 助攻多头",
            ))
            result.breakdown["funding"] = w
            result.score += w
        elif side == "short" and market.funding_rate < -extreme_threshold:
            w = ADD_WEIGHTS["funding_favorable"]
            result.supporting.append(SignalRef(
                source="funding_rate", read=f"{market.funding_rate*100:.3f}%",
                weight=w, detail="funding 助攻空头",
            ))
            result.breakdown["funding"] = w
            result.score += w

    # ── squeeze 释放 ──────────────────────────────────
    sq = market.squeeze_state
    if (sq == "released_up" and side == "long") or (sq == "released_down" and side == "short"):
        w = ADD_WEIGHTS["squeeze_release_align"]
        result.supporting.append(SignalRef(
            source="bb_squeeze", read=sq, weight=w, detail="收敛释放方向一致",
        ))
        result.breakdown["squeeze"] = w
        result.score += w
    elif (sq == "released_up" and side == "short") or (sq == "released_down" and side == "long"):
        w = ADD_WEIGHTS["squeeze_release_oppose"]
        result.blocking.append(SignalRef(
            source="bb_squeeze", read=sq, weight=w, detail="收敛释放方向相反",
        ))
        result.breakdown["squeeze"] = w
        result.score += w

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 减仓 confidence scoring
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def compute_reduce_confidence(
    position: UserPosition,
    plan: RollPlan,
    market: MarketContext,
) -> ConfidenceResult:
    """计算减仓置信度分数（只累加 plan.reduce_signals 中启用的信号）。"""
    result = ConfidenceResult(score=0.0)
    side = position.side
    enabled = set(plan.reduce_signals)

    def _add(key: str, detail: str, source: str, read: str):
        w = REDUCE_WEIGHTS.get(key, 0.0)
        result.supporting.append(SignalRef(
            source=source, read=read, weight=w, detail=detail,
        ))
        result.breakdown[key] = w
        result.score += w

    # 长影（依持仓方向辨识）—— 需要 pattern 且与减仓方向契合
    pat = market.reversal_pattern
    if "long_upper_wick" in enabled and side == "long" and pat == "pin_bar_resistance":
        _add("long_upper_wick", "持多遇长上影（pin bar resistance）",
             "candlestick_patterns", pat)
    if "long_lower_wick" in enabled and side == "short" and pat == "pin_bar_support":
        _add("long_lower_wick", "持空遇长下影（pin bar support）",
             "candlestick_patterns", pat)
    if "reversal_pattern" in enabled and pat != "none":
        # 任何反转形态同向（对持仓的反转）都计入
        if (
            (side == "long" and pat in {"pin_bar_resistance", "engulfing_bearish"})
            or (side == "short" and pat in {"pin_bar_support", "engulfing_bullish"})
        ):
            _add("reversal_pattern", f"反转形态 {pat}", "candlestick_patterns", pat)

    # CVD 背离
    cvd = market.cvd_divergence
    if "cvd_bear_div" in enabled and side == "long" and cvd == "bear_div":
        _add("cvd_bear_div", "持多遇 CVD 顶背离", "cvd", "bear_div")
    if "cvd_bull_div" in enabled and side == "short" and cvd == "bull_div":
        _add("cvd_bull_div", "持空遇 CVD 底背离", "cvd", "bull_div")

    # 衰竭
    if "exhaustion_warn" in enabled and market.te_overall_state == "exhaustion_warn":
        _add("exhaustion_warn", "动能衰竭预警", "trend_exhaustion", "exhaustion_warn")
    if market.te_overall_state == "exhaustion_confirmed":
        _add("exhaustion_confirmed", "动能衰竭确认", "trend_exhaustion", "exhaustion_confirmed")

    # 关键位假突破 / 回收
    if market.nearest_level:
        lv = market.nearest_level
        if "fake_break" in enabled and lv.state == "fake_break":
            _add("fake_break", f"关键位 {lv.price:.2f} 假突破",
                 f"key_level_v2#{lv.price:.2f}", "fake_break")
        if "sweep_fail_to_hold" in enabled and lv.state == "fake_break":
            # sweep_fail_to_hold 是 fake_break 的一种表达；在 plan 同时启用时不重复计分
            if "fake_break" not in enabled:
                _add("sweep_fail_to_hold", "关键位扫盘未站稳",
                     f"key_level_v2#{lv.price:.2f}", "sweep_fail_to_hold")

    # 结构反向 CHoCH
    if (
        "structure_choch_against" in enabled
        and market.ms_last_event_4h == "CHoCH"
        and market.ms_last_event_side_4h is not None
        and market.ms_last_event_side_4h != side
    ):
        _add("structure_choch_against", "4H 发生反向 CHoCH",
             "market_structure_4h", "CHoCH_against")

    # funding 极端
    if "funding_extreme" in enabled and market.funding_rate is not None:
        if side == "long" and market.funding_rate < -0.001:
            _add("funding_extreme", f"funding 极端 {market.funding_rate*100:.3f}%",
                 "funding_rate", "extreme_bear")
        elif side == "short" and market.funding_rate > 0.001:
            _add("funding_extreme", f"funding 极端 {market.funding_rate*100:.3f}%",
                 "funding_rate", "extreme_bull")

    return result


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 1 · 离场（close）判定
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 默认值：距爆仓 < 5% 触发硬离场。运行时由 RollGlobalSettings.liq_emergency_pct 覆盖
# （见 RollService.evaluate_position → evaluate(..., liq_emergency_pct=...)）
LIQ_EMERGENCY_PCT_DEFAULT = 5.0


def _evaluate_exit(
    position: UserPosition,
    plan: RollPlan,
    market: MarketContext,
    liq_emergency_pct: float = LIQ_EMERGENCY_PCT_DEFAULT,
) -> Optional[tuple[str, list[SignalRef]]]:
    """若需立即离场，返回 (reason_cn, supporting_signals)；否则 None。

    liq_emergency_pct: 距爆仓百分比阈值（%）。来自 RollGlobalSettings.liq_emergency_pct，
                       由 RollService 注入；此处兼容旧调用使用默认 5.0。
    """
    supporting: list[SignalRef] = []

    # 止损被击穿
    if position.stop_loss is not None:
        if position.side == "long" and market.current_price <= position.stop_loss:
            supporting.append(SignalRef(
                source="stop_loss", read="hit", weight=0,
                detail=f"现价 {market.current_price} ≤ 止损 {position.stop_loss}",
            ))
            return ("止损触发", supporting)
        if position.side == "short" and market.current_price >= position.stop_loss:
            supporting.append(SignalRef(
                source="stop_loss", read="hit", weight=0,
                detail=f"现价 {market.current_price} ≥ 止损 {position.stop_loss}",
            ))
            return ("止损触发", supporting)

    # 爆仓距离 < 5%
    liq = estimate_liq_price(
        side=position.side,
        margin_mode=position.margin_mode,
        entry_price=position.entry_price,
        leverage=position.leverage,
        position_size=position.position_size,
        margin_used_usd=position.margin_used_usd,
        total_account_usd=position.total_account_usd,
    )
    if liq is not None and liq > 0:
        if position.side == "long":
            liq_dist = (market.current_price - liq) / market.current_price * 100.0
        else:
            liq_dist = (liq - market.current_price) / market.current_price * 100.0
        if liq_dist < liq_emergency_pct:
            supporting.append(SignalRef(
                source="liq_price", read=f"{liq_dist:.2f}%", weight=0,
                detail=f"距爆仓仅 {liq_dist:.2f}%（阈值 {liq_emergency_pct:.1f}%）",
            ))
            return (f"爆仓临近（距爆仓 {liq_dist:.2f}% < 阈值 {liq_emergency_pct:.1f}%）", supporting)

    # 结构 + 衰竭双确认
    if (
        market.ms_last_event_4h == "CHoCH"
        and market.ms_last_event_side_4h is not None
        and market.ms_last_event_side_4h != position.side
        and market.te_overall_state == "exhaustion_confirmed"
    ):
        supporting.append(SignalRef(
            source="market_structure_4h", read="CHoCH_against", weight=0,
            detail="4H 反向 CHoCH",
        ))
        supporting.append(SignalRef(
            source="trend_exhaustion", read="exhaustion_confirmed", weight=0,
            detail="动能衰竭确认",
        ))
        return ("结构反转 + 动能衰竭", supporting)

    return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 5 · 前瞻扫描
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 前瞻扫描独立在 processors/roll_forward.py（ForwardScanner），支持频控。
# evaluate() 接受可选的 scanner 参数；未传则跳过前瞻（保留纯数据路径）。


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口：evaluate
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _metrics_to_preview(m: SimulatedMetrics, account_usd: float) -> PreviewMetrics:
    return PreviewMetrics(
        avg_price=m.avg_price,
        distance_to_price_pct=m.distance_to_price_pct,
        effective_leverage=m.effective_leverage,
        liq_price=m.liq_price,
        liq_distance_pct=m.liq_distance_pct,
        position_value_usd=m.position_value_usd,
        margin_used_usd=m.margin_used_usd,
        account_margin_pct=m.account_margin_pct,
    )


def evaluate(
    position: UserPosition,
    plan: RollPlan,
    market: MarketContext,
    stabilizer: Optional[IntensityStabilizer] = None,
    forward_scanner: Optional["object"] = None,  # ForwardScanner | None（避免循环引用）
    first_add_ratio: float = 0.5,
    reinvest_ratio: float = 1.0,
    liq_emergency_pct: float = LIQ_EMERGENCY_PCT_DEFAULT,
) -> RollSignal:
    """单个活跃滚仓计划的评估入口。

    返回 RollSignal，始终包含完整字段（hold 动作也要填 supporting/blocking）。
    调用方负责写 events.jsonl（仅对 alert_* / gate_blocked 落盘）与 WS 推送。
    """
    if stabilizer is None:
        stabilizer = IntensityStabilizer()

    signal = RollSignal(
        position_id=position.id,
        plan_id=plan.id,
        ts=market.ts,
        coin=position.coin,
        current_price=market.current_price,
    )

    # ── 实时状态（先算好，所有分支共用） ─────────────
    pnl_usd = unrealized_pnl_usd(
        position.side, position.entry_price, market.current_price, position.position_size,
    )
    pnl_pct = unrealized_pnl_pct(
        position.side, position.entry_price, market.current_price, position.leverage,
    )
    eff_lev = effective_leverage(
        position.position_size, market.current_price, position.margin_used_usd, pnl_usd,
    )

    liq = estimate_liq_price(
        side=position.side,
        margin_mode=position.margin_mode,
        entry_price=position.entry_price,
        leverage=position.leverage,
        position_size=position.position_size,
        margin_used_usd=position.margin_used_usd,
        total_account_usd=position.total_account_usd,
    )
    liq_dist_pct: Optional[float] = None
    if liq is not None and liq > 0:
        if position.side == "long":
            liq_dist_pct = (market.current_price - liq) / market.current_price * 100.0
        else:
            liq_dist_pct = (liq - market.current_price) / market.current_price * 100.0

    sl_dist_pct: Optional[float] = None
    if position.stop_loss is not None and position.stop_loss > 0:
        if position.side == "long":
            sl_dist_pct = (market.current_price - position.stop_loss) / market.current_price * 100.0
        else:
            sl_dist_pct = (position.stop_loss - market.current_price) / market.current_price * 100.0

    signal.unrealized_pnl_pct = pnl_pct
    signal.unrealized_pnl_usd = pnl_usd
    signal.effective_leverage = eff_lev
    signal.distance_to_liq_pct = liq_dist_pct
    signal.distance_to_sl_pct = sl_dist_pct
    signal.data_quality = market.data_quality
    signal.missing_inputs = list(market.missing_inputs)

    # ── 前瞻扫描（并行于主动作） ──────────────────────
    if forward_scanner is not None:
        signal.forward_windows = forward_scanner.scan(position.id, market)

    # ── Phase 0 · 数据健康 & 系统护栏 ─────────────────
    if market.data_quality == "insufficient":
        signal.action = "hold"
        signal.urgency = "info"
        signal.headline_cn = "数据不足，引擎暂停评估"
        signal.detail_cn = f"缺失: {', '.join(market.missing_inputs) or '未知'}"
        signal.blocking.append(SignalRef(
            source="data_quality", read="insufficient",
            weight=-100, detail="数据不足",
        ))
        return signal

    if market.safety_gate == "block":
        signal.action = "hold"
        signal.urgency = "urgent"
        signal.headline_cn = "系统护栏触发：暂停所有滚仓动作"
        signal.detail_cn = market.safety_gate_reason or "safety gate blocked"
        signal.blocking.append(SignalRef(
            source="safety_gate", read="block",
            weight=-100, detail=signal.detail_cn,
        ))
        return signal

    # ── Phase 1 · 离场扫描 ───────────────────────────
    exit_result = _evaluate_exit(position, plan, market, liq_emergency_pct=liq_emergency_pct)
    if exit_result is not None:
        reason, sigs = exit_result
        signal.action = "close"
        signal.urgency = "urgent"
        signal.headline_cn = f"建议立即平仓：{reason}"
        signal.detail_cn = "； ".join(s.detail for s in sigs)
        signal.supporting = sigs
        return signal

    # ── Phase 2 · 减仓扫描 ───────────────────────────
    reduce_conf = compute_reduce_confidence(position, plan, market)
    if reduce_conf.score >= plan.thresholds.full_reduce:
        signal.action = "reduce"
        signal.urgency = "attention"
        signal.reduce_pct = plan.reduce_step_size_pct
        signal.reduce_confidence = reduce_conf.score
        signal.supporting = reduce_conf.supporting
        signal.confidence_breakdown = dict(reduce_conf.breakdown)
        signal.headline_cn = f"建议减仓 {plan.reduce_step_size_pct*100:.0f}%（信号分 {reduce_conf.score:.0f}）"
        signal.detail_cn = "； ".join(s.detail for s in reduce_conf.supporting)
        return signal
    if reduce_conf.score >= plan.thresholds.half_reduce:
        signal.action = "reduce"
        signal.urgency = "attention"
        signal.reduce_pct = plan.reduce_step_size_pct * 0.5
        signal.reduce_confidence = reduce_conf.score
        signal.supporting = reduce_conf.supporting
        signal.confidence_breakdown = dict(reduce_conf.breakdown)
        signal.headline_cn = f"建议半量减仓（信号分 {reduce_conf.score:.0f}）"
        signal.detail_cn = "； ".join(s.detail for s in reduce_conf.supporting)
        return signal

    # ── Phase 3 · 止损上移 ───────────────────────────
    past_adds = count_add_events(position)
    new_sl = compute_trail_sl(
        position, market.current_price, market.atr, past_adds, plan,
    )
    if new_sl is not None:
        signal.action = "move_sl"
        signal.urgency = "attention"
        signal.suggested_new_sl = new_sl
        signal.sl_move_reason = (
            "breakeven" if past_adds == plan.trail_sl_after_add_n else "trail_atr"
        )
        signal.headline_cn = f"建议移止损至 {new_sl:.2f}"
        signal.detail_cn = f"已完成 {past_adds} 次加仓，触发追踪止损规则"
        signal.supporting.append(SignalRef(
            source="roll_risk.trail_sl", read=signal.sl_move_reason,
            weight=0, detail=signal.detail_cn,
        ))
        return signal

    # ── Phase 4 · 加仓评估 ────────────────────────────
    add_signal = _evaluate_add(
        position, plan, market, pnl_pct, stabilizer,
        first_add_ratio=first_add_ratio, reinvest_ratio=reinvest_ratio,
    )
    # add_signal 一定返回非 None（内部会根据情况返回 hold 或 add）
    signal.action = add_signal.action
    signal.urgency = add_signal.urgency
    signal.confidence_score = add_signal.confidence_score
    signal.confidence_breakdown = add_signal.confidence_breakdown
    signal.add_intensity = add_signal.add_intensity
    signal.add_preview = add_signal.add_preview
    signal.supporting = add_signal.supporting
    signal.blocking = add_signal.blocking
    signal.headline_cn = add_signal.headline_cn
    signal.detail_cn = add_signal.detail_cn
    return signal


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Phase 4 加仓评估（独立函数便于单测）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _evaluate_add(
    position: UserPosition,
    plan: RollPlan,
    market: MarketContext,
    pnl_pct: float,
    stabilizer: IntensityStabilizer,
    first_add_ratio: float = 0.5,
    reinvest_ratio: float = 1.0,
) -> RollSignal:
    """加仓评估 —— 返回部分填充的 RollSignal（供 evaluate 合并）。

    此处返回的 RollSignal 仅用于把加仓相关字段打包返回，action 可能是 hold/add。
    """
    result = RollSignal(
        position_id=position.id,
        plan_id=plan.id,
        ts=market.ts,
        coin=position.coin,
        current_price=market.current_price,
    )

    # 先验硬约束 1：浮盈不够
    # min_profit_pct_to_add 采用"价格口径"（K 线走了多少百分比），
    # pnl_pct 是"保证金口径"（× leverage），两者换算：price_move_pct = pnl_pct / leverage
    price_move_pct = pnl_pct / position.leverage if position.leverage > 0 else 0.0
    if price_move_pct < plan.min_profit_pct_to_add:
        result.action = "hold"
        result.urgency = "info"
        result.headline_cn = "浮盈未达加仓门槛"
        result.detail_cn = (
            f"价格走 {price_move_pct:.2f}% (保证金 {pnl_pct:.1f}%)，"
            f"门槛 {plan.min_profit_pct_to_add:.2f}%"
        )
        result.blocking.append(SignalRef(
            source="roll_plan.min_profit", read=f"{price_move_pct:.2f}%",
            weight=0, detail=result.detail_cn,
        ))
        return result

    # 先验硬约束 2：最大加仓次数
    past_adds = count_add_events(position)
    if past_adds >= plan.max_add_times:
        result.action = "hold"
        result.urgency = "info"
        result.headline_cn = "已达最大加仓次数"
        result.detail_cn = f"已加仓 {past_adds} 次 / 上限 {plan.max_add_times}"
        result.blocking.append(SignalRef(
            source="roll_plan.max_add_times", read=str(past_adds),
            weight=0, detail=result.detail_cn,
        ))
        return result

    # 先验硬约束 3：距上次加仓的 ATR 间距不够
    bars = bars_since_last_add_in_atr(position, market.current_price, market.atr)
    if bars < plan.gates.min_add_bar_distance_atr:
        result.action = "hold"
        result.urgency = "info"
        result.headline_cn = "距上次加仓间距不足"
        result.detail_cn = (
            f"仅 {bars:.1f} × ATR，要求 ≥ {plan.gates.min_add_bar_distance_atr} × ATR"
        )
        result.blocking.append(SignalRef(
            source="roll_plan.min_add_bar_distance", read=f"{bars:.1f}ATR",
            weight=0, detail=result.detail_cn,
        ))
        return result

    # 计算 add confidence score
    conf = compute_add_confidence(position, market)
    result.confidence_score = conf.score
    result.confidence_breakdown = dict(conf.breakdown)
    result.supporting = list(conf.supporting)
    result.blocking = list(conf.blocking)

    # 硬红线（regime=extreme）直接拒绝，不走稳定器
    if conf.hard_block:
        result.action = "hold"
        result.urgency = "urgent"
        result.add_intensity = "reject"
        result.headline_cn = "极端行情硬红线：禁止加仓"
        result.detail_cn = "； ".join(b.detail for b in conf.blocking) or "regime=extreme"
        return result

    # 映射到烈度 + 稳态滞后
    raw_intensity = intensity_from_score(conf.score, plan)
    stable_intensity = stabilizer.observe(position.id, raw_intensity, market.ts)

    if stable_intensity == "reject":
        result.action = "hold"
        result.urgency = "info"
        result.add_intensity = "reject"
        # 若 raw 是更高档但还没稳态 → 解释为"等待确认"
        if raw_intensity != "reject":
            result.headline_cn = f"置信度 {conf.score:.0f} 进入 {raw_intensity}，等待稳态确认"
            result.detail_cn = "需连续 3 分钟保持才会切换为加仓建议"
        else:
            result.headline_cn = f"置信度 {conf.score:.0f} 未达加仓阈值"
            result.detail_cn = f"小档阈值 {plan.thresholds.small_add}"
        return result

    # 到这里：stable_intensity ∈ {small, half, full}
    # 计算理论加仓量 → 应用烈度乘 → 过闸门
    ctx = IdealAddContext(
        position=position, plan=plan,
        current_price=market.current_price,
        past_add_count=past_adds,
        first_add_ratio=first_add_ratio,
        reinvest_ratio=reinvest_ratio,
    )
    ideal_margin = compute_ideal_add_margin(ctx)
    multiplier = INTENSITY_MULTIPLIER[stable_intensity]
    after_intensity = ideal_margin * multiplier

    # 资金占用硬约束
    projected_total_margin = position.margin_used_usd + after_intensity
    projected_acct_pct = projected_total_margin / position.total_account_usd if position.total_account_usd > 0 else 0
    if projected_acct_pct > plan.max_margin_pct_of_account:
        # 缩到刚好不超限
        cap_add = max(0.0,
                      position.total_account_usd * plan.max_margin_pct_of_account
                      - position.margin_used_usd)
        after_intensity = min(after_intensity, cap_add)

    # 闸门检查 + 二分缩量
    safe = binary_search_safe_margin(
        position, after_intensity, market.current_price,
        plan.gates, intensity=stable_intensity,
    )

    # 构造 AddPreview（before/after 对比）
    before_m = simulate_after_add(position, 0.0, market.current_price)
    after_m = simulate_after_add(position, safe.final_margin_usd, market.current_price)

    gates_status = GatesStatus(
        gate_a_pass=safe.gates.gate_a_pass,
        gate_b_pass=safe.gates.gate_b_pass,
        gate_c_pass=safe.gates.gate_c_pass,
        gate_a_actual=safe.gates.gate_a_actual,
        gate_a_required=safe.gates.gate_a_required,
        gate_b_actual=safe.gates.gate_b_actual,
        gate_b_required=safe.gates.gate_b_required,
        gate_c_actual=safe.gates.gate_c_actual,
        gate_c_required=safe.gates.gate_c_required,
    )

    preview = AddPreview(
        mode=plan.add_mode,
        intensity=stable_intensity,
        ideal_margin_usd=ideal_margin,
        intensity_multiplier=multiplier,
        after_intensity_usd=after_intensity,
        final_margin_usd=safe.final_margin_usd,
        shrink_reason=safe.shrink_reason,
        add_size_delta=(safe.final_margin_usd * position.leverage / market.current_price)
            if market.current_price > 0 else 0.0,
        before=_metrics_to_preview(before_m, position.total_account_usd),
        after=_metrics_to_preview(after_m, position.total_account_usd),
        gates=gates_status,
    )
    result.add_intensity = stable_intensity
    result.add_preview = preview

    if not safe.accepted:
        result.action = "hold"
        result.urgency = "attention"
        result.headline_cn = f"加仓条件达成但被闸门拦截：{safe.shrink_reason}"
        result.detail_cn = "； ".join(safe.gates.failing_reasons()) or safe.shrink_reason
        result.blocking.append(SignalRef(
            source="safety_gates", read="blocked",
            weight=0, detail=result.detail_cn,
        ))
        return result

    # 加仓建议生成
    urgency_map = {"full": "attention", "half": "attention", "small": "info"}
    result.action = "add"
    result.urgency = urgency_map.get(stable_intensity, "info")  # type: ignore[assignment]
    result.headline_cn = (
        f"建议 {stable_intensity} 档加仓 ≈ ${safe.final_margin_usd:.1f}"
        + (f"（模板 {ideal_margin:.1f} × {multiplier:.0%}{'，闸门缩量' if safe.final_margin_usd < after_intensity else ''}）"
           if multiplier < 1.0 or safe.final_margin_usd < after_intensity else "")
    )
    result.detail_cn = (
        f"置信度 {conf.score:.0f} · 烈度 {stable_intensity} · "
        f"加仓后均价 {after_m.avg_price:.2f}（距现价 "
        f"{abs(after_m.distance_to_price_pct):.2f}%）· "
        f"有效杠杆 {after_m.effective_leverage:.2f}x"
    )
    return result


