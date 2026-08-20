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
import time
from dataclasses import dataclass
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_BINANCE_PREMIUM_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"
_OKX_FUNDING_URL = "https://www.okx.com/api/v5/public/funding-rate"
_BINANCE_HISTORY_URL = "https://fapi.binance.com/fapi/v1/fundingRate"
_DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=6)


@dataclass(frozen=True)
class OfficialFundingObservation:
    binance_rate_observed: Optional[float]
    okx_rate_observed: Optional[float]
    last_settled_rate: Optional[float]
    next_funding_time: int
    observed_at: int

    @property
    def predicted_rate_observed(self) -> Optional[float]:
        values = [
            value for value in (self.binance_rate_observed, self.okx_rate_observed)
            if value is not None
        ]
        return sum(values) / len(values) if values else None


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


async def _fetch_binance_observation(
    session: aiohttp.ClientSession, symbol: str,
) -> tuple[Optional[float], Optional[float], int]:
    current: Optional[float] = None
    settled: Optional[float] = None
    next_time = 0
    try:
        async with session.get(
            _BINANCE_PREMIUM_URL, params={"symbol": symbol}, timeout=_DEFAULT_TIMEOUT,
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
            if isinstance(payload, dict):
                raw = payload.get("lastFundingRate")
                current = float(raw) if raw is not None else None
                next_time = int(payload.get("nextFundingTime", 0) or 0) // 1000
    except Exception as exc:  # noqa: BLE001
        logger.warning("[funding-official] binance observation %s fail: %s", symbol, exc)
    try:
        async with session.get(
            _BINANCE_HISTORY_URL,
            params={"symbol": symbol, "limit": 1}, timeout=_DEFAULT_TIMEOUT,
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
            if isinstance(payload, list) and payload:
                raw = payload[-1].get("fundingRate")
                settled = float(raw) if raw is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[funding-official] binance settled %s fail: %s", symbol, exc)
    return current, settled, next_time


async def _fetch_okx_observation(
    session: aiohttp.ClientSession, inst_id: str,
) -> tuple[Optional[float], Optional[float], int]:
    try:
        async with session.get(
            _OKX_FUNDING_URL, params={"instId": inst_id}, timeout=_DEFAULT_TIMEOUT,
        ) as response:
            response.raise_for_status()
            payload = await response.json(content_type=None)
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            row = rows[0] if isinstance(rows, list) and rows else {}
            current_raw = row.get("fundingRate")
            settled_raw = row.get("settFundingRate")
            return (
                float(current_raw) if current_raw not in (None, "") else None,
                float(settled_raw) if settled_raw not in (None, "") else None,
                int(row.get("nextFundingTime", 0) or 0) // 1000,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[funding-official] okx observation %s fail: %s", inst_id, exc)
        return None, None, 0


async def fetch_official_observation(
    symbol_binance: str, inst_id_okx: Optional[str] = None, *,
    session: Optional[aiohttp.ClientSession] = None,
) -> OfficialFundingObservation:
    """当前观测、最近实际结算和下一结算时间三者不互相回填。"""
    inst_id = inst_id_okx or to_okx_inst_id(symbol_binance)

    async def _run(active_session: aiohttp.ClientSession) -> OfficialFundingObservation:
        binance, okx = await asyncio.gather(
            _fetch_binance_observation(active_session, symbol_binance),
            _fetch_okx_observation(active_session, inst_id),
        )
        bn_current, bn_settled, bn_next = binance
        okx_current, okx_settled, okx_next = okx
        settled_values = [value for value in (bn_settled, okx_settled) if value is not None]
        return OfficialFundingObservation(
            binance_rate_observed=bn_current,
            okx_rate_observed=okx_current,
            last_settled_rate=(sum(settled_values) / len(settled_values) if settled_values else None),
            next_funding_time=min(
                (value for value in (bn_next, okx_next) if value > 0), default=0,
            ),
            observed_at=int(time.time()),
        )

    if session is not None:
        return await _run(session)
    async with aiohttp.ClientSession() as active_session:
        return await _run(active_session)
