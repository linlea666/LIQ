"""V1 vs V2 关键位行为对比回测引擎（V3-M3 · 2026-04）

定位：
    M2.5 上线后，KeyLevelV2 同时携带 V1 旧字段 (bounce_quality / breakout_stage /
    state==fake_break) 与 V2 新影子字段 (behavior.bounce_quality_enhanced /
    breakout_stage_enhanced / fake_break_strength)。本模块在历史快照上做事后真相
    判定，量化对比 V1 vs V2 在反弹质量、突破阶段、假破回收三个维度的命中率，
    供 M4 决定是否切换。

设计纪律：
    1. 纯函数库：无 IO、无单例、无运行时副作用
    2. 只消费历史快照 (list[KeyLevelSnapshotV2])，不调用 tracker / signal_builder
    3. 不修改 KeyLevelV2 / BehaviorEval；不写日志副作用
    4. 任何"真相不可判定"情形 → ambiguous，**不计入分母**（避免污染指标）
    5. 单元可测：每个判定函数可独立喂合成数据验证

主要数据流：
    list[KeyLevelSnapshotV2]
       ↓  build_outcome_records (按 level_id 配对当前快照 vs N 小时后快照)
    list[OutcomeRecord]
       ↓  compute_comparison_stats (按维度切分 + 卡方检验)
    list[ComparisonStats]   ← API / CLI / 前端消费

事后真相判定阈值（针对 ATR）：
    - 真破：close 偏离破位方向 ≥ truth_atr_mult × ATR (默认 1.0)
    - 假破：close 已回到未破侧
    - 中间区域 → ambiguous

字段对应矩阵（V1 → V2）：
    | 维度        | V1（来自 lv 顶层）              | V2（来自 lv.behavior）          |
    | 反弹质量    | bounce_quality (str)           | bounce_quality_enhanced (0-1)   |
    | 突破阶段    | breakout_stage (0/1/2/3)       | breakout_stage_enhanced (0/1/2/3) |
    | 假破回收    | state == "fake_break"          | fake_break_strength (0-1)       |

注：`dynamic_break_depth_pct` 是阈值建议值不是预测值，回测时不参与命中率对比，
   只在 CLI 报告中作为"V2 提议的阈值范围"参考列表展示。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from models.key_level import KeyLevelSnapshotV2, KeyLevelV2


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 配置
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

DEFAULT_FUTURE_WINDOW_SEC = 4 * 3600         # 4h 后判真相（基线 = 1H timeframe）
DEFAULT_TRUTH_ATR_MULT = 1.0                 # 偏离 1×ATR 算"显著"
DEFAULT_AMBIGUOUS_ATR_BAND = 0.3             # ≤ 0.3×ATR 算"未动" → ambiguous
DEFAULT_V2_BINARY_THRESHOLD = 0.5            # V2 0-1 → 二分类阈值

# V3-M4 P1-2：按 timeframe 缩放 future_window_sec / tolerance_sec
# 基线为 1H → 1.0；1D level 用 24×window 才能等到回踩，1W 用 168×。
# 与 key_level_behavior_eval._TF_SCALE_SECONDS 同源（保持一致；各自定义避免跨模块耦合）。
_TF_WINDOW_SCALE: dict[str, float] = {
    "15m": 0.25, "30m": 0.5,
    "1H": 1.0,  "1h": 1.0,
    "2H": 2.0,  "2h": 2.0,
    "4H": 4.0,  "4h": 4.0,
    "12H": 12.0, "12h": 12.0,
    "1D": 24.0,  "1d": 24.0,
    "3D": 72.0,  "3d": 72.0,
    "1W": 168.0, "1w": 168.0,
}
# 容差缩放上限（避免 1W level 的 tolerance 撑到 100h+ 导致 future 配对失真）
_TOLERANCE_CAP_SEC = 6 * 3600


def _resolve_tf_scaled_window(
    timeframe: str, base_window_sec: int, base_tolerance_sec: int,
) -> tuple[int, int]:
    """按 timeframe 缩放 future_window / tolerance（V3-M4 P1-2）。

    返回 (scaled_window_sec, scaled_tolerance_sec)；timeframe 未知时按 1.0 处理。
    tolerance 同比例缩放但封顶 _TOLERANCE_CAP_SEC，防止超长窗口下配对漂移过大。
    """
    tf = (timeframe or "1H").strip()
    scale = _TF_WINDOW_SCALE.get(tf, 1.0)
    return (
        int(base_window_sec * scale),
        min(int(base_tolerance_sec * scale), _TOLERANCE_CAP_SEC),
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 数据结构
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

@dataclass
class OutcomeRecord:
    """单 level 在某个快照时刻的「预测 vs 真相」配对。

    每条记录代表："t0 时刻 level_id=X 的预测，对照 t0+window 时刻的市场真相"。
    一个 level 在多个快照中会产生多条记录（取决于配对密度）。
    """
    coin: str
    snapshot_ts: int
    future_ts: int

    level_id: str
    level_price: float
    level_side: str  # "support" / "resistance"
    strength_tier: str  # S/A/B/C
    state: str       # 评估时的 V1 state
    state_ts: int    # state 进入的时间戳（用于事件去重）
    timeframe: str
    regime: str      # 评估时所处的 regime（来自 lv.regime_at_score 或 snap.regime）

    # ── V1 端预测（来自 lv 顶层） ──
    v1_bounce_quality: str        # "" / "proactive" / "passive"
    v1_breakout_stage: int        # 0/1/2/3
    v1_state_is_fake_break: bool  # state == "fake_break"
    v1_state_is_broken: bool      # state in {"broken", "flipped"}

    # ── V2 端预测（来自 lv.behavior） ──
    v2_bounce_quality_enhanced: float
    v2_breakout_stage_enhanced: int
    v2_fake_break_strength: float
    v2_dynamic_break_depth_pct: float

    # ── 事后真相 ──
    future_price: float
    future_atr: float
    # 方向化距离：support 用 -dist（破位为负），resistance 用 +dist（破位为正）
    # 单位是 ATR 倍数
    future_distance_atr: float

    breakout_truth: Optional[str] = None    # "true_breakout"|"failed_breakout"|"ambiguous"|None
    bounce_truth: Optional[str] = None      # "real_bounce"|"weak_bounce"|"ambiguous"|None
    fake_break_truth: Optional[str] = None  # "confirmed_fake"|"true_break"|"ambiguous"|None


@dataclass
class ConfusionMatrix:
    """二分类混淆矩阵。"""
    tp: int = 0
    fp: int = 0
    tn: int = 0
    fn: int = 0

    @property
    def total(self) -> int:
        return self.tp + self.fp + self.tn + self.fn

    @property
    def accuracy(self) -> float:
        return (self.tp + self.tn) / self.total if self.total else 0.0

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if (self.tp + self.fp) else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if (self.tp + self.fn) else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if (p + r) else 0.0

    @property
    def specificity(self) -> float:
        """特异度 = TN / (TN + FP)；类别不平衡时与 recall 配对评估。"""
        return self.tn / (self.tn + self.fp) if (self.tn + self.fp) else 0.0

    @property
    def balanced_accuracy(self) -> float:
        """平衡准确率 = (recall + specificity) / 2；类别不平衡更稳健。"""
        return (self.recall + self.specificity) / 2.0

    @property
    def mcc(self) -> float:
        """Matthews 相关系数（-1~1）：-1 全错，0 随机，1 完美。
        类别不平衡下比 accuracy/F1 更可靠的单指标。"""
        denom_sq = (
            (self.tp + self.fp) * (self.tp + self.fn)
            * (self.tn + self.fp) * (self.tn + self.fn)
        )
        if denom_sq <= 0:
            return 0.0
        num = self.tp * self.tn - self.fp * self.fn
        return num / math.sqrt(denom_sq)

    def to_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn,
            "accuracy": round(self.accuracy, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
            "specificity": round(self.specificity, 4),
            "balanced_accuracy": round(self.balanced_accuracy, 4),
            "mcc": round(self.mcc, 4),
        }


@dataclass
class CalibrationBucket:
    """V2 0-1 分数的单个分桶校准结果。

    用于检查"高分桶命中率是否单调高于低分桶"；
    若不单调，说明 V2 分数没有判别力（数值上的高分≠真实的高命中）。
    """
    range_low: float      # [0.0, 0.2, 0.4, 0.6, 0.8]
    range_high: float
    sample_size: int
    hit_count: int        # 真相 = 正样本的数量
    hit_rate: float       # hit_count / sample_size

    def to_dict(self) -> dict:
        return {
            "range_low": round(self.range_low, 2),
            "range_high": round(self.range_high, 2),
            "sample_size": self.sample_size,
            "hit_count": self.hit_count,
            "hit_rate": round(self.hit_rate, 4),
        }


@dataclass
class ComparisonStats:
    """V1 vs V2 单维度对比指标（M3.1 升级：McNemar + Wilson CI + 校准）。"""
    dimension: str  # "bounce_quality" / "breakout_stage" / "fake_break"
    sample_size: int
    ambiguous_count: int  # 因真相不可判定被剔除的样本数（透明度）

    confusion_v1: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    confusion_v2: ConfusionMatrix = field(default_factory=ConfusionMatrix)

    delta_accuracy: float = 0.0      # v2.accuracy - v1.accuracy
    delta_precision: float = 0.0
    delta_recall: float = 0.0
    delta_f1: float = 0.0
    delta_balanced_accuracy: float = 0.0
    delta_mcc: float = 0.0

    # ── 卡方检验（保留作参考；GPT 审查指出对配对样本不严谨） ──
    chi_square_stat: float = 0.0
    chi_square_p_value: float = 1.0

    # ── McNemar 检验（M3.1 新增 · 配对样本主指标） ──
    # 看 V1 错/V2 对 vs V1 对/V2 错 的方向性优势
    discordant_v1_wrong_v2_right: int = 0  # b：V1 错 而 V2 对
    discordant_v1_right_v2_wrong: int = 0  # c：V1 对 而 V2 错
    mcnemar_stat: float = 0.0      # χ² = (|b-c|-1)²/(b+c) （连续性校正）
    mcnemar_p_value: float = 1.0

    # ── 多重比较修正（V3-M4 P1-1 新增 · 跨维度族错误率控制） ──
    # 解决"3 维度平行检验导致的假阳性膨胀（α=0.05 → 实际 ≈ 14.3%）"。
    # 由 run_full_comparison 在 3 个 stats 出来后回写：
    #   mcnemar_p_bonferroni = min(1, p × N_tests)         （保守，控 FWER）
    #   mcnemar_p_fdr        = BH 修正后 q 值              （宽松，控 FDR）
    # 决策（is_v2_significantly_better）使用 Bonferroni 修正后的 p。
    # 单维度查询（如 compute_*_stats 直接调用）不做修正，二者 = mcnemar_p_value。
    family_size: int = 1                  # 修正时所属"检验族"大小（默认 1=不修正）
    mcnemar_p_bonferroni: float = 1.0     # Bonferroni 修正后 p
    mcnemar_p_fdr: float = 1.0            # BH 修正后 q（FDR 控制）

    # ── Wilson 95% CI（M3.1 新增 · 替代点估计） ──
    accuracy_ci_v1: tuple[float, float] = (0.0, 0.0)
    accuracy_ci_v2: tuple[float, float] = (0.0, 0.0)

    # ── 决策综合（M3.1 升级 · 多条件联合判定） ──
    is_v2_significantly_better: bool = False
    decision_reasons: list[str] = field(default_factory=list)  # 通过/不通过的具体原因

    # ── 分桶校准（M3.1 新增 · 仅 V2 0-1 分数有效；breakout_stage 维度无） ──
    calibration_v2: list[CalibrationBucket] = field(default_factory=list)
    calibration_monotonic: bool = False  # 高分桶 hit_rate 是否单调 ≥ 低分桶

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "sample_size": self.sample_size,
            "ambiguous_count": self.ambiguous_count,
            "v1": self.confusion_v1.to_dict(),
            "v2": self.confusion_v2.to_dict(),
            "delta_accuracy": round(self.delta_accuracy, 4),
            "delta_precision": round(self.delta_precision, 4),
            "delta_recall": round(self.delta_recall, 4),
            "delta_f1": round(self.delta_f1, 4),
            "delta_balanced_accuracy": round(self.delta_balanced_accuracy, 4),
            "delta_mcc": round(self.delta_mcc, 4),
            "chi_square_stat": round(self.chi_square_stat, 4),
            "chi_square_p_value": round(self.chi_square_p_value, 4),
            "discordant_v1_wrong_v2_right": self.discordant_v1_wrong_v2_right,
            "discordant_v1_right_v2_wrong": self.discordant_v1_right_v2_wrong,
            "mcnemar_stat": round(self.mcnemar_stat, 4),
            "mcnemar_p_value": round(self.mcnemar_p_value, 4),
            "family_size": self.family_size,
            "mcnemar_p_bonferroni": round(self.mcnemar_p_bonferroni, 4),
            "mcnemar_p_fdr": round(self.mcnemar_p_fdr, 4),
            "accuracy_ci_v1": [round(x, 4) for x in self.accuracy_ci_v1],
            "accuracy_ci_v2": [round(x, 4) for x in self.accuracy_ci_v2],
            "is_v2_significantly_better": self.is_v2_significantly_better,
            "decision_reasons": list(self.decision_reasons),
            "calibration_v2": [b.to_dict() for b in self.calibration_v2],
            "calibration_monotonic": self.calibration_monotonic,
        }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 事后真相判定（核心：把"市场后续走势"翻译成离散标签）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _signed_distance_atr(
    level_price: float, future_price: float, side: str, atr: float,
) -> float:
    """方向化的"未来价格相对 level 的偏离量"，单位 ATR。

    支撑：future < level 表示"已破位"（返回负值）；future > level 表示"在 level 上方"（正值）
    阻力：future > level 表示"已破位"（返回正值）；future < level 表示"在 level 下方"（负值）
    """
    if atr <= 0 or level_price <= 0:
        return 0.0
    raw = (future_price - level_price) / atr
    return raw  # support / resistance 对"破位"的方向不同，由调用方解释


def evaluate_breakout_outcome(
    lv: KeyLevelV2,
    future_price: float,
    future_atr: float,
    *,
    truth_atr_mult: float = DEFAULT_TRUTH_ATR_MULT,
    ambiguous_band: float = DEFAULT_AMBIGUOUS_ATR_BAND,
) -> Optional[str]:
    """判定 lv 在"未来时刻"是否真的发生了突破。

    仅当 lv.state ∈ {broken, flipped} 时有意义；其它返回 None。

    返回：
      - "true_breakout"   未来 close 偏离破位方向 ≥ truth_atr_mult × ATR
      - "failed_breakout" 未来 close 已回到未破侧（≥ ambiguous_band × ATR）
      - "ambiguous"       未来 close 在 [-ambiguous_band, +truth_atr_mult] × ATR 之间
      - None              state 不匹配 / ATR 缺失 / 数据不足
    """
    if lv.state not in ("broken", "flipped"):
        return None
    if future_atr <= 0 or lv.price <= 0 or future_price <= 0:
        return None

    dist_atr = _signed_distance_atr(lv.price, future_price, lv.side, future_atr)

    # 支撑：破位是向下；阻力：破位是向上
    if lv.side == "support":
        # dist_atr < 0 = 在 level 下方（破位方向）
        if dist_atr <= -truth_atr_mult:
            return "true_breakout"
        if dist_atr >= ambiguous_band:
            return "failed_breakout"
        return "ambiguous"
    else:  # resistance
        if dist_atr >= truth_atr_mult:
            return "true_breakout"
        if dist_atr <= -ambiguous_band:
            return "failed_breakout"
        return "ambiguous"


def evaluate_bounce_outcome(
    lv: KeyLevelV2,
    future_price: float,
    future_atr: float,
    *,
    truth_atr_mult: float = DEFAULT_TRUTH_ATR_MULT,
    ambiguous_band: float = DEFAULT_AMBIGUOUS_ATR_BAND,
) -> Optional[str]:
    """判定 lv 标记 bounced 后是否"真反弹"。

    仅当 lv.state == "bounced" 时有意义。

    返回：
      - "real_bounce" 未来 close 沿反弹方向延续 ≥ truth_atr_mult × ATR
      - "weak_bounce" 未来 close 已穿透到反弹反向（≥ ambiguous_band × ATR）
      - "ambiguous"   未来 close 在中间带
      - None          state 不匹配 / ATR 缺失
    """
    if lv.state != "bounced":
        return None
    if future_atr <= 0 or lv.price <= 0 or future_price <= 0:
        return None

    dist_atr = _signed_distance_atr(lv.price, future_price, lv.side, future_atr)

    # 支撑反弹：方向是向上（dist_atr > 0）
    if lv.side == "support":
        if dist_atr >= truth_atr_mult:
            return "real_bounce"
        if dist_atr <= -ambiguous_band:
            return "weak_bounce"
        return "ambiguous"
    else:  # resistance 反弹：方向向下
        if dist_atr <= -truth_atr_mult:
            return "real_bounce"
        if dist_atr >= ambiguous_band:
            return "weak_bounce"
        return "ambiguous"


def evaluate_fake_break_outcome(
    lv: KeyLevelV2,
    future_price: float,
    future_atr: float,
    *,
    truth_atr_mult: float = DEFAULT_TRUTH_ATR_MULT,
    ambiguous_band: float = DEFAULT_AMBIGUOUS_ATR_BAND,
) -> Optional[str]:
    """判定"曾被标记为破位"的 lv 是否实际为假破。

    适用 state ∈ {broken, flipped, fake_break} 的样本（与 evaluate_breakout 同源数据）。

    返回：
      - "confirmed_fake" 未来 close 已回到未破侧 ≥ ambiguous_band × ATR
      - "true_break"     未来 close 继续偏离破位方向 ≥ truth_atr_mult × ATR
      - "ambiguous"      中间带
      - None             state 不匹配 / ATR 缺失
    """
    if lv.state not in ("broken", "flipped", "fake_break"):
        return None
    if future_atr <= 0 or lv.price <= 0 or future_price <= 0:
        return None

    dist_atr = _signed_distance_atr(lv.price, future_price, lv.side, future_atr)

    if lv.side == "support":
        if dist_atr >= ambiguous_band:
            return "confirmed_fake"
        if dist_atr <= -truth_atr_mult:
            return "true_break"
        return "ambiguous"
    else:  # resistance
        if dist_atr <= -ambiguous_band:
            return "confirmed_fake"
        if dist_atr >= truth_atr_mult:
            return "true_break"
        return "ambiguous"


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 主入口：从历史快照构建 OutcomeRecord 列表
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _find_future_snapshot(
    history: list[KeyLevelSnapshotV2],
    base_idx: int,
    future_window_sec: int,
    tolerance_sec: int = 600,
) -> Optional[KeyLevelSnapshotV2]:
    """在 history 中找到 base_idx 之后、最接近 base.ts + window 的快照。

    history 必须按 ts 升序。tolerance 控制"配对窗口的容差"。
    """
    if base_idx >= len(history):
        return None
    base = history[base_idx]
    target_ts = base.ts + future_window_sec
    best: Optional[KeyLevelSnapshotV2] = None
    best_delta = float("inf")
    for j in range(base_idx + 1, len(history)):
        snap = history[j]
        delta = abs(snap.ts - target_ts)
        if delta > tolerance_sec and best is None and snap.ts > target_ts:
            # 已经超过 target + tolerance 且还没找到候选 → 放弃
            return None
        if delta <= tolerance_sec and delta < best_delta:
            best = snap
            best_delta = delta
        elif snap.ts > target_ts + tolerance_sec:
            break
    return best


def build_outcome_records(
    history: list[KeyLevelSnapshotV2],
    *,
    coin: str,
    future_window_sec: int = DEFAULT_FUTURE_WINDOW_SEC,
    tolerance_sec: int = 600,
    truth_atr_mult: float = DEFAULT_TRUTH_ATR_MULT,
    ambiguous_band: float = DEFAULT_AMBIGUOUS_ATR_BAND,
    deduplicate: bool = True,
    require_behavior_eval: bool = True,
    timeframe_adaptive_window: bool = True,
) -> list[OutcomeRecord]:
    """从一组历史快照生成 OutcomeRecord。

    流程：
      1. history 按 ts 升序排序
      2. 对每个 base_snap：按 lv.timeframe 缩放 window 后单独配对 future_snap
      3. 调用 3 个 evaluate_* 函数生成真相
      4. 过滤无 level_id / 无 ATR / 无配对的样本

    设计选择（M3.1 + M4 P1-2 升级）：
      - level_id 是配对依据（M3 R9 已稳定）；缺 level_id 的旧样本被跳过
      - **timeframe 自适应窗口**（V3-M4 P1-2，默认 True）：
        future_window_sec 视为 1H timeframe 基线，按 lv.timeframe 缩放：
          15m→×0.25 / 1H→×1.0 / 4H→×4.0 / 1D→×24 / 1W→×168
        否则 1D/1W level 在 4h 窗口下还没回踩就盖戳，导致大量误判。
        若 timeframe_adaptive_window=False 则保持旧行为（向后兼容测试）
      - **事件去重**（deduplicate=True，默认）：同一 (level_id, state, state_ts)
        在 N 个连续快照中只保留**最早一条**，避免一个物理事件膨胀样本数；
        若 deduplicate=False 则保留全部时序观测（旧行为）
      - **元信息过滤**（require_behavior_eval=True，默认）：跳过
        behavior_eval_available=False 的样本（避免污染回测）
      - look-ahead 防护：仅读 future.current_price + future.atr，
        **不**读 future_snap.levels[*].state（不让"未来 state"污染当时预测的评估）
    """
    if not history:
        return []
    ordered = sorted(history, key=lambda h: h.ts)
    records: list[OutcomeRecord] = []
    # 去重 key：(level_id, state, state_ts, dimension); 这里 dimension 隐含在 state
    # 中（bounced=反弹维度，broken/flipped=突破/假破维度）。
    seen_event_keys: set[tuple[str, str, int]] = set()

    for i, base in enumerate(ordered):
        # 优先用 lv 自身 regime；fallback 到 snapshot 级 regime
        snap_regime = getattr(base, "regime", "") or ""
        # 缓存：每个 timeframe 在当前 base 下只查一次 future（避免 O(N×L) 二次遍历）
        future_cache: dict[str, Optional[KeyLevelSnapshotV2]] = {}

        for lv in base.levels:
            if not lv.level_id:
                continue
            # 元信息过滤：behavior 评估失败的样本不参与回测（避免 0 分污染）
            beh = lv.behavior
            if require_behavior_eval and beh is not None:
                if not getattr(beh, "behavior_eval_available", True):
                    continue

            # 事件去重：同一 (level_id, state, state_ts) 只保留最早记录
            if deduplicate:
                key = (lv.level_id, lv.state, int(lv.state_ts or 0))
                if key in seen_event_keys:
                    continue
                seen_event_keys.add(key)

            # V3-M4 P1-2：按 timeframe 缩放 window；同 base 同 tf 缓存复用
            tf_key = (lv.timeframe or "1H").strip() if timeframe_adaptive_window else "_FIXED_"
            if tf_key not in future_cache:
                if timeframe_adaptive_window:
                    win, tol = _resolve_tf_scaled_window(
                        tf_key, future_window_sec, tolerance_sec,
                    )
                else:
                    win, tol = future_window_sec, tolerance_sec
                future_cache[tf_key] = _find_future_snapshot(ordered, i, win, tol)
            future = future_cache[tf_key]
            if future is None:
                continue
            future_price = future.current_price
            future_atr = future.atr if future.atr > 0 else base.atr
            if future_price <= 0 or future_atr <= 0:
                continue

            dist_atr = _signed_distance_atr(lv.price, future_price, lv.side, future_atr)
            lv_regime = getattr(lv, "regime_at_score", "") or snap_regime

            rec = OutcomeRecord(
                coin=coin,
                snapshot_ts=base.ts,
                future_ts=future.ts,
                level_id=lv.level_id,
                level_price=lv.price,
                level_side=lv.side,
                strength_tier=lv.strength_tier or "C",
                state=lv.state,
                state_ts=int(lv.state_ts or 0),
                timeframe=lv.timeframe or "",
                regime=lv_regime,
                v1_bounce_quality=lv.bounce_quality or "",
                v1_breakout_stage=int(lv.breakout_stage or 0),
                v1_state_is_fake_break=(lv.state == "fake_break"),
                v1_state_is_broken=(lv.state in ("broken", "flipped")),
                v2_bounce_quality_enhanced=float(beh.bounce_quality_enhanced) if beh else 0.0,
                v2_breakout_stage_enhanced=int(beh.breakout_stage_enhanced) if beh else 0,
                v2_fake_break_strength=float(beh.fake_break_strength) if beh else 0.0,
                v2_dynamic_break_depth_pct=float(beh.dynamic_break_depth_pct) if beh else 0.0,
                future_price=future_price,
                future_atr=future_atr,
                future_distance_atr=dist_atr,
                breakout_truth=evaluate_breakout_outcome(
                    lv, future_price, future_atr,
                    truth_atr_mult=truth_atr_mult, ambiguous_band=ambiguous_band,
                ),
                bounce_truth=evaluate_bounce_outcome(
                    lv, future_price, future_atr,
                    truth_atr_mult=truth_atr_mult, ambiguous_band=ambiguous_band,
                ),
                fake_break_truth=evaluate_fake_break_outcome(
                    lv, future_price, future_atr,
                    truth_atr_mult=truth_atr_mult, ambiguous_band=ambiguous_band,
                ),
            )
            records.append(rec)
    return records


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 统计检验（不引入 scipy 依赖；McNemar = 配对样本金标准）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _chi_square_2x2_p(c1: ConfusionMatrix, c2: ConfusionMatrix) -> tuple[float, float]:
    """非配对 2x2 卡方（保留作参考；对配对样本不严谨）。

    返回 (chi_square_stat, p_value)。
    df=1 时用近似 p_value: 利用 erfc 关系；不依赖 scipy。

    列联表：
              correct   wrong
       v1     a         b      a+b
       v2     c         d      c+d
              a+c       b+d    n

    注：V1/V2 在同一事件上做判断 → **配对样本** → 应优先用 mcnemar_test。
    本函数仅在历史报告 / 兼容场景中提供参考。
    """
    a = c1.tp + c1.tn
    b = c1.fp + c1.fn
    c = c2.tp + c2.tn
    d = c2.fp + c2.fn
    n = a + b + c + d
    if n < 10 or (a + c) == 0 or (b + d) == 0 or (a + b) == 0 or (c + d) == 0:
        return 0.0, 1.0

    numerator = abs(a * d - b * c) - n / 2.0
    if numerator <= 0:
        return 0.0, 1.0
    chi_sq = (n * numerator * numerator) / ((a + b) * (c + d) * (a + c) * (b + d))
    p = math.erfc(math.sqrt(chi_sq / 2.0))
    return chi_sq, p


def mcnemar_test(b: int, c: int) -> tuple[float, float]:
    """McNemar 检验（配对样本主指标）。

    输入：
      b = V1 错而 V2 对（discordant_v1_wrong_v2_right）
      c = V1 对而 V2 错（discordant_v1_right_v2_wrong）

    一致样本（V1/V2 同对或同错）不影响检验结果。

    返回 (chi_square_stat, p_value)；df=1。
    带 Edwards 连续性校正（|b-c| - 1），样本不平衡时更稳健。
    """
    if b + c < 10:
        # 样本太少：精确二项检验更合理（这里简化返回不显著）
        return 0.0, 1.0
    diff = abs(b - c) - 1
    if diff <= 0:
        return 0.0, 1.0
    chi_sq = (diff * diff) / (b + c)
    p = math.erfc(math.sqrt(chi_sq / 2.0))
    return chi_sq, p


def wilson_ci(success: int, total: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 二项分布置信区间（默认 95% CI）。

    比 Wald 区间在小样本和极端比例时更稳健（不会越过 0/1 边界）。

    返回 (low, high)。total=0 时返回 (0, 0)。
    """
    if total <= 0:
        return 0.0, 0.0
    p_hat = success / total
    z2 = z * z
    denom = 1.0 + z2 / total
    centre = (p_hat + z2 / (2 * total)) / denom
    margin = z * math.sqrt((p_hat * (1 - p_hat) / total + z2 / (4 * total * total))) / denom
    return max(0.0, centre - margin), min(1.0, centre + margin)


