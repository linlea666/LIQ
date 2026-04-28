export interface TickerData {
  coin: string;
  ts: number;
  last: number;
  high_24h: number;
  low_24h: number;
  vol_24h: number;
  change_24h: number;
  change_pct_24h: number;
}

export interface FactorCard {
  id: string;
  name: string;
  value: string;
  direction: "bullish" | "bearish" | "neutral";
  sub_text: string;
  percentile: number;
  summary: string;
}

export interface MarketTemperature {
  coin: string;
  ts: number;
  score: number;
  label: string;
  pin_risk_level: string;
  pin_risk_label: string;
  factors: FactorCard[];
}

export interface WaterfallItem {
  factor_id: string;
  factor_name: string;
  contribution_pct: number;
  direction: "bullish" | "bearish";
}

export interface WaterfallData {
  coin: string;
  ts: number;
  items: WaterfallItem[];
  bullish_total: number;
  bearish_total: number;
  net_bias: number;
  net_label: string;
}

export interface PriceLevel {
  price: number;
  label: string;
  level_type: "support" | "resistance";
  strength: number;
  sources: string[];
  note: string;
}

export interface StopLossZone {
  direction: string;
  price: number;
  zone_from: number;
  zone_to: number;
  reasons: string[];
  atr_multiple: number;
}

export interface EntryZone {
  direction: string;
  price_from: number;
  price_to: number;
  confluence_sources: string[];
  confirmation_note: string;
}

export interface LadderEntry {
  tier: number;
  entry_price: number;
  stop_loss: number;
  take_profit: number;
  rr_ratio: number;
  position_weight: number;
  risk_pct: number;
  zone_label: string;
  entry_logic: string[];
  invalidation: string;
}

export interface LadderPlan {
  direction: string;
  tier_count: number;
  entries: LadderEntry[];
  total_risk_pct: number;
  best_case_rr: number;
  worst_case_loss_pct: number;
  expected_edge: string;
  plan_summary: string;
  coverage_range: string;
}

export interface LevelAnalysis {
  coin: string;
  ts: number;
  current_price: number;
  supports: PriceLevel[];
  resistances: PriceLevel[];
  stop_loss_zones: StopLossZone[];
  entry_zones: EntryZone[];
  pin_risk_zones: { price: number; side: string; liq_amount_usd: number; note: string }[];
  ladder_plans?: LadderPlan[];
}

export interface LiqBand {
  price_from: number;
  price_to: number;
  turnover_usd: number;
}

export interface LiqLeverageGroup {
  leverage: string;
  short_bands: LiqBand[];
  long_bands: LiqBand[];
  short_total_usd: number;
  long_total_usd: number;
}

export interface LiqCluster {
  price_center: number;
  price_from: number;
  price_to: number;
  total_usd: number;
  side: string;
  dominant_leverage: string;
  distance_pct: number;
}

export interface LiquidationMap {
  coin: string;
  ts: number;
  cycle: string;
  leverage_groups: LiqLeverageGroup[];
  clusters_above: LiqCluster[];
  clusters_below: LiqCluster[];
  vacuum_zones: { price_from: number; price_to: number; midpoint: number; note: string }[];
  imbalance_ratio: number;
}

export interface CVDPoint {
  ts: number;
  delta: number;
  cvd: number;
}

export interface OIData {
  coin: string;
  ts: number;
  current_usd: number;
  change_1h_pct: number;
  change_5m_pct: number;
  trend: string;
}

export interface FundingRateData {
  coin: string;
  ts: number;
  okx_rate: number | null;
  binance_rate: number | null;
  avg_rate: number;
  interpretation: string;
}

export interface BasisData {
  coin: string;
  ts: number;
  mark_price: number;
  index_price: number;
  basis_pct: number;
  interpretation: string;
}

export interface WallInfo {
  price: number;
  size: number;
  size_usd: number;
  order_count: number;
}

export interface OrderBookAnalysis {
  coin: string;
  ts: number;
  bid_walls: WallInfo[];
  ask_walls: WallInfo[];
  bid_total_usd: number;
  ask_total_usd: number;
  spread_pct: number;
}

export interface SourceHealth {
  name: string;
  status: "connected" | "degraded" | "disconnected";
  latency_ms: number;
}

export interface SignalSummary {
  direction: "bullish" | "bearish" | "neutral";
  confidence: "high" | "medium" | "low";
  reason: string;
}

export interface AIAnalysisResult {
  coin: string;
  ts: number;
  price_at_analysis: number;
  market_overview: string;
  key_levels: { type: string; price: string; strength: string; reason: string }[];
  stop_loss_suggestion: { raw: string };
  entry_zones: { direction: string; raw: string; details: string[] }[];
  sniper_setup?: string;
  sniper_plans?: {
    direction: string;
    entry: number | null;
    stop_loss: number | null;
    tp1: number | null;
    tp2: number | null;
    rr: number | null;
    logic: string;
    invalidation: string;
    raw_text: string;
  }[];
  ladder_plan_text?: string;
  trading_plan?: string;
  trading_plan_entries?: {
    tier: string;
    direction: string;
    entry: number | null;
    stop_loss: number | null;
    tp1: number | null;
    tp2: number | null;
    rr: number | null;
    source: string;
    logic: string;
  }[];
  risk_warnings: string[];
  scenario_analysis: { label: string; description: string }[];
  data_quality_feedback?: string;
  raw_text: string;
  user_prompt?: string;
  /** v2 · 完整 AI 交互过程：含 AI 人设/裁决框架/终审员权限/输出契约 */
  system_prompt?: string;
  /**
   * 2026-04-22 · 跨模型对照切片：已剥除我方规则侧结论 / 指令 / 输出格式要求的
   * 纯数据版 prompt，供用户一键复制到 Claude / Gemini / GPT-4 / Kimi 等其他 AI
   * 做独立方向判断，对比模型准确率。
   */
  data_snapshot_prompt?: string;
  signal_summary?: SignalSummary | null;
}

