"""W4-T1：流动性墙后验报告（核心库）。

回应审计 P1-2 与 W3-T4 阶段决策：
    - W3-T4-b（trade_count 入 confidence）和 W4-T2（阈值校准）都需要"事后命中率"
      数据驱动决策。本模块输出该数据。
    - 评分性质沿用 W3-T3 口径——本模块输出"经验权重风险评分"在历史数据上的
      事后命中率（empirical hit rate），**不是统计概率**。

设计原则（按 dev-constraints）：
    1. 只读：不修改任何引擎逻辑、不动阈值、不重新打分（W4-T2 才动阈值）
    2. 纯函数：判定 / 归类 / 统计逻辑全部不带 IO，可独立单测
    3. IO 在 scripts 主入口（read jsonl + 拉 K 线 + 输出报告）
    4. 复用：归档数据来自 W1-T2 LiquidityWallArchiver；K 线来自 binance_futures
    5. 失败保守：数据缺口 → 标 outcome=insufficient_data；时间未到 → pending

输入数据（来自 W1-T2 落盘）：
    backend/data/liquidity_wall_history/{COIN}/{YYYYMMDD}.jsonl
    每行一帧 OrderbookPressureSnapshot 摘要

输出报告：
    1. wall_consumed_confidence ≥ 0.6 后验：墙真被吃了吗？
    2. break_through_risk ≥ 0.6 后验：高破穿风险墙真被打穿了吗？
    3. wall_removal_risk ≥ 0.6 后验：高撤单风险墙真撤了吗？
    4. trust_score 区分度：高 trust 墙活得更长吗？
    5. SR/SA 维度：高 SR 真反弹？高 SA 真扫单？
    6. dominant_role 后验：每类角色的样本量、典型寿命、命中率
"""
from __future__ import annotations

import json
import os
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Literal, Optional

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据模型（dataclass — 纯结构，便于序列化）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 命中结果分类（事后判定）
Outcome = Literal[
    "hit",                # 命中：评分预言被验证
    "miss",               # 未命中：评分高但事后未发生
    "partial",            # 部分命中（如价格触碰但未维持）
    "insufficient_data",  # 数据缺口（K 线缺失/窗口边界）
    "pending",            # 时间未到（窗口尚未结束）
]


@dataclass
class ZoneRecord:
    """单帧单墙的扁平记录（落盘 wall_summary 的必要字段提炼）。"""
    coin: str
    ts: int                     # 落盘帧时间戳（秒）
    wall_zone_id: str
    side: str                   # "ask" / "bid"
    price_low: float
    price_high: float
    price_mid: float
    current_usd: float
    trust_score: float
    break_through_risk: float
    wall_removal_risk: float
    wall_consumed_confidence: float
    support_resistance_trust_score: float
    sweep_attractiveness_score: float
    active_attack_score: float
    persistence_score: float
    persistence_min: float
    dominant_role: str = "ordinary"
    next_magnet_price: Optional[float] = None
    dual_source: bool = False
    coinbase_spot_confluence: bool = False
    has_spot_confluence: bool = False


@dataclass
class EventRecord:
    """单帧事件的扁平记录。"""
    coin: str
    ts: int
    wall_zone_id: str
    event_type: str
    side: str
    price_mid: float
    confidence: float
    executed_usd_value: Optional[float] = None
    size_before_usd: Optional[float] = None
    size_after_usd: Optional[float] = None


@dataclass
class KlinePoint:
    """K 线最简表示（OHLC + 时间戳秒）。"""
    ts: int                     # 开盘时间秒
    open: float
    high: float
    low: float
    close: float


@dataclass
class JudgedOutcome:
    """单笔判定结果（含元数据，便于 audit）。"""
    outcome: Outcome
    note: str = ""
    metric: float = 0.0         # 与 outcome 关联的连续数值（如穿透深度 %、维持时长秒）


@dataclass
class HitRateStats:
    """评分桶级别统计。"""
    score_threshold: float
    sample_count: int
    hits: int
    misses: int
    partials: int
    insufficient: int
    pending: int

    @property
    def evaluated(self) -> int:
        return self.hits + self.misses + self.partials

    @property
    def hit_rate(self) -> float:
        if self.evaluated == 0:
            return 0.0
        return round(self.hits / self.evaluated, 3)

    @property
    def partial_or_hit_rate(self) -> float:
        if self.evaluated == 0:
            return 0.0
        return round((self.hits + self.partials) / self.evaluated, 3)


@dataclass
class TrustBucketStat:
    bucket_label: str           # 如 "0.55-0.65"
    sample_count: int
    median_persistence_min: float
    median_visible_hours: float


@dataclass
class DominantRoleStat:
    role: str
    sample_count: int
    avg_trust: float
    avg_sr: float
    avg_sa: float
    median_persistence_min: float


@dataclass
class ZoneBandProfile:
    """P4：每价位带（wall_zone_id 桶）的历史画像。

    - presence_ratio：窗口内出现小时占比（0-1）
    - avg_lifetime_min：按 episode（帧间断 >30min 视为新一段）的平均寿命
    - consumed_ratio：consumed/(consumed+removed)，同类事件按 10min 桶去重
    - sr_hold_rate：作为支撑/阻力被"测试"（价格触及价区）后守住的比率；
      未被触及的 episode 不计入测试
    """
    wall_zone_id: str
    side: str
    price_mid: float
    frames_seen: int = 0
    hours_present: int = 0
    presence_ratio: float = 0.0
    episode_count: int = 0
    avg_lifetime_min: float = 0.0
    consumed_events: int = 0
    removed_events: int = 0
    consumed_ratio: Optional[float] = None
    sr_tests: int = 0
    sr_holds: int = 0
    sr_hold_rate: Optional[float] = None


