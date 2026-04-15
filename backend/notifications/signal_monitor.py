"""S 级信号监控：检测关键位 / 箱体模块的高优信号并触发通知。"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from models.key_level import KeyLevelSnapshotV2, KeyLevelV2
    from models.flow import RangeSignalData

logger = logging.getLogger(__name__)

ACTIONABLE_ACTIONS = frozenset({
    "snipe_long", "snipe_short", "flip_long", "flip_short",
})


@dataclass
class AlertEvent:
    """一条待发送的通知事件。"""
    coin: str
    source: str              # "key_level" | "range"
    direction: str           # "long" | "short"
    signal_tier: str         # "S" | "A"
    price: float             # 当前价
    entry: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    rr_ratio: Optional[float] = None
    reason: str = ""
    level_price: Optional[float] = None
    level_state: str = ""    # "swept" | "flipped" | ...
    cascade_risk: float = 0
    warnings: list[str] = field(default_factory=list)
    ts: float = field(default_factory=time.time)

    @property
    def dedup_key(self) -> str:
        if self.source == "key_level":
            return f"{self.coin}:kl:{self.level_price}:{self.level_state}:{self.direction}"
        return f"{self.coin}:range:{self.signal_tier}:{self.direction}"


class AlertDedup:
    """基于冷却时间的去重器。"""

    def __init__(self, cooldown_seconds: int = 1800):
        self.cooldown_seconds = cooldown_seconds
        self._sent: dict[str, float] = {}

    def should_send(self, key: str) -> bool:
        now = time.time()
        last = self._sent.get(key, 0)
        if now - last < self.cooldown_seconds:
            return False
        self._sent[key] = now
        return True

    def cleanup(self, max_age: int = 7200):
        """清理过期条目，防止内存泄漏。"""
        now = time.time()
        expired = [k for k, ts in self._sent.items() if now - ts > max_age]
        for k in expired:
            del self._sent[k]


def _find_level(price: float, levels: list) -> Optional[KeyLevelV2]:
    """从 V2 关键位列表中找到与信号价格匹配的 level。"""
    best = None
    best_dist = float("inf")
    for lv in levels:
        dist = abs(lv.price - price)
        if dist < best_dist:
            best_dist = dist
            best = lv
    if best and best_dist / max(price, 1) < 0.005:
        return best
    return None


def scan_alerts(
    coin: str,
    price: float,
    kl_snapshot: Optional[KeyLevelSnapshotV2],
    range_signal: Optional[RangeSignalData],
    min_tier: str = "S",
    include_key_levels: bool = True,
    include_range: bool = True,
) -> list[AlertEvent]:
    """扫描当前状态，返回所有满足条件的告警事件。"""
    allowed_tiers = {"S"} if min_tier == "S" else {"S", "A"}
    events: list[AlertEvent] = []

    if include_key_levels and kl_snapshot and kl_snapshot.signals:
        for sig in kl_snapshot.signals:
            if sig.action not in ACTIONABLE_ACTIONS:
                continue
            level = _find_level(sig.level_price, kl_snapshot.levels)
            if not level:
                continue
            if level.strength_tier not in allowed_tiers:
                continue
            direction = "long" if "long" in sig.action else "short"
            events.append(AlertEvent(
                coin=coin,
                source="key_level",
                direction=direction,
                signal_tier=level.strength_tier,
                price=price,
                entry=sig.entry_price,
                stop_loss=sig.stop_loss,
                tp1=sig.tp1,
                rr_ratio=sig.rr_ratio,
                reason=sig.reason,
                level_price=sig.level_price,
                level_state=sig.state,
                cascade_risk=level.cascade_risk,
                warnings=sig.warnings,
            ))

    if include_range and range_signal and range_signal.signal_grade:
        if range_signal.signal_grade in allowed_tiers:
            direction = "long" if range_signal.signal_direction == "long" else "short"
            events.append(AlertEvent(
                coin=coin,
                source="range",
                direction=direction,
                signal_tier=range_signal.signal_grade,
                price=price,
                entry=range_signal.signal_entry,
                stop_loss=range_signal.signal_stop_loss,
                tp1=range_signal.signal_tp1,
                rr_ratio=range_signal.signal_rr_ratio,
                reason=range_signal.signal_reason,
            ))

    return events
