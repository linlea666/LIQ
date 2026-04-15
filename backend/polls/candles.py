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
from processors.range_signal import calculate_range_signal
from processors.volume_profile import calc_volume_profile
from sources.coinglass import CoinglassSource

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


def parse_candles(raw: list) -> list[dict]:
    """将 Coinglass K 线数据统一为 {ts, o, h, l, c, vol} 格式。"""
    candles = []
    for item in raw:
        try:
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
    """基于关键位 V2 快照重新计算箱体信号。"""
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
        if state.candles_4h and len(state.candles_4h) >= 20:
            closes = [c.close for c in state.candles_4h]
            highs = [c.high for c in state.candles_4h]
            lows = [c.low for c in state.candles_4h]
            sq = detect_bb_squeeze(closes, highs, lows)
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


async def poll_indicators(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """从 Coinglass 获取所有技术指标：RSI/MACD/MA/EMA/ATR/BOLL"""
    exchange = coin.exchange_primary
    pair = coin.symbol_cg_pair

    def _last_item(data):
        if isinstance(data, list) and len(data) > 0:
            return data[-1]
        return None

    try:
        rsi_last = _last_item(await cg.fetch_rsi(exchange, pair, interval="1d", limit=2, window=14))
        if rsi_last:
            state.rsi_14 = float(rsi_last.get("rsi", rsi_last.get("value", 0)))
    except Exception:
        logger.debug("indicators: RSI parse failed", exc_info=True)

    try:
        macd_last = _last_item(await cg.fetch_macd(exchange, pair, interval="1d", limit=2))
        if macd_last:
            state.macd_data = {
                "macd": float(macd_last.get("macd", 0)),
                "signal": float(macd_last.get("signal", 0)),
                "histogram": float(macd_last.get("histogram", macd_last.get("hist", 0))),
                "above_zero": float(macd_last.get("macd", 0)) > 0,
            }
    except Exception:
        logger.debug("indicators: MACD parse failed", exc_info=True)

    try:
        ma60_last = _last_item(await cg.fetch_ma(exchange, pair, interval="1d", limit=2, window=60))
        if ma60_last:
            state.ma60_daily_cg = float(ma60_last.get("ma", ma60_last.get("value", 0)))
    except Exception:
        logger.debug("indicators: MA60 parse failed", exc_info=True)

    try:
        ma120_last = _last_item(await cg.fetch_ma(exchange, pair, interval="1d", limit=2, window=120))
        if ma120_last:
            state.ma120_daily_cg = float(ma120_last.get("ma", ma120_last.get("value", 0)))
    except Exception:
        logger.debug("indicators: MA120 parse failed", exc_info=True)

    try:
        atr_last = _last_item(await cg.fetch_atr(exchange, pair, interval="1h", limit=2, window=14))
        if atr_last:
            state.atr_cg = float(atr_last.get("atr", atr_last.get("value", 0)))
            state.atr = state.atr_cg
    except Exception:
        logger.debug("indicators: ATR parse failed", exc_info=True)

    try:
        boll_last = _last_item(await cg.fetch_boll(exchange, pair, interval="1d", limit=2))
        if boll_last:
            state.boll_data = {
                "upper": float(boll_last.get("upper", boll_last.get("upperBand", 0))),
                "middle": float(boll_last.get("middle", boll_last.get("middleBand", 0))),
                "lower": float(boll_last.get("lower", boll_last.get("lowerBand", 0))),
            }
    except Exception:
        logger.debug("indicators: BOLL parse failed", exc_info=True)

    try:
        ema20_last = _last_item(await cg.fetch_ema(exchange, pair, interval="1d", limit=2, window=20))
        if ema20_last:
            val = float(ema20_last.get("ema", ema20_last.get("value", 0)))
            state.ema20_cg = val
            state.ema_daily[20] = val
    except Exception:
        logger.debug("indicators: EMA20 parse failed", exc_info=True)

    # V2 新增：日线 EMA 50/100/200 + SMA 200（关键位多维共振）
    for period in (50, 100, 200):
        try:
            item = _last_item(await cg.fetch_ema(exchange, pair, interval="1d", limit=2, window=period))
            if item:
                state.ema_daily[period] = float(item.get("ema", item.get("value", 0)))
        except Exception:
            logger.debug("indicators: EMA%d parse failed", period, exc_info=True)

    try:
        sma200_last = _last_item(await cg.fetch_ma(exchange, pair, interval="1d", limit=2, window=200))
        if sma200_last:
            state.sma200_daily_cg = float(sma200_last.get("ma", sma200_last.get("value", 0)))
    except Exception:
        logger.debug("indicators: SMA200 parse failed", exc_info=True)

    # V2 新增：4H 布林带（突破蓄力检测）
    try:
        boll_4h = _last_item(await cg.fetch_boll(exchange, pair, interval="4h", limit=2))
        if boll_4h:
            state.boll_4h_data = {
                "upper": float(boll_4h.get("upper", boll_4h.get("upperBand", 0))),
                "middle": float(boll_4h.get("middle", boll_4h.get("middleBand", 0))),
                "lower": float(boll_4h.get("lower", boll_4h.get("lowerBand", 0))),
            }
    except Exception:
        logger.debug("indicators: BOLL 4H parse failed", exc_info=True)


async def poll_candles_4h(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取 4H K 线用于 Swing 检测 / 中期 Fibonacci / Pivot Points。"""
    data = await cg.fetch_price_history(
        coin.exchange_primary, coin.symbol_cg_pair,
        interval="4h", limit=200,
    )
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_4h = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]


async def poll_candles_1h(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取 1H K线用于 Volume Profile / ATR 计算。"""
    data = await cg.fetch_price_history(
        coin.exchange_primary, coin.symbol_cg_pair,
        interval="1h", limit=200,
    )
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
    vp = calc_volume_profile(candle_models, coin=coin.ccy)
    if vp:
        state.vp = vp

    if len(candle_models) >= 15 and state.atr_cg is None:
        from processors.volume_profile import calc_atr
        atr_val = calc_atr(candle_models)
        if atr_val > 0:
            state.atr = atr_val


async def poll_candles_daily(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取日线 K线用于 range_signal 箱体检测。"""
    data = await cg.fetch_price_history(
        coin.exchange_primary, coin.symbol_cg_pair,
        interval="1d", limit=150,
    )
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_daily = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]


async def poll_candles_weekly(cg: CoinglassSource, coin: CoinConfig, state: CoinState) -> None:
    """获取周线 K线用于 range_signal 周线 MA60。"""
    data = await cg.fetch_price_history(
        coin.exchange_primary, coin.symbol_cg_pair,
        interval="1w", limit=70,
    )
    if not data:
        return
    raw = parse_candles(data)
    if raw:
        state.candles_weekly = [
            CandleData(coin=coin.ccy, ts=c["ts"], o=c["o"], h=c["h"],
                       l=c["l"], c=c["c"], vol=c["vol"])
            for c in raw
        ]