def compute_calibration_buckets(
    records: list["OutcomeRecord"],
    *,
    dimension: str,
    bucket_count: int = 5,
) -> list[CalibrationBucket]:
    """计算 V2 0-1 分数的分桶校准（仅对连续分数维度有意义）。

    设计：
      - 把 V2 分数按等宽切 5 桶 [0,0.2)(0.2,0.4)...(0.8,1.0]
      - 每桶统计真实命中率（真相 = 正样本占比）
      - 期望：高分桶 hit_rate 单调 ≥ 低分桶；若不单调则 V2 分数无判别力

    breakout_stage 维度不适用（V2 输出 0/1/2/3 离散），返回空列表。
    """
    if dimension == "breakout_stage":
        return []

    # 选取与维度对应的 V2 分数 + 真相
    pairs: list[tuple[float, bool]] = []
    if dimension == "bounce_quality":
        for r in records:
            if r.state != "bounced" or r.bounce_truth in (None, "ambiguous"):
                continue
            pairs.append((r.v2_bounce_quality_enhanced, r.bounce_truth == "real_bounce"))
    elif dimension == "fake_break":
        for r in records:
            if r.fake_break_truth in (None, "ambiguous"):
                continue
            pairs.append((r.v2_fake_break_strength, r.fake_break_truth == "confirmed_fake"))
    else:
        return []

    if not pairs:
        return []

    width = 1.0 / bucket_count
    buckets: list[CalibrationBucket] = []
    for i in range(bucket_count):
        low = i * width
        high = (i + 1) * width if i < bucket_count - 1 else 1.0001  # 保证 1.0 落入最后桶
        in_bucket = [t for s, t in pairs if low <= s < high]
        n = len(in_bucket)
        hits = sum(1 for t in in_bucket if t)
        buckets.append(CalibrationBucket(
            range_low=low,
            range_high=min(high, 1.0),
            sample_size=n,
            hit_count=hits,
            hit_rate=(hits / n) if n else 0.0,
        ))
    return buckets