@dataclass
class PostmortemReport:
    """完整后验报告（机器可读）。"""
    coin: str
    window_start_ts: int
    window_end_ts: int
    total_snapshots: int
    total_zone_records: int
    total_events: int

    # 各类后验
    consumed_stats: HitRateStats
    break_through_stats: HitRateStats
    removal_stats: HitRateStats

    # 区分度
    trust_buckets: list[TrustBucketStat] = field(default_factory=list)
    dominant_role_stats: list[DominantRoleStat] = field(default_factory=list)

    # 高 SR / 高 SA 的事后表现
    high_sr_hit_rate: HitRateStats = field(
        default_factory=lambda: HitRateStats(0.7, 0, 0, 0, 0, 0, 0)
    )
    high_sa_hit_rate: HitRateStats = field(
        default_factory=lambda: HitRateStats(0.6, 0, 0, 0, 0, 0, 0)
    )

    # P4：每价位带画像
    zone_band_profiles: list["ZoneBandProfile"] = field(default_factory=list)

    # 数据质量
    insufficient_kline_zones: int = 0
    notes: list[str] = field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 输入解析（纯函数）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_archived_snapshots(lines: Iterable[str]) -> list[dict]:
    """读 archiver 输出的 JSONL 行，每行解析为 dict。

    跳过空行 / 无法解析的行（best-effort），返回所有合法 dict。
    """
    out: list[dict] = []
    for raw in lines:
        s = raw.strip()
        if not s:
            continue
        try:
            d = json.loads(s)
            if isinstance(d, dict):
                out.append(d)
        except (json.JSONDecodeError, ValueError):
            continue
    return out


def extract_zone_records(snapshots: list[dict]) -> list[ZoneRecord]:
    """从 snapshot dict 中扁平化所有 wall（above + below）为 ZoneRecord。

    向前兼容：缺字段用默认值（dataclass 默认值兜底）。
    """
    out: list[ZoneRecord] = []
    for s in snapshots:
        coin = str(s.get("coin", ""))
        ts = int(s.get("ts", 0) or 0)
        if not coin or ts <= 0:
            continue
        for key in ("walls_above", "walls_below"):
            for w in s.get(key, []) or []:
                if not isinstance(w, dict):
                    continue
                try:
                    rec = ZoneRecord(
                        coin=coin,
                        ts=ts,
                        wall_zone_id=str(w.get("wall_zone_id", "") or ""),
                        side=str(w.get("side", "")),
                        price_low=float(w.get("price_low", 0) or 0),
                        price_high=float(w.get("price_high", 0) or 0),
                        price_mid=float(w.get("price_mid", 0) or 0),
                        current_usd=float(w.get("current_usd", 0) or 0),
                        trust_score=float(w.get("trust_score", 0) or 0),
                        break_through_risk=float(w.get("break_through_risk", 0) or 0),
                        wall_removal_risk=float(w.get("wall_removal_risk", 0) or 0),
                        wall_consumed_confidence=float(
                            w.get("wall_consumed_confidence", 0) or 0
                        ),
                        support_resistance_trust_score=float(
                            w.get("support_resistance_trust_score", 0) or 0
                        ),
                        sweep_attractiveness_score=float(
                            w.get("sweep_attractiveness_score", 0) or 0
                        ),
                        active_attack_score=float(
                            w.get("active_attack_score", 0) or 0
                        ),
                        persistence_score=float(w.get("persistence_score", 0) or 0),
                        persistence_min=float(w.get("persistence_min", 0) or 0),
                        dominant_role=str(w.get("dominant_role", "ordinary") or "ordinary"),
                        next_magnet_price=(
                            float(w["next_magnet_price"])
                            if w.get("next_magnet_price") is not None
                            else None
                        ),
                        dual_source=bool(w.get("dual_source", False)),
                        coinbase_spot_confluence=bool(
                            w.get("coinbase_spot_confluence", False)
                        ),
                        has_spot_confluence=bool(w.get("has_spot_confluence", False)),
                    )
                    out.append(rec)
                except (TypeError, ValueError, KeyError):
                    continue
    return out


