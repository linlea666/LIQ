"""
宏观 / 情报领域：ETF、Coinbase 溢价、稳定币市值、巨鲸、期权、新闻、宏观指数、链上周期等轮询。
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from config.settings import CoinConfig
from models.flow import ETFFlowData, ETFFlowDay, MarketIndexData
from models.macro import NewsData
from models.options import OptionInfoData, OptionMaxPainData
from models.whale import WhaleData
from processors.cycle import calculate_cycle_position
from sources.coinglass import CoinglassSource

logger = logging.getLogger(__name__)


async def poll_etf_flow(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取 ETF 资金流"""
    for asset, fetch_fn in [
        ("BTC", cg.fetch_btc_etf_flow_history),
        ("ETH", cg.fetch_eth_etf_flow_history),
    ]:
        try:
            data = await fetch_fn()
            if not data or not isinstance(data, list):
                logger.warning("ETF %s: no data or not list (type=%s)", asset, type(data).__name__)
                continue

            recent = data[-5:] if len(data) >= 5 else data
            days = []
            net_3d = 0.0
            for item in recent:
                try:
                    total_net = float(item.get("flow_usd",
                                    item.get("total_netflow", item.get("totalNetflow", item.get("netflow", 0)))))
                    ts_ms = item.get("timestamp", item.get("time", 0))
                    date_str = datetime.utcfromtimestamp(int(ts_ms) / 1000).strftime("%Y-%m-%d") if ts_ms else ""
                    days.append(ETFFlowDay(
                        date=date_str,
                        total_net=total_net,
                    ))
                    net_3d += total_net
                except (ValueError, KeyError):
                    continue

            trend = "inflow" if net_3d > 0 else "outflow" if net_3d < 0 else "mixed"
            etf = ETFFlowData(
                ts=int(time.time()), asset=asset,
                recent_days=days, net_3d=net_3d, trend=trend,
            )
            logger.info("ETF %s parsed | days=%d net_3d=%.0f trend=%s",
                        asset, len(days), net_3d, trend)

            for ccy in supported_coins:
                if asset == "BTC" or ccy == asset:
                    states[ccy].etf_flow = etf
        except Exception:
            logger.warning("etf: %s flow failed", asset, exc_info=True)


