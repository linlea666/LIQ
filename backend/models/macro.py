"""宏观数据模型：经济日历、新闻、山寨季、泡沫指数等"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


class EconomicEvent(BaseModel):
    """经济日历事件"""
    ts: int
    country: str = ""
    event_name: str = ""
    importance: str = ""  # "high" | "medium" | "low"
    actual: Optional[str] = None
    forecast: Optional[str] = None
    previous: Optional[str] = None


class EconomicCalendarData(BaseModel):
    ts: int
    events: list[EconomicEvent] = []


class NewsArticle(BaseModel):
    """新闻快讯"""
    ts: int
    title: str = ""
    content: str = ""
    source: str = ""
    url: str = ""
    importance: str = ""


class NewsData(BaseModel):
    ts: int
    articles: list[NewsArticle] = []


class AltcoinSeasonPoint(BaseModel):
    ts: int
    value: float = 0


class AltcoinSeasonData(BaseModel):
    ts: int
    current_value: float = 0
    label: str = ""  # "altcoin_season" | "bitcoin_season" | "neutral"
    history: list[AltcoinSeasonPoint] = []


class BubbleIndexPoint(BaseModel):
    ts: int
    value: float = 0
    price: float = 0


class BubbleIndexData(BaseModel):
    ts: int
    current_value: float = 0
    history: list[BubbleIndexPoint] = []


class BullMarketPeakPoint(BaseModel):
    ts: int
    value: float = 0
    price: float = 0


class BullMarketPeakData(BaseModel):
    ts: int
    current_value: float = 0
    history: list[BullMarketPeakPoint] = []


class CoinbasePremiumPoint(BaseModel):
    ts: int
    premium: float = 0
    price: float = 0


class CoinbasePremiumData(BaseModel):
    ts: int
    current_premium: float = 0
    history: list[CoinbasePremiumPoint] = []


class StablecoinMcapPoint(BaseModel):
    ts: int
    total_mcap: float = 0
    usdt_mcap: float = 0
    usdc_mcap: float = 0


class StablecoinMcapData(BaseModel):
    ts: int
    current_total: float = 0
    history: list[StablecoinMcapPoint] = []


class FearGreedPoint(BaseModel):
    ts: int
    value: float = 0
    label: str = ""


class FearGreedData(BaseModel):
    ts: int
    current_value: float = 0
    current_label: str = ""
    history: list[FearGreedPoint] = []


class BtcDominancePoint(BaseModel):
    ts: int
    dominance: float = 0


class BtcDominanceData(BaseModel):
    ts: int
    current: float = 0
    history: list[BtcDominancePoint] = []
