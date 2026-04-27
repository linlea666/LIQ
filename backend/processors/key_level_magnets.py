"""Key Level 清算磁铁通道（M1 · V3 准备阶段）。

为什么独立成 channel（不进 candidate 池）：
  V3 评审采纳：max_pain / 高杠杆密度带是"价格磁铁"而非"支撑/阻力"。
  - 直接进 candidate 池升 S → 单源单证据触发伪 S 信号、稀释关键位密度
  - 但用户应能看到"全市场多头痛点位"作为参考；故独立通道，前端紫色 💥 徽标

输入：
  - liq_max_pain_24h: LiqMaxPainItem | None    （已 _pick_max_pain_for_coin 过滤）
  - liq_heatmap_24h:  HeatmapData | None
  - levels:           list[KeyLevelV2]          （用于去重：与已有 level 距离 > 0.5×ATR 才入选）
  - current_price:    float
  - atr:              float
  - max_density_bands: int = 3                  （从 heatmap 取 top-N 高密度带做 leverage_magnet）

输出：
  list[LiqMagnetLevel]，按距当前价排序

设计要点（保守 dev-constraints #6）：
  - 不影响 levels 列表 / strength_tier 评分
  - 仅生成独立通道供 UI/AI 展示
  - 与已有 level 距离过近时跳过（避免重复）
"""

from __future__ import annotations

import logging
from typing import Optional

from models.key_level import KeyLevelV2, LiqMagnetLevel
from models.liquidation import HeatmapData, LiqMaxPainItem

logger = logging.getLogger(__name__)


def _fmt_usd_cn(usd: float) -> str:
    """中文 USD 格式化（与 level_discovery.fmt_usd_cn 同步）"""
    sign = "-" if usd < 0 else ""
    a = abs(usd)
    if a >= 1e8:
        return f"{sign}{a / 1e8:.1f}亿"
    if a >= 1e7:
        return f"{sign}{a / 1e7:.0f}千万"
    if a >= 1e6:
        return f"{sign}{a / 1e6:.0f}百万"
    if a >= 1e4:
        return f"{sign}{a / 1e4:.0f}万"
    if a >= 1:
        return f"{sign}{a:,.0f}"
    return "0"


def _too_close_to_existing_level(
    price: float,
    levels: list[KeyLevelV2],
    atr: float,
    threshold: float = 0.5,
) -> bool:
    """与已有 level 距离 < threshold × ATR 视为重复，跳过。

    设计：磁铁与 level 重合时优先显示 level（信息密度更高）。
    """
    if atr <= 0:
        return False
    cutoff = atr * threshold
    for lv in levels:
        if abs(lv.price - price) < cutoff:
            return True
    return False


def discover_magnets(
    *,
    liq_max_pain_24h: Optional[LiqMaxPainItem],
    liq_heatmap_24h: Optional[HeatmapData],
    levels: list[KeyLevelV2],
    current_price: float,
    atr: float,
    max_density_bands: int = 3,
) -> list[LiqMagnetLevel]:
    """生成独立的清算磁铁通道。

    返回：list[LiqMagnetLevel]，按 |distance_pct| 升序
    """
    out: list[LiqMagnetLevel] = []
    if current_price <= 0:
        return out

    # ── A. max_pain 双痛点 ─────────────────────────────────────────
    if liq_max_pain_24h is not None:
        long_p = float(getattr(liq_max_pain_24h, "long_pain_price", 0) or 0)
        long_usd = float(getattr(liq_max_pain_24h, "long_pain_usd", 0) or 0)
        short_p = float(getattr(liq_max_pain_24h, "short_pain_price", 0) or 0)
        short_usd = float(getattr(liq_max_pain_24h, "short_pain_usd", 0) or 0)

        # 多头痛点（价格"下行"触达 → 多头集中爆仓；通常 < 当前价 → 显示为下方磁吸）
        if long_p > 0 and not _too_close_to_existing_level(long_p, levels, atr):
            dist = (long_p - current_price) / current_price * 100
            out.append(LiqMagnetLevel(
                price=round(long_p, 2),
                magnet_role="downside_pain_center",
                source="max_pain_long",
                usd=long_usd,
                distance_pct=round(dist, 2),
                note=f"全市场多头痛点 {_fmt_usd_cn(long_usd)}，下破后磁吸点",
            ))

        # 空头痛点（价格"上行"触达 → 空头集中爆仓；通常 > 当前价）
        if short_p > 0 and not _too_close_to_existing_level(short_p, levels, atr):
            dist = (short_p - current_price) / current_price * 100
            out.append(LiqMagnetLevel(
                price=round(short_p, 2),
                magnet_role="upside_short_squeeze",
                source="max_pain_short",
                usd=short_usd,
                distance_pct=round(dist, 2),
                note=f"全市场空头痛点 {_fmt_usd_cn(short_usd)}，上破后轧空磁吸点",
            ))

    # ── B. heatmap top-N 高密度带（leverage_magnet）─────────────────
    # heatmap.data: list[HeatmapDataPoint(price, value, ts)]
    # 取 value 最大的 max_density_bands 个；同价位附近合并；与 level 去重
    if liq_heatmap_24h is not None and liq_heatmap_24h.data:
        # 距离过滤（仅取 ±15% 以内的高密度带，远端没参考价值）
        radius = current_price * 0.15
        candidates = [
            (p.price, p.value)
            for p in liq_heatmap_24h.data
            if p.price > 0 and abs(p.price - current_price) <= radius
        ]
        # 按强度降序
        candidates.sort(key=lambda t: t[1], reverse=True)

        added = 0
        seen_buckets: set[float] = set()
        # 桶宽：用 atr × 0.5 作合并容差；fallback 到 0.3% 价格
        bucket_w = max(atr * 0.5, current_price * 0.003) if atr > 0 else current_price * 0.003

        for price, value in candidates:
            if added >= max_density_bands:
                break
            if value <= 0:
                continue
            # 合并相邻热力点
            bucket_key = round(price / bucket_w) * bucket_w
            if bucket_key in seen_buckets:
                continue
            # 去重 vs level
            if _too_close_to_existing_level(price, levels, atr):
                continue
            # 去重 vs 已加入的 magnet（max_pain / 前面的 heatmap）
            if any(abs(price - m.price) < bucket_w for m in out):
                continue

            seen_buckets.add(bucket_key)
            dist = (price - current_price) / current_price * 100
            out.append(LiqMagnetLevel(
                price=round(price, 2),
                magnet_role="leverage_magnet",
                source="heatmap_top_density",
                usd=round(value, 2),
                distance_pct=round(dist, 2),
                note=f"杠杆密度高发带 {_fmt_usd_cn(value)}，价格易被磁吸",
            ))
            added += 1

    # 按距离绝对值升序（最近的优先展示）
    out.sort(key=lambda m: abs(m.distance_pct))
    return out
