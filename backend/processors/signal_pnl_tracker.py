"""P2.1 · 信号 PnL 价格回放追踪器

职责：
    统一追踪 math ExecutionPlan、AI trading_plans、FinalDecision 三类信号在
    后续价格轨迹中的实际命中情况（**纯价格回放，不涉及下单**），产出：
        entry_filled / tp1_hit / tp2_hit / sl_hit / expired / invalidated
    以及每条样本的 R multiple / MFE / MAE。

与既有 PlanBacktestStore 的关系：
    - PlanBacktestStore 只跟踪数学 ExecutionPlan，做"方向对不对"胜率
    - 本模块是更精细的一层，按多维度（origin/tier/coin/regime）聚合真实 PnL
    - 两者并存，互不替代（九原则 §6 保守修改）

数据流：
    engine._recompute 末尾：
        record_plans_from_math(execution_plan)
        record_plans_from_ai(ai_report)
        record_plans_from_final(final_decision)   # 可选（避免重复计数）
    每次 price 更新时：
        tick(coin, price, ts)
        → pending 样本看是否到达 entry
        → entry_filled 样本看是否命中 SL/TP/过期
        → 维护 MFE/MAE

窗口策略：
    - 未触发 72h → expired
    - 已触发后 72h 未碰 SL/TP → expired（保留 outcome_price 为最后价）
    - 任何时刻被 SafetyGate blackswan/extreme_vol 标记 → invalidated
      （本阶段暂不接入 SafetyGate，预留 mark_invalidated 接口）

存储：
    backend/data/signal_pnl.json（滚动 500 条/coin）
    超 72h 已 resolved 的样本 → 归档到 data/signal_pnl_archive.jsonl（追加写）
    长期统计从归档文件按需读取
"""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_WINDOW_SEC = 72 * 60 * 60       # 72 小时窗口
_MAX_RECORDS_PER_COIN = 500
_GC_THRESHOLD = 700               # 超过则保留最近 500 条
_PERSIST_EVERY_N_CHANGES = 15     # 每 N 次变更落盘
_STATS_WINDOW_DEFAULT = 200       # 聚合统计最近 N 条已 resolved 样本

_DATA_FILE = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "signal_pnl.json",
    )
)
_ARCHIVE_FILE = os.path.abspath(
    os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data",
        "signal_pnl_archive.jsonl",
    )
)

_PRICE_DECIMALS = {"BTC": 0, "ETH": 1, "SOL": 2}

# outcome 终态集合
_TERMINAL_OUTCOMES = {"tp1_hit", "tp2_hit", "sl_hit", "expired", "invalidated"}

