"""NOFX 快照组装器：从 Engine 内存 CoinState 抽取"原始/统计类"字段。

设计契约（与 NOFX_SCHEMA.md 对齐）：
  1. 只读内存 state，不触发任何外部请求 / processor 重算
  2. 只返回"原始 + 统计"字段，剔除本项目所有"已下结论"的加工层：
       - trend_exhaustion / market_structure_* / direction_vote
       - temperature / range_signal / key_level_snapshot_v2
       - regime_snapshot / execution_plan / ai_trader_report / final_decision
       - waterfall / levels.sniper_entries / levels.ladder_plans
       - cvd_*_trend / funding_interpretation / taker_dominant / oi_trend
       - candlestick_pattern_*
  3. 所有缺失字段返回 None / [] / 0，永远不抛异常
  4. Schema 版本：`SCHEMA_VERSION` 常量；破坏性变更必升 major

字段命名约定：
  - 全部 snake_case
  - 时间戳统一 unix 秒
  - 金额统一 USD
"""

from __future__ import annotations

import time
from typing import Any, Optional

SCHEMA_VERSION = "1.1.0"


# ── 工具函数 ────────────────────────────────────────────────

def _normalize_ts(ts: Any) -> int:
    """统一归一化时间戳到 unix 秒。

    源头不一致：Binance K 线 / Coinglass whale / Hyperliquid 返回毫秒，
    其它 processor 返回秒。我们对外契约承诺秒，因此在 builder 层做"智能换算"：
    任何 ts > 1e12 视为毫秒（约合 2001 年后的毫秒都会大于这个阈值），÷1000。
    """
    try:
        v = int(ts or 0)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    if v > 10_000_000_000:  # ≈ 2286 年的秒数；任何超过此阈值的都是毫秒
        return v // 1000
    return v


def _safe_dump(obj: Any) -> Any:
    """pydantic / dict / 其它 -> dict；失败返回 None。"""
    if obj is None:
        return None
    try:
        return obj.model_dump()
    except AttributeError:
        pass
    if isinstance(obj, dict):
        return obj
    try:
        return dict(obj)
    except Exception:
        return None


def _compact_candle(c: Any) -> Optional[list]:
    """CandleData -> [ts, o, h, l, c, v]。用数组节省 JSON 体积。
    ts 统一输出秒（源头可能是毫秒）。
    """
    try:
        return [
            _normalize_ts(getattr(c, "ts", 0)),
            float(getattr(c, "open", 0) or 0),
            float(getattr(c, "high", 0) or 0),
            float(getattr(c, "low", 0) or 0),
            float(getattr(c, "close", 0) or 0),
            float(getattr(c, "vol", 0) or 0),
        ]
    except Exception:
        return None


def _tail_candles(candles: Any, limit: int) -> list[list]:
    if not candles:
        return []
    try:
        tail = list(candles)[-int(limit):]
    except Exception:
        return []
    out: list[list] = []
    for c in tail:
        row = _compact_candle(c)
        if row is not None:
            out.append(row)
    return out


def _last_ts(candles: Any) -> int:
    if not candles:
        return 0
    try:
        return _normalize_ts(getattr(candles[-1], "ts", 0))
    except Exception:
        return 0


def _age_sec(ts: Optional[int], now: int) -> Optional[int]:
    """data_age_sec 计算；ts 缺失或非法 -> None。自动识别毫秒并归一化。"""
    if not ts:
        return None
    try:
        age = now - _normalize_ts(ts)
        return age if age >= 0 else 0
    except Exception:
        return None


# ── 分块构造 ────────────────────────────────────────────────

def _build_candles(state: Any, candle_limit: dict[str, int]) -> dict[str, list]:
    """多周期 K 线，按 state 实际可用字段逐项裁剪。"""
    mapping = [
        ("15m", getattr(state, "candles_15m", None)),
        ("1h",  getattr(state, "candles_1h", None)),
        ("4h",  getattr(state, "candles_4h", None)),
        ("1d",  getattr(state, "candles_daily", None)),
        ("1w",  getattr(state, "candles_weekly", None)),
    ]
    out: dict[str, list] = {}
    for tf, arr in mapping:
        n = int(candle_limit.get(tf, 0) or 0)
        if n > 0:
            out[tf] = _tail_candles(arr, n)
    return out


def _build_cvd(state: Any) -> dict:
    def _one(cvd: Any) -> Optional[dict]:
        if cvd is None:
            return None
        series = getattr(cvd, "series", None) or []
        last = series[-1] if series else None
        # 只暴露原始数值：cumulative / delta_1h + 最近一条序列点的原始数值
        return {
            "delta_1h": float(getattr(cvd, "delta_1h", 0) or 0),
            "has_divergence": bool(getattr(cvd, "has_divergence", False)),
            "cumulative": float(getattr(last, "cvd", 0) or 0) if last else 0.0,
            "last_point": {
                "ts": _normalize_ts(getattr(last, "ts", 0)),
                "buy_vol": float(getattr(last, "buy_vol", 0) or 0),
                "sell_vol": float(getattr(last, "sell_vol", 0) or 0),
                "delta": float(getattr(last, "delta", 0) or 0),
            } if last else None,
            "series_len": len(series),
        }

    return {
        "contract": _one(getattr(state, "cvd_contract", None)),
        "spot": _one(getattr(state, "cvd_spot", None)),
    }


