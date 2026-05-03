"""短线预测合约信号模型 · 币安事件合约方向预测（V1：BTC × 30min）

设计目标：
- 二元方向预测（up/down）+ 命中概率 + 自动结算闭环
- 完全独立于既有 OpportunityEngine（后者是永续合约 RR setup）
- 仅供测试与统计，不实盘下单（test_mode 永远为 True）
- 兼容 10/30/60min 三种周期，但 V1 实施只启用 30min

铁律（与系统其他模块对齐）：
1. 完全只读 state，不修改任何上游字段
2. 不重新计算 KL / Wall / MAA 评分
3. 信号不构成投资建议，UI 显示 reference_price（参考价）而非 entry_price
4. 数据 quality 不达标 → veto 并明确标注
5. K 线必须 bar_closed=True 才出信号

币安事件合约赔率结构：
- 押 100 USDT，赢拿回 180（净收益 +80），输失 100
- 期望 = 0 临界胜率 = 100/180 ≈ 55.56%
- 任何策略真实命中率 < 56% → 自动停用（auto_disabled）

字段口径：
- reference_price = 信号生成瞬时的 state.ticker.last（非建议价）
- expiry_ts       = created_at + horizon_min × 60
- 结算时取 expiry_ts ±10s 内 last 的中位数（防插针）
- |settlement_price - reference_price| < 0.05% 计入 push（不算输赢）
"""

from __future__ import annotations

import hashlib
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field

from models.common_enums import MarketRegimeLabel


# ────────────────────────────────────────────────────────────────────────────
# 枚举与字面量
# ────────────────────────────────────────────────────────────────────────────

class StrategyName(str, Enum):
    """策略 ID 枚举（V1 三个）"""
    A_SWEEP_RECLAIM = "A_sweep_reclaim"
    B_CVD_DIVERGENCE = "B_cvd_divergence"
    C_RANGE_EDGE_FADE = "C_range_edge_fade"


ScalpDirection = Literal["up", "down"]
HorizonMin = Literal[10, 30, 60]
SignalState = Literal[
    "active",          # 已生成，等待到期
    "expired_won",     # 到期，方向命中
    "expired_lost",    # 到期，方向未中
    "expired_push",    # 到期，价格变化 < 0.05% 不计胜负
    "cancelled",       # 提前取消（手动 / regime 反转 / data stale / 黑天鹅）
]
SignalOutcome = Literal["won", "lost", "push"]

# 取消原因 · 用于双口径统计（P0-4 shadow settlement）
InvalidationKind = Literal["regime_flip", "data_stale", "blackswan", "manual", "conflict"]

# 结算质量 · 反映 settlement_price 取值精度（P0-1）
SettlementQuality = Literal[
    "ok",            # ±10s 内样本 >= 2，中位数可信
    "low_samples",   # ±10s 内只有 1 个样本
    "fallback",      # ±10s 内 0 样本，用 expiry 之后第一个 tick
    "no_data",       # 完全无数据（极端：服务刚启动）
]

# hit_probability 来源 · 透明度（P0-2）
HitProbabilitySource = Literal[
    "calibrated",    # 来自 calibration bucket actual_win_rate
    "uncalibrated",  # 样本不足（< 30），不公开数字
]

# Regime 直接复用项目共享枚举 MarketRegimeLabel（6 分类）：
#   trend_up / trend_down / range / squeeze / high_vol_chop / extreme
# 各策略 suitable_regimes 定义详见 base_strategy.py
RegimeLabel = MarketRegimeLabel


# ────────────────────────────────────────────────────────────────────────────
# 基础数据结构
# ────────────────────────────────────────────────────────────────────────────

class EvidenceItem(BaseModel):
    """单条证据 · 用于看板展示信号决策依据

    - dimension：证据来源维度（"Sweep" / "Absorption" / "CVD" / "KeyLevel" / ...）
    - observation：中文事实陈述（必须引用 facts 中的具体数值或字段）
    - score_contribution：该证据在 confidence 公式里的贡献分（0-1，归一化前）
    - weight：证据权重档位（high/medium/low）
    """
    dimension: str
    observation: str
    score_contribution: float = 0.0
    weight: Literal["high", "medium", "low"] = "medium"


