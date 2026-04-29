#!/usr/bin/env python3
"""Coinbase 现货 orderbook probe（独立调研工具，不进生产链路）。

用途：
    回答两个问题——
      1. Coinbase /products/{id}/book?level=2 单次拉到的 orderbook 实际多大？
      2. 各距离区间（±0.1% / ±0.5% / ±1% / ±2% / ±5%）的 USD 厚度规模？

为何独立 probe：
    - Coinbase 有自家 600/min 限速，与 Coinglass 10/min 配额完全独立
    - 此 probe 仅出网到 api.exchange.coinbase.com，不消耗 Coinglass 任何配额
    - 使用现有 CoinbaseNativeSource + parse_orderbook_frame（复用，不重写）

用法：
    cd backend
    python3 scripts/probe_coinbase.py                  # 默认 BTC-USD
    python3 scripts/probe_coinbase.py --product ETH-USD
    python3 scripts/probe_coinbase.py --product BTC-USD --json   # 输出完整 raw 到 stdout

输出：
    控制台汇总 + （--save 时）写到 backend/scripts/coinbase_probe_samples/{ts}/{product}.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.coinbase_native import CoinbaseNativeSource, parse_orderbook_frame  # noqa: E402

logger = logging.getLogger("probe_coinbase")


# 距离桶定义（百分比，相对 mid price）
DISTANCE_BUCKETS = [0.1, 0.5, 1.0, 2.0, 5.0]


def _format_usd(usd: float) -> str:
    if usd >= 1e8:
        return f"{usd / 1e8:.2f} 亿"
    if usd >= 1e4:
        return f"{usd / 1e4:.1f} 万"
    return f"{usd:.0f}"


def _format_bytes(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024 * 1024):.2f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


async def probe_one(src: CoinbaseNativeSource, product: str,
                    save_dir: Path | None) -> dict:
    """拉取一次 Coinbase orderbook 并输出统计（src 由调用方管理生命周期）。"""
    t0 = time.time()
    raw = await src.fetch_orderbook(product_id=product, level=2)
    elapsed_ms = (time.time() - t0) * 1000

    if not raw:
        return {"ok": False, "product": product, "error": "fetch failed"}

    payload_bytes = len(json.dumps(raw, ensure_ascii=False).encode("utf-8"))
    frame = parse_orderbook_frame(coin=product.split("-")[0], product_id=product, raw=raw)

    if frame is None or (not frame.bids and not frame.asks):
        return {"ok": False, "product": product, "error": "parse failed or empty"}

    # 价格元数据
    best_bid = frame.bids[-1].price if frame.bids else 0.0     # 升序排列后最高 bid 在末尾
    best_ask = frame.asks[0].price if frame.asks else 0.0      # 升序排列后最低 ask 在开头
    mid = (best_bid + best_ask) / 2.0 if (best_bid and best_ask) else max(best_bid, best_ask)
    spread = best_ask - best_bid if (best_bid and best_ask) else 0.0
    spread_bps = (spread / mid * 10_000) if mid > 0 else 0.0

    # 总档位
    bid_count = len(frame.bids)
    ask_count = len(frame.asks)

    # 单档最大订单（USD = price × size）
    bid_max_single = max((b.price * b.size for b in frame.bids), default=0.0)
    ask_max_single = max((b.price * b.size for b in frame.asks), default=0.0)

    # 前 50 档累计
    top_n = 50
    bid_top_usd = sum(b.price * b.size for b in frame.bids[-top_n:])      # 升序末尾是 best
    ask_top_usd = sum(b.price * b.size for b in frame.asks[:top_n])

    # 距离桶累计 USD（按 mid 计算百分比距离）
    distance_results: dict[float, dict[str, float]] = {}
    for pct in DISTANCE_BUCKETS:
        if mid <= 0:
            distance_results[pct] = {"bid_usd": 0.0, "ask_usd": 0.0,
                                     "bid_levels": 0, "ask_levels": 0}
            continue
        lo = mid * (1 - pct / 100.0)
        hi = mid * (1 + pct / 100.0)
        bid_usd = sum(b.price * b.size for b in frame.bids if b.price >= lo)
        ask_usd = sum(b.price * b.size for b in frame.asks if b.price <= hi)
        bid_levels = sum(1 for b in frame.bids if b.price >= lo)
        ask_levels = sum(1 for b in frame.asks if b.price <= hi)
        distance_results[pct] = {
            "bid_usd": bid_usd, "ask_usd": ask_usd,
            "bid_levels": bid_levels, "ask_levels": ask_levels,
        }

    # 总盘 USD
    total_bid_usd = sum(b.price * b.size for b in frame.bids)
    total_ask_usd = sum(b.price * b.size for b in frame.asks)

    summary = {
        "ok": True,
        "product": product,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "api_ts": frame.api_ts_iso,
        "sequence": frame.sequence,
        "latency_ms": round(elapsed_ms, 1),
        "payload_bytes": payload_bytes,
        "bid_count": bid_count,
        "ask_count": ask_count,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_usd": spread,
        "spread_bps": spread_bps,
        "bid_max_single_usd": bid_max_single,
        "ask_max_single_usd": ask_max_single,
        "top_50_bid_usd": bid_top_usd,
        "top_50_ask_usd": ask_top_usd,
        "total_bid_usd": total_bid_usd,
        "total_ask_usd": total_ask_usd,
        "distance_buckets": distance_results,
    }

    if save_dir:
        save_dir.mkdir(parents=True, exist_ok=True)
        raw_path = save_dir / f"{product}.raw.json"
        with raw_path.open("w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        summary_path = save_dir / f"{product}.summary.json"
        with summary_path.open("w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        summary["_saved_raw"] = str(raw_path)
        summary["_saved_summary"] = str(summary_path)

    return summary


def _print_report(s: dict) -> None:
    if not s.get("ok"):
        print(f"[FAIL] {s.get('product')}: {s.get('error')}")
        return

    print(f"\n{'='*70}")
    print(f"  Coinbase Exchange · {s['product']}")
    print(f"{'='*70}")
    print(f"  请求时间    : {s['ts']}")
    print(f"  API 时间戳  : {s['api_ts']}  (sequence={s['sequence']})")
    print(f"  延迟        : {s['latency_ms']:.1f} ms")
    print(f"  Payload     : {_format_bytes(s['payload_bytes'])}")
    print()
    print(f"  --- 当前价 ---")
    print(f"  Best bid    : ${s['best_bid']:,.2f}")
    print(f"  Best ask    : ${s['best_ask']:,.2f}")
    print(f"  Mid         : ${s['mid']:,.2f}")
    print(f"  Spread      : ${s['spread_usd']:.2f}  ({s['spread_bps']:.2f} bps)")
    print()
    print(f"  --- 档位规模 ---")
    print(f"  bids 档位   : {s['bid_count']:,}")
    print(f"  asks 档位   : {s['ask_count']:,}")
    print(f"  单档最大bid : {_format_usd(s['bid_max_single_usd'])} USD")
    print(f"  单档最大ask : {_format_usd(s['ask_max_single_usd'])} USD")
    print()
    print(f"  --- 前 50 档累计 ---")
    print(f"  Top-50 bid  : {_format_usd(s['top_50_bid_usd'])} USD")
    print(f"  Top-50 ask  : {_format_usd(s['top_50_ask_usd'])} USD")
    print()
    print(f"  --- 距离桶累计 USD（相对 mid）---")
    print(f"  {'距离':<8} {'BID 累计':<14} {'(档数)':<8} {'ASK 累计':<14} {'(档数)'}")
    for pct in DISTANCE_BUCKETS:
        r = s["distance_buckets"][pct]
        bid_str = _format_usd(r["bid_usd"])
        ask_str = _format_usd(r["ask_usd"])
        print(f"  ±{pct:<5}%  {bid_str:<14} ({r['bid_levels']:>3})    "
              f"{ask_str:<14} ({r['ask_levels']:>3})")
    print()
    print(f"  --- 全盘累计 ---")
    print(f"  Total bid   : {_format_usd(s['total_bid_usd'])} USD")
    print(f"  Total ask   : {_format_usd(s['total_ask_usd'])} USD")
    print(f"{'='*70}")


async def main():
    parser = argparse.ArgumentParser(description="Coinbase 现货 orderbook probe")
    parser.add_argument("--product", default="BTC-USD",
                        help="如 BTC-USD / ETH-USD / SOL-USD（默认 BTC-USD）")
    parser.add_argument("--products", default=None,
                        help="逗号分隔多个 product，如 BTC-USD,ETH-USD")
    parser.add_argument("--save", action="store_true",
                        help="保存 raw + summary 到 backend/scripts/coinbase_probe_samples/")
    parser.add_argument("--json", action="store_true",
                        help="只输出 JSON summary（用于脚本管道）")
    args = parser.parse_args()

    products = [p.strip() for p in (args.products.split(",") if args.products
                                    else [args.product]) if p.strip()]

    save_dir = None
    if args.save:
        ts_dir = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        save_dir = Path(__file__).resolve().parent / "coinbase_probe_samples" / ts_dir

    src = CoinbaseNativeSource()
    try:
        results = []
        for p in products:
            # Coinbase 限速 1s/req（rate_per_min=60），多 product 时自动间隔
            s = await probe_one(src, p, save_dir)
            results.append(s)
    finally:
        await src.close()

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for s in results:
            _print_report(s)
        if save_dir:
            print(f"\n[saved] {save_dir}")


if __name__ == "__main__":
    asyncio.run(main())