export interface RangeSignalData {
  ts: number;
  range_upper: number | null;
  range_upper_source: string;
  range_upper_tier: string;
  range_upper_score: number;
  range_upper_test_count: number;
  range_lower: number | null;
  range_lower_source: string;
  range_lower_tier: string;
  range_lower_score: number;
  range_lower_test_count: number;
  micro_upper: number | null;
  micro_upper_source: string;
  micro_upper_tier: string;
  micro_lower: number | null;
  micro_lower_source: string;
  micro_lower_tier: string;
  micro_width_pct: number;
  price_position: string;
  price_position_pct: number;
  box_state: string;
  box_state_ts: number;
  box_age_hours: number;
  box_width_pct: number;
  box_quality: number;
  breakout_probability: number;
  breakout_direction_bias: string;
  breakout_reason: string;
  ma60_daily: number | null;
  ma120_daily: number | null;
  ma60_weekly: number | null;
  macd_daily_above_zero: boolean | null;
  macd_daily_histogram: number | null;
  macd_daily_hist_rising: boolean | null;
  unfilled_wick_low: number | null;
  unfilled_wick_high: number | null;
  signal_grade: string | null;
  signal_direction: string | null;
  signal_reason: string;
  signal_entry: number | null;
  signal_stop_loss: number | null;
  signal_tp1: number | null;
  signal_rr_ratio: number | null;
  sweep_confirmed: boolean;
  cps_aligned: boolean;
  bb_squeeze: boolean;
  oi_buildup: boolean;
  volume_declining: boolean;
  funding_extreme: boolean;
  orderbook_imbalance: string;
  confluence_count: number;

  // Commit 3: 市场结构对齐度（与 1h MarketStructure 的关系）
  ms_direction?: "bullish" | "bearish" | "ranging" | "transitioning" | null;
  ms_event?: "BOS_up" | "BOS_down" | "CHoCH_up" | "CHoCH_down" | null;
  ms_bias?: "long_only" | "short_only" | "both_ok" | "stand_aside" | null;
  ms_confidence?: number;
  ms_alignment?: "aligned" | "conflict" | "neutral" | "unknown" | "";
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// TrendExhaustion · 趋势动能 / 衰竭 / 反转侦测
//   对齐后端 `models.trend_exhaustion.TrendExhaustionSignal`
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type TETimeframe = "1h" | "4h" | "1d";
export type TEDirection = "up" | "down" | "flat";
export type TERegime =
  | "trend_up"
  | "trend_down"
  | "range"
  | "squeeze"
  | "high_vol_chop"
  | "extreme";
export type TEExhaustionState =
  | "healthy_continuation"
  | "momentum_fading"
  | "exhaustion_warn"
  | "structural_reversal"
  | "neutral";
export type TEConsensusLevel = "strong_agree" | "partial" | "conflict" | "neutral";
export type TEOverallAction =
  | "add"
  | "hold"
  | "reduce"
  | "close"
  | "counter_small"
  | "counter_main"
  | "stand_aside";
export type TEActionHint =
  | "add"
  | "hold"
  | "reduce"
  | "close"
  | "counter_small"
  | "stand_aside";

export interface TESubScore {
  key: string;
  name: string;
  score: number;
  note: string;
  value: number | null;
}

export interface TrendExhaustionState {
  tf: TETimeframe;
  direction?: TEDirection;
  momentum_score: number;
  participation_score: number;
  exhaustion_score: number;
  composite_score: number;
  state: TEExhaustionState;
  state_age_min: number;
  confirmed_ticks?: number;
  triggers: string[];
  sub_scores: TESubScore[];
  action_hint: TEActionHint;
  reason_cn: string;
}

export interface TrendExhaustionSignal {
  coin: string;
  ts: number;
  tf_1h: TrendExhaustionState | null;
  tf_4h: TrendExhaustionState | null;
  tf_1d: TrendExhaustionState | null;
  consensus_level: TEConsensusLevel;
  overall_direction?: TEDirection;
  overall_state: TEExhaustionState;
  overall_action: TEOverallAction;
  overall_position_pct: number;
  /** 白话第一行：一眼结论，如 "📈 还在涨，动能健康" */
  overall_plain_cn?: string;
  /** 白话第二行：行动建议，如 "顺势持有或加仓都可以" */
  overall_tip_cn?: string;
  /** 白话第三行 / 专业细节：MTF 溯源 */
  overall_reason_cn: string;
  regime?: TERegime | null;
  regime_vetoed?: boolean;
  data_quality: "ok" | "partial" | "insufficient";
  missing_inputs: string[];
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// TE · AI 深度解读（DeepSeek V4-Flash 非思考模式驱动）
//   对齐后端 `models.te_interpretation.TEAIInterpretation`
//   WebSocket 事件：`te_ai_result` / `te_ai_error`
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type TEAIAlignment =
  | "agree"
  | "partial_disagree"
  | "strong_disagree"
  | "neutral"
  | "insufficient";

export type TEAIScenario =
  | "trend_continuation"
  | "bear_rebound"
  | "bull_pullback"
  | "reversal_early"
  | "reversal_confirmed"
  | "choppy_range"
  | "unclear";

// ── AI 审计员定位新增：趋势评估 ───────────────────────
export type TEPrimaryTrend = "uptrend" | "downtrend" | "sideways" | "transition";
export type TEMomentumQuality =
  | "fuel_full"
  | "fuel_adequate"
  | "fuel_fading"
  | "fuel_exhausted"
  | "unclear";
export type TEMomentumDirection =
  | "accelerating"
  | "stable"
  | "decelerating"
  | "unclear";

export interface TETrendAssessment {
  primary_trend: TEPrimaryTrend;
  momentum_quality: TEMomentumQuality;
  momentum_direction: TEMomentumDirection;
  health_summary_cn: string;
  evidence_cn: string;
}

// ── 关键位投射 ────────────────────────────────────────
export type TEBreakLikelihood =
  | "very_likely"
  | "likely"
  | "uncertain"
  | "unlikely"
  | "very_unlikely"
  | "insufficient";

export type TEDirectionTested = "resistance" | "support" | "both" | "none";

export interface TELevelProjection {
  target_level: number | null;
  direction_tested: TEDirectionTested;
  break_likelihood: TEBreakLikelihood;
  break_conviction: number;
  reasoning_cn: string;
  if_break_cn: string;
  if_fail_cn: string;
}

// ── 交易倾向 ──────────────────────────────────────────
export type TETradeDirection = "long" | "short" | "neutral" | "avoid";
export type TETradeStrength = "probe" | "standard" | "strong" | "none";

export interface TETradeBias {
  direction: TETradeDirection;
  strength: TETradeStrength;
  entry_zone_cn: string;
  invalidation_cn: string;
  timeframe_cn: string;
  why_cn: string;
}

export interface TEAIInterpretation {
  coin: string;
  ts: number;
  signal_fingerprint: string;
  model: string;
  cache_hit: boolean;
  from_cache_age_sec: number;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  reasoning_tokens: number;
  summary_cn: string;
  scenario: TEAIScenario;
  trend_assessment: TETrendAssessment | null;
  level_projection: TELevelProjection | null;
  trade_bias: TETradeBias | null;
  conflict_resolution: string;
  traps: string[];
  triggers_to_watch: string[];
  independent_view: string;
  action_suggestion: string;
  confidence: number;
  alignment_with_rules: TEAIAlignment;
  alignment_reason: string;
  reasoning: string;
  error: string | null;
  raw_text: string;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// TE · AI 解读 · 历史与详情（对齐 `monitoring.te_ai_log.log_interpretation`）
//   /api/te/ai_interpret/{coin}/history
//   /api/te/ai_interpret/{coin}/detail/{ts}
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface TEAIHistoryItem {
  ts: number;
  coin: string;
  price: number;
  fingerprint: string;
  model: string;
  cache_hit: boolean;
  from_cache_age_sec: number;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  reasoning_tokens: number;
  ai: {
    summary_cn: string;
    scenario: TEAIScenario;
    trend_assessment: TETrendAssessment | null;
    level_projection: TELevelProjection | null;
    trade_bias: TETradeBias | null;
    conflict_resolution: string;
    traps: string[];
    triggers_to_watch: string[];
    independent_view: string;
    action_suggestion: string;
    confidence: number;
    alignment_with_rules: TEAIAlignment;
    alignment_reason: string;
  };
  rules_snapshot: {
    overall_state?: string | null;
    overall_action?: string | null;
    overall_direction?: string | null;
    consensus_level?: string | null;
    regime?: string | null;
    regime_vetoed?: boolean;
    overall_position_pct?: number | null;
  };
  error: string | null;
}

export interface TEAIHistoryResponse {
  coin: string;
  items: TEAIHistoryItem[];
  total: number;
  limit: number;
}

/** /detail 返回在 TEAIHistoryItem 基础上附加 reasoning 字段 */
export interface TEAIDetailResponse extends TEAIHistoryItem {
  reasoning?: string;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// MTF 市场结构（日/周线完整快照）
//   对齐后端 `models.market_structure.MarketStructure`
//   仅 WebSocket market_update 的 `market_structure_1d/1w` 使用
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type MSDirection = "bullish" | "bearish" | "ranging" | "transitioning";
export type MSEvent = "BOS_up" | "BOS_down" | "CHoCH_up" | "CHoCH_down";
export type MSBias = "long_only" | "short_only" | "both_ok" | "stand_aside";

export interface MarketStructureSwing {
  ts: number;
  price: number;
  kind: "high" | "low";
}

export interface MarketStructureSnapshot {
  timeframe: "1h" | "1d" | "1w" | string;
  direction: MSDirection | string;
  last_event?: MSEvent | string | null;
  event_ts?: number | null;
  operate_bias: MSBias | string;
  confidence: number;
  structure_high?: number | null;
  structure_low?: number | null;
  swing_highs?: MarketStructureSwing[];
  swing_lows?: MarketStructureSwing[];
  summary?: string | null;
}

/**
 * 后端信号分类 id（backend/processors/key_level_tracker_v2.py `_finalize_signal`）
 * 前端据此渲染徽章文案、颜色、图标，比 A/B/C 字母更直观。
 */
export type KeyLevelSignalKind =
  | "wait_approach"
  | "wait_sweep"
  | "snipe_sweep"            // 扫取后做反向（A 级常见）
  | "snipe_bounce"           // 反弹确认做反向
  | "breakout_observing"    // 破位刚发生，观望
  | "breakout_retest"       // 破位回踩（B 级）
  | "breakout_continuation" // 破位三步确认完成（A 级延续）
  | "fake_break_reversal"   // 假突破回收反转（A 级）
  | "flip_retest"           // S/R 翻转回踩
  | "scalp"                 // 日内极小止损
  | "";

export interface KeyLevelSignal {
  level_price: number;
  side: string;
  state: string;
  action: string;
  confidence: "A" | "B" | "C";
  entry_price?: number;
  stop_loss?: number;
  tp1?: number;
  tp2?: number;
  rr_ratio?: number;
  reason: string;
  warnings: string[];
  /** 通过的确认项（如 sweep_taken / pattern_pin_bar / mtf_aligned / cvd_aligned ...） */
  confirmations: string[];
  /** 信号分类 id，前端据此渲染中文徽章 */
  signal_kind: KeyLevelSignalKind;
  /** 透明化置信度分数 0-100 (base(A=80/B=60/C=40) + 确认项×4 上限+20 - warnings×3) */
  score: number;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// V2 关键位系统（V1 KeyLevelSnapshot 已于 Commit 2 下线，payload 不再产出）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface KeyLevelV2 {
  price: number;
  side: "support" | "resistance";
  category: string;
  sources: string[];
  source_count: number;
  confluence_score: number;
  strength_tier: "S" | "A" | "B" | "C";
  state: "idle" | "approaching" | "testing" | "swept" | "bounced" | "broken" | "flipped";
  state_ts: number;
  prev_state: string;
  test_count: number;
  sweep_usd: number;
  lowest_wick?: number;
  break_start_ts: number;
  cascade_risk: number;
  cascade_layers: number;
  cascade_total_usd: number;
  distance_pct: number;
  timeframe: string;
  first_seen_ts: number;
  last_confirmed_ts: number;
  note: string;
  pattern_detected?: string;
  pattern_strength?: number;
  // Phase 2: 历史验证 + 屏障 + 最终打分
  bounce_count?: number;
  historical_validity?: number;
  barrier_score?: number;
  final_score?: number;