class FactorBreakdown(BaseModel):
    """Confidence 5 因子分解 · 透明度展示

    每个因子 ∈ [0, 1]，weighted_score = Σ(factor × weight)
    总分 confidence = round(weighted_score × 100)
    """
    core_signal_strength: float = 0.0       # 策略本身硬证据强度 · 权重 0.40
    multi_tf_alignment: float = 0.0         # 上下文 prior + 近期方向对齐度 · 权重 0.25
    key_level_quality: float = 0.0          # 相关 KL 的 final_score 归一 · 权重 0.15
    data_freshness: float = 0.0             # 无 stale + bar_closed · 权重 0.10
    historical_winrate: float = 0.0         # 该策略真实命中率（冷启动 0.55，权重随样本量上升） · 权重 0.10

    # 透明度（P0-3）：暴露样本量，前端可据此显示"冷启动 / 校准中 / 已校准"
    historical_winrate_sample_size: int = 0
    historical_winrate_blended_with_default: bool = True  # 样本 < 100 时启用样本量惩罚

    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "core_signal_strength": 0.40,
            "multi_tf_alignment": 0.25,
            "key_level_quality": 0.15,
            "data_freshness": 0.10,
            "historical_winrate": 0.10,
        }
    )

    @property
    def weighted_score(self) -> float:
        """加权总分 ∈ [0, 1]，confidence = round(weighted_score × 100)"""
        return (
            self.core_signal_strength * self.weights["core_signal_strength"]
            + self.multi_tf_alignment * self.weights["multi_tf_alignment"]
            + self.key_level_quality * self.weights["key_level_quality"]
            + self.data_freshness * self.weights["data_freshness"]
            + self.historical_winrate * self.weights["historical_winrate"]
        )


class StateTransition(BaseModel):
    """状态机迁移历史单条记录"""
    ts: int                                 # 秒级 unix
    from_state: SignalState
    to_state: SignalState
    reason: str = ""
    price_at_ts: Optional[float] = None


# ────────────────────────────────────────────────────────────────────────────
# 核心 ScalpSignal · 信号生命周期单元
# ────────────────────────────────────────────────────────────────────────────

