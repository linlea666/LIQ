"""通用 Prompt Debug 模型（MAA + Strategic AI 共用）。

设计原则：
1. 字段与原 `models/market_action.py.PromptDebug` **完全一致**（顺序/类型/默认值）
   → 旧 `market_action_history.json` 反序列化路径无破坏；
   → 现有 `MarketActionReport(**item)` 入口仍可用，pydantic 按字段名匹配，与类的
   module path 无关。
2. 提取动机：Strategic AI 需要同样的"transparent prompt 透明化"能力（system /
   user / raw_response / sections / model / tokens / latency）；与 MAA 共享一份
   schema 可避免双重维护。
3. `PromptSection` 同步提取（PromptDebug 引用它）。

向后兼容路径：
- `models/market_action.py` 改为 re-export：
    `from models.common_prompt_debug import PromptDebug, PromptSection`
  所有现有 `from models.market_action import PromptDebug` 调用点（生产代码 +
  history 反序列化）仍按原路径解析到本模块的同一类对象。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class PromptSection(BaseModel):
    """Prompt 中的章节锚点，供前端生成 TOC。

    字段语义沿用原 `market_action.PromptSection`：
      - anchor：章节编号（"§1" / "§2" / "§9c" 等）
      - title：章节中文标题
      - level：markdown 标题级别（2=##, 3=###）
    """
    anchor: str
    title: str
    level: int = 2


class PromptDebug(BaseModel):
    """AI 调用透明度 · 供前端"本轮喂给 AI 的完整数据"卡片展示。

    通用字段：MAA / Strategic / 未来其他 AI 仲裁器共享。
    历史字段：
      - `ai_reasoning_content`：R1/reasoner 时代的 Chain-of-Thought 原文。
        v4-flash 非思考模式恒为空，保留以兼容旧快照 + 未来思考模式平滑接入。
    """
    system: str
    user: str
    chars: int
    sections: list[PromptSection] = Field(default_factory=list)
    model: str
    tokens_prompt: Optional[int] = None
    tokens_completion: Optional[int] = None
    tokens_reasoning: Optional[int] = None
    latency_ms: int = 0
    generated_at: int = 0
    ai_raw_response: Optional[str] = None
    ai_reasoning_content: Optional[str] = None
    parse_ok: bool = True
    parse_error: Optional[str] = None