def _build_oi(state: Any) -> Optional[dict]:
    oi = getattr(state, "oi", None)
    if oi is None:
        return None
    history = getattr(oi, "history", None) or []
    recent = []
    try:
        for pt in list(history)[-30:]:
            recent.append({
                "ts": _normalize_ts(getattr(pt, "ts", 0)),
                "oi": float(getattr(pt, "oi", 0) or 0),
                "oi_usd": float(getattr(pt, "oi_usd", 0) or 0),
            })
    except Exception:
        recent = []
    rank_payload = getattr(state, "oi_exchange_rank", None) or {}
    by_exchange = rank_payload.get("exchanges", []) if isinstance(rank_payload, dict) else []
    return {
        "current_usd": float(getattr(oi, "current_usd", 0) or 0),
        "change_5m_pct": float(getattr(oi, "change_5m_pct", 0) or 0),
        "change_1h_pct": float(getattr(oi, "change_1h_pct", 0) or 0),
        "change_24h_pct": getattr(state, "oi_change_24h_pct", None),
        "history_30pts": recent,
        "by_exchange": list(by_exchange) if by_exchange else [],
    }


def _build_funding(state: Any) -> Optional[dict]:
    fr = getattr(state, "funding", None)
    mfr = getattr(state, "multi_funding", None)
    if fr is None and mfr is None:
        return None
    by_exchange: list[dict] = []
    avg_7d = None
    avg_current = None
    oi_weighted = None
    if mfr is not None:
        try:
            for ex in getattr(mfr, "exchanges", None) or []:
                by_exchange.append({
                    "exchange": getattr(ex, "exchange", "") or "",
                    "current": getattr(ex, "current", None),
                    "avg_3d": getattr(ex, "avg_3d", None),
                    "avg_7d": getattr(ex, "avg_7d", None),
                    "avg_30d": getattr(ex, "avg_30d", None),
                })
            avg_7d = float(getattr(mfr, "avg_7d", 0) or 0) or None
            avg_current = float(getattr(mfr, "avg_current", 0) or 0) or None
            oi_weighted = float(getattr(mfr, "oi_weighted", 0) or 0) or None
        except Exception:
            pass
    return {
        "okx": getattr(fr, "okx_rate", None) if fr else None,
        "binance": getattr(fr, "binance_rate", None) if fr else None,
        "avg_current": avg_current if avg_current is not None else (getattr(fr, "avg_rate", None) if fr else None),
        "avg_7d": avg_7d,
        "oi_weighted": oi_weighted,
        "next_funding_ts": int(getattr(fr, "next_funding_ts", 0) or 0) if fr else 0,
        "by_exchange": by_exchange,
    }


def _build_ls_ratio(state: Any) -> dict:
    def _one(ls: Any) -> Optional[dict]:
        if ls is None:
            return None
        exchanges = []
        try:
            for ex in getattr(ls, "exchanges", None) or []:
                exchanges.append({
                    "exchange": getattr(ex, "exchange", "") or "",
                    "long_pct": float(getattr(ex, "long_pct", 0) or 0),
                    "short_pct": float(getattr(ex, "short_pct", 0) or 0),
                    "ratio": float(getattr(ex, "ratio", 0) or 0),
                })
        except Exception:
            exchanges = []
        return {
            "cycle": getattr(ls, "cycle", "") or "",
            "dimension": getattr(ls, "dimension", "") or "",
            "avg_ratio": float(getattr(ls, "avg_ratio", 0) or 0),
            "by_exchange": exchanges,
        }

    out: dict[str, Any] = {
        "global": _one(getattr(state, "ls_ratio", None)),
        "top_account": _one(getattr(state, "ls_ratio_top_account", None)),
        "top_position": _one(getattr(state, "ls_ratio_top_position", None)),
    }
    # 顶层聚合（与前端面板一致的百分比字段）
    out["global_long_pct"] = getattr(state, "ls_ratio_long_pct", None)
    out["global_short_pct"] = getattr(state, "ls_ratio_short_pct", None)
    out["global_change_24h"] = getattr(state, "ls_ratio_change_24h", None)
    out["top_account_long_pct"] = getattr(state, "ls_top_acct_long_pct", None)
    out["top_account_short_pct"] = getattr(state, "ls_top_acct_short_pct", None)
    out["top_account_change_24h"] = getattr(state, "ls_top_acct_change_24h", None)
    return out


def _build_liq_map(liq_map: Any) -> Optional[dict]:
    if liq_map is None:
        return None
    d = _safe_dump(liq_map) or {}
    # 保留原始结构字段，剔除可能夹带的交互/解读字段（当前模型里没有，兼容未来）
    return {
        "ts": d.get("ts"),
        "cycle": d.get("cycle"),
        "exchange": d.get("exchange", ""),
        "imbalance_ratio": d.get("imbalance_ratio", 0),
        "clusters_above": d.get("clusters_above", []),
        "clusters_below": d.get("clusters_below", []),
        "vacuum_zones": d.get("vacuum_zones", []),
        "leverage_groups": d.get("leverage_groups", []),
    }


