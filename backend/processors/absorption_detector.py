"""Absorption Detector · 从 Footprint buckets 识别价位级被动吸收带

「吸收」定义（订单流方法论经典概念）：
  某价位出现**大量真实成交**但**买卖接近均衡**，说明有被动挂单端在持续
  接单。这是**已成交事实**（CVD 硬证据），不可撤单，比订单墙（挂单意图）
  可靠得多。

输入：
  - state.footprint_contract（deque，近 3 根 1h bar，含价位 buckets）
  - state.footprint_spot （同上，现货兜底）
  - current_price（用于判 support/resistance）

输出：
  AbsorptionSnapshot（zones_support / zones_resistance + meta）

阈值（保守基线，上线后可根据回测调整）：
  · 保守：该 bar 的 buckets 按 vol 排序后取 top 20%，且 |delta_pct| < 0.20
  · 放宽（兜底）：top 30% + |delta_pct| < 0.30，fallback_used=True 告知
  · 绝对下限：vol ≥ $500k，防止冷清时段误判

合并策略：
  跨多根 bar 的同价位（容差 0.15%）合并为一个 zone：
  - taker_volume_usd = 累加
  - delta_pct_abs_avg = vol 加权平均
  - bar_count = 命中 bar 数（越大越可靠）
  - age_hours = 最近一次命中距今小时数

side 判定：
  bucket_mid < current_price → support
  bucket_mid > current_price → resistance
  （按"相对现价"的语境，与关键位系统一致）

双源策略：
  合约优先（Coinglass futures footprint 覆盖更广） → 合约 fallback →
  现货兜底（保守）→ 现货 fallback。单一 zone 同时只挂一个 source。
"""
from __future__ import annotations

import math
import time
from collections.abc import Iterable
from typing import Optional

from models.market_action import AbsorptionSnapshot, AbsorptionZone

# ── 保守阈值 ──
_VOL_TOP_PCT_STRICT = 0.20
_DELTA_PCT_ABS_STRICT = 0.20

# ── 兜底（放宽一档） ──
_VOL_TOP_PCT_LOOSE = 0.30
_DELTA_PCT_ABS_LOOSE = 0.30

# 绝对下限（防止冷清时段 bucket vol 太小引发噪声）
_MIN_VOL_USD = 500_000.0

# 合并同价位容差（相对现价，0.15%）
_MERGE_TOL_PCT = 0.0015

# 每侧最多输出 zones 数量
_MAX_ZONES_PER_SIDE = 8


def _bucket_mid(bucket: dict) -> float:
    return (float(bucket["price_lo"]) + float(bucket["price_hi"])) / 2.0


def _bucket_vol(bucket: dict) -> float:
    return max(
        0.0,
        float(bucket.get("buy_quote", 0.0)) + float(bucket.get("sell_quote", 0.0)),
    )


def _bucket_delta_pct_abs(bucket: dict) -> float:
    vol = _bucket_vol(bucket)
    if vol <= 0:
        return 1.0
    delta = float(bucket.get("buy_quote", 0.0)) - float(bucket.get("sell_quote", 0.0))
    return abs(delta) / vol


def _scan_bar(
    bar: dict,
    now_ts: int,
    vol_top_pct: float,
    delta_pct_abs_max: float,
) -> list[dict]:
    """扫描单 bar，返回命中阈值的 buckets（带派生字段）"""
    buckets = bar.get("buckets") or []
    if not buckets:
        return []
    sorted_by_vol = sorted(buckets, key=_bucket_vol, reverse=True)
    n_top = max(1, int(math.ceil(len(sorted_by_vol) * vol_top_pct)))
    top_buckets = sorted_by_vol[:n_top]

    try:
        bar_ts = int(bar.get("ts", now_ts))
    except (TypeError, ValueError):
        bar_ts = now_ts
    age_hours = max(0.0, (now_ts - bar_ts) / 3600.0)

    hits: list[dict] = []
    for b in top_buckets:
        vol = _bucket_vol(b)
        if vol < _MIN_VOL_USD:
            continue
        d_abs = _bucket_delta_pct_abs(b)
        if d_abs >= delta_pct_abs_max:
            continue
        hits.append({
            "price": _bucket_mid(b),
            "vol": vol,
            "delta_pct_abs": d_abs,
            "age_hours": age_hours,
            "bar_ts": bar_ts,
        })
    return hits


