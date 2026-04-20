"""趋势动能 / 衰竭 / 反转侦测（Trend Exhaustion）数据模型

设计目标：
    *不*做"顶/底的具体价格点位预测"，只回答三件事：
        1. 当前趋势是 **续航(healthy_continuation)** 还是 **衰竭(momentum_fading / exhaustion_warn)**？
        2. **反转(structural_reversal)** 是否已经启动？
        3. 什么都还看不出来时，诚实返回 **neutral**，不假装有信号。

正交性说明（为什么不合并进 range_signal / key_level_v2）：
    - range_signal 描述"价格相对 MA 骨架的箱体位置"，是 *空间*。
    - key_level_v2 描述"单个关键位的生命周期/攻防"，是 *位点*。
    - trend_exhaustion 描述"趋势推进的内部健康度"，是 *动能*，三者互不蕴含。
      同一个价格点可以：箱体上沿 + 关键位阻力 + 动能衰竭（三独立确认）也可以
      箱体上沿 + 关键位阻力 + 动能健康（续航突破）——结论完全不同。

该模型同时向 WebSocket 前端推送 与 AISnapshot 注入，供规则层 + AI 双通道消费。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

Timeframe = Literal["1h", "4h", "1d"]
ExhaustionState = Literal[
    "healthy_continuation",  # 健康续航：动能强、参与度在跟、尚未出现衰竭
    "momentum_fading",       # 动能衰减：一阶动能开始减速但还未出现结构破坏
    "exhaustion_warn",       # 衰竭警戒：多项衰竭触发器共振（背离 / TD / Fib 扩展到位）
    "structural_reversal",   # 结构反转：已经给出 MTF 侧的反向结构信号
    "neutral",               # 样本不足或信号互相抵消，观望
]
ConsensusLevel = Literal["strong_agree", "partial", "conflict", "neutral"]
OverallAction = Literal[
    "add",            # 顺势加仓
    "hold",           # 持仓不动
    "reduce",         # 减仓
    "close",          # 离场
    "counter_small",  # 小仓逆势试错
    "counter_main",   # 主攻方向已切换
    "stand_aside",    # 观望
]


class SubScore(BaseModel):
    """单个子项读数，用于前端"进阶/专业"模式展开与回测溯源。"""

    key: str                  # 如 "m1_macd_2d", "e2_rsi_div"
    name: str                 # 白话名
    score: float              # -1 ~ +1，+ 代表支持趋势续航 / - 代表支持衰竭
    note: str = ""            # 一句白话解释，如 "MACD 柱 3 根连续收敛"
    value: Optional[float] = None  # 原始读数（便于调试）


class TrendExhaustionState(BaseModel):
    """单周期衰竭状态（1h/4h/1d 各一份）。"""

    tf: Timeframe

    # 三维得分：+1 强续航 / 0 中性 / -1 强衰竭
    momentum_score: float = 0.0       # D1 动能（MACD 二阶 / 价格离 EMA20 σ / RSI 区）
    participation_score: float = 0.0  # D2 参与度（CVD 动能 / OI-Price 共振）
    exhaustion_score: float = 0.0     # D3 衰竭触发器（TD / 背离 / Fib 扩展命中）

    composite_score: float = 0.0      # 三维加权综合，-1~+1

    state: ExhaustionState = "neutral"
    state_age_min: int = 0            # 当前状态持续多少分钟（前端用来判断"刚翻"还是"老状态"）

    # 触发器 + 人类可读解释
    triggers: list[str] = Field(default_factory=list)  # 命中的关键词，如 "rsi_bear_div", "td_setup_9"
    sub_scores: list[SubScore] = Field(default_factory=list)

    action_hint: Literal["add", "hold", "reduce", "close", "counter_small", "stand_aside"] = "stand_aside"
    reason_cn: str = ""               # 一句小白都能懂的话


class TrendExhaustionSignal(BaseModel):
    """三周期 + MTF 共识的完整输出，直接推给前端 / 注入 AISnapshot。"""

    coin: str
    ts: int

    tf_1h: Optional[TrendExhaustionState] = None
    tf_4h: Optional[TrendExhaustionState] = None
    tf_1d: Optional[TrendExhaustionState] = None

    # ── MTF 共识总评（小白最终看这个） ─────────────────────────────
    consensus_level: ConsensusLevel = "neutral"
    overall_state: ExhaustionState = "neutral"
    overall_action: OverallAction = "stand_aside"
    overall_position_pct: float = 0.0   # 建议仓位占比 0~1（仅为参考）
    overall_reason_cn: str = ""         # 一句白话，如 "4h/1d 共振衰竭，1h 仍在最后一冲"

    # ── 调试 / 日志 ──────────────────────────────────────────────
    data_quality: Literal["ok", "partial", "insufficient"] = "insufficient"
    missing_inputs: list[str] = Field(default_factory=list)