class ScalpSignal(BaseModel):
    """短线预测合约信号 · 二元方向 + 自动结算 + 命中率统计

    生命周期：
      created → active（等待到期）→ expired_won/lost/push（命中判定）
            └→ cancelled（提前终止：手动 / regime 反转 / data stale / 黑天鹅）

    结算口径：
      - settlement_price 取 expiry_ts ±10s 内的 last 价（中位数防插针）
      - direction=up + settlement_price > reference_price → won
      - direction=down + settlement_price < reference_price → won
      - |settlement - reference| / reference < 0.0005 → push（不计胜负）

    test_mode：V1 永远 True，UI 全程显示"测试模式"横幅，零实盘风险
    """
    # 标识
    signal_id: str
    coin: str
    horizon_min: HorizonMin
    direction: ScalpDirection
    strategy: StrategyName

    # 价格 / 时机
    reference_price: float                  # 生成瞬时的 state.ticker.last
    created_at: int                         # 秒级 unix
    expiry_ts: int                          # = created_at + horizon_min × 60
    entry_window_sec: int = 60              # 信号有效期（用户应在此时间内开仓）

    # 决策
    confidence: int = Field(ge=0, le=100)
    hit_probability: Optional[float] = Field(default=None, ge=0.0, le=1.0)  # P0-2: 样本不足返 None
    hit_probability_source: HitProbabilitySource = "uncalibrated"           # P0-2 透明度
    calibration_sample_size: int = 0                                        # P0-2 当前 confidence 桶样本量

    regime: RegimeLabel = "range"           # 默认值用最常见的 range（cold start 安全）
    bias_score: float = 0.0                 # 多周期偏置 ∈ [-1, +1]
    factor_breakdown: FactorBreakdown = Field(default_factory=FactorBreakdown)
    evidence: list[EvidenceItem] = Field(default_factory=list)
    veto_check_passed: list[str] = Field(default_factory=list)

    # 版本化（P0-7）：锁定生成时算法版本，便于回放 / A-B 对比 / 出问题溯源
    strategy_version: str = "v1"
    scorer_version: str = "v1"
    config_hash: str = ""                   # ScalpConfig 关键字段 hash 的前 12 位

    # 关键事实快照（P0-7）：保存生成瞬间的核心特征值，避免事后回看时上游 state 已变
    # 字段例：cvd_delta_1h_usd / box_state / box_width_pct / nearest_kl_score / nearest_kl_dist_pct /
    #         rsi_15m / adx_15m / range_test_count_max / fr_8h / oi_change_4h / news_brief_severity
    features_snapshot: dict = Field(default_factory=dict)

    # 状态机
    state: SignalState = "active"
    state_history: list[StateTransition] = Field(default_factory=list)
    invalidation_kind: Optional[InvalidationKind] = None  # P0-4 取消原因（用于 shadow 统计分类）

    # 结算（自动回填）
    settlement_price: Optional[float] = None
    outcome: Optional[SignalOutcome] = None
    settled_at: Optional[int] = None
    settlement_note: Optional[str] = None
    settlement_quality: SettlementQuality = "ok"  # P0-1：反映 settlement_price 精度
    settlement_window_samples: int = 0            # P0-1：±10s 内取到的 tick 样本数

    # Shadow settlement（P0-4）：cancelled 信号到期仍计算"如果不取消会怎样"
    # 仅参与 calibration_shadow 统计，不计入 win_rate
    shadow_settlement_price: Optional[float] = None
    shadow_outcome: Optional[SignalOutcome] = None
    shadow_settled_at: Optional[int] = None

    # 标识：永远是测试模式（V1）
    test_mode: bool = True


# ────────────────────────────────────────────────────────────────────────────
# 配置模型
# ────────────────────────────────────────────────────────────────────────────

class StrategyConfig(BaseModel):
    """单策略配置 · 运行时可通过 PATCH /api/scalp/config 修改"""
    name: StrategyName
    enabled: bool = False                   # 默认全关，由用户登录后手动开
    confidence_threshold: int = Field(default=75, ge=50, le=100)
    cooldown_min: int = Field(default=60, ge=1, le=600)
    notes: str = ""


class ScalpNotificationConfig(BaseModel):
    """通知配置"""
    browser_enabled: bool = True
    browser_min_confidence: int = Field(default=75, ge=0, le=100)
    email_enabled: bool = True
    email_min_confidence: int = Field(default=85, ge=0, le=100)
    test_mode_subject_prefix: str = "[测试]"


class ScalpConfig(BaseModel):
    """整体配置 · 持久化到 backend/data/scalp_signal/config.json

    默认值 = 全部策略 enabled=False（用户登录后手动开），threshold=75（先收样本）
    PATCH /api/scalp/config 可修改任何字段，立即生效不重启
    """
    enabled: bool = True
    test_mode: bool = True                  # V1 永远 True
    coin: str = "BTC"
    horizon_min: HorizonMin = 30

    strategies: dict[StrategyName, StrategyConfig] = Field(
        default_factory=lambda: {
            StrategyName.A_SWEEP_RECLAIM: StrategyConfig(
                name=StrategyName.A_SWEEP_RECLAIM,
                confidence_threshold=75,
                cooldown_min=60,
                notes="扫单回归（流动性扫破后回收 → 反向预测）",
            ),
            StrategyName.B_CVD_DIVERGENCE: StrategyConfig(
                name=StrategyName.B_CVD_DIVERGENCE,
                confidence_threshold=75,
                cooldown_min=60,
                notes="期现 CVD 背离 + 关键位（现强合弱 = 多 / 合强现弱 = 空）",
            ),
            StrategyName.C_RANGE_EDGE_FADE: StrategyConfig(
                name=StrategyName.C_RANGE_EDGE_FADE,
                confidence_threshold=78,
                cooldown_min=90,
                notes="区间边缘均值回归（仅 RANGING + 上沿空 / 下沿多 + Pin bar 确认）",
            ),
        }
    )

    notification: ScalpNotificationConfig = Field(default_factory=ScalpNotificationConfig)