def _merge_zones(
    hits: list[dict],
    current_price: float,
    source: str,
) -> list[AbsorptionZone]:
    """把跨 bar 命中按价位合并为 AbsorptionZone 列表。"""
    if not hits:
        return []
    hits_sorted = sorted(hits, key=lambda h: h["price"])
    merged: list[dict] = []
    tol_abs = max(1.0, current_price * _MERGE_TOL_PCT)

    for h in hits_sorted:
        if merged and abs(h["price"] - merged[-1]["prices_last"]) <= tol_abs:
            m = merged[-1]
            m["prices"].append(h["price"])
            m["vol_list"].append(h["vol"])
            m["d_list"].append(h["delta_pct_abs"])
            m["bar_ts_set"].add(h["bar_ts"])
            m["age_min"] = min(m["age_min"], h["age_hours"])
            m["prices_last"] = h["price"]
        else:
            merged.append({
                "prices": [h["price"]],
                "vol_list": [h["vol"]],
                "d_list": [h["delta_pct_abs"]],
                "bar_ts_set": {h["bar_ts"]},
                "age_min": h["age_hours"],
                "prices_last": h["price"],
            })

    zones: list[AbsorptionZone] = []
    for m in merged:
        total_vol = sum(m["vol_list"])
        if total_vol <= 0:
            continue
        weighted_price = sum(p * v for p, v in zip(m["prices"], m["vol_list"])) / total_vol
        weighted_d = sum(d * v for d, v in zip(m["d_list"], m["vol_list"])) / total_vol
        side = "support" if weighted_price < current_price else "resistance"
        zones.append(AbsorptionZone(
            price=round(weighted_price, 2),
            side=side,
            taker_volume_usd=round(total_vol, 2),
            delta_pct_abs_avg=round(weighted_d, 4),
            bar_count=len(m["bar_ts_set"]),
            age_hours=round(m["age_min"], 2),
            source=source,  # type: ignore[arg-type]
        ))
    return zones


def _detect_from_source(
    bars: Iterable[dict],
    current_price: float,
    now_ts: int,
    vol_top_pct: float,
    delta_pct_abs_max: float,
    source: str,
) -> list[AbsorptionZone]:
    all_hits: list[dict] = []
    for bar in bars:
        all_hits.extend(_scan_bar(bar, now_ts, vol_top_pct, delta_pct_abs_max))
    return _merge_zones(all_hits, current_price, source)


def detect_absorption_zones(
    footprint_contract: Optional[Iterable[dict]],
    footprint_spot: Optional[Iterable[dict]],
    current_price: float,
    now_ts: Optional[int] = None,
    window_hours_default: float = 3.0,
) -> AbsorptionSnapshot:
    """识别价位级被动吸收带（合约优先，放宽兜底 → 现货兜底）。

    返回值永远不 None；当无法识别任何 zone 时返回空 snapshot，下游
    需自行处理"近 N h 无显著吸收带"的语义。
    """
    empty = AbsorptionSnapshot(
        window_hours=window_hours_default,
        lookback_bars=0,
    )
    if current_price <= 0:
        return empty

    now_ts = int(now_ts if now_ts is not None else time.time())

    contract_bars = list(footprint_contract or [])
    spot_bars = list(footprint_spot or [])
    lookback_bars = len(contract_bars) if contract_bars else len(spot_bars)

    if lookback_bars == 0:
        return empty

    # 推算实际覆盖窗口（以 bar 间距 1h 近似）
    ref_bars = contract_bars or spot_bars
    try:
        first_ts = min(int(b.get("ts", now_ts)) for b in ref_bars)
        window_hours = round((now_ts - first_ts) / 3600.0 + 1.0, 1)
        window_hours = max(1.0, window_hours)
    except (TypeError, ValueError):
        window_hours = window_hours_default

    fallback_used = False
    zones: list[AbsorptionZone] = []

    # Stage 1 · 合约 + 保守
    if contract_bars:
        zones = _detect_from_source(
            contract_bars, current_price, now_ts,
            _VOL_TOP_PCT_STRICT, _DELTA_PCT_ABS_STRICT, "contract",
        )

    # Stage 2 · 合约 + 放宽兜底
    if not zones and contract_bars:
        fallback_used = True
        zones = _detect_from_source(
            contract_bars, current_price, now_ts,
            _VOL_TOP_PCT_LOOSE, _DELTA_PCT_ABS_LOOSE, "contract",
        )

    # Stage 3 · 现货 + 保守（现货作为独立兜底数据源）
    if not zones and spot_bars:
        zones = _detect_from_source(
            spot_bars, current_price, now_ts,
            _VOL_TOP_PCT_STRICT, _DELTA_PCT_ABS_STRICT, "spot",
        )

    # Stage 4 · 现货 + 放宽
    if not zones and spot_bars:
        fallback_used = True
        zones = _detect_from_source(
            spot_bars, current_price, now_ts,
            _VOL_TOP_PCT_LOOSE, _DELTA_PCT_ABS_LOOSE, "spot",
        )

    support = sorted(
        (z for z in zones if z.side == "support"),
        key=lambda z: z.taker_volume_usd, reverse=True,
    )[:_MAX_ZONES_PER_SIDE]
    resistance = sorted(
        (z for z in zones if z.side == "resistance"),
        key=lambda z: z.taker_volume_usd, reverse=True,
    )[:_MAX_ZONES_PER_SIDE]

    strongest_sup = support[0] if support else None
    strongest_res = resistance[0] if resistance else None

    return AbsorptionSnapshot(
        zones_support=list(support),
        zones_resistance=list(resistance),
        total_zone_count=len(support) + len(resistance),
        strongest_support=strongest_sup,
        strongest_resistance=strongest_res,
        window_hours=window_hours,
        lookback_bars=lookback_bars,
        fallback_used=fallback_used,
    )