  // Commit 4: 质量标注（主动吸筹 / 被动触发 · 三步确认）
  bounce_quality?: "proactive" | "passive" | "";
  breakout_stage?: 0 | 1 | 2 | 3;
  // 假突破反转累计
  fake_break_count?: number;

  // ── M1（V3 准备阶段）— 多周期清算 + 算法化失效价 + 数据血统 ──
  // 全部 optional，向后兼容
  exchange_count?: number;            // 跨所共振数（来自 LiqCluster.exchange_count，max）
  consensus_multiplier?: number;      // 实际作用于评分的共识乘子（0.85-1.6）
  dominant_leverage?: string;         // 主导杠杆（如 "50x"）
  leverage_intensity?: number;        // 主导杠杆 USD 占比（0-1）

  invalidation_price?: number | null; // 算法化失效价
  invalidation_condition?: string;    // "15m 收盘 < $63,000"（对齐状态机 broken 判定口径）
  invalidation_atr_mult?: number;     // 计算时使用的 ATR 倍数

  next_magnet_price?: number | null;  // 破位后的下一个磁铁价位
  vacuum_gap_pct?: number;            // 当前位到下一磁铁的真空跨度（%）

  primary_source_age_hours?: number | null; // 主源年龄（小时）
  is_stale?: boolean;                 // 主源是否过期；UI 据此显示 "⏳ 数据偏旧"

  explain_chips?: string[];           // 白话证据芯片（"7d清算簇"、"3所共振"…）