def _build_liq_heatmap(state: Any) -> Optional[dict]:
    # poll 层写入 key 已统一为 "24h"/"7d"（旧版 m1_24h 已废弃，理论不会再出现）
    heatmaps = getattr(state, "liq_heatmaps", None) or {}
    hm = heatmaps.get("24h") or heatmaps.get("7d")
    if hm is None:
        return None
    data = getattr(hm, "data", None) or []
    # 价格参考价（若无则 hotspots 仍返回，只是 pct_from_price = None）
    ticker = getattr(state, "ticker", None)
    price = float(getattr(ticker, "last", 0) or 0) if ticker else 0
    pts = sorted(data, key=lambda p: getattr(p, "value", 0) or 0, reverse=True)[:10]
    hotspots = []
    for pt in pts:
        p = float(getattr(pt, "price", 0) or 0)
        v = float(getattr(pt, "value", 0) or 0)
        pct = round((p - price) / price * 100, 4) if price > 0 else None
        hotspots.append({
            "price": p,
            "total_usd": v,
            "pct_from_price": pct,
            "ts": int(getattr(pt, "ts", 0) or 0),
        })
    return {
        "range": getattr(hm, "range", "") or "",
        "model": int(getattr(hm, "model", 0) or 0),
        "exchange": getattr(hm, "exchange", "") or "",
        "hotspots": hotspots,
        "points_total": len(data),
    }


def _build_liq_max_pain(state: Any, ccy: str) -> Optional[dict]:
    """组装 24h 清算最大痛点（仅当前币种）。

    数据源：state.liq_max_pain["24h"].items（已被 poll 层按 supported_coins 过滤）。
    返回字段含义见 NOFX_SCHEMA.md §3.x · liquidation_max_pain。
    """
    pain_dict = getattr(state, "liq_max_pain", None) or {}
    pain_data = pain_dict.get("24h")
    if pain_data is None:
        return None
    items = getattr(pain_data, "items", None) or []
    target = next((it for it in items if getattr(it, "symbol", "") == ccy), None)
    if target is None:
        return None

    ticker = getattr(state, "ticker", None)
    cur_price = float(getattr(ticker, "last", 0) or 0) if ticker else 0
    long_p = float(getattr(target, "long_pain_price", 0) or 0)
    long_u = float(getattr(target, "long_pain_usd", 0) or 0)
    short_p = float(getattr(target, "short_pain_price", 0) or 0)
    short_u = float(getattr(target, "short_pain_usd", 0) or 0)

    def _pct(p: float) -> Optional[float]:
        if cur_price <= 0 or p <= 0:
            return None
        return round((p - cur_price) / cur_price * 100, 4)

    return {
        "range": "24h",
        "current_price": float(getattr(target, "price", 0) or 0) or cur_price,
        "long_pain_price": long_p,
        "long_pain_usd": long_u,
        "long_pain_pct_from_price": _pct(long_p),
        "short_pain_price": short_p,
        "short_pain_usd": short_u,
        "short_pain_pct_from_price": _pct(short_p),
        "ts": int(getattr(pain_data, "ts", 0) or 0),
    }


def _build_orderbook(state: Any) -> Optional[dict]:
    ob = getattr(state, "orderbook", None)
    if ob is None:
        return None
    return {
        "bid_walls": [
            {
                "price": float(getattr(w, "price", 0) or 0),
                "size": float(getattr(w, "size", 0) or 0),
                "size_usd": float(getattr(w, "size_usd", 0) or 0),
                "order_count": int(getattr(w, "order_count", 0) or 0),
            }
            for w in (getattr(ob, "bid_walls", None) or [])[:10]
        ],
        "ask_walls": [
            {
                "price": float(getattr(w, "price", 0) or 0),
                "size": float(getattr(w, "size", 0) or 0),
                "size_usd": float(getattr(w, "size_usd", 0) or 0),
                "order_count": int(getattr(w, "order_count", 0) or 0),
            }
            for w in (getattr(ob, "ask_walls", None) or [])[:10]
        ],
        "bid_total_usd": float(getattr(ob, "bid_total_usd", 0) or 0),
        "ask_total_usd": float(getattr(ob, "ask_total_usd", 0) or 0),
        "spread_pct": float(getattr(ob, "spread_pct", 0) or 0),
    }


