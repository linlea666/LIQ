/**
 * 短线预测合约信号 · TypeScript 类型镜像
 *
 * 与 backend/models/scalp_signal.py 保持字段一致（手工同步）。
 * 后端字段变更时此文件需同步更新（dev-constraints #2 全局视角）。
 */

// ────────────────────────────────────────────────────────────
// 枚举（来自后端 Literal）
// ────────────────────────────────────────────────────────────

export type StrategyName =
  | "A_sweep_reclaim"
  | "B_cvd_divergence"
  | "C_range_edge_fade";

export type ScalpDirection = "up" | "down";

export type HorizonMin = 10 | 30 | 60;

export type SignalState =
  | "active"
  | "expired_won"
  | "expired_lost"
  | "expired_push"
  | "cancelled";

export type SignalOutcome = "won" | "lost" | "push";

// P0-7：取消原因（用于双口径 shadow 统计）
export type InvalidationKind =
  | "regime_flip"
  | "data_stale"
  | "blackswan"
  | "manual"
  | "conflict";

// P0-1：结算价精度
export type SettlementQuality = "ok" | "low_samples" | "fallback" | "no_data";

// P0-2：hit_probability 是否经过校准
export type HitProbabilitySource = "calibrated" | "uncalibrated";

export type RegimeLabel =
  | "trend_up"
  | "trend_down"
  | "range"
  | "squeeze"
  | "high_vol_chop"
  | "extreme";

export type EvidenceWeight = "high" | "medium" | "low";

// ────────────────────────────────────────────────────────────
// Core models
// ────────────────────────────────────────────────────────────

export interface FactorBreakdown {
  core_signal_strength: number; // 0~1
  multi_tf_alignment: number;
  key_level_quality: number;
  data_freshness: number;
  historical_winrate: number;
  // P0-3 透明度：样本量 + 是否仍 blending 默认值
  historical_winrate_sample_size: number;
  historical_winrate_blended_with_default: boolean;
  weights: {
    core_signal_strength: number;
    multi_tf_alignment: number;
    key_level_quality: number;
    data_freshness: number;
    historical_winrate: number;
  };
}

export interface EvidenceItem {
  dimension: string; // "key_level" | "candle" | "cvd" | ...
  observation: string;
  weight: EvidenceWeight;
}

export interface StateTransition {
  ts: number;
  from_state: SignalState;
  to_state: SignalState;
  note?: string | null;
}

export interface ScalpSignal {
  signal_id: string;
  coin: string;
  horizon_min: HorizonMin;
  direction: ScalpDirection;
  strategy: StrategyName;
  reference_price: number;
  created_at: number;
  expiry_ts: number;
  entry_window_sec: number;
  confidence: number; // 0~100
  // P0-2：可能为 null（未校准），前端必须先判 null 再展示数字
  hit_probability: number | null;
  hit_probability_source: HitProbabilitySource;
  calibration_sample_size: number;
  regime: RegimeLabel;
  bias_score: number; // -1~+1
  factor_breakdown: FactorBreakdown;
  evidence: EvidenceItem[];
  veto_check_passed: string[];
  // P0-7 版本化 + 特征快照
  strategy_version: string;
  scorer_version: string;
  config_hash: string;
  features_snapshot: Record<string, unknown>;
  state: SignalState;
  state_history: StateTransition[];
  invalidation_kind: InvalidationKind | null;
  // 主结算
  settlement_price: number | null;
  outcome: SignalOutcome | null;
  settled_at: number | null;
  settlement_note: string | null;
  // P0-1 结算精度
  settlement_quality: SettlementQuality;
  settlement_window_samples: number;
  // P0-4 Shadow 结算
  shadow_settlement_price: number | null;
  shadow_outcome: SignalOutcome | null;
  shadow_settled_at: number | null;
  test_mode: boolean;
}

// ────────────────────────────────────────────────────────────
// Config
// ────────────────────────────────────────────────────────────

export interface StrategyConfig {
  enabled: boolean;
  display_name: string;
  description: string;
  confidence_threshold: number; // 50~100
  cooldown_min: number;
  notes: string;
  auto_disabled: boolean;
  auto_disabled_reason: string | null;
}

export interface ScalpNotificationConfig {
  browser_enabled: boolean;
  browser_min_confidence: number;
  email_enabled: boolean;
  email_min_confidence: number;
  test_mode_subject_prefix: string;
}

export interface ScalpConfig {
  version: number;
  enabled: boolean;
  coin: string;
  horizon_min: HorizonMin;
  test_mode: boolean;
  test_mode_banner_text: string;
  strategies: Record<StrategyName, StrategyConfig>;
  notification: ScalpNotificationConfig;
  updated_at: number;
}

// PATCH /api/scalp/config 的请求体（后端的 ScalpConfigPatch）
export interface ScalpConfigPatch {
  enabled?: boolean;
  coin?: string;
  horizon_min?: HorizonMin;
  strategies?: Partial<Record<StrategyName, Partial<{
    enabled: boolean;
    confidence_threshold: number;
    cooldown_min: number;
    notes: string;
  }>>>;
  notification?: Partial<{
    browser_enabled: boolean;
    browser_min_confidence: number;
    email_enabled: boolean;
    email_min_confidence: number;
  }>;
}

// ────────────────────────────────────────────────────────────
// Stats / Calibration
// ────────────────────────────────────────────────────────────

export interface ConfidenceBucket {
  bucket_lo: number;
  bucket_hi: number;
  total: number;
  won: number;
  lost: number;
  push: number;
  win_rate: number;
}

