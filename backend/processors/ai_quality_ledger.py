"""P1.8a · AI Quality Ledger

职责：
  每次 `build_ai_trader_report` 完成后，落一条质量记录到滚动 ledger。
  用数据回答"AI 升级到底有没有用"这个问题——matrix_source、plans_source、
  json_valid、延迟、与数学引擎一致度等。

核心字段：
  - matrix_source:   ai_json / rule_fallback / internal_conflict / hybrid
  - plans_source:    ai_json / markdown / sniper_fallback / wait_placeholder
  - json_valid:      True/False
  - json_invalid_reason: missing / malformed / wrong_sections / text_conflict / ...
  - overlay_fields:  AI JSON 覆写的字段总数
  - bias_vs_text:    AI JSON bias 与 markdown signal_summary 是否一致
  - latency_ms / reasoning_tokens
  - math_agreement:  agree / caution / disagree / no_math_plan

查询入口：
  - get_recent(coin, limit=50) → list[dict]
  - get_stats(coin) → 聚合统计（命中率、分歧率、平均延迟）

数据形态参考 PlanBacktestStore / DivergenceBackfillStore，保持一致。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)

_DATA_FILE = os.environ.get(
    "AI_QUALITY_LEDGER_FILE",
    os.path.join(os.path.dirname(__file__), "..", "data", "ai_quality_ledger.json"),
)
_DATA_FILE = os.path.abspath(_DATA_FILE)

_MAX_RECORDS_PER_COIN = 200  # 滚动窗口最大记录数
_GC_THRESHOLD = 300           # 超过则回收到 MAX
_STATS_WINDOW = 50            # 聚合统计用最近 N 条


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class AIQualityRecord:
    ts: int
    coin: str
    price_at_analysis: float = 0.0

    # 来源字段
    matrix_source: str = "rule_fallback"          # ai_json / rule_fallback / internal_conflict
    plans_source: str = "markdown"                # ai_json / markdown / sniper_fallback / wait_placeholder
    json_valid: bool = False                      # AI JSON 是否通过基本校验
    json_invalid_reason: str = ""                 # missing / malformed / wrong_sections / text_conflict

    # 产出质量
    overlay_fields: int = 0                       # AI JSON 覆写的字段总数
    ai_plans_count: int = 0                       # AI JSON 给出的 plans 数量（合法的）
    ai_extra_rows: int = 0                        # AI 追加行数（7 板块合计）
    bias_vs_text: str = "unknown"                 # consistent / conflict / text_missing / json_missing

    # 决策
    final_bias: str = "neutral"
    final_conviction: int = 0
    math_agreement: str = "no_math_plan"          # agree / caution / disagree / no_math_plan

    # 开销
    latency_ms: int = 0
    reasoning_tokens: int = 0
    model: str = ""

    # 备注（可选）
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AIQualityRecord":
        try:
            return cls(
                ts=int(d.get("ts", 0) or 0),
                coin=str(d.get("coin", "")),
                price_at_analysis=float(d.get("price_at_analysis", 0) or 0),
                matrix_source=str(d.get("matrix_source", "rule_fallback")),
                plans_source=str(d.get("plans_source", "markdown")),
                json_valid=bool(d.get("json_valid", False)),
                json_invalid_reason=str(d.get("json_invalid_reason", "")),
                overlay_fields=int(d.get("overlay_fields", 0) or 0),
                ai_plans_count=int(d.get("ai_plans_count", 0) or 0),
                ai_extra_rows=int(d.get("ai_extra_rows", 0) or 0),
                bias_vs_text=str(d.get("bias_vs_text", "unknown")),
                final_bias=str(d.get("final_bias", "neutral")),
                final_conviction=int(d.get("final_conviction", 0) or 0),
                math_agreement=str(d.get("math_agreement", "no_math_plan")),
                latency_ms=int(d.get("latency_ms", 0) or 0),
                reasoning_tokens=int(d.get("reasoning_tokens", 0) or 0),
                model=str(d.get("model", "")),
                notes=list(d.get("notes") or []),
            )
        except (TypeError, ValueError) as e:
            logger.warning("[D14.quality] record parse failed: %s", e)
            return cls(ts=0, coin="", price_at_analysis=0.0)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Ledger
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AIQualityLedger:
    """AI 分析质量滚动记录仓

    存储结构：
      {coin: [AIQualityRecord, ...]}（每 coin 最多 200 条，按 ts 升序）

    线程安全：RLock
    """

    def __init__(self, data_file: str = _DATA_FILE) -> None:
        self._lock = threading.RLock()
        self._data_file = data_file
        self._records: dict[str, list[AIQualityRecord]] = {}
        self._load_from_disk()

    # ── 持久化 ────────────────────────────────────────

    def _load_from_disk(self) -> None:
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for coin, lst in (raw.get("records") or {}).items():
                self._records[coin] = [
                    AIQualityRecord.from_dict(d) for d in lst if isinstance(d, dict)
                ]
            logger.info(
                "[D14.quality] ledger loaded | coins=%s total=%d",
                list(self._records.keys()),
                sum(len(v) for v in self._records.values()),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[D14.quality] load failed: %s", e)

    def _persist_to_disk(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
            payload = {
                "records": {
                    coin: [r.to_dict() for r in records]
                    for coin, records in self._records.items()
                },
                "ts": int(time.time()),
            }
            tmp = self._data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._data_file)
        except (OSError, TypeError) as e:
            logger.debug("[D14.quality] persist failed: %s", e)

    # ── 记录 ──────────────────────────────────────────

    def record(self, rec: AIQualityRecord) -> None:
        """追加一条记录；过量时自动裁剪"""
        if not rec.coin:
            return
        coin = rec.coin.upper()
        with self._lock:
            bucket = self._records.setdefault(coin, [])
            bucket.append(rec)
            if len(bucket) > _GC_THRESHOLD:
                bucket[:] = bucket[-_MAX_RECORDS_PER_COIN:]
            self._persist_to_disk()
            self._mark_tracker_locked(coin)

    # ── 查询 ──────────────────────────────────────────

    def get_recent(self, coin: str, limit: int = _STATS_WINDOW) -> list[dict]:
        with self._lock:
            bucket = self._records.get(coin.upper(), [])
            recent = bucket[-int(max(1, limit)):]
            return [r.to_dict() for r in reversed(recent)]  # 最新在前

    def get_stats(self, coin: str, window: int = _STATS_WINDOW) -> dict:
        """聚合统计最近 N 条记录

        返回字段：
          sample_size, ai_json_hit_rate, ai_plans_hit_rate,
          bias_consistency_rate, internal_conflict_rate,
          math_agreement_rate, avg_latency_ms, avg_reasoning_tokens,
          avg_overlay_fields, avg_ai_plans
        """
        with self._lock:
            bucket = self._records.get(coin.upper(), [])
            window_items = bucket[-int(max(1, window)):]

        n = len(window_items)
        if n == 0:
            return {
                "coin": coin.upper(),
                "sample_size": 0,
                "window": int(window),
                "ai_json_hit_rate": 0.0,
                "ai_plans_hit_rate": 0.0,
                "bias_consistency_rate": 0.0,
                "internal_conflict_rate": 0.0,
                "math_agreement_rate": 0.0,
                "avg_latency_ms": 0,
                "avg_reasoning_tokens": 0,
                "avg_overlay_fields": 0.0,
                "avg_ai_plans": 0.0,
                "first_ts": 0,
                "last_ts": 0,
                "trend_hint_cn": "暂无样本",
            }

        ai_json_hits = sum(1 for r in window_items if r.matrix_source == "ai_json")
        plans_hits = sum(1 for r in window_items if r.plans_source == "ai_json")
        conflicts = sum(1 for r in window_items if r.matrix_source == "internal_conflict")
        consistent = sum(
            1 for r in window_items if r.bias_vs_text == "consistent"
        )
        bias_known = sum(
            1 for r in window_items if r.bias_vs_text in ("consistent", "conflict")
        )
        math_agrees = sum(1 for r in window_items if r.math_agreement == "agree")
        math_known = sum(
            1 for r in window_items if r.math_agreement in ("agree", "caution", "disagree")
        )

        stats = {
            "coin": coin.upper(),
            "sample_size": n,
            "window": int(window),
            "ai_json_hit_rate": round(ai_json_hits / n, 4),
            "ai_plans_hit_rate": round(plans_hits / n, 4),
            "bias_consistency_rate": round(consistent / bias_known, 4) if bias_known else 0.0,
            "internal_conflict_rate": round(conflicts / n, 4),
            "math_agreement_rate": round(math_agrees / math_known, 4) if math_known else 0.0,
            "avg_latency_ms": int(sum(r.latency_ms for r in window_items) / n),
            "avg_reasoning_tokens": int(
                sum(r.reasoning_tokens for r in window_items) / n
            ),
            "avg_overlay_fields": round(
                sum(r.overlay_fields for r in window_items) / n, 2
            ),
            "avg_ai_plans": round(
                sum(r.ai_plans_count for r in window_items) / n, 2
            ),
            "first_ts": int(window_items[0].ts),
            "last_ts": int(window_items[-1].ts),
        }

        # 趋势提示（供前端直接显示一句话结论）
        stats["trend_hint_cn"] = _build_trend_hint(stats)
        # 失败原因 top-3（仅当失败存在）
        stats["top_invalid_reasons"] = _top_invalid_reasons(window_items)
        return stats

    # ── DecisionTracker 对接 ──────────────────────────

    def _mark_tracker_locked(self, coin: str) -> None:
        """把最近窗口的聚合指标写到 D14。

        修正点（2026-04）：
          1. 小样本（n < 5）只刷 metrics，不覆盖 status，避免首轮 AI 单条样本
             直接把 D14 压成"质量不合格"（此前 rate=0/1 → fail）。
             5 条阈值与 `_build_trend_hint` 里"样本不足"的判定一致。
          2. 严重不合格 status 用 "failed"（Status Literal 合法值），此前
             写成 "fail" 不在 Literal 里，前端 health_aggregator 拉不出
             failed bucket 且 `fail_count` 也不会累加，显示成 status=fail
             但 ok/warn/fail 计数全 0 的矛盾态。
        """
        try:
            from utils.decision_tracker import D, get_tracker
        except Exception:
            return
        try:
            # 复用 get_stats 但跳过锁（在 locked 上下文里）
            bucket = self._records.get(coin.upper(), [])
            window_items = bucket[-_STATS_WINDOW:]
            n = len(window_items)
            if n == 0:
                return
            ai_json_hits = sum(1 for r in window_items if r.matrix_source == "ai_json")
            plans_hits = sum(1 for r in window_items if r.plans_source == "ai_json")
            conflicts = sum(
                1 for r in window_items if r.matrix_source == "internal_conflict"
            )
            rate = ai_json_hits / n

            # n < 5：只更新 metrics，不覆盖 _mark_d14 已经写好的 status
            if n < 5:
                get_tracker().mark(
                    D.D14_AI_TRADER,
                    status=None,  # 保留既有 status
                    log=False,
                    quality_sample=n,
                    quality_ai_json_rate=round(rate, 3),
                    quality_ai_plans_rate=round(plans_hits / n, 3),
                    quality_conflict_rate=round(conflicts / n, 3),
                    quality_note="warming_up",
                )
                return

            status = (
                "ok" if rate >= 0.6
                else "warn" if rate >= 0.3
                else "failed"
            )
            # conflict 高于 10% 直接标 warn 覆盖
            if conflicts / n >= 0.1 and status == "ok":
                status = "warn"
            get_tracker().mark(
                D.D14_AI_TRADER,
                status=status,
                log=False,
                coin=coin.upper(),
                quality_sample=n,
                quality_ai_json_rate=round(rate, 3),
                quality_ai_plans_rate=round(plans_hits / n, 3),
                quality_conflict_rate=round(conflicts / n, 3),
            )
        except Exception:
            logger.debug("[D14.quality] tracker mark failed", exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_trend_hint(stats: dict) -> str:
    n = int(stats.get("sample_size", 0))
    if n < 5:
        return f"样本不足（{n}/5）· 参考性低"

    ai_rate = float(stats.get("ai_json_hit_rate", 0))
    conflict_rate = float(stats.get("internal_conflict_rate", 0))
    plans_rate = float(stats.get("ai_plans_hit_rate", 0))

    parts = []
    if ai_rate >= 0.8:
        parts.append(f"AI 附录稳定命中（{ai_rate:.0%}）")
    elif ai_rate >= 0.5:
        parts.append(f"AI 附录部分生效（{ai_rate:.0%}）· 可优化 prompt")
    else:
        parts.append(f"AI 附录命中率低（{ai_rate:.0%}）· 需检查模型是否按格式输出")

    if conflict_rate >= 0.1:
        parts.append(f"文本/JSON 冲突率 {conflict_rate:.0%}（偏高）")

    if plans_rate >= 0.7:
        parts.append(f"AI 计划直出率 {plans_rate:.0%}（良好）")
    elif plans_rate < 0.3 and n >= 10:
        parts.append("AI 计划多数走回退路径")

    return " · ".join(parts)


def _top_invalid_reasons(items: list[AIQualityRecord]) -> list[dict]:
    """统计失败原因 top-3"""
    counter: dict[str, int] = {}
    for r in items:
        reason = (r.json_invalid_reason or "").strip()
        if reason:
            counter[reason] = counter.get(reason, 0) + 1
    sorted_items = sorted(counter.items(), key=lambda x: x[1], reverse=True)
    return [{"reason": k, "count": v} for k, v in sorted_items[:3]]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Optional[AIQualityLedger] = None
_instance_lock = threading.Lock()


def get_ai_quality_ledger() -> AIQualityLedger:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = AIQualityLedger()
    return _instance


def reset_for_testing(data_file: Optional[str] = None) -> AIQualityLedger:
    """仅测试使用：重置单例到指定数据文件"""
    global _instance
    with _instance_lock:
        _instance = AIQualityLedger(data_file=data_file or _DATA_FILE)
    return _instance
