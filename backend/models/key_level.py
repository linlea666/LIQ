"""关键位生命周期追踪数据模型（V2 架构）

历史说明：V1 `KeyLevel` / `KeyLevelSnapshot` 与追踪器 `key_level_tracker.py`
已于本次提交整体下线（V1 产线链路在 Commit 2 已弃用，处理器 0 调用）。
`KeyLevelSignal` 同时被 V2 `KeyLevelSnapshotV2.signals` 复用，故保留。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class KeyLevelSignal(BaseModel):
    """关键位产出的交易信号"""

    level_price: float
    side: str  # "support" | "resistance"
    state: str
    action: str
    # "snipe_long" | "snipe_short" | "flip_short" | "flip_long" | "wait_sweep" | "wait_approach"
    confidence: str = "C"  # "A" | "B" | "C"
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)

    # 置信度透明化（小白视线友好）
    # confirmations：本信号通过的确认项清单（方便前端 ✅ chip 链 + 评分透明化）
    #   可取值参考 key_level_tracker_v2._CONFIRMATION_KEYS
    #   如 ["closed_bar", "volume_proactive", "pattern_pin_bar", "sweep_taken",
    #       "retest_done", "continuation", "fake_break_reclaim", "mtf_aligned",
    #       "cvd_aligned"]
    # signal_kind：信号分类，前端据此渲染徽章
    #   如 "snipe_sweep" / "snipe_bounce" / "flip_retest" / "scalp"
    #   / "fake_break_reversal" / "breakout_retest" / "breakout_continuation"
    #   / "wait_approach" / "wait_sweep"
    # score：0-100 置信度分数
    #   base(A=80/B=60/C=40) + 确认项×4（上限 +20） - warnings×3；clamp [0,100]
    confirmations: list[str] = Field(default_factory=list)
    signal_kind: str = ""
    score: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V2 — 多维共振关键位系统
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeyLevelV2(BaseModel):
    """单个关键价位（多维共振 + 生命周期追踪）"""

    price: float
    side: str               # "support" / "resistance"
    category: str = ""      # "strong_support" / "moderate_resistance" / "fib_level" / "pivot" / "ma_cluster" / ...
    sources: list[str] = Field(default_factory=list)
    source_count: int = 0
    confluence_score: float = 0  # 0-100
    strength_tier: str = "C"     # "S" (最强) / "A" / "B" / "C"

    # 状态机
    # idle / approaching / testing / swept / bounced / broken / fake_break / flipped
    state: str = "idle"
    state_ts: int = 0
    prev_state: str = ""
    test_count: int = 0
    sweep_usd: float = 0
    lowest_wick: Optional[float] = None
    break_start_ts: int = 0

    # 级联风险
    cascade_risk: float = 0
    cascade_layers: int = 0
    cascade_total_usd: float = 0

    distance_pct: float = 0

    # V2 新增
    timeframe: str = ""            # 该位最强的时间框架 ("1H"/"4H"/"1D"/"1W")
    first_seen_ts: int = 0
    last_confirmed_ts: int = 0
    note: str = ""                 # 白话说明

    # K 线形态确认（AI 自检建议：暴露 pin bar / engulfing / doji 结构化信号给 AI）
    pattern_detected: str = ""     # "锤子线" / "射击之星" / "看涨吞没" / "看跌吞没" / "十字星" / ""
    pattern_strength: float = 0.0  # 0~1，形态强度（detect_reversal_pattern 输出）

    # Phase 2：历史验证 + 结构屏障 + 最终打分（供"强位卡片"与 tier 评定使用）
    bounce_count: int = 0               # 历史成功反弹/拒绝次数（状态机进入 bounced 累加）
    historical_validity: float = 0.0    # 0~1，由 bounce_count / test_count / sweep_usd 组合得出
    barrier_score: float = 0.0          # 0~20，结构屏障加分（多个清算簇前置、时间存活等）
    final_score: float = 0.0            # 0~100，= confluence_score × 时间衰减 + 历史验证 + 屏障加分

    # Commit 4：质量标注（博主方法论：主动 vs 被动 · 三步确认）
    bounce_quality: str = ""       # "proactive"(主动吸筹) / "passive"(被动触发) / ""(未反弹)
    breakout_stage: int = 0        # 0(未破位) / 1(破位) / 2(回踩) / 3(确认)

    # 假突破反转追踪（2026-04 新增）
    # - fake_break_count：本 level 历史被假突破次数；多次假破 = 防守强度高
    fake_break_count: int = 0


class BullBearLine(BaseModel):
    """多空分界线（独立展示区域）"""

    sma200d: Optional[float] = None
    bmsa_upper: Optional[float] = None   # 20W SMA
    bmsa_lower: Optional[float] = None   # 21W EMA
    ichimoku_cloud_top: Optional[float] = None
    ichimoku_cloud_bottom: Optional[float] = None
    current_regime: str = ""             # "bull" / "bear" / "neutral"
    regime_reason: str = ""


class BreakoutZone(BaseModel):
    """突破蓄力区"""

    bb_squeeze: bool = False
    squeeze_direction: str = ""          # "up" / "down" / "unknown"
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    keltner_upper: Optional[float] = None
    keltner_lower: Optional[float] = None
    note: str = ""


class FibSnapshot(BaseModel):
    """Fibonacci 参考位快照"""

    swing_high: float = 0
    swing_low: float = 0
    direction: str = ""      # "up" / "down"
    levels: list[dict] = Field(default_factory=list)  # [{ratio, price, label}]


class KeyLevelSnapshotV2(BaseModel):
    """关键位系统 V2 完整快照（推送 + 详情页）"""

    ts: int = 0
    current_price: float = 0
    atr: float = 0

    # 核心关键位（不限数量，按距离 + 强度排序）
    levels: list[KeyLevelV2] = Field(default_factory=list)

    # 特殊展示
    bull_bear_line: Optional[BullBearLine] = None
    breakout_zone: Optional[BreakoutZone] = None
    fib_snapshot: Optional[FibSnapshot] = None

    # 信号
    signals: list[KeyLevelSignal] = Field(default_factory=list)
    active_count: int = 0

    # 市场结构摘要
    structure_summary: str = ""
    nearest_strong_support: Optional[float] = None
    nearest_strong_resistance: Optional[float] = None

    # 多周期关键位（日线 / 周线级别最强支撑阻力）
    daily_strong_support: Optional[str] = None
    daily_strong_resistance: Optional[str] = None
    weekly_strong_support: Optional[str] = None
    weekly_strong_resistance: Optional[str] = None
