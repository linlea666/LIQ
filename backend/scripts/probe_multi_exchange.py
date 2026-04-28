#!/usr/bin/env python3
"""多家交易所 OP 数据一致性 probe（L1/L2 限制调研专用）

目标：验证 Binance / OKX / Bybit / Coinbase 四家在以下 6 个 endpoint 的数据可用性，
为「方案 A：多家并行 large_orders + 5m 热力图 poll」提供决策依据。

测试矩阵（共 18 次调用）：
  合约（不测 Coinbase，无合约市场）：
    - /api/futures/orderbook/history             × Binance/OKX/Bybit
    - /api/futures/orderbook/large-limit-order   × Binance/OKX/Bybit
    - /api/futures/orderbook/large-limit-order-history × Binance/OKX/Bybit

  现货（不测 Bybit，深度小且本身定位为合约所）：
    - /api/spot/orderbook/history                × Binance/OKX/Coinbase
    - /api/spot/orderbook/large-limit-order      × Binance/OKX/Coinbase
    - /api/spot/orderbook/large-limit-order-history × Binance/OKX/Coinbase

输出指标（每家）：
  - http_ok: 调用是否成功（200 + data 非空）
  - bins/items_count: 数据点数
  - latency_ms
  - depth_usd_total: bids/asks 总额（仅 heatmap）
  - sample_fields: 关键字段示例
  - error: 失败原因（含 500 / 数据格式不对）

用法：
  cd backend
  python3 scripts/probe_multi_exchange.py
  python3 scripts/probe_multi_exchange.py --coin ETH
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
from typing import Any, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.coinglass import create_coinglass_source  # noqa: E402

logger = logging.getLogger("probe_multi_exchange")


# ──────────────────────────────────────────────────────────────
# 测试矩阵
# ──────────────────────────────────────────────────────────────

FUTURES_EXCHANGES = ["Binance", "OKX", "Bybit"]
SPOT_EXCHANGES = ["Binance", "OKX", "Coinbase"]

PROBES = [
    # (label, method_name, exchanges_list, kind, kwargs_template)
    ("合约 5m 热力图", "fetch_orderbook_heatmap", FUTURES_EXCHANGES, "heatmap_futures",
     {"interval": "5m", "limit": 12}),
    ("合约大单 holding", "fetch_large_orders", FUTURES_EXCHANGES, "large_futures",
     {}),
    ("合约大单历史", "fetch_large_orders_history", FUTURES_EXCHANGES, "large_history_futures",
     {}),
    ("现货 5m 热力图", "fetch_spot_orderbook_heatmap", SPOT_EXCHANGES, "heatmap_spot",
     {"interval": "5m", "limit": 12}),
    ("现货大单 holding", "fetch_spot_large_orders", SPOT_EXCHANGES, "large_spot",
     {}),
    ("现货大单历史", "fetch_spot_large_orders_history", SPOT_EXCHANGES, "large_history_spot",
     {}),
]


# ──────────────────────────────────────────────────────────────
# 数据指标提取
# ──────────────────────────────────────────────────────────────

def _safe_first(lst: Any, default=None) -> Any:
    if isinstance(lst, list) and lst:
        return lst[0]
    return default


def _summarize_heatmap(data: Any) -> dict:
    """heatmap 数据结构：list[ [ts, bids, asks] ]"""
    out = {"frames": 0, "latest_bids_bins": 0, "latest_asks_bins": 0,
           "latest_bids_usd": 0.0, "latest_asks_usd": 0.0,
           "ts_range_min": None, "sample_keys": None}
    if not isinstance(data, list):
        out["error"] = f"非 list（{type(data).__name__}）"
        return out
    out["frames"] = len(data)
    if not data:
        return out
    latest = data[-1]
    if not (isinstance(latest, list) and len(latest) >= 3):
        out["error"] = f"latest frame 不是 [ts, bids, asks]: {type(latest).__name__}"
        return out
    ts = latest[0]
    bids = latest[1] if isinstance(latest[1], list) else []
    asks = latest[2] if isinstance(latest[2], list) else []
    out["latest_bids_bins"] = len(bids)
    out["latest_asks_bins"] = len(asks)
    out["sample_keys"] = "[ts, bids[[price,qty]], asks[[price,qty]]]"
    try:
        out["latest_bids_usd"] = round(sum(
            float(b[0]) * float(b[1]) for b in bids if isinstance(b, list) and len(b) >= 2
        ) / 1e6, 2)
        out["latest_asks_usd"] = round(sum(
            float(a[0]) * float(a[1]) for a in asks if isinstance(a, list) and len(a) >= 2
        ) / 1e6, 2)
    except (ValueError, TypeError):
        pass
    if isinstance(ts, (int, float)):
        ts_sec = int(ts) // 1000 if int(ts) > 10_000_000_000 else int(ts)
        out["ts_range_min"] = round((time.time() - ts_sec) / 60, 1)
    return out


def _summarize_large_orders(data: Any) -> dict:
    """large_orders 数据结构：list[dict]，每 dict 含价位、size、status、exchange_name 等"""
    out = {"items": 0, "holding": 0, "ended": 0, "exchange_names": [],
           "size_usd_avg": 0.0, "size_usd_max": 0.0,
           "sample_fields": [], "first_item_keys": []}
    if not isinstance(data, list):
        out["error"] = f"非 list（{type(data).__name__}）"
        return out
    out["items"] = len(data)
    if not data:
        return out

    first = _safe_first(data, {})
    if isinstance(first, dict):
        out["first_item_keys"] = list(first.keys())[:20]
        out["sample_fields"] = {
            k: (str(v)[:60] if not isinstance(v, (int, float)) else v)
            for k, v in list(first.items())[:8]
        }

    holding_count = 0
    ended_count = 0
    sizes = []
    exchanges = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        ex = item.get("exchange_name") or item.get("exchange")
        if ex:
            exchanges.add(ex)
        status = (item.get("order_status") or item.get("status") or "").lower()
        if "hold" in status:
            holding_count += 1
        elif "end" in status or "filled" in status or "cancel" in status:
            ended_count += 1
        usd = item.get("size_usd") or item.get("usd_value")
        if isinstance(usd, (int, float)):
            sizes.append(float(usd))
    out["holding"] = holding_count
    out["ended"] = ended_count
    out["exchange_names"] = sorted(exchanges)[:5]
    if sizes:
        out["size_usd_avg"] = round(sum(sizes) / len(sizes) / 1e6, 2)
        out["size_usd_max"] = round(max(sizes) / 1e6, 2)
    return out


def _summarize(kind: str, data: Any) -> dict:
    if kind.startswith("heatmap"):
        return _summarize_heatmap(data)
    return _summarize_large_orders(data)


# ──────────────────────────────────────────────────────────────
# 单次 probe
# ──────────────────────────────────────────────────────────────

async def probe_one(cg, label: str, method_name: str, exchange: str,
                    kind: str, kwargs_template: dict, coin: str,
                    max_retries: int = 1) -> dict:
    """探测单家 × 单 endpoint。

    设计约束：Coinglass rate limit 10 calls/min（≥ 6s/call）。
    本函数仅做单次调用 + 1 次失败重试（间隔 30s），主流程负责调用之间 sleep 7s。
    """
    cg._cache.clear()  # 确保 fresh

    method = getattr(cg, method_name, None)
    if method is None:
        return {"label": label, "exchange": exchange, "ok": False,
                "error": f"method {method_name} not found"}

    kwargs = dict(kwargs_template)
    kwargs["exchange"] = exchange
    kwargs["symbol"] = f"{coin}USDT"

    started = time.time()
    err: Optional[str] = None
    data: Any = None
    attempt = 0
    while True:
        try:
            data = await method(**kwargs)
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            break
        if data is not None:
            break
        attempt += 1
        if attempt > max_retries:
            break
        await asyncio.sleep(30)  # 失败后 30s 大间隔再试（避开 rate limit）
    elapsed_ms = round((time.time() - started) * 1000, 1)

    summary = _summarize(kind, data) if data is not None and err is None else {}
    return {
        "label": label,
        "exchange": exchange,
        "method": method_name,
        "kind": kind,
        "ok": err is None and data is not None and (
            (isinstance(data, list) and len(data) > 0)
            or isinstance(data, dict)
        ),
        "elapsed_ms": elapsed_ms,
        "attempts": attempt + 1,
        "error": err,
        "data_is_none": data is None,
        "summary": summary,
    }


# ──────────────────────────────────────────────────────────────
# 主流程
# ──────────────────────────────────────────────────────────────

async def main(coin: str, out_dir: Path) -> None:
    cg = create_coinglass_source()
    if not cg:
        print("ERROR: 创建 CoinglassSource 失败（key 未配置？）")
        return

    out_dir.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*78}")
    print(f"  多家交易所 OP 数据 probe | coin={coin} | out={out_dir}")
    print(f"{'='*78}\n")

    all_results: list[dict] = []
    total_probes = sum(len(exchanges) for _, _, exchanges, _, _ in PROBES)
    progress = 0
    INTER_CALL_SLEEP = 7.0  # Coinglass 10/min 限制 → 严格 ≥ 6s/call
    for label, method_name, exchanges, kind, kwargs_template in PROBES:
        print(f"\n┌─ {label} ({method_name})")
        for ex in exchanges:
            progress += 1
            print(f"│  [{progress}/{total_probes}] {ex} ...", end=" ", flush=True)
            r = await probe_one(cg, label, method_name, ex, kind, kwargs_template, coin)
            all_results.append(r)
            if r["ok"]:
                s = r["summary"]
                if kind.startswith("heatmap"):
                    print(f"✓ frames={s.get('frames',0)} "
                          f"bids={s.get('latest_bids_bins',0)} "
                          f"asks={s.get('latest_asks_bins',0)} "
                          f"bid$={s.get('latest_bids_usd',0):.1f}M "
                          f"ask$={s.get('latest_asks_usd',0):.1f}M "
                          f"({r['elapsed_ms']:.0f}ms)")
                else:
                    print(f"✓ items={s.get('items',0)} "
                          f"holding={s.get('holding',0)} "
                          f"avg=${s.get('size_usd_avg',0):.2f}M "
                          f"max=${s.get('size_usd_max',0):.2f}M "
                          f"({r['elapsed_ms']:.0f}ms)")
            elif r.get("data_is_none"):
                print(f"✗ data=None ({r['elapsed_ms']:.0f}ms attempts={r.get('attempts',1)})")
            else:
                print(f"✗ ({r['elapsed_ms']:.0f}ms) | err={r.get('error','无返回')}")
            # 严格遵守 Coinglass 10 calls/min（除最后一次外，每次后 sleep 7s）
            if progress < total_probes:
                await asyncio.sleep(INTER_CALL_SLEEP)

    # 写汇总
    summary_path = out_dir / f"{coin}_multi_exchange_summary.json"
    summary_path.write_text(json.dumps(all_results, indent=2, ensure_ascii=False),
                            encoding="utf-8")

    # 打印决策表
    print(f"\n{'='*78}")
    print(f"  接入决策表（{coin}）")
    print(f"{'='*78}")
    print(f"\n{'Endpoint':32} | {'Binance':>10} | {'OKX':>10} | {'Bybit':>10} | {'Coinbase':>10}")
    print(f"{'-'*32} | {'-'*10} | {'-'*10} | {'-'*10} | {'-'*10}")

    by_label: dict[str, dict[str, dict]] = {}
    for r in all_results:
        by_label.setdefault(r["label"], {})[r["exchange"]] = r

    for label in [p[0] for p in PROBES]:
        row = by_label.get(label, {})
        cells = []
        for ex in ["Binance", "OKX", "Bybit", "Coinbase"]:
            r = row.get(ex)
            if not r:
                cells.append("       —")
                continue
            if not r["ok"]:
                cells.append("       ✗")
                continue
            s = r["summary"]
            if "heatmap" in r["kind"]:
                cells.append(f"{s.get('latest_bids_bins',0):>4}+{s.get('latest_asks_bins',0):<4}")
            else:
                cells.append(f"items={s.get('items',0):>4}")
        print(f"{label:32} | {cells[0]:>10} | {cells[1]:>10} | {cells[2]:>10} | {cells[3]:>10}")

    print(f"\n汇总文件：{summary_path}")
    await cg.close()


def cli():
    p = argparse.ArgumentParser(description="多家交易所 OP 数据 probe")
    p.add_argument("--coin", default="BTC")
    p.add_argument("--out-dir", default=None,
                   help="输出目录（默认 backend/scripts/multi_exchange_probe/{ts}）")
    args = p.parse_args()

    if args.out_dir:
        out_dir = Path(args.out_dir)
    else:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%SZ")
        out_dir = (Path(__file__).resolve().parent
                   / "multi_exchange_probe" / ts)

    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main(args.coin, out_dir))


if __name__ == "__main__":
    cli()
