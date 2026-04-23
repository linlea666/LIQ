"""Market Action Analyzer · 事后评估引擎（Phase 5-B）

职责
----
读取 `logs/maa_shadow/<day>/<COIN>.jsonl` 里的 report 记录，对每条过去 >= 4h 的
报告按 **T+4h / T+8h / T+24h** 寻价，按 bias + scenario 两套规则判定
`correct / wrong / neutral`，产出 MAASummary：
  - 每 horizon 的命中率
  - Confidence 分桶校准（0-49/50-59/60-69/70-79/80-100）
  - Per-scenario 命中率（24h）
  - 最近样本列表（供前端小面板展示）

关键设计
--------
- **判定规则以 bias 为先**（最稳定的方向性信号）：
    - bias=long    ：delta_pct >= +0.3% → correct；<= -0.3% → wrong；否则 neutral
    - bias=short   ：delta_pct <= -0.3% → correct；>= +0.3% → wrong；否则 neutral
    - bias=wait/neutral：|delta_pct| < 0.4% → correct（判断正确："不该动就别动"）；
                        否则 neutral（没有操作就不算错）
- **阈值**（相对价 pct，而非 ATR 归一）：
    - T+4h 窗口：±0.3%
    - T+8h 窗口：±0.5%
    - T+24h 窗口：±0.8%
  加密永续波动天生大，阈值稍保守避免"擦边算对"。
- **寻价**：用 `maa_shadow.find_nearest_price` 在 target_ts 附近 ±30min 找最近 heartbeat 或 report 记录。
  若 target_ts 尚未到来（未来时间）或漂移超限，该 horizon 判为 `pending`。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

from monitoring.maa_shadow import find_nearest_price, iter_records

logger = logging.getLogger(__name__)

# ── 判定阈值（pct）─────────────────────────────────────────
_THRESHOLDS = {
    "4h": 0.3,   # %
    "8h": 0.5,
    "24h": 0.8,
}

_WAIT_NEUTRAL_BAND = {
    "4h": 0.4,
    "8h": 0.7,
    "24h": 1.2,
}

_HORIZONS = ["4h", "8h", "24h"]
_HORIZON_SEC = {"4h": 4 * 3600, "8h": 8 * 3600, "24h": 24 * 3600}

# Confidence 分桶（前闭后开，最后一桶是 [80, 100]）
_CONF_BUCKETS = [
    ("0-49", 0, 50),
    ("50-59", 50, 60),
    ("60-69", 60, 70),
    ("70-79", 70, 80),
    ("80-100", 80, 101),
]

Outcome = Literal["correct", "wrong", "neutral", "pending"]


# ── 核心判定逻辑 ────────────────────────────────────────────

def _evaluate_bias_outcome(
    bias: Optional[str],
    delta_pct: float,
    horizon: str,
) -> Outcome:
    """根据 bias + pct 涨跌判定是否兑现。"""
    th = _THRESHOLDS.get(horizon, 0.5)
    if bias == "long":
        if delta_pct >= th:
            return "correct"
        if delta_pct <= -th:
            return "wrong"
        return "neutral"
    if bias == "short":
        if delta_pct <= -th:
            return "correct"
        if delta_pct >= th:
            return "wrong"
        return "neutral"
    # wait / neutral / None
    band = _WAIT_NEUTRAL_BAND.get(horizon, 0.7)
    if abs(delta_pct) < band:
        return "correct"
    return "neutral"


# ── 数据结构 ────────────────────────────────────────────────

@dataclass
class _SampleHorizonOutcome:
    price: Optional[float] = None
    delta_pct: Optional[float] = None
    label: Outcome = "pending"


@dataclass
class EvalSample:
    ts: int
    coin: str
    scenario: str
    bias: str
    confidence: int
    price_at_analysis: float
    outcomes: dict[str, _SampleHorizonOutcome] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "ts": self.ts,
            "coin": self.coin,
            "scenario": self.scenario,
            "bias": self.bias,
            "confidence": self.confidence,
            "price_at_analysis": self.price_at_analysis,
            "outcomes": {
                h: {
                    "price": o.price,
                    "delta_pct": o.delta_pct,
                    "label": o.label,
                }
                for h, o in self.outcomes.items()
            },
        }


@dataclass
class EvalSummary:
    coin: str
    window_days: int
    sample_size: int
    last_updated_ts: int
    horizons: list[dict] = field(default_factory=list)
    calibration: list[dict] = field(default_factory=list)
    per_scenario: list[dict] = field(default_factory=list)
    recent: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "coin": self.coin,
            "window_days": self.window_days,
            "sample_size": self.sample_size,
            "last_updated_ts": self.last_updated_ts,
            "horizons": self.horizons,
            "calibration": self.calibration,
            "per_scenario": self.per_scenario,
            "recent": self.recent,
        }


# ── 主入口 ──────────────────────────────────────────────────

def evaluate_coin(coin: str, *, window_days: int = 7) -> EvalSummary:
    """对单币种跑一次事后评估，返回 EvalSummary。

    Args:
        coin: 币种
        window_days: 回看窗口（只评估 window_days 天内产生的报告）
    """
    coin_up = coin.upper()
    records = iter_records(coin_up, days=window_days + 1)
    now = time.time()
    cutoff_ts = now - window_days * 86400

    report_records = [
        r for r in records
        if r.get("kind") == "report"
        and r.get("ts", 0) >= cutoff_ts
        and r.get("parse_ok", True)  # 跳过 fallback 报告，它们不是真实 AI 判断
        and r.get("price", 0) > 0
    ]

    # 先构造所有样本（含未来未到期的 horizon → pending）
    samples: list[EvalSample] = []
    for rec in report_records:
        ts = int(rec["ts"])
        price0 = float(rec["price"])
        sample = EvalSample(
            ts=ts,
            coin=coin_up,
            scenario=rec.get("scenario") or "range_bound",
            bias=rec.get("bias") or "wait",
            confidence=int(rec.get("confidence") or 0),
            price_at_analysis=price0,
        )

        for h in _HORIZONS:
            target = ts + _HORIZON_SEC[h]
            out = _SampleHorizonOutcome()
            if target > now:
                out.label = "pending"
            else:
                fp = find_nearest_price(
                    coin_up, target, max_drift_sec=30 * 60, records=records,
                )
                if fp is None or price0 <= 0:
                    out.label = "pending"
                else:
                    out.price = fp
                    out.delta_pct = (fp - price0) / price0 * 100.0
                    out.label = _evaluate_bias_outcome(sample.bias, out.delta_pct, h)
            sample.outcomes[h] = out
        samples.append(sample)

    return _aggregate(coin_up, samples, window_days)


# ── 聚合统计 ────────────────────────────────────────────────

def _aggregate(coin: str, samples: list[EvalSample], window_days: int) -> EvalSummary:
    now_ts = int(time.time())

    # 1. horizon 级命中率
    horizons_stats: list[dict] = []
    for h in _HORIZONS:
        correct = wrong = neutral = 0
        resolved = 0
        for s in samples:
            out = s.outcomes.get(h)
            if out is None or out.label == "pending":
                continue
            resolved += 1
            if out.label == "correct":
                correct += 1
            elif out.label == "wrong":
                wrong += 1
            else:
                neutral += 1
        acc_base = correct + wrong  # neutral 样本不计入准确率分母（没错也没对）
        accuracy = (correct / acc_base * 100.0) if acc_base > 0 else None
        horizons_stats.append({
            "horizon": h,
            "sample_size": resolved,
            "correct": correct,
            "wrong": wrong,
            "neutral": neutral,
            "accuracy_pct": round(accuracy, 1) if accuracy is not None else None,
        })

    # 2. Confidence 分桶校准（以 24h 为基准，它最能反映"对不对"）
    calibration: list[dict] = []
    for name, lo, hi in _CONF_BUCKETS:
        bucket = [s for s in samples if lo <= s.confidence < hi]
        resolved = [s for s in bucket if s.outcomes.get("24h") and s.outcomes["24h"].label != "pending"]
        correct_cnt = sum(1 for s in resolved if s.outcomes["24h"].label == "correct")
        base = sum(1 for s in resolved if s.outcomes["24h"].label in ("correct", "wrong"))
        accuracy = (correct_cnt / base * 100.0) if base > 0 else None
        calibration.append({
            "range": name,
            "sample_size": len(resolved),
            "accuracy_pct": round(accuracy, 1) if accuracy is not None else None,
        })

    # 3. Per-scenario 命中率（24h）
    per_scenario_map: dict[str, dict] = {}
    for s in samples:
        out24 = s.outcomes.get("24h")
        if not out24 or out24.label == "pending":
            continue
        sc = s.scenario
        bucket = per_scenario_map.setdefault(sc, {"total": 0, "correct": 0, "wrong": 0})
        bucket["total"] += 1
        if out24.label == "correct":
            bucket["correct"] += 1
        elif out24.label == "wrong":
            bucket["wrong"] += 1
    per_scenario_stats: list[dict] = []
    for sc, b in per_scenario_map.items():
        base = b["correct"] + b["wrong"]
        acc = (b["correct"] / base * 100.0) if base > 0 else None
        per_scenario_stats.append({
            "scenario": sc,
            "sample_size": b["total"],
            "accuracy_pct": round(acc, 1) if acc is not None else None,
            "horizon": "24h",
        })
    per_scenario_stats.sort(key=lambda x: x["sample_size"], reverse=True)

    # 4. 最近样本（倒序前 20）
    recent_sorted = sorted(samples, key=lambda s: s.ts, reverse=True)[:20]
    recent = [s.to_dict() for s in recent_sorted]

    return EvalSummary(
        coin=coin,
        window_days=window_days,
        sample_size=len(samples),
        last_updated_ts=now_ts,
        horizons=horizons_stats,
        calibration=calibration,
        per_scenario=per_scenario_stats,
        recent=recent,
    )


# ── 快捷 headline（供前端徽章/日志用）─────────────────────────

def summary_headline(summary: EvalSummary) -> str:
    """生成一行中文摘要，例如："BTC · 7d 样本 42 · 24h 命中 62.5% · 校准偏差 -8"。"""
    parts: list[str] = [f"{summary.coin} · {summary.window_days}d 样本 {summary.sample_size}"]
    h24 = next((h for h in summary.horizons if h["horizon"] == "24h"), None)
    if h24 and h24.get("accuracy_pct") is not None:
        parts.append(f"24h 命中 {h24['accuracy_pct']}%")
    # 校准偏差：高分桶准确率 vs 平均准确率差距
    hi = next((c for c in summary.calibration if c["range"] in ("70-79", "80-100") and c["accuracy_pct"] is not None), None)
    if h24 and hi and h24.get("accuracy_pct") is not None:
        diff = round(hi["accuracy_pct"] - h24["accuracy_pct"], 1)
        parts.append(f"高分桶偏差 {diff:+.1f}")
    return " · ".join(parts)
