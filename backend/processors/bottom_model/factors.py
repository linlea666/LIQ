"""Bottom Model 因子引擎：标准化原语 + 六因子 + Confirmation + 假底过滤。

设计要点（对齐方案裁定）：
- 标准化 = 3y/5y/全历史三窗口滚动百分位混合（权重 30/30/40），窗口样本
  不足 90 天则跳过该窗口并重归一化——BGeometrics 4 年史自动退化为 3y/4y。
- 评分方向统一：**分数越高越接近"底部特征"**（0-100）。
- 双层分离：Stress（市场多惨，慢变量）与 Confirmation（是否开始改善，
  快变量）各自独立计算，绝不混合——这是假底过滤的结构性前提。
- 铁律：任何单一信号只加分、不一票否决；缺失子信号只降低因子覆盖率，
  coverage < 0.4 的因子整体弃权并把权重重归一化给其余因子。
- 纯函数：输入截断后的日级序列 dict，无 IO、无状态，历史类比直接复用。
"""

from __future__ import annotations

import logging
from statistics import median
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
    "demand": "需求",
    "structure": "价格结构",
    "macro": "宏观",
}

# 因子有效覆盖率下限：低于此值该因子弃权（权重重归一化）
_MIN_FACTOR_COVERAGE = 0.4


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
    """最近值相对 n 天前的变化率；基准接近 0 时返回 None。"""
    if len(rows) <= n:
        return None
    base = rows[-1 - n][1]
    if abs(base) < 1e-12:
        return None
    return (rows[-1][1] - base) / abs(base)


def percentile_rank(values: list[float], value: float) -> Optional[float]:
    if not values:
        return None
    return 100.0 * sum(1 for v in values if v <= value) / len(values)


def robust_z(values: list[float], value: float) -> Optional[float]:
    """Robust Z = (v - median) / (1.4826 × MAD)；MAD=0 时返回 None。"""
    if len(values) < 10:
        return None
    med = median(values)
    mad = median(abs(v - med) for v in values)
    if mad < 1e-12:
        return None
    return (value - med) / (1.4826 * mad)


def blended_percentile(rows: Rows, value: Optional[float] = None) -> Optional[dict[str, Any]]:
    """三窗口混合百分位。返回 {pct, windows:[{days,n,pct}]}；数据不足返回 None。"""
    if not rows:
        return None
    if value is None:
        value = rows[-1][1]
    parts: list[tuple[float, float, dict[str, Any]]] = []
    for days, weight in PERCENTILE_WINDOWS:
        window_rows = rows if days is None else rows[-days:]
        vals = values_of(window_rows)
        if len(vals) < _MIN_WINDOW_SAMPLES:
            continue
        pct = percentile_rank(vals, value)
        parts.append((weight, pct, {
            "days": days, "n": len(vals), "pct": round(pct, 1),
        }))
    if not parts:
        return None
    total_w = sum(w for w, _, _ in parts)
    blended = sum(w * p for w, p, _ in parts) / total_w
    return {"pct": round(blended, 1), "windows": [meta for _, _, meta in parts]}


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
         note: str = "") -> dict[str, Any]:
    ok = score is not None
    return {
        "key": key, "label": label, "weight": weight, "ok": ok,
        "score": round(clamp(score), 1) if ok else None,
        "value": round(value, 6) if isinstance(value, (int, float)) else None,
        "percentile": round(percentile, 1) if isinstance(percentile, (int, float)) else None,
        "note": note,
    }


def _pct_sub(key: str, label: str, rows: Rows, weight: float,
             invert: bool = True, note: str = "") -> dict[str, Any]:
    """按混合百分位打分的通用子信号；invert=True 表示低分位 = 高分（底部方向）。"""
    bp = blended_percentile(rows)
    if bp is None:
        return _sub(key, label, None, weight, note=note or "数据不足")
    score = 100.0 - bp["pct"] if invert else bp["pct"]
    return _sub(key, label, score, weight, value=last_value(rows),
                percentile=bp["pct"], note=note)