  // ── M2（V3 评分体系核心升级）— 独立证据组 + S 4 分型 + 矛盾扣分 ──
  evidence_groups?: string[];         // 8 组之一或多个：structure_anchor / macro_technical /
                                      //   local_technical / liquidation_macro / liquidation_meso /
                                      //   liquidation_short / microstructure_local / flow_dynamic
  independent_group_count?: number;   // 去重后的组数；A/B/C 评级核心因子

  s_class?: "" | "S-Macro" | "S-Liquidity" | "S-Micro" | "S-Composite";

  contradiction_penalty?: number;     // 矛盾扣分（0~30+）
  contradiction_reasons?: string[];   // 矛盾原因（白话）

  cascade_components?: CascadeComponents | null;

  // ── M3（V3 评分体系精装）— regime-aware + 稳定 level_id + 生命周期 ──
  // 全部 optional，向后兼容
  level_id?: string;                  // 稳定 ID（基于 price_bucket，跨快照持久化）
  regime_modifier_applied?: number;   // 当前 regime 下作用于该位的乘数（0.85-1.10）
  regime_at_score?: string;           // 评分时的 regime 标签
  regime_weight_version?: string;     // regime 权重表版本号
  lifecycle_events?: LifecycleEvent[]; // 该位最近的生命周期事件（最多 20 条）

  // ── M4（V3 行为评估层 · 2026-04）— 关键位行为验证引擎 ──
  // 设计纪律（与后端 BehaviorEval docstring 同步）：
  //   1. 不影响 final_score / strength_tier / cascade_risk / state（state 仍由 state machine 决定）
  //   2. M1 阶段为纯观测：前端独立成区展示，不污染原 explain_chips
  //   3. 字段全部 optional，旧 snapshot 反序列化无破坏
  behavior?: BehaviorEval | null;
}

// M3 新增：关键位生命周期单条事件
export interface LifecycleEvent {
  ts: number;
  event_type:
    | "born"
    | "strengthening"
    | "weakening"
    | "tier_upgraded"
    | "tier_downgraded"
    | "tested"
    | "reacted"
    | "broken"
    | "fake_break"
    | "flipped"
    | "expired"
    | string;
  detail?: string;
  score_before?: number;
  score_after?: number;
  tier_before?: string;
  tier_after?: string;
  state_before?: string;
  state_after?: string;
}

// M2 新增：cascade_risk 4 子分（拆解原 0-1 单值）
export interface CascadeComponents {
  count_score: number;       // 0-1: 穿越簇数量
  usd_score: number;         // 0-1: 累计 USD
  velocity_score: number;    // 0-1: 真空跨度紧凑度
  leverage_score: number;    // 0-1: 主导杠杆密度
}

// M4 新增（V3 行为评估层 · 2026-04）：关键位行为验证引擎
//
// 设计原则：
//   - 旧 state machine 决定"事件是否发生"（broken/bounced/flipped 几何门）；
//     本接口决定"事件多可信"（连续 0-1 分数 + 多因子）
//   - 所有分数仅在合适的 state 下被填充，其它情况保持 0.0
//   - M1 阶段为纯观测：前端独立成区展示，不污染原 explain_chips、不进 AI prompt
//
// 6 个分数：
//   breakout_validity         真突破质量（state ∈ {broken, flipped, testing}）
//   retest_quality            回踩质量（state ∈ {bounced, flipped}）
//   selloff_continuation_risk 放量破位延续风险（state == broken & support）
//   capitulation_bottom_score 恐慌出清候选（state == broken & support）
//   flip_confirmation         翻转确认度（state == flipped）
//   false_break_risk          假突破风险（state ∈ {testing, broken, fake_break}）
export type BehaviorState =
  | "pending"                // 数据不足或独立计算无意义
  | "pending_breakout"       // 放量逼近，等收盘确认
  | "true_breakout"          // 真突破
  | "healthy_retest"         // 健康回踩
  | "failed_breakout"        // 假突破 / 失败突破
  | "heavy_volume_breakdown" // 放量破位（继续下行风险高）
  | "capitulation_flush"     // 恐慌出清候选（等二次确认）
  | "confirmed_flip"         // 翻转确认
  | "wait_for_second_test"   // 等二次测试
  | string;                  // 容错：后端可能扩展

export interface BehaviorEval {
  breakout_validity: number;          // 0-1
  retest_quality: number;             // 0-1
  selloff_continuation_risk: number;  // 0-1
  capitulation_bottom_score: number;  // 0-1
  flip_confirmation: number;          // 0-1
  false_break_risk: number;           // 0-1

  behavior_state: BehaviorState;
  state_confidence: number;           // 0-1：旧 state 的可信度

  explain_chips: string[];            // 行为侧 chip（独立显示，不污染原 explain_chips）
  components_used: string[];          // 数据完整性：哪些子因子参与了计算
  evaluated_at: number;               // 评估时间戳（秒）

  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // M2.5 双轨并行 · 影子字段（与后端 BehaviorEval 一致）
  // ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  // 旧 4 个 tracker 函数（_assess_bounce_quality / _assess_breakout_stage /
  // _fake_break_reclaim / _is_broken）保持不变；这里同时输出 V2 增强版结果，
  // 用于前端"V1 vs V2 对比"展示与 M3 回测决定何时切换。
  bounce_quality_enhanced?: number;   // 0-1 连续（替代 V1 的 proactive/passive 分类）
  breakout_stage_enhanced?: number;   // 0/1/2/3，时间窗按 timeframe 自适应缩放
  fake_break_strength?: number;       // 0-1 假突破回收强度（替代 V1 布尔事件）
  dynamic_break_depth_pct?: number;   // 动态破位阈值百分比（V1 固定 0.3%）