def _is_calibration_monotonic(buckets: list[CalibrationBucket]) -> bool:
    """检查校准桶 hit_rate 是否（弱）单调递增。

    允许小波动：仅当任一相邻桶 hit_rate 反向降幅 > 0.10 才视为不单调。
    桶样本少（n<3）的桶视为"无效"跳过判定。
    """
    valid = [b for b in buckets if b.sample_size >= 3]
    if len(valid) < 2:
        return False
    for i in range(len(valid) - 1):
        if valid[i + 1].hit_rate + 0.10 < valid[i].hit_rate:
            return False
    return True


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 多重比较修正（V3-M4 P1-1 · 跨维度族错误率控制）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 痛点：
#   run_full_comparison 同时跑 3 个维度（bounce_quality / breakout_stage /
#   fake_break），各得一个 McNemar p 值。若直接以"任一 p<0.05 即认为 V2 显著优"，
#   则族错误率（Family-Wise Error Rate）= 1 - 0.95^3 ≈ 14.3%，远高于 5%。
#
# 解决：
#   - Bonferroni：p_adj = min(1, p × N)，最严格，控 FWER
#   - Benjamini-Hochberg (BH/FDR)：控期望错误发现率，比 Bonferroni 宽松一些，
#     大规模检验中更实用；这里 N=3 时差别不大但保留以便后续扩展（如多 coin / 多 regime）
#
# 决策（_evaluate_decision）使用 Bonferroni 后的 p（最严格 → 最少假阳性）。