def _factor(key: str, subs: list[dict[str, Any]]) -> dict[str, Any]:
    total_w = sum(s["weight"] for s in subs)
    ok_subs = [s for s in subs if s["ok"]]
    ok_w = sum(s["weight"] for s in ok_subs)
    coverage = ok_w / total_w if total_w > 0 else 0.0
    score = (
        sum(s["weight"] * s["score"] for s in ok_subs) / ok_w
        if ok_w > 0 and coverage >= _MIN_FACTOR_COVERAGE else None
    )
    return {
        "key": key,
        "label": FACTOR_LABELS[key],
        "weight": FACTOR_WEIGHTS[key],
        "score": round(score, 1) if score is not None else None,
        "coverage": round(coverage, 2),
        "sub_signals": subs,
    }


# ══════════════════════ 六因子 ══════════════════════

def _factor_valuation(d: dict[str, Rows]) -> dict[str, Any]:
    subs = [
        _pct_sub("mvrv_z", "MVRV Z-Score", d.get("mvrv_zscore", []), 1.0,
                 note="BGeometrics，4 年窗口"),
        _pct_sub("nupl", "NUPL", d.get("nupl", []), 1.0, note="2009 起全历史"),
        _pct_sub("reserve_risk", "Reserve Risk", d.get("reserve_risk", []), 1.0,
                 note="2010 起全历史"),
    ]
    # STH-MVRV：优先 BGeometrics；缺失则用 价格/STH Realized Price 派生
    sth_mvrv = d.get("sth_mvrv", [])
    if not sth_mvrv:
        sth_mvrv = ratio_series(d.get("btc_price_onchain", []),
                                d.get("sth_realized_price", []))
    subs.append(_pct_sub("sth_mvrv", "STH-MVRV", sth_mvrv, 1.0))
    # 价格 / 200 周均线 偏离（2010 起全历史）
    ratio_200w = ratio_series(d.get("btc_price_onchain", []), d.get("ma_200w", []))
    subs.append(_pct_sub("price_vs_200w", "价格/200W均线", ratio_200w, 1.0,
                         note="<1 = 跌破 200 周均线"))
    return _factor("valuation", subs)


