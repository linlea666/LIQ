"""流动性墙引擎运行时监控指标（W1-T1）。

核心目标（出自审计报告 P1-1）：
    回答"我们的稳态需求 31-33 calls/min vs 限速器 8-10/min 是否真的造成排队、
    数据 stale、Coinbase 异常"——上线前必须可观测，否则只能事后猜测。

定位：
    process-singleton 聚合器，零外部依赖（不读写磁盘 / 不上报 Prometheus），
    只在内存里维护滑动 30min 样本；通过 GET /api/liquidity-wall/metrics 暴露。
    后续若上 Prometheus，把 snapshot() 的 dict 转成 metric family 即可。

数据流：
    Coinglass FixedIntervalLimiter.acquire() →    record_coinglass_queue_wait(ms)
    Coinglass _request(path) 完成              →    record_coinglass_call(endpoint, ok)
    Coinbase fetch_orderbook 完成              →    record_coinbase_call(latency_ms, ok)
    compute_pressure_snapshot 完成             →    record_data_quality(coin, quality)

铁律：
    监控写入路径必须**只 best-effort**——任何异常都被吞掉，绝不影响主轮询。
"""
from __future__ import annotations

import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from time import time as _now
from typing import Optional

logger = logging.getLogger(__name__)

# 滑动窗口大小：30min
_WINDOW_SEC = 1800
_LATENCY_SAMPLE_CAP = 2000      # 30min × 平均 1/s 上限 ≈ 1800
_DQ_SAMPLE_CAP = 600            # 30min × 5min 粒度 ≈ 360（留余量）
_ERROR_TS_CAP = 500


