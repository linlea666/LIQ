"""市场结构（Market Structure）数据模型 —— Wyckoff / SMC 学派

描述：基于 swing high / swing low 识别的价格结构方向，给出：
  - direction: bullish / bearish / ranging / transitioning
  - last_event: BOS (Break of Structure，结构延续) / CHoCH (Change of Character，结构反转)
  - operate_bias: 顺势操作建议
  - structure_high / structure_low: 当前有效结构上下沿

该模型为系统的"共享结构上下文"，被 range_signal、key_level_v2、ai_snapshot 共同消费。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class SwingPoint(BaseModel):
    """对外序列化的摆动点。"""

    ts: int = 0
    price: float
    kind: Literal["high", "low"]
    strength: int = 0  # 对应 fractal lookback，左右各 N 根都不超过


class MarketStructure(BaseModel):
    """市场结构快照。"""

    timeframe: str = "1h"

    direction: Literal["bullish", "bearish", "ranging", "transitioning"] = "ranging"

    swing_highs: list[SwingPoint] = Field(default_factory=list)
    swing_lows: list[SwingPoint] = Field(default_factory=list)

    last_event: Literal["BOS_up", "BOS_down", "CHoCH_up", "CHoCH_down", ""] = ""
    event_ts: int = 0
    event_price: float = 0.0

    structure_high: float = 0.0  # 最近有效 swing high
    structure_low: float = 0.0   # 最近有效 swing low

    operate_bias: Literal[
        "long_only", "short_only", "both_ok", "stand_aside"
    ] = "stand_aside"

    confidence: float = 0.0  # 0~1

    summary: str = ""  # 白话：如 "1h 上升结构，最近 BOS 向上 · 3h 前"
