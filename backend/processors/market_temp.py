"""市场温度计 + 插针风险等级 + 因子卡片 + 瀑布图"""

from __future__ import annotations

import logging
import time

from typing import Optional, Tuple

from config.settings import get_settings
from models.flow import (
    BasisData, CVDData, ETFFlowData, FundingRateData,
    GlobalLiquidationData, LongShortRatioData, MarketIndexData,
    OIData, TakerFlowData,
)
from models.liquidation import LiquidationMap, LiquidationStats

from models.snapshot import (
    FactorCard,
    MarketTemperature,
    WaterfallData,
    WaterfallItem,
)

logger = logging.getLogger(__name__)


def calc_market_temperature(
    coin: str,
    funding: FundingRateData | None,
    oi: OIData | None,
    cvd_contract: CVDData | None,
    basis: BasisData | None,
    liq_map: LiquidationMap | None,
    liq_stats: LiquidationStats | None,
    taker_flow: TakerFlowData | None,
    atr: float = 0,
    ls_ratio: Optional[LongShortRatioData] = None,
    market_index: Optional[MarketIndexData] = None,
    etf_flow: Optional[ETFFlowData] = None,
    global_liq: Optional[GlobalLiquidationData] = None,
) -> Tuple[MarketTemperature, dict[str, float]]:
    """
    综合 12 个因子计算市场温度(0-100)和插针风险等级。
    """
    weights = get_settings().processors.market_temp["weights"]

    factor_scores: dict[str, float] = {}  # -50 到 +50 范围

    # D1: 清算失衡度
    d1_score = 0.0
    d1_value = "0"
    d1_sub = ""
    d1_summary = "数据不足"
    if liq_map and liq_map.imbalance_ratio > 0:
        ratio = liq_map.imbalance_ratio
        d1_score = min((ratio - 1) * 20, 50) if ratio > 1 else max((ratio - 1) * 20, -50)
        d1_value = f"{ratio:+.1f}" if abs(ratio - 1) > 0.1 else "≈1.0"
        above_total = sum(c.total_usd for c in liq_map.clusters_above) / 1e8
        below_total = sum(c.total_usd for c in liq_map.clusters_below) / 1e8
        d1_sub = f"空{above_total:.1f}亿 / 多{below_total:.1f}亿"
        d1_summary = "上扫空头" if ratio > 1.2 else "下扫多头" if ratio < 0.8 else "多空均衡"
    factor_scores["liq_imbalance"] = d1_score

    # D2: CVD动向
    d2_score = 0.0
    d2_value = "N/A"
    d2_sub = ""
    d2_summary = "数据不足"
    if cvd_contract:
        d2_score = min(cvd_contract.delta_1h / 1e5, 50) if cvd_contract.delta_1h > 0 else max(cvd_contract.delta_1h / 1e5, -50)
        delta_m = cvd_contract.delta_1h / 1e6
        d2_value = f"{delta_m:+.1f}M" if abs(delta_m) > 0.1 else "~0"
        d2_sub = f"合约:{cvd_contract.trend_1h}"
        d2_summary = "买方主导" if cvd_contract.trend_1h == "rising" else "卖方主导" if cvd_contract.trend_1h == "declining" else "多空拉锯"
        if cvd_contract.has_divergence:
            d2_summary += f" ⚠️{cvd_contract.divergence_note[:6]}"
    factor_scores["cvd"] = d2_score

    # D3: OI杠杆
    d3_score = 0.0
    d3_value = "N/A"
    d3_sub = ""
    d3_summary = "数据不足"
    if oi:
        d3_score = max(min(oi.change_1h_pct * 10, 50), -50)
        d3_value = f"${oi.current_usd / 1e9:.1f}B"
        d3_sub = f"1h:{oi.change_1h_pct:+.1f}%"
        if oi.change_1h_pct > 3:
            d3_summary = "杠杆急升"
        elif oi.change_1h_pct < -3:
            d3_summary = "杠杆清洗"
        elif abs(oi.change_1h_pct) < 0.5:
            d3_summary = "杠杆稳定"
        else:
            d3_summary = "缓慢变化"
    factor_scores["oi"] = d3_score

    # D4: 资金费率
    d4_score = 0.0
    d4_value = "N/A"
    d4_sub = ""
    d4_summary = "数据不足"
    if funding:
        rate = funding.avg_rate
        d4_score = min(rate * 1e4 * 10, 50) if rate > 0 else max(rate * 1e4 * 10, -50)
        d4_value = f"{rate * 100:.4f}%"
        parts = []
        if funding.okx_rate is not None:
            parts.append(f"OKX:{funding.okx_rate * 100:.4f}%")
        if funding.binance_rate is not None:
            parts.append(f"BN:{funding.binance_rate * 100:.4f}%")
        d4_sub = " ".join(parts) if parts else ""
        d4_summary = funding.interpretation
    factor_scores["funding"] = d4_score

    # D5: 期现溢价
    d5_score = 0.0
    d5_value = "N/A"
    d5_sub = ""
    d5_summary = "数据不足"
    if basis:
        d5_score = max(min(basis.basis_pct * 100, 50), -50)
        d5_value = f"{basis.basis_pct:+.3f}%"
        d5_sub = f"标记${basis.mark_price:.0f}"
        d5_summary = basis.interpretation or ("合约偏贵" if basis.basis_pct > 0.1 else "合约折价" if basis.basis_pct < -0.1 else "中性")
    factor_scores["basis"] = d5_score

    # D6: 买卖力量
    d6_score = 0.0
    d6_value = "N/A"
    d6_sub = ""
    d6_summary = "数据不足"
    if taker_flow:
        d6_score = (taker_flow.buy_ratio - 0.5) * 100
        d6_value = f"{'买' if taker_flow.buy_ratio > 0.5 else '卖'}>{('买' if taker_flow.buy_ratio <= 0.5 else '卖')}"
        d6_sub = f"买{taker_flow.buy_ratio:.0%} 卖{taker_flow.sell_ratio:.0%}"
        d6_summary = "买方强势" if taker_flow.dominant == "buyers" else "卖方强势" if taker_flow.dominant == "sellers" else "势均力敌"
    factor_scores["taker"] = d6_score

    # D7: 波幅范围
    d7_value = f"${atr:.0f}" if atr > 0 else "N/A"
    d7_summary = "波动适中"
    d7_sub = "ATR(14)"

    # D8: 爆仓烈度
    d8_score = 0.0
    d8_value = "N/A"
    d8_sub = ""
    d8_summary = "数据不足"
    if liq_stats:
        ratio = liq_stats.ratio
        d8_score = min((ratio - 1) * 15, 50) if ratio > 1 else max((ratio - 1) * 15, -50)
        d8_value = f"多{liq_stats.long_count}:空{liq_stats.short_count}"
        d8_sub = f"多${liq_stats.long_total_usd / 1e6:.1f}M 空${liq_stats.short_total_usd / 1e6:.1f}M"
        d8_summary = "多头被清洗" if ratio > 2 else "空头被清洗" if ratio < 0.5 else "双向均衡"
    factor_scores["liq_intensity"] = d8_score

    # D9: 多空比
    d9_score = 0.0
    d9_value = "N/A"
    d9_sub = ""
    d9_summary = "数据不足"
    if ls_ratio and ls_ratio.exchanges:
        r = ls_ratio.avg_ratio
        d9_score = max(min((r - 1) * 30, 50), -50)
        d9_value = f"{r:.2f}"
        d9_sub = ls_ratio.interpretation
        d9_summary = "多头拥挤" if r > 1.3 else "空头拥挤" if r < 0.77 else "多空均衡"
    factor_scores["ls_ratio"] = d9_score

    # D10: 恐惧贪婪指数
    d10_score = 0.0
    d10_value = "N/A"
    d10_sub = ""
    d10_summary = "数据不足"
    if market_index and market_index.fear_greed is not None:
        fgi = market_index.fear_greed
        d10_score = max(min((fgi - 50) * 1.0, 50), -50)
        d10_value = f"{int(fgi)}"
        if fgi >= 75:
            d10_summary = "极度贪婪"
        elif fgi >= 55:
            d10_summary = "贪婪"
        elif fgi >= 45:
            d10_summary = "中性"
        elif fgi >= 25:
            d10_summary = "恐惧"
        else:
            d10_summary = "极度恐惧"
        d10_sub = d10_summary
    factor_scores["fear_greed"] = d10_score

    # D11: ETF 资金流
    d11_score = 0.0
    d11_value = "N/A"
    d11_sub = ""
    d11_summary = "数据不足"
    if etf_flow and etf_flow.recent_days:
        net_3d_m = etf_flow.net_3d / 1e6
        d11_score = max(min(net_3d_m / 10, 50), -50)
        d11_value = f"{'+'if net_3d_m > 0 else ''}{net_3d_m:.0f}M"
        d11_sub = f"3日净{'流入' if net_3d_m > 0 else '流出'}"
        d11_summary = "机构加仓" if net_3d_m > 100 else "机构撤退" if net_3d_m < -100 else "机构观望"
    factor_scores["etf_flow"] = d11_score

    # D12: 全网爆仓不对称
    d12_score = 0.0
    d12_value = "N/A"
    d12_sub = ""
    d12_summary = "数据不足"
    if global_liq:
        r24 = global_liq.ratio_24h
        d12_score = max(min((r24 - 1) * 15, 50), -50)
        long_m = global_liq.long_24h_usd / 1e6
        short_m = global_liq.short_24h_usd / 1e6
        d12_value = f"多${long_m:.0f}M:空${short_m:.0f}M"
        d12_sub = f"24h多空比{r24:.1f}"
        d12_summary = "多头被清洗" if r24 > 2 else "空头被清洗" if r24 < 0.5 else "双向清洗"
    factor_scores["global_liq"] = d12_score

    # ── 综合温度 ──
    # 12 因子加权（D7 无方向不参与）
    w = weights
    temp = 50 + (
        factor_scores.get("liq_imbalance", 0) * w.get("liq_imbalance", 0.08)
        + factor_scores.get("cvd", 0) * w.get("cvd_trend", 0.14)
        + factor_scores.get("oi", 0) * w.get("oi_change", 0.10)
        + factor_scores.get("funding", 0) * w.get("funding_rate", 0.10)
        + factor_scores.get("basis", 0) * w.get("basis", 0.07)
        + factor_scores.get("taker", 0) * w.get("taker_flow", 0.13)
        + factor_scores.get("liq_intensity", 0) * w.get("liquidation_ratio", 0.08)
        + factor_scores.get("ls_ratio", 0) * w.get("ls_ratio", 0.08)
        + factor_scores.get("fear_greed", 0) * w.get("fear_greed", 0.07)
        + factor_scores.get("etf_flow", 0) * w.get("etf_flow", 0.08)
        + factor_scores.get("global_liq", 0) * w.get("global_liq", 0.07)
    )
    temp = max(0, min(100, temp))

    if temp >= 80:
        temp_label = "极热"
    elif temp >= 65:
        temp_label = "偏热"
    elif temp >= 35:
        temp_label = "中性"
    elif temp >= 20:
        temp_label = "偏冷"
    else:
        temp_label = "极冷"

    # ── 插针风险 ──
    pin_risk = _calc_pin_risk_level(liq_map, oi, cvd_contract)

    # ── 组装因子卡片 ──
    cards = [
        FactorCard(id="D1", name="清算失衡", value=d1_value, direction=_dir(d1_score), sub_text=d1_sub, percentile=50, summary=d1_summary),
        FactorCard(id="D2", name="CVD动向", value=d2_value, direction=_dir(d2_score), sub_text=d2_sub, percentile=50, summary=d2_summary),
        FactorCard(id="D3", name="OI杠杆", value=d3_value, direction=_dir(d3_score), sub_text=d3_sub, percentile=50, summary=d3_summary),
        FactorCard(id="D4", name="费率拥挤", value=d4_value, direction=_dir(d4_score), sub_text=d4_sub, percentile=50, summary=d4_summary),
        FactorCard(id="D5", name="期现溢价", value=d5_value, direction=_dir(d5_score), sub_text=d5_sub, percentile=50, summary=d5_summary),
        FactorCard(id="D6", name="买卖力量", value=d6_value, direction=_dir(d6_score), sub_text=d6_sub, percentile=50, summary=d6_summary),
        FactorCard(id="D7", name="波幅范围", value=d7_value, direction="neutral", sub_text=d7_sub, percentile=50, summary=d7_summary),
        FactorCard(id="D8", name="爆仓烈度", value=d8_value, direction=_dir(d8_score), sub_text=d8_sub, percentile=50, summary=d8_summary),
        FactorCard(id="D9", name="多空比", value=d9_value, direction=_dir(d9_score), sub_text=d9_sub, percentile=50, summary=d9_summary),
        FactorCard(id="D10", name="恐惧贪婪", value=d10_value, direction=_dir(d10_score), sub_text=d10_sub, percentile=50, summary=d10_summary),
        FactorCard(id="D11", name="ETF资金", value=d11_value, direction=_dir(d11_score), sub_text=d11_sub, percentile=50, summary=d11_summary),
        FactorCard(id="D12", name="全网爆仓", value=d12_value, direction=_dir(d12_score), sub_text=d12_sub, percentile=50, summary=d12_summary),
    ]

    result = MarketTemperature(
        coin=coin,
        ts=int(time.time()),
        score=round(temp, 1),
        label=temp_label,
        pin_risk_level=pin_risk[0],
        pin_risk_label=pin_risk[1],
        factors=cards,
    )
    return result, factor_scores