  // state vs behavior 严重不一致时记入（白话原因，前端 ⚠ 提示）
  contradiction_with_state?: string[];
}

// M1 新增：清算磁铁通道（独立列表，不参与 levels 评分）
export interface LiqMagnetLevel {
  price: number;
  magnet_role: "downside_pain_center" | "upside_short_squeeze" | "leverage_magnet" | string;
  source: "max_pain_long" | "max_pain_short" | "heatmap_top_density" | string;
  usd: number;
  distance_pct: number;
  leverage_hint?: string;
  note?: string;
}

// M1 新增：数据新鲜度元信息
export interface DataFreshness {
  ts: number;
  sources_age_seconds: Record<string, number>;
  overall_freshness_score: number;
  stale_sources: string[];
  missing_sources: string[];
}

export interface BullBearLine {
  sma200d: number | null;
  bmsa_upper: number | null;
  bmsa_lower: number | null;
  ichimoku_cloud_top: number | null;
  ichimoku_cloud_bottom: number | null;
  current_regime: "bull" | "bear" | "neutral" | "";
  regime_reason: string;
}

export interface BreakoutZone {
  bb_squeeze: boolean;
  squeeze_direction: "up" | "down" | "unknown" | "";
  bb_upper: number | null;
  bb_lower: number | null;
  keltner_upper: number | null;
  keltner_lower: number | null;
  note: string;
}

export interface FibSnapshotLevel {
  ratio: number;
  price: number;
  label: string;
}

export interface FibSnapshot {
  swing_high: number;
  swing_low: number;
  direction: string;
  levels: FibSnapshotLevel[];
}

export interface KeyLevelSnapshotV2 {
  ts: number;
  current_price: number;
  atr: number;
  levels: KeyLevelV2[];
  bull_bear_line: BullBearLine | null;
  breakout_zone: BreakoutZone | null;
  fib_snapshot: FibSnapshot | null;
  signals: KeyLevelSignal[];
  active_count: number;
  structure_summary: string;
  nearest_strong_support: number | null;
  nearest_strong_resistance: number | null;
  daily_strong_support: string | null;
  daily_strong_resistance: string | null;
  weekly_strong_support: string | null;
  weekly_strong_resistance: string | null;
  // ── M1 新增：磁铁通道 + 数据新鲜度 ──
  magnet_levels?: LiqMagnetLevel[];
  data_freshness?: DataFreshness | null;
  // ── M3 新增：snapshot 级 regime 上下文（来自 RegimeSnapshot） ──
  regime?: string;                  // trend_up / trend_down / range / extreme / squeeze / high_vol_chop
  regime_confidence?: number;       // 0-1
  regime_description?: string;      // 中文描述
  regime_weight_version?: string;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 挂单压力监测器（Orderbook Pressure Monitor）· 盘口订单流仪表盘
// 重构（2026-04）后定位 = 辅助参考，不再产出 snipe 信号。
// 对应后端 models/orderbook_pressure.py
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type WallSide = "ask" | "bid";

/** 数据来源：5min 订单簿热力图（近+中距）/ 大单 lifecycle（远距） */
export type WallSource = "depth_5m" | "large_orders";

/** 中性挂单标签（不再含真假语义）：
 *   wall_ask / wall_bid       - 当前活跃挂单墙
 *   wall_vanished             - 上轮存在、本轮消失（撤单/吃单未区分）
 *   wall_broken               - 价格已穿越该墙
 */
export type WallLabel = "wall_ask" | "wall_bid" | "wall_vanished" | "wall_broken";

export interface PressureWall {
  side: WallSide;
  price_lo: number;
  price_hi: number;
  price_mid: number;
  distance_pct: number;
  size_usd: number;
  size_base: number;
  rank: number;
  source: WallSource;
  large_order_ids: number[];
  large_order_count: number;
  has_active_whale: boolean;
  /** 加权平均挂单时长（秒），仅 large_orders 路径有意义 */
  holding_avg_age_sec: number;
  label: WallLabel;
  confluence_with_absorption: boolean;
  absorption_zone_price: number | null;
  /** 综合强度评分 = size_usd × duration × (1+0.3·whale) × (1+0.2·absorption) */
  strength_score: number;
  /** 强度等级（USD 绝对阈值）：S ≥ $30M / A ≥ $10M / B ≥ $3M / C ≥ $500K */
  strength_tier: "S" | "A" | "B" | "C";
  /** 中性摘要文案（前端 tooltip / 卡片说明用） */
  reason: string;
}

export interface OrderbookPressureSnapshot {
  coin: string;
  ts_sec: number;
  last_price: number;
  atr: number | null;
  walls: PressureWall[];
  top_resistance: number | null;
  top_support: number | null;
  sample_count_depth: number;
  sample_count_large_history: number;
  sample_count_large_orders_walls: number;
  data_quality: "ok" | "partial" | "stale" | "missing";
  notes: string[];
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// L4 数学引擎输出：ExecutionPlan（对应后端 models/execution_plan.py）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type TrafficLight = "green" | "yellow" | "orange" | "red" | "gray";
export type SafetyGateStatus = "pass" | "warn" | "block";
export type MarketRegimeLabel =
  | "trend_up"
  | "trend_down"
  | "range"
  | "squeeze"
  | "high_vol_chop"
  | "extreme";
export type TradingAction = "long" | "short" | "wait" | "avoid";
export type SignalDirection = "bullish" | "bearish" | "neutral" | "potential_reversal";

export interface ExecutionScoreBreakdown {
  base: number;
  tier_bonus: number;
  confidence_bonus: number;
  regime_alignment: number;
  corroboration_bonus: number;
  cascade_penalty: number;
  rr_bonus: number;
  backtest_bonus: number;
  event_factor: number;
  geo_risk_factor: number;
  safety_gate_delta: number;
  final_score: number;
}

export interface SafetyGateResultUI {
  g1_extreme_vol: SafetyGateStatus;
  g2_macro_event: SafetyGateStatus;
  g3_liq_chaos: SafetyGateStatus;
  g4_api_degrade: SafetyGateStatus;
  g5_blackswan: SafetyGateStatus;
  triggered: boolean;
  block_reason: string;
  warnings: string[];
}

export interface ExecutionPlan {
  coin: string;
  ts: number;
  current_price: number;
  regime: MarketRegimeLabel;
  regime_confidence: number;
  execution_score: number;
  traffic_light: TrafficLight;
  headline: string;
  one_liner: string;
  action: TradingAction;
  direction: SignalDirection;
  tier_hint: "S" | "A" | "B" | "C";
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  position_size_pct: number | null;
  expires_at: number | null;
  breakdown: ExecutionScoreBreakdown;
  safety_gates: SafetyGateResultUI;
  corroborating_sources: string[];
  historical_win_rate: number | null;
  historical_sample_size: number;
}

export interface ExecutionPlanResponse {
  ready: boolean;
  coin?: string;
  plan?: ExecutionPlan;
}

export interface MarketUpdate {
  coin: string;
  ts: number;
  ticker?: TickerData;
  temperature?: MarketTemperature;
  waterfall?: WaterfallData;
  levels?: LevelAnalysis;
  cvd_contract?: {
    trend: string;
    delta_1h: number;
    has_divergence: boolean;
    last_points: CVDPoint[];
  };
  oi?: OIData;
  funding?: FundingRateData;
  basis?: BasisData;
  orderbook?: OrderBookAnalysis;
  multi_funding?: Record<string, unknown>;
  ls_ratio?: Record<string, unknown>;
  ls_ratio_top_account?: Record<string, unknown>;
  ls_ratio_top_position?: Record<string, unknown>;
  etf_flow?: Record<string, unknown>;
  global_liq?: Record<string, unknown>;
  market_index?: Record<string, unknown>;
  sniper_entries?: Record<string, unknown>[];
  ladder_plans?: LadderPlan[];
  range_signal?: RangeSignalData;
  key_levels_v2?: KeyLevelSnapshotV2;
  orderbook_pressure?: OrderbookPressureSnapshot;
  trend_exhaustion?: TrendExhaustionSignal;
  market_structure_1d?: MarketStructureSnapshot;
  market_structure_1w?: MarketStructureSnapshot;
  option_max_pain?: Record<string, unknown>;
  option_info?: Record<string, unknown>;
  large_orders?: Record<string, unknown>;
  whale_data?: Record<string, unknown>;
  liq_max_pain?: Record<string, unknown>;
  liq_heatmaps?: Record<string, Record<string, unknown>>;
  rsi_14?: number;
  macd?: Record<string, unknown>;
  boll?: Record<string, unknown>;
  news_count?: number;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.3 · L7 AITraderReport（AI 引擎独立交易员产出）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type Resonance = "low" | "medium" | "high";
export type AgreementWithMath = "agree" | "caution" | "disagree";

export interface AIFactorRow {
  dimension: string;
  value_display: string;
  value_raw?: number | null;
  signal: string;
  direction: SignalDirection;
  resonance: Resonance;
  data_source_ref: string;
  link_anchor: string;
  confidence: number;
}

export interface AIFactorSection {
  section_id: "A" | "B" | "C" | "D" | "E" | "F" | "G";
  section_name_cn: string;
  section_emoji: string;
  rows: AIFactorRow[];
  section_summary: string;
  section_bias: SignalDirection;
}

export interface AIFactorMatrix {
  sections: AIFactorSection[];
  summary_line: string;
  overall_bias: SignalDirection;
  overall_confidence: "high" | "medium" | "low";
  analysis_ts: number;
  price_at_analysis: number;
}

export interface AIKeyLevelInterpretation {
  primary_support_price: number | null;
  primary_support_reason: string;
  primary_resistance_price: number | null;
  primary_resistance_reason: string;
  trap_warning: string;
  extra_levels: Array<{ price?: number; side?: string; reason?: string }>;
}

export interface AITradingPlan {
  priority: number;
  direction: TradingAction;
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  trigger_condition: string;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  position_suggestion_pct: number;
  conviction: number;
  tier_hint: "S" | "A" | "B" | "C";
  invalidation: string;
  related_narratives: string[];
  reason: string;
  aligned_with_math_engine: boolean;
  alignment_note: string;
}

export interface AINarrativeImpact {
  theme_id: string;
  theme_name_cn: string;
  ai_view_cn: string;
  weight_on_current_plan: Resonance;
}

export interface AITraderReport {
  coin: string;
  ts: number;
  price_at_analysis: number;
  model: string;
  thinking_tokens: number;
  latency_ms: number;
  market_view_cn: string;
  bias: SignalDirection;
  conviction: number;
  key_level_interpretation: AIKeyLevelInterpretation;
  trading_plans: AITradingPlan[];
  narrative_impact: AINarrativeImpact[];
  news_impact_summary_cn: string;
  geo_risk_assessment_cn: string;
  agreement_with_math_engine: AgreementWithMath;
  agreement_notes_cn: string;
  key_risks: string[];
  factor_matrix: AIFactorMatrix | null;
  scenario_analysis: Array<Record<string, unknown>>;
  raw_text: string;
  user_prompt: string;
}

export interface AITraderReportResponse {
  ready: boolean;
  coin?: string;
  report?: AITraderReport;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.3 · L7.5 FinalDecision（双引擎融合层对外主视图）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type ConsensusLevel =
  | "strong"
  | "agree"
  | "math_lead"
  | "ai_lead"
  | "conflict"
  | "both_wait";

export type RecommendedAction = "execute" | "reduce_size" | "wait" | "avoid";

export interface EngineBrief {
  engine_name: "math" | "ai";
  score: number;
  bias: SignalDirection;
  action: TradingAction;
  entry_hint: number | null;
  stop_loss_hint: number | null;
  tp1_hint: number | null;
  rr_ratio: number | null;
  position_pct: number;
  summary_cn: string;
}

export interface DivergenceStats {
  divergence_type: string;
  sample_size: number;
  math_win_rate: number;
  ai_win_rate: number;
  avg_delta_pct_24h: number;
  winner_hint_cn: string;
}

export interface FinalDecision {
  coin: string;
  ts: number;
  current_price: number;
  final_score: number;
  traffic_light: TrafficLight;
  headline: string;
  one_liner: string;
  consensus_level: ConsensusLevel;
  consensus_stars: number;
  consensus_summary_cn: string;
  math_brief: EngineBrief;
  ai_brief: EngineBrief;
  divergence_summary_cn: string;
  historical_divergence: DivergenceStats | null;
  recommended_action: RecommendedAction;
  recommended_position_pct: number;
  entry_zone_low: number | null;
  entry_zone_high: number | null;
  stop_loss: number | null;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  underlying_math_plan_ref: string;
  underlying_ai_plan_priority: number;
  active_themes_count: number;
  geo_risk_overall_level: number;
  geo_risk_label: string;
  has_blackswan_warning: boolean;
  safety_gate_triggered: boolean;
  safety_gate_reason: string;
  expires_at: number | null;
}

export interface FinalDecisionResponse {
  ready: boolean;
  coin?: string;
  decision?: FinalDecision;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// AI 详情页专用：AIAnalysisResult + L7 trader_report + L7.5 final_decision + 新闻简报
// 由 /api/ai/detail/{coin}/{ts} 返回，字段非破坏性追加
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface AIDetailNewsBrief {
  text: string;
  version: number;
  trigger: string;
  updated_at: number;
  geo_overview: Record<string, unknown> | null;
  active_narratives: Array<Record<string, unknown>>;
}

export interface AIDetailResponse extends AIAnalysisResult {
  ai_trader_report: AITraderReport | null;
  final_decision: FinalDecision | null;
  execution_plan: Record<string, unknown> | null;
  news_brief: AIDetailNewsBrief | null;
  _extras_source: "live" | "archive" | "none";
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// D09 · 滚动新闻简报（供 /news-brief 页面人工审计 AI 记忆锚）
// 由 GET /api/news-brief/current 返回
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export interface NewsBriefSection {
  section_id: "macro" | "regulatory" | "onchain" | "risk";
  section_title_cn: string;
  bullets: string[];
  max_bullets: number;
  last_rewritten_ts: number;
}

export interface NewsBriefTrackedTheme {
  theme_id: string;
  theme_name_cn: string;
  current_stance_cn: string;
  flip_flop_count_24h: number;
  latest_update_ts: number;
  relevance_score: number;
}

export interface NewsBriefFull {
  version: number;
  updated_at: number;
  ts_range_start: number;
  ts_range_end: number;
  coverage_hours: number;
  sections: NewsBriefSection[];
  tldr_cn: string;
  tracked_themes: NewsBriefTrackedTheme[];
  char_count: number;
  token_estimate: number;
  update_trigger: "scheduled" | "blackswan" | "user";
  based_on_events_count: number;
  model_used: string;
  generation_cost_ms: number;
  diff_from_prev_version: string;
  prev_version_updated_at: number | null;
}

export type NewsBriefUIStatus =
  | "ok"
  | "circuit_break"
  | "ai_failed"
  | "unexpected_empty"
  | "warming_up"
  | "bootstrap";

export interface NewsBriefCurrentResponse {
  ready: boolean;
  status: NewsBriefUIStatus;
  reason?: string;
  brief?: NewsBriefFull;
}

export interface NewsBriefHistoryResponse {
  ready: boolean;
  count: number;
  items: NewsBriefFull[];
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.6 · DecisionTracker · D1-D17 全景灯
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type DecisionStatus =
  | "pending"
  | "in_progress"
  | "ok"
  | "warn"
  | "failed"
  | "skipped";

export type OverallHealth =
  | "all_ok"
  | "partial"
  | "degraded"
  | "unhealthy";

export interface DecisionRecord {
  id: string;
  title: string;
  owner_module: string;
  success_criteria: string;
  status: DecisionStatus;
  detail: string;
  metrics: Record<string, unknown>;
  last_update_ts: number;
  last_ok_ts: number;
  last_warn_ts: number;
  last_fail_ts: number;
  total_marks: number;
  ok_count: number;
  warn_count: number;
  fail_count: number;
}

export interface DecisionSummary {
  ts: number;
  decisions: DecisionRecord[];
  overall_health: OverallHealth;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.5 · 分歧回测样本（原始轨迹 + 聚合统计）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type DivergenceOutcome = "pending" | "resolved" | "expired";

export interface DivergenceSampleRaw {
  sample_id: string;
  coin: string;
  divergence_type: string;
  math_action: string;
  math_bias: string;
  ai_action: string;
  ai_bias: string;
  price_at_record: number;
  created_ts: number;