def bonferroni_correction(p_values: list[float]) -> list[float]:
    """Bonferroni 修正：p_adj_i = min(1, p_i × N)。

    无脑保守，直接乘检验数。N<=1 时返回原 p。
    """
    n = len(p_values)
    if n <= 1:
        return list(p_values)
    return [min(1.0, p * n) for p in p_values]


def benjamini_hochberg_correction(p_values: list[float]) -> list[float]:
    """Benjamini-Hochberg (FDR) 修正。

    步骤：
      1. 把 p_values 升序排列，记原索引
      2. 按排名 i (1-based) 计算 q_i = p_i × N / i
      3. 从大到小累计取最小值（保证单调）：q_i = min(q_i, q_{i+1})
      4. 按原索引顺序输出

    返回与输入等长的 q 值列表（每个 q ≤ 1.0）。
    """
    n = len(p_values)
    if n <= 1:
        return list(p_values)
    indexed = sorted(enumerate(p_values), key=lambda x: x[1])  # [(orig_idx, p), ...]
    q_sorted = [0.0] * n
    # 步骤 2 + 3：从大到小累积取 min
    last_q = 1.0
    for rank in range(n - 1, -1, -1):
        _, p = indexed[rank]
        q_raw = p * n / (rank + 1)  # rank 是 0-based，所以 +1
        last_q = min(last_q, q_raw)
        q_sorted[rank] = min(1.0, last_q)
    out = [0.0] * n
    for sort_pos, (orig_idx, _) in enumerate(indexed):
        out[orig_idx] = q_sorted[sort_pos]
    return out


