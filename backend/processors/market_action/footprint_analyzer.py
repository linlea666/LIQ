"""Footprint 派生分析 · 从原始 buckets 提取 AI 可用的高价值信号

输入：polls.footprint 写入的 state.footprint_contract / state.footprint_spot
  格式：deque([{ts, buckets: [{price_lo, price_hi, buy_base, sell_base,
                              buy_quote, sell_quote, buy_trades, sell_trades}]}])

输出：FootprintBarStats
  - POC（成交最密集的价位）
  - 强失衡价位（ratio > 3 的 bucket）
  - 上/下 1/3 价位区的 delta_pct（识别衰竭/吸筹）
"""

from __future__ import annotations

from typing import Optional

from models.market_action import FootprintBarStats, FootprintSnapshot

IMBALANCE_RATIO_THRESHOLD = 3.0
TOP_IMBALANCE_LIMIT = 6


def _mid(b: dict) -> float:
    return (b["price_lo"] + b["price_hi"]) / 2


def analyze_bar(bar: dict) -> Optional[FootprintBarStats]:
    """把一根 K 线的原始 buckets 压缩成 AI 友好的统计。"""
    if not bar or not bar.get("buckets"):
        return None
    buckets = bar["buckets"]
    total_buy = sum(b["buy_quote"] for b in buckets)
    total_sell = sum(b["sell_quote"] for b in buckets)
    delta = total_buy - total_sell
    total = total_buy + total_sell
    delta_pct = (delta / total) if total > 0 else 0.0

    # POC = 成交量（buy+sell quote）最大的 bucket 中点
    poc_bucket = max(buckets, key=lambda b: b["buy_quote"] + b["sell_quote"])
    poc_price = _mid(poc_bucket)

    # 强失衡 top N
    imbalances = []
    for b in buckets:
        bq = b["buy_quote"]
        sq = b["sell_quote"]
        if bq <= 0 and sq <= 0:
            continue
        ratio = (bq / sq) if sq > 0 else float("inf") if bq > 0 else 0.0
        inv_ratio = (sq / bq) if bq > 0 else float("inf") if sq > 0 else 0.0
        if ratio >= IMBALANCE_RATIO_THRESHOLD:
            imbalances.append({
                "price": round(_mid(b), 4),
                "buy": round(bq, 2), "sell": round(sq, 2),
                "ratio": round(min(ratio, 999.9), 2),
                "side": "stacked_buy",
            })
        elif inv_ratio >= IMBALANCE_RATIO_THRESHOLD:
            imbalances.append({
                "price": round(_mid(b), 4),
                "buy": round(bq, 2), "sell": round(sq, 2),
                "ratio": round(min(inv_ratio, 999.9), 2),
                "side": "stacked_sell",
            })
    imbalances.sort(key=lambda x: x["ratio"], reverse=True)
    top_imbalances = imbalances[:TOP_IMBALANCE_LIMIT]

    # 上/下 1/3 价位区
    prices = [_mid(b) for b in buckets]
    if prices:
        p_min, p_max = min(prices), max(prices)
        p_span = p_max - p_min
        if p_span > 0:
            third = p_span / 3.0
            high_zone = [b for b in buckets if _mid(b) >= p_max - third]
            low_zone = [b for b in buckets if _mid(b) <= p_min + third]
            hb = sum(b["buy_quote"] for b in high_zone)
            hs = sum(b["sell_quote"] for b in high_zone)
            lb = sum(b["buy_quote"] for b in low_zone)
            ls = sum(b["sell_quote"] for b in low_zone)
            high_pct = ((hb - hs) / (hb + hs)) if (hb + hs) > 0 else None
            low_pct = ((lb - ls) / (lb + ls)) if (lb + ls) > 0 else None
        else:
            high_pct = low_pct = None
    else:
        high_pct = low_pct = None

    return FootprintBarStats(
        ts=int(bar["ts"]),
        total_buy_usd=round(total_buy, 2),
        total_sell_usd=round(total_sell, 2),
        delta_usd=round(delta, 2),
        delta_pct=round(delta_pct, 4),
        poc_price=round(poc_price, 4),
        top_imbalance_zones=top_imbalances,
        high_price_delta_pct=round(high_pct, 4) if high_pct is not None else None,
        low_price_delta_pct=round(low_pct, 4) if low_pct is not None else None,
    )


def build_snapshot(
    contract_bars: list[dict],
    spot_bars: list[dict],
) -> Optional[FootprintSnapshot]:
    """合约+现货 footprint 快照。bars 应按时间升序。"""
    if not contract_bars and not spot_bars:
        return None

    c_latest = analyze_bar(contract_bars[-1]) if contract_bars else None
    c_prev = analyze_bar(contract_bars[-2]) if len(contract_bars) >= 2 else None
    s_latest = analyze_bar(spot_bars[-1]) if spot_bars else None
    s_prev = analyze_bar(spot_bars[-2]) if len(spot_bars) >= 2 else None

    diff_pct: Optional[float] = None
    interp: Optional[str] = None
    if c_latest and s_latest:
        diff_pct = round(s_latest.delta_pct - c_latest.delta_pct, 4)
        if abs(diff_pct) < 0.05:
            interp = "期现一致"
        elif diff_pct > 0.15:
            interp = "现货领先（健康扩张）"
        elif diff_pct < -0.15:
            interp = "合约单边（杠杆推动，现货未跟）"
        else:
            interp = "期现轻度分化"

    return FootprintSnapshot(
        contract_latest=c_latest,
        contract_prev=c_prev,
        spot_latest=s_latest,
        spot_prev=s_prev,
        spot_contract_delta_diff_pct=diff_pct,
        interpretation=interp,
    )
