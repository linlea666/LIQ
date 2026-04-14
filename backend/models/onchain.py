"""链上数据模型：交易所余额、Token 解锁"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class ExchangeBalanceItem(BaseModel):
    """单交易所余额"""
    exchange: str
    balance: float = 0
    change_24h: float = 0
    change_pct_24h: float = 0


class ExchangeBalanceData(BaseModel):
    """交易所余额汇总"""
    symbol: str
    ts: int
    total_balance: float = 0
    total_change_24h: float = 0
    exchanges: list[ExchangeBalanceItem] = []


class TokenUnlockEvent(BaseModel):
    """Token 解锁事件"""
    symbol: str
    name: str = ""
    unlock_date: str = ""
    unlock_amount: float = 0
    unlock_usd: float = 0
    unlock_pct: float = 0  # 占流通量百分比
    description: str = ""


class TokenUnlockData(BaseModel):
    """Token 解锁列表"""
    ts: int
    events: list[TokenUnlockEvent] = []
