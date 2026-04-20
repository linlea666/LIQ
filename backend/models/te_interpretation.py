"""趋势衰竭模块 · AI 解读结果数据契约

设计原则
--------
1. **只补充规则给不了的价值**：矛盾消解 + 陷阱 + 触发条件，禁止复读规则结论
2. **强制结构化**：AI 必须输出固定 schema，便于前端渲染 + 事后打标
3. **显式对齐度**：让用户看到 AI 与规则是否一致，避免盲从
4. **置信度必填**：AI 必须自估信心，低信心就老老实实说"证据不足"
5. **思考过程单独字段**：DeepSeek R1 的 reasoning_content 归档，前端默认折叠

本模型不直接入 WebSocket 广播（按需触发），只通过 REST 拉取。
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# AI 对规则判断的对齐度标签（影响前端提醒色）
AlignmentWithRules = Literal[
    "agree",              # AI 同意规则结论（绿色小标：AI 确认）
    "partial_disagree",   # AI 有补充 / 部分不同意（黄色：AI 有补充）
    "strong_disagree",    # AI 认为规则错了（红色：AI 不同意，请警惕）
    "insufficient",       # 证据不足，AI 不敢下判（灰色：证据不足）
]


# 用户面向场景（让 AI 必须明确是哪类，避免骑墙）
ScenarioLabel = Literal[
    "trend_continuation",     # 顺势续航（规则 healthy 的常见对齐）
    "bear_rebound",           # 熊市反弹（小级别翻多但大级别仍空，常见陷阱）
    "bull_pullback",          # 牛市回调（大级别仍多但小级别在调整）
    "reversal_early",         # 反转早期（小级别已翻，大级别还没跟上）
    "reversal_confirmed",     # 反转确认（规则 structural_reversal 的常见对齐）
    "choppy_range",           # 震荡/没戏（regime_vetoed 的常见对齐）
    "unclear",                # AI 也看不清（对应 insufficient）
]


class TEAIInterpretation(BaseModel):
    """单次 AI 解读结果，既供前端展示，也供 shadow log 归档。"""

    # ── 元信息 ────────────────────────────────────────
    coin: str
    ts: int                             # 触发解读的 unix ts
    signal_fingerprint: str             # 输入信号的指纹（缓存 key / 去重）
    model: str = ""                     # 实际用的模型名（"deepseek-reasoner"）
    cache_hit: bool = False             # True = 命中缓存，没实际调 AI
    from_cache_age_sec: int = 0         # 若命中，已缓存多久
    latency_ms: int = 0                 # 调 AI 的耗时（cache_hit=True 时为 0）
    tokens_in: int = 0
    tokens_out: int = 0
    reasoning_tokens: int = 0

    # ── AI 输出（三段式 + 辅助字段） ──────────────────
    summary_cn: str = ""                # 一句话主结论（不超过 40 字）
    scenario: ScenarioLabel = "unclear"
    conflict_resolution: str = ""       # 矛盾消解（若有，最多 2-3 句）
    traps: list[str] = Field(default_factory=list)         # 陷阱提醒，每条一句
    triggers_to_watch: list[str] = Field(default_factory=list)  # 等待确认的触发条件
    action_suggestion: str = ""         # 一句行动建议
    confidence: float = 0.0             # 0~1，AI 自估信心

    # ── 对齐度 ────────────────────────────────────────
    alignment_with_rules: AlignmentWithRules = "insufficient"
    alignment_reason: str = ""          # 一句解释"为什么 agree/disagree"

    # ── 思考过程（可选，默认前端折叠） ────────────────
    reasoning: str = ""                 # DeepSeek Reasoner 的 reasoning_content

    # ── 错误兜底（解析失败时） ────────────────────────
    error: Optional[str] = None         # 非空=本次调用失败，但仍返回给前端提示
    raw_text: str = ""                  # AI 原始输出（用于调试，前端不展示）
