"""关键位生命周期追踪数据模型（V1 保留兼容 + V2 新架构）"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V1 (保留，供旧链路兼容)
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeyLevel(BaseModel):
    """单个关键价位及其生命周期状态"""

    price: float
    side: str  # "support" | "resistance"
    sources: list[str]
    strength: int = 1  # 1-5, 来源数量决定

    # 状态机
    state: str = "idle"
    # idle / approaching / testing / swept / bounced / broken / flipped
    state_ts: int = 0  # 进入当前状态的时间戳
    prev_state: str = ""

    # 测试 / 扫取指标
    test_count: int = 0
    sweep_usd: float = 0
    lowest_wick: Optional[float] = None  # 测试期间的最低（支撑）或最高（阻力）价
    break_start_ts: int = 0  # 首次突破的时间（用于确认倒计时）

    # 级联风险
    cascade_risk: float = 0  # 0-1
    cascade_layers: int = 0  # 下方/上方还有几层清算簇
    cascade_total_usd: float = 0

    # 距当前价
    distance_pct: float = 0


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


class KeyLevelSnapshot(BaseModel):
    """一个币种的关键位追踪快照（推送到前端 + AI）"""

    ts: int
    levels: list[KeyLevel] = []
    signals: list[KeyLevelSignal] = []
    active_count: int = 0  # 非 idle 的数量


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