def _apply_multiple_comparison(stats_list: list[ComparisonStats]) -> None:
    """对一组 ComparisonStats 就地写入 Bonferroni / FDR 修正 + 重算 is_v2_significantly_better。

    设计：
      - 修正只作用在 mcnemar_p_value（McNemar 是配对样本主指标）
      - 修正后用 mcnemar_p_bonferroni（更严格）作为决策 p
      - decision_reasons 中"McNemar p"行替换为"McNemar p (Bonferroni N=3)"
      - 单维度调用 compute_*_stats 不会经过此函数 → 保持原始 p（行为兼容）
    """
    if not stats_list:
        return
    n = len(stats_list)
    p_raws = [s.mcnemar_p_value for s in stats_list]
    p_bonf = bonferroni_correction(p_raws)
    p_fdr = benjamini_hochberg_correction(p_raws)

    for i, s in enumerate(stats_list):
        s.family_size = n
        s.mcnemar_p_bonferroni = p_bonf[i]
        s.mcnemar_p_fdr = p_fdr[i]
        # 用修正后 p 重判决策
        is_better, reasons = _evaluate_decision(
            s.confusion_v1, s.confusion_v2,
            s.mcnemar_p_bonferroni, s.sample_size,
            s.calibration_monotonic,
            p_label=f"McNemar p (Bonferroni N={n})",
        )
        s.is_v2_significantly_better = is_better
        s.decision_reasons = reasons


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 三个维度的 V1/V2 命中率计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 显著性阈值
P_VALUE_SIGNIFICANT = 0.05
# M3.1 升级（GPT 审查）：30 → 100，30~100 视为"观察期"
MIN_SAMPLES_TRUSTED = 100
MIN_SAMPLES_OBSERVE = 30
RECALL_FLOOR_RATIO = 0.85  # V2 recall 不能低于 V1 × 此比例