def extract_event_records(snapshots: list[dict]) -> list[EventRecord]:
    """从 snapshot 中扁平化 wall_events 为 EventRecord。"""
    out: list[EventRecord] = []
    for s in snapshots:
        coin = str(s.get("coin", ""))
        for ev in s.get("wall_events", []) or []:
            if not isinstance(ev, dict):
                continue
            ts = int(ev.get("ts", 0) or 0)
            if ts <= 0:
                continue
            try:
                out.append(EventRecord(
                    coin=coin,
                    ts=ts,
                    wall_zone_id=str(ev.get("wall_zone_id", "") or ""),
                    event_type=str(ev.get("event_type", "")),
                    side=str(ev.get("side", "")),
                    price_mid=float(ev.get("price_mid", 0) or 0),
                    confidence=float(ev.get("confidence", 0) or 0),
                    executed_usd_value=(
                        float(ev["executed_usd_value"])
                        if ev.get("executed_usd_value") is not None
                        else None
                    ),
                    size_before_usd=(
                        float(ev["size_before_usd"])
                        if ev.get("size_before_usd") is not None
                        else None
                    ),
                    size_after_usd=(
                        float(ev["size_after_usd"])
                        if ev.get("size_after_usd") is not None
                        else None
                    ),
                ))
            except (TypeError, ValueError, KeyError):
                continue
    return out


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# K 线工具（纯函数）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def parse_binance_klines(raw: list[list[Any]]) -> list[KlinePoint]:
    """Binance /klines 原始返回 → KlinePoint 列表（按 ts 升序）。

    Binance 元素格式：[openTime_ms, open, high, low, close, volume, closeTime_ms, ...]
    """
    out: list[KlinePoint] = []
    for row in raw:
        try:
            ts_ms = int(row[0])
            out.append(KlinePoint(
                ts=ts_ms // 1000,
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
            ))
        except (TypeError, ValueError, IndexError):
            continue
    out.sort(key=lambda k: k.ts)
    return out


def klines_in_range(
    klines: list[KlinePoint], start_ts: int, end_ts: int,
) -> list[KlinePoint]:
    """筛选 [start_ts, end_ts) 范围内的 K 线（端点已规整为秒）。"""
    if start_ts >= end_ts:
        return []
    return [k for k in klines if start_ts <= k.ts < end_ts]


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 判定函数（纯函数 — 可独立单测）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def judge_consumed_outcome(
    rec: ZoneRecord,
    klines: list[KlinePoint],
    *,
    window_sec: int = 30 * 60,
    threshold: float = 0.6,
    now_ts: Optional[int] = None,
) -> JudgedOutcome:
    """W1-T2 落盘了 wall_consumed_confidence；本函数判断"该高分墙是否真被吃"。

    判定规则（bid 墙）：
      - 命中（hit）：window 内任意 K 线 close < price_low（真破位）
      - 部分（partial）：window 内任意 K 线 low < price_low 但所有 close 都回到 price_low 之上
      - 未命中（miss）：window 内 low 始终 ≥ price_low
    ask 墙对称（high > price_high 为命中）。
    """
    if rec.wall_consumed_confidence < threshold:
        return JudgedOutcome("miss", note="below threshold (not in evaluation set)")
    return _judge_breakout_in_window(
        rec, klines, window_sec=window_sec, now_ts=now_ts,
    )


def judge_break_through_outcome(
    rec: ZoneRecord,
    klines: list[KlinePoint],
    *,
    window_sec: int = 2 * 3600,
    threshold: float = 0.6,
    now_ts: Optional[int] = None,
) -> JudgedOutcome:
    """break_through_risk ≥ 0.6 → 价格在 window_sec 内是否真打穿到 next_magnet。

    判定规则（bid 墙）：
      - 命中：window 内 K 线 low ≤ next_magnet_price（即从 wall 穿到磁铁）
      - 部分：window 内 close < price_low 但未触及 magnet（仅穿墙未到磁铁）
      - 未命中：close 始终 ≥ price_low（墙保住了）
    缺 next_magnet_price → 退化为"穿墙"判定（同 consumed）。
    """
    if rec.break_through_risk < threshold:
        return JudgedOutcome("miss", note="below threshold (not in evaluation set)")

    if rec.next_magnet_price is None:
        return _judge_breakout_in_window(
            rec, klines, window_sec=window_sec, now_ts=now_ts,
        )

    end_ts = rec.ts + window_sec
    if now_ts is not None and end_ts > now_ts:
        return JudgedOutcome("pending", note="window not closed")

    window_kl = klines_in_range(klines, rec.ts, end_ts)
    if not window_kl:
        return JudgedOutcome("insufficient_data", note="no klines in window")

    magnet = rec.next_magnet_price
    if rec.side == "bid":
        # 价格应跌：到达 magnet 命中；只穿墙不到 magnet 部分；未穿墙未命中
        min_low = min(k.low for k in window_kl)
        worst_close = min(k.close for k in window_kl)
        if min_low <= magnet:
            depth = (rec.price_low - min_low) / max(rec.price_low, 1e-9) * 100
            return JudgedOutcome("hit", note=f"reached magnet @ low={min_low:.2f}",
                                 metric=depth)
        if worst_close < rec.price_low:
            depth = (rec.price_low - worst_close) / max(rec.price_low, 1e-9) * 100
            return JudgedOutcome("partial",
                                 note="broke wall but did not reach magnet",
                                 metric=depth)
        return JudgedOutcome("miss", note="wall held")
    elif rec.side == "ask":
        max_high = max(k.high for k in window_kl)
        peak_close = max(k.close for k in window_kl)
        if max_high >= magnet:
            depth = (max_high - rec.price_high) / max(rec.price_high, 1e-9) * 100
            return JudgedOutcome("hit", note=f"reached magnet @ high={max_high:.2f}",
                                 metric=depth)
        if peak_close > rec.price_high:
            depth = (peak_close - rec.price_high) / max(rec.price_high, 1e-9) * 100
            return JudgedOutcome("partial",
                                 note="broke wall but did not reach magnet",
                                 metric=depth)
        return JudgedOutcome("miss", note="wall held")
    return JudgedOutcome("insufficient_data", note=f"unknown side: {rec.side}")