export interface RegimeSlice {
  regime: RegimeLabel;
  total: number;
  won: number;
  lost: number;
  win_rate: number;
}

export interface HourSlice {
  hour_utc: number;
  total: number;
  won: number;
  win_rate: number;
}

export interface StrategyStats {
  strategy: StrategyName;
  total: number;
  won: number;
  lost: number;
  push: number;
  cancelled: number;
  win_rate: number;
  expected_return_per_signal: number; // 实际盈亏率（按 0.8:1 推算）
  break_even_win_rate: number; // 0.5556
  is_profitable: boolean;
  by_confidence: ConfidenceBucket[];
  by_regime: RegimeSlice[];
  by_hour_utc: HourSlice[];
  last_signal_ts: number | null;
  // P0-4 Shadow window（双口径）
  shadow_total: number;
  shadow_won: number;
  shadow_lost: number;
  shadow_win_rate: number | null;
  shadow_breakdown_by_kind: Record<string, number>;
}

export interface GlobalStats {
  total_signals: number;
  total_won: number;
  total_lost: number;
  total_push: number;
  total_cancelled: number;
  global_win_rate: number;
  global_expected_return: number;
  // P0-4
  overall_shadow_total: number;
  overall_shadow_win_rate: number | null;
  by_strategy: StrategyStats[];
  computed_at: number;
}

export interface CalibrationPoint {
  predicted_min: number; // 置信度区间下界（如 70）
  predicted_max: number; // 置信度区间上界（如 80）
  actual_win_rate: number; // 0~1
  sample_size: number;
}

export interface CalibrationCurve {
  points: CalibrationPoint[];
  sample_size_total: number;
  computed_at: number;
}

// ────────────────────────────────────────────────────────────
// API responses
// ────────────────────────────────────────────────────────────

export interface ListSignalsResp {
  count: number;
  signals: ScalpSignal[];
  ts: number;
}

export interface CancelSignalResp {
  ok: boolean;
  signal: ScalpSignal;
}

// 历史过滤参数
export interface HistoryFilter {
  limit?: number;
  strategy?: StrategyName;
  coin?: string;
  horizon_min?: HorizonMin;
  since_ts?: number;
}

// ────────────────────────────────────────────────────────────
// 工具：策略元数据（前端展示常量）
// ────────────────────────────────────────────────────────────

export const STRATEGY_META: Record<
  StrategyName,
  { displayCn: string; shortCn: string; emoji: string; color: string }
> = {
  A_sweep_reclaim: {
    displayCn: "扫单回归 (Sweep & Reclaim)",
    shortCn: "扫单回归",
    emoji: "🎯",
    color: "#0891b2",
  },
  B_cvd_divergence: {
    displayCn: "现合 CVD 背离 (CVD Divergence)",
    shortCn: "CVD 背离",
    emoji: "🔀",
    color: "#9333ea",
  },
  C_range_edge_fade: {
    displayCn: "区间边缘回归 (Range Edge Fade)",
    shortCn: "区间回归",
    emoji: "📊",
    color: "#ea580c",
  },
};

export const REGIME_META: Record<
  RegimeLabel,
  { displayCn: string; color: string }
> = {
  trend_up: { displayCn: "上升趋势", color: "#16a34a" },
  trend_down: { displayCn: "下降趋势", color: "#dc2626" },
  range: { displayCn: "震荡区间", color: "#0891b2" },
  squeeze: { displayCn: "压缩积蓄", color: "#9333ea" },
  high_vol_chop: { displayCn: "高波动洗盘", color: "#f59e0b" },
  extreme: { displayCn: "极端行情", color: "#7c3aed" },
};

// 0.8:1 赔率下的盈亏平衡点
export const BREAK_EVEN_WIN_RATE = 5 / 9; // ≈ 0.5556

/** 根据 outcome 返回盈亏率（用于历史 tile 显示） */
export function pnlRateOf(outcome: SignalOutcome | null): number {
  if (outcome === "won") return 0.8;
  if (outcome === "lost") return -1.0;
  return 0;
}

// ────────────────────────────────────────────────────────────
// P0-8 样本量分档 + Wilson CI
// ────────────────────────────────────────────────────────────

export type SampleSizeTier = "cold_start" | "early" | "mid" | "mature";

export function tierForSampleSize(n: number): SampleSizeTier {
  if (n < 30) return "cold_start";
  if (n < 100) return "early";
  if (n < 300) return "mid";
  return "mature";
}

export const SAMPLE_TIER_LABEL: Record<SampleSizeTier, string> = {
  cold_start: "冷启动 (N<30)",
  early: "样本不足 (30-100)",
  mid: "校准中 (100-300)",
  mature: "可信 (300+)",
};

export const SAMPLE_TIER_BADGE_COLOR: Record<SampleSizeTier, string> = {
  cold_start: "#9ca3af", // gray
  early: "#f59e0b",      // amber
  mid: "#0891b2",        // cyan
  mature: "#16a34a",     // green
};

/**
 * Wilson 95% CI（二项分布置信区间，z=1.96）
 * 比"win/total"的简单比例更稳健（小样本不夸张）
 */
export function wilson95(won: number, decided: number): { lo: number; hi: number } {
  if (decided <= 0) return { lo: 0, hi: 0 };
  const z = 1.96;
  const p = won / decided;
  const denom = 1 + (z * z) / decided;
  const center = p + (z * z) / (2 * decided);
  const margin = z * Math.sqrt((p * (1 - p)) / decided + (z * z) / (4 * decided * decided));
  return {
    lo: Math.max(0, (center - margin) / denom),
    hi: Math.min(1, (center + margin) / denom),
  };
}