def _evaluate_decision(
    c_v1: ConfusionMatrix, c_v2: ConfusionMatrix,
    mcnemar_p: float, sample_size: int,
    calibration_monotonic: bool,
    *,
    p_label: str = "McNemar p",
) -> tuple[bool, list[str]]:
    """V2 显著优于 V1 的多条件联合判定（M3.1 升级 · GPT 审查采纳）。

    必要条件（全部满足才算"V2 显著优"）：
      1. 样本量 ≥ MIN_SAMPLES_TRUSTED (=100)
      2. McNemar p < 0.05（配对检验显著，非卡方）
         · 多维度调用时由 _apply_multiple_comparison 替换为 Bonferroni 后的 p
      3. Δprecision ≥ 0.05（精度提升，避免靠 recall 崩塌赢 acc）
      4. V2 recall ≥ V1 recall × RECALL_FLOOR_RATIO (=0.85)（recall 不崩塌）
      5. 校准曲线弱单调（高分桶 hit_rate ≥ 低分桶；breakout_stage 维度跳过此项）

    返回：(is_better, reasons[])；reasons 列出每项判定的"通过 ✓"或"未通过 ✗"
    """
    reasons: list[str] = []

    cond_n = sample_size >= MIN_SAMPLES_TRUSTED
    reasons.append(
        f"{'✓' if cond_n else '✗'} 样本量 n={sample_size} ≥ {MIN_SAMPLES_TRUSTED}"
    )

    cond_p = mcnemar_p < P_VALUE_SIGNIFICANT
    reasons.append(
        f"{'✓' if cond_p else '✗'} {p_label}={mcnemar_p:.4f} < {P_VALUE_SIGNIFICANT}"
    )

    delta_prec = c_v2.precision - c_v1.precision
    cond_prec = delta_prec >= 0.05
    reasons.append(
        f"{'✓' if cond_prec else '✗'} Δprecision={delta_prec:+.4f} ≥ 0.05"
    )

    if c_v1.recall <= 0:
        # V1 recall=0 时，只要 V2 recall 不下降就算通过
        cond_rec = c_v2.recall >= 0
        rec_text = f"V1 recall=0；V2 recall={c_v2.recall:.4f}"
    else:
        floor = c_v1.recall * RECALL_FLOOR_RATIO
        cond_rec = c_v2.recall >= floor
        rec_text = (
            f"V2 recall={c_v2.recall:.4f} ≥ V1×{RECALL_FLOOR_RATIO}"
            f"={floor:.4f}"
        )
    reasons.append(f"{'✓' if cond_rec else '✗'} {rec_text}")

    cond_calib = calibration_monotonic
    reasons.append(
        f"{'✓' if cond_calib else '⚠'} 校准曲线"
        f"{'单调' if cond_calib else '不单调或不适用'}"
    )

    # 校准不单调仅警告，不直接否决（breakout_stage 维度无校准）
    is_better = cond_n and cond_p and cond_prec and cond_rec
    return is_better, reasons


def _filter(
    records: list[OutcomeRecord],
    *,
    tier_filter: Optional[list[str]] = None,
    regime_filter: Optional[list[str]] = None,
    state_filter: Optional[list[str]] = None,
) -> list[OutcomeRecord]:
    """统一过滤器：tier / regime / state 三维度。"""
    out = records
    if tier_filter:
        s = set(tier_filter)
        out = [r for r in out if r.strength_tier in s]
    if regime_filter:
        s = set(regime_filter)
        out = [r for r in out if r.regime in s]
    if state_filter:
        s = set(state_filter)
        out = [r for r in out if r.state in s]
    return out


def _build_comparison_stats(
    dimension: str,
    valid: list[OutcomeRecord],
    ambiguous_count: int,
    *,
    truth_picker,                 # callable(rec) -> bool（真相是否为正样本）
    v1_pos_picker,                # callable(rec) -> bool（V1 是否预测正样本）
    v2_pos_picker,                # callable(rec) -> bool（V2 是否预测正样本）
    v2_score_records: list[OutcomeRecord],  # 校准用全样本（含 ambiguous 剔除前）
) -> ComparisonStats:
    """三个维度共用的 ComparisonStats 构造器（M3.1 升级）。

    职责：
      - 构造 V1/V2 混淆矩阵
      - 计算配对差异（discordant b/c）→ McNemar
      - Wilson 95% CI
      - 卡方（仅作参考）
      - 分桶校准 + 单调性
      - 多条件决策
    """
    c1, c2 = ConfusionMatrix(), ConfusionMatrix()
    discordant_b = 0  # V1 错 而 V2 对
    discordant_c = 0  # V1 对 而 V2 错

    for r in valid:
        truth_pos = truth_picker(r)
        v1_pos = v1_pos_picker(r)
        v2_pos = v2_pos_picker(r)
        v1_correct = (v1_pos == truth_pos)
        v2_correct = (v2_pos == truth_pos)

        if v1_pos and truth_pos: c1.tp += 1
        elif v1_pos and not truth_pos: c1.fp += 1
        elif (not v1_pos) and truth_pos: c1.fn += 1
        else: c1.tn += 1

        if v2_pos and truth_pos: c2.tp += 1
        elif v2_pos and not truth_pos: c2.fp += 1
        elif (not v2_pos) and truth_pos: c2.fn += 1
        else: c2.tn += 1

        if v1_correct and not v2_correct:
            discordant_c += 1
        elif (not v1_correct) and v2_correct:
            discordant_b += 1

    chi_sq, chi_p = _chi_square_2x2_p(c1, c2)
    mcnemar_stat, mcnemar_p = mcnemar_test(discordant_b, discordant_c)
    ci_v1 = wilson_ci(c1.tp + c1.tn, c1.total)
    ci_v2 = wilson_ci(c2.tp + c2.tn, c2.total)

    calibration = compute_calibration_buckets(v2_score_records, dimension=dimension)
    cal_monotonic = _is_calibration_monotonic(calibration) if calibration else True
    # breakout_stage 无连续校准 → 不参与单调性判定，treated as 通过
    is_better, reasons = _evaluate_decision(
        c1, c2, mcnemar_p, len(valid), cal_monotonic,
    )

    return ComparisonStats(
        dimension=dimension,
        sample_size=len(valid),
        ambiguous_count=ambiguous_count,
        confusion_v1=c1,
        confusion_v2=c2,
        delta_accuracy=c2.accuracy - c1.accuracy,
        delta_precision=c2.precision - c1.precision,
        delta_recall=c2.recall - c1.recall,
        delta_f1=c2.f1 - c1.f1,
        delta_balanced_accuracy=c2.balanced_accuracy - c1.balanced_accuracy,
        delta_mcc=c2.mcc - c1.mcc,
        chi_square_stat=chi_sq,
        chi_square_p_value=chi_p,
        discordant_v1_wrong_v2_right=discordant_b,
        discordant_v1_right_v2_wrong=discordant_c,
        mcnemar_stat=mcnemar_stat,
        mcnemar_p_value=mcnemar_p,
        # family_size / mcnemar_p_bonferroni / mcnemar_p_fdr 由 run_full_comparison
        # 在 3 个维度都算完后回写；这里先把默认值设为原始 p，确保单维度调用也有合理值
        family_size=1,
        mcnemar_p_bonferroni=mcnemar_p,
        mcnemar_p_fdr=mcnemar_p,
        accuracy_ci_v1=ci_v1,
        accuracy_ci_v2=ci_v2,
        is_v2_significantly_better=is_better,
        decision_reasons=reasons,
        calibration_v2=calibration,
        calibration_monotonic=cal_monotonic,
    )