def _build_large_orders(state: Any) -> Optional[dict]:
    lo = getattr(state, "large_orders", None)
    if lo is None:
        return None
    orders = list(getattr(lo, "orders", None) or [])
    buy_count = sum(1 for o in orders if getattr(o, "side", "") == "bid")
    sell_count = sum(1 for o in orders if getattr(o, "side", "") == "ask")
    net_usd = 0.0
    for o in orders:
        side = getattr(o, "side", "")
        usd = float(getattr(o, "size_usd", 0) or 0)
        net_usd += usd if side == "bid" else (-usd)
    recent = []
    for o in sorted(orders, key=lambda x: float(getattr(x, "size_usd", 0) or 0), reverse=True)[:15]:
        recent.append({
            "ts": _normalize_ts(getattr(o, "ts", 0)),
            "exchange": getattr(o, "exchange", "") or "",
            "symbol": getattr(o, "symbol", "") or "",
            "side": getattr(o, "side", "") or "",
            "price": float(getattr(o, "price", 0) or 0),
            "size_usd": float(getattr(o, "size_usd", 0) or 0),
            "status": getattr(o, "status", "") or "",
        })
    return {
        "buy_count": buy_count,
        "sell_count": sell_count,
        "net_usd": net_usd,
        "total_bid_usd": float(getattr(lo, "total_bid_usd", 0) or 0),
        "total_ask_usd": float(getattr(lo, "total_ask_usd", 0) or 0),
        "recent": recent,
    }


def _build_whale(state: Any) -> Optional[dict]:
    whale = getattr(state, "whale_data", None)
    if whale is None:
        return None
    alerts = list(getattr(whale, "hl_alerts", None) or [])
    positions = list(getattr(whale, "hl_positions", None) or [])
    transfers = list(getattr(whale, "transfers", None) or [])

    # 转账资金流向：exchange 标签进/出
    # 注：上游有时会产生"空壳 transfer"（ts=0 amount_usd=0 仅有 from/to 标签），
    #     这里用 amount_usd > 0 过滤，避免 count 虚高而 net_usd=0 的割裂数据。
    inflow_usd = 0.0   # 转入交易所
    outflow_usd = 0.0  # 转出交易所
    EXCHANGE_LABELS = {"binance", "okx", "coinbase", "bybit", "kraken", "huobi", "kucoin", "bitfinex"}
    for t in transfers:
        amt = float(getattr(t, "amount_usd", 0) or 0)
        if amt <= 0:
            continue
        to_label = (getattr(t, "to_label", "") or "").lower()
        from_label = (getattr(t, "from_label", "") or "").lower()
        if "exchange" in to_label or any(k in to_label for k in EXCHANGE_LABELS):
            inflow_usd += amt
        if "exchange" in from_label or any(k in from_label for k in EXCHANGE_LABELS):
            outflow_usd += amt

    # 过滤掉 ts=0 且 amount_usd=0 的空壳转账（Coinglass 字段映射问题，见已知问题）
    valid_transfers = [
        t for t in transfers
        if _normalize_ts(getattr(t, "ts", 0)) > 0
        and float(getattr(t, "amount_usd", 0) or 0) > 0
    ]
    top_transfers = []
    for t in sorted(valid_transfers, key=lambda x: float(getattr(x, "amount_usd", 0) or 0), reverse=True)[:15]:
        top_transfers.append({
            "ts": _normalize_ts(getattr(t, "ts", 0)),
            "symbol": getattr(t, "symbol", "") or "",
            "amount": float(getattr(t, "amount", 0) or 0),
            "amount_usd": float(getattr(t, "amount_usd", 0) or 0),
            "from_label": getattr(t, "from_label", "") or "",
            "to_label": getattr(t, "to_label", "") or "",
            "blockchain": getattr(t, "blockchain", "") or "",
        })

    top_alerts = []
    for a in sorted(alerts, key=lambda x: _normalize_ts(getattr(x, "ts", 0)), reverse=True)[:20]:
        top_alerts.append({
            "ts": _normalize_ts(getattr(a, "ts", 0)),
            "symbol": getattr(a, "symbol", "") or "",
            "side": getattr(a, "side", "") or "",
            "action": getattr(a, "action", "") or "",
            "size_usd": float(getattr(a, "size_usd", 0) or 0),
            "entry_price": float(getattr(a, "entry_price", 0) or 0),
        })

    hl_positions = []
    for p in positions[:20]:
        hl_positions.append({
            "address": getattr(p, "address", "") or "",
            "symbol": getattr(p, "symbol", "") or "",
            "side": getattr(p, "side", "") or "",
            "size_usd": float(getattr(p, "size_usd", 0) or 0),
            "entry_price": float(getattr(p, "entry_price", 0) or 0),
            "unrealized_pnl": float(getattr(p, "unrealized_pnl", 0) or 0),
            "leverage": float(getattr(p, "leverage", 0) or 0),
        })

    return {
        "hl_alerts_count": len(alerts),
        "hl_positions_count": len(positions),
        "transfers_count": len(valid_transfers),
        "transfers_count_raw": len(transfers),
        "transfer_inflow_usd": round(inflow_usd, 2),
        "transfer_outflow_usd": round(outflow_usd, 2),
        "transfer_net_usd": round(inflow_usd - outflow_usd, 2),
        "hl_alerts_recent": top_alerts,
        "hl_positions": hl_positions,
        "top_transfers": top_transfers,
    }


