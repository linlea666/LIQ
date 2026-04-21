"""规则引擎 8 维方向共识汇总模型（DirectionVoteSummary）

定位
----
本模块**不是**一个新的预测器，而是把现有规则引擎里 8 个独立方向维度
（结构 / MTF / 动能 / 箱体 / 关键位 / 资金流 / 持仓与情绪 / 动能衰竭）
的**既有读数**统一归一化为 {bullish, bearish, neutral} + 强度 [0,1]，
再按固定权重加权，给 AI / 前端一份「规则侧一眼能看懂的整体倾向」。

为什么要做这一层
----------------
- AI 现在要从 30+ 字段里自己"点数"，易错、易被单维噪声带偏。
- 规则引擎本身各维度分散，没有统一口径告诉 AI：「8 维里有几维看多、几维看空、
  加权得分是多少、互相冲突严重吗」。
- 有了 DirectionVoteSummary，AI 在 Prompt §9k 一眼看到"规则共识"后，可以：
  1) 在与规则大方向一致时，把票数当作加分项提高 conviction；
  2) 在自己想与规则相反时，必须在 §十 终审员声明里显式列出证据（防止随手翻盘）。

正交性
------
- 不吃 range_signal 以外的"结构方向"硬判（市场结构方向交给 market_structure）。
- 不产生任何订单/价格/止损字段，纯粹做"投票"。
- 不替代 trend_exhaustion / range_signal / market_structure，仅聚合它们。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

# 8 维方向投票使用统一三值枚举（与 market_structure / trend_exhaustion 对齐）
VoteDirection = Literal["bullish", "bearish", "neutral"]

# 共识级别
#   strong_agree：同向 ≥5 维 且加权得分绝对值 ≥0.5
#   partial    ：同向 3-4 维 且绝对值 ≥0.25
#   conflict   ：至少 2 维看多 同时 2 维看空（严重对冲）
#   low_signal ：其他情况（绝大多数维度 neutral / 样本不足）
ConsensusLevel = Literal["strong_agree", "partial", "conflict", "low_signal"]


class DirectionVote(BaseModel):
    """单维投票结果（1 票）。"""

    key: str                    # "structure" / "mtf_align" / "momentum" / "range" /
                                # "key_level" / "flow" / "positioning" / "exhaustion"
    name_cn: str                # 白话名（AI 可读、前端可展示）
    direction: VoteDirection = "neutral"
    strength: float = 0.0       # 0 ~ 1，衡量该维读数的"把握度"
    weight: float = 0.0         # 该维在聚合中的默认权重（0~1）
    note: str = ""              # 一句白话解释：是什么读数导致当前方向/强度


class DirectionVoteSummary(BaseModel):
    """8 维方向共识的最终产物（注入 AISnapshot，供 AI Prompt §9k 渲染）。"""

    coin: str
    ts: int

    # 明细（保持稳定顺序，便于前端表格 & AI 阅读）
    votes: list[DirectionVote] = Field(default_factory=list)

    # 聚合结果
    bullish_votes: int = 0        # direction=="bullish" 的维度数
    bearish_votes: int = 0        # direction=="bearish" 的维度数
    neutral_votes: int = 0
    weighted_score: float = 0.0   # Σ(sign × strength × weight)，-1 ~ +1
    dominant_direction: VoteDirection = "neutral"
    consensus_level: ConsensusLevel = "low_signal"

    # 贡献最大的 2 维（正向 / 反向），便于 AI 在 §十 引用证据
    top_bullish: list[str] = Field(default_factory=list)   # key 列表（最多 2）
    top_bearish: list[str] = Field(default_factory=list)

    # 调试字段：参与计算的维度数（有数据）与缺失维度
    active_dimensions: int = 0
    missing_dimensions: list[str] = Field(default_factory=list)

    # 一句话白话（前端卡片 / AI 直接读）
    summary_cn: str = ""