def compute_bounce_quality_stats(
    records: list[OutcomeRecord],
    *,
    v2_threshold: float = DEFAULT_V2_BINARY_THRESHOLD,
    tier_filter: Optional[list[str]] = None,
    regime_filter: Optional[list[str]] = None,
    state_filter: Optional[list[str]] = None,
) -> ComparisonStats:
    """反弹质量维度：V1 (proactive/passive) vs V2 (≥ threshold)。

    样本范围：state == "bounced" 的 OutcomeRecord（M3.1 仍按 state 锚定，
    state_filter 仅在用户显式指定时进一步缩小范围）
    真相：bounce_truth ∈ {"real_bounce", "weak_bounce"}（ambiguous 剔除）
    """
    rs = _filter(records, tier_filter=tier_filter,
                 regime_filter=regime_filter, state_filter=state_filter)
    rs = [r for r in rs if r.state == "bounced" and r.bounce_truth is not None]
    valid = [r for r in rs if r.bounce_truth != "ambiguous"]
    ambiguous = len(rs) - len(valid)

    return _build_comparison_stats(
        "bounce_quality", valid, ambiguous,
        truth_picker=lambda r: r.bounce_truth == "real_bounce",
        v1_pos_picker=lambda r: r.v1_bounce_quality == "proactive",
        v2_pos_picker=lambda r: r.v2_bounce_quality_enhanced >= v2_threshold,
        v2_score_records=valid,
    )


def compute_breakout_stage_stats(
    records: list[OutcomeRecord],
    *,
    stage_threshold: int = 3,
    tier_filter: Optional[list[str]] = None,
    regime_filter: Optional[list[str]] = None,
    state_filter: Optional[list[str]] = None,
) -> ComparisonStats:
    """突破阶段维度：V1 (stage>=3) vs V2 (stage>=3)。

    样本范围：state ∈ {broken, flipped} 的 OutcomeRecord
    真相：breakout_truth ∈ {"true_breakout", "failed_breakout"}（ambiguous 剔除）
    """
    rs = _filter(records, tier_filter=tier_filter,
                 regime_filter=regime_filter, state_filter=state_filter)
    rs = [r for r in rs if r.v1_state_is_broken and r.breakout_truth is not None]
    valid = [r for r in rs if r.breakout_truth != "ambiguous"]
    ambiguous = len(rs) - len(valid)

    return _build_comparison_stats(
        "breakout_stage", valid, ambiguous,
        truth_picker=lambda r: r.breakout_truth == "true_breakout",
        v1_pos_picker=lambda r: r.v1_breakout_stage >= stage_threshold,
        v2_pos_picker=lambda r: r.v2_breakout_stage_enhanced >= stage_threshold,
        v2_score_records=valid,
    )


def compute_fake_break_stats(
    records: list[OutcomeRecord],
    *,
    v2_threshold: float = DEFAULT_V2_BINARY_THRESHOLD,
    tier_filter: Optional[list[str]] = None,
    regime_filter: Optional[list[str]] = None,
    state_filter: Optional[list[str]] = None,
) -> ComparisonStats:
    """假破回收维度：V1 (state==fake_break 布尔) vs V2 (≥ threshold)。

    样本范围：state ∈ {broken, flipped, fake_break} 的 OutcomeRecord
    真相：fake_break_truth ∈ {"confirmed_fake", "true_break"}（ambiguous 剔除）
    """
    rs = _filter(records, tier_filter=tier_filter,
                 regime_filter=regime_filter, state_filter=state_filter)
    rs = [r for r in rs if r.fake_break_truth is not None]
    valid = [r for r in rs if r.fake_break_truth != "ambiguous"]
    ambiguous = len(rs) - len(valid)

    return _build_comparison_stats(
        "fake_break", valid, ambiguous,
        truth_picker=lambda r: r.fake_break_truth == "confirmed_fake",
        v1_pos_picker=lambda r: r.v1_state_is_fake_break,
        v2_pos_picker=lambda r: r.v2_fake_break_strength >= v2_threshold,
        v2_score_records=valid,
    )


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 顶层入口（CLI / API 使用）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def run_full_comparison(
    history: list[KeyLevelSnapshotV2],
    *,
    coin: str,
    future_window_sec: int = DEFAULT_FUTURE_WINDOW_SEC,
    tolerance_sec: int = 600,
    truth_atr_mult: float = DEFAULT_TRUTH_ATR_MULT,
    ambiguous_band: float = DEFAULT_AMBIGUOUS_ATR_BAND,
    v2_threshold: float = DEFAULT_V2_BINARY_THRESHOLD,
    breakout_stage_threshold: int = 3,
    tier_filter: Optional[list[str]] = None,
    regime_filter: Optional[list[str]] = None,
    state_filter: Optional[list[str]] = None,
    deduplicate_events: bool = True,
    require_behavior_eval: bool = True,
    timeframe_adaptive_window: bool = True,
) -> dict:
    """端到端运行：从历史快照 → records → 三维度对比 → 摘要 dict。

    M3.1 升级（GPT 审查采纳）：
      - McNemar 检验（配对样本主指标）
      - Wilson 95% CI
      - 分桶校准 + 单调性
      - 多条件决策（McNemar + Δprecision + recall 不崩塌 + n≥100）
      - 事件去重（默认 True）
      - 元信息过滤（默认 True，跳过 behavior_eval_available=False 的样本）
      - regime / state 过滤

    V3-M4 升级：
      - P1-1：3 维度构成检验族，做 Bonferroni / FDR 修正后再判决策
      - P1-2：future_window 按 lv.timeframe 自适应（1D=24h+ / 1W=1 周+），
        让长周期 level 不被短窗口"过早盖戳"
    """
    records = build_outcome_records(
        history,
        coin=coin,
        future_window_sec=future_window_sec,
        tolerance_sec=tolerance_sec,
        truth_atr_mult=truth_atr_mult,
        ambiguous_band=ambiguous_band,
        deduplicate=deduplicate_events,
        require_behavior_eval=require_behavior_eval,
        timeframe_adaptive_window=timeframe_adaptive_window,
    )

    bq = compute_bounce_quality_stats(
        records, v2_threshold=v2_threshold,
        tier_filter=tier_filter, regime_filter=regime_filter, state_filter=state_filter,
    )
    bs = compute_breakout_stage_stats(
        records, stage_threshold=breakout_stage_threshold,
        tier_filter=tier_filter, regime_filter=regime_filter, state_filter=state_filter,
    )
    fb = compute_fake_break_stats(
        records, v2_threshold=v2_threshold,
        tier_filter=tier_filter, regime_filter=regime_filter, state_filter=state_filter,
    )

    # V3-M4 P1-1：3 个维度构成"检验族"，做 Bonferroni / FDR 修正后重判决策
    _apply_multiple_comparison([bq, bs, fb])

    return {
        "coin": coin,
        "params": {
            "future_window_sec": future_window_sec,
            "tolerance_sec": tolerance_sec,
            "truth_atr_mult": truth_atr_mult,
            "ambiguous_band": ambiguous_band,
            "v2_threshold": v2_threshold,
            "breakout_stage_threshold": breakout_stage_threshold,
            "deduplicate_events": deduplicate_events,
            "require_behavior_eval": require_behavior_eval,
            "timeframe_adaptive_window": timeframe_adaptive_window,
            "min_samples_trusted": MIN_SAMPLES_TRUSTED,
            "min_samples_observe": MIN_SAMPLES_OBSERVE,
            "recall_floor_ratio": RECALL_FLOOR_RATIO,
        },
        "total_records": len(records),
        "tier_filter": list(tier_filter) if tier_filter else [],
        "regime_filter": list(regime_filter) if regime_filter else [],
        "state_filter": list(state_filter) if state_filter else [],
        "stats": {
            "bounce_quality": bq.to_dict(),
            "breakout_stage": bs.to_dict(),
            "fake_break": fb.to_dict(),
        },
    }


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# V3-M4 P0-3 · 滚动统计（rolling stats）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 设计：
#   按 step_hours 步进生成 anchor_ts；每个 anchor 取
#   (anchor_ts - window_days × 86400, anchor_ts] 内的快照子集，
#   调用 run_full_comparison，提取关键指标做时间序列展示。
#
# 输出指标（每个 anchor 一行 / 每个维度一行）：
#   - sample_size, mcnemar_p_bonferroni, delta_precision, delta_recall
#   - is_v2_significantly_better（是否当前满足切换门槛）
#   - 用于 v1v2-rolling API + 前端 14 天折线
#
# 性能：
#   - 内存计算（不持久化）；大 history 时较慢（O(anchors × records)）
#   - 由调用方加缓存（API 层 5min TTL）；CLI 可直接调用

