"""滚仓前瞻窗口扫描 —— 独立于主动作的"预告类"提醒

职责：
  - 识别未来可能影响决策的市场事件（收敛释放 / 关键位临近 / 结构即将确认 / 动能早期拐头）
  - 提供 per-position-per-kind 的频控（默认 30 分钟 1 次）
  - 返回可合并到 RollSignal.forward_windows 的列表

与 roll_position_engine 的关系：
  - engine.evaluate 每次调用后，传入 ForwardScanner 生成前瞻窗口
  - ForwardScanner 跨调用维持"最近一次 emit 的时间戳"，实现频控
  - 测试时可注入自定义实例验证频控行为
"""

from __future__ import annotations

from dataclasses import dataclass

from models.roll_signal import ForwardWindow, SignalRef
from processors.roll_position_engine import MarketContext


FORWARD_KEY_LEVEL_TRIGGER_PCT = 1.5   # 关键位距现价 < 1.5% 时预警
FORWARD_DEFAULT_COOLDOWN_SEC = 30 * 60  # 默认 30 分钟频控


@dataclass
class _EmitKey:
    """频控 key：position_id + ForwardKind 字符串。"""
    position_id: str
    kind: str


class ForwardScanner:
    """跨调用维护 per-(position_id, kind) 的"最近 emit 时间"，实现静默期频控。

    - settings.forward_alert_cooldown_min 决定默认冷却时长；
      不同 kind 可通过 cooldown_overrides 单独设置。
    - 非线程安全（asyncio 单事件循环内顺序调用）。
    """

    def __init__(
        self,
        default_cooldown_sec: int = FORWARD_DEFAULT_COOLDOWN_SEC,
        cooldown_overrides: dict[str, int] | None = None,
    ):
        self.default_cooldown_sec = default_cooldown_sec
        self.cooldown_overrides = cooldown_overrides or {}
        self._last_emit: dict[tuple[str, str], int] = {}

    def set_cooldown(self, kind: str, seconds: int) -> None:
        self.cooldown_overrides[kind] = seconds

    def _cooldown_for(self, kind: str) -> int:
        return self.cooldown_overrides.get(kind, self.default_cooldown_sec)

    def _can_emit(self, position_id: str, kind: str, ts: int) -> bool:
        key = (position_id, kind)
        last = self._last_emit.get(key, 0)
        return (ts - last) >= self._cooldown_for(kind)

    def _mark(self, position_id: str, kind: str, ts: int) -> None:
        self._last_emit[(position_id, kind)] = ts

    def reset(self, position_id: str) -> None:
        self._last_emit = {k: v for k, v in self._last_emit.items() if k[0] != position_id}

    def scan(
        self,
        position_id: str,
        market: MarketContext,
    ) -> list[ForwardWindow]:
        """扫描当前市场，返回 due（通过频控）的前瞻窗口列表。"""
        windows: list[ForwardWindow] = []

        # ── BB 收敛即将释放 ────────────────────────────
        if market.squeeze_state == "pending":
            kind = "squeeze_release_imminent"
            if self._can_emit(position_id, kind, market.ts):
                windows.append(ForwardWindow(
                    kind=kind,
                    ts=market.ts,
                    expires_at=market.ts + 3600,
                    hint_cn="布林带收敛将释放，关注方向突破",
                    related_signals=[SignalRef(
                        source="bb_squeeze", read="pending",
                        weight=0, detail="待释放",
                    )],
                ))
                self._mark(position_id, kind, market.ts)

        # ── 关键位临近 ─────────────────────────────────
        if (
            market.nearest_level
            and market.nearest_level.distance_pct < FORWARD_KEY_LEVEL_TRIGGER_PCT
        ):
            kind = "key_level_approaching"
            if self._can_emit(position_id, kind, market.ts):
                lv = market.nearest_level
                windows.append(ForwardWindow(
                    kind=kind,
                    ts=market.ts,
                    expires_at=market.ts + 1800,
                    hint_cn=(
                        f"关键位 {lv.price:.2f} 距现价 {lv.distance_pct:.2f}%，"
                        f"准备观察反应"
                    ),
                    related_signals=[SignalRef(
                        source=f"key_level_v2#{lv.price:.2f}",
                        read=lv.state,
                        weight=0,
                        detail=f"{lv.kind} conf={lv.confluence_score:.0f}",
                    )],
                ))
                self._mark(position_id, kind, market.ts)

        # ── 动能早期拐头 ───────────────────────────────
        if market.te_overall_state == "exhaustion_warn":
            kind = "exhaustion_early_hint"
            if self._can_emit(position_id, kind, market.ts):
                windows.append(ForwardWindow(
                    kind=kind,
                    ts=market.ts,
                    expires_at=market.ts + 3600,
                    hint_cn="动能开始衰竭，关注后续是否有确认",
                    related_signals=[SignalRef(
                        source="trend_exhaustion", read="exhaustion_warn",
                        weight=0,
                        detail=f"score={market.te_overall_score:.2f}",
                    )],
                ))
                self._mark(position_id, kind, market.ts)

        # ── 结构即将确认（当 direction=transitioning 时） ──
        if market.ms_direction_4h == "transitioning":
            kind = "structure_pending_confirm"
            if self._can_emit(position_id, kind, market.ts):
                windows.append(ForwardWindow(
                    kind=kind,
                    ts=market.ts,
                    expires_at=market.ts + 3600,
                    hint_cn="4H 结构过渡中，等待 BOS/CHoCH 确认",
                    related_signals=[SignalRef(
                        source="market_structure_4h", read="transitioning",
                        weight=0, detail="待确认",
                    )],
                ))
                self._mark(position_id, kind, market.ts)

        return windows
