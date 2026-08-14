"""Deterministic, beginner-friendly interpretation for Trading Brain context."""

from __future__ import annotations

import time
from typing import Any, Optional

from models.data_meta import DataMeta
from models.trading_brain import BrainMarketRead
from processors.market_facts import build_funding_snapshot, build_price_snapshot
from processors.market_thresholds import (
    FUNDING_LONG_CROWDED_PCT,
    FUNDING_SHORT_CROWDED_PCT,
    OI_STRONG_CHANGE_PCT,
)
from utils.time_series import normalize_epoch_seconds


_CONTEXT_TTL_SEC = 600
_OI_DIRECTION_DEADBAND_PCT = 0.2


def _meta(
    *, ts: object, now_ts: int, source: str, pending: bool = False,
) -> DataMeta:
    as_of = normalize_epoch_seconds(ts)
    if as_of <= 0:
        return DataMeta(status="missing", source=source)
    age = max(0, now_ts - as_of)
    if age > _CONTEXT_TTL_SEC:
        status = "stale"
    elif pending:
        status = "pending"
    else:
        status = "fresh"
    return DataMeta(
        as_of=as_of,
        staleness_sec=age,
        status=status,
        pending_reason="当前5分钟数据尚未收盘" if pending else "",
        source=source,
    )


def _cvd_meta(cvd: Any, *, now_ts: int, source: str) -> DataMeta:
    series = list(getattr(cvd, "series", []) or []) if cvd is not None else []
    ts = getattr(cvd, "ts", 0) if cvd is not None else 0
    if not ts and series:
        ts = getattr(series[-1], "ts", 0)
    ts_sec = normalize_epoch_seconds(ts)
    pending = bool(ts_sec and now_ts + 30 < ts_sec + 300)
    return _meta(ts=ts_sec, now_ts=now_ts, source=source, pending=pending)


def _direction(value: Optional[float]) -> int:
    if value is None or abs(value) < _OI_DIRECTION_DEADBAND_PCT:
        return 0
    return 1 if value > 0 else -1


def _rank_oi(state: Any, *, now_ts: int) -> tuple[Optional[float], DataMeta]:
    rank = getattr(state, "oi_exchange_rank", None) or {}
    all_agg = rank.get("all_aggregated") or {}
    try:
        value = float(all_agg["change_1h_pct"])
    except (KeyError, TypeError, ValueError):
        value = None
    meta = _meta(
        ts=rank.get("ts", 0), now_ts=now_ts,
        source="coinglass-oi-exchange-list",
    )
    if value is None:
        meta = DataMeta(status="missing", source=meta.source)
    return value, meta


def _local_oi(state: Any, *, now_ts: int) -> tuple[Optional[float], DataMeta]:
    oi = getattr(state, "oi", None)
    try:
        value = float(oi.change_1h_pct) if oi is not None else None
    except (TypeError, ValueError):
        value = None
    meta = _meta(
        ts=getattr(oi, "ts", 0), now_ts=now_ts,
        source="coinglass-oi-history",
    )
    if value is None:
        meta = DataMeta(status="missing", source=meta.source)
    return value, meta


