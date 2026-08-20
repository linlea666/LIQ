"""PIT 安全的普通异常正规化器。

只接受来源时间发生变化的新事实；重复引擎 tick、未来/倒序事实和低方差基线
都不能产生“极端”叙事。所有输出仅供展示，不参与事件评分。
"""
from __future__ import annotations

import math
import statistics
from collections import deque
from typing import Any


class RollingPitAnomalyNormalizer:
    CHECKPOINT_SOURCE = "market_risk_anomaly_v1"

    def __init__(
        self, store: Any, *, max_samples: int = 2_880,
        min_samples: int = 96, min_span_sec: int = 86_400,
    ) -> None:
        self._store = store
        self._max_samples = max(120, int(max_samples))
        self._min_samples = max(30, int(min_samples))
        self._min_span_sec = max(3_600, int(min_span_sec))
        self._windows: dict[tuple[str, str], deque[tuple[int, float]]] = {}
        self._loaded: set[str] = set()
        self._last_checkpoint_at: dict[str, int] = {}
        self._pending_samples: dict[str, int] = {}

    def _load_coin(self, coin: str) -> None:
        coin = coin.upper()
        if coin in self._loaded:
            return
        payload = self._store.load_checkpoint(self.CHECKPOINT_SOURCE, coin) or {}
        metrics = payload.get("metrics", {}) if isinstance(payload, dict) else {}
        if isinstance(metrics, dict):
            for metric, samples in metrics.items():
                window: deque[tuple[int, float]] = deque(maxlen=self._max_samples)
                if isinstance(samples, list):
                    for sample in samples[-self._max_samples:]:
                        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
                            continue
                        try:
                            as_of, value = int(sample[0]), float(sample[1])
                        except (TypeError, ValueError):
                            continue
                        if as_of > 0 and math.isfinite(value):
                            window.append((as_of, value))
                self._windows[(coin, str(metric))] = window
        self._loaded.add(coin)

    def _save_coin(self, coin: str) -> None:
        metrics = {
            metric: [[as_of, value] for as_of, value in window]
            for (window_coin, metric), window in self._windows.items()
            if window_coin == coin
        }
        self._store.save_checkpoint(
            self.CHECKPOINT_SOURCE, coin, {"version": 1, "metrics": metrics},
        )

    @staticmethod
    def _percentile(values: list[float], value: float) -> float:
        less = sum(item < value for item in values)
        equal = sum(item == value for item in values)
        return (less + 0.5 * equal) / len(values)

    @staticmethod
    def _robust_scale(values: list[float]) -> tuple[float, float]:
        median = statistics.median(values)
        mad = statistics.median(abs(item - median) for item in values)
        scale = 1.4826 * mad
        if scale <= 0:
            quartiles = statistics.quantiles(values, n=4, method="inclusive")
            scale = (quartiles[2] - quartiles[0]) / 1.349
        return median, scale

    def evaluate(
        self, coin: str, facts: dict[str, dict[str, Any]], decision_time: int,
    ) -> list[dict[str, Any]]:
        coin = coin.upper()
        self._load_coin(coin)
        results: list[dict[str, Any]] = []
        changed = False
        for metric, fact in facts.items():
            try:
                value = float(fact.get("value"))
                as_of = int(fact.get("as_of") or 0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(value):
                rejected = self._result(metric, fact, 0.0, as_of, "non_finite_value")
                rejected["reported_value"] = str(value)
                results.append(rejected)
                continue
            if as_of <= 0:
                results.append(self._result(metric, fact, value, as_of, "missing_as_of"))
                continue
            window = self._windows.setdefault(
                (coin, metric), deque(maxlen=self._max_samples),
            )
            if as_of > decision_time:
                results.append(self._result(metric, fact, value, as_of, "pit_rejected"))
                continue
            if window and as_of < window[-1][0]:
                results.append(self._result(metric, fact, value, as_of, "non_monotonic"))
                continue
            if window and as_of == window[-1][0]:
                duplicate = self._result(metric, fact, value, as_of, "duplicate_as_of")
                duplicate["sample_count"] = len(window)
                duplicate["history_span_sec"] = (
                    window[-1][0] - window[0][0] if len(window) >= 2 else 0
                )
                results.append(duplicate)
                continue
            history = list(window)
            values = [item[1] for item in history]
            span = history[-1][0] - history[0][0] if len(history) >= 2 else 0
            status = "warming"
            robust_z = None
            percentile = None
            reason = "insufficient_unique_history"
            if len(values) >= self._min_samples and span >= self._min_span_sec:
                median, scale = self._robust_scale(values)
                absolute_floor = max(1e-9, abs(median) * 1e-6)
                if scale <= absolute_floor:
                    status = "baseline_flat"
                    reason = "robust_scale_below_floor"
                else:
                    robust_z = (value - median) / scale
                    percentile = self._percentile(values, value)
                    tail = min(percentile, 1.0 - percentile)
                    status = (
                        "extreme" if abs(robust_z) >= 4.0 or tail <= 0.005
                        else "unusual" if abs(robust_z) >= 2.5 or tail <= 0.025
                        else "normal"
                    )
                    reason = "ready"
            result = self._result(metric, fact, value, as_of, reason)
            result.update({
                "status": status, "robust_z": robust_z, "percentile": percentile,
                "sample_count": len(history), "history_span_sec": span,
                "min_samples": self._min_samples, "min_span_sec": self._min_span_sec,
            })
            results.append(result)
            if not window or as_of > window[-1][0]:
                window.append((as_of, value))
                changed = True
            # 同一 as_of 的重复 tick 只展示，不重复采样。
        if changed:
            pending = self._pending_samples.get(coin, 0) + 1
            last_saved = self._last_checkpoint_at.get(coin, 0)
            if last_saved == 0 or decision_time - last_saved >= 300 or pending >= 10:
                self._save_coin(coin)
                self._last_checkpoint_at[coin] = decision_time
                pending = 0
            self._pending_samples[coin] = pending
        return results

    @staticmethod
    def _result(
        metric: str, fact: dict[str, Any], value: float, as_of: int, reason: str,
    ) -> dict[str, Any]:
        notes = {
            "insufficient_unique_history": "唯一时间样本或覆盖时长不足，仍在建立异常基线。",
            "robust_scale_below_floor": "历史变化过小，无法可靠判断异常强度。",
            "pit_rejected": "来源时间晚于决策时间，已拒绝进入异常基线。",
            "non_monotonic": "来源时间倒序，已拒绝进入异常基线。",
            "missing_as_of": "缺少来源时间，不能进入异常基线。",
            "duplicate_as_of": "来源时间未变化，本次引擎 tick 不重复采样。",
            "non_finite_value": "指标值不是有限数，已拒绝进入异常基线。",
            "ready": "滚动 PIT 异常仅作信息展示，未通过 OOS 前不计分。",
        }
        return {
            "metric": metric, "label": fact.get("label", metric), "value": value,
            "direction": fact.get("direction", "unknown"), "status": "warming",
            "robust_z": None, "percentile": None, "sample_count": 0,
            "history_span_sec": 0, "as_of": as_of,
            "decision_role": "informational", "reason": reason,
            "note": notes.get(reason, notes["ready"]),
        }
