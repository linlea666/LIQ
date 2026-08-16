"""订单流小时/日桶聚合器（P2 · 零 Coinglass 配额）。

职责：
    每 5 分钟从 CoinState 内存态增量聚合订单流数据进 OrderflowStore：
      1. taker 买卖量：复用 cvd_spot / cvd_contract 的 5m 序列
         （series 覆盖 ~8h，按小时分桶整桶重算，幂等自愈）
      2. 大额挂单被动成交：追踪 large_orders_history / spot_large_orders_history
         各 order id 的 executed_usd_value 增量，归入当前小时桶
      3. 日桶：由小时桶汇总（UTC+8 日界）
      4. 每日触发一次保留期清理

诚实性约定：
      - 小时桶 coverage_pct = 观测 bar 数 / 12，数据断档时 < 1，不插值
      - 序列窗口左边界被截断的小时（可能不完整）不覆写已有完整桶
      - 大单 executed 首次见到的 order 只记基线不计增量
        （避免进程重启后把历史累计误算为当期成交）
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from storage.orderflow_store import OrderflowStore, day_key_cn

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_SEC = 86400
# executed 基线字典的护栏：远超正常规模（每币每市场 ~1500 个 lifecycle）
_MAX_TRACKED_ORDERS = 20000


class OrderflowStatsAggregator:
    """process 内单实例；由 engine 的聚合协程每 5 分钟驱动一次。"""

    def __init__(self, store: OrderflowStore) -> None:
        self._store = store
        # (coin, market) → {order_id: last_executed_usd_value}
        self._exec_baseline: dict[tuple[str, str], dict[int, float]] = {}
        # (coin, market) → 上次 executed flush 时刻（增量归属小时用）
        self._last_exec_flush_ts: dict[tuple[str, str], int] = {}
        self._last_cleanup_ts = time.time()

    # ── 主入口 ─────────────────────────────────────────────────────────

    def flush_coin(self, state: Any) -> None:
        """聚合单个币的当前内存态（调用方保证 state 非空）。"""
        coin = (getattr(state, "ccy", "") or "").upper()
        if not coin:
            return
        try:
            self._flush_taker_hours(coin, "spot", getattr(state, "cvd_spot", None))
            self._flush_taker_hours(coin, "futures", getattr(state, "cvd_contract", None))
            self._flush_large_executed(
                coin, "futures", getattr(state, "large_orders_history", None),
            )
            self._flush_large_executed(
                coin, "spot", getattr(state, "spot_large_orders_history", None),
            )
            self._rollup_recent_days(coin)
        except Exception:
            logger.warning("[orderflow_stats] flush failed | coin=%s", coin, exc_info=True)

        now = time.time()
        if now - self._last_cleanup_ts >= _CLEANUP_INTERVAL_SEC:
            self._last_cleanup_ts = now
            self._store.cleanup()

    # ── taker 小时桶（整桶重算，幂等）─────────────────────────────────

    def _flush_taker_hours(self, coin: str, market: str, cvd: Any) -> None:
        series = list(getattr(cvd, "series", None) or []) if cvd is not None else []
        if not series:
            return
        first_ts = min(int(getattr(p, "ts", 0) or 0) for p in series)

        buckets: dict[int, tuple[float, float, int]] = {}
        for p in series:
            ts = int(getattr(p, "ts", 0) or 0)
            if ts <= 0:
                continue
            hour_ts = ts - ts % 3600
            buy = float(getattr(p, "buy_vol", 0) or 0)
            sell = float(getattr(p, "sell_vol", 0) or 0)
            b, s, n = buckets.get(hour_ts, (0.0, 0.0, 0))
            buckets[hour_ts] = (b + buy, s + sell, n + 1)

        for hour_ts, (buy, sell, samples) in sorted(buckets.items()):
            # 左边界小时可能被序列窗口截断（series 从小时中间开始）：
            # 除非本次观测已是完整 12 bar，否则跳过，避免覆写既有完整桶
            if hour_ts < first_ts and samples < 12:
                continue
            self._store.upsert_taker_hour(
                coin, market, hour_ts, buy, sell, samples,
            )

    # ── 大额挂单被动成交（增量累加）───────────────────────────────────

    def _flush_large_executed(self, coin: str, market: str, lifecycles: Any) -> None:
        items = list(lifecycles or [])
        if not items:
            return
        key = (coin, market)
        baseline = self._exec_baseline.get(key)
        first_run = baseline is None
        if baseline is None:
            baseline = {}
            self._exec_baseline[key] = baseline

        now = int(time.time())
        prev_flush = self._last_exec_flush_ts.get(key)
        self._last_exec_flush_ts[key] = now
        bid_delta = 0.0
        ask_delta = 0.0
        seen_ids: set[int] = set()

        for lo in items:
            oid = int(getattr(lo, "id", 0) or 0)
            if oid <= 0:
                continue
            seen_ids.add(oid)
            executed = float(getattr(lo, "executed_usd_value", 0) or 0)
            prev = baseline.get(oid)
            if prev is not None and executed > prev:
                delta = executed - prev
                if (getattr(lo, "side", "") or "") == "bid":
                    bid_delta += delta
                else:
                    ask_delta += delta
            # 首次见到只记基线（含进程重启场景），不把历史累计算进当期
            baseline[oid] = executed

        # 基线字典护栏：只保留当前快照仍可见的 id（消失的 order 不会再产生增量）
        if len(baseline) > _MAX_TRACKED_ORDERS or len(baseline) > len(seen_ids) * 2:
            for oid in list(baseline.keys()):
                if oid not in seen_ids:
                    del baseline[oid]

        if not first_run and (bid_delta > 0 or ask_delta > 0):
            # 增量产生于 (上次 flush, 本次 flush] 窗口 → 归属窗口起点所在小时，
            # 避免刚跨整点的 flush 把上一小时末尾的成交记入下一小时桶
            attr_ts = prev_flush if prev_flush is not None else now
            hour_ts = attr_ts - attr_ts % 3600
            self._store.add_large_executed(coin, market, hour_ts, bid_delta, ask_delta)

    # ── 日桶汇总 ───────────────────────────────────────────────────────

    def _rollup_recent_days(self, coin: str) -> None:
        """汇总今天 + 昨天（覆盖跨日边界的迟到小时桶）。"""
        now = int(time.time())
        for ts in (now, now - 86400):
            key = day_key_cn(ts)
            for market in ("spot", "futures"):
                self._store.rollup_daily(coin, market, key)


# ── process 单例（engine 与 API 共用）────────────────────────────────

_instance: Optional[OrderflowStatsAggregator] = None


def get_orderflow_aggregator(
    hourly_keep_days: int = 180, daily_keep_days: int = 400,
) -> OrderflowStatsAggregator:
    global _instance
    if _instance is None:
        store = OrderflowStore(
            hourly_keep_days=hourly_keep_days, daily_keep_days=daily_keep_days,
        )
        _instance = OrderflowStatsAggregator(store)
    return _instance


def get_orderflow_store() -> OrderflowStore:
    return get_orderflow_aggregator()._store


def reset_for_test(data_dir: str) -> OrderflowStatsAggregator:
    """仅测试用：用独立目录重建单例。"""
    global _instance
    if _instance is not None:
        _instance._store.close()
    _instance = OrderflowStatsAggregator(OrderflowStore(data_dir=data_dir))
    return _instance