def extract_market_read_inputs(state: Any, *, now_ts: Optional[int] = None) -> dict[str, Any]:
    """Read existing CoinState only; this function performs no network calls."""
    now = int(time.time() if now_ts is None else now_ts)
    spot = getattr(state, "cvd_spot", None)
    contract = getattr(state, "cvd_contract", None)
    spot_meta = _cvd_meta(spot, now_ts=now, source="coinglass-spot-cvd")
    contract_meta = _cvd_meta(contract, now_ts=now, source="coinglass-contract-cvd")

    rank_oi, rank_meta = _rank_oi(state, now_ts=now)
    local_oi, local_meta = _local_oi(state, now_ts=now)
    rank_usable = rank_meta.status in ("fresh", "pending")
    local_usable = local_meta.status in ("fresh", "pending")
    oi_value = rank_oi if rank_usable else local_oi if local_usable else None
    oi_meta = rank_meta if rank_usable else local_meta
    oi_conflict = bool(
        rank_usable and local_usable
        and _direction(rank_oi) != 0 and _direction(local_oi) != 0
        and _direction(rank_oi) != _direction(local_oi)
    )

    funding_fact = build_funding_snapshot(state)
    funding_obj = getattr(state, "multi_funding", None) or getattr(state, "funding", None)
    funding_meta = _meta(
        ts=getattr(funding_obj, "ts", 0), now_ts=now,
        source="binance-okx-funding",
    )
    funding_decimal = funding_fact.avg_current if funding_fact is not None else None
    funding_pct = float(funding_decimal) * 100.0 if funding_decimal is not None else None
    if funding_pct is None:
        funding_meta = DataMeta(status="missing", source=funding_meta.source)

    price_fact = build_price_snapshot(state)
    return {
        "cvd_spot_trend": getattr(spot, "trend_1h", "") or "",
        "cvd_contract_trend": getattr(contract, "trend_1h", "") or "",
        "oi_delta_1h_pct": oi_value,
        "oi_conflict": oi_conflict,
        "funding_rate_8h_pct": funding_pct,
        "funding_interpretation": (
            getattr(funding_obj, "interpretation", "") if funding_obj is not None else ""
        ) or "",
        "price_change_1h_pct": (
            price_fact.change_1h_pct if price_fact is not None else None
        ),
        "source_meta": {
            "cvd_spot": spot_meta,
            "cvd_contract": contract_meta,
            "oi": oi_meta,
            "funding": funding_meta,
        },
    }


