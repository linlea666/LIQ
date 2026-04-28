"""
衍生品 / 资金费率相关 Coinglass 轮询（模块级 async 函数）。

由 Engine 注入 cg、state(s)、percentile 等依赖；不在此模块内调用 recompute。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Optional

from config.settings import CoinConfig

if TYPE_CHECKING:
    from engine import CoinState
from models.flow import (
    BasisData,
    ExchangeFundingRate,
    FundingRateData,
    LongShortRatioData,
    LongShortRatioExchange,
    MultiFundingRateData,
    OIData,
    OISnapshot,
)
from models.market import TickerData
from processors.percentile import PercentileTracker
from sources.binance_futures import BinanceFuturesSource
from sources.coinglass import CoinglassSource
from sources.funding_official import (
    fetch_official_pair as fetch_official_funding_pair,
    to_okx_inst_id,
)

logger = logging.getLogger(__name__)


def log_api_fields_once(tag: str, sample: Any, logged_keys: set[str]) -> None:
    """与 Engine._log_keys_once 等价：每个 tag 仅记录一次 API 字段名。"""
    if tag in logged_keys:
        return
    logged_keys.add(tag)
    if isinstance(sample, dict):
        logger.info("API fields [%s]: %s", tag, list(sample.keys())[:20])
    elif isinstance(sample, list) and sample and isinstance(sample[0], dict):
        logger.info("API fields [%s]: %s", tag, list(sample[0].keys())[:20])


async def poll_ticker_all(
    cg: CoinglassSource,
    states: dict[str, CoinState],
    supported_coins: list[str],
    get_coin: Callable[[str], CoinConfig],
    percentile: PercentileTracker,
    logged_keys: set[str],
    bn: BinanceFuturesSource | None = None,
) -> None:
    """coins-markets 一次获取全币种行情（纯 Binance，不回退 Coinglass）。"""
    del cg
    if not bn:
        logger.warning("Binance ticker 源未注入，ticker 跳过")
        return
    data = await bn.fetch_tickers_24h()
    if not data:
        return
    log_api_fields_once("ticker-markets", data, logged_keys)

    symbol_to_ccy: dict[str, str] = {}
    for c in supported_coins:
        coin_cfg = get_coin(c)
        # 兼容 Coinglass(BTC) 与 Binance(BTCUSDT) 两种 symbol 形态
        symbol_to_ccy[coin_cfg.symbol_cg] = c
        symbol_to_ccy[coin_cfg.symbol_cg_pair] = c

    for item in data:
        symbol = str(item.get("symbol", ""))
        ccy = symbol_to_ccy.get(symbol)
        if not ccy and symbol.endswith("USDT"):
            ccy = symbol_to_ccy.get(symbol.replace("USDT", ""))
        if not ccy:
            continue

        state = states[ccy]
        try:
            price = float(item.get("current_price", item.get("lastPrice", item.get("price", 0))))
            if price <= 0:
                continue
            chg_pct = float(item.get("price_change_percent_24h", item.get("priceChangePercent", 0)))
            open_24h = price / (1 + chg_pct / 100) if chg_pct != 0 else price
            state.ticker = TickerData(
                coin=ccy,
                ts=int(time.time() * 1000),
                last=price,
                high_24h=float(item.get("high24h", item.get("highPrice", price))),
                low_24h=float(item.get("low24h", item.get("lowPrice", price))),
                vol_24h=float(item.get("volUsd24h", item.get("quoteVolume", 0))),
                change_24h=round(price - open_24h, 2),
                change_pct_24h=round(chg_pct, 2),
            )

            oi_usd = float(item.get("open_interest_usd", item.get("openInterest", 0)))
            if oi_usd > 0:
                snapshot = OISnapshot(
                    coin=ccy, ts=int(time.time()),
                    oi=oi_usd, oi_usd=oi_usd,
                )
                state.oi_history.append(snapshot)
                percentile.push(ccy, "oi", oi_usd)
            if "binance_ticker_ready" not in state._log_once_keys:
                state._log_once_keys.add("binance_ticker_ready")
                logger.info(
                    "Binance ticker 生效 | coin=%s last=%.4f chg24h=%.2f%%",
                    ccy, price, chg_pct,
                )
        except (ValueError, KeyError):
            continue


async def poll_oi(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """获取 OI 聚合历史。"""
    data = await cg.fetch_oi_aggregated_history(
        coin.symbol_cg, interval="5m", limit=50,
    )
    if not data:
        return

    for item in data:
        try:
            oi_usd = float(item.get("close", item.get("openInterest", item.get("value", 0))))
            ts = int(item.get("time", item.get("t", 0)))
            if oi_usd > 0:
                snapshot = OISnapshot(
                    coin=coin.ccy, ts=ts, oi=oi_usd, oi_usd=oi_usd,
                )
                state.oi_history.append(snapshot)
        except (ValueError, KeyError):
            continue

    if state.oi_history:
        current = state.oi_history[-1]
        first = state.oi_history[0]
        change_1h = 0.0
        if first.oi_usd > 0:
            change_1h = (current.oi_usd - first.oi_usd) / first.oi_usd * 100

        recent_5m = list(state.oi_history)[-30:]
        change_5m = 0.0
        if recent_5m and recent_5m[0].oi_usd > 0:
            change_5m = (current.oi_usd - recent_5m[0].oi_usd) / recent_5m[0].oi_usd * 100

        trend = "stable"
        if change_1h > 3:
            trend = "surging"
        elif change_1h < -3:
            trend = "declining"

        state.oi = OIData(
            coin=coin.ccy, ts=current.ts,
            current_usd=current.oi_usd,
            change_1h_pct=round(change_1h, 2),
            change_5m_pct=round(change_5m, 2),
            trend=trend,
        )

    oi_1h = await cg.fetch_oi_aggregated_history(
        coin.symbol_cg, interval="1h", limit=24,
    )
    if oi_1h and isinstance(oi_1h, list) and len(oi_1h) >= 2:
        try:
            first_val = float(oi_1h[0].get("close", oi_1h[0].get("openInterest", 0)))
            last_val = float(oi_1h[-1].get("close", oi_1h[-1].get("openInterest", 0)))
            if first_val > 0:
                state.oi_change_24h_pct = round((last_val - first_val) / first_val * 100, 2)
        except (ValueError, KeyError, TypeError):
            pass


async def poll_funding_all(
    cg: CoinglassSource,
    states: dict[str, CoinState],
    supported_coins: list[str],
    get_coin: Callable[[str], CoinConfig],
    percentile: PercentileTracker,
    logged_keys: set[str],
) -> None:
    """获取全币种资金费率（官方主源 + Coinglass fallback）。

    取源策略（阶段 1 切换后）：
      1. 主源：并发打 Binance `/fapi/v1/premiumIndex` + OKX `/api/v5/public/funding-rate`
         - 单位明确：两家均为小数（0.0001 = 0.01%），无 ×10/×100 单位风险
         - 无 API key，公共接口，延迟 80-120ms
      2. 官方两家同时失败才退回 Coinglass `fetch_fr_exchange_list` 的 avg
         - 保留 Coinglass 是为了全球网络异常的兜底，而非常态使用

    方案 B（展示层精简）：
      exchanges 列表只保留 Binance + OKX 两条，不再展示 Coinglass 其他 8 家。
      理由：下游阈值判定和 AI prompt 均未真正使用 Bybit/HTX/Gate 等数据，
            展示 10 家反而分散注意力、增加前端噪音。
    """
    # ── 主源：并发拉官方 Binance + OKX，每币一次 ──
    official: dict[str, tuple[Optional[float], Optional[float]]] = {}
    tasks = []
    for ccy in supported_coins:
        coin = get_coin(ccy)
        tasks.append((
            ccy,
            fetch_official_funding_pair(
                coin.symbol_cg_pair,  # e.g. "BTCUSDT"
                to_okx_inst_id(coin.symbol_cg_pair),  # e.g. "BTC-USDT-SWAP"
            ),
        ))
    results = await asyncio.gather(*(t[1] for t in tasks), return_exceptions=True)
    for idx, (ccy, _) in enumerate(tasks):
        res = results[idx]
        if isinstance(res, Exception):
            logger.warning("[funding-official] %s gather exception: %s", ccy, res)
            official[ccy] = (None, None)
        else:
            official[ccy] = res  # (bn_rate, okx_rate)

    # ── Fallback 源：若任何币种官方两家同时失败，拉一次 Coinglass ──
    need_fallback = any(bn is None and ox is None for bn, ox in official.values())
    cg_by_symbol: dict[str, list[dict]] = {}
    if need_fallback:
        try:
            data = await cg.fetch_fr_exchange_list()
        except Exception as e:  # noqa: BLE001
            logger.warning("[funding-official] coinglass fallback fetch fail: %s", e)
            data = None
        if data:
            log_api_fields_once("funding-rate", data, logged_keys)
            for item in data:
                sym = item.get("symbol", "")
                if sym:
                    cg_by_symbol[sym] = item.get(
                        "stablecoin_margin_list", item.get("uMarginList", []),
                    ) or []

    # ── 组装每币 state ──
    now_ts = int(time.time())
    for ccy in supported_coins:
        state = states[ccy]
        coin = get_coin(ccy)
        bn_rate, okx_rate = official.get(ccy, (None, None))

        exchanges: list[ExchangeFundingRate] = []
        source_used = "official"
        avg_rates: list[float] = []

        if bn_rate is not None:
            exchanges.append(ExchangeFundingRate(exchange="Binance", current=bn_rate))
            avg_rates.append(bn_rate)
        if okx_rate is not None:
            exchanges.append(ExchangeFundingRate(exchange="OKX", current=okx_rate))
            avg_rates.append(okx_rate)

        # Fallback 到 Coinglass（仅当官方两家都失败）
        if not avg_rates:
            source_used = "coinglass_fallback"
            cg_margin = cg_by_symbol.get(coin.symbol_cg, [])
            cg_rates: list[float] = []
            for ex_item in cg_margin:
                name = (ex_item.get("exchange") or ex_item.get("exchangeName") or "").lower()
                rate = ex_item.get("funding_rate", ex_item.get("rate"))
                if rate is None:
                    continue
                try:
                    r = float(rate)
                except (TypeError, ValueError):
                    continue
                cg_rates.append(r)
                if "binance" in name and bn_rate is None:
                    bn_rate = r
                    exchanges.append(ExchangeFundingRate(exchange="Binance", current=r))
                elif ("okx" in name or "okex" in name) and okx_rate is None:
                    okx_rate = r
                    exchanges.append(ExchangeFundingRate(exchange="OKX", current=r))
            # 若 Coinglass 也没给出 Binance/OKX，用中位数兜底
            if not exchanges and len(cg_rates) >= 3:
                sorted_rates = sorted(cg_rates)
                median = sorted_rates[len(sorted_rates) // 2]
                filtered = [
                    r for r in cg_rates
                    if abs(r - median) < 10 * max(abs(median), 0.0005)
                ]
                if filtered:
                    avg_rates = filtered
            elif not avg_rates and exchanges:
                avg_rates = [e.current for e in exchanges]

        # ── 聚合 + 阈值判定（阈值保留原值 0.0005） ──
        avg_current = sum(avg_rates) / len(avg_rates) if avg_rates else 0.0
        interp = "中性"
        if avg_current > 0.0005:
            interp = "多头拥挤"
        elif avg_current < -0.0005:
            interp = "空头拥挤"

        state.multi_funding = MultiFundingRateData(
            coin=ccy, ts=now_ts,
            exchanges=exchanges,
            avg_current=round(avg_current, 6),
            interpretation=interp,
        )
        state.funding = FundingRateData(
            coin=ccy, ts=now_ts,
            okx_rate=okx_rate, binance_rate=bn_rate,
            avg_rate=round(avg_current, 6),
            interpretation=interp,
        )
        percentile.push(ccy, "funding", avg_current)

        # ── 首次日志（每币种只打一次，便于验证切源成功） ──
        log_key = f"funding_official_ready_{ccy}"
        if log_key not in state._log_once_keys and (bn_rate is not None or okx_rate is not None):
            state._log_once_keys.add(log_key)
            logger.info(
                "[funding-official] %s source=%s bn=%s okx=%s avg=%.6f interp=%s",
                ccy, source_used,
                f"{bn_rate:.6f}" if bn_rate is not None else "N/A",
                f"{okx_rate:.6f}" if okx_rate is not None else "N/A",
                avg_current, interp,
            )


async def poll_ls_ratio(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    logged_keys: set[str],
) -> None:
    """获取多空比（全局 + 大户账户 + 大户持仓）。"""
    exchange = coin.exchange_primary

    global_data = await cg.fetch_global_ls_ratio_history(
        exchange, coin.symbol_cg_pair, interval="1h", limit=24,
    )
    log_api_fields_once("ls-ratio-global", global_data, logged_keys)
    if global_data and isinstance(global_data, list) and len(global_data) > 0:
        item = global_data[-1]
        long_pct = float(item.get("global_account_long_percent", item.get("longAccount", 50)))
        short_pct = float(item.get("global_account_short_percent", item.get("shortAccount", 50)))
        ratio = float(item.get("global_account_long_short_ratio", long_pct / short_pct if short_pct > 0 else 1.0))
        state.ls_ratio = LongShortRatioData(
            coin=coin.ccy, ts=int(time.time()),
            dimension="global",
            exchanges=[LongShortRatioExchange(
                exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
            )],
            avg_ratio=ratio,
        )
        state.ls_ratio_long_pct = long_pct
        state.ls_ratio_short_pct = short_pct
        if len(global_data) >= 2:
            first = global_data[0]
            first_ratio = float(first.get("global_account_long_short_ratio",
                           float(first.get("longAccount", 50)) / max(float(first.get("shortAccount", 50)), 0.01)))
            state.ls_ratio_change_24h = round(ratio - first_ratio, 4)

    top_acct_data = await cg.fetch_top_ls_account_ratio_history(
        exchange, coin.symbol_cg_pair, interval="1h", limit=24,
    )
    if top_acct_data and isinstance(top_acct_data, list) and len(top_acct_data) > 0:
        item = top_acct_data[-1]
        long_pct = float(item.get("top_account_long_percent", item.get("longAccount", 50)))
        short_pct = float(item.get("top_account_short_percent", item.get("shortAccount", 50)))
        ratio = float(item.get("top_account_long_short_ratio", long_pct / short_pct if short_pct > 0 else 1.0))
        state.ls_ratio_top_account = LongShortRatioData(
            coin=coin.ccy, ts=int(time.time()),
            dimension="top_account",
            exchanges=[LongShortRatioExchange(
                exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
            )],
            avg_ratio=ratio,
        )
        state.ls_top_acct_long_pct = long_pct
        state.ls_top_acct_short_pct = short_pct
        if len(top_acct_data) >= 2:
            first = top_acct_data[0]
            first_ratio = float(first.get("top_account_long_short_ratio",
                           float(first.get("longAccount", 50)) / max(float(first.get("shortAccount", 50)), 0.01)))
            state.ls_top_acct_change_24h = round(ratio - first_ratio, 4)

    top_pos_data = await cg.fetch_top_ls_position_ratio_history(
        exchange, coin.symbol_cg_pair, interval="1h", limit=1,
    )
    if top_pos_data and isinstance(top_pos_data, list) and len(top_pos_data) > 0:
        item = top_pos_data[-1]
        long_pct = float(item.get("top_position_long_percent", item.get("longPosition", 50)))
        short_pct = float(item.get("top_position_short_percent", item.get("shortPosition", 50)))
        ratio = float(item.get("top_position_long_short_ratio", long_pct / short_pct if short_pct > 0 else 1.0))
        state.ls_ratio_top_position = LongShortRatioData(
            coin=coin.ccy, ts=int(time.time()),
            dimension="top_position",
            exchanges=[LongShortRatioExchange(
                exchange=exchange, long_pct=long_pct, short_pct=short_pct, ratio=ratio,
            )],
            avg_ratio=ratio,
        )


async def poll_basis(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
    bn: BinanceFuturesSource | None = None,
) -> None:
    """获取期现溢价：纯 Binance premiumIndex 本地计算（不回退 Coinglass）。"""
    del cg
    if not bn:
        logger.warning("Binance basis 源未注入 | coin=%s", coin.ccy)
        return
    try:
        premium = await bn.fetch_premium_index(coin.symbol_cg_pair)
        if not premium:
            return
        mark_price = float(premium.get("markPrice", 0))
        index_price = float(premium.get("indexPrice", 0))
        if mark_price <= 0 or index_price <= 0:
            return
        basis_pct = (mark_price - index_price) / index_price * 100
        interp = "合约偏贵" if basis_pct > 0.1 else "合约折价" if basis_pct < -0.1 else "中性"
        now_ts = int(time.time())
        state.basis = BasisData(
            coin=coin.ccy,
            ts=now_ts,
            mark_price=mark_price,
            index_price=index_price,
            basis_pct=round(basis_pct, 4),
            interpretation=interp,
        )
        # ── Market Action Analyzer: basis 序列（maxlen=60 ≈ 近 1h，60s 粒度）──
        from collections import deque as _deque
        bh = getattr(state, "basis_history", None)
        if not isinstance(bh, _deque):
            bh = _deque(maxlen=60)
            state.basis_history = bh
        bh.append({"ts": now_ts, "basis_pct": round(basis_pct, 4)})
        if "binance_basis_ready" not in state._log_once_keys:
            state._log_once_keys.add("binance_basis_ready")
            logger.info(
                "Binance basis 生效 | coin=%s basis_pct=%.4f mark=%.2f index=%.2f",
                coin.ccy, basis_pct, mark_price, index_price,
            )
    except Exception:
        logger.warning("poll_basis (binance) failed | coin=%s", coin.ccy, exc_info=True)


async def poll_oi_exchange_rank(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """获取交易所 OI 持仓占比排名。"""
    data = await cg.fetch_oi_exchange_list(symbol=coin.symbol_cg)
    if not data or not isinstance(data, list):
        return
    try:
        exchanges = []
        total_oi = 0.0
        all_aggregated: dict | None = None     # M2：All 行的完整 delta + 保证金分布
        for item in data:
            ex = item.get("exchange", "")
            if ex == "All":
                total_oi = float(item.get("open_interest_usd", 0))
                # M2 拥挤度引擎需要：6 周期 delta + 币本位/U 本位金额（liquidity_wall_engine 消费）
                all_aggregated = {
                    "oi_usd": float(item.get("open_interest_usd", 0) or 0),
                    "oi_coin_margin_usd": float(item.get("open_interest_by_coin_margin", 0) or 0),
                    "oi_stable_margin_usd": float(item.get("open_interest_by_stable_coin_margin", 0) or 0),
                    "change_5m_pct": float(item.get("open_interest_change_percent_5m", 0) or 0),
                    "change_15m_pct": float(item.get("open_interest_change_percent_15m", 0) or 0),
                    "change_30m_pct": float(item.get("open_interest_change_percent_30m", 0) or 0),
                    "change_1h_pct": float(item.get("open_interest_change_percent_1h", 0) or 0),
                    "change_4h_pct": float(item.get("open_interest_change_percent_4h", 0) or 0),
                    "change_24h_pct": float(item.get("open_interest_change_percent_24h", 0) or 0),
                }
                continue
            exchanges.append({
                "exchange": ex,
                "oi_usd": float(item.get("open_interest_usd", 0)),
                "change_1h": float(item.get("open_interest_change_percent_1h", 0)),
                "change_24h": float(item.get("open_interest_change_percent_24h", 0)),
            })
        exchanges.sort(key=lambda x: x["oi_usd"], reverse=True)
        state.oi_exchange_rank = {
            "ts": int(time.time()),
            "total_oi_usd": total_oi,
            "exchanges": exchanges[:8],
            "all_aggregated": all_aggregated,   # M2 新增 key（旧消费者忽略即可）
        }
    except (ValueError, KeyError):
        logger.debug("oi_exchange_rank parse failed", exc_info=True)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# §9h: 净持仓 + 合约资金流 + TD 序列
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def poll_net_position(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取净持仓 v2 (1h)，取最新值与趋势。"""
    try:
        rows = await cg.fetch_net_position_v2_history(
            exchange="Binance", symbol=coin.symbol_cg_pair, interval="1h", limit=24,
        )
        if not rows:
            return
        vals = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get("netPosition", r.get("net_position"))
            if v is None:
                v = r.get("netPositionChangeCum", r.get("net_position_change_cum"))
            if v is None:
                v = r.get("netPositionChange", r.get("net_position_change"))
            if v is not None:
                vals.append(float(v))
        if not vals:
            return
        state.net_position_latest = vals[-1]
        if len(vals) >= 2:
            state.net_position_change_24h = vals[-1] - vals[0]
        # P0.2 · 趋势标签与展示端点差同源（防"下降(多头减仓) + 43.7% 显著增持"自相矛盾）
        # 旧版用"滚动均值差"，与 prompt 侧用"端点差"计算的百分比不同源，曲线形态特殊时方向打架。
        # 新版：统一用 24h 端点差符号 + 5%/2% 显著性阈值（与 prompt 侧展示口径一致）。
        if state.net_position_change_24h is not None and len(vals) >= 2:
            diff = float(state.net_position_change_24h)
            # 分母口径与 prompt 侧完全一致：24h 两端绝对值较大者（防方向翻转期分母接近 0）
            base = max(abs(float(vals[-1])), abs(float(vals[0])))
            if base < 1.0:
                # 基数过小（方向翻转/接近零持仓）：只按符号定方向，不加显著性修饰
                if diff > 0:
                    state.net_position_trend = "上升(多头增仓)"
                elif diff < 0:
                    state.net_position_trend = "下降(多头减仓)"
                else:
                    state.net_position_trend = "持平"
            else:
                pct = diff / base
                if pct > 0.05:
                    state.net_position_trend = "上升(多头增仓)"
                elif pct < -0.05:
                    state.net_position_trend = "下降(多头减仓)"
                else:
                    state.net_position_trend = "持平"
    except Exception:
        logger.warning("poll_net_position failed", exc_info=True)
        state.poll_failures["net_position"] = "API调用失败"