def compute_rolling_comparison(
    history: list[KeyLevelSnapshotV2],
    *,
    coin: str,
    window_days: int = 7,
    step_hours: int = 24,
    max_anchors: int = 14,
    end_ts: Optional[int] = None,
    future_window_sec: int = DEFAULT_FUTURE_WINDOW_SEC,
    tolerance_sec: int = 600,
    truth_atr_mult: float = DEFAULT_TRUTH_ATR_MULT,
    ambiguous_band: float = DEFAULT_AMBIGUOUS_ATR_BAND,
    v2_threshold: float = DEFAULT_V2_BINARY_THRESHOLD,
    breakout_stage_threshold: int = 3,
    timeframe_adaptive_window: bool = True,
    deduplicate_events: bool = True,
    require_behavior_eval: bool = True,
) -> dict:
    """生成 N 个时间锚点的 V1/V2 对比指标时间序列。

    Args:
      history: 历史快照
      window_days: 每个 anchor 回看几天的数据（默认 7 天）
      step_hours: anchor 之间的步长（默认 24h，即每天一锚）
      max_anchors: 最多生成多少个 anchor（默认 14，覆盖 ~14 天）
      end_ts: 最右锚点的时间戳（默认 = max(history.ts)）

    Returns:
      {
        "coin": "BTC",
        "params": {...},
        "anchors": [
          {"anchor_ts": ..., "sample_size": 80,
           "bounce_quality": {sample_size, mcnemar_p_bonferroni, delta_precision, ...},
           "breakout_stage": {...},
           "fake_break": {...}},
          ...
        ]
      }
    """
    if not history:
        return {
            "coin": coin,
            "params": {
                "window_days": window_days,
                "step_hours": step_hours,
                "max_anchors": max_anchors,
            },
            "anchors": [],
        }

    ordered = sorted(history, key=lambda h: h.ts)
    actual_end_ts = end_ts if end_ts is not None else ordered[-1].ts
    step_sec = step_hours * 3600
    window_sec = window_days * 86400

    anchors: list[dict] = []
    for i in range(max_anchors):
        anchor_ts = actual_end_ts - i * step_sec
        sub_start = anchor_ts - window_sec
        sub_history = [s for s in ordered if sub_start < s.ts <= anchor_ts]
        if len(sub_history) < 2:
            # 至少需要 2 个快照才能配对
            continue
        try:
            result = run_full_comparison(
                sub_history, coin=coin,
                future_window_sec=future_window_sec,
                tolerance_sec=tolerance_sec,
                truth_atr_mult=truth_atr_mult,
                ambiguous_band=ambiguous_band,
                v2_threshold=v2_threshold,
                breakout_stage_threshold=breakout_stage_threshold,
                deduplicate_events=deduplicate_events,
                require_behavior_eval=require_behavior_eval,
                timeframe_adaptive_window=timeframe_adaptive_window,
            )
        except Exception:
            # 单 anchor 异常不应阻断整个 series
            continue

        def _slim(s: dict) -> dict:
            """提取折线展示所需的关键指标，丢弃完整混淆矩阵以减小 payload。"""
            return {
                "sample_size": s.get("sample_size", 0),
                "mcnemar_p_value": s.get("mcnemar_p_value", 1.0),
                "mcnemar_p_bonferroni": s.get("mcnemar_p_bonferroni", 1.0),
                "delta_precision": s.get("delta_precision", 0.0),
                "delta_recall": s.get("delta_recall", 0.0),
                "delta_accuracy": s.get("delta_accuracy", 0.0),
                "delta_balanced_accuracy": s.get("delta_balanced_accuracy", 0.0),
                "is_v2_significantly_better": s.get("is_v2_significantly_better", False),
                "calibration_monotonic": s.get("calibration_monotonic", False),
            }

        anchors.append({
            "anchor_ts": anchor_ts,
            "window_start_ts": sub_start,
            "snapshot_count": len(sub_history),
            "total_records": result.get("total_records", 0),
            "bounce_quality": _slim(result["stats"]["bounce_quality"]),
            "breakout_stage": _slim(result["stats"]["breakout_stage"]),
            "fake_break": _slim(result["stats"]["fake_break"]),
        })

    # 反转：旧→新排列，方便前端按时间正序绘折线
    anchors.reverse()

    return {
        "coin": coin,
        "params": {
            "window_days": window_days,
            "step_hours": step_hours,
            "max_anchors": max_anchors,
            "end_ts": actual_end_ts,
            "future_window_sec": future_window_sec,
            "timeframe_adaptive_window": timeframe_adaptive_window,
        },
        "anchors": anchors,
    }