def build_market_read(
    *,
    cvd_spot_trend: str,
    cvd_contract_trend: str,
    oi_delta_1h_pct: Optional[float],
    funding_rate_8h_pct: Optional[float],
    source_meta: dict[str, DataMeta],
    oi_conflict: bool = False,
    price_change_1h_pct: Optional[float] = None,
) -> BrainMarketRead:
    """Compose a transparent interpretation without allowing OI/Funding to flip flow."""
    spot_meta = source_meta.get("cvd_spot", DataMeta(status="missing"))
    fut_meta = source_meta.get("cvd_contract", DataMeta(status="missing"))
    oi_meta = source_meta.get("oi", DataMeta(status="missing"))
    funding_meta = source_meta.get("funding", DataMeta(status="missing"))
    usable = {"fresh", "pending"}
    cautions = ["偏多/偏空只表示当前证据倾向，不代表现在可以买入或卖出。"]
    evidence: list[str] = []

    if spot_meta.status not in usable or fut_meta.status not in usable:
        missing = []
        if spot_meta.status not in usable:
            missing.append("现货资金流")
        if fut_meta.status not in usable:
            missing.append("合约资金流")
        return BrainMarketRead(
            bias="insufficient",
            evidence_grade="insufficient",
            title="证据不足 · 等待数据",
            summary=f"{'、'.join(missing)}缺失或过期，暂不判断偏多偏空。",
            flow_state="insufficient",
            leverage_state="unavailable" if oi_meta.status not in usable else "small_change",
            funding_state="unavailable" if funding_meta.status not in usable else "neutral",
            cautions=cautions,
        )

    flow_map = {
        ("rising", "rising"): ("aligned_buy", "bullish", "买盘同向 · 偏多", "短线偏多"),
        ("declining", "declining"): ("aligned_sell", "bearish", "卖盘同向 · 偏空", "短线偏空"),
        ("rising", "declining"): (
            "spot_strong_split", "bullish", "资金流分化 · 现货偏强", "短线分化偏多",
        ),
        ("declining", "rising"): (
            "spot_weak_split", "bearish", "资金流分化 · 现货偏弱", "短线分化偏空",
        ),
    }
    mapped = flow_map.get((cvd_spot_trend, cvd_contract_trend))
    if mapped is None:
        flow_state, bias, title, bias_text = "unclear", "neutral", "方向不清 · 等待", "方向暂不清楚"
    else:
        flow_state, bias, title, bias_text = mapped

    spot_text = "主动买入" if cvd_spot_trend == "rising" else "主动卖出" if cvd_spot_trend == "declining" else "方向不明"
    fut_text = "主动买入" if cvd_contract_trend == "rising" else "主动卖出" if cvd_contract_trend == "declining" else "方向不明"
    evidence.append(f"现货{spot_text}；合约{fut_text}")

    if oi_conflict:
        leverage_state = "conflict"
        leverage_text = "OI 两种现有口径方向冲突，暂不判断杠杆变化"
        cautions.append("OI 聚合接口与本地固定窗口方向冲突，已停止给出杠杆结论。")
    elif oi_meta.status not in usable or oi_delta_1h_pct is None:
        leverage_state = "unavailable"
        leverage_text = "OI 不可用，无法判断杠杆"
    elif oi_delta_1h_pct <= -OI_STRONG_CHANGE_PCT:
        leverage_state = "deleveraging"
        leverage_text = f"OI 1h {oi_delta_1h_pct:+.3f}%，持仓明显减少，杠杆退潮"
    elif oi_delta_1h_pct >= OI_STRONG_CHANGE_PCT:
        leverage_state = "leverage_building"
        leverage_text = f"OI 1h {oi_delta_1h_pct:+.3f}%，持仓明显增加，杠杆升温"
    else:
        leverage_state = "small_change"
        move = "小幅减少" if oi_delta_1h_pct < 0 else "小幅增加" if oi_delta_1h_pct > 0 else "变化不大"
        leverage_text = (
            f"OI 1h {oi_delta_1h_pct:+.3f}%，持仓{move}，未达到明显升温/退潮阈值"
        )
    evidence.append(leverage_text)

    if funding_meta.status not in usable or funding_rate_8h_pct is None:
        funding_state = "unavailable"
        funding_text = "Funding 不可用，无法判断拥挤度"
    elif funding_rate_8h_pct >= FUNDING_LONG_CROWDED_PCT:
        funding_state = "long_crowded"
        funding_text = f"Funding {funding_rate_8h_pct:+.4f}%/8h，多头拥挤，追多风险升高"
        cautions.append("多头资金成本偏高，方向即使偏多也要防拥挤回撤。")
    elif funding_rate_8h_pct <= FUNDING_SHORT_CROWDED_PCT:
        funding_state = "short_crowded"
        funding_text = f"Funding {funding_rate_8h_pct:+.4f}%/8h，空头拥挤，存在轧空风险"
        cautions.append("空头资金成本偏高，方向即使偏空也要防轧空反弹。")
    else:
        funding_state = "neutral"
        funding_text = f"Funding {funding_rate_8h_pct:+.4f}%/8h，资金费中性"
    evidence.append(funding_text)

    provisional = spot_meta.status == "pending" or fut_meta.status == "pending"
    if provisional:
        cautions.append("当前5分钟数据尚未收盘，结论仍可能变化。")
    if price_change_1h_pct is not None:
        evidence.append(f"价格 1h {price_change_1h_pct:+.3f}%，仅作仓位变化背景")

    if flow_state == "unclear":
        grade = "weak"
    elif provisional or leverage_state in ("unavailable", "conflict") or funding_state == "unavailable":
        grade = "weak"
    elif flow_state in ("spot_strong_split", "spot_weak_split"):
        grade = "medium"
    else:
        grade = "strong"

    if flow_state == "spot_strong_split" and leverage_state == "deleveraging":
        title = "现货偏强 · 杠杆退潮"

    summary = f"{evidence[0]}；{leverage_text}；{funding_text}。现有证据{bias_text}，不构成交易指令。"
    return BrainMarketRead(
        bias=bias,
        evidence_grade=grade,
        title=title,
        summary=summary,
        flow_state=flow_state,
        leverage_state=leverage_state,
        funding_state=funding_state,
        evidence=evidence,
        cautions=cautions,
    )


def build_market_read_from_state(state: Any, *, now_ts: Optional[int] = None) -> dict[str, Any]:
    inputs = extract_market_read_inputs(state, now_ts=now_ts)
    read = build_market_read(
        cvd_spot_trend=inputs["cvd_spot_trend"],
        cvd_contract_trend=inputs["cvd_contract_trend"],
        oi_delta_1h_pct=inputs["oi_delta_1h_pct"],
        funding_rate_8h_pct=inputs["funding_rate_8h_pct"],
        source_meta=inputs["source_meta"],
        oi_conflict=inputs["oi_conflict"],
        price_change_1h_pct=inputs["price_change_1h_pct"],
    )
    return {**inputs, "market_read": read}
