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
    "snipe_long", "snipe_short",
    "flip_long", "flip_short",
    "scalp_long", "scalp_short",   # 日内极小止损档
})

# 兜底：scalp 信号用 signal.confidence 映射到 tier（scalp 不依赖 level.strength_tier 的 S/A 阈值，
# 因为 scalp 在引擎层已经做了 S/A 过滤，这里若 _find_level 因浮点误差失败则不应丢信号）
_SCALP_ACTIONS = frozenset({"scalp_long", "scalp_short"})


@dataclass
class AlertEvent:
    """一条待发送的通知事件。"""
    coin: str
    source: str              # "key_level" | "range"
    direction: str           # "long" | "short"
    signal_tier: str         # "S" | "A"
    price: float             # 当前价
    action: str = ""         # "snipe_long" / "scalp_long" / "flip_short" / "" (range 信号)
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
    def is_scalp(self) -> bool:
        return self.action in _SCALP_ACTIONS

    @property
    def dedup_key(self) -> str:
        if self.source == "key_level":
            # scalp 与 snipe/flip 即使关键位相同也应分别去重（时间尺度不同）
            kind = "scalp" if self.is_scalp else "kl"
            return f"{self.coin}:{kind}:{self.level_price}:{self.level_state}:{self.direction}"
        return f"{self.coin}:range:{self.signal_tier}:{self.direction}"


class AlertDedup:
    """基于冷却时间的去重器。

    拆分 should_send / mark_sent 的原因（P1 可靠性修复）：
    旧实现 should_send 成功时直接把 now 写入 _sent，但调用方 send_alert_email
    若 SMTP 暂时失败（配置缺失 / 网络抖动）会返回 False，此时冷却位已被占用，
    导致同一 dedup_key 静默锁定整个 cooldown 窗口（默认 45 分钟），
    用户观感是"S 级信号有了但收不到邮件"。现在强制调用方必须在发送成功后
    显式 mark_sent，失败时允许下一轮立即重试。
    """

    def __init__(self, cooldown_seconds: int = 1800):
        self.cooldown_seconds = cooldown_seconds
        self._sent: dict[str, float] = {}

    def should_send(self, key: str) -> bool:
        """纯查询：是否超出冷却窗口可以发送。不产生副作用。"""
        last = self._sent.get(key, 0)
        return (time.time() - last) >= self.cooldown_seconds

    def mark_sent(self, key: str) -> None:
        """记录一次"成功送达"的时间戳。调用方必须在 send 返回 True 后调用。"""
        self._sent[key] = time.time()

    def cleanup(self, max_age: int = 7200):
        """清理过期条目，防止内存泄漏。"""
        now = time.time()
        expired = [k for k, ts in self._sent.items() if now - ts > max_age]
        for k in expired:
            del self._sent[k]


def _find_level(price: float, levels: list, max_dist_pct: float = 0.008) -> Optional[KeyLevelV2]:
    """从 V2 关键位列表中找到与信号价格匹配的 level。

    容差从 0.5% 放宽到 0.8%，避免浮点误差 / 动态重算导致 level_price 与 snapshot 中
    对应 level 微小偏差时反查失败而静默丢信号。
    """
    best = None
    best_dist = float("inf")
    for lv in levels:
        dist = abs(lv.price - price)
        if dist < best_dist:
            best_dist = dist
            best = lv
    if best and best_dist / max(price, 1) < max_dist_pct:
        return best
    return None


def scan_alerts(
    coin: str,
    price: float,
    kl_snapshot: Optional[KeyLevelSnapshotV2],
    range_signal: Optional[RangeSignalData],
    min_tier: str = "A",
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

            # tier 判定：
            #  - scalp：一律以 sig.confidence 为准。scalp 的置信度是生成器按
            #    "S级 level + 强形态(≥0.8) → A，其余 → B" 精算出来的；
            #    若用 level.strength_tier 会夸大强度（如 S 级 level + 弱吞没本应 B，
            #    却被误报为 S），邮件标题就会误导交易员。
            #  - snipe/flip：以 level.strength_tier 为准（保留原语义）。
            if sig.action in _SCALP_ACTIONS:
                if not (sig.entry_price and sig.stop_loss and sig.tp1):
                    continue  # scalp 参数不全，跳过
                tier = sig.confidence if sig.confidence in ("S", "A", "B") else "B"
                cascade = level.cascade_risk if level else 0.0
            elif level:
                tier = level.strength_tier
                cascade = level.cascade_risk
            else:
                continue  # 非 scalp 且找不到 level，放弃以免发错价

            if tier not in allowed_tiers:
                continue

            direction = "long" if "long" in sig.action else "short"
            events.append(AlertEvent(
                coin=coin,
                source="key_level",
                direction=direction,
                signal_tier=tier,
                price=price,
                action=sig.action,
                entry=sig.entry_price,
                stop_loss=sig.stop_loss,
                tp1=sig.tp1,
                rr_ratio=sig.rr_ratio,
                reason=sig.reason,
                level_price=sig.level_price,
                level_state=sig.state,
                cascade_risk=cascade,
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