def _build_etf(state: Any) -> Optional[dict]:
    etf = getattr(state, "etf_flow", None)
    if etf is None:
        return None
    recent = []
    for d in (getattr(etf, "recent_days", None) or [])[:10]:
        recent.append({
            "date": getattr(d, "date", "") or "",
            "total_net": float(getattr(d, "total_net", 0) or 0),
            "detail": getattr(d, "detail", None) or {},
        })
    return {
        "asset": getattr(etf, "asset", "") or "",
        "net_3d": float(getattr(etf, "net_3d", 0) or 0),
        "recent_days": recent,
    }


def _build_on_chain_cycle(state: Any) -> Optional[dict]:
    cp = getattr(state, "cycle_position", None)
    if cp is None:
        return None
    d = _safe_dump(cp) or {}
    # 剔除解读类字段 cps_label / price_vs_sth_label（保留原始数值）
    safe_keys = {
        "ts", "cps",
        "mvrv_z_score", "mvrv_z_contribution",
        "ahr999_value", "ahr999_contribution",
        "price_vs_200w_ratio", "price_vs_200w_contribution",
        "price_vs_sth_contribution",
        "pi_cycle_ratio", "pi_cycle_contribution",
        "rplr_proxy", "btc_rsi_daily",
        "sma_200w", "sth_cost_1d", "sth_cost_1w", "sth_cost_1m", "sth_cost_3m",
        "pi_350dma", "pi_111dma_x2", "cvdd",
    }
    return {k: d.get(k) for k in safe_keys if k in d}


def _build_options(state: Any) -> Optional[dict]:
    opt = getattr(state, "option_max_pain", None)
    info = getattr(state, "option_info", None)
    if opt is None and info is None:
        return None
    expiries: list[dict] = []
    nearest_mp = None
    nearest_expiry = ""
    nearest_call_oi = None
    nearest_put_oi = None
    nearest_pcr = None
    total_call_oi_sum = 0.0
    total_put_oi_sum = 0.0
    if opt is not None:
        try:
            for e in getattr(opt, "expiries", None) or []:
                call_oi = float(getattr(e, "call_oi", 0) or 0)
                put_oi = float(getattr(e, "put_oi", 0) or 0)
                raw_pcr = float(getattr(e, "put_call_ratio", 0) or 0)
                # 源头 put_call_ratio 常为 0（Coinglass 未返回），用 call/put OI 自算
                pcr = raw_pcr if raw_pcr > 0 else (round(put_oi / call_oi, 4) if call_oi > 0 else 0.0)
                expiries.append({
                    "expiry_date": getattr(e, "expiry_date", "") or "",
                    "max_pain_price": float(getattr(e, "max_pain_price", 0) or 0),
                    "call_oi": call_oi,
                    "put_oi": put_oi,
                    "put_call_ratio": pcr,
                })
                total_call_oi_sum += call_oi
                total_put_oi_sum += put_oi
            nearest_mp = getattr(opt, "nearest_max_pain", None)
            nearest_expiry = getattr(opt, "nearest_expiry", "") or ""
            if expiries:
                nearest_call_oi = expiries[0]["call_oi"]
                nearest_put_oi = expiries[0]["put_oi"]
                nearest_pcr = expiries[0]["put_call_ratio"]
        except Exception:
            pass

    # 顶层 OI 比率：优先用 OptionInfoData；为 0 时退回用 max_pain 表汇总估算
    pcr_oi_info = float(getattr(info, "put_call_oi_ratio", 0) or 0) if info else 0.0
    pcr_vol_info = float(getattr(info, "put_call_vol_ratio", 0) or 0) if info else 0.0
    if pcr_oi_info <= 0 and total_call_oi_sum > 0:
        pcr_oi_info = round(total_put_oi_sum / total_call_oi_sum, 4)

    return {
        "nearest_max_pain": nearest_mp,
        "nearest_expiry": nearest_expiry,
        "nearest_call_oi": nearest_call_oi,
        "nearest_put_oi": nearest_put_oi,
        "nearest_put_call_ratio": nearest_pcr,
        "expiries": expiries[:10],
        "total_oi_usd": float(getattr(info, "total_oi_usd", 0) or 0) if info else 0,
        "total_vol_24h_usd": float(getattr(info, "total_vol_24h_usd", 0) or 0) if info else 0,
        "put_call_oi_ratio": pcr_oi_info,
        "put_call_vol_ratio": pcr_vol_info,
        "iv_atm": getattr(info, "iv_atm", None) if info else None,
    }