def build_waterfall(
    temp: MarketTemperature,
    factor_scores: dict[str, float] | None = None,
) -> WaterfallData:
    """从因子卡片构建多空归因瀑布图。

    factor_scores 为因子原始分(-50~+50)，乘以权重得到实际贡献。
    若未传入则从 direction 做粗略估算（兼容旧调用）。
    """
    weights = get_settings().processors.market_temp["weights"]
    factor_id_to_weight_key = {
        "D1": "liq_imbalance",
        "D2": "cvd_trend",
        "D3": "oi_change",
        "D4": "funding_rate",
        "D5": "basis",
        "D6": "taker_flow",
        "D8": "liquidation_ratio",
        "D9": "ls_ratio",
        "D10": "fear_greed",
        "D11": "etf_flow",
        "D12": "global_liq",
    }

    items: list[WaterfallItem] = []
    bullish_total = 0.0
    bearish_total = 0.0

    for card in temp.factors:
        if card.id == "D7":
            continue
        if factor_scores is not None:
            score_key = {
                "D1": "liq_imbalance", "D2": "cvd", "D3": "oi",
                "D4": "funding", "D5": "basis", "D6": "taker",
                "D8": "liq_intensity", "D9": "ls_ratio",
                "D10": "fear_greed", "D11": "etf_flow",
                "D12": "global_liq",
            }.get(card.id, "")
            raw = factor_scores.get(score_key, 0)
            w_key = factor_id_to_weight_key.get(card.id, "")
            w = weights.get(w_key, 0.1)
            contrib = raw * w
        else:
            contrib = _estimate_contribution(card)
        direction = "bullish" if contrib > 0 else "bearish"
        if contrib > 0:
            bullish_total += contrib
        else:
            bearish_total += contrib
        items.append(WaterfallItem(
            factor_id=card.id,
            factor_name=card.name,
            contribution_pct=round(contrib, 1),
            direction=direction,
        ))

    items.sort(key=lambda x: abs(x.contribution_pct), reverse=True)
    net = bullish_total + bearish_total
    net_label = "偏向看多" if net > 5 else "偏向看空" if net < -5 else "多空均衡"

    return WaterfallData(
        coin=temp.coin,
        ts=temp.ts,
        items=items,
        bullish_total=round(bullish_total, 1),
        bearish_total=round(bearish_total, 1),
        net_bias=round(net, 1),
        net_label=net_label,
    )


