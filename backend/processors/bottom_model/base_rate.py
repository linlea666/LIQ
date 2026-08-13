"""历史频率层：把模型分数对照实测的历史结果。

本模块回答的**不是**"现在见底的概率是多少"，而是"历史上模型给出同类读数时，
往后 13/26/52 周的实际结果分布如何"。两者的区别是本模块存在的全部理由——
前者需要一个可外推的概率模型（本项目没有，也不打算假装有），后者只是条件
分布的观测值，是对规则引擎的一次**独立检验**。

三条防自欺的口径：

1. **有效样本量 = 前向窗口互不重叠的最大观测数**，不是时点数。周级采样下连续
   30 周处于同一象限会产出 30 个时点，但它们的 26 周前向收益几乎是同一件事。
   取一个贪心的不重叠子集（选中一点后跳过其后 N 周），少于 5 个时只给线索、
   不给百分比——此时 hit_rate 与 median_return 直接置空，避免任何消费方
   （前端、证据包、原始 JSON）把 n=2 的 100% 当结论。
   另外附带"独立事件段数"（相邻 28 天内合并），它描述的是条件出现在几段不同
   行情里，与样本独立性是两件事：连续覆盖 13 年的全样本只有 1 段，但含约
   26 个不重叠的 26 周观测。
2. **终点收益**而非窗口内最高价。最高价口径会系统性夸大结果（没人卖在最高点）。
3. **窗口未走完的时点直接排除**，不做任何插值——否则近期时点会因"还没跌完"
   而虚增胜率。
"""

from __future__ import annotations

import logging
from bisect import bisect_left
from datetime import date, timedelta
from statistics import median
from typing import Any, Callable, Optional

from processors.bottom_model.factors import (
    QUADRANTS,
    Rows,
    SeriesIndex,
    classify_quadrant,
)

logger = logging.getLogger(__name__)

# compute_core 由调用方注入：它住在 snapshot.py，而 snapshot 要引用本模块，
# 直接 import 会成环。回放只需要"截断数据 → 双层分数"这一个纯函数契约。
CoreFn = Callable[[dict[str, Rows], Optional[str]], dict[str, Any]]

# 回放起点：2013 年起 BTC 有连续价格与链上估值数据，早于此的样本构成过于单薄
REPLAY_START = "2013-01-01"
REPLAY_STEP_DAYS = 7
PRICE_METRIC = "btc_price_onchain"

FORWARD_WEEKS: tuple[int, ...] = (13, 26, 52)
# 胜率门槛：底部判断的实用含义是"能否吃到一段主升"，30% 是熊转牛的常见首程
HIT_THRESHOLD = 0.30
# 独立事件段合并间隔：一个月内的相邻时点视为同一段行情
SEGMENT_GAP_DAYS = 28
# 不重叠观测数下限：低于此值不输出百分比结论
MIN_INDEPENDENT = 5
# 回放点下限：整层弃权的门槛，低于此值连全样本基准都算不出来
MIN_REPLAY_POINTS = 100

# 读数邻域半径（Stress ± / Confirmation ±）
NEIGHBORHOOD_STRESS = 5.0
NEIGHBORHOOD_CONF = 10.0

STRESS_LADDER: tuple[float, ...] = (45.0, 55.0, 65.0, 75.0)
CONFIRMATION_LADDER: tuple[float, ...] = (35.0, 50.0, 65.0, 80.0)


def _to_date(day: str) -> Optional[date]:
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


# ══════════════════════ 回放 ══════════════════════

def replay_days(data: dict[str, Rows], start: str = REPLAY_START,
                step_days: int = REPLAY_STEP_DAYS) -> list[str]:
    """周级采样日历：start 起每 step_days 一点，到价格序列最后一天为止。"""
    price = data.get(PRICE_METRIC) or []
    if not price:
        return []
    begin, end = _to_date(start), _to_date(price[-1][0])
    if begin is None or end is None:
        return []
    days: list[str] = []
    cursor = begin
    while cursor <= end:
        days.append(cursor.isoformat())
        cursor += timedelta(days=step_days)
    return days


def _replay_point(index: SeriesIndex, day: str,
                  compute_core: CoreFn) -> dict[str, Any]:
    core = compute_core(index.truncate(day), day)
    stress = (core.get("stress") or {}).get("score")
    return {
        "day": day,
        "stress": stress,
        "confirmation": (core.get("confirmation") or {}).get("score"),
        "quadrant": (core.get("quadrant") or {}).get("key") or "",
    }