def _factor_capitulation(d: dict[str, Rows]) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    # 已实现亏损：极值（投降已发生）+ 衰减（卖方衰竭）
    loss_abs: Rows = [(day, abs(v)) for day, v in d.get("realized_loss", [])]
    if len(loss_abs) >= 120:
        peak_90d = max(tail_values(loss_abs, 90))
        bp = blended_percentile(loss_abs, peak_90d)
        subs.append(_sub(
            "loss_extreme", "已实现亏损·90d 峰值", bp["pct"] if bp else None, 1.0,
            value=peak_90d, percentile=bp["pct"] if bp else None,
            note="峰值分位越高 = 投降越充分",
        ))
        avg_14d = sma_tail(loss_abs, 14)
        if peak_90d > 1e-9 and avg_14d is not None:
            decay_ratio = avg_14d / peak_90d
            subs.append(_sub(
                "loss_decay", "亏损衰减（卖方衰竭）",
                100.0 * (1.0 - min(1.0, decay_ratio)), 1.0,
                value=decay_ratio, note="14d 均值 / 90d 峰值，越低越衰竭",
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
        subs.append(_sub(
            "sopr_structure", "aSOPR 跌破1→收复", score, 1.0, value=avg_14,
            note=f"90d 内 <1 占比 {below_share:.0%}，14d 均 {avg_14:.4f}",
        ))
    else:
        subs.append(_sub("sopr_structure", "aSOPR 跌破1→收复", None, 1.0, note="数据不足"))
    subs.append(_pct_sub("lth_sopr", "LTH-SOPR", d.get("lth_sopr", []), 0.5,
                         note="低分位 = 长期持有者亏损兑现"))
    # LTH 已实现亏损极值
    lth_loss_abs: Rows = [(day, abs(v)) for day, v in d.get("lth_realized_loss", [])]
    if len(lth_loss_abs) >= 120:
        peak = max(tail_values(lth_loss_abs, 90))
        bp = blended_percentile(lth_loss_abs, peak)
        subs.append(_sub(
            "lth_loss_extreme", "LTH 亏损·90d 峰值", bp["pct"] if bp else None, 0.5,
            value=peak, percentile=bp["pct"] if bp else None,
            note="LTH 投降是历史大底标志",
        ))
    else:
        subs.append(_sub("lth_loss_extreme", "LTH 亏损·90d 峰值", None, 0.5, note="数据不足"))
    # STH 供应 90d 变化：恐慌换手后筹码转移给 LTH → 变化率低分位加分
    sth_chg = change_rate(d.get("sth_supply", []), 90)
    sth_supply = d.get("sth_supply", [])
    if sth_chg is not None and len(sth_supply) > 180:
        chg_series: Rows = [
            (sth_supply[i][0],
             (sth_supply[i][1] - sth_supply[i - 90][1]) / abs(sth_supply[i - 90][1]))
            for i in range(90, len(sth_supply))
            if abs(sth_supply[i - 90][1]) > 1e-9
        ]
        bp = blended_percentile(chg_series, sth_chg)
        subs.append(_sub(
            "sth_supply_drop", "STH 供应 90d 变化", 100.0 - bp["pct"] if bp else None,
            0.5, value=sth_chg, percentile=bp["pct"] if bp else None,
            note="下降 = 筹码从短期恐慌盘转移至长期持有者",
        ))
    else:
        subs.append(_sub("sth_supply_drop", "STH 供应 90d 变化", None, 0.5, note="数据不足"))
    return _factor("capitulation", subs)


def _oi_flush_sub(key: str, label: str, rows: Rows, weight: float, note: str = "") -> dict[str, Any]:
    """OI 从 180d 峰值的回撤幅度 → 线性映射（回撤 ≥30% 满分）。"""
    if len(rows) < 60:
        return _sub(key, label, None, weight, note=note or "数据不足")
    peak = max(tail_values(rows, 180))
    current = rows[-1][1]
    if peak <= 1e-9:
        return _sub(key, label, None, weight, note="峰值异常")
    flush = 1.0 - current / peak
    score = clamp(flush / 0.30 * 100.0)
    return _sub(key, label, score, weight, value=flush,
                note=note or f"距 180d 峰值回撤 {flush:.1%}")


def _factor_leverage(d: dict[str, Rows]) -> dict[str, Any]:
    subs = [
        _oi_flush_sub("oi_flush", "聚合 OI 出清", d.get("oi_agg_usd", []), 1.0),
        _oi_flush_sub("cme_oi_flush", "CME OI 出清（机构）", d.get("cme_oi_usd", []), 1.0,
                      note="机构持仓出清比成交量更硬"),
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
        subs.append(_sub(
            "funding_reset", "OI 加权资金费·30d", 100.0 - bp["pct"] if bp else None,
            1.0, value=avg_30, percentile=bp["pct"] if bp else None,
            note="窗口仅 2023-11 起，分位解读需谨慎",
        ))
    else:
        subs.append(_sub("funding_reset", "OI 加权资金费·30d", None, 1.0, note="数据不足"))
    # 清算出清：多空合计 90d 峰值分位（窗口仅 2023-11 起）
    liq_long, liq_short = d.get("liq_long_usd", []), d.get("liq_short_usd", [])
    liq_total: Rows = [
        (day, vl + vs) for day, vl, vs in align(liq_long, liq_short)
    ]
    if len(liq_total) >= 120:
        peak = max(tail_values(liq_total, 90))
        bp = blended_percentile(liq_total, peak)
        subs.append(_sub(
            "liq_flush", "清算量·90d 峰值", bp["pct"] if bp else None, 1.0,
            value=peak, percentile=bp["pct"] if bp else None,
            note="窗口仅 2023-11 起；极端清算 = 强制出清已发生",
        ))
    else:
        subs.append(_sub("liq_flush", "清算量·90d 峰值", None, 1.0, note="数据不足"))
    # CME 恐慌周量：近 8 周峰值的全历史分位（加分项，权重减半）
    cme_vol = d.get("cme_vol_1w", [])
    if len(cme_vol) >= 100:
        peak_8w = max(tail_values(cme_vol, 8))
        pct = percentile_rank(values_of(cme_vol), peak_8w)
        subs.append(_sub(
            "cme_panic_vol", "CME 恐慌周量", pct, 0.5,
            value=peak_8w, percentile=pct,
            note="BTC=F 前月合约代理指标（2017-12 起）；加分项不作否决",
        ))
    else:
        subs.append(_sub("cme_panic_vol", "CME 恐慌周量", None, 0.5, note="数据不足"))
    return _factor("leverage", subs)


def _factor_demand(d: dict[str, Rows]) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
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
        subs.append(_sub(
            "etf_momentum", "ETF 流动量·7d/30d", score, 1.0, value=sum_30,
            note=f"7d 均 {avg_7 / 1e6:+.0f}M，30d 均 {avg_30 / 1e6:+.0f}M（2024-01 起）",
        ))
    else:
        subs.append(_sub("etf_momentum", "ETF 流动量·7d/30d", None, 1.0, note="数据不足"))
    # 稳定币市值 30d 增速分位：高 = 场外弹药积累
    sc = d.get("stablecoin_total_mcap", [])
    sc_chg = change_rate(sc, 30)
    if sc_chg is not None and len(sc) > 180:
        chg_series: Rows = [
            (sc[i][0], (sc[i][1] - sc[i - 30][1]) / abs(sc[i - 30][1]))
            for i in range(30, len(sc)) if abs(sc[i - 30][1]) > 1e-9
        ]
        bp = blended_percentile(chg_series, sc_chg)
        subs.append(_sub(
            "stablecoin_growth", "稳定币市值·30d 增速", bp["pct"] if bp else None,
            1.0, value=sc_chg, percentile=bp["pct"] if bp else None,
            note="增速高分位 = 场外资金积累",
        ))
    else:
        subs.append(_sub("stablecoin_growth", "稳定币市值·30d 增速", None, 1.0, note="数据不足"))
    # 交易所余额 30d 变化：流出（负）加分
    bal_chg = change_rate(d.get("exchange_balance_btc", []), 30)
    if bal_chg is not None:
        score = clamp(50.0 - bal_chg / 0.05 * 50.0)   # -5% 流出满分，+5% 流入 0 分
        subs.append(_sub(
            "exchange_outflow", "交易所余额·30d 变化", score, 1.0, value=bal_chg,
            note="窗口仅 2024-08 起；流出 = 持币意愿",
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


def _factor_structure(d: dict[str, Rows]) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    price = d.get("btc_price_onchain", [])
    # 回撤深度：当前回撤在全历史回撤分布中的分位
    dd = drawdown_series(price)
    subs.append(_pct_sub("drawdown", "回撤深度", dd, 1.0, invert=False,
                         note="当前回撤在 2010 起回撤分布中的分位"))
    # 周线 LL→HL 结构
    hl = weekly_higher_low(d.get("btc_low_1w", []))
    subs.append(_sub(
        "weekly_hl", "周线 LL→HL", None if hl is None else (100.0 if hl else 20.0),
        1.0, note="最低周后连续抬高低点 = 结构反转",
    ))
    # 收复 STH Realized Price
    pair = align(price, d.get("sth_realized_price", []))
    if pair:
        _, close, sth_rp = pair[-1]
        if sth_rp > 1e-9:
            ratio = close / sth_rp
            score = clamp((ratio - 0.80) / 0.25 * 100.0)   # 0.80→0 分，1.05→满分
            subs.append(_sub(
                "reclaim_sth_rp", "价格 vs STH 成本", score, 1.0, value=ratio,
                note=f"价格/STH-RP = {ratio:.3f}（>1 = 短期持有者回到盈利）",
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
        if made_new_low:
            peak_90 = max(tail_values(loss_abs, 90))
            recent_loss = max(tail_values(loss_abs, 14))
            diverged = peak_90 > 1e-9 and recent_loss < 0.5 * peak_90
            subs.append(_sub(
                "bottom_divergence", "新低卖压背离", 100.0 if diverged else 30.0, 0.5,
                note="创新低但亏损兑现显著低于前峰 = 卖压衰竭背离" if diverged
                else "创新低且卖压未衰竭",
            ))
        else:
            subs.append(_sub(
                "bottom_divergence", "新低卖压背离", 60.0, 0.5,
                note="近 14d 未创 90d 新低",
            ))
    else:
        subs.append(_sub("bottom_divergence", "新低卖压背离", None, 0.5, note="数据不足"))
    return _factor("structure", subs)


def _factor_macro(d: dict[str, Rows]) -> dict[str, Any]:
    subs: list[dict[str, Any]] = []
    # 全球 M2 同比：回升趋势 + 低位分位（宽松空间）
    m2 = d.get("global_m2_yoy", [])
    if len(m2) >= 30:
        bp = blended_percentile(m2)
        rising = len(m2) >= 4 and m2[-1][1] > m2[-4][1]
        level_score = 100.0 - bp["pct"] if bp else 50.0
        score = 0.5 * level_score + 0.5 * (100.0 if rising else 0.0)
        subs.append(_sub(
            "m2_trend", "全球 M2 同比", score, 1.0, value=last_value(m2),
            percentile=bp["pct"] if bp else None,
            note=f"{'回升中' if rising else '未回升'}（上游滞后约 1 个月）",
        ))
    else:
        subs.append(_sub("m2_trend", "全球 M2 同比", None, 1.0, note="数据不足"))
    # 恐惧贪婪：当前极端 + 持续时长
    fg = d.get("fear_greed", [])
    if len(fg) >= 60:
        current = fg[-1][1]
        extreme_share = sum(1 for v in tail_values(fg, 30) if v < 25) / 30.0
        score = 0.6 * (100.0 - current) + 0.4 * (extreme_share * 100.0)
        subs.append(_sub(
            "fear_extreme", "恐惧贪婪·极端持续", score, 1.0, value=current,
            note=f"当前 {current:.0f}，近 30d 极端恐惧占比 {extreme_share:.0%}",
        ))
    else:
        subs.append(_sub("fear_extreme", "恐惧贪婪·极端持续", None, 1.0, note="数据不足"))
    return _factor("macro", subs)


# ══════════════════════ Stress 汇总 ══════════════════════

def compute_factors(d: dict[str, Rows]) -> list[dict[str, Any]]:
    return [
        _factor_valuation(d),
        _factor_capitulation(d),
        _factor_leverage(d),
        _factor_demand(d),
        _factor_structure(d),
        _factor_macro(d),
    ]


def compute_stress(factors: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Bottom Stress 0-100：有效因子加权，弃权因子的权重重归一化。"""
    active = [f for f in factors if f["score"] is not None]
    total_w = sum(f["weight"] for f in active)
    if total_w < 0.5:    # 有效权重不足一半，整体拒绝给分
        return None
    stress = sum(f["weight"] * f["score"] for f in active) / total_w
    return {
        "score": round(stress, 1),
        "active_weight": round(total_w, 2),
        "abstained": [f["key"] for f in factors if f["score"] is None],
    }


# ══════════════════════ Confirmation 层 ══════════════════════

def compute_confirmation(d: dict[str, Rows]) -> dict[str, Any]:
    """底部确认分 0-100：改善迹象（快变量），与 Stress 严格分离。"""
    checks: list[dict[str, Any]] = []

    def _check(key: str, label: str, score: Optional[float], note: str = "") -> None:
        checks.append({
            "key": key, "label": label, "ok": score is not None,
            "score": round(clamp(score), 1) if score is not None else None,
            "note": note,
        })

    # 1. aSOPR 收复 1
    sopr = d.get("sopr", [])
    if len(sopr) >= 90:
        avg_14 = sma_tail(sopr, 14) or 0.0
        was_below = min(tail_values(sopr, 90)) < 0.98
        if avg_14 >= 1.0 and was_below:
            _check("sopr_reclaim", "aSOPR 收复 1", 100.0, f"14d 均 {avg_14:.4f}")
        elif avg_14 >= 0.995:
            _check("sopr_reclaim", "aSOPR 收复 1", 50.0, f"14d 均 {avg_14:.4f}，接近收复")
        else:
            _check("sopr_reclaim", "aSOPR 收复 1", 0.0, f"14d 均 {avg_14:.4f}，仍亏损兑现")
    else:
        _check("sopr_reclaim", "aSOPR 收复 1", None, "数据不足")

    # 2. 价格收复 STH Realized Price
    pair = align(d.get("btc_price_onchain", []), d.get("sth_realized_price", []))
    if pair:
        _, close, sth_rp = pair[-1]
        ratio = close / sth_rp if sth_rp > 1e-9 else 0.0
        _check("price_reclaim_sth", "收复 STH 成本线",
               100.0 if ratio >= 1.0 else 50.0 if ratio >= 0.97 else 0.0,
               f"价格/STH-RP = {ratio:.3f}")
    else:
        _check("price_reclaim_sth", "收复 STH 成本线", None, "数据不足")

    # 3. ETF 流转正
    etf = d.get("etf_flow_usd", [])
    if len(etf) >= 40:
        avg_7, avg_30 = sma_tail(etf, 7) or 0.0, sma_tail(etf, 30) or 0.0
        if avg_7 > 0 and avg_30 < 0:
            _check("etf_turn", "ETF 流反转", 100.0, "30d 净流出后 7d 转正")
        elif avg_7 > 0:
            _check("etf_turn", "ETF 流反转", 70.0, "7d/30d 均为净流入")
        else:
            _check("etf_turn", "ETF 流反转", 0.0, "7d 仍净流出")
    else:
        _check("etf_turn", "ETF 流反转", None, "数据不足")

    # 4. 资金费率正常化（从负极端回升）
    funding = d.get("funding_oiw", [])
    if len(funding) >= 90:
        avg_14 = sma_tail(funding, 14) or 0.0
        low_90 = min(tail_values(funding, 90))
        if low_90 < -0.0001 and avg_14 >= -0.0001:
            _check("funding_normalize", "资金费正常化", 100.0,
                   f"90d 低点 {low_90:.4%} → 14d 均 {avg_14:.4%}")
        elif avg_14 > low_90:
            _check("funding_normalize", "资金费正常化", 50.0, "从低点回升中")
        else:
            _check("funding_normalize", "资金费正常化", 0.0, "仍在极端区")
    else:
        _check("funding_normalize", "资金费正常化", None, "数据不足")

    # 5. 周线 HL 结构
    hl = weekly_higher_low(d.get("btc_low_1w", []))
    _check("weekly_hl", "周线低点抬高", None if hl is None else (100.0 if hl else 0.0))

    ok_checks = [c for c in checks if c["ok"]]
    base = sum(c["score"] for c in ok_checks) / len(ok_checks) if ok_checks else None
    return {
        "score": round(base, 1) if base is not None else None,
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
                "note": f"14d 均 {avg_14:.4%} 处 {bp['pct']:.0f} 分位，多头拥挤",
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
    """卖方衰竭 0-100：亏损衰减 / SOPR 回升 / 清算衰减 / STH 供应稳定，等权。"""
    parts: list[tuple[str, float]] = []

    loss_abs: Rows = [(day, abs(v)) for day, v in d.get("realized_loss", [])]
    if len(loss_abs) >= 120:
        peak = max(tail_values(loss_abs, 90))
        avg_14 = sma_tail(loss_abs, 14)
        if peak > 1e-9 and avg_14 is not None:
            parts.append(("loss_decay", clamp(100.0 * (1.0 - min(1.0, avg_14 / peak)))))

    sopr = d.get("sopr", [])
    if len(sopr) >= 30:
        avg_14 = sma_tail(sopr, 14) or 0.0
        parts.append(("sopr_recovery", clamp((avg_14 - 0.97) / 0.05 * 100.0)))

    liq_long, liq_short = d.get("liq_long_usd", []), d.get("liq_short_usd", [])
    liq_total: Rows = [(day, vl + vs) for day, vl, vs in align(liq_long, liq_short)]
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
    if stress < 55:
        note = "底部压力不足，市场未进入极端区" if confirmation is None or confirmation < 50 \
            else "无极端压力下的修复 = 普通回调结束，非周期大底"
        return {"key": "bear_market", "label": QUADRANTS["bear_market"], "note": note}
    if confirmation is None or confirmation < 35:
        return {"key": "panic_flush", "label": QUADRANTS["panic_flush"],
                "note": "极端压力已现，改善迹象未确认——历史大底多在此象限完成左侧"}
    if confirmation < 65:
        return {"key": "basing", "label": QUADRANTS["basing"],
                "note": "压力极端 + 改善迹象萌芽，筑底进行中"}
    return {"key": "confirmed_recovery", "label": QUADRANTS["confirmed_recovery"],
            "note": "压力极端 + 多项确认信号共振"}
