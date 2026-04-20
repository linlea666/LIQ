"""新闻事件数据模型 —— 三层流水线产出

层级：
  1. RawNewsItem        ← 源头抓取 + 标准化（多源统一）
  2. MarketEventSignal  ← Layer 2 AI 结构化（三层思维）
  3. EnrichedNewsEvent  ← 入账本后，持续回填价格反应

时间戳约定：
  - `publish_time` 毫秒（对齐 OKX API 原始字段）
  - 其他 `ts` 秒级（对齐系统其他模块）
"""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field

from models.common_enums import (
    Direction, NarrativeStage, NewsProcessor, NewsRiskType, NewsTier,
    TimeHorizon,
)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Layer 0 · 源头抓取（多源统一）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class RawNewsItem(BaseModel):
    """多源标准化后的原始新闻条目

    由 sources/news/*.py 产出，Layer 1 / Layer 2 的共同输入。
    """

    # ── 来源标识 ──
    source_type: str                  # "okx" / "twitter" / "rss"
    source_author: str = ""           # "Blockbeats" / "ChainCatcher" / "@zhusu"
    source_reliability: float = 0.8   # 0-1 可信度（由 registry 配置）

    # ── 唯一键 & 时间 ──
    external_id: str                  # OKX contentId / Twitter tweet_id / RSS guid
    publish_time: int = 0             # ⚠️ 毫秒（对齐 OKX API 原始字段）
    fetch_time: int = 0               # 秒（我们拉取到的时刻）

    # ── 内容 ──
    title: str = ""
    content: str = ""
    lang: str = "zh"                  # "zh" / "en"
    translated_title: str = ""        # OKX 自带英文翻译；其他源可能为空
    translated_content: str = ""

    # ── 热度 & 标签 ──
    heat_score: float = 0.0           # 归一化 0-1（viewCount/likes 统一）
    view_count: int = 0               # 原始观看数
    like_count: int = 0
    comment_count: int = 0
    raw_tags: list[str] = Field(default_factory=list)
    # cashTagList + hashTagList + tokens 合并（OKX 官方常为空）

    # ── 跳转 ──
    share_url: str = ""

    # ── 原始 payload（留底，debug 用） ──
    raw_payload: dict[str, Any] = Field(default_factory=dict)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# Layer 2 · AI 结构化产出
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class AssetImpact(BaseModel):
    """事件对单一资产的影响评估"""

    asset: str                        # "BTC" / "ETH" / "ALT" / "oil" / "usd" / "gold"
    direction: Direction = "neutral"
    magnitude: str = "low"            # "low" / "medium" / "high"
    # 用 str 而非 Literal：未来可能扩展 "extreme"


class MarketEventSignal(BaseModel):
    """Layer 2 AI 结构化解读的完整输出（三层思维）"""

    # ── 定位 ──
    event_id: str                     # 对应 RawNewsItem.external_id
    ts: int = 0                       # 秒级（= publish_time / 1000）
    expires_at: int = 0               # 秒级，按 horizon 衰减

    # ── 基础分类（Layer 1 翻译层） ──
    target: str = "noise"             # "macro" / "BTC" / "ETH" / "sector:DeFi" / "noise"
    direction: Direction = "neutral"

    # ── 解读层（Layer 2） ──
    first_order_impact: str = ""      # 一阶直接影响（一句话）
    second_order_impact: str = ""     # 二阶传导（可为空）

    impact_score: int = 0             # -5 ~ +5
    confidence: float = 0.5           # AI 自评 0-1
    source_credibility: float = 0.5   # 来源可信度（嘴炮 vs 官方）

    horizon: TimeHorizon = "short"

    # ── 叙事层（Layer 3） ──
    narrative_theme: str = ""         # "Middle_East_Iran" / "Fed_Rate_Policy" / ...
    narrative_stage: NarrativeStage = "new"
    flip_flop_warning: bool = False   # 本次是否属于反复拉扯的一次
    already_priced_in_pct: float = 0.0  # 0-100 市场已定价程度

    # ── 风险类型 ──
    risk_type: NewsRiskType = "none"

    # ── 资产影响分拆（BTC/ETH/ALT 不同）──
    impact_on_assets: list[AssetImpact] = Field(default_factory=list)
    rationale_cn: str = ""            # 为什么是这个方向+幅度

    # ── 人类可读摘要 ──
    summary_cn: str = ""              # ≤20 字精炼
    trading_insight: str = ""         # ≤40 字操作启示（可为空）

    # ── 元信息 ──
    tier: NewsTier = "normal"
    processed_by: NewsProcessor = "ai"
    processed_at: int = 0             # 秒级
    model_used: str = ""              # "deepseek-chat" / ...（空=规则层产出）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 账本存储单元
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class EnrichedNewsEvent(BaseModel):
    """账本里存的完整事件（Raw + Structured + 价格反应回填）"""

    raw: RawNewsItem
    structured: MarketEventSignal

    # ── 价格反应（自动回填） ──
    # 用当前活跃币（BTC）的价格，因为地缘/宏观事件主要看 BTC 反应
    price_at_publish: Optional[float] = None
    price_after_1h: Optional[float] = None
    price_after_2h: Optional[float] = None
    price_after_24h: Optional[float] = None
    delta_pct_1h: Optional[float] = None
    delta_pct_2h: Optional[float] = None
    delta_pct_24h: Optional[float] = None

    backfill_status: str = "pending"   # "pending" / "partial" / "complete"
    backfilled_at: int = 0             # 秒级最后一次回填时刻