  price_1h?: number | null;
  price_2h?: number | null;
  price_24h?: number | null;
  ts_1h?: number | null;
  ts_2h?: number | null;
  ts_24h?: number | null;

  resolved_ts?: number | null;
  delta_pct_1h?: number | null;
  delta_pct_2h?: number | null;
  delta_pct_24h?: number | null;
  math_win?: boolean | null;
  ai_win?: boolean | null;
  outcome: DivergenceOutcome;
}

export interface DivergenceStatsResponse {
  ready: boolean;
  coin: string;
  stats: DivergenceStats[];
  total_samples: number;
  recent_samples: DivergenceSampleRaw[];
  error?: string;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// P1.8a · AI Quality Ledger
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type AIMatrixSource =
  | "ai_json"
  | "rule_fallback"
  | "internal_conflict";

export type AIPlansSource =
  | "ai_json"
  | "markdown"
  | "sniper_fallback"
  | "wait_placeholder";

export type AIBiasVsText =
  | "consistent"
  | "conflict"
  | "text_missing"
  | "json_missing"
  | "unknown";

export type AIMathAgreement = "agree" | "caution" | "disagree" | "no_math_plan";

export interface AIQualityRecord {
  ts: number;
  coin: string;
  price_at_analysis: number;

  matrix_source: AIMatrixSource;
  plans_source: AIPlansSource;
  json_valid: boolean;
  json_invalid_reason: string;