def load_or_build_replay(store: Any, data: dict[str, Rows],
                         algorithm_version: str, compute_core: CoreFn,
                         index: Optional[SeriesIndex] = None) -> list[dict[str, Any]]:
    """取回放表；版本不符则全量重算，版本相符只补新时点。

    算不出分数的时点（早年指标不足）也写入 stress=NULL，否则每次增量都会
    重试这些点。统计阶段按 stress is None 过滤。
    """
    # 显式传常量而非依赖默认参数：默认值在函数定义时就已冻结，测试无法覆盖
    targets = replay_days(data, REPLAY_START, REPLAY_STEP_DAYS)
    if not targets:
        return []
    cached = store.replay_rows(algorithm_version)
    known = {row["day"] for row in cached}
    missing = [day for day in targets if day not in known]
    if missing:
        idx = index or SeriesIndex(data)
        fresh: list[dict[str, Any]] = []
        for day in missing:
            try:
                fresh.append(_replay_point(idx, day, compute_core))
            except Exception:                       # noqa: BLE001
                logger.exception("bottom_model replay point failed: %s", day)
        if fresh:
            store.upsert_replay(algorithm_version, fresh)
            cached = store.replay_rows(algorithm_version)
        logger.info(
            "bottom_model replay %s: +%d points (total %d)",
            algorithm_version, len(fresh), len(cached),
        )
    # 回放表可能残留 targets 之外的旧时点（起点/步长调整过），按当前日历取用
    wanted = set(targets)
    return [row for row in cached if row["day"] in wanted]


# ══════════════════════ 前向收益 ══════════════════════

class _ForwardPrice:
    """按日索引的价格序列，给出"N 周后终点收益"。"""

    def __init__(self, price: Rows):
        self._days = [d for d, _ in price]
        self._vals = [v for _, v in price]

    def ret(self, day: str, weeks: int) -> Optional[float]:
        start = _to_date(day)
        if start is None or not self._days:
            return None
        i = bisect_left(self._days, day)
        if i >= len(self._days):
            return None
        target = (start + timedelta(weeks=weeks)).isoformat()
        j = bisect_left(self._days, target)
        if j >= len(self._days):        # 窗口尚未走完 → 排除，不做外推
            return None
        base = self._vals[i]
        if base <= 0:
            return None
        return (self._vals[j] - base) / base


# ══════════════════════ 条件统计 ══════════════════════

def count_segments(days: list[str], gap_days: int = SEGMENT_GAP_DAYS) -> int:
    """独立事件段数：相邻时点间隔超过 gap_days 才算新的一段。"""
    parsed = [d for d in (_to_date(x) for x in sorted(days)) if d is not None]
    if not parsed:
        return 0
    segments = 1
    prev = parsed[0]
    for cur in parsed[1:]:
        if (cur - prev).days > gap_days:
            segments += 1
        prev = cur
    return segments


def count_independent(days: list[str], weeks: int) -> int:
    """前向窗口互不重叠的最大观测数——本模块的有效样本量口径。

    贪心取最早可用点并跳过其后 weeks 周，即为最大不重叠子集的最优解。
    """
    parsed = sorted(d for d in (_to_date(x) for x in days) if d is not None)
    picked = 0
    cutoff: Optional[date] = None
    for day in parsed:
        if cutoff is None or day >= cutoff:
            picked += 1
            cutoff = day + timedelta(weeks=weeks)
    return picked


def _window_stats(rows: list[dict[str, Any]], weeks: int,
                  fwd: _ForwardPrice) -> dict[str, Any]:
    samples = [
        (row["day"], ret) for row in rows
        if (ret := fwd.ret(row["day"], weeks)) is not None
    ]
    if not samples:
        return {"weeks": weeks, "points": 0, "independent": 0, "segments": 0,
                "hit_rate": None, "median_return": None, "worst_return": None,
                "reliable": False}
    rets = [ret for _, ret in samples]
    days = [day for day, _ in samples]
    independent = count_independent(days, weeks)
    reliable = independent >= MIN_INDEPENDENT
    hits = sum(1 for ret in rets if ret >= HIT_THRESHOLD)
    return {
        "weeks": weeks,
        "points": len(samples),
        "independent": independent,
        "segments": count_segments(days),
        # 样本不足时不输出频率与中位数：n=2 的"100%"没有信息量，只有误导
        "hit_rate": round(100.0 * hits / len(samples), 1) if reliable else None,
        "median_return": round(100.0 * median(rets), 1) if reliable else None,
        # 最差一次是事实而非频率估计，样本再少也照实给出，作为风险线索
        "worst_return": round(100.0 * min(rets), 1),
        "reliable": reliable,
    }


def _condition(label: str, rows: list[dict[str, Any]], fwd: _ForwardPrice,
               description: str = "") -> dict[str, Any]:
    windows = [_window_stats(rows, weeks, fwd) for weeks in FORWARD_WEEKS]
    return {
        "label": label,
        "description": description,
        "points": len(rows),
        "windows": windows,
        "reliable": any(w["reliable"] for w in windows),
    }


