"""巨鲸数据模型"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class HyperliquidWhaleAlert(BaseModel):
    """Hyperliquid 巨鲸警报"""
    ts: int
    symbol: str
    side: str  # "long" | "short"
    size_usd: float = 0
    entry_price: float = 0
    address: str = ""
    action: str = ""  # "open" | "close" | "increase" | "decrease"


class HyperliquidWhalePosition(BaseModel):
    """Hyperliquid 巨鲸持仓"""
    address: str
    symbol: str
    side: str
    size_usd: float = 0
    entry_price: float = 0
    unrealized_pnl: float = 0
    leverage: float = 0


class WhaleTransfer(BaseModel):
    """链上巨鲸转账"""
    ts: int
    symbol: str
    amount: float = 0
    amount_usd: float = 0
    from_address: str = ""
    to_address: str = ""
    from_label: str = ""
    to_label: str = ""
    tx_hash: str = ""
    blockchain: str = ""


class WhaleData(BaseModel):
    """巨鲸数据汇总"""
    ts: int
    hl_alerts: list[HyperliquidWhaleAlert] = []
    hl_positions: list[HyperliquidWhalePosition] = []
    transfers: list[WhaleTransfer] = []
