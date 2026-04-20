"""官方交易所 funding rate 取数（Binance + OKX 公共接口）

目的：
    作为 LIQ 的 funding rate 主源，替换 Coinglass 的多交易所聚合。
    Coinglass 仅作为"官方两家同时不可用"时的 fallback。

单位契约（关键）：
    - Binance /fapi/v1/premiumIndex.lastFundingRate → 字符串形式的小数
      例如 "0.00010000" = 0.01% = 1 bp
    - OKX /api/v5/public/funding-rate.fundingRate    → 字符串形式的小数
      例如 "0.0001234"  = 0.01234%
    两家口径**统一为小数**，与 LIQ 下游阈值 ±0.0005 完全对齐，
    不存在 ×100 / ×10 放缩风险。

接口选型：
    - Binance: /fapi/v1/premiumIndex?symbol=BTCUSDT
      公共接口，无 key，全球 CDN，平均延迟 80-120ms
    - OKX    : /api/v5/public/funding-rate?instId=BTC-USDT-SWAP
      公共接口，无 key，亚太节点，平均延迟 60-100ms

错误处理：
    - 单家失败 → 返回 None，不抛异常
    - 上层（derivatives.poll_funding_all）根据结果决定是否 fallback
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=6)


def to_okx_inst_id(symbol_pair: str) -> str:
    """BTCUSDT → BTC-USDT-SWAP（兼容 symbol_cg_pair 直接派生）"""
    pair = (symbol_pair or "").upper().strip()
    if pair.endswith("USDT"):
        base = pair[:-4]
        return f"{base}-USDT-SWAP"
    if pair.endswith("USDC"):
        base = pair[:-4]
        return f"{base}-USDC-SWAP"
    # 已经是 instId 形态就原样返回
    return pair


async def fetch_binance_funding(
    session: aiohttp.ClientSession, symbol: str,
) -> Optional[float]:
    """Binance 永续 funding rate（小数，如 0.0001 = 0.01%）"""
    try:
        async with session.get(
            _BINANCE_PREMIUM_URL,
            params={"symbol": symbol},
            timeout=_DEFAULT_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            raw = data.get("lastFundingRate") if isinstance(data, dict) else None
            if raw is None:
                return None
            return float(raw)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-official] binance %s fail: %s", symbol, e)
        return None


async def fetch_okx_funding(
    session: aiohttp.ClientSession, inst_id: str,
) -> Optional[float]:
    """OKX 永续当前 funding rate（小数）"""
    try:
        async with session.get(
            _OKX_FUNDING_URL,
            params={"instId": inst_id},
            timeout=_DEFAULT_TIMEOUT,
        ) as resp:
            resp.raise_for_status()
            data = await resp.json(content_type=None)
            if not isinstance(data, dict):
                return None
            arr = data.get("data") or []
            if not arr or not isinstance(arr, list):
                return None
            rate_str = arr[0].get("fundingRate")
            if rate_str is None:
                return None
            return float(rate_str)
    except Exception as e:  # noqa: BLE001
        logger.warning("[funding-official] okx %s fail: %s", inst_id, e)
        return None


async def fetch_official_pair(
    symbol_binance: str,
    inst_id_okx: Optional[str] = None,
    *,
    session: Optional[aiohttp.ClientSession] = None,
) -> tuple[Optional[float], Optional[float]]:
    """并发拉取 Binance + OKX funding rate（小数）。

    返回 (binance_rate, okx_rate)，任一失败返回 None。
    - session 可传入复用连接；未传则本函数自建并回收。
    - inst_id_okx 为 None 时由 symbol_binance 派生。
    """
    inst_id = inst_id_okx or to_okx_inst_id(symbol_binance)

    if session is not None:
        return await asyncio.gather(
            fetch_binance_funding(session, symbol_binance),
            fetch_okx_funding(session, inst_id),
        )

    async with aiohttp.ClientSession() as s:
        return await asyncio.gather(
            fetch_binance_funding(s, symbol_binance),
            fetch_okx_funding(s, inst_id),
        )