async def poll_coinbase_premium(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取 Coinbase 溢价指数（机构买盘方向信号）"""
    from models.macro import CoinbasePremiumData, CoinbasePremiumPoint
    data = await cg.fetch_coinbase_premium(symbol="BTC", interval="5m", limit=12)
    if not data or not isinstance(data, list):
        return
    try:
        history = []
        for item in data:
            history.append(CoinbasePremiumPoint(
                ts=int(item.get("time", 0)),
                premium=float(item.get("premium_rate", item.get("premium", 0))),
                price=float(item.get("coinbase_price", 0)),
            ))
        latest = data[-1]
        cb_data = CoinbasePremiumData(
            ts=int(latest.get("time", 0)),
            current_premium=float(latest.get("premium_rate", latest.get("premium", 0))),
            history=history,
        )
        for ccy in supported_coins:
            states[ccy].coinbase_premium = cb_data
    except (ValueError, KeyError, IndexError):
        logger.debug("coinbase_premium parse failed", exc_info=True)


async def poll_stablecoin_mcap(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取稳定币市值变化（场外资金入/出场领先指标）"""
    from models.macro import StablecoinMcapData, StablecoinMcapPoint
    data = await cg.fetch_stablecoin_mcap(limit=7)
    if not data or not isinstance(data, dict):
        return
    try:
        data_list = data.get("data_list", [])
        time_list = data.get("time_list", [])
        if not data_list or not time_list:
            return

        n = min(len(data_list), len(time_list))
        tail = 10
        start_idx = max(0, n - tail)

        history = []
        for i in range(start_idx, n):
            item = data_list[i]
            raw_ts = time_list[i]
            ts = int(raw_ts) // 1000 if int(raw_ts) > 1e12 else int(raw_ts)
            usdt = float(item.get("USDT", 0))
            usdc = float(item.get("USDC", 0))
            total = sum(float(v) for v in item.values() if isinstance(v, (int, float)))
            if total < 1e9:
                continue
            history.append(StablecoinMcapPoint(ts=ts, total_mcap=total, usdt_mcap=usdt, usdc_mcap=usdc))
        latest = history[-1] if history else None
        if latest:
            pct = 0.0
            if len(history) >= 2 and history[0].total_mcap > 1e9:
                pct = (latest.total_mcap - history[0].total_mcap) / history[0].total_mcap * 100
                if abs(pct) > 50:
                    logger.warning("stablecoin 7d change abnormal: %.2f%%, clamped", pct)
                    pct = max(-50, min(50, pct))
            sc_data = StablecoinMcapData(
                ts=latest.ts,
                current_total=latest.total_mcap,
                history=history,
            )
            for ccy in supported_coins:
                states[ccy].stablecoin_mcap = sc_data
    except (ValueError, KeyError, IndexError):
        logger.debug("stablecoin_mcap parse failed", exc_info=True)


async def poll_macro_index(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取宏观市场指标"""
    mi = MarketIndexData(ts=int(time.time()))

    try:
        fg_data = await cg.fetch_fear_greed()
        if fg_data:
            if isinstance(fg_data, dict):
                data_list = fg_data.get("data_list", [])
                if data_list:
                    mi.fear_greed = float(data_list[-1])
            elif isinstance(fg_data, list) and fg_data:
                mi.fear_greed = float(fg_data[-1].get("value", 0))
    except Exception:
        logger.debug("macro: fear_greed parse failed", exc_info=True)

    try:
        dom_data = await cg.fetch_btc_dominance()
        if dom_data and isinstance(dom_data, list) and dom_data:
            last = dom_data[-1]
            mi.btc_dominance = float(last.get("bitcoin_dominance", last.get("dominance", 0)))
    except Exception:
        logger.debug("macro: btc_dominance parse failed", exc_info=True)

    for ccy in supported_coins:
        states[ccy].market_index = mi


async def poll_onchain_cycle(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """从 Coinglass 获取链上周期数据 → 计算 CPS"""
    from models.flow import OnchainCycleData

    raw = OnchainCycleData(ts=int(time.time()))

    ahr_data = await cg.fetch_ahr999()
    if ahr_data and len(ahr_data) > 0:
        last_ahr = ahr_data[-1]
        raw.ahr999 = float(last_ahr.get("ahr999_value",
                           last_ahr.get("value",
                           last_ahr.get("ahr999", 0))))

    pi_data = await cg.fetch_pi_cycle()
    if pi_data and len(pi_data) > 0:
        last = pi_data[-1]
        raw.pi_111dma_x2 = float(last.get("ma_110",
                            last.get("sma111X2", last.get("ma111x2", 0))))
        raw.pi_350dma = float(last.get("ma_350_mu_2",
                         last.get("sma350", last.get("ma350", 0))))

    ma_200w_data = await cg.fetch_200w_ma_heatmap()
    if ma_200w_data and len(ma_200w_data) > 0:
        last = ma_200w_data[-1]
        raw.sma_200w = float(last.get("moving_average_1440",
                        last.get("sma", last.get("ma200w", 0))))

    sth_data = await cg.fetch_sth_realized_price()
    if sth_data and len(sth_data) > 0:
        last_sth = sth_data[-1]
        raw.sth_cost_1d = float(last_sth.get("sth_realized_price",
                           last_sth.get("value", last_sth.get("price", 0))))

    btc_state = states.get("BTC")
    btc_price = btc_state.ticker.last if btc_state and btc_state.ticker else 0
    if btc_state and btc_state.market_index:
        mi_mvrv = btc_state.market_index.btc_mvrv
        if mi_mvrv is not None and mi_mvrv > 0:
            raw.mvrv_ratio = mi_mvrv

    # ── BTC 日线收盘价（供 cycle.py 计算 Wilder RSI(14)；需 ≥15 根）──
    if btc_state and btc_state.candles_daily:
        raw.btc_daily_prices = [c.close for c in btc_state.candles_daily[-60:]]

    cycle_pos = calculate_cycle_position(raw, btc_price) if btc_price > 0 else None

    for ccy in supported_coins:
        states[ccy].cycle_position = cycle_pos


async def poll_whale_data(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取巨鲸数据"""
    from models.whale import HyperliquidWhaleAlert, WhaleTransfer

    alerts_data = await cg.fetch_hyperliquid_whale_alert()
    alerts = []
    if alerts_data and isinstance(alerts_data, list):
        for item in alerts_data:
            try:
                pos_size = float(item.get("position_size", item.get("sizeUsd", 0)))
                side = "short" if pos_size < 0 else "long"
                action_code = item.get("position_action", item.get("action", ""))
                action_map = {1: "open", 2: "close", 3: "increase", 4: "decrease"}
                action = action_map.get(action_code, str(action_code)) if isinstance(action_code, int) else str(action_code)
                alerts.append(HyperliquidWhaleAlert(
                    ts=int(item.get("create_time", item.get("time", 0))),
                    symbol=item.get("symbol", ""),
                    side=side,
                    size_usd=float(item.get("position_value_usd", abs(pos_size))),
                    entry_price=float(item.get("entry_price", item.get("entryPrice", 0))),
                    address=item.get("user", item.get("address", "")),
                    action=action,
                ))
            except (ValueError, KeyError):
                continue

    transfers_data = await cg.fetch_whale_transfer()
    transfers = []
    if transfers_data:
        for item in transfers_data:
            try:
                transfers.append(WhaleTransfer(
                    ts=int(item.get("time", item.get("ts", 0))),
                    symbol=item.get("symbol", ""),
                    amount=float(item.get("amount", 0)),
                    amount_usd=float(item.get("amountUsd", item.get("valueUsd", 0))),
                    from_label=item.get("fromLabel", item.get("from", "")),
                    to_label=item.get("toLabel", item.get("to", "")),
                    tx_hash=item.get("txHash", ""),
                    blockchain=item.get("blockchain", item.get("chain", "")),
                ))
            except (ValueError, KeyError):
                continue

    from models.whale import HyperliquidWhalePosition
    hl_positions = []
    positions_data = await cg.fetch_hyperliquid_whale_position()
    if positions_data and isinstance(positions_data, list):
        for item in positions_data:
            try:
                pos_size = float(item.get("position_size", 0))
                side = "short" if pos_size < 0 else "long"
                hl_positions.append(HyperliquidWhalePosition(
                    address=item.get("user", ""),
                    symbol=item.get("symbol", ""),
                    side=side,
                    size_usd=float(item.get("position_value_usd", 0)),
                    entry_price=float(item.get("entry_price", 0)),
                    unrealized_pnl=float(item.get("unrealized_pnl", 0)),
                    leverage=float(item.get("leverage", 0)),
                ))
            except (ValueError, KeyError):
                continue

    whale = WhaleData(
        ts=int(time.time()),
        hl_alerts=alerts,
        hl_positions=hl_positions,
        transfers=transfers,
    )

    for ccy in supported_coins:
        states[ccy].whale_data = whale


async def poll_options(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取期权数据"""
    from models.options import OptionMaxPainExpiry
    for symbol in ("BTC", "ETH"):
        try:
            max_pain = await cg.fetch_option_max_pain(symbol)
            if max_pain and isinstance(max_pain, list):
                expiries = []
                for item in max_pain:
                    try:
                        mp = item.get("max_pain_price", item.get("maxPain", item.get("price", 0)))
                        c_oi = item.get("call_open_interest", item.get("callOI", 0))
                        p_oi = item.get("put_open_interest", item.get("putOI", 0))
                        expiries.append(OptionMaxPainExpiry(
                            expiry_date=item.get("date", item.get("expiryDate", "")),
                            max_pain_price=float(mp),
                            call_oi=float(c_oi),
                            put_oi=float(p_oi),
                        ))
                    except (ValueError, KeyError):
                        continue

                nearest = expiries[0] if expiries else None
                if symbol in states:
                    states[symbol].option_max_pain = OptionMaxPainData(
                        symbol=symbol, ts=int(time.time()),
                        expiries=expiries,
                        nearest_max_pain=nearest.max_pain_price if nearest else None,
                        nearest_expiry=nearest.expiry_date if nearest else "",
                    )
        except Exception:
            logger.warning("options: max_pain %s failed", symbol, exc_info=True)

        try:
            info = await cg.fetch_option_info(symbol)
            if info and symbol in states:
                agg = info
                if isinstance(info, list):
                    agg = next((x for x in info if x.get("exchange_name") == "All"), info[0] if info else None)
                if agg and isinstance(agg, dict):
                    state = states[symbol]
                    state.option_info = OptionInfoData(
                        symbol=symbol, ts=int(time.time()),
                        total_oi_usd=float(agg.get("open_interest_usd", agg.get("totalOI", 0))),
                        total_vol_24h_usd=float(agg.get("volume_usd_24h", agg.get("totalVol24h", 0))),
                        put_call_oi_ratio=float(agg.get("putCallOIRatio", 0)),
                        put_call_vol_ratio=float(agg.get("putCallVolRatio", 0)),
                    )
        except Exception:
            logger.debug("options: info %s failed", symbol, exc_info=True)


async def poll_news(
    cg: CoinglassSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """获取新闻"""
    from models.macro import NewsArticle
    data = await cg.fetch_news(language="zh", per_page=10)
    if not data:
        return

    articles = []
    for item in data:
        try:
            articles.append(NewsArticle(
                ts=int(item.get("time", item.get("ts", 0))),
                title=item.get("title", ""),
                content=item.get("content", item.get("desc", "")),
                source=item.get("source", ""),
                url=item.get("url", ""),
            ))
        except (ValueError, KeyError):
            continue

    news = NewsData(ts=int(time.time()), articles=articles)
    for ccy in supported_coins:
        states[ccy].news = news


def calc_whale_direction(wd: WhaleData | None) -> str:
    if not wd:
        return ""
    in_count = sum(1 for t in wd.transfers if "exchange" in (t.to_label or "").lower())
    out_count = sum(1 for t in wd.transfers if "exchange" in (t.from_label or "").lower())
    if in_count > out_count + 1:
        return "充入交易所(看跌)"
    elif out_count > in_count + 1:
        return "提出交易所(看涨)"
    return "中性"


def calc_whale_transfer_flows(wd: WhaleData | None) -> dict:
    """按 USD 金额聚合巨鲸转账流向，解决 AI 反馈"仅有笔数缺金额"的问题。

    返回字段：
    - inflow_usd: 流入交易所总额（to_label 包含 exchange）
    - outflow_usd: 流出交易所总额（from_label 包含 exchange）
    - net_usd: inflow - outflow（正 = 净充入交易所 = 偏空）
    - top_transfers: 前 3 大转账详情（含方向/金额/交易所标签）
    """
    empty = {
        "inflow_usd": 0.0,
        "outflow_usd": 0.0,
        "net_usd": 0.0,
        "top_transfers": [],
    }
    if not wd or not wd.transfers:
        return empty

    inflow_usd = 0.0
    outflow_usd = 0.0
    enriched: list[dict] = []
    for t in wd.transfers:
        to_label = (t.to_label or "").lower()
        from_label = (t.from_label or "").lower()
        to_is_ex = "exchange" in to_label
        from_is_ex = "exchange" in from_label
        if to_is_ex and not from_is_ex:
            direction = "inflow"
            inflow_usd += t.amount_usd
        elif from_is_ex and not to_is_ex:
            direction = "outflow"
            outflow_usd += t.amount_usd
        elif to_is_ex and from_is_ex:
            direction = "ex_to_ex"
        else:
            direction = "wallet_to_wallet"
        enriched.append({
            "direction": direction,
            "amount_usd": t.amount_usd,
            "from_label": t.from_label or "wallet",
            "to_label": t.to_label or "wallet",
        })

    enriched.sort(key=lambda x: x["amount_usd"], reverse=True)
    return {
        "inflow_usd": round(inflow_usd, 2),
        "outflow_usd": round(outflow_usd, 2),
        "net_usd": round(inflow_usd - outflow_usd, 2),
        "top_transfers": enriched[:3],
    }


def build_hl_positions(wd: WhaleData | None, ccy: str) -> list[dict]:
    if not wd or not wd.hl_positions:
        return []
    relevant = [p for p in wd.hl_positions if p.symbol.upper() == ccy]
    relevant.sort(key=lambda x: x.size_usd, reverse=True)
    return [
        {"side": p.side, "size_usd": p.size_usd,
         "entry": p.entry_price, "pnl": p.unrealized_pnl, "leverage": p.leverage}
        for p in relevant[:10]
    ]


def calc_cb_premium_trend(cb: Any) -> str:
    """premium_rate 单位为小数 (0.001 = 0.1%)"""
    if not cb or not cb.history or len(cb.history) < 2:
        return ""
    recent = [p.premium for p in cb.history[-6:]]
    avg = sum(recent) / len(recent)
    if avg > 0.001:
        return "机构买入偏强"
    elif avg < -0.001:
        return "机构卖出偏强"
    return "中性"


def calc_stablecoin_change(sc: Any) -> float:
    if not sc or not sc.history or len(sc.history) < 2:
        return 0
    latest = sc.history[-1].total_mcap
    earliest = sc.history[0].total_mcap
    if earliest > 0:
        return round((latest - earliest) / earliest * 100, 2)
    return 0