# ────────────────────────────────────────────────────────────────────────────
# 统计 / Calibration 模型
# ────────────────────────────────────────────────────────────────────────────

class ConfidenceBucket(BaseModel):
    """置信度分桶 · 看实际命中率 vs 预测置信度（calibration 基础数据）"""
    range_label: str                        # "70-75" / "75-80" / "80-85" / "85-90" / "90-100"
    sample_size: int = 0
    won_count: int = 0
    lost_count: int = 0
    push_count: int = 0
    actual_win_rate: Optional[float] = None  # = won / (won + lost)
    expected_win_rate: Optional[float] = None  # = 桶中点 / 100


class RegimeSlice(BaseModel):
    """按 regime 切片"""
    regime: str
    sample_size: int = 0
    won: int = 0
    lost: int = 0
    push: int = 0
    win_rate: Optional[float] = None


class HourSlice(BaseModel):
    """按 UTC 小时切片（识别强/弱时段）"""
    hour_utc: int                           # 0-23
    sample_size: int = 0
    won: int = 0
    lost: int = 0
    win_rate: Optional[float] = None


class StrategyStats(BaseModel):
    """单策略统计快照（含全量 + 多维度切片）"""
    strategy: StrategyName
    horizon_min: HorizonMin

    # 全量
    total_signals: int = 0
    won: int = 0
    lost: int = 0
    push: int = 0
    cancelled: int = 0
    win_rate: Optional[float] = None        # = won / (won + lost)（不含 push）
    avg_confidence: Optional[float] = None

    # 净期望（按 0.8:1 赔率，押 100 USDT 单笔）
    # = win_rate × 80 - (1 - win_rate) × 100
    net_return_per_signal_usdt: Optional[float] = None

    # 多维度切片
    confidence_buckets: list[ConfidenceBucket] = Field(default_factory=list)
    by_regime: list[RegimeSlice] = Field(default_factory=list)
    by_hour_utc: list[HourSlice] = Field(default_factory=list)

    # 时间窗口
    window_days: int = 30
    last_window_sample_size: int = 0

    # P0-4 Shadow window：cancelled 信号到期 shadow 结算的双口径统计
    # 用于回答："如果不被取消，cancellation 触发器是否真的有效？"
    shadow_total: int = 0                   # cancelled 且已做 shadow 结算的信号数
    shadow_won: int = 0
    shadow_lost: int = 0
    shadow_win_rate: Optional[float] = None
    shadow_breakdown_by_kind: dict[str, int] = Field(default_factory=dict)  # 按 invalidation_kind 分组

    # 自动停用标记（持续 < 56% 命中率达样本门槛后触发）
    auto_disabled: bool = False
    auto_disabled_reason: Optional[str] = None
    auto_disabled_at: Optional[int] = None

    generated_at: int = 0


class GlobalStats(BaseModel):
    """跨策略汇总（看板顶部 KPI）"""
    total_signals: int = 0
    total_won: int = 0
    total_lost: int = 0
    total_push: int = 0
    overall_win_rate: Optional[float] = None
    overall_net_return_usdt: Optional[float] = None  # 假设每信号押 100
    # Shadow（P0-4）
    overall_shadow_total: int = 0
    overall_shadow_win_rate: Optional[float] = None
    by_strategy: list[StrategyStats] = Field(default_factory=list)
    generated_at: int = 0


class CalibrationPoint(BaseModel):
    """Calibration 曲线单点 · 预测置信度 vs 实际命中率"""
    confidence_mid: float                   # 0.725 = 70-75 桶中点（除以 100）
    sample_size: int
    actual_win_rate: float