def judge_removal_outcome(
    rec: ZoneRecord,
    events: list[EventRecord],
    *,
    window_sec: int = 30 * 60,
    threshold: float = 0.6,
    now_ts: Optional[int] = None,
) -> JudgedOutcome:
    """wall_removal_risk ≥ 0.6 → window_sec 内是否真产生 wall_removed 或
    wall_consumed_and_removed 事件（同 wall_zone_id，未来时刻）。

    判定规则：
      - 命中：window 内出现 wall_removed / wall_consumed_and_removed
      - 部分：window 内出现 wall_consumed（被吃 ≠ 撤单，但行为相关）
      - 未命中：window 内无相关事件
    """
    if rec.wall_removal_risk < threshold:
        return JudgedOutcome("miss", note="below threshold (not in evaluation set)")

    end_ts = rec.ts + window_sec
    if now_ts is not None and end_ts > now_ts:
        return JudgedOutcome("pending", note="window not closed")

    if not rec.wall_zone_id:
        return JudgedOutcome("insufficient_data", note="missing wall_zone_id")

    related_events = [
        e for e in events
        if e.wall_zone_id == rec.wall_zone_id
        and rec.ts < e.ts <= end_ts
    ]
    removed_events = [
        e for e in related_events
        if e.event_type in ("wall_removed", "wall_consumed_and_removed")
    ]
    consumed_events = [
        e for e in related_events
        if e.event_type == "wall_consumed"
    ]

    if removed_events:
        return JudgedOutcome("hit",
                             note=f"{len(removed_events)} removal event(s)",
                             metric=float(len(removed_events)))
    if consumed_events:
        return JudgedOutcome("partial",
                             note="consumed but not formally removed",
                             metric=float(len(consumed_events)))
    return JudgedOutcome("miss", note="no related event in window")


def _judge_breakout_in_window(
    rec: ZoneRecord,
    klines: list[KlinePoint],
    *,
    window_sec: int,
    now_ts: Optional[int] = None,
) -> JudgedOutcome:
    """通用"墙是否被穿"判定（无 magnet 版本）。"""
    end_ts = rec.ts + window_sec
    if now_ts is not None and end_ts > now_ts:
        return JudgedOutcome("pending", note="window not closed")

    window_kl = klines_in_range(klines, rec.ts, end_ts)
    if not window_kl:
        return JudgedOutcome("insufficient_data", note="no klines in window")

    if rec.side == "bid":
        min_low = min(k.low for k in window_kl)
        worst_close = min(k.close for k in window_kl)
        if worst_close < rec.price_low:
            depth = (rec.price_low - worst_close) / max(rec.price_low, 1e-9) * 100
            return JudgedOutcome("hit", note="close below price_low", metric=depth)
        if min_low < rec.price_low:
            return JudgedOutcome("partial",
                                 note="touched but close above price_low")
        return JudgedOutcome("miss", note="wall held")
    elif rec.side == "ask":
        max_high = max(k.high for k in window_kl)
        peak_close = max(k.close for k in window_kl)
        if peak_close > rec.price_high:
            depth = (peak_close - rec.price_high) / max(rec.price_high, 1e-9) * 100
            return JudgedOutcome("hit", note="close above price_high", metric=depth)
        if max_high > rec.price_high:
            return JudgedOutcome("partial",
                                 note="touched but close below price_high")
        return JudgedOutcome("miss", note="wall held")
    return JudgedOutcome("insufficient_data", note=f"unknown side: {rec.side}")


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 统计聚合（纯函数）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def aggregate_outcomes(
    outcomes: list[JudgedOutcome], threshold: float,
) -> HitRateStats:
    """汇总 JudgedOutcome 列表为 HitRateStats（按阈值分桶后已筛过的样本）。"""
    hits = sum(1 for o in outcomes if o.outcome == "hit")
    misses = sum(1 for o in outcomes if o.outcome == "miss"
                 and o.note != "below threshold (not in evaluation set)")
    partials = sum(1 for o in outcomes if o.outcome == "partial")
    insufficient = sum(1 for o in outcomes if o.outcome == "insufficient_data")
    pending = sum(1 for o in outcomes if o.outcome == "pending")
    excluded = sum(1 for o in outcomes if o.outcome == "miss"
                   and o.note == "below threshold (not in evaluation set)")

    return HitRateStats(
        score_threshold=threshold,
        sample_count=len(outcomes) - excluded,
        hits=hits,
        misses=misses,
        partials=partials,
        insufficient=insufficient,
        pending=pending,
    )


def compute_trust_buckets(records: list[ZoneRecord]) -> list[TrustBucketStat]:
    """按 trust_score 分桶统计 persistence。

    桶：[0.55, 0.65) / [0.65, 0.75) / [0.75, 1.0]
    """
    buckets: dict[str, list[ZoneRecord]] = {
        "0.55-0.65": [],
        "0.65-0.75": [],
        "0.75-1.00": [],
    }
    for r in records:
        if r.trust_score < 0.55:
            continue
        if r.trust_score < 0.65:
            buckets["0.55-0.65"].append(r)
        elif r.trust_score < 0.75:
            buckets["0.65-0.75"].append(r)
        else:
            buckets["0.75-1.00"].append(r)

    out: list[TrustBucketStat] = []
    for label, recs in buckets.items():
        if not recs:
            out.append(TrustBucketStat(label, 0, 0.0, 0.0))
            continue
        persists = [r.persistence_min for r in recs]
        out.append(TrustBucketStat(
            bucket_label=label,
            sample_count=len(recs),
            median_persistence_min=round(statistics.median(persists), 2),
            median_visible_hours=round(statistics.median(persists) / 60, 2),
        ))
    return out


