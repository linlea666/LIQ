"""地缘风险（Geo Risk）数据模型

职责：
  - 为"美伊/俄乌/朝韩/台海"等地缘主题维护实时风险等级（0-5）
  - 追踪升级/降级/反复拉扯
  - 记录资产特定的历史反应（BTC/ETH/ALT 差异）

等级语义：
  0 · PEACE       无风险（静默）
  1 · WATCHING    关注（有舆情但未升级）
  2 · TENSION     紧张（外交辞令升级）
  3 · CRISIS      危机（军事动作/制裁）
  4 · ESCALATION  升级（空袭/小规模冲突）
  5 · WAR         战争（全面冲突）

flip-flop 规则：
  24h 内同一 theme 的 level 来回穿越 ≥2 次 → flip_flop_warning=True
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.common_enums import GeoRiskLabel


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 等级变更历史
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GeoRiskLevelChange(BaseModel):
    """单次等级跃迁记录"""

    ts: int = 0                                   # 秒级
    level_before: int = 0                         # 0-5
    level_after: int = 0                          # 0-5
    event_id: str = ""                            # 触发该跃迁的事件
    trigger_summary: str = ""                     # ≤30 字触发摘要


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 单主题的地缘风险状态
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GeoRiskAssetReaction(BaseModel):
    """某等级下历史上的资产反应均值"""

    level_transition: str                         # "3_to_2" / "2_to_3" / "at_3"
    avg_btc_pct: float = 0.0
    avg_eth_pct: float = 0.0
    avg_alt_pct: float = 0.0
    sample_size: int = 0


class GeoRiskState(BaseModel):
    """单个地缘主题的实时风险状态"""

    # ── 身份 ──
    theme_id: str                                 # "Middle_East_Iran" / "Russia_Ukraine"
    theme_name_cn: str                            # "美伊关系"

    # ── 当前等级 ──
    current_level: int = 0                        # 0-5
    level_label: GeoRiskLabel = "PEACE"
    level_emoji: str = "🟢"                       # 🟢🟡🟠🔴🟣⚫

    # ── 历史 ──
    level_history: list[GeoRiskLevelChange] = Field(default_factory=list)
    level_stable_hours: float = 0.0               # 已在当前等级持续时长

    # ── 反复拉扯 ──
    flip_flop_count_24h: int = 0
    flip_flop_count_7d: int = 0
    flip_flop_warning: bool = False

    # ── 资产反应 ──
    accumulated_reaction_pct_btc: float = 0.0     # 本次等级维持期内 BTC 累计反应
    accumulated_reaction_pct_eth: float = 0.0
    historical_reactions: list[GeoRiskAssetReaction] = Field(default_factory=list)

    # ── 最近事件 ──
    latest_event_id: str = ""
    latest_event_summary: str = ""
    latest_event_ts: int = 0

    # ── 元信息 ──
    last_updated: int = 0                         # 秒级


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 地缘风险单次事件信号（进入 SignalBus 的载体）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GeoRiskEvent(BaseModel):
    """地缘事件信号 —— 由 GeoRiskTracker 产出供 Synthesizer 消费"""

    event_id: str
    theme_id: str
    theme_name_cn: str = ""
    ts: int = 0                                   # 秒级

    level_before: int = 0
    level_after: int = 0
    severity: str = "stable"
    # "escalation" / "de-escalation" / "flip_flop" / "stable"

    estimated_crypto_reaction_pct: float = 0.0    # AI+历史均值综合估算
    estimated_direction: str = "neutral"          # "bullish" / "bearish" / "neutral"
    confidence: float = 0.5

    flip_flop_warning: bool = False
    is_blackswan: bool = False                    # level=5 或 24h 内 0→4+ 跳档

    summary_cn: str = ""                          # ≤40 字结论


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 全局地缘风险聚合视图
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class GeoRiskOverview(BaseModel):
    """所有地缘主题的全局聚合（前端 GeoRiskBadge + AI 输入）"""

    ts: int = 0

    # 全局等级 = 所有活跃主题的 max（带权重）
    overall_level: int = 0
    overall_label: GeoRiskLabel = "PEACE"
    overall_emoji: str = "🟢"
    overall_summary_cn: str = ""                  # "美伊紧张 + 俄乌升级 · 综合风险 3/5"

    # 活跃主题列表（current_level >= 1 的）
    active_themes: list[GeoRiskState] = Field(default_factory=list)

    # 24h 统计
    escalation_count_24h: int = 0
    de_escalation_count_24h: int = 0
    has_blackswan_24h: bool = False

    # SafetyGate 建议标志
    suggest_safety_gate_block: bool = False       # overall_level >= 4
    suggest_position_cap_pct: Optional[float] = None  # overall_level 3 → 30%, 4 → 15%
