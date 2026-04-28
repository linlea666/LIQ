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
    invalidation_condition: str = ""     # 中文条件描述（"15m 收盘 < $63,000"，对齐状态机口径）
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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M2（V3 评分体系核心升级）— 独立证据组 + S 4 分型 + 矛盾扣分 + cascade 4 子分
    # 全部 Optional/默认值，向后兼容（旧 snapshot 反序列化无破坏）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 独立证据组（8 组之一或多个）— 由 _score_cluster 从 RawCandidate.evidence_group
    # 聚合而来，决定独立组数 (count_independent_groups)
    # 8 组：structure_anchor / macro_technical / local_technical /
    #       liquidation_macro / liquidation_meso / liquidation_short /
    #       microstructure_local / flow_dynamic
    evidence_groups: list[str] = Field(default_factory=list)
    independent_group_count: int = 0     # 去重后的组数；A/B/C 评级核心因子

    # S 级 4 分型（GPT V3 评审采纳）— 仅 strength_tier=='S' 时填充
    # 实际门槛见 confluence_scoring.classify_s_level()，此处为语义说明：
    # S-Liquidity: 跨所共振强清算簇（exchange_count ≥ 4 且 liquidation_* 组 ≥ 2）
    # S-Macro:     长周期独占（macro_technical / liquidation_macro，且 evidence_groups ≥ 2，无微结构）
    # S-Micro:     盘口微结构 + flow 共振（microstructure_local + flow_dynamic，无宏观）
    # S-Composite: 兜底（≥3 独立组但不属上述任一类）
    # 优先级：Liquidity > Macro > Micro > Composite
    s_class: str = ""

    # 矛盾扣分（contradiction_penalty）— 6 类一票否决式扣分
    # 在 _calc_final_score 末段统一应用：final_score -= contradiction_penalty
    contradiction_penalty: float = 0.0
    contradiction_reasons: list[str] = Field(default_factory=list)

    # cascade 4 子分（M1 cascade_risk 单值 → M2 拆解可解释）
    cascade_components: Optional["CascadeComponents"] = None

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M3（V3 架构精装）— level_id + lifecycle_events + regime-aware scoring
    # 全部 Optional/默认值，向后兼容（旧 snapshot 反序列化无破坏）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # R9：稳定 level_id（基于 ATR/price bucket 的 sha1[:12]）
    # 设计：side 翻转时 level_id 保持不变（只记录 flipped 事件），跨币种内唯一
    # 用于 lifecycle 追踪 + diff API 关联同一关键位的不同时刻
    level_id: str = ""

    # R9：生命周期事件流（最多保留 20 条最近事件）
    # 事件类型：born / strengthening / weakening / tier_upgraded / tier_downgraded
    #          / tested / reacted / broken / fake_break / flipped / expired
    # 由 confluence_scoring._diff_lifecycle 在每轮快照生成时 diff 推入
    # 由 key_level_tracker_v2._set_state 在状态变化时追加
    lifecycle_events: list["LifecycleEvent"] = Field(default_factory=list)

    # R8：regime-aware scoring（市场状态自适应权重）
    # 在 score_and_build_snapshot 末段、tier 判定之前应用：
    #   final_score *= regime_modifier_applied
    # 取值范围 [0.85, 1.10]，按 evidence_groups 主组在 6×8 表查询
    # regime_at_score：本次评分时的 regime 标签（用于 NOFX/前端审计）
    regime_modifier_applied: float = 1.0
    regime_at_score: str = ""  # "" / "trend_up" / "trend_down" / "range" / "squeeze" / "high_vol_chop" / "extreme"
    regime_weight_version: str = ""  # "3.0" 表示 M3 权重版本

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M4-行为评估层（V3 行为验证引擎 · 2026-04 新增）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 设计纪律（与 BehaviorEval docstring 同步）：
    #   1. 由独立模块 key_level_behavior_eval.evaluate_behavior 写入
    #   2. 不抢 state machine 决定权（lv.state 仍由 _transition 唯一负责）
    #   3. 不修改 final_score / strength_tier / cascade_risk
    #   4. M1 阶段为纯观测：不参与 signal 生成、不进 AI prompt
    #   5. 旧 snapshot 反序列化无破坏（默认 None）
    behavior: Optional["BehaviorEval"] = None


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M3 新增：生命周期事件（关键位演化追踪）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class LifecycleEvent(BaseModel):
    """关键位生命周期单条事件（M3 · GPT V3 评审采纳）。

    用途：
    - 让前端展示"该支撑过去 24h 的演化时间线"
    - 让外部 AI / NOFX 知道"哪些位刚增强 / 刚失效 / 刚翻转"
    - 配合 /api/key-levels/lifecycle/{coin}/{level_id} 实现历史追溯

    事件分类（11 种）：
      born              首次出现（prev_levels 中无匹配）
      strengthening     final_score 上涨 ≥ 5 分
      weakening         final_score 下跌 ≥ 5 分
      tier_upgraded     strength_tier 提升（C→B / B→A / A→S）
      tier_downgraded   strength_tier 下降
      tested            进入 testing 状态
      reacted           bounced / 反弹成功
      broken            被有效突破
      fake_break        假突破后重夺
      flipped           support↔resistance 翻转
      expired           不再出现在新快照（由 history 端点检测，不写入 lv 自身）
    """
    ts: int
    event_type: str
    detail: str = ""             # 中文白话说明
    score_before: float = 0.0
    score_after: float = 0.0
    tier_before: str = ""
    tier_after: str = ""
    state_before: str = ""
    state_after: str = ""
    # V3-P1-6：事件来源层（去重维度的第三个 key）
    # "scoring"  ← 由 confluence_scoring._detect_lifecycle_events 检测的"分数/分级/翻转"
    # "tracker"  ← 由 key_level_tracker_v2._set_state 检测的"状态机转移（tested/broken/...)"
    # 设为 ""（未知/历史）时退化为旧行为（仅按 ts+event_type 去重）
    layer: str = ""


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M4 新增：关键位行为评估（V3 行为验证引擎）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class BehaviorEval(BaseModel):
    """关键位行为评估（V3-M1 行为验证层 · 2026-04）。

    设计目标：
        旧 state machine 回答"事件是否发生"（broken / bounced / flipped 几何门）；
        本模块回答"这次事件多可信"（连续 0-1 分数 + 多因子）。
        两者职责互补，不竞争 state 决定权。

    设计纪律（违反即视为 bug）：
        1. 不写入 lv.state / lv.final_score / lv.strength_tier / lv.cascade_risk
        2. 由 key_level_behavior_eval.evaluate_behavior 在 state machine 后调用
        3. 输入仅读 KeyLevelV2 + 行情数据（candles/cvd/oi），不依赖 footprint/funding
           （M1 最小依赖；footprint/funding 留给 M2）
        4. M1 阶段不参与 signal 生成、不进 AI prompt（仅前端观测）
        5. 旧 snapshot 反序列化无破坏（KeyLevelV2.behavior 默认 None）

    四象限分数（每项 0-1，仅在合适的 state 下被填充，其它情况保持 0.0）：
        breakout_validity         真突破质量（state ∈ {broken, flipped}）
        retest_quality            回踩质量（state ∈ {bounced, flipped}）
        selloff_continuation_risk 放量破位延续风险（state == broken & side == support）
        capitulation_bottom_score 恐慌出清候选分（state == broken & side == support，且伴随极端放量+长下影）
        flip_confirmation         翻转确认度（state == flipped）
        false_break_risk          假突破风险（state ∈ {testing, broken}；与事件级 fake_break 互补）

    behavior_state（综合状态标签，9 选 1）：
        pending                独立计算无意义 / 数据不足
        pending_breakout       放量逼近，等收盘确认
        true_breakout          真突破
        healthy_retest         健康回踩
        failed_breakout        假突破 / 失败突破
        heavy_volume_breakdown 放量破位（继续下行风险高）
        capitulation_flush     恐慌出清候选（等二次确认，禁止直接做多）
        confirmed_flip         翻转确认
        wait_for_second_test   等二次测试

    state_confidence（0-1）：
        旧 state 的可信度。由当前 state 对应的核心分数派生：
          broken    → breakout_validity
          bounced   → retest_quality
          flipped   → flip_confirmation
          fake_break → 1 - false_break_risk（事件已成立时 false_break_risk = 1.0）
          其它     → 0.0（无意义）

    explain_chips：
        简短中文 chip，前端可叠加到 lv.explain_chips 之后单独成区展示。
        不污染原 explain_chips（避免覆盖 V3 已有解释）。
    """
    breakout_validity: float = 0.0
    retest_quality: float = 0.0
    selloff_continuation_risk: float = 0.0
    capitulation_bottom_score: float = 0.0
    flip_confirmation: float = 0.0
    false_break_risk: float = 0.0

    behavior_state: str = "pending"
    state_confidence: float = 0.0

    explain_chips: list[str] = Field(default_factory=list)

    # 数据完整性（哪些子因子参与了计算）
    components_used: list[str] = Field(default_factory=list)

    # 评估时间戳（用于前端"评估于 X 秒前"显示与回测对齐）
    evaluated_at: int = 0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M2.5（双轨并行 · 影子字段）— 旧 4 个 tracker 函数的 V2 增强版输出
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 设计：旧 _assess_bounce_quality / _assess_breakout_stage / _fake_break_reclaim
    #       / _is_broken 在 tracker 中保持不变（生产链路稳定）；这里同步计算"V2 增强版"
    #       结果作为影子字段，前端可对比新旧、AI 可同时参考、M3 回测可分桶验证。
    # 严格只读：影子字段写入后**不会**反向影响 lv.state / lv.bounce_quality / lv.breakout_stage。
    #
    # 字段对照：
    #   bounce_quality_enhanced     vs  lv.bounce_quality (string proactive/passive/"")
    #     V2: 用 z-score / percentile 自适应代替死阈值 1.5x；输出 0-1 连续分。
    #   breakout_stage_enhanced     vs  lv.breakout_stage (固定 0/1/2/3)
    #     V2: 时间窗按 lv.timeframe 自适应缩放（1D 用 24h，1W 用 1 周），让长周期位也能拿到 stage 3。
    #   fake_break_strength         vs  事件级 lv.state == "fake_break"（旧只有布尔）
    #     V2: 0-1 连续，看 close 回收强度（长下影 / 实体 / 连续根数）。
    #   dynamic_break_depth_pct     vs  cfg["break_depth_pct"]（默认 0.3 死阈值）
    #     V2: max(cfg_pct, 0.3 × ATR%)，自适应不同 regime 的波动率。仅记录展示，不真用。
    bounce_quality_enhanced: float = 0.0
    breakout_stage_enhanced: int = 0
    fake_break_strength: float = 0.0
    dynamic_break_depth_pct: float = 0.0

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M2.5 新增：state vs behavior 冲突预警
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 当旧 state 与新 behavior_state 严重不一致时记入（白话原因）。
    # 不写入 lv.contradiction_reasons（保持主路径清洁），仅前端 / AI 可见。
    # 例如：state=broken 但 false_break_risk≥0.65 → "形态破位但量价未确认"。
    contradiction_with_state: list[str] = Field(default_factory=list)

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M3.1 新增：评估元信息（GPT 审查采纳 · 区分"未评估"vs"评估为 0"）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 痛点：
    #   旧版本：所有分数默认 0.0，下游分不清"模块没运行"和"运行了但低分"。
    #   M3.1 修复：
    #     - behavior_eval_available=False 表示评估失败 / 缺数据 / 未运行
    #     - 回测引擎默认跳过这类样本（require_behavior_eval=True）
    #     - 前端可显示"未评估"灰色态而非误导性的"低分红色"
    behavior_eval_available: bool = True            # False = 该 lv 的 behavior 评估未成功
    behavior_eval_version: str = "1.0"              # 评估器版本（M3.1 起）
    input_quality: str = "ok"                        # ok / partial / missing
    missing_inputs: list[str] = Field(default_factory=list)  # 例如 ["candles", "atr"]
    evaluator_error: str = ""                        # 评估失败时的错误简述（最多 120 字符）


# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
# M2 新增：cascade_risk 4 子分（拆解原 0-1 单值）
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

class CascadeComponents(BaseModel):
    """级联风险拆 4 子分（M2 · GPT V3 评审采纳）。

    设计原则：
      - 总 cascade_risk 仍是 0-1（向后兼容），但 4 子分让用户/AI 看清"风险来自哪"
      - 子分各自归一到 0-1；总值 = max(0,1, 加权和)，权重见 _calc_cascade_risk
      - 4 子分独立于现有 cascade_layers/cascade_total_usd（保留向后兼容）

    含义：
      count_score:    破位后穿越的清算簇数量（0=无穿越/5+簇=满分）
      usd_score:      穿越簇累计 USD（0-200M+ 满分）
      velocity_score: 真空跨度紧凑度（gap 越小越易急速穿越）
      leverage_score: 主导杠杆密度（50x+ 主导 → 满分）
    """
    count_score: float = 0.0
    usd_score: float = 0.0
    velocity_score: float = 0.0
    leverage_score: float = 0.0


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

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # M3（V3 架构精装）— regime 上下文（snapshot 级）
    # 全部 Optional/默认值，向后兼容
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # 本快照评分时所处的 regime（来自 RegimeSnapshot.regime）
    # 前端可在头部显示 "📊 当前 Regime: 趋势上涨 · 0.72"
    regime: str = ""
    regime_confidence: float = 0.0
    regime_description: str = ""
    regime_weight_version: str = ""  # "3.0" 表示 M3 权重版本