def _dir(score: float) -> str:
    if score > 5:
        return "bullish"
    elif score < -5:
        return "bearish"
    return "neutral"


def _estimate_contribution(card: FactorCard) -> float:
    base = {"bullish": 15, "bearish": -15, "neutral": 0}
    return base.get(card.direction, 0)


def _calc_pin_risk_level(
    liq_map: LiquidationMap | None,
    oi: OIData | None,
    cvd: CVDData | None,
) -> tuple[str, str]:
    """
    插针风险等级:
    - low: 清算池远, OI正常
    - attention: 清算池<2%, OI在堆积
    - high: 清算池<1%, OI极端+CVD背离迹象
    - extreme: 即将触碰清算池
    """
    risk_score = 0

    if liq_map:
        nearest_above = liq_map.clusters_above[0].distance_pct if liq_map.clusters_above else 99
        nearest_below = liq_map.clusters_below[0].distance_pct if liq_map.clusters_below else 99
        nearest = min(nearest_above, nearest_below)

        if nearest < 0.5:
            risk_score += 4
        elif nearest < 1.0:
            risk_score += 3
        elif nearest < 2.0:
            risk_score += 2
        elif nearest < 3.0:
            risk_score += 1

    if oi and abs(oi.change_1h_pct) > 5:
        risk_score += 2
    elif oi and abs(oi.change_1h_pct) > 2:
        risk_score += 1

    if cvd and cvd.has_divergence:
        risk_score += 2

    if risk_score >= 6:
        return "extreme", "🔴 极高"
    elif risk_score >= 4:
        return "high", "🟠 中高"
    elif risk_score >= 2:
        return "attention", "🟡 关注"
    else:
        return "low", "🟢 低风险"