_VALID_ORIGINS = {"math", "ai", "final"}


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据类
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class PlanPnLSample:
    sample_id: str
    coin: str
    origin: str                              # math / ai / final
    priority: int = 1
    action: str = "wait"                     # long / short / wait
    tier: str = "C"
    regime: str = ""

    # 入场参数
    entry_low: float = 0.0
    entry_high: Optional[float] = None
    stop_loss: float = 0.0
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None
    position_pct: float = 0.0

    # 元信息
    created_ts: int = 0
    price_at_create: float = 0.0

    # 状态机
    entry_filled_ts: Optional[int] = None
    entry_filled_price: Optional[float] = None
    outcome: str = "pending"                 # pending / entry_filled / tp1_hit / tp2_hit / sl_hit / expired / invalidated
    outcome_ts: Optional[int] = None
    outcome_price: Optional[float] = None
    r_multiple: Optional[float] = None
    max_favorable_r: float = 0.0
    max_adverse_r: float = 0.0

    # 备注
    invalidation_reason: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PlanPnLSample":
        try:
            return cls(
                sample_id=str(d.get("sample_id", "")),
                coin=str(d.get("coin", "")).upper(),
                origin=str(d.get("origin", "math")),
                priority=int(d.get("priority", 1) or 1),
                action=str(d.get("action", "wait")),
                tier=str(d.get("tier", "C")),
                regime=str(d.get("regime", "")),
                entry_low=float(d.get("entry_low", 0) or 0),
                entry_high=_opt_float(d.get("entry_high")),
                stop_loss=float(d.get("stop_loss", 0) or 0),
                tp1=_opt_float(d.get("tp1")),
                tp2=_opt_float(d.get("tp2")),
                rr_ratio=_opt_float(d.get("rr_ratio")),
                position_pct=float(d.get("position_pct", 0) or 0),
                created_ts=int(d.get("created_ts", 0) or 0),
                price_at_create=float(d.get("price_at_create", 0) or 0),
                entry_filled_ts=_opt_int(d.get("entry_filled_ts")),
                entry_filled_price=_opt_float(d.get("entry_filled_price")),
                outcome=str(d.get("outcome", "pending")),
                outcome_ts=_opt_int(d.get("outcome_ts")),
                outcome_price=_opt_float(d.get("outcome_price")),
                r_multiple=_opt_float(d.get("r_multiple")),
                max_favorable_r=float(d.get("max_favorable_r", 0) or 0),
                max_adverse_r=float(d.get("max_adverse_r", 0) or 0),
                invalidation_reason=str(d.get("invalidation_reason", "")),
            )
        except (TypeError, ValueError) as e:
            logger.warning("[P2.1] sample parse failed: %s", e)
            return cls(sample_id="", coin="", origin="math")

    def is_terminal(self) -> bool:
        return self.outcome in _TERMINAL_OUTCOMES

    def is_actionable(self) -> bool:
        """有效方向信号（需要继续 tick）"""
        return self.action in {"long", "short"} and self.stop_loss > 0


