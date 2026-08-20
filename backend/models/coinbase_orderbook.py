"""Coinbase 现货原生订单簿数据模型（Phase C）。

Coinbase Exchange 公开端点：
    GET https://api.exchange.coinbase.com/products/{product_id}/book?level=2

字段（实测 BTC-USD probe，2026-04-28）：
    {
      "bids": [[price_str, size_str, num_orders], ...],
      "asks": [[price_str, size_str, num_orders], ...],
      "sequence": int,
      "auction_mode": bool,
      "auction": dict|None,
      "time": ISO8601 str
    }

实测体量：
    bids ~22000+ 档、asks ~24000+ 档（全档位深度）
    ±0.5% 内 ~250-300 档，USD 量级 $14-17M
    spread 通常 1 cent (≈ 0.001 bp)

设计要点：
    - Coinbase 用 USD（法币），与 Binance/OKX 的 USDT 略有差异（典型 spread < 5bp）
    - 第三个字段 num_orders 是 Coinbase 独有：可区分"1 笔大单 vs N 笔小单聚集"
    - 远端档位（distance > 5%）噪声多，由墙引擎现有 ±5% 距离过滤兜底
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class CoinbaseBookLevel(BaseModel):
    """Coinbase level=2 单个价位档。

    Coinbase API 返回 [price_str, size_str, num_orders]：
        - price/size 是 string 类型（避免精度损失）
        - num_orders 是该价位上的订单笔数（≥3 表示多笔聚集，更可信；=1 表示孤立大单）
    """
    price: float                              # USD 价位
    size: float                               # base coin 数量（如 BTC）
    num_orders: int                           # 该价位订单笔数

    @property
    def usd_value(self) -> float:
        return self.price * self.size


class CoinbaseOrderbookFrame(BaseModel):
    """Coinbase 现货订单簿快照（顶层 state 字段类型）。

    bids / asks 升序排序（与 OrderbookDepthSnapshot 保持一致）。
    时间戳使用秒（与项目其他 model 同步）。
    """
    coin: str                                 # 币种 (BTC/ETH/SOL ...)
    product_id: str                           # Coinbase 产品 ID (BTC-USD)
    ts_sec: int                               # 拉取时刻（local now）
    api_ts_iso: str = ""                      # API 返回的 time 字段（保留方便排查时间偏差）
    sequence: Optional[int] = None            # API 返回的 sequence number
    watermark: Optional[int] = None           # 已接受的最高 sequence；持久化用于重启防倒退
    source_capability: str = "snapshot_only" # REST L2 快照不能证明逐增量连续性
    validity: str = "valid"
    validity_reasons: list[str] = Field(default_factory=list)

    bids: list[CoinbaseBookLevel] = Field(default_factory=list)
    asks: list[CoinbaseBookLevel] = Field(default_factory=list)

    bid_count: int = 0                        # = len(bids)（冗余存放，便于快速校验）
    ask_count: int = 0