class CalibrationCurve(BaseModel):
    """全局 + 各策略独立 calibration 曲线"""
    overall: list[CalibrationPoint] = Field(default_factory=list)
    by_strategy: dict[str, list[CalibrationPoint]] = Field(default_factory=dict)
    sample_size_total: int = 0
    generated_at: int = 0


# ────────────────────────────────────────────────────────────────────────────
# 工厂 / 工具函数
# ────────────────────────────────────────────────────────────────────────────

# 单笔押注金额（USDT，仅用于"净期望"展示，不真实下单）
DEFAULT_STAKE_USDT = 100.0
# 币安事件合约赔率（赢得 stake × 该值，输失 stake）
BINANCE_EVENT_PAYOUT_RATIO = 0.8
# push 阈值：|settlement - reference| / reference < 0.05% 不计胜负
PUSH_THRESHOLD_PCT = 0.05

# Calibration（P0-2）：单 confidence 桶下，至少多少样本才公开 hit_probability 数字
CALIBRATION_MIN_BUCKET_SAMPLES = 30
# Calibration 全局信任阈值：跨桶总样本 >= 该值，才允许使用 calibrated 概率
CALIBRATION_MIN_TOTAL_SAMPLES = 100

# 结算窗口（P0-1）：取 expiry_ts ±N 秒内 last 的中位数
SETTLEMENT_WINDOW_SEC = 10
# 结算窗口最少样本数 → 否则 settlement_quality 标 "low_samples" / "fallback"
SETTLEMENT_WINDOW_MIN_SAMPLES = 2
# Shadow settlement 等待秒数：cancelled 信号到 expiry_ts 后再等多久再 shadow 结算
SHADOW_SETTLE_GRACE_SEC = 10


def make_signal_id(
    coin: str,
    strategy: StrategyName,
    created_at: int,
    direction: ScalpDirection,
) -> str:
    """生成稳定的 signal_id（跨实例可重现，便于回放）"""
    payload = f"{coin}|{strategy.value}|{created_at}|{direction}"
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def calc_expiry_ts(created_at: int, horizon_min: HorizonMin) -> int:
    """根据周期计算到期时间（秒级 unix）"""
    return created_at + int(horizon_min) * 60


def calc_outcome(
    direction: ScalpDirection,
    reference_price: float,
    settlement_price: float,
    push_threshold_pct: float = PUSH_THRESHOLD_PCT,
) -> SignalOutcome:
    """根据方向 + 参考价 + 结算价计算赢/输/平

    push: 价格变化绝对值 < push_threshold_pct (默认 0.05%) → 不计胜负
    避免极小波动被错误归类（如 78400 → 78410，0.013% 噪声不应算输赢）
    """
    if reference_price <= 0:
        return "push"
    diff_pct = abs(settlement_price - reference_price) / reference_price * 100
    if diff_pct < push_threshold_pct:
        return "push"
    if direction == "up":
        return "won" if settlement_price > reference_price else "lost"
    # direction == "down"
    return "won" if settlement_price < reference_price else "lost"


def expected_return_per_signal(
    win_rate: float,
    *,
    odds_payout: float = BINANCE_EVENT_PAYOUT_RATIO,
    stake: float = DEFAULT_STAKE_USDT,
) -> float:
    """按赔率计算单笔期望收益（USDT）

    币安事件合约赔率 0.8:1：
      - 赢：+ stake × 0.8
      - 输：- stake
    临界 win_rate = 1 / (1 + 0.8) ≈ 55.56%

    Args:
        win_rate: 实际命中率 ∈ [0, 1]
        odds_payout: 赢的赔率（赢得 stake × odds_payout）
        stake: 单笔押注金额（默认 100 USDT）

    Returns:
        期望收益（USDT，可负）
    """
    return win_rate * stake * odds_payout - (1.0 - win_rate) * stake


def break_even_win_rate(odds_payout: float = BINANCE_EVENT_PAYOUT_RATIO) -> float:
    """根据赔率计算盈亏平衡点命中率

    公式：win × payout = (1 - win) × 1.0 → win = 1 / (1 + payout)
    币安 0.8:1 → 0.5556
    """
    return 1.0 / (1.0 + odds_payout)