def _build_macro(state: Any) -> dict:
    mi = getattr(state, "market_index", None)
    cb = getattr(state, "coinbase_premium", None)
    sm = getattr(state, "stablecoin_mcap", None)
    macro: dict[str, Any] = {
        "dxy": getattr(mi, "dxy", None) if mi else None,
        "dxy_change_pct": getattr(mi, "dxy_change_pct", None) if mi else None,
        "nasdaq": getattr(mi, "nasdaq", None) if mi else None,
        "nasdaq_change_pct": getattr(mi, "nasdaq_change_pct", None) if mi else None,
        "sp500": getattr(mi, "sp500", None) if mi else None,
        "sp500_change_pct": getattr(mi, "sp500_change_pct", None) if mi else None,
        "gold": getattr(mi, "gold", None) if mi else None,
        "gold_change_pct": getattr(mi, "gold_change_pct", None) if mi else None,
        "us_10y_yield": getattr(mi, "us_10y_yield", None) if mi else None,
        "fed_rate": getattr(mi, "fed_rate", None) if mi else None,
        "fear_greed": getattr(mi, "fear_greed", None) if mi else None,
        "fear_greed_prev": getattr(state, "fear_greed_prev", None),
        "btc_dominance": getattr(mi, "btc_dominance", None) if mi else None,
        "stablecoin_dominance": getattr(mi, "stablecoin_dominance", None) if mi else None,
        "usdt_market_cap": getattr(mi, "usdt_market_cap", None) if mi else None,
        "btc_hashrate": getattr(mi, "btc_hashrate", None) if mi else None,
        "btc_hist_vol": getattr(mi, "btc_hist_vol", None) if mi else None,
        "btc_implied_vol": getattr(mi, "btc_implied_vol", None) if mi else None,
        "btc_iv_skew_1m": getattr(mi, "btc_iv_skew_1m", None) if mi else None,
        "btc_put_call_oi": getattr(mi, "btc_put_call_oi", None) if mi else None,
        "btc_mvrv": getattr(mi, "btc_mvrv", None) if mi else None,
        "ahr999": getattr(mi, "ahr999", None) if mi else None,
        "coinbase_btc_premium_mi": getattr(mi, "coinbase_btc_premium", None) if mi else None,
        "usdt_otc_premium": getattr(mi, "usdt_otc_premium", None) if mi else None,
        "okx_ls_ratio_btc": getattr(mi, "okx_ls_ratio_btc", None) if mi else None,
        "binance_ls_ratio_btc": getattr(mi, "binance_ls_ratio_btc", None) if mi else None,
    }
    if cb is not None:
        macro["coinbase_premium_current"] = float(getattr(cb, "current_premium", 0) or 0)
    if sm is not None:
        macro["stablecoin_total_mcap"] = float(getattr(sm, "current_total", 0) or 0)
        # 7d 变化百分比：取最近一条 vs 7 天前
        try:
            hist = list(getattr(sm, "history", None) or [])
            if len(hist) >= 2:
                cur = float(getattr(sm, "current_total", 0) or 0)
                oldest = hist[0]
                old_val = float(getattr(oldest, "total_mcap", 0) or 0)
                if old_val > 0:
                    macro["stablecoin_7d_change_pct"] = round((cur - old_val) / old_val * 100, 3)
        except Exception:
            pass
    return macro


def _build_taker(state: Any) -> Optional[dict]:
    tf = getattr(state, "taker_flow", None)
    if tf is None:
        return None
    return {
        "buy_ratio": float(getattr(tf, "buy_ratio", 0) or 0),
        "sell_ratio": float(getattr(tf, "sell_ratio", 0) or 0),
        "spot_buy_vol": float(getattr(tf, "spot_buy_vol", 0) or 0),
        "spot_sell_vol": float(getattr(tf, "spot_sell_vol", 0) or 0),
        "contract_buy_vol": float(getattr(tf, "contract_buy_vol", 0) or 0),
        "contract_sell_vol": float(getattr(tf, "contract_sell_vol", 0) or 0),
        "spot_contract_divergence": bool(getattr(tf, "spot_contract_divergence", False)),
    }


def _build_volume_profile(state: Any) -> Optional[dict]:
    vp = getattr(state, "vp", None)
    if vp is None:
        return None
    return {
        "poc": float(getattr(vp, "poc_price", 0) or 0),
        "value_area_high": float(getattr(vp, "value_area_high", 0) or 0),
        "value_area_low": float(getattr(vp, "value_area_low", 0) or 0),
        "vwap": float(getattr(vp, "vwap", 0) or 0),
    }


def _build_liq_stats(state: Any) -> Optional[dict]:
    ls = getattr(state, "liq_stats", None)
    gl = getattr(state, "global_liq", None)
    if ls is None and gl is None:
        return None
    out: dict[str, Any] = {}
    if ls is not None:
        out["recent_24h"] = {
            "long_usd": float(getattr(ls, "long_total_usd", 0) or 0),
            "short_usd": float(getattr(ls, "short_total_usd", 0) or 0),
            "long_count": int(getattr(ls, "long_count", 0) or 0),
            "short_count": int(getattr(ls, "short_count", 0) or 0),
            "ratio": float(getattr(ls, "ratio", 0) or 0),
            "period_min": int(getattr(ls, "period_min", 0) or 0),
        }
    if gl is not None:
        out["global"] = {
            "long_1h_usd": float(getattr(gl, "long_1h_usd", 0) or 0),
            "short_1h_usd": float(getattr(gl, "short_1h_usd", 0) or 0),
            "long_24h_usd": float(getattr(gl, "long_24h_usd", 0) or 0),
            "short_24h_usd": float(getattr(gl, "short_24h_usd", 0) or 0),
            "ratio_1h": float(getattr(gl, "ratio_1h", 0) or 0),
            "ratio_24h": float(getattr(gl, "ratio_24h", 0) or 0),
            "largest_single_usd": float(getattr(gl, "largest_single_usd", 0) or 0),
        }
    return out


