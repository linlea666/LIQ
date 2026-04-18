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
    ("i:diniw:ice",   "dxy_change_pct"),
    ("i:ndx:nasdaq",  "nasdaq_change_pct"),
    ("i:inx:sp",      "sp500_change_pct"),
    ("i:xauusd:liffe", "gold_change_pct"),
]


# BBX 字段覆盖率健康阈值（低于此比例则 WARNING，帮助及时发现上游数据退化）
_BBX_COVERAGE_WARN_THRESHOLD = 0.80
_BBX_PREV_MISSING: set[str] = set()  # 仅在失联集合变化时 WARN，避免稳定失联刷屏

# 覆盖率分母：只统计"本应由 BBX 填充"的字段，排除 MarketIndexData 中已移除/未映射的
# 历史遗留字段（btc_max_pain/btc_hashrate/usdt_market_cap/stablecoin_dominance/
# okx_ls_ratio_btc/binance_ls_ratio_btc/raw_items）——这些字段代码里永远为 None，
# 把它们算进分母会让覆盖率永久虚低、触发假告警。
_BBX_MAPPABLE_FIELDS: frozenset[str] = (
    frozenset(f for _, f in _DIRECT_MAP)
    | frozenset(f for _, f in _CHANGE_PCT_MAP)
    | frozenset({"exchange_btc_change_24h"})
)


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
    missing_keys: list[str] = []

    for bbx_key, mi_field in _DIRECT_MAP:
        val = bbx.get_float(bbx_key)
        if val is not None:
            setattr(mi, mi_field, val)
        else:
            missing_keys.append(f"{bbx_key}→{mi_field}")

    for bbx_key, mi_field in _CHANGE_PCT_MAP:
        val = bbx.get_change_pct(bbx_key)
        if val is not None:
            setattr(mi, mi_field, val)
        else:
            missing_keys.append(f"{bbx_key}→{mi_field}")

    _EX_BAL_KEYS = [
        "i:bnbbtchold:arkm", "i:okxbtchold:arkm",
        "i:bitfbtchold:arkm", "i:coinbtchold:arkm",
    ]
    chg_parts = [bbx.get_change(k) for k in _EX_BAL_KEYS]
    chg_valid = [c for c in chg_parts if c is not None]
    if chg_valid:
        mi.exchange_btc_change_24h = sum(chg_valid)
    else:
        missing_keys.append("exchange_btc_balance_changes→exchange_btc_change_24h")

    # 覆盖率按"可映射字段数"计算，不把未映射的历史遗留字段算进去
    total = len(_BBX_MAPPABLE_FIELDS)
    populated = sum(
        1 for f in _BBX_MAPPABLE_FIELDS if getattr(mi, f, None) is not None
    )
    logger.info("BBX → MarketIndexData | %d/%d mappable fields populated", populated, total)

    coverage = populated / total if total > 0 else 1.0
    global _BBX_PREV_MISSING
    current_missing = set(missing_keys)
    if coverage < _BBX_COVERAGE_WARN_THRESHOLD and current_missing != _BBX_PREV_MISSING:
        logger.warning(
            "BBX coverage degraded: %d/%d (%.0f%%) | missing %d fields: %s",
            populated,
            total,
            coverage * 100,
            len(missing_keys),
            ", ".join(missing_keys) if len(missing_keys) <= 12 else
            ", ".join(missing_keys[:12]) + f" ... (+{len(missing_keys) - 12} more)",
        )
        _BBX_PREV_MISSING = current_missing
    elif coverage >= _BBX_COVERAGE_WARN_THRESHOLD and _BBX_PREV_MISSING:
        logger.info("BBX coverage recovered to %.0f%% (%d/%d)", coverage * 100, populated, total)
        _BBX_PREV_MISSING = set()

    for ccy in supported_coins:
        st = states[ccy]
        if mi.fear_greed is not None and st.market_index and st.market_index.fear_greed is not None:
            st.fear_greed_prev = int(st.market_index.fear_greed)
        st.market_index = mi

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