  overlay_fields: number;
  ai_plans_count: number;
  ai_extra_rows: number;
  bias_vs_text: AIBiasVsText;

  final_bias: string;
  final_conviction: number;
  math_agreement: AIMathAgreement;

  latency_ms: number;
  reasoning_tokens: number;
  model: string;

  notes?: string[];
}

export interface AIQualityStats {
  coin: string;
  sample_size: number;
  window: number;

  ai_json_hit_rate: number;
  ai_plans_hit_rate: number;
  bias_consistency_rate: number;
  internal_conflict_rate: number;
  math_agreement_rate: number;

  avg_latency_ms: number;
  avg_reasoning_tokens: number;
  avg_overlay_fields: number;
  avg_ai_plans: number;

  first_ts: number;
  last_ts: number;

  trend_hint_cn: string;
  top_invalid_reasons?: Array<{ reason: string; count: number }>;
}

export interface AIQualityResponse {
  ready: boolean;
  coin: string;
  stats: AIQualityStats;
  recent: AIQualityRecord[];
  error?: string;
}

// ── P2.1 · Signal PnL 回放 ──────────────────────────

export type PlanPnLOrigin = "math" | "ai" | "final" | "all";
export type PlanPnLOutcome =
  | "pending"
  | "entry_filled"
  | "tp1_hit"
  | "tp2_hit"
  | "sl_hit"
  | "expired"
  | "invalidated";

export interface PlanPnLSample {
  sample_id: string;
  coin: string;
  origin: "math" | "ai" | "final";
  priority: number;
  action: "long" | "short" | "wait";
  tier: string;
  regime: string;
  entry_low: number;
  entry_high: number | null;
  stop_loss: number;
  tp1: number | null;
  tp2: number | null;
  rr_ratio: number | null;
  position_pct: number;
  created_ts: number;
  price_at_create: number;
  entry_filled_ts: number | null;
  entry_filled_price: number | null;
  outcome: PlanPnLOutcome;
  outcome_ts: number | null;
  outcome_price: number | null;
  r_multiple: number | null;
  max_favorable_r: number;
  max_adverse_r: number;
  invalidation_reason?: string;
}

export interface SignalPnLStats {
  coin: string;
  origin: string;
  tier: string;
  sample_size: number;
  window: number;
  win_rate: number;
  avg_r: number;
  expectancy_r: number;
  tp_hits: number;
  sl_hits: number;
  expired: number;
  invalidated: number;
  avg_mfe_r: number;
  avg_mae_r: number;
  entry_fill_rate: number;
  trend_hint_cn: string;
}

export interface SignalPnLResponse {
  ready: boolean;
  coin: string;
  stats: SignalPnLStats;
  origin_breakdown: SignalPnLStats[];
  tier_breakdown: SignalPnLStats[];
  recent: PlanPnLSample[];
  error?: string;
}

// ── P2.2 · Decision Health ─────────────────────────

export type HealthOverall = "ok" | "warn" | "fail" | "pending";
export type HealthEventKind =
  | "degrade"
  | "recover"
  | "escalate"
  | "de-escalate"
  | "change";

export interface HealthEvent {
  ts: number;
  id: string;
  title: string;
  from: string;
  to: string;
  kind: HealthEventKind;
  detail: string;
  metrics: Record<string, unknown>;
}

export interface HealthDegradedItem {
  id: string;
  title: string;
  owner_module: string;
  status: "warn" | "failed";
  detail: string;
  stuck_sec: number;
  metrics: Record<string, unknown>;
  last_update_ts: number;
}

export interface HealthSummaryResponse {
  ts: number;
  overall: HealthOverall;
  counts: { green: number; yellow: number; red: number; pending: number };
  ok_ids: string[];
  warn_ids: string[];
  fail_ids: string[];
  pending_ids: string[];
  degraded: HealthDegradedItem[];
  events: HealthEvent[];
}

// ── P2.4 · 历史快照回放 ──────────────────────────

export interface ReplayListItem {
  ts: number;
  coin: string;
  price_at_capture: number;
  ai_analysis_brief: string;
  has_plan: boolean;
  has_ai_report: boolean;
  has_final: boolean;
}

export interface ReplayFrame {
  ts: number;
  coin: string;
  price_at_capture: number;
  ai_analysis_brief: string;
  snapshot: Record<string, unknown>;
  execution_plan: Record<string, unknown> | null;
  ai_trader_report: Record<string, unknown> | null;
  final_decision: Record<string, unknown> | null;
}

export interface ReplayListResponse {
  ready: boolean;
  count: number;
  items: ReplayListItem[];
  error?: string;
}

export interface ReplayFrameResponse {
  ready: boolean;
  frame: ReplayFrame;
  error?: string;
}


// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Market Action Analyzer (MAA)
//   对齐 backend/models/market_action.py
//   WebSocket 事件：`market_action_report`
//   REST：GET /api/market-action/report, /report/history, /report/all
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export type MAAScenario =
  | "trend_continuation_up"
  | "trend_continuation_down"
  | "short_squeeze_up"
  | "long_squeeze_down"
  | "fake_breakout_up"
  | "fake_breakdown_down"
  | "exhaustion_top"
  | "exhaustion_bottom"
  | "range_bound";

export type MAAPhase =
  | "accumulation"
  | "markup"
  | "distribution"
  | "markdown"
  | "transition";

export type MAABias = "long" | "short" | "neutral" | "wait";
export type MAADataQuality = "ok" | "partial" | "insufficient";
export type MAAEvidenceSupports = "main" | "contrarian" | "neutral";
export type MAAEvidenceWeight = "high" | "medium" | "low";
export type MAAContinuityStance =
  | "continuation"
  | "refinement"
  | "reversal"
  | "first_run";

export type MAADimension =
  | "PriceContext"
  | "OI"
  | "Funding"
  | "Basis"
  | "CVD"
  | "Liquidation"
  | "LiqMap"
  | "LiqSweep"
  | "Footprint"
  | "Taker"
  | "Orderbook"
  | "Options"
  | string;

export interface MAAEvidenceItem {
  dimension: MAADimension;
  observation: string;
  inference?: string | null;
  supports: MAAEvidenceSupports;
  weight: MAAEvidenceWeight;
}

export interface MAAAlternativeScenario {
  scenario: MAAScenario;
  probability_pct: number;
  trigger: string;
}

export interface MAAContinuityVerdict {
  stance: MAAContinuityStance;
  previous_scenario?: MAAScenario | null;
  previous_ts?: number | null;
  note: string;
}

export interface MAATradingImplications {
  bias: MAABias;
  entry_zone?: [number, number] | null;
  stop_loss_beyond?: number | null;
  take_profit_targets: number[];
  notes?: string | null;
  trader_intuition?: string | null;
}

export interface MAAPromptSection {
  anchor: string;
  title: string;
  level: number;
}

export interface MAAPromptDebug {
  system: string;
  user: string;
  chars: number;
  sections: MAAPromptSection[];
  model: string;
  tokens_prompt?: number | null;
  tokens_completion?: number | null;
  tokens_reasoning?: number | null;
  latency_ms?: number | null;
  generated_at: number;
  ai_raw_response?: string | null;
  ai_reasoning_content?: string | null;
  parse_ok: boolean;
  parse_error?: string | null;
}

export interface MarketActionReport {
  coin: string;
  timestamp: number;
  market_conclusion: string;
  scenario: MAAScenario;
  market_phase: MAAPhase;
  analyst_reasoning?: string | null;
  confidence_rationale?: string | null;
  alternative_scenario?: MAAAlternativeScenario | null;
  continuity?: MAAContinuityVerdict | null;
  evidence_breakdown: MAAEvidenceItem[];
  trading_implications: MAATradingImplications;
  invalidation_conditions: string[];
  confidence: number;
  data_quality: MAADataQuality;
  stale_minutes: number;
  /** slim=true 时不返回 */
  facts_snapshot?: Record<string, unknown> | null;
  /** slim=true 或 include_prompt=false 时不返回 */
  prompt_debug?: MAAPromptDebug | null;
}

export interface MAAReportHistoryResponse {
  coin: string;
  count: number;
  items: MarketActionReport[];
}

// ── MAA 事后校准（Phase 5） ───────────────────────────
export type MAAHorizon = "4h" | "8h" | "24h";
export type MAAOutcomeLabel = "correct" | "wrong" | "neutral" | "pending";

export interface MAAEvalSample {
  ts: number;
  coin: string;
  scenario: MAAScenario;
  bias: MAABias;
  confidence: number;
  price_at_analysis: number;
  outcomes: Partial<Record<MAAHorizon, {
    price: number | null;
    delta_pct: number | null;
    label: MAAOutcomeLabel;
  }>>;
}

export interface MAACalibrationBucket {
  /** 如 "0-49", "50-59", "60-69", "70-79", "80-100" */
  range: string;
  sample_size: number;
  accuracy_pct: number | null;
}

export interface MAAHorizonStats {
  horizon: MAAHorizon;
  sample_size: number;
  correct: number;
  wrong: number;
  neutral: number;
  accuracy_pct: number | null;
}

export interface MAAEvalSummary {
  coin: string;
  window_days: number;
  sample_size: number;
  last_updated_ts: number;
  horizons: MAAHorizonStats[];
  calibration: MAACalibrationBucket[];
  per_scenario: Array<{
    scenario: MAAScenario;
    sample_size: number;
    accuracy_pct: number | null;
    horizon: MAAHorizon;
  }>;
  recent: MAAEvalSample[];
}

export interface MAAEvalResponse {
  ready: boolean;
  coin: string;
  summary?: MAAEvalSummary;
  error?: string;
}
