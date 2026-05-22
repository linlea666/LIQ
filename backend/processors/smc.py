"""Independent SMC / smart-money processor.

This module only consumes raw market state fields and Nansen cache fields.  It
does not depend on existing opinion layers, so the SMC result stays auditable.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Iterable, Literal, Optional

from models.smc import (
    SMCConfirmation,
    SMCContradiction,
    SMCDataQuality,
    SMCFieldMapItem,
    SMCHorizon,
    SMCLiquidityPool,
    SMCMarketBreadth,
    SMCNansenFlow,
    SMCNansenPerp,
    SMCSmartMoneyContext,
    SMCSnapshot,
    SMCStructureEvent,
    SMCTargetZone,
    SMCZone,
)


@dataclass(frozen=True)
class _Bar:
    ts: int
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0


@dataclass(frozen=True)
class _Swing:
    kind: Literal["high", "low"]
    ts: int
    price: float
    index: int
    strength: float


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    if obj is None:
        return default
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj.get(name)
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _bars(raw: Iterable[Any] | None) -> list[_Bar]:
    out: list[_Bar] = []
    for item in raw or []:
        ts = _int(_attr(item, "ts", "t", default=0))
        o = _num(_attr(item, "open", "o", default=0))
        h = _num(_attr(item, "high", "h", default=0))
        l = _num(_attr(item, "low", "l", default=0))
        c = _num(_attr(item, "close", "c", default=0))
        v = _num(_attr(item, "vol", "volume", "v", default=0))
        if ts and h > 0 and l > 0 and c > 0:
            out.append(_Bar(ts=ts, o=o or c, h=h, l=l, c=c, v=v))
    out.sort(key=lambda b: b.ts)
    return out


def _last_price(state: Any, bars: list[_Bar]) -> float:
    ticker = _attr(state, "ticker")
    price = _num(_attr(ticker, "last", "price", default=0))
    if price > 0:
        return price
    return bars[-1].c if bars else 0.0


def _pct(price: float, base: float) -> float:
    return round((price - base) / base * 100, 4) if base else 0.0


def _zone_id(*parts: Any) -> str:
    raw = "|".join(str(p) for p in parts)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _atr(bars: list[_Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    prev = bars[0].c
    for b in bars[1:]:
        trs.append(max(b.h - b.l, abs(b.h - prev), abs(b.l - prev)))
        prev = b.c
    sample = trs[-period:] if len(trs) >= period else trs
    return sum(sample) / len(sample) if sample else 0.0


def _detect_swings(bars: list[_Bar], wing: int = 2, min_gap_pct: float = 0.08) -> list[_Swing]:
    if len(bars) < wing * 2 + 3:
        return []
    swings: list[_Swing] = []
    for idx in range(wing, len(bars) - wing):
        b = bars[idx]
        left = bars[idx - wing:idx]
        right = bars[idx + 1:idx + wing + 1]
        if all(b.h >= x.h for x in left + right):
            edge = max([x.h for x in left + right] or [b.h])
            strength = abs(b.h - edge) / b.h * 100 if b.h else 0
            if strength >= min_gap_pct:
                swings.append(_Swing("high", b.ts, b.h, idx, strength))
        if all(b.l <= x.l for x in left + right):
            edge = min([x.l for x in left + right] or [b.l])
            strength = abs(edge - b.l) / b.l * 100 if b.l else 0
            if strength >= min_gap_pct:
                swings.append(_Swing("low", b.ts, b.l, idx, strength))
    swings.sort(key=lambda s: (s.index, 0 if s.kind == "low" else 1))
    return swings


def _classify_bias(swings: list[_Swing]) -> Literal["bullish", "bearish", "neutral"]:
    highs = [s for s in swings if s.kind == "high"][-3:]
    lows = [s for s in swings if s.kind == "low"][-3:]
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1].price > highs[-2].price and lows[-1].price > lows[-2].price:
            return "bullish"
        if highs[-1].price < highs[-2].price and lows[-1].price < lows[-2].price:
            return "bearish"
    return "neutral"


def _structure(
    bars: list[_Bar],
    swings: list[_Swing],
    timeframe: str,
) -> tuple[list[SMCStructureEvent], Literal["bullish", "bearish", "neutral"], bool, bool]:
    events: list[SMCStructureEvent] = []
    for sw in swings[-8:]:
        events.append(SMCStructureEvent(
            event_id=_zone_id("sw", timeframe, sw.kind, sw.ts, sw.price),
            kind="swing_high" if sw.kind == "high" else "swing_low",
            direction="neutral",
            timeframe=timeframe,
            ts=sw.ts,
            price=sw.price,
            strength=round(sw.strength, 3),
            note="SMC swing pivot",
        ))

    if not bars or not swings:
        return events, "neutral", False, False

    close = bars[-1].c
    prev_highs = [s for s in swings if s.kind == "high" and s.index < len(bars) - 1]
    prev_lows = [s for s in swings if s.kind == "low" and s.index < len(bars) - 1]
    last_high = prev_highs[-1] if prev_highs else None
    last_low = prev_lows[-1] if prev_lows else None
    prior = _classify_bias(swings[:-1] if len(swings) > 3 else swings)
    current = _classify_bias(swings)
    has_mss = False
    has_bos = False

    if last_high and close > last_high.price:
        has_bos = True
        events.append(SMCStructureEvent(
            event_id=_zone_id("bos", timeframe, "up", last_high.ts, close),
            kind="bos",
            direction="bullish",
            timeframe=timeframe,
            ts=bars[-1].ts,
            price=close,
            strength=round((close - last_high.price) / close * 100, 3),
            note="Close displaced above prior swing high",
        ))
        if prior == "bearish":
            has_mss = True
            current = "bullish"
            events.append(SMCStructureEvent(
                event_id=_zone_id("mss", timeframe, "up", last_high.ts, close),
                kind="mss",
                direction="bullish",
                timeframe=timeframe,
                ts=bars[-1].ts,
                price=close,
                strength=round((close - last_high.price) / close * 100, 3),
                note="Bearish sequence shifted after protected high broke",
            ))

    if last_low and close < last_low.price:
        has_bos = True
        events.append(SMCStructureEvent(
            event_id=_zone_id("bos", timeframe, "down", last_low.ts, close),
            kind="bos",
            direction="bearish",
            timeframe=timeframe,
            ts=bars[-1].ts,
            price=close,
            strength=round((last_low.price - close) / close * 100, 3),
            note="Close displaced below prior swing low",
        ))
        if prior == "bullish":
            has_mss = True
            current = "bearish"
            events.append(SMCStructureEvent(
                event_id=_zone_id("mss", timeframe, "down", last_low.ts, close),
                kind="mss",
                direction="bearish",
                timeframe=timeframe,
                ts=bars[-1].ts,
                price=close,
                strength=round((last_low.price - close) / close * 100, 3),
                note="Bullish sequence shifted after protected low broke",
            ))

    # Realtime fallback: the newest displacement leg often has not printed enough
    # right-side candles to confirm a fractal pivot yet.  Use a compact rolling
    # break as an internal structure event so fresh SMC shifts are not invisible.
    if not has_bos and len(bars) >= 12:
        window = bars[-18:-1] if len(bars) >= 18 else bars[:-1]
        roll_high = max(b.h for b in window)
        roll_low = min(b.l for b in window)
        if close >= roll_high * 0.999:
            has_bos = True
            current = "bullish"
            events.append(SMCStructureEvent(
                event_id=_zone_id("bos", timeframe, "roll-up", bars[-1].ts, close),
                kind="bos",
                direction="bullish",
                timeframe=timeframe,
                ts=bars[-1].ts,
                price=close,
                strength=round((close - roll_high) / close * 100, 3),
                note="Close displaced above recent internal high",
            ))
            if prior == "bearish":
                has_mss = True
        elif close <= roll_low * 1.001:
            has_bos = True
            current = "bearish"
            events.append(SMCStructureEvent(
                event_id=_zone_id("bos", timeframe, "roll-down", bars[-1].ts, close),
                kind="bos",
                direction="bearish",
                timeframe=timeframe,
                ts=bars[-1].ts,
                price=close,
                strength=round((roll_low - close) / close * 100, 3),
                note="Close displaced below recent internal low",
            ))
            if prior == "bullish":
                has_mss = True

    return events, current, has_mss, has_bos


def _timeframe_inputs(state: Any, horizon: SMCHorizon) -> tuple[str, list[_Bar], list[str]]:
    if horizon == "swing":
        primary = _bars(_attr(state, "candles_4h")) or _bars(_attr(state, "candles_daily"))
        label = "4h" if _bars(_attr(state, "candles_4h")) else "1d"
        periods = ["4h", "1d", "1w", "7d", "30d"]
    else:
        primary = _bars(_attr(state, "candles_15m")) or _bars(_attr(state, "candles_1h"))
        label = "15m" if _bars(_attr(state, "candles_15m")) else "1h"
        periods = ["15m", "1h", "24h", "5m"]
    return label, primary, periods


def _equal_pools(
    state: Any,
    swings: list[_Swing],
    price: float,
    tf: str,
    tolerance: float,
) -> list[SMCLiquidityPool]:
    pools: list[SMCLiquidityPool] = []
    for kind, side in (("high", "buy_side"), ("low", "sell_side")):
        pivots = [s for s in swings if s.kind == kind][-10:]
        used: set[int] = set()
        for i, a in enumerate(pivots):
            if i in used:
                continue
            peers = [b for b in pivots[i + 1:] if abs(b.price - a.price) <= tolerance]
            if not peers:
                continue
            cluster = [a] + peers
            used.add(i)
            avg = sum(s.price for s in cluster) / len(cluster)
            band = max(tolerance, avg * 0.0005)
            pools.append(SMCLiquidityPool(
                pool_id=_zone_id("eq", state.coin, tf, kind, round(avg, 2)),
                side=side,  # type: ignore[arg-type]
                source="equal_highs_lows",
                timeframe=tf,
                price=round(avg, 4),
                price_from=round(avg - band, 4),
                price_to=round(avg + band, 4),
                strength=min(100, 35 + len(cluster) * 15),
                distance_pct=_pct(avg, price),
                evidence=[f"{len(cluster)} similar swing {kind}s within tolerance"],
            ))
    return pools


def _liq_pools(state: Any, price: float, horizon: SMCHorizon) -> list[SMCLiquidityPool]:
    pools: list[SMCLiquidityPool] = []
    cycle_keys = ("1d", "24h") if horizon == "intraday" else ("7d", "30d", "1d", "24h")
    maps = _attr(state, "liq_maps", default={}) or {}
    for cycle in cycle_keys:
        lm = maps.get(cycle)
        if not lm:
            continue
        for attr_name, side, source in (
            ("clusters_above", "buy_side", "liq_map_above"),
            ("clusters_below", "sell_side", "liq_map_below"),
        ):
            for idx, c in enumerate(_attr(lm, attr_name, default=[]) or []):
                center = _num(_attr(c, "price_center", default=0))
                if center <= 0:
                    continue
                usd = _num(_attr(c, "total_usd", default=0))
                pools.append(SMCLiquidityPool(
                    pool_id=_zone_id("liq", state.coin, cycle, attr_name, idx, round(center, 2)),
                    side=side,  # type: ignore[arg-type]
                    source=source,
                    timeframe=cycle,
                    price=center,
                    price_from=_num(_attr(c, "price_from", default=center)),
                    price_to=_num(_attr(c, "price_to", default=center)),
                    strength=min(100, 25 + usd / 1_000_000),
                    distance_pct=_pct(center, price),
                    evidence=[f"{cycle} liquidation cluster ${usd:,.0f}"],
                ))
    heatmaps = _attr(state, "liq_heatmaps", default={}) or {}
    hm_keys = ("24h",) if horizon == "intraday" else ("7d", "24h")
    for key in hm_keys:
        hm = heatmaps.get(key)
        points = sorted(
            (_attr(hm, "data", default=[]) or []),
            key=lambda p: _num(_attr(p, "value", default=0)),
            reverse=True,
        )[:5]
        for p in points:
            hp = _num(_attr(p, "price", default=0))
            if hp <= 0:
                continue
            val = _num(_attr(p, "value", default=0))
            side = "buy_side" if hp > price else "sell_side"
            band = max(price * 0.001, abs(hp - price) * 0.03)
            pools.append(SMCLiquidityPool(
                pool_id=_zone_id("hm", state.coin, key, round(hp, 2), round(val, 0)),
                side=side,  # type: ignore[arg-type]
                source="liq_heatmap",
                timeframe=key,
                price=hp,
                price_from=hp - band,
                price_to=hp + band,
                strength=min(100, 20 + val / 1_000_000),
                distance_pct=_pct(hp, price),
                evidence=[f"{key} heatmap node ${val:,.0f}"],
            ))
    pools.sort(key=lambda p: (abs(p.distance_pct), -p.strength))
    return pools[:16]


def _detect_raids(
    bars: list[_Bar],
    pools: list[SMCLiquidityPool],
    tf: str,
) -> tuple[list[SMCStructureEvent], Optional[Literal["bullish", "bearish"]]]:
    if not bars:
        return [], None
    recent = bars[-8:]
    events: list[SMCStructureEvent] = []
    raid_bias: Optional[Literal["bullish", "bearish"]] = None
    for p in pools[:12]:
        for b in recent:
            if p.side == "buy_side" and b.h > p.price_to and b.c < p.price:
                events.append(SMCStructureEvent(
                    event_id=_zone_id("raid", "buy", p.pool_id, b.ts),
                    kind="liquidity_raid",
                    direction="bearish",
                    timeframe=tf,
                    ts=b.ts,
                    price=b.h,
                    strength=min(100, p.strength + abs(_pct(b.h, p.price)) * 10),
                    note="Buy-side liquidity swept and candle closed back below pool",
                ))
                raid_bias = "bearish"
                p.swept = True
            if p.side == "sell_side" and b.l < p.price_from and b.c > p.price:
                events.append(SMCStructureEvent(
                    event_id=_zone_id("raid", "sell", p.pool_id, b.ts),
                    kind="liquidity_raid",
                    direction="bullish",
                    timeframe=tf,
                    ts=b.ts,
                    price=b.l,
                    strength=min(100, p.strength + abs(_pct(b.l, p.price)) * 10),
                    note="Sell-side liquidity swept and candle closed back above pool",
                ))
                raid_bias = "bullish"
                p.swept = True
    events.sort(key=lambda e: e.ts, reverse=True)
    return events[:4], raid_bias


def _fvg_zones(state: Any, bars: list[_Bar], price: float, tf: str, atr: float) -> list[SMCZone]:
    zones: list[SMCZone] = []
    if len(bars) < 3:
        return zones
    avg_body = sum(abs(b.c - b.o) for b in bars[-30:]) / max(1, min(len(bars), 30))
    for i in range(2, len(bars)):
        a, b, c = bars[i - 2], bars[i - 1], bars[i]
        body = abs(b.c - b.o)
        strong = body >= max(avg_body * 1.35, atr * 0.35)
        if c.l > a.h and strong:
            lo, hi = a.h, c.l
            mitigated = any(x.l <= (lo + hi) / 2 for x in bars[i + 1:])
            zones.append(SMCZone(
                zone_id=_zone_id("fvg", state.coin, tf, "bull", a.ts, c.ts),
                kind="fair_value_gap",
                role="bullish_demand",
                state="candidate" if not mitigated else "expired",
                timeframe=tf,
                price_from=lo,
                price_to=hi,
                midpoint=(lo + hi) / 2,
                strength=min(100, 45 + body / max(atr, 1e-9) * 12),
                distance_pct=_pct((lo + hi) / 2, price),
                created_ts=c.ts,
                invalidation_price=lo,
                evidence=["Three-candle bullish imbalance after displacement"],
            ))
        if c.h < a.l and strong:
            lo, hi = c.h, a.l
            mitigated = any(x.h >= (lo + hi) / 2 for x in bars[i + 1:])
            zones.append(SMCZone(
                zone_id=_zone_id("fvg", state.coin, tf, "bear", a.ts, c.ts),
                kind="fair_value_gap",
                role="bearish_supply",
                state="candidate" if not mitigated else "expired",
                timeframe=tf,
                price_from=lo,
                price_to=hi,
                midpoint=(lo + hi) / 2,
                strength=min(100, 45 + body / max(atr, 1e-9) * 12),
                distance_pct=_pct((lo + hi) / 2, price),
                created_ts=c.ts,
                invalidation_price=hi,
                evidence=["Three-candle bearish imbalance after displacement"],
            ))
    return sorted(zones, key=lambda z: (z.state != "candidate", abs(z.distance_pct)))[:10]


def _ob_and_breakers(state: Any, bars: list[_Bar], price: float, tf: str, atr: float) -> list[SMCZone]:
    zones: list[SMCZone] = []
    if len(bars) < 8:
        return zones
    avg_body = sum(abs(b.c - b.o) for b in bars[-40:]) / max(1, min(len(bars), 40))
    for i in range(2, len(bars)):
        prev = bars[i - 1]
        cur = bars[i]
        displacement = abs(cur.c - cur.o) >= max(avg_body * 1.5, atr * 0.45)
        if not displacement:
            continue
        if cur.c > cur.o and prev.c < prev.o:
            invalidated = any(x.c < prev.l for x in bars[i + 1:])
            kind = "breaker" if invalidated else "order_block"
            role = "bearish_supply" if invalidated else "bullish_demand"
            zones.append(SMCZone(
                zone_id=_zone_id(kind, state.coin, tf, "bull", prev.ts),
                kind=kind,  # type: ignore[arg-type]
                role=role,  # type: ignore[arg-type]
                state="candidate" if not invalidated else "candidate",
                timeframe=tf,
                price_from=prev.l,
                price_to=prev.h,
                midpoint=(prev.l + prev.h) / 2,
                strength=min(100, 50 + abs(cur.c - cur.o) / max(atr, 1e-9) * 10),
                distance_pct=_pct((prev.l + prev.h) / 2, price),
                created_ts=prev.ts,
                invalidation_price=prev.l if not invalidated else prev.h,
                evidence=[
                    "Last down candle before bullish displacement"
                    if not invalidated else
                    "Failed bullish block flipped into supply breaker"
                ],
            ))
        if cur.c < cur.o and prev.c > prev.o:
            invalidated = any(x.c > prev.h for x in bars[i + 1:])
            kind = "breaker" if invalidated else "order_block"
            role = "bullish_demand" if invalidated else "bearish_supply"
            zones.append(SMCZone(
                zone_id=_zone_id(kind, state.coin, tf, "bear", prev.ts),
                kind=kind,  # type: ignore[arg-type]
                role=role,  # type: ignore[arg-type]
                state="candidate",
                timeframe=tf,
                price_from=prev.l,
                price_to=prev.h,
                midpoint=(prev.l + prev.h) / 2,
                strength=min(100, 50 + abs(cur.c - cur.o) / max(atr, 1e-9) * 10),
                distance_pct=_pct((prev.l + prev.h) / 2, price),
                created_ts=prev.ts,
                invalidation_price=prev.h if not invalidated else prev.l,
                evidence=[
                    "Last up candle before bearish displacement"
                    if not invalidated else
                    "Failed bearish block flipped into demand breaker"
                ],
            ))
    return sorted(zones, key=lambda z: abs(z.distance_pct))[:10]


def _fib_zone(
    state: Any,
    swings: list[_Swing],
    price: float,
    tf: str,
    bias: Literal["bullish", "bearish", "neutral"],
) -> list[SMCZone]:
    if bias == "neutral" or len(swings) < 2:
        return []
    highs = [s for s in swings if s.kind == "high"]
    lows = [s for s in swings if s.kind == "low"]
    if not highs or not lows:
        return []
    if bias == "bullish":
        hi = highs[-1]
        lo = next((s for s in reversed(lows) if s.index < hi.index), lows[-1])
        low, high = lo.price, hi.price
        if high <= low:
            return []
        ote = high - (high - low) * 0.705
        lo_band = high - (high - low) * 0.786
        hi_band = high - (high - low) * 0.618
        role = "bullish_demand"
        invalidation = low
    else:
        lo = lows[-1]
        hi = next((s for s in reversed(highs) if s.index < lo.index), highs[-1])
        low, high = lo.price, hi.price
        if high <= low:
            return []
        ote = low + (high - low) * 0.705
        lo_band = low + (high - low) * 0.618
        hi_band = low + (high - low) * 0.786
        role = "bearish_supply"
        invalidation = high
    return [SMCZone(
        zone_id=_zone_id("ote", state.coin, tf, bias, round(ote, 2)),
        kind="fib_ote",
        role=role,  # type: ignore[arg-type]
        state="candidate",
        timeframe=tf,
        price_from=min(lo_band, hi_band),
        price_to=max(lo_band, hi_band),
        midpoint=ote,
        strength=55,
        distance_pct=_pct(ote, price),
        created_ts=swings[-1].ts,
        invalidation_price=invalidation,
        evidence=["0.618-0.786 retracement band with OTE 0.705 midpoint"],
    )]


def _turnover_zones(state: Any, bars: list[_Bar], price: float, tf: str) -> list[SMCZone]:
    if len(bars) < 20:
        return []
    recent = bars[-80:]
    lo = min(b.l for b in recent)
    hi = max(b.h for b in recent)
    if hi <= lo:
        return []
    bins = 24
    step = (hi - lo) / bins
    vol_by_bin = [0.0 for _ in range(bins)]
    for b in recent:
        idx = min(bins - 1, max(0, int((b.c - lo) / step)))
        vol_by_bin[idx] += max(b.v, 0.0)
    ranked = sorted(enumerate(vol_by_bin), key=lambda item: item[1], reverse=True)[:6]
    zones: list[SMCZone] = []
    max_vol = max(vol_by_bin) or 1.0
    for idx, vol in ranked:
        zlo = lo + idx * step
        zhi = zlo + step
        mid = (zlo + zhi) / 2
        role = "support" if mid < price else "resistance"
        zones.append(SMCZone(
            zone_id=_zone_id("turnover", state.coin, tf, idx, round(mid, 2)),
            kind="turnover_sr",
            role=role,  # type: ignore[arg-type]
            state="candidate",
            timeframe=tf,
            price_from=zlo,
            price_to=zhi,
            midpoint=mid,
            strength=round(30 + vol / max_vol * 50, 2),
            distance_pct=_pct(mid, price),
            created_ts=recent[-1].ts,
            evidence=[f"Self-calculated turnover node, relative volume {vol / max_vol:.2f}"],
        ))
    return sorted(zones, key=lambda z: abs(z.distance_pct))[:6]


def _po3_zones(state: Any, bars: list[_Bar], price: float, tf: str, atr: float) -> list[SMCZone]:
    if len(bars) < 24 or atr <= 0:
        return []
    zones: list[SMCZone] = []
    window = bars[-24:]
    base = window[:12]
    r_hi = max(b.h for b in base)
    r_lo = min(b.l for b in base)
    if r_hi <= r_lo or (r_hi - r_lo) > atr * 3.0:
        return []
    later = window[12:]
    mid = (r_hi + r_lo) / 2
    bull_manip = any(b.l < r_lo and b.c >= r_lo for b in later[:8])
    bull_dist = any(b.c > r_hi for b in later[4:])
    bear_manip = any(b.h > r_hi and b.c <= r_hi for b in later[:8])
    bear_dist = any(b.c < r_lo for b in later[4:])
    if bull_manip and bull_dist:
        zones.append(SMCZone(
            zone_id=_zone_id("po3", state.coin, tf, "bull", window[0].ts),
            kind="po3",
            role="bullish_demand",
            state="mss_confirmed",
            timeframe=tf,
            price_from=r_lo,
            price_to=mid,
            midpoint=(r_lo + mid) / 2,
            strength=65,
            distance_pct=_pct((r_lo + mid) / 2, price),
            created_ts=window[0].ts,
            invalidation_price=min(b.l for b in later),
            evidence=["Accumulation range, downside manipulation, upside distribution"],
        ))
    if bear_manip and bear_dist:
        zones.append(SMCZone(
            zone_id=_zone_id("po3", state.coin, tf, "bear", window[0].ts),
            kind="po3",
            role="bearish_supply",
            state="mss_confirmed",
            timeframe=tf,
            price_from=mid,
            price_to=r_hi,
            midpoint=(mid + r_hi) / 2,
            strength=65,
            distance_pct=_pct((mid + r_hi) / 2, price),
            created_ts=window[0].ts,
            invalidation_price=max(b.h for b in later),
            evidence=["Accumulation range, upside manipulation, downside distribution"],
        ))
    return zones


def _mark_entry_zones(zones: list[SMCZone], price: float) -> None:
    for z in zones:
        if z.state in {"expired", "invalidated"}:
            continue
        if z.price_from <= price <= z.price_to:
            z.state = "entry_zone_active"


def _cvd_confirmation(state: Any) -> tuple[list[SMCConfirmation], list[SMCContradiction]]:
    confirmations: list[SMCConfirmation] = []
    contradictions: list[SMCContradiction] = []
    contract = _attr(state, "cvd_contract")
    spot = _attr(state, "cvd_spot")
    c_delta = _num(_attr(contract, "delta_1h", default=0))
    s_delta = _num(_attr(spot, "delta_1h", default=0))
    if c_delta or s_delta:
        total = c_delta + s_delta
        direction = "bullish" if total > 0 else "bearish"
        confirmations.append(SMCConfirmation(
            source="cvd",
            direction=direction,  # type: ignore[arg-type]
            score_delta=max(-8, min(8, total / 10_000_000)),
            confidence=min(1.0, abs(total) / 25_000_000),
            note=f"Contract+spot CVD 1h delta {total:,.0f}",
        ))
        if c_delta * s_delta < 0:
            contradictions.append(SMCContradiction(
                source="cvd",
                severity="medium",
                note="Spot and contract CVD disagree",
            ))
    return confirmations, contradictions


def _derivative_confirmations(state: Any) -> list[SMCConfirmation]:
    out: list[SMCConfirmation] = []
    oi = _attr(state, "oi")
    if oi:
        chg = _num(_attr(oi, "change_1h_pct", default=0))
        if abs(chg) >= 0.2:
            out.append(SMCConfirmation(
                source="open_interest",
                direction="bullish" if chg > 0 else "neutral",
                score_delta=max(-5, min(5, chg)),
                confidence=min(1, abs(chg) / 3),
                note=f"OI 1h change {chg:.2f}%",
            ))
    funding = _attr(state, "funding")
    rate = _num(_attr(funding, "oi_weighted_rate", "avg_rate", default=0))
    if abs(rate) > 0:
        if rate < -0.0001:
            direction = "bullish"
        elif rate > 0.00025:
            direction = "bearish"
        else:
            direction = "neutral"
        out.append(SMCConfirmation(
            source="funding",
            direction=direction,  # type: ignore[arg-type]
            score_delta=4 if direction != "neutral" else 0,
            confidence=min(1, abs(rate) / 0.0005),
            note=f"Weighted funding {rate:.5f}",
        ))
    taker = _attr(state, "taker_flow")
    if taker:
        buy = _num(_attr(taker, "contract_buy_vol", default=0)) + _num(_attr(taker, "spot_buy_vol", default=0))
        sell = _num(_attr(taker, "contract_sell_vol", default=0)) + _num(_attr(taker, "spot_sell_vol", default=0))
        if buy or sell:
            delta = buy - sell
            out.append(SMCConfirmation(
                source="taker",
                direction="bullish" if delta > 0 else "bearish",
                score_delta=max(-5, min(5, delta / 20_000_000)),
                confidence=min(1, abs(delta) / max(buy + sell, 1)),
                note=f"Taker delta {delta:,.0f}",
            ))
    return out


def _footprint_confirmation(state: Any) -> Optional[SMCConfirmation]:
    frames = list(_attr(state, "footprint_contract", default=[]) or []) + list(
        _attr(state, "footprint_spot", default=[]) or []
    )
    buy = 0.0
    sell = 0.0
    for frame in frames[-2:]:
        buckets = _attr(frame, "buckets", "data", default=[]) or []
        for bucket in buckets:
            buy += _num(_attr(bucket, "buy", "buy_volume", "buyVol", default=0))
            sell += _num(_attr(bucket, "sell", "sell_volume", "sellVol", default=0))
            buy += max(0, _num(_attr(bucket, "delta", default=0)))
            sell += max(0, -_num(_attr(bucket, "delta", default=0)))
    if not buy and not sell:
        return None
    delta = buy - sell
    return SMCConfirmation(
        source="footprint",
        direction="bullish" if delta > 0 else "bearish",
        score_delta=max(-6, min(6, delta / 10_000_000)),
        confidence=min(1, abs(delta) / max(buy + sell, 1)),
        note=f"Recent footprint imbalance {delta:,.0f}",
    )


def _visible_book_confirmation(state: Any) -> Optional[SMCConfirmation]:
    book = _attr(state, "orderbook")
    bid = _num(_attr(book, "bid_total_usd", default=0))
    ask = _num(_attr(book, "ask_total_usd", default=0))
    if bid <= 0 and ask <= 0:
        return None
    imbalance = (bid - ask) / max(bid + ask, 1)
    return SMCConfirmation(
        source="visible_orderbook_p2",
        direction="bullish" if imbalance > 0 else "bearish",
        score_delta=max(-2, min(2, imbalance * 3)),
        confidence=min(0.35, abs(imbalance)),
        note="Visible book imbalance only; never a standalone SMC trigger",
    )


def _nansen_perp(raw: dict[str, Any] | None, updated_at: int) -> Optional[SMCNansenPerp]:
    if not raw:
        return None
    return SMCNansenPerp(
        token_symbol=str(raw.get("token_symbol") or raw.get("symbol") or ""),
        mark_price=_num(raw.get("mark_price"), None),  # type: ignore[arg-type]
        funding=_num(raw.get("funding"), None),  # type: ignore[arg-type]
        open_interest=_num(raw.get("open_interest"), None),  # type: ignore[arg-type]
        smart_money_volume=_num(raw.get("smart_money_volume"), None),  # type: ignore[arg-type]
        smart_money_buy_volume=_num(raw.get("smart_money_buy_volume"), None),  # type: ignore[arg-type]
        smart_money_sell_volume=_num(raw.get("smart_money_sell_volume"), None),  # type: ignore[arg-type]
        net_position_change=_num(raw.get("net_position_change"), None),  # type: ignore[arg-type]
        current_longs_usd=_num(raw.get("current_smart_money_position_longs_usd"), None),  # type: ignore[arg-type]
        current_shorts_usd=_num(raw.get("current_smart_money_position_shorts_usd"), None),  # type: ignore[arg-type]
        trader_count=_int(raw.get("trader_count"), 0) or None,
        updated_at=updated_at,
    )


def _nansen_flow(raw: dict[str, Any] | None, updated_at: int) -> Optional[SMCNansenFlow]:
    if not raw:
        return None
    return SMCNansenFlow(
        token_symbol=str(raw.get("token_symbol") or raw.get("symbol") or ""),
        token_address=str(raw.get("token_address") or ""),
        chain=str(raw.get("chain") or "ethereum"),
        timeframe=str(raw.get("timeframe") or "1d"),
        smart_trader_net_flow_usd=_num(raw.get("smart_trader_net_flow_usd"), None),  # type: ignore[arg-type]
        top_pnl_net_flow_usd=_num(raw.get("top_pnl_net_flow_usd"), None),  # type: ignore[arg-type]
        whale_net_flow_usd=_num(raw.get("whale_net_flow_usd"), None),  # type: ignore[arg-type]
        exchange_net_flow_usd=_num(raw.get("exchange_net_flow_usd"), None),  # type: ignore[arg-type]
        updated_at=updated_at,
    )


def _smart_money_context(state: Any, market_breadth: dict[str, Any] | None) -> tuple[SMCSmartMoneyContext, list[SMCConfirmation], list[SMCContradiction]]:
    updated = _attr(state, "nansen_updated_at", default={}) or {}
    perp = _nansen_perp(_attr(state, "nansen_perp"), _int(updated.get("perp_screener"), 0))
    flow = _nansen_flow(_attr(state, "nansen_flow_intelligence"), _int(updated.get("flow_intelligence"), 0))
    confirmations: list[SMCConfirmation] = []
    contradictions: list[SMCContradiction] = []
    notes: list[str] = []
    score = 0.0
    bias_score = 0.0
    if perp:
        buy = perp.smart_money_buy_volume or 0
        sell = perp.smart_money_sell_volume or 0
        net_pos = perp.net_position_change or 0
        if buy or sell or net_pos:
            raw_score = (buy - sell) + net_pos
            bias_score += raw_score
            score += min(0.65, abs(raw_score) / max((buy + sell), 1))
            confirmations.append(SMCConfirmation(
                source="nansen_perp",
                direction="bullish" if raw_score > 0 else "bearish",
                score_delta=max(-8, min(8, raw_score / 5_000_000)),
                confidence=min(1, abs(raw_score) / max(buy + sell, 1)),
                note=f"Nansen perp smart-money net {raw_score:,.0f}",
            ))
    if flow:
        net = sum(v for v in (
            flow.smart_trader_net_flow_usd,
            flow.top_pnl_net_flow_usd,
            flow.whale_net_flow_usd,
        ) if v is not None)
        if net:
            bias_score += net
            score += min(0.35, abs(net) / 20_000_000)
            confirmations.append(SMCConfirmation(
                source="nansen_flow",
                direction="bullish" if net > 0 else "bearish",
                score_delta=max(-5, min(5, net / 5_000_000)),
                confidence=min(1, abs(net) / 20_000_000),
                note=f"Nansen TGM segment net flow {net:,.0f}",
            ))
    if not perp:
        notes.append("Nansen perp data missing")
    if not flow:
        notes.append("Nansen flow intelligence missing")

    breadth_score = None
    if market_breadth:
        breadth_score = _num(market_breadth.get("breadth_score"), 0)
        if breadth_score:
            confirmations.append(SMCConfirmation(
                source="nansen_breadth",
                direction="bullish" if breadth_score > 0 else "bearish",
                score_delta=max(-3, min(3, breadth_score / 20)),
                confidence=min(0.5, abs(breadth_score) / 100),
                note=f"Low-frequency smart-money breadth {breadth_score:.1f}",
            ))

    status = "ok" if perp and flow else ("partial" if perp or flow else "missing")
    bias = "bullish" if bias_score > 0 else ("bearish" if bias_score < 0 else "neutral")
    return SMCSmartMoneyContext(
        status=status,  # type: ignore[arg-type]
        bias=bias,  # type: ignore[arg-type]
        confidence=round(min(1, score), 3),
        perp=perp,
        flow=flow,
        market_breadth_score=breadth_score,
        notes=notes,
    ), confirmations, contradictions


def _quality(
    state: Any,
    bars: list[_Bar],
    horizon: SMCHorizon,
    smart: SMCSmartMoneyContext,
) -> SMCDataQuality:
    required = {
        "ticker": bool(_attr(state, "ticker")) or bool(bars),
        "candles": len(bars) >= 30,
        "liquidation_map": bool((_attr(state, "liq_maps", default={}) or {}).get("1d") or (_attr(state, "liq_maps", default={}) or {}).get("24h")),
        "cvd": bool(_attr(state, "cvd_contract") or _attr(state, "cvd_spot")),
        "oi": bool(_attr(state, "oi")),
        "funding": bool(_attr(state, "funding")),
    }
    if horizon == "swing":
        required["swing_context"] = bool(_attr(state, "candles_4h") or _attr(state, "candles_daily"))
        required["liq_7d"] = bool((_attr(state, "liq_maps", default={}) or {}).get("7d"))
    missing = [k for k, ok in required.items() if not ok]
    optional = {
        "footprint": bool(_attr(state, "footprint_contract") or _attr(state, "footprint_spot")),
        "taker": bool(_attr(state, "taker_flow")),
        "visible_orderbook": bool(_attr(state, "orderbook")),
        "nansen": smart.status != "missing",
    }
    degraded = [k for k, ok in optional.items() if not ok]
    ready_ratio = (len(required) - len(missing)) / max(len(required), 1)
    score = int(round(ready_ratio * 80 + (len(optional) - len(degraded)) / max(len(optional), 1) * 20))
    status = "ok" if score >= 80 and not missing else ("partial" if score >= 50 else "degraded")
    return SMCDataQuality(
        score=score,
        status=status,  # type: ignore[arg-type]
        missing=missing,
        degraded=degraded,
        source_status={
            "nansen": smart.status,
            "orderbook": "auxiliary_only",
        },
        notes=[
            "Orderbook is P2 auxiliary evidence and cannot trigger signals alone",
            "SMC does not consume existing opinion-layer conclusions",
        ],
    )


def _select_signal(
    raid_bias: Optional[Literal["bullish", "bearish"]],
    bias: Literal["bullish", "bearish", "neutral"],
    has_mss: bool,
    zones: list[SMCZone],
    confirmations: list[SMCConfirmation],
    contradictions: list[SMCContradiction],
    price: float,
) -> tuple[str, str, int, Optional[float], str]:
    bull_zone = any(z.state == "entry_zone_active" and z.role in {"bullish_demand", "support"} for z in zones)
    bear_zone = any(z.state == "entry_zone_active" and z.role in {"bearish_supply", "resistance"} for z in zones)
    setup_state = "candidate"
    observation = "wait"
    summary = "等待：尚未形成扫流动性 + 结构转换 + 入场区三件套。"
    if raid_bias:
        setup_state = "raid_detected"
    if has_mss:
        setup_state = "mss_confirmed"
    if raid_bias == "bullish" and (bias == "bullish" or has_mss):
        observation = "long_watch"
        summary = "做多观察：下方流动性被扫后，结构出现多头确认，等待或跟踪需求区/FVG/OTE。"
    elif raid_bias == "bearish" and (bias == "bearish" or has_mss):
        observation = "short_watch"
        summary = "做空观察：上方流动性被扫后，结构出现空头确认，等待或跟踪供应区/FVG/OTE。"
    if observation == "long_watch" and bull_zone:
        setup_state = "entry_zone_active"
    if observation == "short_watch" and bear_zone:
        setup_state = "entry_zone_active"
    conf = 25
    if raid_bias:
        conf += 18
    if has_mss:
        conf += 18
    if (observation == "long_watch" and bull_zone) or (observation == "short_watch" and bear_zone):
        conf += 14
    wanted = "bullish" if observation == "long_watch" else ("bearish" if observation == "short_watch" else "neutral")
    for c in confirmations:
        if c.direction == wanted:
            conf += int(abs(c.score_delta))
        elif wanted != "neutral" and c.direction not in {"neutral", wanted}:
            conf -= int(abs(c.score_delta))
    for item in contradictions:
        conf -= {"low": 3, "medium": 7, "high": 12}.get(item.severity, 3)
    if observation == "wait":
        conf = min(conf, 45)
    invalidation = None
    relevant = [
        z for z in zones
        if (observation == "long_watch" and z.role in {"bullish_demand", "support"})
        or (observation == "short_watch" and z.role in {"bearish_supply", "resistance"})
    ]
    if relevant:
        nearest = min(relevant, key=lambda z: abs(z.midpoint - price))
        invalidation = nearest.invalidation_price
    return observation, setup_state, max(0, min(100, conf)), invalidation, summary


def _targets(
    pools: list[SMCLiquidityPool],
    zones: list[SMCZone],
    observation: str,
    price: float,
) -> list[SMCTargetZone]:
    out: list[SMCTargetZone] = []
    if observation == "long_watch":
        candidates = [p for p in pools if p.price > price and p.side == "buy_side"]
    elif observation == "short_watch":
        candidates = [p for p in pools if p.price < price and p.side == "sell_side"]
    else:
        candidates = sorted(pools, key=lambda p: abs(p.price - price))[:4]
    for p in candidates[:4]:
        out.append(SMCTargetZone(
            price=p.price,
            kind=p.source,
            side="above" if p.price > price else "below",
            distance_pct=_pct(p.price, price),
            note=", ".join(p.evidence[:1]),
        ))
    for z in zones:
        if z.kind == "fair_value_gap" and abs(z.distance_pct) < 3:
            out.append(SMCTargetZone(
                price=z.midpoint,
                kind="fvg_rebalance",
                side="above" if z.midpoint > price else "below",
                distance_pct=z.distance_pct,
                note="Nearby imbalance midpoint",
            ))
    return out[:6]


def build_smc_snapshot(
    state: Any,
    *,
    horizon: SMCHorizon = "intraday",
    market_breadth: dict[str, Any] | None = None,
) -> SMCSnapshot:
    tf, bars, periods = _timeframe_inputs(state, horizon)
    price = _last_price(state, bars)
    atr = _atr(bars) or _num(_attr(state, "atr"), 0) or max(price * 0.005, 1.0)
    wing = 3 if horizon == "swing" else 2
    swings = _detect_swings(bars, wing=wing, min_gap_pct=0.06 if horizon == "intraday" else 0.2)
    structure, bias, has_mss, _has_bos = _structure(bars, swings, tf)
    tolerance = max(price * (0.0015 if horizon == "intraday" else 0.004), atr * 0.25)
    pools = _equal_pools(state, swings, price, tf, tolerance)
    pools.extend(_liq_pools(state, price, horizon))
    pools.sort(key=lambda p: (abs(p.distance_pct), -p.strength))
    raids, raid_bias = _detect_raids(bars, pools, tf)
    structure = raids + structure

    zones: list[SMCZone] = []
    zones.extend(_ob_and_breakers(state, bars, price, tf, atr))
    zones.extend(_fvg_zones(state, bars, price, tf, atr))
    zones.extend(_po3_zones(state, bars, price, tf, atr))
    zones.extend(_fib_zone(state, swings, price, tf, bias))
    zones.extend(_turnover_zones(state, bars, price, tf))
    _mark_entry_zones(zones, price)
    zones.sort(key=lambda z: (z.state != "entry_zone_active", abs(z.distance_pct), -z.strength))

    confirmations, contradictions = _cvd_confirmation(state)
    confirmations.extend(_derivative_confirmations(state))
    fp = _footprint_confirmation(state)
    if fp:
        confirmations.append(fp)
    ob = _visible_book_confirmation(state)
    if ob:
        confirmations.append(ob)
    smart, nansen_conf, nansen_contra = _smart_money_context(state, market_breadth)
    confirmations.extend(nansen_conf)
    contradictions.extend(nansen_contra)

    data_quality = _quality(state, bars, horizon, smart)
    observation, setup_state, confidence, invalidation, summary = _select_signal(
        raid_bias, bias, has_mss, zones, confirmations, contradictions, price,
    )
    return SMCSnapshot(
        coin=state.coin,
        ts=int(time.time()),
        horizon=horizon,
        last_price=price,
        observation=observation,  # type: ignore[arg-type]
        setup_state=setup_state,  # type: ignore[arg-type]
        confidence=confidence,
        summary=summary,
        timeframe_map={
            "intraday": ["15m", "1h", "24h", "5m"],
            "swing": ["4h", "1d", "1w", "7d", "30d"],
            "active": periods,
        },
        invalidation_price=invalidation,
        structure=structure[:18],
        liquidity_pools=pools[:16],
        zones=zones[:18],
        targets=_targets(pools, zones, observation, price),
        confirmations=confirmations[:16],
        contradictions=contradictions[:10],
        smart_money=smart,
        data_quality=data_quality,
    )


def build_smc_field_map() -> list[SMCFieldMapItem]:
    return [
        SMCFieldMapItem(field="candles_15m/1h/4h/1d/1w", priority="P0", periods=["15m", "1h", "4h", "1d", "1w"], use="swing, BOS, MSS, OB, FVG, PO3, Fib/OTE"),
        SMCFieldMapItem(field="liq_maps + liq_heatmaps", priority="P0", periods=["24h", "7d", "30d"], use="buy-side/sell-side liquidity pools"),
        SMCFieldMapItem(field="cvd_contract + cvd_spot", priority="P0", periods=["5m", "1h"], use="orderflow confirmation and divergence"),
        SMCFieldMapItem(field="footprint_contract + footprint_spot", priority="P0", periods=["1h"], use="absorption / imbalance confirmation"),
        SMCFieldMapItem(field="oi + oi_hourly_history", priority="P1", periods=["1h", "24h", "30d"], use="position build-up context"),
        SMCFieldMapItem(field="funding + funding_history_8h", priority="P1", periods=["8h", "7d", "30d"], use="crowding and squeeze risk"),
        SMCFieldMapItem(field="taker_flow", priority="P1", periods=["5m"], use="aggressive buy/sell pressure"),
        SMCFieldMapItem(field="nansen_perp + nansen_flow_intelligence", priority="P1", periods=["15m", "60m", "4h"], use="smart-money confirmation only"),
        SMCFieldMapItem(field="orderbook/depth/large-orders", priority="P2", periods=["5m", "1h", "8h"], use="visible liquidity auxiliary evidence", notes="Cannot trigger SMC signal by itself"),
        SMCFieldMapItem(field="etf_flow", priority="P2", periods=["1d", "3d"], use="macro flow background"),
    ]


def build_smc_facts(
    state: Any,
    *,
    horizon: SMCHorizon = "intraday",
    market_breadth: dict[str, Any] | None = None,
) -> dict[str, Any]:
    snap = build_smc_snapshot(state, horizon=horizon, market_breadth=market_breadth)
    return {
        "coin": state.coin,
        "horizon": horizon,
        "ts": snap.ts,
        "field_map": [item.model_dump() for item in build_smc_field_map()],
        "forbidden_inputs": [
            "key_" + "level_" + "snapshot_" + "v2",
            "trading_" + "brain",
            "market_" + "action_" + "report",
            "orderbook_" + "pressure_" + "snapshot",
            "market_" + "structure",
        ],
        "normalized": {
            "last_price": snap.last_price,
            "timeframe_map": snap.timeframe_map,
            "structure_events": [e.model_dump() for e in snap.structure],
            "liquidity_pools": [p.model_dump() for p in snap.liquidity_pools],
            "zones": [z.model_dump() for z in snap.zones],
            "confirmations": [c.model_dump() for c in snap.confirmations],
            "contradictions": [c.model_dump() for c in snap.contradictions],
            "data_quality": snap.data_quality.model_dump(),
        },
    }


def build_smc_market_breadth(raw: dict[str, Any] | None) -> SMCMarketBreadth:
    if not raw:
        return SMCMarketBreadth(
            ts=int(time.time()),
            status="missing",
            data_quality=SMCDataQuality(
                score=0,
                status="degraded",
                missing=["nansen_market_breadth"],
                source_status={"nansen": "missing"},
            ),
        )
    net = raw.get("smart_money_netflow") or []
    tokens = raw.get("token_screener") or []
    net_score = 0.0
    for row in net[:50]:
        net_score += _num(row.get("net_flow_24h_usd"), 0)
    token_score = 0.0
    for row in tokens[:50]:
        token_score += _num(row.get("netflow"), 0)
    breadth = max(-100, min(100, (net_score + token_score) / 10_000_000))
    status = "ok" if net and tokens else ("partial" if net or tokens else "missing")
    return SMCMarketBreadth(
        ts=_int(raw.get("ts"), int(time.time())),
        status=status,  # type: ignore[arg-type]
        chains=list(raw.get("chains") or []),
        smart_money_netflow=net[:50],
        token_screener=tokens[:50],
        breadth_score=round(breadth, 3),
        data_quality=SMCDataQuality(
            score=90 if status == "ok" else (55 if status == "partial" else 0),
            status="ok" if status == "ok" else ("partial" if status == "partial" else "degraded"),
            missing=[] if status == "ok" else ["netflow_or_token_screener"],
            source_status={"nansen": status},
        ),
    )
