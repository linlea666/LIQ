"""
回测离线脚本：验证 AI 交易计划和关键位信号的历史准确率。

用法:
    python3 -m scripts.backtest_signals [--window 24] [--coin BTC] [--source ai|kl|all]

读取 backend/data/ai_history.json 和 kl_history.json 中的结构化信号，
通过 Binance USD-M Futures 1H K 线回测信号触发 & TP/SL 命中情况。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sources.binance_futures import BinanceFuturesSource

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
BINANCE_BASE = "https://fstream.binance.com"


@dataclass
class Signal:
    """统一信号结构，同时覆盖 AI TradingPlanEntry 和 KeyLevelSignal。"""
    source: str             # "ai" | "kl"
    coin: str
    report_ts: int          # 报告时间戳（秒）
    price_at_report: float
    direction: str          # "long" | "short"
    entry: float
    stop_loss: float
    tp1: float
    tp2: Optional[float] = None
    rr: Optional[float] = None
    tier: str = ""          # "short" | "mid" | "long" (AI only)
    action: str = ""        # "snipe_long" etc (KL only)
    confidence: str = ""    # "A" | "B" | "C" (KL only)


@dataclass
class BacktestResult:
    signal: Signal
    entry_hit: bool = False
    entry_hit_ts: int = 0
    tp1_hit: bool = False
    tp1_hit_ts: int = 0
    tp2_hit: bool = False
    tp2_hit_ts: int = 0
    sl_hit: bool = False
    sl_hit_ts: int = 0
    outcome: str = "no_trigger"  # "tp1" | "tp2" | "sl" | "timeout" | "no_trigger"
    time_to_entry_h: float = 0
    time_to_outcome_h: float = 0


@dataclass
class Stats:
    total: int = 0
    triggered: int = 0
    tp1_wins: int = 0
    tp2_wins: int = 0
    sl_losses: int = 0
    timeouts: int = 0
    no_trigger: int = 0
    results: list[BacktestResult] = field(default_factory=list)


def load_ai_signals(coin: str) -> list[Signal]:
    """从 ai_history.json 提取带 trading_plan_entries 的信号。"""
    path = DATA_DIR / "ai_history.json"
    if not path.exists():
        logger.warning("AI history file not found: %s", path)
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    signals = []
    items = raw.get(coin, [])
    for report in items:
        ts = report.get("ts", 0)
        price = report.get("price_at_analysis", 0)
        entries = report.get("trading_plan_entries", [])
        if not entries or price <= 0:
            continue

        for e in entries:
            entry_val = e.get("entry")
            sl_val = e.get("stop_loss")
            tp1_val = e.get("tp1")
            direction = e.get("direction", "")
            if not entry_val or not sl_val or not tp1_val or not direction:
                continue
            signals.append(Signal(
                source="ai", coin=coin, report_ts=ts,
                price_at_report=price, direction=direction,
                entry=entry_val, stop_loss=sl_val, tp1=tp1_val,
                tp2=e.get("tp2"), rr=e.get("rr"),
                tier=e.get("tier", ""),
            ))

    logger.info("Loaded %d AI signals for %s", len(signals), coin)
    return signals


def load_kl_signals(coin: str) -> list[Signal]:
    """从 kl_history.json 提取带完整价格的关键位信号。"""
    path = DATA_DIR / "kl_history.json"
    if not path.exists():
        logger.warning("KL history file not found: %s", path)
        return []

    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    signals = []
    items = raw.get(coin, [])
    for snapshot in items:
        ts = snapshot.get("ts", 0)
        price = snapshot.get("current_price", 0)
        for sig in snapshot.get("signals", []):
            entry = sig.get("entry_price")
            sl = sig.get("stop_loss")
            tp1 = sig.get("tp1")
            action = sig.get("action", "")
            if not entry or not sl or not tp1:
                continue
            direction = "long" if "long" in action else "short" if "short" in action else ""
            if not direction:
                continue
            signals.append(Signal(
                source="kl", coin=coin, report_ts=ts,
                price_at_report=price, direction=direction,
                entry=entry, stop_loss=sl, tp1=tp1,
                tp2=sig.get("tp2"), rr=sig.get("rr_ratio"),
                action=action, confidence=sig.get("confidence", "C"),
            ))

    logger.info("Loaded %d KL signals for %s", len(signals), coin)
    return signals


async def fetch_klines_for_signal(
    bn: BinanceFuturesSource, symbol: str, start_ts: int, window_hours: int,
) -> list[dict]:
    """拉取信号时间戳后 window_hours 小时的 1H K 线。"""
    start_ms = start_ts * 1000
    end_ms = start_ms + window_hours * 3600 * 1000
    limit = min(window_hours + 1, 1500)

    raw = await bn.fetch_klines(
        symbol=symbol, interval="1h", limit=limit,
        start_time=start_ms, end_time=end_ms,
    )
    if not raw:
        return []

    candles = []
    for bar in raw:
        if len(bar) < 5:
            continue
        candles.append({
            "ts": int(bar[0]) // 1000,
            "o": float(bar[1]),
            "h": float(bar[2]),
            "l": float(bar[3]),
            "c": float(bar[4]),
        })
    return candles


def evaluate_signal(signal: Signal, candles: list[dict]) -> BacktestResult:
    """对单个信号进行回测评估。"""
    result = BacktestResult(signal=signal)

    if not candles:
        return result

    is_long = signal.direction == "long"
    entry_triggered = False

    for candle in candles:
        if not entry_triggered:
            hit = (candle["l"] <= signal.entry) if is_long else (candle["h"] >= signal.entry)
            if hit:
                entry_triggered = True
                result.entry_hit = True
                result.entry_hit_ts = candle["ts"]
                result.time_to_entry_h = (candle["ts"] - signal.report_ts) / 3600
        else:
            if is_long:
                tp1_hit = candle["h"] >= signal.tp1
                tp2_hit = signal.tp2 is not None and candle["h"] >= signal.tp2
                sl_hit = candle["l"] <= signal.stop_loss
            else:
                tp1_hit = candle["l"] <= signal.tp1
                tp2_hit = signal.tp2 is not None and candle["l"] <= signal.tp2
                sl_hit = candle["h"] >= signal.stop_loss

            if sl_hit and tp1_hit:
                if is_long:
                    result.outcome = "sl" if candle["l"] <= signal.stop_loss else "tp1"
                else:
                    result.outcome = "sl" if candle["h"] >= signal.stop_loss else "tp1"
                result.sl_hit = sl_hit
                result.tp1_hit = tp1_hit
                result.time_to_outcome_h = (candle["ts"] - result.entry_hit_ts) / 3600
                break

            if sl_hit:
                result.sl_hit = True
                result.sl_hit_ts = candle["ts"]
                result.outcome = "sl"
                result.time_to_outcome_h = (candle["ts"] - result.entry_hit_ts) / 3600
                break

            if tp1_hit:
                result.tp1_hit = True
                result.tp1_hit_ts = candle["ts"]
                if tp2_hit:
                    result.tp2_hit = True
                    result.tp2_hit_ts = candle["ts"]
                    result.outcome = "tp2"
                else:
                    result.outcome = "tp1"
                result.time_to_outcome_h = (candle["ts"] - result.entry_hit_ts) / 3600
                break

    if entry_triggered and result.outcome == "no_trigger":
        result.outcome = "timeout"

    return result


def aggregate_stats(results: list[BacktestResult]) -> Stats:
    stats = Stats(total=len(results))
    for r in results:
        if r.entry_hit:
            stats.triggered += 1
            if r.outcome == "tp1":
                stats.tp1_wins += 1
            elif r.outcome == "tp2":
                stats.tp2_wins += 1
            elif r.outcome == "sl":
                stats.sl_losses += 1
            elif r.outcome == "timeout":
                stats.timeouts += 1
        else:
            stats.no_trigger += 1
    stats.results = results
    return stats


def print_stats(stats: Stats, label: str) -> None:
    print(f"\n{'='*60}")
    print(f" {label}")
    print(f"{'='*60}")
    print(f" 总信号数:       {stats.total}")
    print(f" 触发入场:       {stats.triggered} ({stats.triggered/max(stats.total,1)*100:.1f}%)")
    if stats.triggered > 0:
        wins = stats.tp1_wins + stats.tp2_wins
        wr = wins / stats.triggered * 100
        print(f" TP1 命中:       {stats.tp1_wins}")
        print(f" TP2 命中:       {stats.tp2_wins}")
        print(f" SL 止损:        {stats.sl_losses}")
        print(f" 超时未平:       {stats.timeouts}")
        print(f" 胜率:           {wr:.1f}%")
    print(f" 未触发入场:     {stats.no_trigger}")

    if stats.triggered > 0:
        print(f"\n 按方向:")
        for direction in ("long", "short"):
            sub = [r for r in stats.results if r.signal.direction == direction and r.entry_hit]
            if not sub:
                continue
            wins = sum(1 for r in sub if r.outcome in ("tp1", "tp2"))
            print(f"   {direction}: {len(sub)} 触发, {wins} 盈利 ({wins/len(sub)*100:.1f}%)")

    tier_signals = [r for r in stats.results if r.signal.tier]
    if tier_signals:
        print(f"\n 按档位:")
        for tier in ("short", "mid", "long"):
            sub = [r for r in stats.results if r.signal.tier == tier and r.entry_hit]
            if not sub:
                continue
            wins = sum(1 for r in sub if r.outcome in ("tp1", "tp2"))
            tier_label = {"short": "短线", "mid": "中线", "long": "远线"}.get(tier, tier)
            print(f"   {tier_label}: {len(sub)} 触发, {wins} 盈利 ({wins/len(sub)*100:.1f}%)")

    conf_signals = [r for r in stats.results if r.signal.confidence]
    if conf_signals:
        print(f"\n 按置信度:")
        for conf in ("A", "B", "C"):
            sub = [r for r in stats.results if r.signal.confidence == conf and r.entry_hit]
            if not sub:
                continue
            wins = sum(1 for r in sub if r.outcome in ("tp1", "tp2"))
            print(f"   {conf}级: {len(sub)} 触发, {wins} 盈利 ({wins/len(sub)*100:.1f}%)")

    print()


async def run_backtest(coin: str, source: str, window_hours: int) -> None:
    symbol = f"{coin}USDT"
    bn = BinanceFuturesSource(base_url=BINANCE_BASE, timeout_sec=15)

    signals: list[Signal] = []
    if source in ("ai", "all"):
        signals.extend(load_ai_signals(coin))
    if source in ("kl", "all"):
        signals.extend(load_kl_signals(coin))

    if not signals:
        print(f"\n没有找到可回测的信号 (coin={coin}, source={source})")
        await bn.close()
        return

    signals.sort(key=lambda s: s.report_ts)
    print(f"\n共 {len(signals)} 个信号待回测 (window={window_hours}h)")

    results: list[BacktestResult] = []
    for i, sig in enumerate(signals):
        candles = await fetch_klines_for_signal(bn, symbol, sig.report_ts, window_hours)
        result = evaluate_signal(sig, candles)
        results.append(result)
        if (i + 1) % 10 == 0 or i == len(signals) - 1:
            logger.info("Progress: %d/%d", i + 1, len(signals))
        await asyncio.sleep(0.1)

    await bn.close()

    if source in ("ai", "all"):
        ai_results = [r for r in results if r.signal.source == "ai"]
        if ai_results:
            print_stats(aggregate_stats(ai_results), f"AI 交易计划回测 ({coin}, {window_hours}h)")

    if source in ("kl", "all"):
        kl_results = [r for r in results if r.signal.source == "kl"]
        if kl_results:
            print_stats(aggregate_stats(kl_results), f"关键位信号回测 ({coin}, {window_hours}h)")

    all_stats = aggregate_stats(results)
    if source == "all" and all_stats.total > 0:
        print_stats(all_stats, f"综合回测 ({coin}, {window_hours}h)")


def main():
    parser = argparse.ArgumentParser(description="回测 AI/关键位信号准确率")
    parser.add_argument("--coin", default="BTC", help="币种 (default: BTC)")
    parser.add_argument("--source", default="all", choices=["ai", "kl", "all"],
                        help="信号来源: ai=AI交易计划, kl=关键位, all=全部")
    parser.add_argument("--window", type=int, default=24,
                        help="评估窗口（小时，default=24）")
    args = parser.parse_args()

    asyncio.run(run_backtest(args.coin.upper(), args.source, args.window))


if __name__ == "__main__":
    main()
