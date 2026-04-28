"""Coinbase 现货原生订单簿轮询（Phase C）。

写入 ``state.coinbase_orderbook``（单帧 latest 快照，不存历史）。
墙引擎 ``_augment_zones_with_coinbase`` 直接消费 latest 帧，按合约 zone 价区
[price_low, price_high] 累加 Coinbase USD 厚度，作为"机构现货验证"维度。

为何不存 deque 历史：
    Coinbase 数据用途仅是"当前快照下机构是否在该价位有挂单"，
    属于即时验证。trust_score 仅消费 current_usd（USD/USDT 容差容忍下的瞬时厚度），
    不需要历史持续性（墙的 persistence 仍由合约 5m heatmap 历史承担）。

降级：
    - 单次失败：跳过本 cycle，不更新 state.coinbase_orderbook（保留旧值）
    - 30min 未更新：墙引擎检测 stale，coinbase_spot_confluence 不参与计算
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from config.settings import CoinConfig
from sources.coinbase_native import CoinbaseNativeSource, parse_orderbook_frame

if TYPE_CHECKING:
    from engine import CoinState

logger = logging.getLogger(__name__)


def _resolve_product_id(coin: CoinConfig) -> Optional[str]:
    """派生 Coinbase product_id。

    优先级：
        1. coin.symbol_coinbase（config.yaml 显式指定，包含空字符串=禁用）
        2. f"{coin.ccy}-USD"（默认）

    显式禁用约定：symbol_coinbase 设为空字符串 "" → 返回 None，跳过该币 Coinbase 拉取。
    """
    explicit = getattr(coin, "symbol_coinbase", None)
    if explicit is not None:
        explicit = str(explicit).strip()
        if not explicit:
            return None
        return explicit
    return f"{coin.ccy}-USD"


async def poll_coinbase_orderbook(
    cb: CoinbaseNativeSource, coin: CoinConfig, state: "CoinState",
) -> None:
    """拉取 Coinbase 现货 orderbook level=2，写入 state.coinbase_orderbook。

    ``cb`` 是独立的 CoinbaseNativeSource 实例（不复用 CoinglassSource），
    其 rate limiter 也独立，不消耗 Coinglass 配额。
    """
    product_id = _resolve_product_id(coin)
    if not product_id:
        return

    raw = await cb.fetch_orderbook(product_id=product_id, level=2)
    if not raw:
        return

    frame = parse_orderbook_frame(coin=coin.ccy, product_id=product_id, raw=raw)
    if frame is None:
        return

    state.coinbase_orderbook = frame

    log_key = f"coinbase_ob_ready_{coin.ccy}"
    if log_key not in state._log_once_keys:
        state._log_once_keys.add(log_key)
        logger.info(
            "Coinbase 现货 orderbook 接通 | coin=%s product=%s bids=%d asks=%d "
            "spread=%.2f$ api_ts=%s",
            coin.ccy, product_id, frame.bid_count, frame.ask_count,
            (frame.asks[0].price - frame.bids[-1].price) if (frame.bids and frame.asks) else 0.0,
            frame.api_ts_iso,
        )
