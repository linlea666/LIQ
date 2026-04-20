"""统一信号契约 CandidateSignal

职责：
  - 所有信号 producer（tracker_v2 / levels / range_signal / ai / news_event）
    产出的候选信号都通过此契约进入 SignalBus。
  - Synthesizer 只消费 CandidateSignal，不关心源头差异。

复用策略：
  - 现有 KeyLevelSignal / RangeSignalData 等**保持不动**（向后兼容）
  - 在 engine._recompute 末尾，由 adapter 将现有信号转成 CandidateSignal
  - 未来新 producer 直接产出 CandidateSignal，无需 adapter

与 KeyLevelSignal 的区别：
  - KeyLevelSignal 是 V2 关键位系统内部的信号
  - CandidateSignal 是**跨模块的统一契约**，包含来源/置信度/溯源
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from models.common_enums import Direction, SignalTier, TradingAction


class CandidateSignal(BaseModel):
    """候选信号 —— SignalBus 的原子单位"""

    # ── 来源溯源 ──
    source: str
    # 取值约定：
    #   "tracker_v2.swept" / "tracker_v2.bounced" / "tracker_v2.scalp"
    #   "range_signal.breakout" / "range_signal.return"
    #   "levels.sniper" / "levels.ladder"
    #   "news_event.bullish" / "news_event.bearish"
    #   "geo_risk.escalation"
    source_id: str = ""             # 可追溯的原始 id（例如 event_id / level_price）
    ts: int = 0                     # 秒级

    # ── 信号本体 ──
    action: TradingAction = "wait"
    direction: Direction = "neutral"

    anchor_price: float = 0.0       # 信号关联的关键价位（入场参考）
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None

    # ── 强度与置信 ──
    confidence: SignalTier = "C"    # A/B/C 置信度分档
    score: float = 0.0              # 0-100 原始分（各 source 自评）

    # ── 解释与警告 ──
    reason: str = ""                # 触发理由（白话）
    warnings: list[str] = Field(default_factory=list)

    # ── 时效 ──
    expires_at: Optional[int] = None  # 秒级，过期自动淘汰

    # ── 溯源原始数据 ──
    # 保存该信号产生时的关键输入快照（供回测 / debug）
    provenance: dict[str, Any] = Field(default_factory=dict)
    # 约定键名：
    #   tracker_v2 → {"level_price": ..., "state": ..., "cascade_risk": ...}
    #   news_event → {"event_id": ..., "theme": ..., "impact_score": ...}
    #   geo_risk   → {"theme_id": ..., "level_before": ..., "level_after": ...}
