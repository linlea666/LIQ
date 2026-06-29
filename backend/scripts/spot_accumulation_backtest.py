#!/usr/bin/env python3
"""从JSON日级样本运行BTC现货抄底慢周期回测。

输入格式：[{"ts": 1700000000, "price": 50000, "valuation_score": 72}, ...]
仅评估估值层；ETF、订单簿和Footprint必须走前向影子验证。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processors.spot_accumulation_backtest import BacktestPoint, run_backtest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="日级JSON文件")
    parser.add_argument(
        "--capital-usdt",
        type=float,
        default=20_000.0,
        help="抄底总资金，默认20000；回测核心预算按65%%派生",
    )
    args = parser.parse_args()
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    points = [BacktestPoint(**item) for item in raw]
    results = run_backtest(points, total_capital_usdt=args.capital_usdt)
    payload = []
    for item in results:
        payload.append({
            "peak_ts": item.episode.peak_ts,
            "peak_price": item.episode.peak_price,
            "start_ts": item.episode.start_ts,
            "end_ts": item.episode.end_ts,
            "max_drawdown_pct": round(item.episode.max_drawdown_pct, 2),
            "strategies": {
                name: {
                    "invested_usdt": result.invested_usdt,
                    "btc_acquired": result.btc_acquired,
                    "average_cost": result.average_cost,
                    "ending_cash_usdt": result.ending_cash_usdt,
                    "max_drawdown_from_cost_pct": result.max_drawdown_from_cost_pct,
                    "trade_count": len(result.trades),
                }
                for name, result in item.strategies.items()
            },
        })
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
