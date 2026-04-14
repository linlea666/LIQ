"""关键位生命周期追踪数据模型"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


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
    rr_ratio: Optional[float] = None
    reason: str = ""
    warnings: list[str] = []


class KeyLevelSnapshot(BaseModel):
    """一个币种的关键位追踪快照（推送到前端 + AI）"""

    ts: int
    levels: list[KeyLevel] = []
    signals: list[KeyLevelSignal] = []
    active_count: int = 0  # 非 idle 的数量
