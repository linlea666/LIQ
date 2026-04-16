"""
BBX 市场指数轮询：一次 POST 获取全部宏观/链上/衍生品指标，
映射到 MarketIndexData 和 CoinbasePremiumData，
替代 Coinglass 的 fear_greed、btc_dominance、coinbase_premium 等低频接口。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from models.flow import MarketIndexData
from sources.bbx import BBXSource

logger = logging.getLogger(__name__)

# ── BBX key → MarketIndexData 字段映射 ──

_DIRECT_MAP: list[tuple[str, str]] = [
    # (bbx_key, MarketIndexData_field)
    ("i:fgi:alternative",           "fear_greed"),
    ("i:bitcoinsuprp:aicoin",       "btc_dominance"),
    ("i:diniw:ice",                 "dxy"),
    ("i:ndx:nasdaq",                "nasdaq"),
    ("i:inx:sp",                    "sp500"),
    ("i:xauusd:liffe",              "gold"),
    ("i:mvrv:bitcoin",              "btc_mvrv"),
    ("i:btcinvest:ahr999",          "ahr999"),
    ("i:btcdpi:aicoin",             "coinbase_btc_premium"),
    ("i:premiumrateusdt:okex",      "usdt_otc_premium"),
    ("i:btcposhistvol:okex",        "btc_hist_vol"),
    ("i:btcposimpvol:okex",         "btc_implied_vol"),
    ("i:btcopt1mimpvolskew:okex",   "btc_iv_skew_1m"),
    ("i:btcusdvolatility:deribit",  "btc_dvol"),
    ("i:btcoptposlsratio:okex",     "btc_put_call_oi"),
    ("i:usty10y:nybot",             "us_10y_yield"),
    ("i:fedeffr:fed",               "fed_rate"),
    ("i:bnbbtchold:arkm",           "binance_btc_balance"),
    ("i:okxbtchold:arkm",           "okx_btc_balance"),
    ("i:bitfbtchold:arkm",          "bitfinex_btc_balance"),
    ("i:coinbtchold:arkm",          "coinbase_btc_balance"),
    # 已移除：btc_hashrate(低频无用)、usdt_market_cap(冗余)、stablecoin_dominance(冗余)
    # 已移除：okx_ls_ratio_btc、binance_ls_ratio_btc（使用Coinglass数据源）
]

_CHANGE_PCT_MAP: list[tuple[str, str]] = [
    ("i:ndx:nasdaq", "nasdaq_change_pct"),
    ("i:inx:sp",     "sp500_change_pct"),
    ("i:xauusd:liffe","gold_change_pct"),
]


async def poll_bbx_index(
    bbx: BBXSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """从 BBX 获取全部宏观指标，写入各币种 state.market_index。"""
    indices = await bbx.fetch_all()
    if not indices:
        return

    mi = MarketIndexData(ts=int(time.time()))

    for bbx_key, mi_field in _DIRECT_MAP:
        val = bbx.get_float(bbx_key)
        if val is not None:
            setattr(mi, mi_field, val)

    for bbx_key, mi_field in _CHANGE_PCT_MAP:
        val = bbx.get_change_pct(bbx_key)
        if val is not None:
            setattr(mi, mi_field, val)

    populated = sum(1 for f in mi.__fields__ if getattr(mi, f) is not None and f != "ts")
    logger.info("BBX → MarketIndexData | %d/%d fields populated", populated, len(mi.__fields__) - 1)

    for ccy in supported_coins:
        states[ccy].market_index = mi

    _update_coinbase_premium(bbx, states, supported_coins)


def _update_coinbase_premium(
    bbx: BBXSource,
    states: dict[str, Any],
    supported_coins: list[str],
) -> None:
    """用 BBX Coinbase 溢价数据填充 state.coinbase_premium（仅当 Coinglass 未提供时）。"""
    from models.macro import CoinbasePremiumData
    prem = bbx.get_float("i:btcdpi:aicoin")
    if prem is None:
        return

    for ccy in supported_coins:
        state = states[ccy]
        if state.coinbase_premium and (time.time() - state.coinbase_premium.ts) < 600:
            continue
        state.coinbase_premium = CoinbasePremiumData(
            ts=int(time.time()),
            current_premium=prem,
        )