@dataclass
class _LatencySamples:
    """滑动 30min 窗口的延迟/排队样本，O(1) 推入；p50/p95 按需现算。"""
    samples: deque = field(default_factory=lambda: deque(maxlen=_LATENCY_SAMPLE_CAP))

    def push(self, latency_ms: float) -> None:
        self.samples.append((_now(), float(latency_ms)))

    def _within_window(self) -> list[float]:
        cutoff = _now() - _WINDOW_SEC
        # 只读时不修改 samples（避免与并发推入打架），就地过滤后排序
        return sorted(v for ts, v in self.samples if ts >= cutoff)

    def p50(self) -> float:
        vals = self._within_window()
        if not vals:
            return 0.0
        return vals[len(vals) // 2]

    def p95(self) -> float:
        vals = self._within_window()
        if not vals:
            return 0.0
        idx = min(len(vals) - 1, int(len(vals) * 0.95))
        return vals[idx]

    def count(self) -> int:
        cutoff = _now() - _WINDOW_SEC
        return sum(1 for ts, _ in self.samples if ts >= cutoff)


@dataclass
class _DataQualityHistory:
    """data_quality 跳变历史：滑动 30min，统计 stale/missing 占比。"""
    states: deque = field(default_factory=lambda: deque(maxlen=_DQ_SAMPLE_CAP))

    def push(self, quality: str) -> None:
        self.states.append((_now(), quality))

    def stale_ratio(self) -> float:
        """returns 占比 ∈ [0, 1]：stale + missing 帧 / 总帧数。"""
        cutoff = _now() - _WINDOW_SEC
        recent = [q for ts, q in self.states if ts >= cutoff]
        if not recent:
            return 0.0
        bad = sum(1 for q in recent if q in ("stale", "missing"))
        return bad / len(recent)

    def latest(self) -> str:
        if not self.states:
            return ""
        return self.states[-1][1]


class LiquidityWallMetrics:
    """全局监控聚合器（process-singleton）。线程安全：所有写入用 RLock。"""

    def __init__(self):
        self._lock = threading.RLock()
        # Coinglass
        self._cg_queue_wait = _LatencySamples()
        self._cg_calls_by_endpoint: dict[str, int] = {}
        self._cg_errors_by_endpoint: dict[str, int] = {}
        self._cg_last_success_by_endpoint: dict[str, float] = {}
        # Coinbase
        self._cb_latency = _LatencySamples()
        self._cb_total: int = 0
        self._cb_errors: deque = deque(maxlen=_ERROR_TS_CAP)
        # data_quality（per coin）
        self._dq_by_coin: dict[str, _DataQualityHistory] = {}

    # ── 写入入口 ───────────────────────────────────────────────────────

    def record_coinglass_queue_wait(self, wait_ms: float) -> None:
        try:
            with self._lock:
                self._cg_queue_wait.push(max(0.0, wait_ms))
        except Exception:
            logger.debug("metrics: queue_wait push failed", exc_info=True)

    def record_coinglass_call(self, endpoint: str, ok: bool) -> None:
        if not endpoint:
            return
        try:
            with self._lock:
                self._cg_calls_by_endpoint[endpoint] = (
                    self._cg_calls_by_endpoint.get(endpoint, 0) + 1
                )
                if ok:
                    self._cg_last_success_by_endpoint[endpoint] = _now()
                else:
                    self._cg_errors_by_endpoint[endpoint] = (
                        self._cg_errors_by_endpoint.get(endpoint, 0) + 1
                    )
        except Exception:
            logger.debug("metrics: cg call push failed", exc_info=True)

    def record_coinbase_call(self, latency_ms: float, ok: bool) -> None:
        try:
            with self._lock:
                self._cb_total += 1
                if ok:
                    self._cb_latency.push(latency_ms)
                else:
                    self._cb_errors.append(_now())
        except Exception:
            logger.debug("metrics: cb call push failed", exc_info=True)

    def record_data_quality(self, coin: str, quality: str) -> None:
        if not coin or not quality:
            return
        try:
            with self._lock:
                self._dq_by_coin.setdefault(
                    coin.upper(), _DataQualityHistory()
                ).push(quality)
        except Exception:
            logger.debug("metrics: data_quality push failed", exc_info=True)

    # ── 读取入口 ───────────────────────────────────────────────────────

    def coinbase_error_rate_30min(self) -> float:
        with self._lock:
            cutoff = _now() - _WINDOW_SEC
            recent_err = sum(1 for t in self._cb_errors if t >= cutoff)
            total = self._cb_total
        return recent_err / max(1, total)

    def source_age_by_endpoint(self) -> dict[str, int]:
        now = _now()
        with self._lock:
            return {
                ep: int(max(0, now - ts))
                for ep, ts in self._cg_last_success_by_endpoint.items()
            }

    def snapshot(self, coin: Optional[str] = None) -> dict:
        """单点快照——给 API 返回。

        参数 coin 可选：传入则同时返回该币的 stale_ratio / latest data_quality；
        不传则返回 by_coin 字典，便于大屏一次取齐。
        """
        with self._lock:
            cg_p50 = self._cg_queue_wait.p50()
            cg_p95 = self._cg_queue_wait.p95()
            cg_calls = dict(self._cg_calls_by_endpoint)
            cg_errors = dict(self._cg_errors_by_endpoint)
            cb_p50 = self._cb_latency.p50()
            cb_p95 = self._cb_latency.p95()
            cb_total = self._cb_total
            dq_by_coin_copy = self._dq_by_coin

            out = {
                "ts": int(_now()),
                "window_sec": _WINDOW_SEC,
                # Coinglass
                "coinglass_queue_wait_ms_p50": round(cg_p50, 1),
                "coinglass_queue_wait_ms_p95": round(cg_p95, 1),
                "coinglass_call_count_by_endpoint": cg_calls,
                "coinglass_error_count_by_endpoint": cg_errors,
                "source_age_by_endpoint_sec": self.source_age_by_endpoint(),
                # Coinbase
                "coinbase_latency_ms_p50": round(cb_p50, 1),
                "coinbase_latency_ms_p95": round(cb_p95, 1),
                "coinbase_error_rate_30min": round(self.coinbase_error_rate_30min(), 4),
                "coinbase_total_calls": cb_total,
            }

            if coin:
                ck = coin.upper()
                hist = dq_by_coin_copy.get(ck)
                out["coin"] = ck
                out["orderbook_pressure_stale_ratio_30min"] = (
                    round(hist.stale_ratio(), 4) if hist else 0.0
                )
                out["orderbook_pressure_data_quality_latest"] = (
                    hist.latest() if hist else ""
                )
            else:
                out["orderbook_pressure_stale_ratio_30min_by_coin"] = {
                    c: round(h.stale_ratio(), 4) for c, h in dq_by_coin_copy.items()
                }
                out["orderbook_pressure_data_quality_latest_by_coin"] = {
                    c: h.latest() for c, h in dq_by_coin_copy.items()
                }

        return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Process-level singleton
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_global_metrics: Optional[LiquidityWallMetrics] = None
_global_lock = threading.Lock()


def get_metrics() -> LiquidityWallMetrics:
    """获取全局 metrics 实例（首次调用时懒初始化）。"""
    global _global_metrics
    if _global_metrics is None:
        with _global_lock:
            if _global_metrics is None:
                _global_metrics = LiquidityWallMetrics()
    return _global_metrics


def reset_metrics_for_test() -> None:
    """仅用于测试：重置全局实例。"""
    global _global_metrics
    with _global_lock:
        _global_metrics = LiquidityWallMetrics()
