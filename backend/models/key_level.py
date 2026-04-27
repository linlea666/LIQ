"""关键位生命周期追踪数据模型（V2 架构）

历史说明：V1 `KeyLevel` / `KeyLevelSnapshot` 与追踪器 `key_level_tracker.py`
已于本次提交整体下线（V1 产线链路在 Commit 2 已弃用，处理器 0 调用）。
`KeyLevelSignal` 同时被 V2 `KeyLevelSnapshotV2.signals` 复用，故保留。
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class KeyLevelSignal(BaseModel):
    """关键位产出的交易信号"""

    level_price: float
    side: str  # "support" | "resistance"
    state: str
    action: str
    # "snipe_long" | "snipe_short" | "flip_short" | "flip_long" | "wait_sweep" | "wait_approach"
    confidence: str = "C"  # "A" | "B" | "C"
    entry_price: Optional[float] = None
    stop_loss: Optional[float] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    rr_ratio: Optional[float] = None
    reason: str = ""
    warnings: list[str] = Field(default_factory=list)

    # 置信度透明化（小白视线友好）
    # confirmations：本信号通过的确认项清单（方便前端 ✅ chip 链 + 评分透明化）
    #   可取值参考 key_level_tracker_v2._CONFIRMATION_KEYS
    #   如 ["closed_bar", "volume_proactive", "pattern_pin_bar", "sweep_taken",
    #       "retest_done", "continuation", "fake_break_reclaim", "mtf_aligned",
    #       "cvd_aligned"]
    # signal_kind：信号分类，前端据此渲染徽章
    #   如 "snipe_sweep" / "snipe_bounce" / "flip_retest" / "scalp"
    #   / "fake_break_reversal" / "breakout_retest" / "breakout_continuation"
    #   / "wait_approach" / "wait_sweep"
    # score：0-100 置信度分数
    #   base(A=80/B=60/C=40) + 确认项×4（上限 +20） - warnings×3；clamp [0,100]
    confirmations: list[str] = Field(default_factory=list)
    signal_kind: str = ""
    score: int = 0


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V2 — 多维共振关键位系统
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class KeyLevelV2(BaseModel):
    """单个关键价位（多维共振 + 生命周期追踪）"""

    price: float
    side: str               # "support" / "resistance"
    category: str = ""      # "strong_support" / "moderate_resistance" / "fib_level" / "pivot" / "ma_cluster" / ...
    sources: list[str] = Field(default_factory=list)
    source_count: int = 0
    confluence_score: float = 0  # 0-100
    strength_tier: str = "C"     # "S" (最强) / "A" / "B" / "C"

    # 状态机
    # idle / approaching / testing / swept / bounced / broken / fake_break / flipped
    state: str = "idle"
    state_ts: int = 0
    prev_state: str = ""
    test_count: int = 0
    sweep_usd: float = 0
    lowest_wick: Optional[float] = None
    break_start_ts: int = 0

    # 级联风险
    cascade_risk: float = 0
    cascade_layers: int = 0
    cascade_total_usd: float = 0

    distance_pct: float = 0

    # V2 新增
    timeframe: str = ""            # 该位最强的时间框架 ("1H"/"4H"/"1D"/"1W")
    first_seen_ts: int = 0
    last_confirmed_ts: int = 0
    note: str = ""                 # 白话说明

    # K 线形态确认（AI 自检建议：暴露 pin bar / engulfing / doji 结构化信号给 AI）
    pattern_detected: str = ""     # "锤子线" / "射击之星" / "看涨吞没" / "看跌吞没" / "十字星" / ""
    pattern_strength: float = 0.0  # 0~1，形态强度（detect_reversal_pattern 输出）

    # Phase 2：历史验证 + 结构屏障 + 最终打分（供"强位卡片"与 tier 评定使用）
    bounce_count: int = 0               # 历史成功反弹/拒绝次数（状态机进入 bounced 累加）
    historical_validity: float = 0.0    # 0~1，由 bounce_count / test_count / sweep_usd 组合得出
    barrier_score: float = 0.0          # 0~20，结构屏障加分（多个清算簇前置、时间存活等）
    final_score: float = 0.0            # 0~100，= confluence_score × 时间衰减 + 历史验证 + 屏障加分

    # Commit 4：质量标注（博主方法论：主动 vs 被动 · 三步确认）
    bounce_quality: str = ""       # "proactive"(主动吸筹) / "passive"(被动触发) / ""(未反弹)
    breakout_stage: int = 0        # 0(未破位) / 1(破位) / 2(回踩) / 3(确认)

    # 假突破反转追踪（2026-04 新增）
    # - fake_break_count：本 level 历史被假突破次数；多次假破 = 防守强度高
    fake_break_count: int = 0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M1（V3 准备阶段）— 多周期清算 + 算法化失效价 + 数据血统
    # 全部 Optional/默认值，向后兼容（旧 snapshot 反序列化无破坏）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 跨所共识：从 cluster.exchange_count 派生（1=单所偶发，≥3=多所共振强簇）
    exchange_count: int = 0
    consensus_multiplier: float = 1.0    # 实际作用于 confluence_score 的共识乘子（0.85-1.6）
    dominant_leverage: str = ""          # 主导杠杆（如 "50x"），来自簇内
    leverage_intensity: float = 0.0      # 主导杠杆 USD 占比（0-1）

    # 算法化失效价（替代用户拍脑袋设止损）
    # 计算规则：support→price - mult×ATR / resistance→price + mult×ATR
    # mult 由 strength_tier 决定：S=2.0 / A=1.5 / B=1.0 / C=0.5
    invalidation_price: Optional[float] = None
    invalidation_condition: str = ""     # 中文条件描述（"1h 收盘 < $63,000"）
    invalidation_atr_mult: float = 0.0   # 计算时使用的 ATR 倍数（透明化）

    # 级联破位后的下一个磁铁价位（M1 仅展示用，不参与 tier）
    next_magnet_price: Optional[float] = None
    vacuum_gap_pct: float = 0.0          # 当前位到下一磁铁的真空跨度（%），越大越危险

    # 数据血统/新鲜度（DataFreshness 在 KeyLevelSnapshotV2 上整体计算 + 这里挂主源年龄）
    # 目的：高分关键位若主源已过期，前端可显示"⏳ 数据偏旧"灰章
    primary_source_age_hours: Optional[float] = None
    is_stale: bool = False               # 主源 age > TTL 时为 True；UI 据此降权显示

    # 解释芯片（前端 chip 渲染：直接 join 即得"为何重要"白话）
    # 例: ["7d清算簇", "3所共振", "50x主导", "VWAP叠加", "EMA200"]
    explain_chips: list[str] = Field(default_factory=list)


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M1 新增：清算磁铁通道（与 levels 平行，独立显示）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LiqMagnetLevel(BaseModel):
    """清算磁铁/痛点价位 — 独立通道，不参与 strength_tier 评分。

    设计原因（V3 评审采纳）：
    - max_pain / 高杠杆密度带不应直接进 candidate 池升 S
      （单源单证据 → 容易制造伪 S 信号、稀释关键位密度）
    - 但它们对"价格磁铁"判断很有价值：用户应能直观看到"这里有大量被吸引的清算筹码"
    - 故独立成"磁铁通道"，前端用紫色徽标 💥 显示
    - 仅当 magnet 价位与某 level 距离 > 0.5×ATR 时显示（避免与 level 重复）
    """
    price: float
    magnet_role: str  # "downside_pain_center" / "upside_short_squeeze" / "leverage_magnet"
    source: str       # "max_pain_long" / "max_pain_short" / "heatmap_top_density"
    usd: float = 0    # 该位关联的清算 USD（max_pain.long_pain_usd 或 heatmap intensity）
    distance_pct: float = 0
    leverage_hint: str = ""  # "50x主导" 或 "" （来自 heatmap）
    note: str = ""           # 白话说明（"全市场多头痛点，下破后急跌磁吸点"）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M1 新增：数据新鲜度元信息（snapshot 级别）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class DataFreshness(BaseModel):
    """快照级数据血统/新鲜度元信息。

    用途：
    - 让 AI / 前端 / 风控感知"这次评分基于哪些源、哪些过期了"
    - 前端可显示"📊 8/9 源新鲜（footprint 已 8 分钟未更新）"
    - confluence_scoring 对 stale 的 level 软降权 0.6-1.0×

    sources_age_seconds：{源名: 距今秒数}；缺失源不出现在该 dict
    overall_freshness_score：综合新鲜度（0-100），= 100 × (1 - stale_count / total_count)
    stale_sources：超过该源 TTL 的列表
    missing_sources：本应有但实际为空的源
    """
    ts: int = 0  # 本次计算时间戳
    sources_age_seconds: dict[str, float] = Field(default_factory=dict)
    overall_freshness_score: float = 100.0
    stale_sources: list[str] = Field(default_factory=list)
    missing_sources: list[str] = Field(default_factory=list)


class BullBearLine(BaseModel):
    """多空分界线（独立展示区域）"""

    sma200d: Optional[float] = None
    bmsa_upper: Optional[float] = None   # 20W SMA
    bmsa_lower: Optional[float] = None   # 21W EMA
    ichimoku_cloud_top: Optional[float] = None
    ichimoku_cloud_bottom: Optional[float] = None
    current_regime: str = ""             # "bull" / "bear" / "neutral"
    regime_reason: str = ""


class BreakoutZone(BaseModel):
    """突破蓄力区"""

    bb_squeeze: bool = False
    squeeze_direction: str = ""          # "up" / "down" / "unknown"
    bb_upper: Optional[float] = None
    bb_lower: Optional[float] = None
    keltner_upper: Optional[float] = None
    keltner_lower: Optional[float] = None
    note: str = ""


class FibSnapshot(BaseModel):
    """Fibonacci 参考位快照"""

    swing_high: float = 0
    swing_low: float = 0
    direction: str = ""      # "up" / "down"
    levels: list[dict] = Field(default_factory=list)  # [{ratio, price, label}]


class KeyLevelSnapshotV2(BaseModel):
    """关键位系统 V2 完整快照（推送 + 详情页）"""

    ts: int = 0
    current_price: float = 0
    atr: float = 0

    # 核心关键位（不限数量，按距离 + 强度排序）
    levels: list[KeyLevelV2] = Field(default_factory=list)

    # 特殊展示
    bull_bear_line: Optional[BullBearLine] = None
    breakout_zone: Optional[BreakoutZone] = None
    fib_snapshot: Optional[FibSnapshot] = None

    # 信号
    signals: list[KeyLevelSignal] = Field(default_factory=list)
    active_count: int = 0

    # 市场结构摘要
    structure_summary: str = ""
    nearest_strong_support: Optional[float] = None
    nearest_strong_resistance: Optional[float] = None

    # 多周期关键位（日线 / 周线级别最强支撑阻力）
    daily_strong_support: Optional[str] = None
    daily_strong_resistance: Optional[str] = None
    weekly_strong_support: Optional[str] = None
    weekly_strong_resistance: Optional[str] = None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M1（V3 准备阶段）— 磁铁通道 + 数据血统
    # 全部 Optional/默认值，向后兼容
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    magnet_levels: list[LiqMagnetLevel] = Field(default_factory=list)
    data_freshness: Optional[DataFreshness] = None
