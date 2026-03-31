"""链上周期位置评分 (Cycle Position Score, CPS)

CPS 综合 5 个链上日级维度对 BTC 所处周期位置进行 0~10 分评分：
  MVRV Z-Score | Ahr999 | 价格/200W SMA | 价格 vs STH 成本 | Pi 周期比值

评分作为全局状态机广播到所有币种（ETH/SOL 等跟随 BTC 周期判断）。
"""

from __future__ import annotations

import logging
from typing import Optional

from models.flow import CyclePositionData, OnchainCycleData

logger = logging.getLogger(__name__)


def calculate_cycle_position(
    raw: OnchainCycleData,
    btc_price: float,
) -> Optional[CyclePositionData]:
    """根据链上周期指标计算 CPS (0~10)。btc_price ≤ 0 时返回 None。"""
    if btc_price <= 0:
        return None

    total = 0.0

    # ── MVRV Z-Score ──
    mvrv_z = raw.mvrv_z
    mvrv_contrib = 0.0
    if mvrv_z is not None:
        if mvrv_z < 0:
            mvrv_contrib = 3.0
        elif mvrv_z < 2:
            mvrv_contrib = 2.0
        elif mvrv_z < 4:
            mvrv_contrib = 1.0
        elif mvrv_z < 6:
            mvrv_contrib = 0.0
        else:
            mvrv_contrib = -2.0
    total += mvrv_contrib

    # ── Ahr999 ──
    ahr999 = raw.ahr999
    ahr_contrib = 0.0
    if ahr999 is not None:
        if ahr999 < 0.45:
            ahr_contrib = 3.0
        elif ahr999 < 1.2:
            ahr_contrib = 1.0
        else:
            ahr_contrib = -1.0
    total += ahr_contrib

    # ── Price vs 200W SMA ──
    sma_200w = raw.sma_200w
    sma_ratio: Optional[float] = None
    sma_contrib = 0.0
    if sma_200w and sma_200w > 0:
        sma_ratio = btc_price / sma_200w
        if sma_ratio < 1.1:
            sma_contrib = 2.0
        elif sma_ratio < 2.0:
            sma_contrib = 1.0
        elif sma_ratio > 3.0:
            sma_contrib = -1.0
    total += sma_contrib

    # ── Price vs STH v1（短期持有者成本） ──
    sth_v1 = raw.sth_cost_1d
    sth_label = ""
    sth_contrib = 0.0
    if sth_v1 and sth_v1 > 0:
        if btc_price < sth_v1:
            sth_contrib = -1.0
            pct = (sth_v1 - btc_price) / sth_v1 * 100
            sth_label = f"价格低于STH成本v1 {pct:.1f}%，短期持有者浮亏"
        else:
            sth_contrib = 0.5
            pct = (btc_price - sth_v1) / sth_v1 * 100
            sth_label = f"价格高于STH成本v1 {pct:.1f}%，短期持有者浮盈"
    total += sth_contrib

    # ── Pi Cycle ratio (350DMA / 111DMA×2) ──
    pi_ratio: Optional[float] = None
    pi_contrib = 0.0
    if raw.pi_350dma and raw.pi_111dma_x2 and raw.pi_111dma_x2 > 0:
        pi_ratio = raw.pi_350dma / raw.pi_111dma_x2
        if pi_ratio < 0.6:
            pi_contrib = 1.0
        elif pi_ratio < 0.85:
            pi_contrib = 0.0
        elif pi_ratio > 0.9:
            pi_contrib = -2.0
    total += pi_contrib

    cps = max(0.0, min(10.0, total))

    if cps >= 8:
        label = "周期底部区"
    elif cps >= 5:
        label = "折扣区"
    elif cps >= 2:
        label = "公允区"
    elif cps >= 0.5:
        label = "溢价区"
    else:
        label = "顶部区"

    # ── RPLR 代理 ──
    rplr: Optional[float] = None
    if sth_v1 and sth_v1 > 0:
        rplr = (btc_price - sth_v1) / sth_v1

    # ── BTC 日线 RSI(14) ──
    rsi = _calc_rsi(raw.btc_daily_prices, 14) if len(raw.btc_daily_prices) >= 15 else None

    result = CyclePositionData(
        ts=raw.ts,
        cps=round(cps, 1),
        cps_label=label,
        mvrv_z_score=mvrv_z,
        mvrv_z_contribution=mvrv_contrib,
        ahr999_value=ahr999,
        ahr999_contribution=ahr_contrib,
        price_vs_200w_ratio=round(sma_ratio, 4) if sma_ratio is not None else None,
        price_vs_200w_contribution=sma_contrib,
        price_vs_sth_label=sth_label,
        price_vs_sth_contribution=sth_contrib,
        pi_cycle_ratio=round(pi_ratio, 4) if pi_ratio is not None else None,
        pi_cycle_contribution=pi_contrib,
        rplr_proxy=round(rplr, 6) if rplr is not None else None,
        btc_rsi_daily=rsi,
        sma_200w=raw.sma_200w,
        sth_cost_1d=raw.sth_cost_1d,
        sth_cost_1w=raw.sth_cost_1w,
        sth_cost_1m=raw.sth_cost_1m,
        sth_cost_3m=raw.sth_cost_3m,
        pi_350dma=raw.pi_350dma,
        pi_111dma_x2=raw.pi_111dma_x2,
        cvdd=raw.cvdd,
    )

    logger.info(
        "CPS calculated | cps=%.1f label=%s mvrv_z=%s ahr999=%s sma_ratio=%s pi_ratio=%s rplr=%s rsi=%s",
        result.cps, result.cps_label,
        f"{mvrv_z:.2f}" if mvrv_z is not None else "N/A",
        f"{ahr999:.4f}" if ahr999 is not None else "N/A",
        f"{sma_ratio:.2f}" if sma_ratio is not None else "N/A",
        f"{pi_ratio:.3f}" if pi_ratio is not None else "N/A",
        f"{rplr:.4f}" if rplr is not None else "N/A",
        f"{rsi:.1f}" if rsi is not None else "N/A",
    )
    return result


def _calc_rsi(prices: list[float], period: int = 14) -> Optional[float]:
    """Wilder 平滑 RSI"""
    if len(prices) < period + 1:
        return None

    changes = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    avg_gain = sum(max(c, 0) for c in changes[:period]) / period
    avg_loss = sum(abs(min(c, 0)) for c in changes[:period]) / period

    for c in changes[period:]:
        avg_gain = (avg_gain * (period - 1) + max(c, 0)) / period
        avg_loss = (avg_loss * (period - 1) + abs(min(c, 0))) / period

    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - 100 / (1 + rs), 1)
