"""派生标签 · 3 个关键判定字段

- oi_price_coherence:     OI 变化方向 × 价格变化方向
- spot_contract_coherence: 现货 CVD × 合约 CVD 方向一致性
- funding_trend:          funding 拥挤度状态
"""

from __future__ import annotations

from typing import Optional

from models.market_action import (
    FundingTrend,
    MarketActionCoherence,
    SpotContractCoherence,
)


def derive_oi_price_coherence(
    oi_change_pct: Optional[float],
    price_change_pct: Optional[float],
) -> MarketActionCoherence:
    """OI × Price 一致性判定

    confirming:
      - 价格上涨 + OI 上升 → 新多入场，趋势确认
      - 价格下跌 + OI 上升 → 新空入场，趋势确认
    diverging:
      - 价格上涨 + OI 下降 → 空头回补，潜在衰竭
      - 价格下跌 + OI 下降 → 多头止损，卖盘耗尽
    neutral: 任一为 None 或幅度过小
    """
    if oi_change_pct is None or price_change_pct is None:
        return "neutral"
    if abs(price_change_pct) < 0.1 or abs(oi_change_pct) < 0.1:
        return "neutral"
    if (price_change_pct > 0 and oi_change_pct > 0) or (price_change_pct < 0 and oi_change_pct > 0):
        return "confirming"
    return "diverging"


def derive_spot_contract_coherence(
    spot_delta_1h: Optional[float],
    contract_delta_1h: Optional[float],
    spot_trend: Optional[str],
    contract_trend: Optional[str],
) -> SpotContractCoherence:
    """现货 × 合约 CVD 一致性

    aligned: 同方向
    spot_leads: 现货大幅主动买/卖，合约跟随
    spot_lags: 合约大幅主动买/卖，现货未跟
    unknown: 数据缺失
    """
    if spot_delta_1h is None or contract_delta_1h is None:
        return "unknown"
    sd = spot_delta_1h
    cd = contract_delta_1h
    if sd == 0 and cd == 0:
        return "unknown"
    # 同号 & 幅度接近 → aligned
    if sd * cd > 0:
        ratio = abs(sd) / abs(cd) if cd != 0 else 0
        if 0.5 <= ratio <= 2.0:
            return "aligned"
        return "spot_leads" if abs(sd) > abs(cd) else "spot_lags"
    # 异号 → 看绝对值谁大（"领先方"在反方向上）
    if abs(sd) > abs(cd) * 1.5:
        return "spot_leads"
    if abs(cd) > abs(sd) * 1.5:
        return "spot_lags"
    return "aligned"


def derive_funding_trend(
    current: Optional[float],
    avg_7d: Optional[float],
) -> FundingTrend:
    """Funding 趋势判定（极端 / 累积 / 平稳 / 松动）"""
    if current is None:
        return "stable"
    # 极端阈值（年化 ≈ 30%+，0.01% × 8h × 365）
    if abs(current) >= 0.0005:
        return "extreme"
    if avg_7d is None:
        return "stable"
    diff = current - avg_7d
    threshold = max(abs(avg_7d) * 0.5, 0.00005)
    if diff > threshold:
        return "building"
    if diff < -threshold:
        return "easing"
    return "stable"
