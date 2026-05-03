"""策略抽象基类 + 共享上下文模型

设计目标（dev-constraints #4 一次到位）：
  - 三个具体策略（A/B/C）只需实现 detect()，无需重复 boilerplate
  - 上下文 StrategyContext 屏蔽 state 内部结构（解耦）
  - 候选 StrategyCandidate 是"raw 证据"，confidence 加权 + veto 在引擎统一做

为什么不复用 OpportunityEngine 的 TradeSetupCandidate？
  - TradeSetupCandidate 是 RR 优化（永续 / 进场+止损+目标），语义不同
  - 短线事件合约只有"方向 + 命中概率"，强行复用会污染语义
  - 故按 dev-constraints #3 选择"独立新写"：完全独立的 schema
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, ClassVar, Optional

from pydantic import BaseModel, Field

from models.common_enums import MarketRegimeLabel
from models.scalp_signal import (
    EvidenceItem,
    HorizonMin,
    ScalpDirection,
    StrategyName,
)


class StrategyContext(BaseModel):
    """策略 detect() 共享的上下文（一次构造，多策略复用）

    - state / btc_state：CoinState 实例（用 Any 避免 engine.py 与本模块循环 import）
    - regime：当前 6 分类 regime
    - range_position_pct：0-100，仅 regime in {range, squeeze} 时有意义
    - bias_score：MTF Bias 输出 ∈ [-1, +1]
    - bias_components：MTF Bias 子项分解（用于策略生成 evidence）
    - now_ts：信号生成基准时间（秒级 unix），统一从 engine 注入避免漂移
    """
    model_config = {"arbitrary_types_allowed": True}

    state: Any = Field(default=None, exclude=True)
    btc_state: Any = Field(default=None, exclude=True)
    coin: str
    horizon_min: HorizonMin
    regime: MarketRegimeLabel
    range_position_pct: Optional[float] = None
    bias_score: float = 0.0
    bias_components: dict[str, float] = Field(default_factory=dict)
    now_ts: int = 0


class StrategyCandidate(BaseModel):
    """策略检测出的"候选信号"（未走 confidence 加权，未 veto）

    - direction：策略本次预测方向
    - reference_price：信号生成瞬时价（来自 state.ticker.last，由策略写入）
    - raw_strength：策略本身硬证据强度 ∈ [0, 1]
    - evidence：策略产生的证据（confidence_scorer 会拼接 MTF / KL 证据）
    - triggered_conditions：策略命中的条件清单（审计 / debug）
    - extra_data：策略专用透传（如 sweep_level, divergence_bars，最终落到 signal.evidence 里）
    """
    direction: ScalpDirection
    reference_price: float
    raw_strength: float = Field(ge=0.0, le=1.0)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    triggered_conditions: list[str] = Field(default_factory=list)
    extra_data: dict[str, Any] = Field(default_factory=dict)


class BaseStrategy(ABC):
    """策略抽象基类 · 子类需声明类常量 + 实现 detect()

    类常量约束：
      - name：StrategyName 枚举（注册唯一性 key）
      - display_name：UI 显示用中文名
      - suitable_regimes：本策略生效的 regime 集合（白名单）
      - suitable_horizons：本策略支持的周期集合

    实例方法：
      - detect(ctx) → StrategyCandidate or None：纯函数，无副作用
      - is_applicable(regime, horizon)：基类提供，子类不重写

    为什么不强制 score()？
      - 评分逻辑跨策略统一（5 因子），由 confidence_scorer 集中处理
      - 策略只输出 raw_strength，不参与最终 confidence 计算
    """

    name: ClassVar[StrategyName]
    display_name: ClassVar[str]
    suitable_regimes: ClassVar[set[str]]       # 用 str 集合（与 MarketRegimeLabel 字面值一致）
    suitable_horizons: ClassVar[set[int]]      # {10, 30, 60} 子集

    @abstractmethod
    def detect(self, ctx: StrategyContext) -> Optional[StrategyCandidate]:
        """检测当前市场是否触发本策略

        Returns:
            非 None：触发，返回候选信号
            None：未触发或数据不足
        """
        raise NotImplementedError

    def is_applicable(self, regime: str, horizon: int) -> bool:
        """快速检查 (regime, horizon) 是否在策略适用范围内"""
        return regime in self.suitable_regimes and horizon in self.suitable_horizons

    # 工具方法（子类可调用）

    @staticmethod
    def safe_attr(obj: Any, *path: str, default: Any = None) -> Any:
        """链式 getattr · 任一段为 None 即短路返回 default

        用例：safe_attr(state, "ticker", "last", default=0.0)
        """
        cur = obj
        for seg in path:
            if cur is None:
                return default
            cur = getattr(cur, seg, None)
        return cur if cur is not None else default
