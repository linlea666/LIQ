"""期权数据模型"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class OptionMaxPainExpiry(BaseModel):
    """单个到期日的期权最大痛点"""
    expiry_date: str
    max_pain_price: float
    call_oi: float = 0
    put_oi: float = 0
    put_call_ratio: float = 0


class OptionMaxPainData(BaseModel):
    """期权最大痛点汇总"""
    symbol: str
    ts: int
    exchange: str = "Deribit"
    expiries: list[OptionMaxPainExpiry] = []
    nearest_max_pain: Optional[float] = None
    nearest_expiry: str = ""


class OptionInfoData(BaseModel):
    """期权概览信息"""
    symbol: str
    ts: int
    total_oi_usd: float = 0
    total_vol_24h_usd: float = 0
    put_call_oi_ratio: float = 0
    put_call_vol_ratio: float = 0
    iv_atm: Optional[float] = None


class OptionOIHistoryPoint(BaseModel):
    ts: int
    call_oi: float = 0
    put_oi: float = 0
    total_oi: float = 0


class OptionOIHistory(BaseModel):
    symbol: str
    exchange: str = ""
    data: list[OptionOIHistoryPoint] = []


class OptionVolHistoryPoint(BaseModel):
    ts: int
    call_vol: float = 0
    put_vol: float = 0
    total_vol: float = 0


class OptionVolHistory(BaseModel):
    symbol: str
    exchange: str = ""
    data: list[OptionVolHistoryPoint] = []