def compute_dominant_role_stats(
    records: list[ZoneRecord],
) -> list[DominantRoleStat]:
    """按 dominant_role 分组统计。"""
    by_role: dict[str, list[ZoneRecord]] = {}
    for r in records:
        by_role.setdefault(r.dominant_role or "ordinary", []).append(r)

    out: list[DominantRoleStat] = []
    for role, recs in sorted(by_role.items()):
        trusts = [r.trust_score for r in recs]
        srs = [r.support_resistance_trust_score for r in recs]
        sas = [r.sweep_attractiveness_score for r in recs]
        persists = [r.persistence_min for r in recs]
        out.append(DominantRoleStat(
            role=role,
            sample_count=len(recs),
            avg_trust=round(statistics.mean(trusts), 3) if trusts else 0.0,
            avg_sr=round(statistics.mean(srs), 3) if srs else 0.0,
            avg_sa=round(statistics.mean(sas), 3) if sas else 0.0,
            median_persistence_min=(
                round(statistics.median(persists), 2) if persists else 0.0
            ),
        ))
    return out


def compute_zone_band_profiles(
    zone_records: list[ZoneRecord],
    event_records: list[EventRecord],
    klines: list[KlinePoint],
    *,
    episode_gap_sec: int = 1800,
    sr_window_sec: int = 2 * 3600,
    event_dedup_bucket_sec: int = 600,
    window_hours: Optional[float] = None,
    now_ts: Optional[int] = None,
) -> list[ZoneBandProfile]:
    """P4：按 wall_zone_id 聚合每价位带画像（出现率/寿命/兑现率/SR 测试成功率）。

    纯函数；klines 为空时 sr_tests/sr_holds 保持 0（sr_hold_rate=None）。
    window_hours 不传时用观测窗口（首尾帧跨度）；运行时画像（7 天固定窗口）
    由调用方显式传入 window_days*24，避免"归档只有 2 天却按 2 天算满勤"。
    """
    by_zone: dict[str, list[ZoneRecord]] = {}
    for r in zone_records:
        if r.wall_zone_id:
            by_zone.setdefault(r.wall_zone_id, []).append(r)
    if not by_zone:
        return []

    ts_min = min(r.ts for r in zone_records)
    ts_max = max(r.ts for r in zone_records)
    if window_hours is None:
        window_hours = max((ts_max - ts_min) / 3600.0, 1.0)
    window_hours = max(float(window_hours), 1.0)

    # 事件按 (zone_id, event_type, 10min 桶) 去重 —— 引擎在条件持续期间
    # 每帧都会重复发同一事件，直接计数会导致兑现率虚高
    consumed_by_zone: dict[str, set[int]] = {}
    removed_by_zone: dict[str, set[int]] = {}
    for ev in event_records:
        if not ev.wall_zone_id:
            continue
        bucket = ev.ts // max(event_dedup_bucket_sec, 1)
        if ev.event_type in ("wall_consumed", "wall_consumed_and_removed"):
            consumed_by_zone.setdefault(ev.wall_zone_id, set()).add(bucket)
        if ev.event_type in ("wall_removed", "wall_consumed_and_removed"):
            removed_by_zone.setdefault(ev.wall_zone_id, set()).add(bucket)

    out: list[ZoneBandProfile] = []
    for zid, recs in by_zone.items():
        recs.sort(key=lambda r: r.ts)
        hours_present = len({r.ts // 3600 for r in recs})

        # episode 切分：相邻帧间隔 > episode_gap_sec 视为新一段
        episodes: list[tuple[ZoneRecord, ZoneRecord]] = []   # (first, last)
        ep_first = recs[0]
        prev = recs[0]
        for r in recs[1:]:
            if r.ts - prev.ts > episode_gap_sec:
                episodes.append((ep_first, prev))
                ep_first = r
            prev = r
        episodes.append((ep_first, prev))
        lifetimes_min = [(last.ts - first.ts) / 60.0 for first, last in episodes]

        consumed_n = len(consumed_by_zone.get(zid, ()))
        removed_n = len(removed_by_zone.get(zid, ()))
        consumed_ratio = (
            round(consumed_n / (consumed_n + removed_n), 3)
            if (consumed_n + removed_n) > 0 else None
        )

        # SR 测试：每个 episode 用首帧判定 —— partial=触及但收盘守住（成功），
        # hit=收盘破区（失败），miss=从未触及（不算测试）
        sr_tests = 0
        sr_holds = 0
        if klines:
            for first, _last in episodes:
                o = _judge_breakout_in_window(
                    first, klines, window_sec=sr_window_sec, now_ts=now_ts,
                )
                if o.outcome == "partial":
                    sr_tests += 1
                    sr_holds += 1
                elif o.outcome == "hit":
                    sr_tests += 1

        mid_prices = sorted(r.price_mid for r in recs)
        out.append(ZoneBandProfile(
            wall_zone_id=zid,
            side=recs[0].side,
            price_mid=round(mid_prices[len(mid_prices) // 2], 4),
            frames_seen=len(recs),
            hours_present=hours_present,
            presence_ratio=round(min(hours_present / window_hours, 1.0), 3),
            episode_count=len(episodes),
            avg_lifetime_min=round(statistics.mean(lifetimes_min), 1)
                if lifetimes_min else 0.0,
            consumed_events=consumed_n,
            removed_events=removed_n,
            consumed_ratio=consumed_ratio,
            sr_tests=sr_tests,
            sr_holds=sr_holds,
            sr_hold_rate=round(sr_holds / sr_tests, 3) if sr_tests > 0 else None,
        ))

    out.sort(key=lambda p: -p.presence_ratio)
    return out


def deduplicate_zones_by_first_high_score(
    records: list[ZoneRecord],
    *,
    score_attr: str,
    threshold: float,
) -> list[ZoneRecord]:
    """同一个 wall_zone_id 在多帧都被标 ≥ threshold，仅取首次出现那一帧。

    防止同一面墙被反复计数（持续 30min 的墙会有 6 帧，每帧都跨阈值）。
    """
    seen: dict[str, ZoneRecord] = {}
    for r in sorted(records, key=lambda x: x.ts):
        score = float(getattr(r, score_attr, 0))
        if score < threshold:
            continue
        if not r.wall_zone_id:
            # 无 ID 的墙逐帧计入（无法去重）—— 但加唯一 key 防累计
            seen[f"{r.coin}-{r.ts}-{r.price_mid}"] = r
            continue
        if r.wall_zone_id not in seen:
            seen[r.wall_zone_id] = r
    return list(seen.values())


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 报告组装（纯函数）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def build_report(
    coin: str,
    snapshots: list[dict],
    klines: list[KlinePoint],
    *,
    now_ts: Optional[int] = None,
    window_consumed_sec: int = 30 * 60,
    window_break_through_sec: int = 2 * 3600,
    window_removal_sec: int = 30 * 60,
) -> PostmortemReport:
    """端到端组装后验报告。"""
    zone_records = extract_zone_records(snapshots)
    event_records = extract_event_records(snapshots)

    if zone_records:
        ts_min = min(r.ts for r in zone_records)
        ts_max = max(r.ts for r in zone_records)
    else:
        ts_min = ts_max = 0

    # —— 1. wall_consumed_confidence ≥ 0.6 后验 ——
    consumed_zones = deduplicate_zones_by_first_high_score(
        zone_records, score_attr="wall_consumed_confidence", threshold=0.6,
    )
    consumed_outcomes = [
        judge_consumed_outcome(z, klines, window_sec=window_consumed_sec,
                               now_ts=now_ts)
        for z in consumed_zones
    ]
    consumed_stats = aggregate_outcomes(consumed_outcomes, threshold=0.6)

    # —— 2. break_through_risk ≥ 0.6 后验 ——
    bt_zones = deduplicate_zones_by_first_high_score(
        zone_records, score_attr="break_through_risk", threshold=0.6,
    )
    bt_outcomes = [
        judge_break_through_outcome(z, klines,
                                    window_sec=window_break_through_sec,
                                    now_ts=now_ts)
        for z in bt_zones
    ]
    bt_stats = aggregate_outcomes(bt_outcomes, threshold=0.6)

    # —— 3. wall_removal_risk ≥ 0.6 后验 ——
    rm_zones = deduplicate_zones_by_first_high_score(
        zone_records, score_attr="wall_removal_risk", threshold=0.6,
    )
    rm_outcomes = [
        judge_removal_outcome(z, event_records,
                              window_sec=window_removal_sec, now_ts=now_ts)
        for z in rm_zones
    ]
    rm_stats = aggregate_outcomes(rm_outcomes, threshold=0.6)

    # —— 4. trust 区分度 ——
    trust_buckets = compute_trust_buckets(zone_records)

    # —— 5. dominant_role 统计 ——
    role_stats = compute_dominant_role_stats(zone_records)

    # —— 6. SR / SA 后验（高 SR 借 consumed 反向逻辑：高 SR 应"未被吃") ——
    sr_zones = deduplicate_zones_by_first_high_score(
        zone_records, score_attr="support_resistance_trust_score", threshold=0.7,
    )
    sr_outcomes = [
        judge_consumed_outcome(z, klines,
                               window_sec=window_break_through_sec,
                               threshold=0.0,
                               now_ts=now_ts)
        for z in sr_zones
    ]
    # 高 SR：希望"未被打穿"才是好——所以 hit 反而不好。我们重新反转：
    sr_inverted = [
        JudgedOutcome(
            outcome=("miss" if o.outcome == "hit"
                     else ("hit" if o.outcome == "miss" else o.outcome)),
            note=f"inverted: {o.note}",
            metric=o.metric,
        )
        for o in sr_outcomes
    ]
    sr_stats = aggregate_outcomes(sr_inverted, threshold=0.7)
    sr_stats.score_threshold = 0.7

    sa_zones = deduplicate_zones_by_first_high_score(
        zone_records, score_attr="sweep_attractiveness_score", threshold=0.6,
    )
    sa_outcomes = [
        judge_break_through_outcome(z, klines,
                                    window_sec=window_break_through_sec,
                                    threshold=0.0,
                                    now_ts=now_ts)
        for z in sa_zones
    ]
    sa_stats = aggregate_outcomes(sa_outcomes, threshold=0.6)

    # —— 7. P4 每价位带画像 ——
    band_profiles = compute_zone_band_profiles(
        zone_records, event_records, klines, now_ts=now_ts,
    )

    # —— 数据质量 ——
    insuf = sum(1 for o in (consumed_outcomes + bt_outcomes)
                if o.outcome == "insufficient_data")

    notes: list[str] = []
    if not zone_records:
        notes.append("no zone records — archive empty or window mismatched")
    if klines and zone_records:
        kl_start = klines[0].ts
        kl_end = klines[-1].ts
        if kl_start > ts_min:
            notes.append(
                f"klines start ({kl_start}) later than first zone ts ({ts_min})"
            )
        if kl_end < ts_max:
            notes.append(
                f"klines end ({kl_end}) earlier than last zone ts ({ts_max})"
            )
    if consumed_stats.evaluated < 30:
        notes.append(
            "consumed sample size < 30; hit_rate is not statistically reliable"
        )
    if bt_stats.evaluated < 30:
        notes.append(
            "break_through sample size < 30; hit_rate is not statistically reliable"
        )

    return PostmortemReport(
        coin=coin,
        window_start_ts=ts_min,
        window_end_ts=ts_max,
        total_snapshots=len(snapshots),
        total_zone_records=len(zone_records),
        total_events=len(event_records),
        consumed_stats=consumed_stats,
        break_through_stats=bt_stats,
        removal_stats=rm_stats,
        trust_buckets=trust_buckets,
        dominant_role_stats=role_stats,
        high_sr_hit_rate=sr_stats,
        high_sa_hit_rate=sa_stats,
        zone_band_profiles=band_profiles,
        insufficient_kline_zones=insuf,
        notes=notes,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 报告渲染（纯函数）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def report_to_dict(rep: PostmortemReport) -> dict:
    """PostmortemReport → JSON-serializable dict。"""
    def _stat(s: HitRateStats) -> dict:
        return {
            "score_threshold": s.score_threshold,
            "sample_count": s.sample_count,
            "hits": s.hits,
            "misses": s.misses,
            "partials": s.partials,
            "insufficient": s.insufficient,
            "pending": s.pending,
            "evaluated": s.evaluated,
            "hit_rate": s.hit_rate,
            "partial_or_hit_rate": s.partial_or_hit_rate,
        }
    return {
        "coin": rep.coin,
        "window_start_ts": rep.window_start_ts,
        "window_end_ts": rep.window_end_ts,
        "totals": {
            "snapshots": rep.total_snapshots,
            "zone_records": rep.total_zone_records,
            "events": rep.total_events,
        },
        "consumed_stats": _stat(rep.consumed_stats),
        "break_through_stats": _stat(rep.break_through_stats),
        "removal_stats": _stat(rep.removal_stats),
        "high_sr_hit_rate": _stat(rep.high_sr_hit_rate),
        "high_sa_hit_rate": _stat(rep.high_sa_hit_rate),
        "trust_buckets": [
            {
                "bucket_label": b.bucket_label,
                "sample_count": b.sample_count,
                "median_persistence_min": b.median_persistence_min,
                "median_visible_hours": b.median_visible_hours,
            }
            for b in rep.trust_buckets
        ],
        "dominant_role_stats": [
            {
                "role": d.role,
                "sample_count": d.sample_count,
                "avg_trust": d.avg_trust,
                "avg_sr": d.avg_sr,
                "avg_sa": d.avg_sa,
                "median_persistence_min": d.median_persistence_min,
            }
            for d in rep.dominant_role_stats
        ],
        "zone_band_profiles": [
            {
                "wall_zone_id": p.wall_zone_id,
                "side": p.side,
                "price_mid": p.price_mid,
                "frames_seen": p.frames_seen,
                "hours_present": p.hours_present,
                "presence_ratio": p.presence_ratio,
                "episode_count": p.episode_count,
                "avg_lifetime_min": p.avg_lifetime_min,
                "consumed_events": p.consumed_events,
                "removed_events": p.removed_events,
                "consumed_ratio": p.consumed_ratio,
                "sr_tests": p.sr_tests,
                "sr_holds": p.sr_holds,
                "sr_hold_rate": p.sr_hold_rate,
            }
            for p in rep.zone_band_profiles
        ],
        "insufficient_kline_zones": rep.insufficient_kline_zones,
        "notes": list(rep.notes),
    }


def report_to_markdown(rep: PostmortemReport) -> str:
    """PostmortemReport → 人类可读 Markdown。

    强调评分性质：所有 hit_rate 都是"事后命中率"（empirical），
    不输出"概率"措辞，与 W3-T3 口径一致。
    """
    def _fmt_ts(ts: int) -> str:
        if ts <= 0:
            return "—"
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M UTC"
        )

    def _stat_section(title: str, s: HitRateStats, hint: str = "") -> list[str]:
        lines = [f"### {title}"]
        if hint:
            lines.append(f"> {hint}")
        lines.append(
            f"- 阈值：≥ {s.score_threshold}　|　样本：{s.sample_count}"
            f"（评估 {s.evaluated} / 待定 {s.pending} / 数据缺 {s.insufficient}）"
        )
        if s.evaluated == 0:
            lines.append("- 评估样本为 0，无法给出命中率。")
            return lines
        lines.append(
            f"- **命中率（hit）**：{s.hit_rate:.1%}（{s.hits}/{s.evaluated}）"
        )
        lines.append(
            f"- 部分命中或命中（partial+hit）：{s.partial_or_hit_rate:.1%}"
            f"（{s.hits + s.partials}/{s.evaluated}）"
        )
        if s.evaluated < 30:
            lines.append(
                "- ⚠ 评估样本 < 30，统计意义不显著（请扩大窗口或等待积累）"
            )
        return lines

    out: list[str] = []
    out.append(f"# 流动性墙后验报告 — {rep.coin}")
    out.append("")
    out.append(
        "> 本报告输出**经验权重风险评分在历史数据上的事后命中率**（empirical "
        "hit rate），**不是统计概率**。所有评分语义与 W3-T3 口径一致。"
    )
    out.append("")
    out.append("## 窗口与样本量")
    out.append(
        f"- 数据窗口：{_fmt_ts(rep.window_start_ts)} → {_fmt_ts(rep.window_end_ts)}"
    )
    out.append(f"- 落盘帧：{rep.total_snapshots}　|　wall 记录：{rep.total_zone_records}"
               f"　|　事件：{rep.total_events}")
    if rep.notes:
        out.append("")
        out.append("### 数据质量备注")
        for n in rep.notes:
            out.append(f"- {n}")
    out.append("")

    out.append("## 1. wall_consumed_confidence ≥ 0.6 后验")
    out.extend(_stat_section(
        "墙真被吃了吗？",
        rep.consumed_stats,
        hint="高 confidence 应预示价格在 30min 内真的破墙",
    ))
    out.append("")

    out.append("## 2. break_through_risk ≥ 0.6 后验")
    out.extend(_stat_section(
        "高破穿风险墙真被打穿到磁铁了吗？",
        rep.break_through_stats,
        hint="hit=穿到 next_magnet；partial=只穿墙未到磁铁；miss=墙保住了",
    ))
    out.append("")

    out.append("## 3. wall_removal_risk ≥ 0.6 后验")
    out.extend(_stat_section(
        "高撤单风险墙真撤了吗？",
        rep.removal_stats,
        hint="hit=出现 wall_removed/wall_consumed_and_removed；"
             "partial=被吃但未正式撤；miss=墙未消失",
    ))
    out.append("")

    out.append("## 4. trust_score 区分度")
    out.append("| trust 桶 | 样本 | persistence 中位（min） | 中位寿命（h） |")
    out.append("|---|---:|---:|---:|")
    for b in rep.trust_buckets:
        out.append(
            f"| {b.bucket_label} | {b.sample_count} | "
            f"{b.median_persistence_min} | {b.median_visible_hours} |"
        )
    out.append("")

    out.append("## 5. dominant_role 分布")
    out.append("| 角色 | 样本 | avg_trust | avg_SR | avg_SA | 中位寿命（min） |")
    out.append("|---|---:|---:|---:|---:|---:|")
    for d in rep.dominant_role_stats:
        out.append(
            f"| {d.role} | {d.sample_count} | {d.avg_trust} | "
            f"{d.avg_sr} | {d.avg_sa} | {d.median_persistence_min} |"
        )
    out.append("")

    out.append("## 6. SR / SA 后验（W2-T1）")
    out.extend(_stat_section(
        "高 SR (≥0.7) 真的「挡住了」吗？",
        rep.high_sr_hit_rate,
        hint="SR 高 → 应作为支撑/阻力起作用 → 价格不应破墙；hit=未被打穿",
    ))
    out.append("")
    out.extend(_stat_section(
        "高 SA (≥0.6) 真的「被扫了」吗？",
        rep.high_sa_hit_rate,
        hint="SA 高 → 应作为清算磁铁吸引价格；hit=价格在 2h 内触及磁铁",
    ))
    out.append("")

    out.append("## 7. 每价位带画像（P4，按出现率降序 Top 15）")
    if rep.zone_band_profiles:
        out.append("| 价位 | 侧 | 出现率 | 平均寿命(min) | consumed | removed | "
                   "吃单兑现率 | SR测试 | 守住率 |")
        out.append("|---:|---|---:|---:|---:|---:|---:|---:|---:|")
        for p in rep.zone_band_profiles[:15]:
            cr = f"{p.consumed_ratio:.0%}" if p.consumed_ratio is not None else "—"
            hr = f"{p.sr_hold_rate:.0%}" if p.sr_hold_rate is not None else "—"
            out.append(
                f"| {p.price_mid} | {'卖' if p.side == 'ask' else '买'} | "
                f"{p.presence_ratio:.0%} | {p.avg_lifetime_min} | "
                f"{p.consumed_events} | {p.removed_events} | {cr} | "
                f"{p.sr_tests} | {hr} |"
            )
    else:
        out.append("- 无带 wall_zone_id 的记录，无法生成画像。")
    out.append("")

    out.append("---")
    out.append(
        "**校准建议**：本报告**不修改任何阈值**。如发现命中率显著偏离预期"
        "（如 break_through hit_rate < 30%），请进入 W4-T2 阶段做阈值校准；"
        "建议样本量 ≥ 100 后再启动校准。"
    )
    out.append("")

    return "\n".join(out)
