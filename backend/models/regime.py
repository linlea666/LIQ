"""市场状态（Regime）数据模型

职责：
  - 描述当前市场所处的"交易环境"：趋势/震荡/蓄力/高波动/极端
  - 由 processors/market_regime.py 产出，供 Synthesizer 做权重切换

与 MarketStructure 的区别：
  - MarketStructure 描述"结构方向"（BOS/CHoCH/bullish/bearish/ranging）
  - RegimeSnapshot 描述"波动性与可交易性"（趋势强度 + 波动率 + 协调性）
  - 二者互补，Synthesizer 同时消费
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from models.common_enums import MarketRegimeLabel


class RegimeFeatures(BaseModel):
    """支撑 regime 判定的原始特征（可解释性）"""

    atr_pct: float = 0.0            # ATR / price * 100
    atr_pct_percentile: float = 0.0  # ATR% 的历史百分位（PercentileTracker）
    adx: float = 0.0                # ADX (趋势强度指标)
    bbw: float = 0.0                # 布林带宽度 (normalized)
    trend_slope_pct: float = 0.0    # 短期 EMA 斜率百分比
    hist_vol_pct: float = 0.0       # 历史波动率
    cvd_persistence: float = 0.0    # CVD 方向持续性 (0-1)
    structure_alignment: str = ""   # 和 MarketStructure 的对齐（bullish/bearish/ranging/transitioning）
    liq_24h_vs_7d_avg: float = 1.0  # 24h 爆仓 / 7d 均值 (>3 视为混乱)


class RegimeSnapshot(BaseModel):
    """一个币种当前的市场状态快照"""

    coin: str
    ts: int                         # 秒级
    regime: MarketRegimeLabel = "range"
    confidence: float = 0.0         # 0-1，软权重（用于融合时避免硬跳档）

    # 原始特征（可解释）
    features: RegimeFeatures = Field(default_factory=RegimeFeatures)

    # 人类可读说明
    description_cn: str = ""        # 例 "4H 上升趋势 + ATR 处于历史 70 分位 · 正常波动"

    # 建议 —— 作为 Synthesizer 权重参考，非强制
    # 每种 action 在此 regime 下的乘数建议（1.0=中性，>1 鼓励，<1 抑制）
    action_weights: dict[str, float] = Field(default_factory=dict)
    # 例：{"snipe_long": 0.7, "flip_long": 1.3, "scalp_long": 1.0, ...}

    # 切换追踪（用于检测 regime shift）
    prev_regime: Optional[MarketRegimeLabel] = None
    regime_changed_at: int = 0      # 本次 regime 首次出现的时间戳
    stable_duration_sec: int = 0    # 当前 regime 已稳定持续时长