def _ladder(rows: list[dict[str, Any]], field: str, thresholds: tuple[float, ...],
            fwd: _ForwardPrice) -> list[dict[str, Any]]:
    """累积门槛表：field >= 各阈值时的三窗口频率，用于检验单调性。"""
    out: list[dict[str, Any]] = []
    for threshold in thresholds:
        subset = [r for r in rows if (r.get(field) or 0.0) >= threshold]
        out.append({
            "threshold": threshold,
            "points": len(subset),
            "windows": [_window_stats(subset, weeks, fwd) for weeks in FORWARD_WEEKS],
        })
    return out


def _band(value: float, width: float = 10.0) -> tuple[float, float]:
    lo = (int(value) // int(width)) * float(width)
    return lo, lo + width


def compute_base_rate(store: Any, data: dict[str, Rows], current_core: dict[str, Any],
                      algorithm_version: str, compute_core: CoreFn,
                      index: Optional[SeriesIndex] = None) -> Optional[dict[str, Any]]:
    """当前读数的历史条件频率。数据不足时返回 None（快照字段留空）。"""
    price = data.get(PRICE_METRIC) or []
    if len(price) < 400:
        return None
    try:
        replay = load_or_build_replay(
            store, data, algorithm_version, compute_core, index,
        )
    except Exception:                               # noqa: BLE001
        logger.exception("bottom_model replay unavailable")
        return None
    scored = [r for r in replay if r.get("stress") is not None]
    if len(scored) < MIN_REPLAY_POINTS:
        return None

    fwd = _ForwardPrice(price)
    stress = (current_core.get("stress") or {}).get("score")
    conf = (current_core.get("confirmation") or {}).get("score")
    quadrant = (current_core.get("quadrant") or {}).get("key") or ""

    baseline = _condition("全样本基准", scored, fwd,
                          "回放期内所有周级时点，作为一切条件频率的对照")

    conditions: list[dict[str, Any]] = []
    if quadrant and quadrant in QUADRANTS:
        rows = [r for r in scored if r["quadrant"] == quadrant]
        conditions.append(_condition(
            f"当前象限 · {QUADRANTS[quadrant]}", rows, fwd,
            "历史上模型判定为同一象限的时点",
        ))
    if stress is not None:
        lo, hi = _band(stress)
        rows = [r for r in scored if lo <= r["stress"] < hi]
        conditions.append(_condition(
            f"压力档 {lo:.0f}-{hi:.0f}", rows, fwd,
            "仅按压力分档，不看确认层",
        ))
    if stress is not None and conf is not None:
        rows = [
            r for r in scored
            if abs(r["stress"] - stress) <= NEIGHBORHOOD_STRESS
            and r.get("confirmation") is not None
            and abs(r["confirmation"] - conf) <= NEIGHBORHOOD_CONF
        ]
        conditions.append(_condition(
            f"读数邻域 压力{stress:.0f}±{NEIGHBORHOOD_STRESS:.0f} "
            f"确认{conf:.0f}±{NEIGHBORHOOD_CONF:.0f}", rows, fwd,
            "与当前双层读数最接近的历史时点，通常样本极小",
        ))

    days = [r["day"] for r in scored]
    return {
        "algorithm_version": algorithm_version,
        "hit_threshold_pct": round(HIT_THRESHOLD * 100, 0),
        "forward_weeks": list(FORWARD_WEEKS),
        "min_independent": MIN_INDEPENDENT,
        "replay": {
            "points": len(scored),
            "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None,
            "step_days": REPLAY_STEP_DAYS,
        },
        "baseline": baseline,
        "conditions": conditions,
        "stress_ladder": _ladder(scored, "stress", STRESS_LADDER, fwd),
        "confirmation_ladder": _ladder(scored, "confirmation", CONFIRMATION_LADDER, fwd),
        "caveats": [
            f"胜率 = 该时点起 N 周后**终点**价格涨幅 ≥ {HIT_THRESHOLD:.0%} 的比例，"
            "非窗口内最高价；窗口未走完的时点已排除。",
            f"有效样本量看 independent（前向窗口互不重叠的观测数），不是 points；"
            f"independent < {MIN_INDEPENDENT} 的格子已不输出胜率与中位数，"
            "只保留最差一次作为风险线索。",
            f"segments 是独立事件段数（相邻 {SEGMENT_GAP_DAYS} 天内合并），描述该"
            "条件出现在几段不同行情里，与样本独立性是两件事：连续覆盖全期的"
            "全样本基准只有 1 段。",
            "即使 independent 达标，周级时点之间仍部分重叠，频率只是条件分布的"
            "观测值，不是独立同分布样本下的概率估计。",
            "2017 年前多数衍生品与需求类指标无数据，早年分数的因子构成与"
            "今天不同，跨期可比性有限。",
            f"回放按 {algorithm_version} 重算，算法版本变化会使全表重新计算，"
            "不同版本的频率不可直接比较。",
        ],
    }
