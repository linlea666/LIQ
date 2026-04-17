#!/usr/bin/env python3
"""市场结构自检脚本（Market Structure Self-Check）

独立工具，用于手动验证 Commit 1 的 detect_market_structure 算法在真实
行情上的输出是否符合预期。不触碰 engine / state / 任何生产代码。

数据源：Binance USD-M Futures 公开 REST（/fapi/v1/klines，无需 API key）。

用法：
    cd backend
    python3 scripts/check_market_structure.py BTC
    python3 scripts/check_market_structure.py ETH --interval 4h
    python3 scripts/check_market_structure.py SOL --limit 300

输出为人类可读的结构报告，包含：
    - 当前价 / K 线数量 / ATR
    - 结构方向（bullish / bearish / ranging / transitioning）与置信度
    - 最近 BOS / CHoCH 事件 + 触发时间与价位
    - 结构上下沿
    - 操作偏置（long_only / short_only / both_ok / stand_aside）
    - 最近 5 个 swing high / swing low 时间价位
    - 白话总结
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# 允许从 backend 根目录 import models/processors
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.market import CandleData  # noqa: E402
from processors.market_structure import detect_market_structure  # noqa: E402
from processors.ta_core import calc_atr  # noqa: E402


BINANCE_BASE = "https://fapi.binance.com"
BYBIT_BASE = "https://api.bybit.com"
OKX_BASE = "https://www.okx.com"

_BYBIT_INTERVAL = {
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30",
    "1h": "60", "2h": "120", "4h": "240", "6h": "360", "12h": "720",
    "1d": "D", "1w": "W",
}

_OKX_BAR = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D", "1w": "1W",
}

DIRECTION_LABEL = {
    "bullish": "🟢 bullish (上升结构)",
    "bearish": "🔴 bearish (下降结构)",
    "ranging": "🔵 ranging (横向震荡)",
    "transitioning": "🟡 transitioning (结构转换中)",
}

BIAS_LABEL = {
    "long_only": "📈 long_only (顺势做多优先)",
    "short_only": "📉 short_only (顺势做空优先)",
    "both_ok": "↔️ both_ok (双向均可)",
    "stand_aside": "⏸️ stand_aside (观望为宜)",
}

EVENT_LABEL = {
    "BOS_up": "BOS_up (向上结构延续 · 破前高)",
    "BOS_down": "BOS_down (向下结构延续 · 破前低)",
    "CHoCH_up": "CHoCH_up (向上结构反转 · 破最近 swing high)",
    "CHoCH_down": "CHoCH_down (向下结构反转 · 破最近 swing low)",
    "": "无近期事件（价格仍在结构区间内）",
}


def _fetch_binance(symbol: str, interval: str, limit: int) -> list[CandleData]:
    url = f"{BINANCE_BASE}/fapi/v1/klines?symbol={symbol}&interval={interval}&limit={limit}"
    req = urllib.request.Request(url, headers={"User-Agent": "LIQ-check/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())

    coin = symbol.replace("USDT", "")
    candles: list[CandleData] = []
    for row in data:
        candles.append(CandleData(
            coin=coin,
            ts=int(row[0] // 1000),
            o=float(row[1]),
            h=float(row[2]),
            l=float(row[3]),
            c=float(row[4]),
            vol=float(row[5]),
        ))
    return candles


def _fetch_bybit(symbol: str, interval: str, limit: int) -> list[CandleData]:
    bb_iv = _BYBIT_INTERVAL.get(interval)
    if not bb_iv:
        raise ValueError(f"Bybit 不支持的 interval: {interval}")
    url = (
        f"{BYBIT_BASE}/v5/market/kline?category=linear"
        f"&symbol={symbol}&interval={bb_iv}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "LIQ-check/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())

    if payload.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {payload.get('retMsg')}")
    rows = payload.get("result", {}).get("list", [])
    # Bybit 返回按时间 **倒序**（新→旧），需反转为 旧→新
    rows = list(reversed(rows))

    coin = symbol.replace("USDT", "")
    candles: list[CandleData] = []
    for row in rows:
        candles.append(CandleData(
            coin=coin,
            ts=int(int(row[0]) // 1000),  # startTime ms → s
            o=float(row[1]),
            h=float(row[2]),
            l=float(row[3]),
            c=float(row[4]),
            vol=float(row[5]),
        ))
    return candles


def _fetch_okx(symbol: str, interval: str, limit: int) -> list[CandleData]:
    bar = _OKX_BAR.get(interval)
    if not bar:
        raise ValueError(f"OKX 不支持的 interval: {interval}")
    coin = symbol.replace("USDT", "")
    inst_id = f"{coin}-USDT-SWAP"
    url = (
        f"{OKX_BASE}/api/v5/market/history-candles"
        f"?instId={inst_id}&bar={bar}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "LIQ-check/1.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        payload = json.loads(resp.read())

    if payload.get("code") != "0":
        raise RuntimeError(f"OKX API error: {payload.get('msg')}")
    rows = payload.get("data", [])
    rows = list(reversed(rows))

    candles: list[CandleData] = []
    for row in rows:
        candles.append(CandleData(
            coin=coin,
            ts=int(int(row[0]) // 1000),
            o=float(row[1]),
            h=float(row[2]),
            l=float(row[3]),
            c=float(row[4]),
            vol=float(row[5]),
        ))
    return candles


def fetch_klines(
    symbol: str, interval: str = "1h", limit: int = 200, source: str = "auto",
) -> tuple[list[CandleData], str]:
    """拉 K 线。source=auto 时按 Binance → Bybit → OKX 顺序尝试。"""
    errors: list[str] = []
    attempts = (
        [("binance", _fetch_binance), ("bybit", _fetch_bybit), ("okx", _fetch_okx)]
        if source == "auto"
        else [(source, {"binance": _fetch_binance, "bybit": _fetch_bybit, "okx": _fetch_okx}[source])]
    )

    for name, fn in attempts:
        try:
            candles = fn(symbol, interval, limit)
            if candles:
                return candles, name
            errors.append(f"{name}: 空返回")
        except Exception as e:
            errors.append(f"{name}: {e}")
            if source != "auto":
                raise

    raise RuntimeError(f"所有数据源均失败: {'; '.join(errors)}")


def _gen_demo_candles(base: float = 75000.0, bars: int = 200) -> list[CandleData]:
    """合成一段清晰的上升结构（HH+HL）用于离线演示。"""
    import math
    now = int(time.time())
    start_ts = now - bars * 3600
    candles: list[CandleData] = []
    for i in range(bars):
        # 慢速上涨趋势 + 正弦波动模拟 swing
        trend = i * (base * 0.0003)   # 每根 ~0.03% 线性上涨
        wave = math.sin(i / 7.0) * (base * 0.01)  # ±1% 正弦波
        close = base + trend + wave
        high = close * 1.002
        low = close * 0.998
        candles.append(CandleData(
            coin="BTC",
            ts=start_ts + i * 3600,
            o=close,
            h=high,
            l=low,
            c=close,
            vol=1000.0,
        ))
    return candles


def fmt_ts(ts: int) -> str:
    if not ts:
        return "N/A"
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M"
    )


def fmt_age(event_ts: int, now_ts: int) -> str:
    if not event_ts:
        return "N/A"
    age_h = max(0, (now_ts - event_ts) // 3600)
    if age_h < 1:
        return "刚刚"
    if age_h < 24:
        return f"{age_h}h 前"
    return f"{age_h // 24}d 前"


def print_report(coin: str, interval: str, candles: list[CandleData], atr: float | None, result):
    now_ts = int(time.time())
    price = candles[-1].close if candles else 0

    print()
    print("━" * 64)
    print(f"  {coin} 市场结构检测 · {interval} · {fmt_ts(now_ts)}")
    print("━" * 64)
    print(f"  当前价:        ${price:,.2f}")
    print(f"  K 线样本数:    {len(candles)} 根")
    if atr is not None:
        print(f"  ATR(14):       {atr:.2f}  (毛刺过滤阈值 = max(0.5%, 0.8×ATR))")
    else:
        print(f"  ATR(14):       N/A（仅用 0.5% 百分比过滤）")

    if result is None:
        print()
        print(f"  ⚠️  返回 None：K 线不足（<50 根）或价格无效")
        print("━" * 64)
        print()
        return

    print()
    print(f"  结构方向:      {DIRECTION_LABEL.get(result.direction, result.direction)}")
    print(f"                 置信度 = {result.confidence:.2f}   时间框架 = {result.timeframe}")
    print(f"  最近事件:      {EVENT_LABEL.get(result.last_event, result.last_event)}")
    if result.event_ts:
        print(f"                 触发: {fmt_ts(result.event_ts)}  ({fmt_age(result.event_ts, now_ts)})  ·  触发价 ${result.event_price:,.2f}")
    print(f"  结构上沿:      ${result.structure_high:,.2f}  (最近 swing high)")
    print(f"  结构下沿:      ${result.structure_low:,.2f}  (最近 swing low)")

    if result.structure_high > 0 and result.structure_low > 0:
        width_pct = (result.structure_high - result.structure_low) / price * 100
        print(f"  结构区间宽度:  {width_pct:.2f}% 当前价")

    print(f"  操作偏置:      {BIAS_LABEL.get(result.operate_bias, result.operate_bias)}")

    if result.swing_highs:
        print()
        print("  最近 Swing High (新 → 旧):")
        for p in result.swing_highs[:5]:
            dist_pct = (p.price - price) / price * 100 if price else 0
            arrow = "↑" if dist_pct >= 0 else "↓"
            print(
                f"    ${p.price:>10,.2f}  @  {fmt_ts(p.ts)}   "
                f"{arrow} {abs(dist_pct):.2f}%   strength={p.strength}"
            )

    if result.swing_lows:
        print()
        print("  最近 Swing Low  (新 → 旧):")
        for p in result.swing_lows[:5]:
            dist_pct = (p.price - price) / price * 100 if price else 0
            arrow = "↑" if dist_pct >= 0 else "↓"
            print(
                f"    ${p.price:>10,.2f}  @  {fmt_ts(p.ts)}   "
                f"{arrow} {abs(dist_pct):.2f}%   strength={p.strength}"
            )

    print()
    print(f"  白话总结:      {result.summary}")
    print("━" * 64)
    print()


def main():
    parser = argparse.ArgumentParser(
        description="市场结构自检脚本（基于 Binance Futures 1h K 线）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("coin", help="币种代号，如 BTC / ETH / SOL")
    parser.add_argument(
        "--interval", default="1h",
        help="K 线周期（1h/4h/15m 等，默认 1h）",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="K 线数量（默认 200，最小 50）",
    )
    parser.add_argument(
        "--source", default="auto", choices=["auto", "binance", "bybit", "okx"],
        help="数据源：auto=Binance → Bybit → OKX 依次尝试；也可强制指定",
    )
    parser.add_argument(
        "--offline-demo", action="store_true",
        help="离线演示模式：使用合成的上升结构 K 线验证脚本 pipeline（不访问网络）",
    )
    args = parser.parse_args()

    if args.limit < 50:
        print("⚠️  limit 至少需要 50（否则算法会返回 None）", file=sys.stderr)
        sys.exit(2)

    symbol = f"{args.coin.upper()}USDT"

    if args.offline_demo:
        print("→ 离线演示模式：生成合成上升结构 K 线（无网络请求）...")
        candles = _gen_demo_candles(base=75000.0, bars=200)
        used_source = "synthetic_demo"
    else:
        print(f"→ 拉取 {symbol} {args.interval} K 线 × {args.limit} 根  (source={args.source}) ...")
        try:
            candles, used_source = fetch_klines(
                symbol, args.interval, args.limit, source=args.source,
            )
        except Exception as e:
            print(f"❌ 拉取 K 线失败: {e}", file=sys.stderr)
            print("   提示：如本地网络同样受限，可用 --offline-demo 验证脚本 pipeline。", file=sys.stderr)
            sys.exit(1)

    if not candles:
        print(f"❌ 未拉到任何 K 线（symbol={symbol} 可能不存在）", file=sys.stderr)
        sys.exit(1)

    print(f"✓ 成功拉取 {len(candles)} 根 K 线 (from {used_source})，开始计算结构 ...")

    # ATR 用于毛刺过滤阈值
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    atr_list = calc_atr(highs, lows, closes, period=14)
    atr = next((v for v in reversed(atr_list) if v is not None), None)

    result = detect_market_structure(
        candles, atr=atr, timeframe=args.interval,
    )

    print_report(args.coin.upper(), args.interval, candles, atr, result)


if __name__ == "__main__":
    main()