def _build_sweeps(state: Any, now: int) -> list[dict]:
    """最近 1h 内的流动性扫取事件（原始事件，NOFX 明确要求保留）。"""
    events = getattr(state, "liq_sweep_events", None) or []
    cutoff = now - 3600
    out: list[dict] = []
    try:
        for e in list(events):
            if not isinstance(e, dict):
                continue
            ts_sec = _normalize_ts(e.get("ts", 0))
            if ts_sec <= cutoff:
                continue
            out.append({
                "ts": ts_sec,
                "side": e.get("side", ""),
                "usd": float(e.get("usd", 0) or 0),
                "price": float(e.get("price", 0) or 0),
                "cluster_price": e.get("cluster_price"),
                "cluster_distance_pct": e.get("cluster_distance_pct"),
            })
    except Exception:
        pass
    return out


def _build_net_position_and_td(state: Any) -> dict:
    """保留原始数值，剔除 trend/direction 这类解读字符串。"""
    return {
        "net_position_latest": getattr(state, "net_position_latest", None),
        "net_position_change_24h": getattr(state, "net_position_change_24h", None),
        "futures_coin_netflow_1h": getattr(state, "futures_coin_netflow_1h", None),
        "td_sequential_count": getattr(state, "td_sequential_count", None),
    }


def _build_news(state: Any) -> dict:
    """仅返回简报"结论"字段（tldr_cn + version + trigger + coverage），
    不返回全文 sections/tracked_themes（按用户确认：只给结论）。
    叙事追踪保留"上游标签"（name_cn + direction + intensity）供 NOFX 识别主题。
    """
    out: dict[str, Any] = {
        "brief": None,
        "geo_risk": None,
        "active_narratives": [],
    }
    try:
        from processors.news_brief import get_current_brief
        brief = get_current_brief()
        if brief is not None:
            out["brief"] = {
                "version": int(getattr(brief, "version", 0) or 0),
                "updated_at": int(getattr(brief, "updated_at", 0) or 0),
                "coverage_hours": float(getattr(brief, "coverage_hours", 0) or 0),
                "tldr_cn": getattr(brief, "tldr_cn", "") or "",
                "update_trigger": getattr(brief, "update_trigger", "") or "",
                "based_on_events_count": int(getattr(brief, "based_on_events_count", 0) or 0),
                "model_used": getattr(brief, "model_used", "") or "",
            }
    except Exception:
        pass
    try:
        from processors.geo_risk_tracker import get_geo_risk_tracker
        geo = get_geo_risk_tracker().get_overview()
        if geo is not None:
            out["geo_risk"] = {
                "ts": int(getattr(geo, "ts", 0) or 0),
                "overall_level": int(getattr(geo, "overall_level", 0) or 0),
                "overall_label": getattr(geo, "overall_label", "") or "",
                "overall_summary_cn": getattr(geo, "overall_summary_cn", "") or "",
                "escalation_count_24h": int(getattr(geo, "escalation_count_24h", 0) or 0),
                "de_escalation_count_24h": int(getattr(geo, "de_escalation_count_24h", 0) or 0),
                "has_blackswan_24h": bool(getattr(geo, "has_blackswan_24h", False)),
            }
    except Exception:
        pass
    try:
        from processors.narrative_tracker import get_narrative_tracker
        briefs = get_narrative_tracker().get_active_briefs(limit=10)
        for t in briefs or []:
            out["active_narratives"].append({
                "theme_id": getattr(t, "theme_id", "") or "",
                "theme_name_cn": getattr(t, "theme_name_cn", "") or "",
                "category": getattr(t, "category", "") or "",
                "latest_event_ts": int(getattr(t, "latest_event_ts", 0) or 0),
                "flip_flop_count_24h": int(getattr(t, "flip_flop_count_24h", 0) or 0),
                "current_intensity": int(getattr(t, "current_intensity", 0) or 0),
                "current_direction_bias": getattr(t, "current_direction_bias", "") or "",
                "avg_abs_reaction_pct": float(getattr(t, "avg_abs_reaction_pct", 0) or 0),
                "hit_rate": float(getattr(t, "hit_rate", 0) or 0),
            })
    except Exception:
        pass
    return out


# ── 主入口 ──────────────────────────────────────────────────