async def poll_futures_coin_netflow(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取合约币种净资金流 (1h)，取最近 1h 净值与趋势。"""
    try:
        rows = await cg.fetch_futures_coin_netflow(
            symbol=coin.symbol_cg_pair, interval="1h", limit=24,
        )
        if not rows:
            return
        if isinstance(rows, dict):
            v1h = rows.get("net_flow_usd_1h", rows.get("netFlowUsd1h"))
            if v1h is not None:
                state.futures_coin_netflow_1h = float(v1h)
            v15m = rows.get("net_flow_usd_15m", rows.get("netFlowUsd15m"))
            v4h = rows.get("net_flow_usd_4h", rows.get("netFlowUsd4h"))
            trend_votes = [x for x in (v15m, v1h, v4h) if x is not None]
            if trend_votes:
                pos = sum(1 for x in trend_votes if float(x) > 0)
                state.futures_coin_netflow_trend = "持续流入" if pos >= 2 else "持续流出"
            state.poll_failures.pop("coin_netflow", None)
            return
        if not isinstance(rows, list):
            return
        vals = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            v = r.get("netflow", r.get("netFlow", r.get("value")))
            if v is not None:
                vals.append(float(v))
        if not vals:
            return
        state.futures_coin_netflow_1h = vals[-1]
        if len(vals) >= 4:
            recent = sum(1 for v in vals[-4:] if v > 0)
            if recent >= 3:
                state.futures_coin_netflow_trend = "持续流入"
            elif recent <= 1:
                state.futures_coin_netflow_trend = "持续流出"
            else:
                state.futures_coin_netflow_trend = "交替"
        state.poll_failures.pop("coin_netflow", None)
    except Exception:
        logger.warning("poll_futures_coin_netflow failed", exc_info=True)
        state.poll_failures["coin_netflow"] = "API调用失败"


async def poll_td_sequential(
    cg: CoinglassSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取 TD 序列 (1d)，取最新计数与方向。"""
    try:
        rows = await cg.fetch_td_sequential(
            exchange="Binance", symbol=coin.symbol_cg_pair, interval="1d", limit=10,
        )
        if not rows:
            return
        latest = rows[-1] if isinstance(rows, list) and rows else None
        if not latest:
            return
        buy_c = latest.get("td_buy_count", latest.get("tdBuyCount"))
        sell_c = latest.get("td_sell_count", latest.get("tdSellCount"))
        old_count = latest.get("td_count", latest.get("tdCount", latest.get("count")))
        old_dir = latest.get("td_direction", latest.get("tdDirection",
                             latest.get("direction", "")))
        if buy_c is not None and int(buy_c) > 0:
            state.td_sequential_count = int(buy_c)
            state.td_sequential_direction = "buy"
        elif sell_c is not None and int(sell_c) > 0:
            state.td_sequential_count = int(sell_c)
            state.td_sequential_direction = "sell"
        elif old_count is not None:
            state.td_sequential_count = int(old_count)
            if old_dir:
                state.td_sequential_direction = str(old_dir)
        state.poll_failures.pop("td_sequential", None)
    except Exception:
        logger.warning("poll_td_sequential failed", exc_info=True)
        state.poll_failures["td_sequential"] = "API调用失败"


# ════════════════════════════════════════════════════════════════════════════
# MAA P0 增强 · 资金费 8h 历史 + OI 30d hourly 历史
#
# 设计原则：
#   1. 只写 state.funding_history_8h / state.oi_hourly_history 两个 deque
#   2. 顺便回填 state.multi_funding.avg_7d / oi_weighted 两个之前永远是 0 的字段
#      （根因：代码从未写入，不是 API 问题）
#   3. 调用频率：5min / 次（两个接口都有 30s 级 Coinglass 缓存，不会爆 quota）
#   4. 所有派生字段由 facts_collector 在消费端计算，poll 层只负责"采样 + 缓存"
# ════════════════════════════════════════════════════════════════════════════

async def poll_funding_history_8h(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """拉 7d × 8h 结算点资金费率历史，缓存到 state.funding_history_8h。

    ⚠ 单位陷阱：
      Coinglass `/api/futures/funding-rate/oi-weight-history` 的 close 字段是
      **百分比单位**（-0.0071 表示 -0.0071% = -0.000071 小数），
      而 Binance/OKX 官方接口返回的是**小数单位**（-0.000071）。
      两个口径差 100 倍！本函数统一归一化为**小数单位**后再写入 deque，
      所有下游（facts_collector / prompt）都按小数处理，与 avg_current 一致。

    归一化策略（三层，优先级从高到低）：
      1. **交叉比对法**（最可靠）：若 state.multi_funding.avg_current 已有值
         （来自 Binance/OKX 官方小数口径），用它和 history 最新点的量级比
         ratio = median(|history|) / |avg_current|，若 ratio > 50 → 百分比
      2. **绝对量级法**（fallback）：单纯基于历史极值
         max_abs > 0.003 → 百分比（历史峰值 BTC 2021 也才 ~0.003 小数单位 = 0.3%/8h）
      3. **不触发归一化**：认为已经是小数单位

      0.0071 百分比在"绝对量级法"下 0.0071 > 0.003 会被识别为百分比
      0.001 小数在"绝对量级法"下 0.001 < 0.003，不会被误判
    """
    try:
        # limit=90 = 30 天 × 3 个 8h 点：
        #   - 满 30d 用于 percentile_30d（< 30 点返回 None）
        #   - 旧版本 60（20d）只够算 sign_flip_7d（42 点窗口），不够 30d 百分位
        #   - 与 state.funding_history_8h.maxlen / raw_points[-90:] 切片三处一致
        data = await cg.fetch_fr_oi_weight_history(
            coin.symbol_cg, interval="8h", limit=90,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-hist-8h] %s fetch fail: %s", coin.ccy, e)
        state.poll_failures["funding_history_8h"] = "API调用失败"
        return

    if not data or not isinstance(data, list):
        return

    raw_rates: list[float] = []
    raw_points: list[tuple[int, float]] = []
    for item in data:
        try:
            ts_raw = int(item.get("time", item.get("t", 0)))
            ts_sec = ts_raw // 1000 if ts_raw > 10_000_000_000 else ts_raw
            rate = float(item.get("close", item.get("c", 0)))
            if ts_sec > 0:
                raw_points.append((ts_sec, rate))
                raw_rates.append(rate)
        except (TypeError, ValueError):
            continue

    if not raw_points:
        return

    # ── 单位自动归一化（三层策略） ──
    scale = 1.0
    unit_note = "decimal (no scale)"
    abs_rates = [abs(r) for r in raw_rates if r != 0]
    max_abs = max(abs_rates) if abs_rates else 0.0

    # 策略 1：交叉比对官方小数口径（最可靠）
    ref_decimal = None
    if state.multi_funding and abs(state.multi_funding.avg_current) > 1e-9:
        ref_decimal = abs(state.multi_funding.avg_current)
    elif state.funding and abs(state.funding.avg_rate) > 1e-9:
        ref_decimal = abs(state.funding.avg_rate)

    if ref_decimal is not None and abs_rates:
        # 用中位数（对异常极值鲁棒）
        sorted_abs = sorted(abs_rates)
        median_abs = sorted_abs[len(sorted_abs) // 2]
        if median_abs > 0:
            ratio = median_abs / ref_decimal
            if ratio > 50:  # 相差 50 倍以上必然是百分比
                scale = 0.01
                unit_note = f"cross-check: median(|hist|)/|avg_current|={ratio:.1f}x → percent"
            elif 0.1 <= ratio <= 10:  # 同量级
                unit_note = f"cross-check: ratio={ratio:.2f}x → decimal"
            # 其他情况（ratio < 0.1 或 10-50）走 fallback
            else:
                ref_decimal = None  # 触发 fallback

    # 策略 2：绝对量级法（fallback）
    if scale == 1.0 and ref_decimal is None:
        # BTC 历史峰值 funding ~0.3%/8h = 0.003 小数；超过就几乎肯定是百分比
        if max_abs > 0.003:
            scale = 0.01
            unit_note = f"abs-magnitude: max_abs={max_abs:.4f} > 0.003 → percent"
        else:
            unit_note = f"abs-magnitude: max_abs={max_abs:.4f} ≤ 0.003 → decimal"

    raw_points.sort(key=lambda p: p[0])
    state.funding_history_8h.clear()
    # 与 state.funding_history_8h.maxlen 对齐：30d × 3 个 8h 结算点 = 90 点
    # 用于 percentile_30d；旧版本为 60（20d），扩容不影响 7d/14d 逻辑
    for ts_sec, rate in raw_points[-90:]:
        state.funding_history_8h.append({"ts_sec": ts_sec, "rate": rate * scale})

    # ── 回填 multi_funding.avg_7d / oi_weighted（若对象已存在） ──
    # 注意：poll_funding_all 每次会新建 MultiFundingRateData 覆盖这两个字段，
    # 所以 facts_collector 不依赖这里的回填，而是直接从 funding_history_8h 重算。
    # 保留这段回填是为了让仪表盘侧的 MultiFundingRateData 也有值（兼容老代码路径）。
    if state.multi_funding:
        recent_21 = list(state.funding_history_8h)[-21:]
        if recent_21:
            state.multi_funding.avg_7d = round(
                sum(p["rate"] for p in recent_21) / len(recent_21), 8,
            )
            state.multi_funding.oi_weighted = round(recent_21[-1]["rate"], 8)

    state.poll_failures.pop("funding_history_8h", None)
    if "funding_hist_8h_ready" not in state._log_once_keys:
        state._log_once_keys.add("funding_hist_8h_ready")
        latest_decimal = raw_points[-1][1] * scale
        logger.info(
            "[funding-hist-8h] %s ready | points=%d latest=%.6f (%s, max_abs=%.4f)",
            coin.ccy, len(state.funding_history_8h),
            latest_decimal, unit_note, max_abs,
        )


async def poll_oi_hourly_30d(
    cg: CoinglassSource,
    coin: CoinConfig,
    state: CoinState,
) -> None:
    """拉 30d × 1h OI 历史，缓存到 state.oi_hourly_history。

    用途：facts_collector 派生 oi.percentile_30d_hourly / is_near_local_high_7d。
    """
    try:
        data = await cg.fetch_oi_aggregated_history(
            coin.symbol_cg, interval="1h", limit=720,
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("[oi-hist-30d] %s fetch fail: %s", coin.ccy, e)
        state.poll_failures["oi_hourly_30d"] = "API调用失败"
        return

    if not data or not isinstance(data, list):
        return

    points: list[dict] = []
    for item in data:
        try:
            ts_raw = int(item.get("time", item.get("t", 0)))
            ts_sec = ts_raw // 1000 if ts_raw > 10_000_000_000 else ts_raw
            oi_usd = float(item.get("close", item.get("openInterest", item.get("value", 0))))
            if ts_sec > 0 and oi_usd > 0:
                points.append({"ts_sec": ts_sec, "oi_usd": oi_usd})
        except (TypeError, ValueError):
            continue

    if not points:
        return

    points.sort(key=lambda p: p["ts_sec"])
    state.oi_hourly_history.clear()
    for p in points[-720:]:
        state.oi_hourly_history.append(p)

    state.poll_failures.pop("oi_hourly_30d", None)
    if "oi_hist_30d_ready" not in state._log_once_keys:
        state._log_once_keys.add("oi_hist_30d_ready")
        logger.info(
            "[oi-hist-30d] %s ready | points=%d latest=$%s",
            coin.ccy, len(state.oi_hourly_history),
            f"{points[-1]['oi_usd']/1e9:.2f}B",
        )
