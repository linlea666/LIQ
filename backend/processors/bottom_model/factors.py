"""Bottom Model 因子引擎：标准化原语 + 六因子 + Confirmation + 假底过滤。

设计要点（对齐方案裁定）：
- 标准化 = 3y/5y/全历史三窗口滚动百分位混合（权重 30/30/40），窗口样本
  不足 90 天则跳过该窗口并重归一化——BGeometrics 4 年史自动退化为 3y/4y。
- 评分方向统一：**分数越高越接近"底部特征"**（0-100）。
- 双层分离：Stress（市场多惨，慢变量）与 Confirmation（是否开始改善，
  快变量）各自独立计算，绝不混合——这是假底过滤的结构性前提。
- 证据质量（EQ）：每个子信号附带 0-100 的证据强度，由序列自身的可观测量
  （历史跨度 / 可用分位窗口 / 新鲜度 / 代理关系）推导，作为**聚合权重乘子**
  而非分数修正——低置信只降低话语权，不把"高信号"翻译成"反向证据"。
- 铁律：任何单一信号只加分、不一票否决；缺失子信号只降低因子覆盖率，
  coverage < 0.4 的因子整体弃权并把权重重归一化给其余因子。
- 纯函数：输入截断后的日级序列 dict，无 IO、无状态，历史类比直接复用。
"""

from __future__ import annotations

import logging
from bisect import bisect_left, bisect_right
from datetime import date, datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)

Rows = list[tuple[str, float]]

# 三窗口混合百分位：(窗口天数, 权重)；None = 全历史
PERCENTILE_WINDOWS: tuple[tuple[Optional[int], float], ...] = (
    (1095, 0.30), (1825, 0.30), (None, 0.40),
)
_MIN_WINDOW_SAMPLES = 90

# 六因子权重（离线相关性审计后固定；总和 1.0）
FACTOR_WEIGHTS: dict[str, float] = {
    "valuation": 0.20,
    "capitulation": 0.20,
    "leverage": 0.15,
    "demand": 0.15,
    "structure": 0.20,
    "macro": 0.10,
}

FACTOR_LABELS: dict[str, str] = {
    "valuation": "估值",
    "capitulation": "投降",
    "leverage": "杠杆出清",
    # 显示名叫"流动性/需求"：稳定币增长只是场外弹药，不等于已进场买盘，
    # 单叫"需求"会让读者高估这个因子的含义。key 保持 demand 以兼容历史快照。
    "demand": "流动性/需求",
    "structure": "价格结构",
    "macro": "宏观",
}

# 因子有效覆盖率下限：低于此值该因子弃权（权重重归一化）
_MIN_FACTOR_COVERAGE = 0.4

# ── 证据质量（EQ）参数 ──
# 以 BTC 周期约 4 年为锚：覆盖两个完整周期才算窗口充分。清算/资金费
# （2023-11 起）自然落到 0.34 量级，NUPL/200W（2009-10 起）为 1.0。
_EQ_SPAN_FULL_YEARS = 8.0
_EQ_SPAN_FLOOR = 0.30
_EQ_FRESHNESS_FLOOR = 0.50

# 代理关系折扣：本模块唯一的判断性常数，仅用于指标与其声称含义存在
# 已知口径差的三处，每一处都注明差在哪里。
PROXY_CME_VOL = 0.80        # BTC=F 前月标准合约，不含 Micro/期权，非全 CME 成交
PROXY_EXCHANGE_BAL = 0.85   # 交易所覆盖名单随上游变化，绝对口径会漂移
PROXY_DERIVED_RATIO = 0.90  # 用 价格/成本线 派生替代上游直供比率指标


# ══════════════════════ 序列原语 ══════════════════════

def clamp(value: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, value))


def values_of(rows: Rows) -> list[float]:
    return [v for _, v in rows]


def last_value(rows: Rows) -> Optional[float]:
    return rows[-1][1] if rows else None


def tail_values(rows: Rows, n: int) -> list[float]:
    return [v for _, v in rows[-n:]] if rows else []


def sma_tail(rows: Rows, n: int) -> Optional[float]:
    vals = tail_values(rows, n)
    return sum(vals) / len(vals) if vals else None


def change_rate(rows: Rows, n: int) -> Optional[float]:
    """最近值相对 n 个自然日前最后可得值的变化率。"""
    if len(rows) < 2:
        return None
    try:
        target = (date.fromisoformat(rows[-1][0]) - timedelta(days=n)).isoformat()
    except (TypeError, ValueError):
        return None
    idx = bisect_right([day for day, _ in rows], target) - 1
    if idx < 0:
        return None
    base = rows[idx][1]
    if abs(base) < 1e-12:
        return None
    return (rows[-1][1] - base) / abs(base)


def change_rate_series(rows: Rows, n: int) -> Rows:
    """逐点 n 个自然日变化率序列（基准≈0 的点跳过）。

    与 change_rate 的单点版本同口径，供"当前变化率处于历史什么分位"这类
    子信号构造分布；相关性审计也复用它，避免用水平量算出伪相关
    （稳定币市值这类单调增长序列，水平值相关系数只反映时间趋势）。
    """
    days = [day for day, _ in rows]
    out: Rows = []
    for i, (day, value) in enumerate(rows):
        try:
            target = (date.fromisoformat(day) - timedelta(days=n)).isoformat()
        except (TypeError, ValueError):
            continue
        j = bisect_right(days, target, 0, i) - 1
        if j >= 0 and abs(rows[j][1]) > 1e-9:
            out.append((day, (value - rows[j][1]) / abs(rows[j][1])))
    return out


def rolling_mean_series(rows: Rows, window: int) -> Rows:
    """自然日滚动均值；稀疏序列不会再把 30 行伪装成 30 天。"""
    if len(rows) < 2:
        return []
    days = [day for day, _ in rows]
    out: Rows = []
    for i, (day, _) in enumerate(rows):
        cutoff = (date.fromisoformat(day) - timedelta(days=window - 1)).isoformat()
        left = bisect_left(days, cutoff, 0, i + 1)
        bucket = rows[left:i + 1]
        if bucket and (date.fromisoformat(day) - date.fromisoformat(bucket[0][0])).days \
                >= max(1, window - 2):
            out.append((day, sum(v for _, v in bucket) / len(bucket)))
    return out


def percentile_rank(values: list[float], value: float) -> Optional[float]:
    if not values:
        return None
    return 100.0 * sum(1 for v in values if v <= value) / len(values)