def build_nofx_snapshot(
    state: Any,
    symbol_pair: str,
    candle_limit: dict[str, int],
    source_health: Optional[list[dict]] = None,
) -> dict:
    """从 CoinState 组装 NOFX 快照。
    约定：state 至少有 .coin 与 .ticker（调用方应先校验），否则返回 ready=False。
    """
    now = int(time.time())
    ticker = getattr(state, "ticker", None)
    if ticker is None:
        return {
            "schema_version": SCHEMA_VERSION,
            "ready": False,
            "ts": now,
            "coin": getattr(state, "coin", ""),
            "symbol": symbol_pair,
            "reason": "ticker not ready",
        }

    liq_maps = getattr(state, "liq_maps", None) or {}

    # 各分块构造
    snapshot: dict[str, Any] = {
        "candles": _build_candles(state, candle_limit),
        "cvd": _build_cvd(state),
        "oi": _build_oi(state),
        "funding": _build_funding(state),
        "long_short_ratio": _build_ls_ratio(state),
        "liquidation_map": {
            "24h": _build_liq_map(liq_maps.get("1d") or liq_maps.get("24h")),
            "7d": _build_liq_map(liq_maps.get("7d")),
            "30d": _build_liq_map(liq_maps.get("30d")),
        },
        "liquidation_heatmap": _build_liq_heatmap(state),
        "liquidation_max_pain": _build_liq_max_pain(state, getattr(state, "coin", "")),
        "liquidation_stats": _build_liq_stats(state),
        "recent_sweeps_1h": _build_sweeps(state, now),
        "orderbook": _build_orderbook(state),
        "large_orders": _build_large_orders(state),
        "whale": _build_whale(state),
        "etf": _build_etf(state),
        "on_chain_cycle": _build_on_chain_cycle(state),
        "options": _build_options(state),
        "taker_volume": _build_taker(state),
        "volume_profile": _build_volume_profile(state),
        "atr_14": float(getattr(state, "atr", 0) or 0),
        "net_position_td": _build_net_position_and_td(state),
        "macro": _build_macro(state),
        "news": _build_news(state),
    }

    # 数据时效自检：每维度最新更新到现在的秒数（None = 该维度缺失）
    ticker_ts = int(getattr(ticker, "ts", 0) or 0)
    cp = getattr(state, "cycle_position", None)
    etf_f = getattr(state, "etf_flow", None)
    mi = getattr(state, "market_index", None)
    opt = getattr(state, "option_max_pain", None)
    whale = getattr(state, "whale_data", None)
    news_brief_ts = snapshot["news"]["brief"]["updated_at"] if snapshot["news"]["brief"] else 0
    liq_map_24h = liq_maps.get("1d") or liq_maps.get("24h")
    heatmaps = getattr(state, "liq_heatmaps", None) or {}
    hm_obj = heatmaps.get("24h") or heatmaps.get("7d")
    pain_dict = getattr(state, "liq_max_pain", None) or {}
    pain_24h_obj = pain_dict.get("24h")

    def _model_ts(obj: Any) -> int:
        if obj is None:
            return 0
        return int(getattr(obj, "ts", 0) or 0)

    def _cvd_last_ts(obj: Any) -> int:
        if obj is None:
            return 0
        series = getattr(obj, "series", None) or []
        if not series:
            return 0
        return int(getattr(series[-1], "ts", 0) or 0)

    data_age_sec = {
        "ticker": _age_sec(ticker_ts, now),
        "candles_15m": _age_sec(_last_ts(getattr(state, "candles_15m", None)), now),
        "candles_1h": _age_sec(_last_ts(getattr(state, "candles_1h", None)), now),
        "candles_4h": _age_sec(_last_ts(getattr(state, "candles_4h", None)), now),
        "candles_1d": _age_sec(_last_ts(getattr(state, "candles_daily", None)), now),
        "candles_1w": _age_sec(_last_ts(getattr(state, "candles_weekly", None)), now),
        "oi": _age_sec(_model_ts(getattr(state, "oi", None)), now),
        "funding": _age_sec(_model_ts(getattr(state, "funding", None)), now),
        "cvd_contract": _age_sec(_cvd_last_ts(getattr(state, "cvd_contract", None)), now),
        "cvd_spot": _age_sec(_cvd_last_ts(getattr(state, "cvd_spot", None)), now),
        "liquidation_map_24h": _age_sec(_model_ts(liq_map_24h), now),
        "liquidation_heatmap": _age_sec(_model_ts(hm_obj), now),
        "liquidation_max_pain": _age_sec(_model_ts(pain_24h_obj), now),
        "orderbook": _age_sec(_model_ts(getattr(state, "orderbook", None)), now),
        "long_short_ratio": _age_sec(_model_ts(getattr(state, "ls_ratio", None)), now),
        "taker_volume": _age_sec(_model_ts(getattr(state, "taker_flow", None)), now),
        "whale": _age_sec(_model_ts(whale), now),
        "etf": _age_sec(_model_ts(etf_f), now),
        "on_chain_cycle": _age_sec(_model_ts(cp), now),
        "options": _age_sec(_model_ts(opt), now),
        "macro": _age_sec(_model_ts(mi), now),
        "news_brief": _age_sec(news_brief_ts, now),
    }

    return {
        "schema_version": SCHEMA_VERSION,
        "ready": True,
        "ts": now,
        "coin": getattr(state, "coin", "") or "",
        "symbol": symbol_pair,
        "price": float(getattr(ticker, "last", 0) or 0),
        "high_24h": float(getattr(ticker, "high_24h", 0) or 0),
        "low_24h": float(getattr(ticker, "low_24h", 0) or 0),
        "vol_24h": float(getattr(ticker, "vol_24h", 0) or 0),
        "change_pct_24h": float(getattr(ticker, "change_pct_24h", 0) or 0),
        "snapshot": snapshot,
        "data_age_sec": data_age_sec,
        "source_health": source_health or [],
    }
