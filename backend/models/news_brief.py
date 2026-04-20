"""滚动新闻简报（Rolling News Brief）数据模型

职责：
  - AI 新闻模块维护的"24h 记忆"
  - 供主 AI 模块（deepseek-reasoner）作为精简新闻上下文注入 prompt
  - 避免把 24h 所有原始新闻丢进 prompt（token 爆炸）

更新触发：
  - scheduled：每小时主 AI 分析前的常规更新
  - blackswan：检测到黑天鹅事件立即重建
  - user：用户手动请求

结构：
  - sections 分 4 板块：macro / regulatory / onchain / risk
  - 每板块 ≤5 条要点（AI 维护，重要性优先）
  - tldr_cn：50 字全局一句话
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


class NewsBriefSection(BaseModel):
    """简报单个板块"""

    section_id: Literal["macro", "regulatory", "onchain", "risk"] = "macro"
    section_title_cn: str = ""                   # "宏观" / "监管" / "链上" / "风险"
    bullets: list[str] = Field(default_factory=list)
    # 每条 ≤40 字，AI 精炼后的要点

    max_bullets: int = 5                         # 本板块最多保留几条
    last_rewritten_ts: int = 0                   # 板块上次被 AI 改写的时间


class BriefTrackedTheme(BaseModel):
    """简报中被跟踪的叙事主题（AI 记忆锚点）"""

    theme_id: str
    theme_name_cn: str
    current_stance_cn: str = ""                  # "看空(已 priced-in 2%)"
    flip_flop_count_24h: int = 0
    latest_update_ts: int = 0
    relevance_score: float = 0.5                 # 0-1 对当前行情相关性


class NewsBrief(BaseModel):
    """滚动新闻简报主体"""

    # ── 版本 & 时序 ──
    version: int = 0                             # 单调递增
    updated_at: int = 0                          # 秒级
    ts_range_start: int = 0                      # 覆盖的事件时间窗起点（秒）
    ts_range_end: int = 0                        # 覆盖终点（通常=updated_at）
    coverage_hours: float = 24.0                 # 通常 24h

    # ── 正文 ──
    sections: list[NewsBriefSection] = Field(default_factory=list)
    tldr_cn: str = ""                            # ≤50 字一句话总结

    # ── 跟踪主题（AI 记忆锚点） ──
    tracked_themes: list[BriefTrackedTheme] = Field(default_factory=list)

    # ── 容量控制 ──
    char_count: int = 0                          # 全文（含 sections）字符数
    token_estimate: int = 0                      # 粗估（char_count / 2.5 for zh）

    # ── 元信息 ──
    update_trigger: Literal["scheduled", "blackswan", "user"] = "scheduled"
    based_on_events_count: int = 0               # 本次 rewrite 基于的事件数
    model_used: str = ""                         # "deepseek-chat"
    generation_cost_ms: int = 0

    # ── 历史对照（用于 AI 判断"新鲜度"） ──
    diff_from_prev_version: str = ""             # "新增：美伊和谈破裂"
    prev_version_updated_at: Optional[int] = None
