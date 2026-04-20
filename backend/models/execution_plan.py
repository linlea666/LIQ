"""数学引擎（Signal Synthesizer）最终输出模型

职责：
  - Synthesizer 把多路 CandidateSignal + Regime + SafetyGate 结果 合成
    成一份 ExecutionPlan（数学决策）
  - 作为双引擎架构中"数学引擎"的代表，和 AITraderReport 并列进入融合层

核心思想：
  - execution_score = base + breakdown 各项
  - 每一分都能追溯到具体 CandidateSignal 和因子（可解释 / 可回测）
  - 最终通过 SafetyGate 过闸，触发则降档或禁入
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.candidate_signal import CandidateSignal
from models.common_enums import (
    Direction, MarketRegimeLabel, SafetyGateStatus, SignalTier,
    TradingAction, TrafficLight,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 打分分项（可解释）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExecutionScoreBreakdown(BaseModel):
    """execution_score 的逐项拆解 —— 可解释性核心"""

    base: float = 50.0                    # 基准分

    tier_bonus: float = 0.0               # 关键位强度加分 (-10 ~ +20)
    confidence_bonus: float = 0.0         # 信号置信度加分 (-5 ~ +8)
    regime_alignment: float = 0.0         # regime 对 action 的加权 (-15 ~ +15)
    corroboration_bonus: float = 0.0      # 多源同向确认 (0 ~ +15)

    cascade_penalty: float = 0.0          # 级联风险罚分 (-20 ~ 0)
    rr_bonus: float = 0.0                 # 盈亏比奖励 (-5 ~ +8)
    backtest_bonus: float = 0.0           # 历史胜率加权 (-10 ~ +10)

    event_factor: float = 0.0             # 新闻事件因子 (-12 ~ +8)
    geo_risk_factor: float = 0.0          # 地缘风险因子 (-15 ~ +5)

    safety_gate_delta: float = 0.0        # 安全护栏调整（通常为负）

    # 最终分 = 以上各项之和（夹紧在 0-100）
    final_score: float = 50.0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 安全护栏结果
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SafetyGateResult(BaseModel):
    """5 道安全护栏的检查结果"""

    g1_extreme_vol: SafetyGateStatus = "pass"    # ATR% > 95 分位
    g2_macro_event: SafetyGateStatus = "pass"    # 4h 内 FOMC/CPI/NFP
    g3_liq_chaos: SafetyGateStatus = "pass"      # 24h 爆仓 > 7d×3
    g4_api_degrade: SafetyGateStatus = "pass"    # 关键源降级
    g5_blackswan: SafetyGateStatus = "pass"      # 黑天鹅/地缘升级

    triggered: bool = False                       # 任一非 pass 即 True
    block_reason: str = ""                        # 若 block，解释原因
    warnings: list[str] = Field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主模型
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class ExecutionPlan(BaseModel):
    """数学引擎产出的执行计划"""

    coin: str
    ts: int                                       # 秒级
    current_price: float

    # ── Regime 上下文 ──
    regime: MarketRegimeLabel = "range"
    regime_confidence: float = 0.0

    # ── 核心输出 ──
    execution_score: float = 50.0                 # 0-100
    traffic_light: TrafficLight = "gray"

    # 人类可读
    headline: str = ""                            # "A级狙击做多 @75,080，仓位建议 40%"
    one_liner: str = ""                           # "关键位反转+突破确认双源 · regime:range · RR 1:2.3"

    # ── 交易参数 ──
    action: TradingAction = "wait"
    direction: Direction = "neutral"
    tier_hint: SignalTier = "C"                   # 原始信号的强度档位
    entry_zone_low: Optional[float] = None
    entry_zone_high: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None
    position_size_pct: Optional[float] = None     # 0-100 建议仓位
    expires_at: Optional[int] = None              # 秒级

    # ── 可解释 ──
    breakdown: ExecutionScoreBreakdown = Field(default_factory=ExecutionScoreBreakdown)
    safety_gates: SafetyGateResult = Field(default_factory=SafetyGateResult)

    # ── 溯源 ──
    corroborating_sources: list[str] = Field(default_factory=list)
    # 例：["tracker_v2.swept", "range_signal.breakout", "news_event.bullish"]

    contributing_signals: list[CandidateSignal] = Field(default_factory=list)
    # 贡献本 plan 的所有候选信号（供 debug / 回测）

    # ── 回测背书 ──
    historical_win_rate: Optional[float] = None   # 0-1
    historical_sample_size: int = 0               # 样本量
