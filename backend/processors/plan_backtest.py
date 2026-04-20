"""D04 · ExecutionPlan 回测闭环（最小版）

职责：
    1. 消费 `SignalSynthesizer` 产出的 ExecutionPlan
    2. 去重记录"已开仓 trade"（entry zone / direction / tier 组合 60min 内视作同一单）
    3. 每次 tick 用 current_price 检查是否触达 stop_loss / tp1
    4. 暴露 `get_stats(coin, *, action, tier)` 返回 BacktestStats，供
       `signal_synthesizer.synthesize(..., backtest_hint=...)` 计算 backtest_bonus
    5. 持久化到 backend/data/plan_backtest.json，进程重启不丢

设计说明：
    - 内存字典优先 + JSON 快照（轻量，每 60 次 track 落盘一次）
    - 只统计 traffic_light in {green, yellow} 且 action in {long, short} 的 plan
    - "触发"定义：价格到达 entry_zone 内（long: price<=high；short: price>=low）
    - "解决"定义：触发后价格先后到 stop_loss 或 tp1（优先 SL 保守）
    - 60min 超时未触发 → pending；保留 7 天后 GC
    - 去重 key：(coin, direction, round(entry_mid, decimals_by_coin))

与 D04 DecisionTracker 联动：
    - 每次 stats() 调用更新 sample_size / win_rate / last_tick_ts
    - 用 `[D04]` 前缀记录决定性事件（new trade / resolved）
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_DEDUP_WINDOW_SEC = 60 * 60              # 同一 trade 60min 内不重复计数
_TRIGGER_TIMEOUT_SEC = 6 * 60 * 60       # 6h 内未触发视为 expired
_RESOLVED_KEEP_SEC = 7 * 24 * 60 * 60    # 已结算交易保留 7 天
_PERSIST_EVERY_N = 20                    # 每 N 次 tick 落盘一次
_DATA_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "plan_backtest.json",
)

# 价格去重精度（按 coin 的小数位）
_ENTRY_DECIMALS = {"BTC": 0, "ETH": 1, "SOL": 2}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 内部数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class _Trade:
    trade_id: str
    coin: str
    direction: str            # "long" / "short"
    tier: str                 # "S"/"A"/"B"/"C"
    entry_low: float
    entry_high: float
    stop_loss: Optional[float]
    tp1: Optional[float]
    rr: Optional[float]
    created_ts: int
    triggered_ts: Optional[int] = None
    resolved_ts: Optional[int] = None
    outcome: str = "pending"  # pending / expired / tp1 / sl
    extremum_after: Optional[float] = None  # 预留给 P1 的时间止损

    def to_dict(self) -> dict:
        return {
            "trade_id": self.trade_id,
            "coin": self.coin,
            "direction": self.direction,
            "tier": self.tier,
            "entry_low": self.entry_low,
            "entry_high": self.entry_high,
            "stop_loss": self.stop_loss,
            "tp1": self.tp1,
            "rr": self.rr,
            "created_ts": self.created_ts,
            "triggered_ts": self.triggered_ts,
            "resolved_ts": self.resolved_ts,
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "_Trade":
        return cls(
            trade_id=str(d.get("trade_id", "")),
            coin=str(d.get("coin", "")),
            direction=str(d.get("direction", "")),
            tier=str(d.get("tier", "C")),
            entry_low=float(d.get("entry_low", 0)),
            entry_high=float(d.get("entry_high", 0)),
            stop_loss=d.get("stop_loss"),
            tp1=d.get("tp1"),
            rr=d.get("rr"),
            created_ts=int(d.get("created_ts", 0)),
            triggered_ts=d.get("triggered_ts"),
            resolved_ts=d.get("resolved_ts"),
            outcome=str(d.get("outcome", "pending")),
        )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# PlanBacktestStore
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class PlanBacktestStore:
    """数学引擎 ExecutionPlan 的回测记录仓"""

    def __init__(self, data_file: str = _DATA_FILE) -> None:
        self._lock = threading.RLock()
        self._data_file = data_file
        # coin -> list[_Trade]（活跃 + 近期已结算，GC 后裁剪）
        self._trades: dict[str, list[_Trade]] = {}
        self._tick_counter = 0
        self._load_from_disk()

    # ── 持久化 ─────────────────────────────────────────────

    def _load_from_disk(self) -> None:
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for coin, lst in (raw.get("trades") or {}).items():
                self._trades[coin] = [_Trade.from_dict(d) for d in lst if isinstance(d, dict)]
            logger.info(
                "[D04] plan_backtest loaded | coins=%s total=%d",
                list(self._trades.keys()),
                sum(len(v) for v in self._trades.values()),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[D04] plan_backtest load failed: %s", e)

    def _persist_to_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
            payload = {
                "trades": {
                    coin: [t.to_dict() for t in trades]
                    for coin, trades in self._trades.items()
                },
                "ts": int(time.time()),
            }
            tmp = self._data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._data_file)
        except (OSError, TypeError) as e:
            logger.debug("[D04] plan_backtest persist failed: %s", e)

    # ── 去重 key ───────────────────────────────────────────

    @staticmethod
    def _make_trade_id(coin: str, direction: str, entry_mid: float) -> str:
        decimals = _ENTRY_DECIMALS.get(coin.upper(), 2)
        rounded = round(entry_mid, decimals) if decimals >= 0 else int(entry_mid)
        return f"{coin.upper()}|{direction}|{rounded}"

    # ── 核心：记录 + tick ──────────────────────────────────

    def track(self, plan, *, current_price: float) -> None:
        """
        每次 _recompute 结束后调用：若 plan 符合条件则记录/更新轨迹。

        - plan: models.execution_plan.ExecutionPlan
        - current_price: 当前 recompute 时的价格（用于 SL/TP 触发判定）
        """
        try:
            if plan is None:
                return
            action = getattr(plan, "action", None)
            light = getattr(plan, "traffic_light", None)
            if action not in ("long", "short"):
                return
            if light not in ("green", "yellow"):
                return
            coin = str(getattr(plan, "coin", "")).upper()
            if not coin:
                return

            entry_low = getattr(plan, "entry_zone_low", None)
            entry_high = getattr(plan, "entry_zone_high", None)
            sl = getattr(plan, "stop_loss", None)
            tp1 = getattr(plan, "tp1", None)
            if entry_low is None or entry_high is None:
                return
            entry_mid = (float(entry_low) + float(entry_high)) / 2.0

            with self._lock:
                trade_id = self._make_trade_id(coin, action, entry_mid)
                bucket = self._trades.setdefault(coin, [])
                now = int(time.time())

                # 1) 尝试找到同 id 的活跃 trade（去重 + tick）
                live: Optional[_Trade] = None
                for t in bucket:
                    if t.trade_id == trade_id and t.outcome == "pending":
                        if now - t.created_ts <= _DEDUP_WINDOW_SEC + _TRIGGER_TIMEOUT_SEC:
                            live = t
                            break

                # 2) 新 trade：仅当无活跃同 id 时记录
                if live is None:
                    # 避免 entry_low > entry_high（极端情况）
                    lo, hi = float(min(entry_low, entry_high)), float(max(entry_low, entry_high))
                    trade = _Trade(
                        trade_id=trade_id,
                        coin=coin,
                        direction=str(action),
                        tier=str(getattr(plan, "tier_hint", "C")),
                        entry_low=lo,
                        entry_high=hi,
                        stop_loss=float(sl) if sl is not None else None,
                        tp1=float(tp1) if tp1 is not None else None,
                        rr=float(getattr(plan, "rr_ratio", 0) or 0) or None,
                        created_ts=now,
                    )
                    bucket.append(trade)
                    logger.info(
                        "[D04] new trade logged | %s | entry=[%.2f~%.2f] sl=%s tp1=%s tier=%s",
                        trade_id, lo, hi, sl, tp1, trade.tier,
                    )
                    live = trade

                # 3) tick：判断是否触发 + 是否结算
                self._advance(live, current_price, now)

                # 4) 每次 track 都静默上报 D04（及时反映样本增长）
                self._mark_tracker_locked()

                # 周期 GC + 持久化（降低磁盘 IO）
                self._tick_counter += 1
                if self._tick_counter % _PERSIST_EVERY_N == 0:
                    self._gc_locked()
                    self._persist_to_disk()
        except Exception:
            logger.debug("[D04] plan_backtest.track failed", exc_info=True)

    def _advance(self, trade: _Trade, price: float, now: int) -> None:
        """推进单个 trade 的生命周期（需已持锁）"""
        if trade.outcome != "pending":
            return

        # a) 触发判定
        if trade.triggered_ts is None:
            if trade.direction == "long" and price <= trade.entry_high:
                trade.triggered_ts = now
            elif trade.direction == "short" and price >= trade.entry_low:
                trade.triggered_ts = now
            elif now - trade.created_ts > _TRIGGER_TIMEOUT_SEC:
                trade.outcome = "expired"
                trade.resolved_ts = now
                logger.info(
                    "[D04] trade expired (no trigger) | %s",
                    trade.trade_id,
                )
                return

        # b) 已触发：SL/TP1
        if trade.triggered_ts is not None:
            if trade.direction == "long":
                if trade.stop_loss is not None and price <= trade.stop_loss:
                    trade.outcome = "sl"
                    trade.resolved_ts = now
                elif trade.tp1 is not None and price >= trade.tp1:
                    trade.outcome = "tp1"
                    trade.resolved_ts = now
            else:  # short
                if trade.stop_loss is not None and price >= trade.stop_loss:
                    trade.outcome = "sl"
                    trade.resolved_ts = now
                elif trade.tp1 is not None and price <= trade.tp1:
                    trade.outcome = "tp1"
                    trade.resolved_ts = now

            if trade.outcome in ("sl", "tp1"):
                logger.info(
                    "[D04] trade resolved | %s | outcome=%s price=%.2f",
                    trade.trade_id, trade.outcome, price,
                )

    def _gc_locked(self) -> None:
        """清理 7 天前已结算 trade（需已持锁）"""
        cutoff = int(time.time()) - _RESOLVED_KEEP_SEC
        for coin, bucket in self._trades.items():
            self._trades[coin] = [
                t for t in bucket
                if t.outcome == "pending" or (t.resolved_ts or 0) >= cutoff
            ]

    def _mark_tracker_locked(self) -> None:
        """向 DecisionTracker 上报 D04 状态（需已持锁）"""
        try:
            from utils.decision_tracker import D, get_tracker
            total = sum(len(v) for v in self._trades.values())
            resolved = sum(
                1 for v in self._trades.values() for t in v if t.outcome in ("tp1", "sl")
            )
            tp1_hit = sum(
                1 for v in self._trades.values() for t in v if t.outcome == "tp1"
            )
            win_rate = (tp1_hit / resolved) if resolved else 0.0
            get_tracker().mark(
                D.D04_BACKTEST_LOOP,
                status="ok" if resolved >= 10 else "warn",
                log=False,
                total_trades=total,
                resolved=resolved,
                tp1_hit=tp1_hit,
                win_rate=round(win_rate, 4),
            )
        except Exception:
            logger.debug("[D04] mark tracker failed", exc_info=True)

    # ── 查询 API ───────────────────────────────────────────

    def get_stats(
        self,
        coin: str,
        *,
        action: Optional[str] = None,
        tier: Optional[str] = None,
    ):
        """返回 BacktestStats（供 synthesizer 的 backtest_hint 使用）"""
        from models.snapshot import BacktestStats
        coin = coin.upper()
        with self._lock:
            bucket = list(self._trades.get(coin, []))

        if action:
            bucket = [t for t in bucket if t.direction == action]
        if tier:
            bucket = [t for t in bucket if t.tier == tier]

        total = len(bucket)
        triggered = sum(1 for t in bucket if t.triggered_ts is not None)
        tp1_hit = sum(1 for t in bucket if t.outcome == "tp1")
        sl_hit = sum(1 for t in bucket if t.outcome == "sl")
        pending = sum(1 for t in bucket if t.outcome == "pending")
        resolved = tp1_hit + sl_hit
        rr_vals = [float(t.rr) for t in bucket if t.rr]

        recent = [
            {
                "ts": t.created_ts,
                "trade_id": t.trade_id,
                "direction": t.direction,
                "tier": t.tier,
                "entry_low": t.entry_low,
                "entry_high": t.entry_high,
                "sl": t.stop_loss,
                "tp1": t.tp1,
                "outcome": t.outcome,
            }
            for t in sorted(bucket, key=lambda x: x.created_ts, reverse=True)[:20]
        ]

        return BacktestStats(
            coin=coin,
            ts=int(time.time()),
            total_signals=total,
            triggered=triggered,
            tp1_hit=tp1_hit,
            sl_hit=sl_hit,
            pending=pending,
            win_rate=round(tp1_hit / resolved, 4) if resolved else 0.0,
            avg_rr=round(sum(rr_vals) / len(rr_vals), 2) if rr_vals else 0.0,
            by_tier={},
            by_direction={},
            by_source={},
            recent_signals=recent,
        )

    def snapshot_dict(self, coin: Optional[str] = None) -> dict:
        """提供给调试 endpoint 的全量 JSON"""
        with self._lock:
            if coin:
                coin_u = coin.upper()
                return {coin_u: [t.to_dict() for t in self._trades.get(coin_u, [])]}
            return {c: [t.to_dict() for t in trades] for c, trades in self._trades.items()}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_store: Optional[PlanBacktestStore] = None
_store_lock = threading.Lock()


def get_plan_backtest_store() -> PlanBacktestStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = PlanBacktestStore()
    return _store


def reset_plan_backtest_store_for_test() -> None:
    """仅单元测试用"""
    global _store
    with _store_lock:
        _store = None