def window_coverage(rows: Rows, cadence: str = "daily") -> float:
    """三窗口中样本达标的权重之和（0.4-1.0），即 EQ 的样本乘子。

    只回答"能不能算出分布"，不回答"历史有多长"——后者由 EQ 的跨度乘子
    单独承担，两者分工明确，避免同一份短历史被惩罚两次。
    """
    if not rows:
        return 0.0
    scale = 7 if cadence == "weekly" else 1
    min_samples = max(10, _MIN_WINDOW_SAMPLES // scale)
    total = 0.0
    for days, weight in PERCENTILE_WINDOWS:
        window_rows = rows if days is None else rows[-max(1, days // scale):]
        if len(window_rows) >= min_samples:
            total += weight
    return round(total, 2)


def blended_percentile(rows: Rows, value: Optional[float] = None,
                       cadence: str = "daily") -> Optional[dict[str, Any]]:
    """三窗口混合百分位。返回 {pct, windows:[{days,n,pct}]}；数据不足返回 None。

    cadence="weekly" 时窗口天数与最小样本数按 7 天/行换算——序列按行存储，
    直接用天数切片会把周级窗口放大 7 倍。
    """
    if not rows:
        return None
    if value is None:
        value = rows[-1][1]
    scale = 7 if cadence == "weekly" else 1
    min_samples = max(10, _MIN_WINDOW_SAMPLES // scale)
    parts: list[tuple[float, float, dict[str, Any]]] = []
    for days, weight in PERCENTILE_WINDOWS:
        window_rows = rows if days is None else rows[-max(1, days // scale):]
        vals = values_of(window_rows)
        if len(vals) < min_samples:
            continue
        pct = percentile_rank(vals, value)
        parts.append((weight, pct, {
            "days": days, "n": len(vals), "pct": round(pct, 1),
        }))
    if not parts:
        return None
    total_w = sum(w for w, _, _ in parts)
    blended = sum(w * p for w, p, _ in parts) / total_w
    # coverage = 达标窗口的权重之和（三窗口权重合计 1.0），即 EQ 的样本乘子
    return {
        "pct": round(blended, 1),
        "coverage": round(total_w, 2),
        "windows": [meta for _, _, meta in parts],
    }


def day_delta(day_a: str, day_b: str) -> Optional[int]:
    """day_a - day_b 的自然日差；日期非法返回 None。"""
    try:
        return (datetime.strptime(day_a, "%Y-%m-%d")
                - datetime.strptime(day_b, "%Y-%m-%d")).days
    except (TypeError, ValueError):
        return None


def median_gap_days(rows: Rows, sample: int = 10) -> float:
    """由序列自身推断行间隔（日级=1，周级=7）——避免额外传 cadence。"""
    tail = rows[-(sample + 1):]
    gaps = [
        gap for i in range(1, len(tail))
        if (gap := day_delta(tail[i][0], tail[i - 1][0])) is not None and gap > 0
    ]
    if not gaps:
        return 1.0
    gaps.sort()
    return float(gaps[len(gaps) // 2])


def evidence_quality(rows: Rows, as_of: Optional[str] = None,
                     sample_factor: float = 1.0,
                     proxy: float = 1.0) -> tuple[Optional[float], str]:
    """子信号证据质量 0-100 = 跨度 × 可用窗口 × 新鲜度 × 代理折扣。

    四个乘子全部来自可观测量，不设人工分数表：
    - 跨度：历史年数 / 8（两个 BTC 周期）后夹逼到 [0.30, 1.0]
    - 可用窗口：三窗口混合百分位中达标窗口的权重和（非分位类由调用方给出）
    - 新鲜度：滞后超过 2 倍行间隔起线性衰减，下限 0.50
    - 代理折扣：仅三处已知口径差（见 PROXY_* 常数）
    """
    if not rows:
        return None, "无数据"
    span_days = day_delta(rows[-1][0], rows[0][0])
    span_years = (span_days or 0) / 365.25
    span_factor = clamp(span_years / _EQ_SPAN_FULL_YEARS, _EQ_SPAN_FLOOR, 1.0)
    sample_factor = clamp(sample_factor, 0.0, 1.0)
    gap = median_gap_days(rows)
    freshness = 1.0
    lag = day_delta(as_of, rows[-1][0]) if as_of else None
    if lag is not None and lag > 2 * gap:
        # 2×间隔内视为正常（周级数据天然滞后数天），之后线性衰减到下限
        excess = min(1.0, (lag - 2 * gap) / max(1.0, 4 * gap))
        freshness = clamp(1.0 - (1.0 - _EQ_FRESHNESS_FLOOR) * excess,
                          _EQ_FRESHNESS_FLOOR, 1.0)
    eq = 100.0 * span_factor * sample_factor * freshness * clamp(proxy, 0.0, 1.0)
    note = (f"跨度 {span_years:.1f}y→{span_factor:.2f} · 窗口 {sample_factor:.2f}"
            f" · 新鲜 {freshness:.2f}")
    if proxy < 1.0:
        note += f" · 代理 {proxy:.2f}"
    return round(clamp(eq), 1), note


class SeriesIndex:
    """预建日期索引的序列集合，支持 O(log n) 按日截断。

    历史类比只截断 4 次，全量重扫无所谓；历史频率层要截断上千次，
    逐点线性过滤会变成 O(时点数 × 指标数 × 天数)。两者共用此类。
    """

    def __init__(self, data: dict[str, Rows]):
        self._data = data
        self._days = {metric: [d for d, _ in rows] for metric, rows in data.items()}

    def truncate(self, day: str) -> dict[str, Rows]:
        """返回所有序列截断到 day（含）的浅切片；语义等价于逐点过滤。"""
        return {
            metric: rows[:bisect_right(self._days[metric], day)]
            for metric, rows in self._data.items()
        }


def align(rows_a: Rows, rows_b: Rows) -> list[tuple[str, float, float]]:
    """按日期对齐两序列（交集，升序）。"""
    map_b = dict(rows_b)
    return [(day, va, map_b[day]) for day, va in rows_a if day in map_b]


def ratio_series(rows_num: Rows, rows_den: Rows) -> Rows:
    """两序列按日对齐后的比值序列（分母≈0 跳过）。"""
    return [
        (day, num / den)
        for day, num, den in align(rows_num, rows_den)
        if abs(den) > 1e-12
    ]


def liq_total_series(d: dict[str, Rows]) -> Rows:
    """多空清算按日合并（杠杆因子与卖方衰竭共用）。"""
    return [
        (day, vl + vs)
        for day, vl, vs in align(d.get("liq_long_usd", []), d.get("liq_short_usd", []))
    ]


def drawdown_series(rows: Rows) -> Rows:
    """滚动历史最高点回撤序列（0 = 创新高，0.8 = 回撤 80%）。"""
    out: Rows = []
    peak = float("-inf")
    for day, value in rows:
        peak = max(peak, value)
        if peak > 1e-12:
            out.append((day, 1.0 - value / peak))
    return out


# ══════════════════════ 子信号构建 ══════════════════════

def _sub(key: str, label: str, score: Optional[float], weight: float,
         value: Optional[float] = None, percentile: Optional[float] = None,
         note: str = "", eq: Optional[float] = None,
         eq_note: str = "") -> dict[str, Any]:
    ok = score is not None
    return {
        "key": key, "label": label, "weight": weight, "ok": ok,
        "score": round(clamp(score), 1) if ok else None,
        "value": round(value, 6) if isinstance(value, (int, float)) else None,
        "percentile": round(percentile, 1) if isinstance(percentile, (int, float)) else None,
        "note": note,
        "evidence_quality": eq,
        "eq_note": eq_note,
    }


def _eq_of(sub: dict[str, Any]) -> float:
    """聚合用 EQ：未标注 EQ 的子信号按满分处理（向后兼容）。"""
    eq = sub.get("evidence_quality")
    return float(eq) if isinstance(eq, (int, float)) else 100.0


def _sample_factor(rows: Rows, cadence: str = "daily") -> float:
    """非分位类子信号的样本乘子：与分位类同口径（达标窗口权重和）。

    下限 0.30：这类子信号各有自己的最小样本门槛（如 ETF 动量要求 40 天），
    既然计算已经成立，就不该被样本乘子清零成"算了但完全没影响"的哑信号，
    只按"缺少完整窗口结构"打折。历史长度不足由跨度乘子单独承担。
    """
    return max(window_coverage(rows, cadence), 0.30)


def _pct_sub(key: str, label: str, rows: Rows, weight: float,
             invert: bool = True, note: str = "", cadence: str = "daily",
             as_of: Optional[str] = None, proxy: float = 1.0) -> dict[str, Any]:
    """按混合百分位打分的通用子信号；invert=True 表示低分位 = 高分（底部方向）。"""
    bp = blended_percentile(rows, cadence=cadence)
    if bp is None:
        return _sub(key, label, None, weight, note=note or "数据不足")
    score = 100.0 - bp["pct"] if invert else bp["pct"]
    eq, eq_note = evidence_quality(rows, as_of, bp["coverage"], proxy)
    return _sub(key, label, score, weight, value=last_value(rows),
                percentile=bp["pct"], note=note, eq=eq, eq_note=eq_note)


def _mean_pct_sub(key: str, label: str, rows: Rows, weight: float,
                  window: int = 30, invert: bool = False, note: str = "",
                  as_of: Optional[str] = None, proxy: float = 1.0) -> dict[str, Any]:
    """滚动均值的混合分位子信号：日级噪声大的流量类指标先平滑再定位分位。"""
    mean_rows = rolling_mean_series(rows, window)
    if not mean_rows:
        return _sub(key, label, None, weight, note=note or "数据不足")
    return _pct_sub(key, label, mean_rows, weight, invert=invert, note=note,
                    as_of=as_of, proxy=proxy)


def _factor(key: str, subs: list[dict[str, Any]]) -> dict[str, Any]:
    """子信号 → 因子：有效权重 = 名义权重 × EQ，分数语义不变。

    coverage 仍按名义权重计算（弃权规则不受 EQ 影响），因子 EQ = 有效权重
    与名义权重之比，即贡献信号的 EQ 加权均值。
    """
    total_w = sum(s["weight"] for s in subs)
    ok_subs = [s for s in subs if s["ok"]]
    ok_w = sum(s["weight"] for s in ok_subs)
    coverage = ok_w / total_w if total_w > 0 else 0.0
    eff_w = sum(s["weight"] * _eq_of(s) / 100.0 for s in ok_subs)
    score = (
        sum(s["weight"] * _eq_of(s) / 100.0 * s["score"] for s in ok_subs) / eff_w
        if eff_w > 1e-9 and coverage >= _MIN_FACTOR_COVERAGE else None
    )
    return {
        "key": key,
        "label": FACTOR_LABELS[key],
        "weight": FACTOR_WEIGHTS[key],
        "score": round(score, 1) if score is not None else None,
        "coverage": round(coverage, 2),
        "evidence_quality": round(100.0 * eff_w / ok_w, 1) if ok_w > 0 else None,
        "sub_signals": subs,
    }


# ══════════════════════ 六因子 ══════════════════════

def _factor_valuation(d: dict[str, Rows], as_of: Optional[str] = None) -> dict[str, Any]:
    subs = [
        _pct_sub("mvrv_z", "MVRV Z-Score", d.get("mvrv_zscore", []), 1.0,
                 note="BGeometrics，4 年窗口", as_of=as_of),
        _pct_sub("nupl", "NUPL", d.get("nupl", []), 1.0, note="2009 起全历史",
                 as_of=as_of),
        _pct_sub("reserve_risk", "Reserve Risk", d.get("reserve_risk", []), 1.0,
                 note="2010 起全历史", as_of=as_of),
        _pct_sub("puell", "Puell Multiple", d.get("puell_multiple", []), 1.0,
                 note="2010 起全历史；低分位 = 矿工收入极度压缩", as_of=as_of),
    ]
    # STH-MVRV：优先 BGeometrics；缺失则用 价格/STH Realized Price 派生
    sth_mvrv = d.get("sth_mvrv", [])
    derived = not sth_mvrv
    if derived:
        sth_mvrv = ratio_series(d.get("btc_price_onchain", []),
                                d.get("sth_realized_price", []))
    subs.append(_pct_sub(
        "sth_mvrv", "STH-MVRV", sth_mvrv, 1.0, as_of=as_of,
        note="价格/STH 成本派生（上游缺失时的退化路径）" if derived else "",
        proxy=PROXY_DERIVED_RATIO if derived else 1.0,
    ))
    # 价格 / 200 周均线 偏离（2010 起全历史）
    ratio_200w = ratio_series(d.get("btc_price_onchain", []), d.get("ma_200w", []))
    subs.append(_pct_sub("price_vs_200w", "价格/200W均线", ratio_200w, 1.0,
                         note="<1 = 跌破 200 周均线", as_of=as_of))
    return _factor("valuation", subs)


def _factor_capitulation(d: dict[str, Rows], as_of: Optional[str] = None) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    # 已实现亏损：极值（投降已发生）+ 衰减（卖方衰竭）
    loss_abs: Rows = [(day, abs(v)) for day, v in d.get("realized_loss", [])]
    if len(loss_abs) >= 120:
        peak_90d = max(tail_values(loss_abs, 90))
        bp = blended_percentile(loss_abs, peak_90d)
        eq, eq_note = evidence_quality(
            loss_abs, as_of, bp["coverage"] if bp else _sample_factor(loss_abs),
        )
        subs.append(_sub(
            "loss_extreme", "已实现亏损·90d 峰值", bp["pct"] if bp else None, 1.0,
            value=peak_90d, percentile=bp["pct"] if bp else None,
            note="峰值分位越高 = 投降越充分", eq=eq, eq_note=eq_note,
        ))
        avg_14d = sma_tail(loss_abs, 14)
        if peak_90d > 1e-9 and avg_14d is not None:
            decay_ratio = avg_14d / peak_90d
            decay_eq, decay_eq_note = evidence_quality(
                loss_abs, as_of, _sample_factor(loss_abs),
            )
            subs.append(_sub(
                "loss_decay", "亏损衰减（卖方衰竭）",
                100.0 * (1.0 - min(1.0, decay_ratio)), 1.0,
                value=decay_ratio, note="14d 均值 / 90d 峰值，越低越衰竭",
                eq=decay_eq, eq_note=decay_eq_note,
            ))
        else:
            subs.append(_sub("loss_decay", "亏损衰减（卖方衰竭）", None, 1.0))
    else:
        subs.append(_sub("loss_extreme", "已实现亏损·90d 峰值", None, 1.0, note="数据不足"))
        subs.append(_sub("loss_decay", "亏损衰减（卖方衰竭）", None, 1.0, note="数据不足"))
    # aSOPR 结构：<1 深度（投降）+ 收复迹象
    sopr = d.get("sopr", [])
    if len(sopr) >= 90:
        below_share = sum(1 for v in tail_values(sopr, 90) if v < 1.0) / 90.0
        avg_14 = sma_tail(sopr, 14) or 0.0
        reclaim = 100.0 if avg_14 >= 1.0 else 50.0 if avg_14 >= 0.995 else 0.0
        score = 60.0 * below_share + 0.4 * reclaim
        eq, eq_note = evidence_quality(sopr, as_of, _sample_factor(sopr))
        subs.append(_sub(
            "sopr_structure", "aSOPR 跌破1→收复", score, 1.0, value=avg_14,
            note=f"90d 内 <1 占比 {below_share:.0%}，14d 均 {avg_14:.4f}",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("sopr_structure", "aSOPR 跌破1→收复", None, 1.0, note="数据不足"))
    subs.append(_pct_sub("sth_sopr", "STH-SOPR", d.get("sth_sopr", []), 0.5,
                         note="低分位 = 短期持有者深度割肉（2010 起全历史）",
                         as_of=as_of))
    subs.append(_pct_sub("lth_sopr", "LTH-SOPR", d.get("lth_sopr", []), 0.5,
                         note="低分位 = 长期持有者亏损兑现", as_of=as_of))
    # LTH 已实现亏损极值
    lth_loss_abs: Rows = [(day, abs(v)) for day, v in d.get("lth_realized_loss", [])]
    if len(lth_loss_abs) >= 120:
        peak = max(tail_values(lth_loss_abs, 90))
        bp = blended_percentile(lth_loss_abs, peak)
        eq, eq_note = evidence_quality(
            lth_loss_abs, as_of,
            bp["coverage"] if bp else _sample_factor(lth_loss_abs),
        )
        subs.append(_sub(
            "lth_loss_extreme", "LTH 亏损·90d 峰值", bp["pct"] if bp else None, 0.5,
            value=peak, percentile=bp["pct"] if bp else None,
            note="LTH 投降是历史大底标志", eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("lth_loss_extreme", "LTH 亏损·90d 峰值", None, 0.5, note="数据不足"))
    # STH 供应 90d 变化：恐慌换手后筹码转移给 LTH → 变化率低分位加分
    sth_chg = change_rate(d.get("sth_supply", []), 90)
    sth_supply = d.get("sth_supply", [])
    if sth_chg is not None and len(sth_supply) > 180:
        chg_series = change_rate_series(sth_supply, 90)
        bp = blended_percentile(chg_series, sth_chg)
        eq, eq_note = evidence_quality(
            chg_series, as_of,
            bp["coverage"] if bp else _sample_factor(chg_series),
        )
        subs.append(_sub(
            "sth_supply_drop", "STH 供应 90d 变化", 100.0 - bp["pct"] if bp else None,
            0.5, value=sth_chg, percentile=bp["pct"] if bp else None,
            note="下降 = 筹码从短期恐慌盘转移至长期持有者（与 LTH 净增持互为镜像）",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("sth_supply_drop", "STH 供应 90d 变化", None, 0.5, note="数据不足"))
    return _factor("capitulation", subs)


def oi_flush_ratio(rows: Rows, peak_window: int = 180) -> Optional[float]:
    """OI 距滚动峰值的回撤比例（0.26 = 已从峰值回落 26%）；数据不足返回 None。"""
    if len(rows) < 60:
        return None
    peak = max(tail_values(rows, peak_window))
    if peak <= 1e-9:
        return None
    return 1.0 - rows[-1][1] / peak


def _oi_flush_sub(key: str, label: str, rows: Rows, weight: float, note: str = "",
                  as_of: Optional[str] = None) -> dict[str, Any]:
    """OI 从 180d 峰值的回撤幅度 → 线性映射（回撤 ≥30% 满分）。"""
    if len(rows) < 60:
        return _sub(key, label, None, weight, note=note or "数据不足")
    flush = oi_flush_ratio(rows)
    if flush is None:
        return _sub(key, label, None, weight, note="峰值异常")
    score = clamp(flush / 0.30 * 100.0)
    eq, eq_note = evidence_quality(rows, as_of, _sample_factor(rows))
    return _sub(key, label, score, weight, value=flush,
                note=note or f"距 180d 峰值回撤 {flush:.1%}", eq=eq, eq_note=eq_note)


def _factor_leverage(d: dict[str, Rows], as_of: Optional[str] = None) -> dict[str, Any]:
    subs = [
        _oi_flush_sub("oi_flush", "聚合 OI 出清", d.get("oi_agg_usd", []), 1.0,
                      as_of=as_of),
        _oi_flush_sub("cme_oi_flush", "CME OI 出清（机构）", d.get("cme_oi_usd", []), 1.0,
                      note="机构持仓出清比成交量更硬", as_of=as_of),
    ]
    # 资金费率极端：30d 均值低分位 = 空头拥挤/杠杆清洗
    funding = d.get("funding_oiw", [])
    if len(funding) >= 120:
        avg_30 = sma_tail(funding, 30)
        avg_series: Rows = [
            (funding[i][0], sum(v for _, v in funding[i - 29:i + 1]) / 30.0)
            for i in range(29, len(funding))
        ]
        bp = blended_percentile(avg_series, avg_30)
        eq, eq_note = evidence_quality(
            funding, as_of, bp["coverage"] if bp else _sample_factor(funding),
        )
        subs.append(_sub(
            "funding_reset", "OI 加权资金费·30d (%/8h)",
            100.0 - bp["pct"] if bp else None,
            1.0, value=avg_30, percentile=bp["pct"] if bp else None,
            note="窗口仅 2023-11 起，分位解读需谨慎", eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("funding_reset", "OI 加权资金费·30d (%/8h)", None, 1.0,
                         note="数据不足"))
    # 清算出清：多空合计 90d 峰值分位（窗口仅 2023-11 起）
    liq_total = liq_total_series(d)
    if len(liq_total) >= 120:
        peak = max(tail_values(liq_total, 90))
        bp = blended_percentile(liq_total, peak)
        eq, eq_note = evidence_quality(
            liq_total, as_of, bp["coverage"] if bp else _sample_factor(liq_total),
        )
        subs.append(_sub(
            "liq_flush", "清算量·90d 峰值", bp["pct"] if bp else None, 1.0,
            value=peak, percentile=bp["pct"] if bp else None,
            note="窗口仅 2023-11 起；极端清算 = 强制出清已发生",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("liq_flush", "清算量·90d 峰值", None, 1.0, note="数据不足"))
    # CME 恐慌周量：近 8 周峰值的全历史分位（加分项，权重减半）
    cme_vol = d.get("cme_vol_1w", [])
    if len(cme_vol) >= 100:
        peak_8w = max(tail_values(cme_vol, 8))
        pct = percentile_rank(values_of(cme_vol), peak_8w)
        eq, eq_note = evidence_quality(
            cme_vol, as_of, _sample_factor(cme_vol, "weekly"), PROXY_CME_VOL,
        )
        subs.append(_sub(
            "cme_panic_vol", "CME 恐慌周量", pct, 0.5,
            value=peak_8w, percentile=pct,
            note="BTC=F 前月合约代理指标（2017-12 起）；加分项不作否决",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("cme_panic_vol", "CME 恐慌周量", None, 0.5, note="数据不足"))
    return _factor("leverage", subs)


def _factor_demand(d: dict[str, Rows], as_of: Optional[str] = None) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    # 现货买盘直接证据放最前：2017-08 起覆盖 2018 与 2022 两个大底，是本因子
    # 唯一不靠代理关系的需求证据（ETF 窗口仅 2024 起，稳定币只是场外弹药）。
    # 实测两者相关系数仅 0.04，各自独立计权。
    subs.append(_mean_pct_sub(
        "coinbase_premium", "Coinbase 溢价·30d 均",
        d.get("coinbase_premium_rate", []), 1.0, as_of=as_of,
        note="高分位 = 美国现货资金净买入；负值 = 美国资金在净卖出",
    ))
    subs.append(_mean_pct_sub(
        "spot_net_taker", "现货净 taker·30d 均",
        d.get("spot_net_taker_usd", []), 1.0, as_of=as_of,
        note="高分位 = 主动买单相对主动卖单占优（现货成交，不含衍生品）",
    ))
    # ETF 动量：30d 累计 + 7d/30d 反转结构
    etf = d.get("etf_flow_usd", [])
    if len(etf) >= 40:
        sum_30 = sum(tail_values(etf, 30))
        avg_7, avg_30 = sma_tail(etf, 7) or 0.0, sma_tail(etf, 30) or 0.0
        if avg_30 < 0 and avg_7 > 0:
            score = 90.0    # 净流出后 7d 转正 = 需求反转（最强底部形态）
        elif avg_7 > 0 and avg_30 > 0:
            score = 65.0    # 持续流入
        elif avg_7 > avg_30:
            score = 45.0    # 仍流出但边际改善
        else:
            score = 10.0    # 持续恶化
        eq, eq_note = evidence_quality(etf, as_of, _sample_factor(etf))
        subs.append(_sub(
            "etf_momentum", "ETF 流动量·7d/30d", score, 1.0, value=sum_30,
            note=f"7d 均 {avg_7 / 1e6:+.0f}M，30d 均 {avg_30 / 1e6:+.0f}M（2024-01 起）",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("etf_momentum", "ETF 流动量·7d/30d", None, 1.0, note="数据不足"))
    # 稳定币市值 30d 增速分位：高 = 场外弹药积累（流动性代理，非直接需求）
    sc = d.get("stablecoin_total_mcap", [])
    sc_chg = change_rate(sc, 30)
    if sc_chg is not None and len(sc) > 180:
        chg_series = change_rate_series(sc, 30)
        bp = blended_percentile(chg_series, sc_chg)
        eq, eq_note = evidence_quality(
            chg_series, as_of,
            bp["coverage"] if bp else _sample_factor(chg_series),
        )
        subs.append(_sub(
            "stablecoin_growth", "稳定币市值·30d 增速", bp["pct"] if bp else None,
            1.0, value=sc_chg, percentile=bp["pct"] if bp else None,
            note="增速高分位 = 场外资金积累（流动性代理，不等于现货买盘）",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("stablecoin_growth", "稳定币市值·30d 增速", None, 1.0, note="数据不足"))
    # 交易所余额 30d 变化：流出（负）加分
    balance = d.get("exchange_balance_btc", [])
    bal_chg = change_rate(balance, 30)
    if bal_chg is not None:
        score = clamp(50.0 - bal_chg / 0.05 * 50.0)   # -5% 流出满分，+5% 流入 0 分
        eq, eq_note = evidence_quality(
            balance, as_of, _sample_factor(balance), PROXY_EXCHANGE_BAL,
        )
        subs.append(_sub(
            "exchange_outflow", "交易所余额·30d 变化", score, 1.0, value=bal_chg,
            note="窗口仅 2024-08 起；流出 = 持币意愿",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("exchange_outflow", "交易所余额·30d 变化", None, 1.0, note="数据不足"))
    return _factor("demand", subs)


def weekly_higher_low(low_rows: Rows, lookback_weeks: int = 26) -> Optional[bool]:
    """周线 LL→HL 检测：最低周之后是否出现连续 2 周低点抬高。"""
    lows = tail_values(low_rows, lookback_weeks)
    if len(lows) < 8:
        return None
    trough_idx = lows.index(min(lows))
    after = lows[trough_idx + 1:]
    if len(after) < 2:
        return False
    return all(after[i] > lows[trough_idx] for i in range(len(after))) and \
        after[-1] > after[0]


def weekly_higher_high(low_rows: Rows, high_rows: Rows,
                       lookback_weeks: int = 26) -> Optional[bool]:
    """周线 HH 检测：最低周之后，后半段高点是否突破前半段反弹高点。"""
    lows = tail_values(low_rows, lookback_weeks)
    highs = tail_values(high_rows, lookback_weeks)
    n = min(len(lows), len(highs))
    if n < 8:
        return None
    lows, highs = lows[-n:], highs[-n:]
    trough_idx = lows.index(min(lows))
    after = highs[trough_idx + 1:]
    if len(after) < 4:
        return False
    half = max(1, len(after) // 2)
    return max(after[half:]) > max(after[:half])


def weekly_structure_stage(d: dict[str, Rows],
                           lookback_weeks: int = 26) -> Optional[dict[str, Any]]:
    """价格结构分阶段确认 0-4：HL 只是第一步，收复成本线与 HH 才是后续。

    阶段划分（分数为"底部确认"方向）：
    0 仍在创新低 10 ｜ 1 停止创新低但未形成 HL 35 ｜ 2 形成 HL 55
    3 HL + 收复 STH 成本线或 200W 均线 75 ｜ 4 再叠加 HH 100
    """
    lows, highs = d.get("btc_low_1w", []), d.get("btc_high_1w", [])
    tail = tail_values(lows, lookback_weeks)
    if len(tail) < 8:
        return None
    hl = weekly_higher_low(lows, lookback_weeks)
    if hl is None:
        return None
    making_new_low = tail[-1] <= min(tail) * 1.001
    hh = bool(weekly_higher_high(lows, highs, lookback_weeks))
    price = last_value(d.get("btc_price_onchain", [])) or last_value(d.get("btc_close_1w", []))
    sth_rp = last_value(d.get("sth_realized_price", []))
    ma_200w = last_value(d.get("ma_200w", []))
    reclaimed_names = [
        name for name, level in (("STH 成本", sth_rp), ("200W 均线", ma_200w))
        if price is not None and level is not None and level > 1e-9 and price >= level
    ]
    reclaimed = bool(reclaimed_names)
    if making_new_low:
        stage, score, tag = 0, 10.0, "周线仍在创新低，下跌结构未破坏"
    elif not hl:
        stage, score, tag = 1, 35.0, "已停止创新低，但尚未形成抬高低点"
    elif not reclaimed:
        stage, score, tag = 2, 55.0, "已形成抬高低点，但仍未收复 STH 成本线/200W 均线"
    elif not hh:
        stage, score, tag = 3, 75.0, f"抬高低点 + 收复{'、'.join(reclaimed_names)}，尚无更高高点"
    else:
        stage, score, tag = 4, 100.0, f"抬高低点 + 更高高点 + 站上{'、'.join(reclaimed_names)}"
    return {"stage": stage, "score": score, "note": f"阶段 {stage}/4：{tag}"}


def _factor_structure(d: dict[str, Rows], as_of: Optional[str] = None) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    price = d.get("btc_price_onchain", [])
    # 回撤深度：当前回撤在全历史回撤分布中的分位
    dd = drawdown_series(price)
    subs.append(_pct_sub("drawdown", "回撤深度", dd, 1.0, invert=False,
                         note="当前回撤在 2010 起回撤分布中的分位", as_of=as_of))
    # 注：周线 LL→HL 已迁至 Confirmation 层的 structure_stage。它是"是否开始
    # 转向"的事件信号，留在 Stress 因子会同时抬高两个仪表（跨层重复计分），
    # 也违反本模块 Stress/Confirmation 严格分离的设计前提。
    # 收复 STH Realized Price（这里看"低于成本线多少"= 压力水平，
    # 与 Confirmation 层的"是否收复"= 事件判定，是有意的水平/事件二分）
    sth_rp_rows = d.get("sth_realized_price", [])
    pair = align(price, sth_rp_rows)
    if pair:
        _, close, sth_rp = pair[-1]
        if sth_rp > 1e-9:
            ratio = close / sth_rp
            score = clamp((ratio - 0.80) / 0.25 * 100.0)   # 0.80→0 分，1.05→满分
            eq, eq_note = evidence_quality(
                sth_rp_rows, as_of, _sample_factor(sth_rp_rows),
            )
            subs.append(_sub(
                "reclaim_sth_rp", "价格 vs STH 成本", score, 1.0, value=ratio,
                note=f"价格/STH-RP = {ratio:.3f}（>1 = 短期持有者回到盈利）",
                eq=eq, eq_note=eq_note,
            ))
        else:
            subs.append(_sub("reclaim_sth_rp", "价格 vs STH 成本", None, 1.0))
    else:
        subs.append(_sub("reclaim_sth_rp", "价格 vs STH 成本", None, 1.0, note="数据不足"))
    # 新低卖压背离：近 14d 创 90d 新低，但已实现亏损未随之创峰
    loss_abs: Rows = [(day, abs(v)) for day, v in d.get("realized_loss", [])]
    if len(price) >= 90 and len(loss_abs) >= 90:
        low_90 = min(tail_values(price, 90))
        recent_low = min(tail_values(price, 14))
        made_new_low = recent_low <= low_90 * 1.001
        eq, eq_note = evidence_quality(loss_abs, as_of, _sample_factor(loss_abs))
        if made_new_low:
            peak_90 = max(tail_values(loss_abs, 90))
            recent_loss = max(tail_values(loss_abs, 14))
            diverged = peak_90 > 1e-9 and recent_loss < 0.5 * peak_90
            subs.append(_sub(
                "bottom_divergence", "新低卖压背离", 100.0 if diverged else 30.0, 0.5,
                note="创新低但亏损兑现显著低于前峰 = 卖压衰竭背离" if diverged
                else "创新低且卖压未衰竭", eq=eq, eq_note=eq_note,
            ))
        else:
            subs.append(_sub(
                "bottom_divergence", "新低卖压背离", 60.0, 0.5,
                note="近 14d 未创 90d 新低", eq=eq, eq_note=eq_note,
            ))
    else:
        subs.append(_sub("bottom_divergence", "新低卖压背离", None, 0.5, note="数据不足"))
    return _factor("structure", subs)


def _factor_macro(d: dict[str, Rows], as_of: Optional[str] = None) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    # 全球 M2 同比：回升趋势 + 低位分位（宽松空间）
    m2 = d.get("global_m2_yoy", [])
    if len(m2) >= 30:
        bp = blended_percentile(m2, cadence="weekly")
        rising = len(m2) >= 4 and m2[-1][1] > m2[-4][1]
        level_score = 100.0 - bp["pct"] if bp else 50.0
        score = 0.5 * level_score + 0.5 * (100.0 if rising else 0.0)
        eq, eq_note = evidence_quality(
            m2, as_of, bp["coverage"] if bp else _sample_factor(m2, "weekly"),
        )
        subs.append(_sub(
            "m2_trend", "全球 M2 同比", score, 1.0, value=last_value(m2),
            percentile=bp["pct"] if bp else None,
            note=f"{'回升中' if rising else '未回升'}（上游滞后约 1 个月）",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("m2_trend", "全球 M2 同比", None, 1.0, note="数据不足"))
    # 恐惧贪婪：当前极端 + 持续时长
    fg = d.get("fear_greed", [])
    if len(fg) >= 60:
        current = fg[-1][1]
        extreme_share = sum(1 for v in tail_values(fg, 30) if v < 25) / 30.0
        score = 0.6 * (100.0 - current) + 0.4 * (extreme_share * 100.0)
        eq, eq_note = evidence_quality(fg, as_of, _sample_factor(fg))
        subs.append(_sub(
            "fear_extreme", "恐惧贪婪·极端持续", score, 1.0, value=current,
            note=f"当前 {current:.0f}，近 30d 极端恐惧占比 {extreme_share:.0%}",
            eq=eq, eq_note=eq_note,
        ))
    else:
        subs.append(_sub("fear_extreme", "恐惧贪婪·极端持续", None, 1.0, note="数据不足"))
    return _factor("macro", subs)


# ══════════════════════ Stress 汇总 ══════════════════════

def compute_factors(d: dict[str, Rows],
                    as_of: Optional[str] = None) -> list[dict[str, Any]]:
    """六因子。as_of 只用于 EQ 的新鲜度乘子，None = 不做新鲜度折扣。"""
    return [
        _factor_valuation(d, as_of),
        _factor_capitulation(d, as_of),
        _factor_leverage(d, as_of),
        _factor_demand(d, as_of),
        _factor_structure(d, as_of),
        _factor_macro(d, as_of),
    ]


def compute_stress(factors: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Bottom Stress 0-100：有效因子加权，弃权因子的权重重归一化。

    因子权重维持离线审计后的固定值（EQ 已在子信号层作用过一次，
    此处再乘一次会造成二次收缩），只汇总一个证据质量供解读。
    """
    active = [f for f in factors if f["score"] is not None]
    total_w = sum(f["weight"] for f in active)
    if total_w < 0.5:    # 有效权重不足一半，整体拒绝给分
        return None
    stress = sum(f["weight"] * f["score"] for f in active) / total_w
    eq_parts = [(f["weight"], f["evidence_quality"]) for f in active
                if f.get("evidence_quality") is not None]
    eq_w = sum(w for w, _ in eq_parts)
    return {
        "score": round(stress, 1),
        "active_weight": round(total_w, 2),
        "evidence_quality": (
            round(sum(w * eq for w, eq in eq_parts) / eq_w, 1) if eq_w > 0 else None
        ),
        "abstained": [f["key"] for f in factors if f["score"] is None],
    }


# ══════════════════════ Confirmation 层 ══════════════════════

# 资金费"负极端"阈值。funding_oiw 上游单位是**百分比/8h**（中位 0.0063%，
# 永续中性基准 0.01%），不是小数比率——所以判定与展示都必须按百分比处理。
# 取 -0.005%：2.7 年历史中仅 2.7% 的交易日低于此值，是真正的空头拥挤区；
# 而"低于 0"占 10.8% 的交易日，不足以称为极端。
_FUNDING_NEG_EXTREME = -0.005


def compute_confirmation(d: dict[str, Rows],
                         as_of: Optional[str] = None) -> dict[str, Any]:
    """底部确认分 0-100：改善迹象（快变量），与 Stress 严格分离。

    各 check 按 EQ 加权（短窗口证据如 ETF/资金费自动降权），而非等权平均。
    """
    checks: list[dict[str, Any]] = []

    def _check(key: str, label: str, score: Optional[float], note: str = "",
               eq: Optional[float] = None, eq_note: str = "",
               weight: float = 1.0) -> None:
        checks.append({
            "key": key, "label": label, "ok": score is not None,
            "score": round(clamp(score), 1) if score is not None else None,
            "note": note, "weight": weight,
            "evidence_quality": eq, "eq_note": eq_note,
        })

    # 1. aSOPR 收复 1
    sopr = d.get("sopr", [])
    if len(sopr) >= 90:
        avg_14 = sma_tail(sopr, 14) or 0.0
        was_below = min(tail_values(sopr, 90)) < 0.98
        eq, eq_note = evidence_quality(sopr, as_of, _sample_factor(sopr))
        if avg_14 >= 1.0 and was_below:
            _check("sopr_reclaim", "aSOPR 收复 1", 100.0, f"14d 均 {avg_14:.4f}",
                   eq, eq_note)
        elif avg_14 >= 0.995:
            _check("sopr_reclaim", "aSOPR 收复 1", 50.0,
                   f"14d 均 {avg_14:.4f}，接近收复", eq, eq_note)
        else:
            _check("sopr_reclaim", "aSOPR 收复 1", 0.0,
                   f"14d 均 {avg_14:.4f}，仍亏损兑现", eq, eq_note)
    else:
        _check("sopr_reclaim", "aSOPR 收复 1", None, "数据不足")

    # 2. 价格收复 STH Realized Price
    sth_rp_rows = d.get("sth_realized_price", [])
    pair = align(d.get("btc_price_onchain", []), sth_rp_rows)
    if pair:
        _, close, sth_rp = pair[-1]
        ratio = close / sth_rp if sth_rp > 1e-9 else 0.0
        eq, eq_note = evidence_quality(sth_rp_rows, as_of, _sample_factor(sth_rp_rows))
        _check("price_reclaim_sth", "收复 STH 成本线",
               100.0 if ratio >= 1.0 else 50.0 if ratio >= 0.97 else 0.0,
               f"价格/STH-RP = {ratio:.3f}", eq, eq_note)
    else:
        _check("price_reclaim_sth", "收复 STH 成本线", None, "数据不足")

    # 3. ETF 流转正
    etf = d.get("etf_flow_usd", [])
    if len(etf) >= 40:
        avg_7, avg_30 = sma_tail(etf, 7) or 0.0, sma_tail(etf, 30) or 0.0
        eq, eq_note = evidence_quality(etf, as_of, _sample_factor(etf))
        if avg_7 > 0 and avg_30 < 0:
            _check("etf_turn", "ETF 流反转", 100.0, "30d 净流出后 7d 转正", eq, eq_note)
        elif avg_7 > 0:
            _check("etf_turn", "ETF 流反转", 70.0, "7d/30d 均为净流入", eq, eq_note)
        else:
            _check("etf_turn", "ETF 流反转", 0.0, "7d 仍净流出", eq, eq_note)
    else:
        _check("etf_turn", "ETF 流反转", None, "数据不足")

    # 4. 资金费 × OI 制度：资金费单看会误判——从极负回正既可能是恐慌结束，
    #    也可能是多头杠杆重新堆积（底部反弹后再杀一轮的典型形态）
    funding, oi = d.get("funding_oiw", []), d.get("oi_agg_usd", [])
    if len(funding) >= 90:
        avg_14 = sma_tail(funding, 14) or 0.0
        low_90 = min(tail_values(funding, 90))
        oi_chg = change_rate(oi, 30)
        recovering = low_90 <= _FUNDING_NEG_EXTREME and avg_14 >= 0
        negative = avg_14 < 0
        f_note = f"资金费 90d 低点 {low_90:.4f}% → 14d 均 {avg_14:.4f}%/8h"
        f_eq, f_eq_note = evidence_quality(funding, as_of, _sample_factor(funding))
        if oi_chg is None:
            score = 70.0 if recovering else 40.0 if negative else 60.0
            note = f"{f_note}；OI 数据不足，无法判定杠杆制度"
            eq, eq_note = f_eq, f_eq_note
        else:
            if recovering and oi_chg > 0.15:
                score, tag = 25.0, "资金费回升但 OI 快速回堆 = 杠杆重新堆积"
            elif recovering:
                score, tag = 100.0, "资金费正常化且 OI 未回堆 = 健康去杠杆后修复"
            elif negative and oi_chg < 0:
                score, tag = 40.0, "资金费为负 + OI 下降 = 恐慌出清进行中（压力事件，非确认）"
            elif negative:
                score, tag = 35.0, "资金费为负 + OI 上升 = 空头持续累积"
            else:
                score, tag = 70.0, "资金费未见负极端、OI 平稳，无正常化事件可确认"
            flush = oi_flush_ratio(oi)
            flush_desc = f"，距 180d 峰值 {flush:.1%}" if flush is not None else ""
            note = f"{f_note}；OI 30d {oi_chg:+.1%}{flush_desc}——{tag}"
            oi_eq, _ = evidence_quality(oi, as_of, _sample_factor(oi))
            # 二维判定的置信度取决于较弱的一侧
            eq = min(f_eq, oi_eq) if f_eq is not None and oi_eq is not None else f_eq
            eq_note = f_eq_note
        _check("funding_oi_regime", "资金费 × OI 制度", score, note, eq, eq_note)
    else:
        _check("funding_oi_regime", "资金费 × OI 制度", None, "数据不足")

    # 5. 周线结构分阶段（HL 只是第一步，收复成本线与 HH 才是后续）
    stage = weekly_structure_stage(d)
    if stage is None:
        _check("structure_stage", "周线结构阶段", None, "数据不足")
    else:
        eq, eq_note = evidence_quality(
            d.get("btc_low_1w", []), as_of,
            _sample_factor(d.get("btc_low_1w", []), "weekly"),
        )
        _check("structure_stage", "周线结构阶段", stage["score"], stage["note"],
               eq, eq_note)

    ok_checks = [c for c in checks if c["ok"]]
    eff_w = sum(c["weight"] * _eq_of(c) / 100.0 for c in ok_checks)
    base = (
        sum(c["weight"] * _eq_of(c) / 100.0 * c["score"] for c in ok_checks) / eff_w
        if eff_w > 1e-9 else None
    )
    nominal_w = sum(c["weight"] for c in ok_checks)
    total_w = sum(c["weight"] for c in checks)
    coverage = nominal_w / total_w if total_w > 0 else 0.0
    # Confirmation 的阈值只有在至少 60% 名义证据存在时才有同一语义；
    # 否则即使剩余项目分数很高也必须弃权，禁止缺失项被静默重归一化。
    if coverage < 0.60:
        base = None
    return {
        "score": round(base, 1) if base is not None else None,
        "evidence_quality": round(100.0 * eff_w / nominal_w, 1) if nominal_w > 0 else None,
        "active_weight": round(nominal_w, 2),
        "coverage": round(coverage, 2),
        "abstained": [c["key"] for c in checks if not c["ok"]],
        "checks": checks,
    }


# ══════════════════════ 假底过滤器 ══════════════════════

def compute_fake_bottom_filter(d: dict[str, Rows]) -> dict[str, Any]:
    """假底触发器：每项触发对 Confirmation 施加惩罚（只降级、不否决 Stress）。"""
    triggers: list[dict[str, Any]] = []

    oi_chg = change_rate(d.get("oi_agg_usd", []), 30)
    price_rows = d.get("btc_close_1d", []) or d.get("btc_price_onchain", [])
    price_chg = change_rate(price_rows, 30)
    if oi_chg is not None and price_chg is not None and oi_chg > 0.15 and price_chg < 0.05:
        triggers.append({
            "key": "oi_rebuild", "label": "OI 快速回堆", "penalty": 15,
            "note": f"30d OI {oi_chg:+.1%} 而价格仅 {price_chg:+.1%}，杠杆抢跑",
        })

    funding = d.get("funding_oiw", [])
    if len(funding) >= 120:
        avg_14 = sma_tail(funding, 14) or 0.0
        avg_series: Rows = [
            (funding[i][0], sum(v for _, v in funding[i - 13:i + 1]) / 14.0)
            for i in range(13, len(funding))
        ]
        bp = blended_percentile(avg_series, avg_14)
        if bp is not None and bp["pct"] > 80 and avg_14 > 0:
            triggers.append({
                "key": "funding_overheat", "label": "资金费转正过热", "penalty": 15,
                "note": f"14d 均 {avg_14:.4f}%/8h 处 {bp['pct']:.0f} 分位，多头拥挤",
            })

    etf = d.get("etf_flow_usd", [])
    if len(etf) >= 40:
        avg_7, avg_30 = sma_tail(etf, 7) or 0.0, sma_tail(etf, 30) or 0.0
        if avg_7 < 0 and avg_30 < 0:
            triggers.append({
                "key": "etf_outflow", "label": "ETF 持续流出", "penalty": 10,
                "note": f"7d 均 {avg_7 / 1e6:+.0f}M / 30d 均 {avg_30 / 1e6:+.0f}M",
            })

    if len(price_rows) >= 97:
        recent_low = min(tail_values(price_rows, 7))
        prior_low = min(tail_values(price_rows, 97)[:-7])
        if recent_low <= prior_low * 1.001:
            triggers.append({
                "key": "price_new_low", "label": "仍在创新低", "penalty": 20,
                "note": "近 7d 跌破前 90d 低点，下跌结构未破坏",
            })

    return {"triggers": triggers, "total_penalty": sum(t["penalty"] for t in triggers)}


# ══════════════════════ 卖方衰竭指数 ══════════════════════

def compute_seller_exhaustion(d: dict[str, Rows]) -> Optional[dict[str, Any]]:
    """卖方衰竭 0-100：亏损衰减 / 亏损利润比 / SOPR 回升 / 清算衰减 / STH 供应稳定，等权。"""
    parts: list[tuple[str, float]] = []

    loss_abs: Rows = [(day, abs(v)) for day, v in d.get("realized_loss", [])]
    if len(loss_abs) >= 120:
        peak = max(tail_values(loss_abs, 90))
        avg_14 = sma_tail(loss_abs, 14)
        if peak > 1e-9 and avg_14 is not None:
            parts.append(("loss_decay", clamp(100.0 * (1.0 - min(1.0, avg_14 / peak)))))

    # 亏损/利润兑现比：恐慌期 >1（亏损主导），衰竭修复期趋近 0
    profit_rows = d.get("realized_profit", [])
    if len(loss_abs) >= 30 and len(profit_rows) >= 30:
        avg_loss_14 = sma_tail(loss_abs, 14)
        avg_profit_14 = sma_tail(profit_rows, 14)
        if avg_loss_14 is not None and avg_profit_14 is not None and avg_profit_14 > 1e-9:
            ratio = avg_loss_14 / avg_profit_14
            parts.append(("loss_profit_ratio", clamp(100.0 * (1.0 - min(1.0, ratio)))))

    sopr = d.get("sopr", [])
    if len(sopr) >= 30:
        avg_14 = sma_tail(sopr, 14) or 0.0
        parts.append(("sopr_recovery", clamp((avg_14 - 0.97) / 0.05 * 100.0)))

    liq_total = liq_total_series(d)
    if len(liq_total) >= 120:
        peak = max(tail_values(liq_total, 90))
        avg_14 = sma_tail(liq_total, 14)
        if peak > 1e-9 and avg_14 is not None:
            parts.append(("liq_decay", clamp(100.0 * (1.0 - min(1.0, avg_14 / peak)))))

    sth_chg = change_rate(d.get("sth_supply", []), 30)
    if sth_chg is not None:
        parts.append(("sth_stable", clamp(100.0 - abs(sth_chg) / 0.10 * 100.0)))

    if len(parts) < 2:
        return None
    return {
        "score": round(sum(v for _, v in parts) / len(parts), 1),
        "components": {k: round(v, 1) for k, v in parts},
    }


# ══════════════════════ 反证清单 ══════════════════════

def build_counter_evidence(factors: list[dict[str, Any]],
                           fake_filter: dict[str, Any]) -> dict[str, Any]:
    """机械生成支持/反对底部的证据清单（供外部 AI 对抗性分析）。"""
    supporting: list[str] = []
    opposing: list[str] = []
    for factor in factors:
        for sub in factor["sub_signals"]:
            if not sub["ok"]:
                continue
            desc = f"[{factor['label']}] {sub['label']}：{sub['score']:.0f} 分"
            if sub["note"]:
                desc += f"（{sub['note']}）"
            if sub["score"] >= 70:
                supporting.append(desc)
            elif sub["score"] <= 30:
                opposing.append(desc)
    for trigger in fake_filter["triggers"]:
        opposing.append(f"[假底过滤] {trigger['label']}：{trigger['note']}")
    return {"supporting": supporting, "opposing": opposing}


# ══════════════════════ 四象限状态 ══════════════════════

QUADRANTS = {
    "bear_market": "熊市进行",
    "panic_flush": "恐慌出清",
    "basing": "筑底改善",
    "confirmed_recovery": "确认恢复",
}


def classify_quadrant(stress: Optional[float], confirmation: Optional[float]) -> dict[str, Any]:
    if stress is None:
        return {"key": "unknown", "label": "数据不足", "note": "有效因子权重不足"}
    # 象限 note 只解释规则状态；历史频率与时间尺度必须由版本化 base_rate
    # 动态展示，禁止把某次样本内结果硬编码成永恒结论。
    if stress < 55:
        note = "底部压力不足，市场未进入极端区" if confirmation is None or confirmation < 50 \
            else "无极端压力下的修复 = 普通回调结束，非周期大底"
        return {"key": "bear_market", "label": QUADRANTS["bear_market"], "note": note}
    if confirmation is None or confirmation < 35:
        return {"key": "panic_flush", "label": QUADRANTS["panic_flush"],
                "note": "极端压力已现，改善迹象未确认；仅表示压力状态，不代表底部概率"}
    if confirmation < 65:
        return {"key": "basing", "label": QUADRANTS["basing"],
                "note": "压力极端 + 改善迹象萌芽；历史结果需按当前版本和时间尺度另行核验"}
    return {"key": "confirmed_recovery", "label": QUADRANTS["confirmed_recovery"],
            "note": "压力极端 + 多项确认信号共振；这是规则状态，不是已校准概率"}