def _opt_float(v: Any) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _opt_int(v: Any) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Tracker
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class SignalPnLTracker:
    """信号价格回放追踪仓（单例）"""

    def __init__(
        self,
        data_file: str = _DATA_FILE,
        archive_file: str = _ARCHIVE_FILE,
    ) -> None:
        self._lock = threading.RLock()
        self._data_file = data_file
        self._archive_file = archive_file
        self._samples: dict[str, list[PlanPnLSample]] = {}
        self._pending_writes = 0
        self._load_from_disk()

    # ── 入账：3 个 origin 的统一入口 ───────────────────

    def record_math_plan(self, plan: Any, current_price: float) -> Optional[str]:
        """消费数学 ExecutionPlan"""
        if plan is None:
            return None
        try:
            sample = PlanPnLSample(
                sample_id=self._mk_sample_id(
                    str(getattr(plan, "coin", "")),
                    "math",
                    int(getattr(plan, "priority", 1) or 1),
                    str(getattr(plan, "action", "wait")),
                    _opt_float(getattr(plan, "entry_zone_low", None)) or 0,
                    _opt_float(getattr(plan, "entry_zone_high", None)),
                    _opt_float(getattr(plan, "stop_loss", None)) or 0,
                ),
                coin=str(getattr(plan, "coin", "")).upper(),
                origin="math",
                priority=int(getattr(plan, "priority", 1) or 1),
                action=str(getattr(plan, "action", "wait")),
                tier=str(getattr(plan, "tier_hint", "C")),
                regime=str(getattr(plan, "regime", "")),
                entry_low=_opt_float(getattr(plan, "entry_zone_low", None)) or 0,
                entry_high=_opt_float(getattr(plan, "entry_zone_high", None)),
                stop_loss=_opt_float(getattr(plan, "stop_loss", None)) or 0,
                tp1=_opt_float(getattr(plan, "tp1", None)),
                tp2=_opt_float(getattr(plan, "tp2", None)),
                rr_ratio=_opt_float(getattr(plan, "rr_ratio", None)),
                position_pct=float(_opt_float(getattr(plan, "position_size_pct", None)) or 0),
                created_ts=int(getattr(plan, "ts", time.time()) or time.time()),
                price_at_create=float(
                    current_price
                    or _opt_float(getattr(plan, "current_price", None))
                    or 0
                ),
            )
            return self._add_or_skip(sample)
        except Exception:
            logger.debug("[P2.1] record_math failed", exc_info=True)
            return None

    def record_ai_plans(
        self,
        coin: str,
        trading_plans: Iterable[Any],
        current_price: float,
        created_ts: Optional[int] = None,
        regime: str = "",
    ) -> list[str]:
        """消费 AITraderReport.trading_plans（多条）"""
        ids: list[str] = []
        ts = int(created_ts or time.time())
        for plan in trading_plans or []:
            try:
                action = str(getattr(plan, "direction", "wait"))
                sl = _opt_float(getattr(plan, "stop_loss", None)) or 0
                entry_low = _opt_float(getattr(plan, "entry_zone_low", None)) or 0
                sample = PlanPnLSample(
                    sample_id=self._mk_sample_id(
                        coin, "ai",
                        int(getattr(plan, "priority", 1) or 1),
                        action, entry_low,
                        _opt_float(getattr(plan, "entry_zone_high", None)),
                        sl,
                    ),
                    coin=coin.upper(),
                    origin="ai",
                    priority=int(getattr(plan, "priority", 1) or 1),
                    action=action,
                    tier=str(getattr(plan, "tier_hint", "C")),
                    regime=regime,
                    entry_low=entry_low,
                    entry_high=_opt_float(getattr(plan, "entry_zone_high", None)),
                    stop_loss=sl,
                    tp1=_opt_float(getattr(plan, "tp1", None)),
                    tp2=_opt_float(getattr(plan, "tp2", None)),
                    rr_ratio=_opt_float(getattr(plan, "rr_ratio", None)),
                    position_pct=float(
                        _opt_float(getattr(plan, "position_suggestion_pct", None)) or 0
                    ),
                    created_ts=ts,
                    price_at_create=float(current_price or 0),
                )
                sid = self._add_or_skip(sample)
                if sid:
                    ids.append(sid)
            except Exception:
                logger.debug("[P2.1] record_ai_plan failed", exc_info=True)
        return ids

    def record_final_decision(
        self,
        decision: Any,
        current_price: float,
    ) -> Optional[str]:
        """消费 FinalDecision（融合后的推荐方案）

        只记录 recommended_action in {execute, reduce_size} 且 SL/entry 有效的情况。
        方向从 math_brief.action / ai_brief.action 推导（FinalDecision 本身
        recommended_action 只有 execute/reduce_size/wait/avoid，没有 long/short）。
        """
        if decision is None:
            return None
        try:
            rec_action = str(getattr(decision, "recommended_action", "wait"))
            if rec_action not in {"execute", "reduce_size"}:
                return None

            # 从两个 engine_brief 推导实际方向
            action = ""
            for brief_name in ("math_brief", "ai_brief"):
                brief = getattr(decision, brief_name, None)
                if brief is not None:
                    b_action = str(getattr(brief, "action", "") or "")
                    if b_action in {"long", "short"}:
                        action = b_action
                        break
            if action not in {"long", "short"}:
                return None

            entry_low = _opt_float(getattr(decision, "entry_zone_low", None)) or 0
            entry_high = _opt_float(getattr(decision, "entry_zone_high", None))
            sl = _opt_float(getattr(decision, "stop_loss", None)) or 0
            if entry_low <= 0 or sl <= 0:
                return None

            # tier_hint 不在 FinalDecision 上，退回 math_brief/ai_brief 尝试取
            tier = "C"
            for brief_name in ("math_brief", "ai_brief"):
                brief = getattr(decision, brief_name, None)
                b_tier = getattr(brief, "tier_hint", None) if brief is not None else None
                if b_tier:
                    tier = str(b_tier)
                    break

            sample = PlanPnLSample(
                sample_id=self._mk_sample_id(
                    str(getattr(decision, "coin", "")),
                    "final", 1, action, entry_low, entry_high, sl,
                ),
                coin=str(getattr(decision, "coin", "")).upper(),
                origin="final",
                priority=1,
                action=action,
                tier=tier,
                regime="",
                entry_low=entry_low,
                entry_high=entry_high,
                stop_loss=sl,
                tp1=_opt_float(getattr(decision, "tp1", None)),
                tp2=_opt_float(getattr(decision, "tp2", None)),
                rr_ratio=_opt_float(getattr(decision, "rr_ratio", None)),
                position_pct=float(
                    _opt_float(getattr(decision, "recommended_position_pct", None)) or 0
                ),
                created_ts=int(getattr(decision, "ts", time.time()) or time.time()),
                price_at_create=float(
                    current_price
                    or _opt_float(getattr(decision, "current_price", None))
                    or 0
                ),
            )
            return self._add_or_skip(sample)
        except Exception:
            logger.debug("[P2.1] record_final failed", exc_info=True)
            return None

    # ── tick：推进状态机 ────────────────────────────

    def tick(self, coin: str, price: float, now_ts: Optional[int] = None) -> None:
        """把 coin 的所有 pending / entry_filled 样本推进一步"""
        if not coin or price <= 0:
            return
        now = int(now_ts or time.time())
        changed = False
        coin_u = coin.upper()
        with self._lock:
            bucket = self._samples.get(coin_u, [])
            for sample in bucket:
                if sample.is_terminal():
                    continue
                if not sample.is_actionable():
                    # 非方向信号（wait/avoid）直接跳过
                    continue
                if self._advance_sample(sample, price, now):
                    changed = True
            if changed:
                self._pending_writes += 1
                if self._pending_writes >= _PERSIST_EVERY_N_CHANGES:
                    self._persist_to_disk_locked()
                    self._pending_writes = 0

    def _advance_sample(
        self, sample: PlanPnLSample, price: float, now: int
    ) -> bool:
        """单样本推进；返回是否发生状态变更"""
        # ── 过期判断 ──
        age = now - (sample.entry_filled_ts or sample.created_ts)
        if age > _WINDOW_SEC:
            sample.outcome = "expired"
            sample.outcome_ts = now
            sample.outcome_price = price
            sample.r_multiple = self._r_from_price(sample, price)
            return True

        # ── 尚未入场：等待触碰 entry_zone ──
        if sample.entry_filled_ts is None:
            if self._price_hits_entry(sample, price):
                sample.entry_filled_ts = now
                sample.entry_filled_price = price
                sample.outcome = "entry_filled"
                self._update_extremes(sample, price)
                return True
            # 还没入场，不算 MFE/MAE
            return False

        # ── 已入场：检查 SL/TP，并维护 MFE/MAE ──
        mfe_changed = self._update_extremes(sample, price)

        if self._price_hits_sl(sample, price):
            sample.outcome = "sl_hit"
            sample.outcome_ts = now
            sample.outcome_price = price
            sample.r_multiple = -1.0
            return True
        if sample.tp2 is not None and self._price_hits_tp(sample, price, sample.tp2):
            sample.outcome = "tp2_hit"
            sample.outcome_ts = now
            sample.outcome_price = price
            sample.r_multiple = self._r_from_price(sample, sample.tp2)
            return True
        if sample.tp1 is not None and self._price_hits_tp(sample, price, sample.tp1):
            sample.outcome = "tp1_hit"
            sample.outcome_ts = now
            sample.outcome_price = price
            sample.r_multiple = self._r_from_price(sample, sample.tp1)
            return True

        return mfe_changed

    def mark_invalidated(
        self, coin: str, reason: str = "safety_gate"
    ) -> int:
        """把 coin 所有 pending 样本标记为 invalidated（SafetyGate 黑天鹅触发时调用）"""
        now = int(time.time())
        invalidated = 0
        with self._lock:
            for sample in self._samples.get(coin.upper(), []):
                if not sample.is_terminal():
                    sample.outcome = "invalidated"
                    sample.outcome_ts = now
                    sample.invalidation_reason = reason[:80]
                    invalidated += 1
            if invalidated:
                self._persist_to_disk_locked()
        return invalidated

    # ── 查询 ────────────────────────────────────────

    def get_recent(
        self, coin: str, limit: int = 50, origin: Optional[str] = None
    ) -> list[dict]:
        with self._lock:
            bucket = self._samples.get(coin.upper(), [])
            if origin:
                bucket = [s for s in bucket if s.origin == origin]
            recent = bucket[-int(max(1, limit)):]
            return [s.to_dict() for s in reversed(recent)]

    def get_stats(
        self,
        coin: str,
        *,
        origin: Optional[str] = None,
        tier: Optional[str] = None,
        window: int = _STATS_WINDOW_DEFAULT,
    ) -> dict:
        """聚合统计（仅算已 resolved 的样本）"""
        coin_u = coin.upper()
        with self._lock:
            bucket = list(self._samples.get(coin_u, []))

        filtered = [
            s for s in bucket
            if s.is_terminal() and s.is_actionable()
            and (origin is None or s.origin == origin)
            and (tier is None or s.tier == tier)
        ]
        filtered = filtered[-int(max(1, window)):]
        total = len(filtered)

        def _safe_r(s: PlanPnLSample) -> float:
            return float(s.r_multiple) if s.r_multiple is not None else 0.0

        tp_hits = sum(1 for s in filtered if s.outcome in ("tp1_hit", "tp2_hit"))
        sl_hits = sum(1 for s in filtered if s.outcome == "sl_hit")
        expired = sum(1 for s in filtered if s.outcome == "expired")
        invalid = sum(1 for s in filtered if s.outcome == "invalidated")

        # 样本量不足时返回骨架
        if total == 0:
            return {
                "coin": coin_u,
                "origin": origin or "all",
                "tier": tier or "all",
                "sample_size": 0,
                "window": int(window),
                "win_rate": 0.0,
                "avg_r": 0.0,
                "expectancy_r": 0.0,
                "tp_hits": 0, "sl_hits": 0, "expired": 0, "invalidated": 0,
                "avg_mfe_r": 0.0,
                "avg_mae_r": 0.0,
                "entry_fill_rate": 0.0,
                "trend_hint_cn": "暂无已结算样本",
            }

        actionable = [s for s in bucket if s.is_actionable()]
        entry_filled_any = sum(1 for s in actionable if s.entry_filled_ts is not None)
        entry_fill_rate = entry_filled_any / len(actionable) if actionable else 0.0

        win_rate = tp_hits / total
        avg_r = sum(_safe_r(s) for s in filtered) / total
        expectancy_r = avg_r  # 与 avg_r 同口径；保留字段方便未来扩展
        avg_mfe = sum(s.max_favorable_r for s in filtered) / total
        avg_mae = sum(s.max_adverse_r for s in filtered) / total

        return {
            "coin": coin_u,
            "origin": origin or "all",
            "tier": tier or "all",
            "sample_size": total,
            "window": int(window),
            "win_rate": round(win_rate, 4),
            "avg_r": round(avg_r, 3),
            "expectancy_r": round(expectancy_r, 3),
            "tp_hits": tp_hits,
            "sl_hits": sl_hits,
            "expired": expired,
            "invalidated": invalid,
            "avg_mfe_r": round(avg_mfe, 3),
            "avg_mae_r": round(avg_mae, 3),
            "entry_fill_rate": round(entry_fill_rate, 4),
            "trend_hint_cn": _build_trend_hint(
                win_rate, avg_r, total, entry_fill_rate
            ),
        }

    def get_origin_breakdown(self, coin: str) -> list[dict]:
        """三个 origin 并列对比，便于直接看 math vs ai 谁更准"""
        return [
            self.get_stats(coin, origin=o)
            for o in ("math", "ai", "final")
        ]

    def get_tier_breakdown(self, coin: str, origin: Optional[str] = None) -> list[dict]:
        return [
            self.get_stats(coin, origin=origin, tier=t)
            for t in ("A", "B", "C")
        ]

    # ── 内部工具 ────────────────────────────────────

    def _add_or_skip(self, sample: PlanPnLSample) -> Optional[str]:
        """按 sample_id 去重；新增返回 id，命中返回 None"""
        if not sample.sample_id or not sample.coin:
            return None
        if sample.origin not in _VALID_ORIGINS:
            return None
        # wait/avoid 不进仓；直接丢弃（除非用户想统计观望率—目前不需要）
        if sample.action not in {"long", "short"}:
            return None
        # SL / entry 不合法则丢弃
        if sample.stop_loss <= 0 or sample.entry_low <= 0:
            return None
        if sample.action == "long" and sample.stop_loss >= sample.entry_low:
            return None
        if sample.action == "short" and sample.stop_loss <= sample.entry_low:
            return None

        with self._lock:
            bucket = self._samples.setdefault(sample.coin, [])
            for existing in bucket:
                if existing.sample_id == sample.sample_id:
                    return None
            bucket.append(sample)
            # GC
            if len(bucket) > _GC_THRESHOLD:
                # 终态且超过 72h 的先 archive
                self._archive_stale_locked(bucket)
                if len(bucket) > _MAX_RECORDS_PER_COIN:
                    bucket[:] = bucket[-_MAX_RECORDS_PER_COIN:]
            self._pending_writes += 1
            if self._pending_writes >= _PERSIST_EVERY_N_CHANGES:
                self._persist_to_disk_locked()
                self._pending_writes = 0
        self._mark_tracker(sample.coin)
        return sample.sample_id

    def _archive_stale_locked(self, bucket: list[PlanPnLSample]) -> None:
        """把终态且超 72h 的样本写入 archive.jsonl，并从 live 剔除"""
        now = int(time.time())
        try:
            stale_keep = []
            stale_archive = []
            for s in bucket:
                age_since_outcome = (
                    now - s.outcome_ts if s.outcome_ts else now - s.created_ts
                )
                if s.is_terminal() and age_since_outcome > _WINDOW_SEC:
                    stale_archive.append(s)
                else:
                    stale_keep.append(s)
            if stale_archive:
                os.makedirs(os.path.dirname(self._archive_file), exist_ok=True)
                with open(self._archive_file, "a", encoding="utf-8") as f:
                    for s in stale_archive:
                        f.write(json.dumps(s.to_dict(), ensure_ascii=False) + "\n")
                bucket[:] = stale_keep
        except OSError as e:
            logger.debug("[P2.1] archive failed: %s", e)

    @staticmethod
    def _mk_sample_id(
        coin: str,
        origin: str,
        priority: int,
        action: str,
        entry_low: float,
        entry_high: Optional[float],
        sl: float,
    ) -> str:
        decimals = _PRICE_DECIMALS.get(coin.upper(), 2)
        el = round(entry_low, decimals) if decimals >= 0 else int(entry_low)
        eh = (
            round(entry_high, decimals) if entry_high is not None and decimals >= 0
            else (int(entry_high) if entry_high is not None else "")
        )
        s = round(sl, decimals) if decimals >= 0 else int(sl)
        return f"{coin.upper()}|{origin}|p{priority}|{action}|{el}-{eh}|sl{s}"

    @staticmethod
    def _price_hits_entry(sample: PlanPnLSample, price: float) -> bool:
        low = sample.entry_low
        high = sample.entry_high if sample.entry_high else low
        if sample.action == "long":
            # 回踩：价格跌入或低于 entry_zone
            return price <= high
        if sample.action == "short":
            # 反弹：价格突破 entry_zone 下沿
            return price >= low
        return False

    @staticmethod
    def _price_hits_sl(sample: PlanPnLSample, price: float) -> bool:
        if sample.action == "long":
            return price <= sample.stop_loss
        if sample.action == "short":
            return price >= sample.stop_loss
        return False

    @staticmethod
    def _price_hits_tp(sample: PlanPnLSample, price: float, tp: float) -> bool:
        if sample.action == "long":
            return price >= tp
        if sample.action == "short":
            return price <= tp
        return False

    @staticmethod
    def _r_from_price(sample: PlanPnLSample, price: float) -> float:
        """基于 entry_filled_price 和 stop_loss 计算 R multiple"""
        entry = sample.entry_filled_price or sample.entry_low
        risk = abs(entry - sample.stop_loss)
        if risk <= 0:
            return 0.0
        if sample.action == "long":
            return (price - entry) / risk
        if sample.action == "short":
            return (entry - price) / risk
        return 0.0

    def _update_extremes(self, sample: PlanPnLSample, price: float) -> bool:
        if sample.entry_filled_ts is None:
            return False
        r = self._r_from_price(sample, price)
        changed = False
        if r > sample.max_favorable_r:
            sample.max_favorable_r = r
            changed = True
        if r < sample.max_adverse_r:
            sample.max_adverse_r = r
            changed = True
        return changed

    # ── 持久化 ──────────────────────────────────────

    def _load_from_disk(self) -> None:
        if not os.path.exists(self._data_file):
            return
        try:
            with open(self._data_file, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for coin, lst in (raw.get("samples") or {}).items():
                self._samples[coin.upper()] = [
                    PlanPnLSample.from_dict(d) for d in lst if isinstance(d, dict)
                ]
            logger.info(
                "[P2.1] signal_pnl loaded | coins=%s total=%d",
                list(self._samples.keys()),
                sum(len(v) for v in self._samples.values()),
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[P2.1] load failed: %s", e)

    def _persist_to_disk_locked(self) -> None:
        try:
            os.makedirs(os.path.dirname(self._data_file), exist_ok=True)
            payload = {
                "samples": {
                    coin: [s.to_dict() for s in samples]
                    for coin, samples in self._samples.items()
                },
                "ts": int(time.time()),
            }
            tmp = self._data_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, self._data_file)
        except (OSError, TypeError) as e:
            logger.debug("[P2.1] persist failed: %s", e)

    # ── DecisionTracker D04 联动 ────────────────────

    def _mark_tracker(self, coin: str) -> None:
        try:
            from utils.decision_tracker import D, get_tracker
        except Exception:
            return
        try:
            stats = self.get_stats(coin)
            get_tracker().mark(
                D.D04_BACKTEST_LOOP,
                status="ok" if stats["sample_size"] >= 10 else "warn",
                log=False,
                coin=coin.upper(),
                pnl_sample=stats["sample_size"],
                pnl_win_rate=stats["win_rate"],
                pnl_avg_r=stats["avg_r"],
                pnl_entry_fill_rate=stats["entry_fill_rate"],
            )
        except Exception:
            logger.debug("[P2.1] D04 mark failed", exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 辅助
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _build_trend_hint(
    win_rate: float, avg_r: float, n: int, entry_fill_rate: float
) -> str:
    if n < 5:
        return f"样本不足（{n}/5）· 参考性低"
    parts = []
    if win_rate >= 0.55:
        parts.append(f"胜率 {win_rate:.0%} · 稳健")
    elif win_rate >= 0.40:
        parts.append(f"胜率 {win_rate:.0%} · 一般")
    else:
        parts.append(f"胜率 {win_rate:.0%} · 偏低")
    if avg_r >= 0.5:
        parts.append(f"avg R {avg_r:+.2f} · 正收益")
    elif avg_r >= 0:
        parts.append(f"avg R {avg_r:+.2f} · 微利")
    else:
        parts.append(f"avg R {avg_r:+.2f} · 亏损")
    if entry_fill_rate < 0.5 and n >= 10:
        parts.append(f"入场触发率 {entry_fill_rate:.0%} · 入场点偏严")
    return " · ".join(parts)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单例
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

_instance: Optional[SignalPnLTracker] = None
_instance_lock = threading.Lock()


def get_signal_pnl_tracker() -> SignalPnLTracker:
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = SignalPnLTracker()
    return _instance


def reset_for_testing(
    data_file: Optional[str] = None,
    archive_file: Optional[str] = None,
) -> SignalPnLTracker:
    global _instance
    with _instance_lock:
        _instance = SignalPnLTracker(
            data_file=data_file or _DATA_FILE,
            archive_file=archive_file or _ARCHIVE_FILE,
        )
    return _instance
