"""Footprint 派生分析 · 从原始 buckets 提取 AI 可用的高价值信号

输入：polls.footprint 写入的 state.footprint_contract / state.footprint_spot
  格式：deque([{ts, buckets: [{price_lo, price_hi, buy_base, sell_base,
                              buy_quote, sell_quote, buy_trades, sell_trades}]}])

输出：FootprintBarStats
  - POC（成交最密集的价位）
  - 强失衡价位（ratio > 3 的 bucket）
  - 上/下 1/3 价位区的 delta_pct（识别衰竭/吸筹）
  - bar_closed：bar 是否已收盘（基于 ts + bar 周期与当前时间）
  - low_volume：bar 总成交是否低于最小阈值（低于则清空 top_imbalance_zones）

最小成交量阈值（按币种 ~价格档位 设定 · 经验值，可调）：
  · BTC：$5M  · ETH：$3M  · SOL：$1.5M  · 其他：$1M
低于阈值的 bar 仍输出聚合数值（buy/sell/delta），但 `top_imbalance_zones=[]`，
避免 AI 在小成交格子上过度解读 stacked imbalance。
"""

from __future__ import annotations

import time
from typing import Optional

from models.market_action import FootprintBarStats, FootprintSnapshot

IMBALANCE_RATIO_THRESHOLD = 3.0
TOP_IMBALANCE_LIMIT = 6

# 单根 bar 最小总成交量阈值（USD），低于此值不输出 top_imbalance_zones
_MIN_BAR_VOL_USD = {
    "BTC": 5_000_000.0,
    "ETH": 3_000_000.0,
    "SOL": 1_500_000.0,
}
_MIN_BAR_VOL_USD_DEFAULT = 1_000_000.0


def _min_bar_volume_for(coin: Optional[str]) -> float:
    if not coin:
        return _MIN_BAR_VOL_USD_DEFAULT
    return _MIN_BAR_VOL_USD.get(coin.upper(), _MIN_BAR_VOL_USD_DEFAULT)


def _mid(b: dict) -> float:
    return (b["price_lo"] + b["price_hi"]) / 2


def _is_bar_closed(bar_ts: int, bar_seconds: int, now_ts: Optional[int] = None) -> bool:
    """bar 是否已收盘 · ts 视作 bar 起始时间，当前时间 ≥ ts + 周期 才算收盘。

    Coinglass 返回的 bar ts 单位可能是秒或毫秒，统一归一为秒后比较。
    给一个 30s 容忍度，避免边界抖动（`now == ts + 周期` 时也判已收盘）。
    """
    if bar_ts <= 0 or bar_seconds <= 0:
        return True  # 无法判定时保守按已收盘（不阻止下游使用）
    ts_sec = int(bar_ts // 1000) if bar_ts > 10_000_000_000 else int(bar_ts)
    now = int(now_ts if now_ts is not None else time.time())
    return now + 30 >= ts_sec + bar_seconds


def analyze_bar(
    bar: dict,
    *,
    coin: Optional[str] = None,
    bar_seconds: int = 3600,
    now_ts: Optional[int] = None,
) -> Optional[FootprintBarStats]:
    """把一根 K 线的原始 buckets 压缩成 AI 友好的统计。

    Args:
      coin: 用于决定最小成交量阈值（BTC/ETH/SOL 各档不同）
      bar_seconds: bar 周期（秒），用于 bar_closed 判定（默认 1h = 3600s）
      now_ts: 注入"当前时间"，仅供测试 / 离线回放用；线上传 None 即可
    """
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

    # 低成交量保护：清空 zones（聚合数值仍输出）
    min_vol = _min_bar_volume_for(coin)
    low_volume = total < min_vol
    if low_volume:
        top_imbalances = []

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
        bar_closed=_is_bar_closed(int(bar["ts"]), bar_seconds, now_ts),
        low_volume=low_volume,
    )


def build_snapshot(
    contract_bars: list[dict],
    spot_bars: list[dict],
    *,
    coin: Optional[str] = None,
    bar_seconds: int = 3600,
    now_ts: Optional[int] = None,
) -> Optional[FootprintSnapshot]:
    """合约+现货 footprint 快照。bars 应按时间升序。

    Args:
      coin: 币种代号（用于最小成交量阈值；缺省走 default 阈值）
      bar_seconds: bar 周期秒数（poll_footprint 默认 1h = 3600）
      now_ts: 注入"当前时间"，便于测试；线上传 None
    """
    if not contract_bars and not spot_bars:
        return None

    def _ab(bar):
        return analyze_bar(bar, coin=coin, bar_seconds=bar_seconds, now_ts=now_ts) if bar else None

    c_latest = _ab(contract_bars[-1]) if contract_bars else None
    c_prev = _ab(contract_bars[-2]) if len(contract_bars) >= 2 else None
    s_latest = _ab(spot_bars[-1]) if spot_bars else None
    s_prev = _ab(spot_bars[-2]) if len(spot_bars) >= 2 else None

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
