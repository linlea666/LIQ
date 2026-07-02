"""Hyperliquid 巨鲸价格分布的只读公开契约。"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.trend_monitor import DataQuality


class HyperliquidWhalePriceBucket(BaseModel):
    price_from: float
    price_to: float
    price_mid: float
    distance_from_mark_pct: float
    long_notional_usd: float = 0
    short_notional_usd: float = 0
    long_count: int = 0
    short_count: int = 0
    long_avg_leverage: float = 0
    short_avg_leverage: float = 0


class HyperliquidWhaleAssetDistribution(BaseModel):
    symbol: Literal["BTC", "ETH"]
    mark_price: Optional[float] = None
    as_of_ts: Optional[int] = None
    bin_size_pct: float = 0.5
    position_count: int = 0
    long_count: int = 0
    short_count: int = 0
    long_notional_usd: float = 0
    short_notional_usd: float = 0
    valid_entry_price_count: int = 0
    invalid_entry_price_count: int = 0
    valid_liquidation_price_count: int = 0
    invalid_liquidation_price_count: int = 0
    entry_buckets: list[HyperliquidWhalePriceBucket] = Field(default_factory=list)
    liquidation_buckets: list[HyperliquidWhalePriceBucket] = Field(default_factory=list)
    quality: DataQuality = Field(default_factory=DataQuality)
    caveats: list[str] = Field(default_factory=list)


class HyperliquidWhaleDistributions(BaseModel):
    source: Literal["coinglass_hyperliquid_whale_position"] = (
        "coinglass_hyperliquid_whale_position"
    )
    sample_scope: str = "CoinGlass 官方 Hyperliquid 巨鲸仓位池"
    fetched_at_ts: Optional[int] = None
    score_weight: Literal[0] = 0
    assets: dict[Literal["BTC", "ETH"], HyperliquidWhaleAssetDistribution]


def pending_hyperliquid_whale_distributions() -> HyperliquidWhaleDistributions:
    assets = {
        symbol: HyperliquidWhaleAssetDistribution(
            symbol=symbol,
            quality=DataQuality(
                valid=False,
                status="pending",
                reason="等待首轮 Hyperliquid 巨鲸仓位快照",
            ),
            caveats=_default_caveats(),
        )
        for symbol in ("BTC", "ETH")
    }
    return HyperliquidWhaleDistributions(assets=assets)


def _default_caveats() -> list[str]:
    return [
        "CoinGlass 官方巨鲸筛选门槛未公开，样本不代表 Hyperliquid 全部仓位。",
        "交叉保证金爆仓价会随账户权益、其他仓位和资金费用变化。",
        "厚度表示当前名义仓位暴露，不是挂单墙或确定发生的清算量。",
        "开仓成本密集区仅供结构观察，不等于建议开仓价。",
    ]
