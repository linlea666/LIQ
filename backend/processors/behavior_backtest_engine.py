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

DEFAULT_FUTURE_WINDOW_SEC = 4 * 3600         # 4h 后判真相
DEFAULT_TRUTH_ATR_MULT = 1.0                 # 偏离 1×ATR 算"显著"
DEFAULT_AMBIGUOUS_ATR_BAND = 0.3             # ≤ 0.3×ATR 算"未动" → ambiguous
DEFAULT_V2_BINARY_THRESHOLD = 0.5            # V2 0-1 → 二分类阈值


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
    timeframe: str

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

    def to_dict(self) -> dict:
        return {
            "tp": self.tp, "fp": self.fp, "tn": self.tn, "fn": self.fn,
            "accuracy": round(self.accuracy, 4),
            "precision": round(self.precision, 4),
            "recall": round(self.recall, 4),
            "f1": round(self.f1, 4),
        }


@dataclass
class ComparisonStats:
    """V1 vs V2 单维度对比指标。"""
    dimension: str  # "bounce_quality" / "breakout_stage" / "fake_break"
    sample_size: int
    ambiguous_count: int  # 因真相不可判定被剔除的样本数（透明度）

    confusion_v1: ConfusionMatrix = field(default_factory=ConfusionMatrix)
    confusion_v2: ConfusionMatrix = field(default_factory=ConfusionMatrix)

    delta_accuracy: float = 0.0      # v2.accuracy - v1.accuracy
    delta_f1: float = 0.0
    chi_square_stat: float = 0.0
    chi_square_p_value: float = 1.0  # 1.0 = 完全无显著差异
    is_v2_significantly_better: bool = False

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "sample_size": self.sample_size,
            "ambiguous_count": self.ambiguous_count,
            "v1": self.confusion_v1.to_dict(),
            "v2": self.confusion_v2.to_dict(),
            "delta_accuracy": round(self.delta_accuracy, 4),
            "delta_f1": round(self.delta_f1, 4),
            "chi_square_stat": round(self.chi_square_stat, 4),
            "chi_square_p_value": round(self.chi_square_p_value, 4),
            "is_v2_significantly_better": self.is_v2_significantly_better,
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
) -> list[OutcomeRecord]:
    """从一组历史快照生成 OutcomeRecord。

    流程：
      1. history 按 ts 升序排序
      2. 对每个 (base_snap, future_snap) 配对，按 level_id 匹配 lv
      3. 调用 3 个 evaluate_* 函数生成真相
      4. 过滤无 level_id / 无 ATR / 无配对的样本

    设计选择：
      - level_id 是配对依据（M3 R9 已稳定）；缺 level_id 的旧样本被跳过
      - 一个 level 在多次快照中产生多条记录（每个 base_snap 都尝试配对）
      - 不去重：分析侧需要时间序列，重复评估同一 level 反映了它的"持续可信度"
    """
    if not history:
        return []
    ordered = sorted(history, key=lambda h: h.ts)
    records: list[OutcomeRecord] = []

    for i, base in enumerate(ordered):
        future = _find_future_snapshot(ordered, i, future_window_sec, tolerance_sec)
        if future is None:
            continue
        # 用 future.current_price 作为"真相价格"；future.atr 作为评估时刻基准
        future_price = future.current_price
        future_atr = future.atr if future.atr > 0 else base.atr
        if future_price <= 0 or future_atr <= 0:
            continue

        for lv in base.levels:
            if not lv.level_id:
                continue
            beh = lv.behavior  # M2.5 写入；旧快照可能为 None

            dist_atr = _signed_distance_atr(lv.price, future_price, lv.side, future_atr)

            rec = OutcomeRecord(
                coin=coin,
                snapshot_ts=base.ts,
                future_ts=future.ts,
                level_id=lv.level_id,
                level_price=lv.price,
                level_side=lv.side,
                strength_tier=lv.strength_tier or "C",
                state=lv.state,
                timeframe=lv.timeframe or "",
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
# 卡方检验（不引入 scipy 依赖，2x2 列联表手算）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def _chi_square_2x2_p(c1: ConfusionMatrix, c2: ConfusionMatrix) -> tuple[float, float]:
    """对比两组 (correct vs wrong) 是否独立，2x2 卡方（Yates 连续性校正）。

    返回 (chi_square_stat, p_value)。
    df=1 时用近似 p_value: 利用 erfc 关系；不依赖 scipy。

    列联表：
              correct   wrong
       v1     a         b      a+b
       v2     c         d      c+d
              a+c       b+d    n
    """
    a = c1.tp + c1.tn
    b = c1.fp + c1.fn
    c = c2.tp + c2.tn
    d = c2.fp + c2.fn
    n = a + b + c + d
    if n < 10 or (a + c) == 0 or (b + d) == 0 or (a + b) == 0 or (c + d) == 0:
        # 样本太少 / 边缘和为 0 → 不可信
        return 0.0, 1.0

    # Yates 连续性校正：|ad - bc| - n/2
    numerator = abs(a * d - b * c) - n / 2.0
    if numerator <= 0:
        return 0.0, 1.0
    chi_sq = (n * numerator * numerator) / ((a + b) * (c + d) * (a + c) * (b + d))

    # df=1 chi-square → p = erfc(sqrt(chi/2))
    p = math.erfc(math.sqrt(chi_sq / 2.0))
    return chi_sq, p


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# 三个维度的 V1/V2 命中率计算
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# 显著性阈值
P_VALUE_SIGNIFICANT = 0.05


def _is_better(c_v1: ConfusionMatrix, c_v2: ConfusionMatrix, p: float) -> bool:
    """V2 显著优于 V1 的判定：accuracy 提升 ≥ 0.05 且 p < 0.05。"""
    delta_acc = c_v2.accuracy - c_v1.accuracy
    return delta_acc >= 0.05 and p < P_VALUE_SIGNIFICANT


def _filter(
    records: list[OutcomeRecord],
    *,
    tier_filter: Optional[list[str]] = None,
) -> list[OutcomeRecord]:
    if not tier_filter:
        return records
    s = set(tier_filter)
    return [r for r in records if r.strength_tier in s]


def compute_bounce_quality_stats(
    records: list[OutcomeRecord],
    *,
    v2_threshold: float = DEFAULT_V2_BINARY_THRESHOLD,
    tier_filter: Optional[list[str]] = None,
) -> ComparisonStats:
    """反弹质量维度：V1 (proactive vs passive) vs V2 (≥ threshold)。

    样本范围：state == "bounced" 的 OutcomeRecord（即两端都有有效预测）
    真相：bounce_truth ∈ {"real_bounce", "weak_bounce"}（ambiguous 剔除）

    二分类约定：
      - V1: bounce_quality == "proactive" → 预测正样本（"会真反弹"）
      - V2: bounce_quality_enhanced >= threshold → 预测正样本
      - 真相：bounce_truth == "real_bounce" → 真值正样本
    """
    rs = _filter(records, tier_filter=tier_filter)
    rs = [r for r in rs if r.state == "bounced" and r.bounce_truth is not None]
    valid = [r for r in rs if r.bounce_truth != "ambiguous"]
    ambiguous = len(rs) - len(valid)

    c1, c2 = ConfusionMatrix(), ConfusionMatrix()
    for r in valid:
        truth_pos = r.bounce_truth == "real_bounce"
        v1_pos = r.v1_bounce_quality == "proactive"
        v2_pos = r.v2_bounce_quality_enhanced >= v2_threshold

        if v1_pos and truth_pos: c1.tp += 1
        elif v1_pos and not truth_pos: c1.fp += 1
        elif (not v1_pos) and truth_pos: c1.fn += 1
        else: c1.tn += 1

        if v2_pos and truth_pos: c2.tp += 1
        elif v2_pos and not truth_pos: c2.fp += 1
        elif (not v2_pos) and truth_pos: c2.fn += 1
        else: c2.tn += 1

    chi_sq, p = _chi_square_2x2_p(c1, c2)
    return ComparisonStats(
        dimension="bounce_quality",
        sample_size=len(valid),
        ambiguous_count=ambiguous,
        confusion_v1=c1,
        confusion_v2=c2,
        delta_accuracy=c2.accuracy - c1.accuracy,
        delta_f1=c2.f1 - c1.f1,
        chi_square_stat=chi_sq,
        chi_square_p_value=p,
        is_v2_significantly_better=_is_better(c1, c2, p),
    )


def compute_breakout_stage_stats(
    records: list[OutcomeRecord],
    *,
    stage_threshold: int = 3,
    tier_filter: Optional[list[str]] = None,
) -> ComparisonStats:
    """突破阶段维度：V1 (stage>=3) vs V2 (stage>=3)。

    样本范围：state ∈ {broken, flipped} 的 OutcomeRecord
    真相：breakout_truth ∈ {"true_breakout", "failed_breakout"}（ambiguous 剔除）

    二分类约定：
      - V1: breakout_stage >= stage_threshold → 预测"已确认真破"
      - V2: breakout_stage_enhanced >= stage_threshold → 预测"已确认真破"
      - 真相：breakout_truth == "true_breakout" → 真值正样本
    """
    rs = _filter(records, tier_filter=tier_filter)
    rs = [r for r in rs if r.v1_state_is_broken and r.breakout_truth is not None]
    valid = [r for r in rs if r.breakout_truth != "ambiguous"]
    ambiguous = len(rs) - len(valid)

    c1, c2 = ConfusionMatrix(), ConfusionMatrix()
    for r in valid:
        truth_pos = r.breakout_truth == "true_breakout"
        v1_pos = r.v1_breakout_stage >= stage_threshold
        v2_pos = r.v2_breakout_stage_enhanced >= stage_threshold

        if v1_pos and truth_pos: c1.tp += 1
        elif v1_pos and not truth_pos: c1.fp += 1
        elif (not v1_pos) and truth_pos: c1.fn += 1
        else: c1.tn += 1

        if v2_pos and truth_pos: c2.tp += 1
        elif v2_pos and not truth_pos: c2.fp += 1
        elif (not v2_pos) and truth_pos: c2.fn += 1
        else: c2.tn += 1

    chi_sq, p = _chi_square_2x2_p(c1, c2)
    return ComparisonStats(
        dimension="breakout_stage",
        sample_size=len(valid),
        ambiguous_count=ambiguous,
        confusion_v1=c1,
        confusion_v2=c2,
        delta_accuracy=c2.accuracy - c1.accuracy,
        delta_f1=c2.f1 - c1.f1,
        chi_square_stat=chi_sq,
        chi_square_p_value=p,
        is_v2_significantly_better=_is_better(c1, c2, p),
    )


def compute_fake_break_stats(
    records: list[OutcomeRecord],
    *,
    v2_threshold: float = DEFAULT_V2_BINARY_THRESHOLD,
    tier_filter: Optional[list[str]] = None,
) -> ComparisonStats:
    """假破回收维度：V1 (state==fake_break 布尔) vs V2 (fake_break_strength >= threshold)。

    样本范围：state ∈ {broken, flipped, fake_break} 的 OutcomeRecord
    真相：fake_break_truth ∈ {"confirmed_fake", "true_break"}（ambiguous 剔除）

    二分类约定：
      - V1: state == "fake_break" → 预测"假破"
      - V2: fake_break_strength >= threshold → 预测"假破"
      - 真相：fake_break_truth == "confirmed_fake" → 真值正样本
    """
    rs = _filter(records, tier_filter=tier_filter)
    rs = [r for r in rs if r.fake_break_truth is not None]
    valid = [r for r in rs if r.fake_break_truth != "ambiguous"]
    ambiguous = len(rs) - len(valid)

    c1, c2 = ConfusionMatrix(), ConfusionMatrix()
    for r in valid:
        truth_pos = r.fake_break_truth == "confirmed_fake"
        v1_pos = r.v1_state_is_fake_break
        v2_pos = r.v2_fake_break_strength >= v2_threshold

        if v1_pos and truth_pos: c1.tp += 1
        elif v1_pos and not truth_pos: c1.fp += 1
        elif (not v1_pos) and truth_pos: c1.fn += 1
        else: c1.tn += 1

        if v2_pos and truth_pos: c2.tp += 1
        elif v2_pos and not truth_pos: c2.fp += 1
        elif (not v2_pos) and truth_pos: c2.fn += 1
        else: c2.tn += 1

    chi_sq, p = _chi_square_2x2_p(c1, c2)
    return ComparisonStats(
        dimension="fake_break",
        sample_size=len(valid),
        ambiguous_count=ambiguous,
        confusion_v1=c1,
        confusion_v2=c2,
        delta_accuracy=c2.accuracy - c1.accuracy,
        delta_f1=c2.f1 - c1.f1,
        chi_square_stat=chi_sq,
        chi_square_p_value=p,
        is_v2_significantly_better=_is_better(c1, c2, p),
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
) -> dict:
    """端到端运行：从历史快照 → records → 三维度对比 → 摘要 dict。

    返回结构（API / CLI 直接消费）：
        {
          "coin": "BTC",
          "params": {...},          # 用了哪些阈值（透明度）
          "total_records": int,
          "tier_filter": [...],
          "stats": {
              "bounce_quality":  {...},  # ComparisonStats.to_dict()
              "breakout_stage":  {...},
              "fake_break":      {...},
          },
        }
    """
    records = build_outcome_records(
        history,
        coin=coin,
        future_window_sec=future_window_sec,
        tolerance_sec=tolerance_sec,
        truth_atr_mult=truth_atr_mult,
        ambiguous_band=ambiguous_band,
    )

    bq = compute_bounce_quality_stats(
        records, v2_threshold=v2_threshold, tier_filter=tier_filter,
    )
    bs = compute_breakout_stage_stats(
        records, stage_threshold=breakout_stage_threshold, tier_filter=tier_filter,
    )
    fb = compute_fake_break_stats(
        records, v2_threshold=v2_threshold, tier_filter=tier_filter,
    )

    return {
        "coin": coin,
        "params": {
            "future_window_sec": future_window_sec,
            "tolerance_sec": tolerance_sec,
            "truth_atr_mult": truth_atr_mult,
            "ambiguous_band": ambiguous_band,
            "v2_threshold": v2_threshold,
            "breakout_stage_threshold": breakout_stage_threshold,
        },
        "total_records": len(records),
        "tier_filter": list(tier_filter) if tier_filter else [],
        "stats": {
            "bounce_quality": bq.to_dict(),
            "breakout_stage": bs.to_dict(),
            "fake_break": fb.to_dict(),
        },
    }
