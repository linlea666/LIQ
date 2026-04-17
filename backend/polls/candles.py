"""K 线 / 指标领域：Coinglass K 线与技术指标轮询。

模块级 async 函数接收 cg 与 state，不依赖 Engine 实例。
range_signal 重算由 Engine 在适当时机调用 ``recompute_range_signal``。
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from config.settings import CoinConfig
from models.market import CandleData
from processors.ta_core import calc_atr as calc_atr_series
from processors.ta_core import calc_ema, calc_macd, calc_sma, last_valid
from processors.market_structure import detect_market_structure
from processors.range_signal import calculate_range_signal
from processors.volume_profile import calc_volume_profile
from sources.binance_futures import BinanceFuturesSource
from sources.coinglass import CoinglassSource

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


def parse_candles(raw: list) -> list[dict]:
    """将 Coinglass K 线数据统一为 {ts, o, h, l, c, vol} 格式。"""
    candles = []
    for item in raw:
        try:
            if isinstance(item, list) and len(item) >= 6:
                candles.append({
                    "ts": int(item[0]),
                    "o": float(item[1]),
                    "h": float(item[2]),
                    "l": float(item[3]),
                    "c": float(item[4]),
                    "vol": float(item[5]),
                })
                continue
            candles.append({
                "ts": int(item.get("t", item.get("ts", 0))),
                "o": float(item.get("o", item.get("open", 0))),
                "h": float(item.get("h", item.get("high", 0))),
                "l": float(item.get("l", item.get("low", 0))),
                "c": float(item.get("c", item.get("close", 0))),
                "vol": float(item.get("v", item.get("vol", item.get("volume", 0)))),
            })
        except (ValueError, TypeError):
            continue
    return candles


def recompute_range_signal(
    state: CoinState,
    btc_state: CoinState | None,
    settings_range: dict[str, Any] | None,
) -> None:
    """重新计算箱体信号（MA 骨架 + 微观区间）。"""
    if not state.ticker:
        return
    price = state.ticker.last
    if price <= 0:
        return

    cutoff = int(time.time()) - 3600
    sweep_above = sum(
        e.get("usd", 0) for e in state.liq_sweep_events
        if e.get("side") == "above" and e.get("ts", 0) > cutoff
    )
    sweep_below = sum(
        e.get("usd", 0) for e in state.liq_sweep_events
        if e.get("side") == "below" and e.get("ts", 0) > cutoff
    )

    cps = None
    if btc_state and btc_state.cycle_position and btc_state.cycle_position.cps is not None:
        cps = btc_state.cycle_position.cps

    bb_squeeze = False
    if state.boll_data and hasattr(state, "boll_4h_data"):
        from processors.ta_core import detect_bb_squeeze
        boll = state.boll_4h_data or state.boll_data
        if boll:
            sq = detect_bb_squeeze(
                boll.get("upper"),
                boll.get("lower"),
                boll.get("middle"),
                None,
                state.macd_data.get("histogram") if state.macd_data else None,
            )
            bb_squeeze = sq.is_squeeze

    oi_change_1h = state.oi.change_1h_pct if state.oi else 0
    funding_rate = None
    if state.funding:
        funding_rate = state.funding.oi_weighted_rate or state.funding.avg_rate

    ob_bid = ob_ask = 0.0
    if state.orderbook:
        ob_bid = state.orderbook.bid_total_usd or 0
        ob_ask = state.orderbook.ask_total_usd or 0

    state.range_signal = calculate_range_signal(
        kl_snapshot=state.key_level_snapshot_v2,
        current_price=price,
        atr=state.atr,
        candles_1d=state.candles_daily or None,
        candles_1w=state.candles_weekly or None,
        prev_range=state.range_signal,
        sweep_above_1h=sweep_above,
        sweep_below_1h=sweep_below,
        cps=cps,
        bb_squeeze=bb_squeeze,
        oi_change_1h=oi_change_1h,
        funding_rate=funding_rate,
        orderbook_bid_total=ob_bid,
        orderbook_ask_total=ob_ask,
        cfg=settings_range,
    )


def _calc_rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, len(values)):
        diff = values[i] - values[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def _calc_boll(values: list[float], window: int = 20, mult: float = 2.0) -> dict | None:
    if len(values) < window:
        return None
    segment = values[-window:]
    mid = sum(segment) / window
    var = sum((v - mid) ** 2 for v in segment) / window
    std = var ** 0.5
    return {"upper": mid + mult * std, "middle": mid, "lower": mid - mult * std}


async def poll_indicators(
    cg: CoinglassSource, coin: CoinConfig, state: CoinState, bn: BinanceFuturesSource | None = None,
) -> None:
    """本地计算技术指标：RSI/MACD/MA/EMA/ATR/BOLL（不消耗 Coinglass 配额）。"""
    del cg, bn

    if state.candles_daily and len(state.candles_daily) >= 30:
        closes_1d = [c.close for c in state.candles_daily]

        rsi = _calc_rsi(closes_1d, period=14)
        if rsi is not None:
            state.rsi_14 = float(rsi)

        macd = calc_macd(closes_1d, fast=12, slow=26, signal=9)
        state.macd_data = {
            "macd": float(last_valid(macd["macd_line"]) or 0),
            "signal": float(last_valid(macd["signal_line"]) or 0),
            "histogram": float(last_valid(macd["histogram"]) or 0),
            "above_zero": bool(macd["above_zero"]),
        }

        ma60 = last_valid(calc_sma(closes_1d, 60))
        if ma60 is not None:
            state.ma60_daily_cg = float(ma60)
        ma120 = last_valid(calc_sma(closes_1d, 120))
        if ma120 is not None:
            state.ma120_daily_cg = float(ma120)
        sma200 = last_valid(calc_sma(closes_1d, 200))
        if sma200 is not None:
            state.sma200_daily_cg = float(sma200)

        ema20 = last_valid(calc_ema(closes_1d, 20))
        if ema20 is not None:
            state.ema20_cg = float(ema20)
            state.ema_daily[20] = float(ema20)
        for period in (50, 100, 200):
            emav = last_valid(calc_ema(closes_1d, period))
            if emav is not None:
                state.ema_daily[period] = float(emav)

        boll = _calc_boll(closes_1d, window=20, mult=2.0)
        if boll:
            state.boll_data = boll

    if state.candles_1h and len(state.candles_1h) >= 20:
        highs = [c.high for c in state.candles_1h]
        lows = [c.low for c in state.candles_1h]
        closes = [c.close for c in state.candles_1h]
        atr = last_valid(calc_atr_series(highs, lows, closes, period=14))
        if atr is not None and atr > 0:
            state.atr_cg = float(atr)
            state.atr = float(atr)

    if state.candles_4h and len(state.candles_4h) >= 20:
        closes_4h = [c.close for c in state.candles_4h]
        boll_4h = _calc_boll(closes_4h, window=20, mult=2.0)
        if boll_4h:
            state.boll_4h_data = boll_4h

    if state.rsi_14 is not None and state.macd_data and state.boll_data:
        if "local_indicators_ready" not in state._log_once_keys:
            state._log_once_keys.add("local_indicators_ready")
            logger.info(
                "Binance技术指标-本地计算生效 | coin=%s rsi=%.2f macd_hist=%.4f atr=%.4f ma60=%.2f ma120=%.2f",
                coin.ccy,
                state.rsi_14 or 0.0,
                float(state.macd_data.get("histogram", 0.0)),
                state.atr or 0.0,
                state.ma60_daily_cg or 0.0,
                state.ma120_daily_cg or 0.0,
            )


async def poll_candles_4h(
    cg: CoinglassSource, coin: CoinConfig, state: CoinState, bn: BinanceFuturesSource | None = None,
) -> None:
    """获取 4H K 线用于 Swing 检测 / 中期 Fibonacci / Pivot Points。"""
    del cg
    if not bn:
        logger.warning("Binance K线源未注入 | coin=%s interval=4h", coin.ccy)
        return
    data = await bn.fetch_klines(coin.symbol_cg_pair, interval="4h", limit=200)
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_4h = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]
        if "binance_klines_4h_ready" not in state._log_once_keys:
            state._log_once_keys.add("binance_klines_4h_ready")
            logger.info("Binance K线生效 | coin=%s interval=4h bars=%d", coin.ccy, len(state.candles_4h))


async def poll_candles_15m(
    cg: CoinglassSource, coin: CoinConfig, state: CoinState, bn: BinanceFuturesSource | None = None,
) -> None:
    """获取 15m K 线用于日内 scalp 信号的影线确认（pin bar / engulfing）。

    注：Coinglass 配额紧张，优先走 Binance；取近 100 根（~25 小时）已足够覆盖
    scalp 信号的"最近 1-2 根"确认窗口，更长历史交给 1h/4h 线。
    """
    del cg
    if not bn:
        logger.warning("Binance K线源未注入 | coin=%s interval=15m", coin.ccy)
        return
    data = await bn.fetch_klines(coin.symbol_cg_pair, interval="15m", limit=100)
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_15m = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]
        if "binance_klines_15m_ready" not in state._log_once_keys:
            state._log_once_keys.add("binance_klines_15m_ready")
            logger.info("Binance K线生效 | coin=%s interval=15m bars=%d", coin.ccy, len(state.candles_15m))


async def poll_candles_1h(
    cg: CoinglassSource, coin: CoinConfig, state: CoinState, bn: BinanceFuturesSource | None = None,
) -> None:
    """获取 1H K线用于 Volume Profile / ATR 计算。"""
    del cg
    if not bn:
        logger.warning("Binance K线源未注入 | coin=%s interval=1h", coin.ccy)
        return
    data = await bn.fetch_klines(coin.symbol_cg_pair, interval="1h", limit=200)
    if not data:
        return
    raw = parse_candles(data)
    if not raw:
        return

    state.candle_prices = [c["c"] for c in raw]
    state.candle_ts = [c["ts"] for c in raw]

    candle_models = [
        CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                   l=c["l"], c=c["c"], vol=c["vol"])
        for c in raw
    ]
    state.candles_1h = candle_models
    if "binance_klines_1h_ready" not in state._log_once_keys:
        state._log_once_keys.add("binance_klines_1h_ready")
        logger.info("Binance K线生效 | coin=%s interval=1h bars=%d", coin.ccy, len(state.candles_1h))
    vp = calc_volume_profile(candle_models, coin=coin.ccy)
    if vp:
        state.vp = vp

    if len(candle_models) >= 15 and state.atr_cg is None:
        from processors.volume_profile import calc_atr
        atr_val = calc_atr(candle_models)
        if atr_val > 0:
            state.atr = atr_val

    # 市场结构识别（Commit 2：1h 数据刚刷新就算，零网络开销）
    recompute_market_structure(state)


_DIRECTION_ICON: dict[str, str] = {
    "bullish": "🟢上升",
    "bearish": "🔴下降",
    "ranging": "⚪震荡",
    "transitioning": "🟡过渡",
}


def recompute_market_structure(state: "CoinState") -> None:
    """基于 state.candles_1h 刷新 state.market_structure。

    纯算法无网络调用，由 poll_candles_1h 在 1h K 线更新后同步调用。
    变化即打 INFO（direction / last_event / operate_bias 任一变），
    不变打 DEBUG，异常 WARNING。
    """
    if not state.candles_1h:
        return
    try:
        ms = detect_market_structure(state.candles_1h, timeframe="1h")
    except Exception as exc:  # 防御：上游算法故障不能影响主流程
        logger.warning("市场结构计算失败 | coin=%s err=%s", state.coin, exc)
        return

    state.market_structure = ms

    curr = (ms.direction, ms.last_event, ms.operate_bias)
    if curr != state._prev_ms_summary:
        logger.info(
            "市场结构 | coin=%s 方向=%s 置信度=%.2f 事件=%s 偏置=%s 区间=[%.2f~%.2f]",
            state.coin,
            _DIRECTION_ICON.get(ms.direction, ms.direction),
            ms.confidence,
            ms.last_event or "-",
            ms.operate_bias,
            ms.structure_low,
            ms.structure_high,
        )
        state._prev_ms_summary = curr
    else:
        logger.debug(
            "市场结构无变化 | coin=%s direction=%s bias=%s",
            state.coin, ms.direction, ms.operate_bias,
        )


async def poll_candles_daily(
    cg: CoinglassSource, coin: CoinConfig, state: CoinState, bn: BinanceFuturesSource | None = None,
) -> None:
    """获取日线 K线用于 range_signal 箱体检测。"""
    del cg
    if not bn:
        logger.warning("Binance K线源未注入 | coin=%s interval=1d", coin.ccy)
        return
    data = await bn.fetch_klines(coin.symbol_cg_pair, interval="1d", limit=150)
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_daily = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]
        if "binance_klines_1d_ready" not in state._log_once_keys:
            state._log_once_keys.add("binance_klines_1d_ready")
            logger.info("Binance K线生效 | coin=%s interval=1d bars=%d", coin.ccy, len(state.candles_daily))


async def poll_candles_weekly(
    cg: CoinglassSource, coin: CoinConfig, state: CoinState, bn: BinanceFuturesSource | None = None,
) -> None:
    """获取周线 K线用于 range_signal 周线 MA60。"""
    del cg
    if not bn:
        logger.warning("Binance K线源未注入 | coin=%s interval=1w", coin.ccy)
        return
    data = await bn.fetch_klines(coin.symbol_cg_pair, interval="1w", limit=70)
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_weekly = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]
        if "binance_klines_1w_ready" not in state._log_once_keys:
            state._log_once_keys.add("binance_klines_1w_ready")
            logger.info("Binance K线生效 | coin=%s interval=1w bars=%d", coin.ccy, len(state.candles_weekly))
