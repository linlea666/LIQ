#!/usr/bin/env python3
"""从JSON日级样本运行BTC现货抄底慢周期回测。

输入可直接提供valuation_score，也可提供价格、MVRV、Ahr999、200WMA等慢周期原始序列。
仅评估估值层；ETF、订单簿和Footprint必须走前向影子验证。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from models.spot_accumulation import SpotAccumulationConfig  # noqa: E402
from processors.spot_accumulation_backtest import (  # noqa: E402
    BacktestPoint,
    build_valuation_points,
    run_backtest,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", help="日级JSON文件")
    parser.add_argument(
        "--capital-usdt",
        type=float,
        default=None,
        help="兼容覆盖项；默认读取策略配置中的总资金",
    )
    parser.add_argument(
        "--config",
        default="data/spot_accumulation/config.json",
        help="v3策略配置；文件不存在时使用模型默认配置",
    )
    parser.add_argument("--fee-bps", type=float, default=10.0)
    parser.add_argument("--slippage-bps", type=float, default=5.0)
    args = parser.parse_args()
    raw = json.loads(Path(args.input).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("输入必须是日级JSON数组")
    points = (
        [BacktestPoint(**item) for item in raw]
        if raw and all(isinstance(item, dict) and "valuation_score" in item for item in raw)
        else build_valuation_points(raw)
    )
    config_path = Path(args.config)
    config = (
        SpotAccumulationConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
        if config_path.exists() else SpotAccumulationConfig()
    )
    results = run_backtest(
        points,
        total_capital_usdt=args.capital_usdt,
        config=config,
        fee_bps=args.fee_bps,
        slippage_bps=args.slippage_bps,
    )
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
                    "fees_usdt": result.fees_usdt,
                    "slippage_usdt": result.slippage_usdt,
                    "trade_count": len(result.trades),
                }
                for name, result in item.strategies.items()
            },
        })
    report = {
        "label": "V层历史回测",
        "capability": "valuation_layer_historical_backtest",
        "disclaimer": "不包含M/A历史验证；M/A仅使用线上影子快照做前向验证。",
        "policy_version": config.policy_version,
        "capital_usdt": args.capital_usdt or config.initial_capital_usdt,
        "fee_bps": args.fee_bps,
        "slippage_bps": args.slippage_bps,
        "episodes": payload,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
