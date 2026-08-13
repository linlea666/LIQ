"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";
import { copyTextToClipboard } from "@/lib/clipboard";

// evidence_quality / correlation_audit / reliability 均为 bottom-v3 新增，
// 全部声明为可选并在渲染处回退，保证 v2 历史快照仍可正常渲染。
type SubSignal = {
  key: string; label: string; weight: number; ok: boolean;
  score: number | null; value: number | null; percentile: number | null; note: string;
  evidence_quality?: number | null; eq_note?: string;
  direction?: "NET_SELLING" | "NEUTRAL" | "NET_BUYING" | null;
};
type Factor = {
  key: string; label: string; weight: number; score: number | null;
  coverage: number; sub_signals: SubSignal[]; evidence_quality?: number | null;
};
type Check = {
  key: string; label: string; ok: boolean; score: number | null; note: string;
  evidence_quality?: number | null;
  state?: string | null; status?: "SCORABLE" | "UNSCORABLE" | string;
};
type Trigger = { key: string; label: string; penalty: number; note: string };
type FilterEvaluation = Trigger & {
  status: "TRIGGERED" | "CLEAR" | "UNSCORABLE";
  coverage?: number;
  inputs?: Record<string, unknown>; thresholds?: Record<string, unknown>;
};
type DemandDimension = {
  key: string; label: string; score: number | null; evidence_quality: number | null;
  coverage: number; status: string; components: SubSignal[];
};
type EvidenceMember = {
  layer: string; key: string; label: string; score: number | null;
  evidence_quality?: number | null; status: string; note: string; direction?: string | null;
};
type EvidenceCluster = {
  key: string; label: string;
  stance: "supporting" | "opposing" | "mixed" | "neutral" | "unknown";
  primary: EvidenceMember | null; members: EvidenceMember[];
  available_n: number; unknown_n: number;
};
type Analog = {
  day: string; label: string; similarity: number | null;
  common_factors: string[]; past_stress?: number | null; note: string;
  reliability?: "high" | "medium" | "low";
};
type CorrelationPair = { a: string; b: string; rho: number; n: number };
type CorrelationAudit = {
  window_days: number;
  groups: { key: string; label: string; note: string; pairs: CorrelationPair[]; max_abs_rho: number; strong_pairs: number }[];
  cross_layer_overlaps: { topic: string; usage: string; note: string }[];
  structural_redundancies?: { topic: string; basis: string; conclusion: string }[];
};
// base_rate 为 bottom-v4 新增（历史频率层），v2/v3 快照缺失时面板整体不渲染
type BaseRateWindow = {
  weeks: number; points: number; independent: number; segments: number;
  hit_rate: number | null; hit_rate_ci95?: number[] | null; median_return: number | null;
  worst_return: number | null; reliable: boolean;
};
type BaseRateCondition = {
  label: string; description: string; points: number;
  windows: BaseRateWindow[]; reliable: boolean;
};
type BaseRateLadderStep = { threshold: number; points: number; windows: BaseRateWindow[] };
type BaseRate = {
  algorithm_version: string; hit_threshold_pct: number;
  forward_weeks: number[]; min_independent: number;
  replay: { points: number; first_day: string | null; last_day: string | null; step_days: number };
  baseline: BaseRateCondition;
  conditions: BaseRateCondition[];
  stress_ladder: BaseRateLadderStep[];
  confirmation_ladder: BaseRateLadderStep[];
  validation_kind?: string; statistical_claim?: string;
  legacy_ladder_kind?: string;
  disjoint_bins?: {
    stress: { label: string; points: number; windows: BaseRateWindow[] }[];
    confirmation: { label: string; points: number; windows: BaseRateWindow[] }[];
  };
  monotonicity?: Record<string, { claim: string; by_weeks: Record<string, { status: string; reliable_bins: number }> }>;
  caveats: string[];
};
type Snapshot = {
  day: string; ts: number; algorithm_version: string;
  schema_version?: string; model_id?: string; data_policy_id?: string; dataset_id?: string;
  as_of?: string; validation_status?: string; audit_id?: string | null;
  audit_match?: { status: "MATCHED" | "NO_MATCHING_AUDIT"; audit_id: string | null; reason: string; summary?: Record<string, unknown> | null };
  prediction?: { kind: "score" | "calibrated_probability"; score: number | null; probability: number | null };
  quality_status?: "OK" | "DEGRADED" | "ABSTAINED" | "INVALID_DATA";
  blocking_reasons?: string[];
  price_context: { price: number | null; ma_200w: number | null; sth_realized_price: number | null; lth_realized_price: number | null };
  stress: { score: number; active_weight: number; abstained: string[]; evidence_quality?: number | null } | null;
  confirmation: { score: number | null; score_before_penalty: number | null; checks: Check[]; evidence_quality?: number | null };
  evidence_quality?: { stress: number | null; confirmation: number | null; overall: number | null };
  correlation_audit?: CorrelationAudit;
  base_rate?: BaseRate | null;
  fake_bottom_filter: { triggers: Trigger[]; total_penalty: number; evaluations?: FilterEvaluation[] };
  quadrant: { key: string; label: string; note: string };
  seller_exhaustion: { score: number; components: Record<string, number> } | null;
  factors: Factor[];
  demand_dimensions?: { direct_spot_demand: DemandDimension; liquidity_ammunition: DemandDimension };
  counter_evidence: {
    supporting: string[]; opposing: string[]; neutral?: string[]; unknown?: string[];
    clusters?: EvidenceCluster[]; independent_counts?: Record<string, number>;
  };
  analogs: Analog[];
  delta: { stress_7d: number | null; stress_30d: number | null; confirmation_7d: number | null; confirmation_30d: number | null };
  data_quality: { ok: boolean; missing: string[]; blocking_missing?: string[]; stale: { metric: string; last_day: string; behind_days: number }[]; blocking_stale?: { metric: string; last_day: string; behind_days: number }[]; failed_fetches: Record<string, string> };
};
type HistoryItem = {
  day: string;
  algorithm_version?: string; model_id?: string; data_policy_id?: string;
  stress?: { score: number } | null;
  confirmation?: { score: number | null } | null;
};

const fmt = (v: number | null | undefined, digits = 1) =>
  v == null ? "—" : v.toFixed(digits);
const fmtSigned = (v: number | null | undefined) =>
  v == null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(1)}`;
const fmtValue = (v: number | null | undefined) => {
  if (v == null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e9) return `${(v / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${v.toFixed(0)}`;
  return `${v.toFixed(4)}`;
};
const scoreTone = (score: number | null) =>
  score == null ? "text-slate-500"
    : score >= 70 ? "text-emerald-400"
    : score >= 45 ? "text-amber-300"
    : "text-rose-400";
// 卖方衰竭子项中文名：该仪表已升到首屏，英文 key 对非专业读者不可读
const EXHAUSTION_LABEL: Record<string, string> = {
  loss_decay: "亏损衰减", loss_profit_ratio: "亏损/利润比",
  sopr_recovery: "SOPR 回升", liq_decay: "清算衰减", sth_stable: "STH 供应稳定",
};
const RELIABILITY_LABEL: Record<string, string> = { high: "高", medium: "中", low: "低" };
const RELIABILITY_TONE: Record<string, string> = {
  high: "text-emerald-400/80", medium: "text-amber-400/80", low: "text-slate-500",
};
const QUADRANT_TONE: Record<string, string> = {
  bear_market: "border-slate-600 bg-slate-800/60 text-slate-300",
  panic_flush: "border-rose-700/60 bg-rose-900/30 text-rose-300",
  basing: "border-amber-700/60 bg-amber-900/30 text-amber-300",
  confirmed_recovery: "border-emerald-700/60 bg-emerald-900/30 text-emerald-300",
  unknown: "border-slate-700 bg-slate-900 text-slate-500",
};

// ── 小白结论横幅：把象限/评分翻译成人话，并展示四阶段进度 ──

const STAGES = [
  { key: "bear_market", label: "压力不足" },
  { key: "panic_flush", label: "高压力／确认不足" },
  { key: "basing", label: "高压力／早期确认" },
  { key: "confirmed_recovery", label: "高压力／多项确认" },
] as const;

// 结论措辞刻意写成"陈述已发生的事实 + 尚未发生的事实"，不预测概率：
// 历史大底样本仅 4 个，模型无法支撑"概率上升"这类断言。
const VERDICT: Record<string, string> = {
  bear_market: "压力不足，未形成周期底部证据集",
  panic_flush: "高压力，但确认不足",
  basing: "高压力，出现部分早期确认",
  confirmed_recovery: "高压力，出现多项确认",
  unknown: "数据不足，暂无法判断",
};

function legacyDemandDimension(snapshot: Snapshot, keys: string[]) {
  const demand = snapshot.factors.find((factor) => factor.key === "demand");
  const selected = (demand?.sub_signals ?? []).filter(
    (sub) => keys.includes(sub.key) && sub.score != null,
  );
  const effectiveWeight = (sub: SubSignal) => (
    sub.weight * ((sub.evidence_quality ?? 100) / 100)
  );
  const effective = selected.reduce((sum, sub) => sum + effectiveWeight(sub), 0);
  const nominalActive = selected.reduce((sum, sub) => sum + sub.weight, 0);
  return {
    score: effective > 0
      ? selected.reduce(
        (sum, sub) => sum + (sub.score as number) * effectiveWeight(sub), 0,
      ) / effective
      : null,
    eq: nominalActive > 0 ? 100 * effective / nominalActive : null,
  };
}

/** 三段式陈述：压力 / 卖方衰竭 / 需求，各自只说当前观测到的事实 */
function verdictStatements(snapshot: Snapshot): string[] {
  const stress = snapshot.stress?.score ?? null;
  const exhaustion = snapshot.seller_exhaustion?.score ?? null;
  const demand = snapshot.demand_dimensions?.direct_spot_demand.score
    ?? legacyDemandDimension(snapshot, ["coinbase_premium", "spot_net_taker", "etf_momentum"]).score;
  const liquidity = snapshot.demand_dimensions?.liquidity_ammunition.score
    ?? legacyDemandDimension(snapshot, ["stablecoin_growth", "exchange_outflow"]).score;
  const stage = snapshot.confirmation.checks.find((c) => c.key === "structure_stage");
  const lines: string[] = [];
  lines.push(
    stress == null ? "市场压力：数据不足"
      : stress >= 70 ? `市场压力 ${stress.toFixed(0)}：规则分数处于高压力区`
      : stress >= 55 ? `市场压力 ${stress.toFixed(0)}：规则分数进入中高压力区`
      : `市场压力 ${stress.toFixed(0)}：规则分数未进入高压力区`,
  );
  lines.push(
    exhaustion == null ? "卖方衰竭：数据不足"
      : exhaustion >= 70 ? `卖方衰竭 ${exhaustion.toFixed(0)}：衰竭组件读数偏高，仍需价格与需求确认`
      : exhaustion >= 45 ? `卖方衰竭 ${exhaustion.toFixed(0)}：部分组件改善，尚未确认枯竭`
      : `卖方衰竭 ${exhaustion.toFixed(0)}：衰竭组件读数偏低`,
  );
  lines.push(
    demand == null ? "直接现货需求：数据不足"
      : demand >= 70 ? `直接现货需求 ${demand.toFixed(0)}：多项流量方向为正`
      : demand >= 50 ? `直接现货需求 ${demand.toFixed(0)}：正负证据有限或分化`
      : `直接现货需求 ${demand.toFixed(0)}：净卖出或需求不足仍占主导`,
  );
  lines.push(
    liquidity == null ? "流动性弹药：数据不足"
      : `流动性弹药 ${liquidity.toFixed(0)}：仅表示潜在资金与持币代理，不代表已经进场`,
  );
  if (stage?.note) lines.push(`价格结构：${stage.note}`);
  return lines;
}

function VerdictBanner({ snapshot }: { snapshot: Snapshot }) {
  const qkey = snapshot.quadrant.key;
  const headline = VERDICT[qkey] ?? VERDICT.unknown;
  const stageIdx = STAGES.findIndex((s) => s.key === qkey);
  const pendingChecks = snapshot.confirmation.checks.filter((c) => (c.score ?? 0) < 100);
  const triggers = snapshot.fake_bottom_filter.triggers;
  const tone = QUADRANT_TONE[qkey] ?? QUADRANT_TONE.unknown;
  const overallEq = snapshot.evidence_quality?.overall ?? null;
  return (
    <div className={`rounded-lg border p-4 ${tone}`}>
      <div className="text-[11px] opacity-70">当前判断（每日更新 · 仅供参考，非交易指令）</div>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <span className="text-2xl font-semibold">{headline}</span>
        <EqBadge eq={overallEq} label="整体证据质量" />
      </div>
      <ul className="mt-2 space-y-0.5 text-[12px] leading-relaxed opacity-90">
        {verdictStatements(snapshot).map((line) => <li key={line}>· {line}</li>)}
      </ul>

      {/* 四阶段进度：回答"现在到什么程度了" */}
      <div className="mt-4 flex items-start gap-1.5">
        {STAGES.map((stage, i) => (
          <div key={stage.key} className="flex-1">
            <div className={`h-1.5 rounded-full ${
              stageIdx >= 0 && i <= stageIdx ? "bg-current opacity-90" : "bg-slate-700/50"}`} />
            <div className={`mt-1.5 text-center text-[10px] ${
              i === stageIdx ? "font-semibold opacity-100" : "opacity-45"}`}>
              {stage.label}{i === stageIdx ? " ◀ 现在" : ""}
            </div>
          </div>
        ))}
      </div>

      <div className="mt-3 grid gap-2 text-[11px] leading-relaxed md:grid-cols-2">
        <div className="opacity-75">
          <span className="opacity-70">读数解释：</span>
          市场压力（市场有多惨，≥55 进入极端区）· 改善确认（有没有开始好转，≥65 算确认）·
          卖方衰竭（抛压是否枯竭）· 证据质量 EQ（这些分数有多可信，越低越该打折）
        </div>
        <div className="space-y-1">
          {qkey !== "confirmed_recovery" && pendingChecks.length > 0 && (
            <div className="opacity-85">
              尚未满足的确认项：{pendingChecks.map((c) => `${c.label}${c.ok ? "" : "（缺失）"}`).join("、")}
            </div>
          )}
          {triggers.length > 0 && (
            <div className="text-rose-300/90">
              ⚠ 假底风险：{triggers.map((t) => t.label).join("、")}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

const EQ_HINT = "证据质量 EQ：由历史跨度、可用分位窗口、数据新鲜度、代理关系四项"
  + "推导（两个 BTC 周期 = 满分跨度），已作为子信号聚合的权重乘子。EQ 越低，"
  + "同样的分数越不该被当真。";

/** 证据质量徽标；v2 快照无该字段时不渲染 */
function EqBadge({ eq, label = "EQ" }: { eq?: number | null; label?: string }) {
  if (eq == null) return null;
  const tone = eq >= 70 ? "border-slate-600 text-slate-400"
    : eq >= 50 ? "border-amber-800/70 text-amber-400/90"
    : "border-rose-900/70 text-rose-400/80";
  return (
    <span className={`rounded border px-1.5 py-px text-[9px] ${tone}`} title={EQ_HINT}>
      {label} {eq.toFixed(0)}
    </span>
  );
}

function ScoreDial({ label, score, sub, eq }: {
  label: string; score: number | null; sub?: string; eq?: number | null;
}) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3">
      <div className="flex items-start justify-between gap-2">
        <div className="text-[11px] text-slate-500">{label}</div>
        <EqBadge eq={eq} />
      </div>
      <div className={`mt-1 text-3xl font-semibold ${scoreTone(score)}`}>{fmt(score)}</div>
      {sub && <div className="mt-1 text-[10px] text-slate-500">{sub}</div>}
    </div>
  );
}

/** 历史频率层（v4 起）：把当前读数放回历史，看同类状态后来实际发生了什么。
 *  样本不足的格子后端已把 hit_rate 置空，前端一律显示为"样本太少"而不补算。 */
function BaseRatePanel({ br }: { br: BaseRate }) {
  const current = br.conditions[0];
  if (!current) return null;
  const baseOf = (weeks: number) => br.baseline.windows.find((w) => w.weeks === weeks);
  const excess = (w: BaseRateWindow) => {
    const base = baseOf(w.weeks);
    if (!w.reliable || !base?.reliable || w.hit_rate == null || base.hit_rate == null) return null;
    return w.hit_rate - base.hit_rate;
  };
  const negative = current.windows.filter((w) => w.reliable && (w.median_return ?? 0) < 0);
  const worst = current.windows.reduce<BaseRateWindow | null>(
    (acc, w) => (w.worst_return != null && (acc?.worst_return == null || w.worst_return < acc.worst_return) ? w : acc),
    null,
  );
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
      <div className="mb-1 text-[12px] font-medium text-slate-300">
        这个状态历史上意味着什么
        <span className="ml-2 text-[10px] font-normal text-slate-500">
          {br.replay.first_day} 起 {br.replay.points} 个周级时点回放（{br.algorithm_version}）·
          胜率 = N 周后涨幅 ≥ {br.hit_threshold_pct}%
        </span>
      </div>
      <div className="mb-3 text-[10px] text-slate-500">
        PIT_APPROX 样本内历史条件描述 · NO_STATISTICAL_CLAIM。不是严格 OOS、概率或模型背书。
      </div>

      <div className="grid gap-2 sm:grid-cols-3">
        {current.windows.map((w) => {
          const base = baseOf(w.weeks);
          const ex = excess(w);
          return (
            <div key={w.weeks} className="rounded border border-slate-800 bg-slate-950/50 p-3">
              <div className="text-[10px] text-slate-500">{w.weeks} 周后</div>
              {w.reliable && w.hit_rate != null ? (
                <>
                  <div className="text-2xl font-semibold text-slate-300">
                    {w.hit_rate.toFixed(0)}%
                  </div>
                  <div className="text-[10px] text-slate-500">
                    全样本基准 {base?.hit_rate?.toFixed(0) ?? "—"}%
                    {ex != null && (
                      <span className="text-slate-400">
                        {" "}（{ex >= 0 ? "+" : ""}{ex.toFixed(0)}pp）
                      </span>
                    )}
                  </div>
                  <div className="text-[10px] text-slate-500">
                    95% Wilson CI {w.hit_rate_ci95?.length === 2
                      ? `${w.hit_rate_ci95[0].toFixed(0)}%–${w.hit_rate_ci95[1].toFixed(0)}%`
                      : "—"}
                  </div>
                  <div className="mt-1 text-[10px] text-slate-400">
                    中位收益{" "}
                    <span className={(w.median_return ?? 0) >= 0 ? "text-emerald-400/90" : "text-rose-400/90"}>
                      {(w.median_return ?? 0) >= 0 ? "+" : ""}{w.median_return?.toFixed(1)}%
                    </span>
                  </div>
                </>
              ) : (
                <>
                  <div className="text-2xl font-semibold text-slate-600">样本太少</div>
                  <div className="text-[10px] text-slate-500">
                    仅 {w.independent} 个不重叠观测（需 ≥ {br.min_independent}），不给频率
                  </div>
                </>
              )}
              <div className="mt-1 text-[10px] text-slate-600">
                {w.points} 个时点 / {w.independent} 个非重叠观测 / {w.segments} 个事件段 · 最差一次{" "}
                {w.worst_return == null ? "—" : `${w.worst_return.toFixed(0)}%`}
              </div>
            </div>
          );
        })}
      </div>

      <div className="mt-3 space-y-1 rounded border border-slate-800/80 bg-slate-950/40 p-3 text-[11px] text-slate-400">
        <div className="text-slate-300">{current.label}</div>
        <div>
          各窗口只展示相对全样本的描述性差值；未进行配对显著性检验，正差值不称为优势，负差值也不直接证明失效。
        </div>
        {negative.length > 0 && (
          <div>
            但 {negative.map((w) => `${w.weeks} 周`).join(" 与 ")}尺度的中位收益为负
            （{negative.map((w) => `${w.median_return?.toFixed(0)}%`).join("、")}），
            这些是终点收益，未计算路径内 MAE，不能据此推断中途是否还会创新低。
          </div>
        )}
        {worst?.worst_return != null && (
          <div>
            同类状态最差的一次：{worst.weeks} 周后仍为 {worst.worst_return.toFixed(0)}%。
          </div>
        )}
      </div>

      <details className="mt-3">
        <summary className="cursor-pointer text-[11px] text-slate-400 hover:text-slate-200">
          其他条件与分档检验（含压力/确认层单调性、口径局限）
        </summary>
        <div className="mt-2 space-y-3 text-[11px]">
          <table className="w-full">
            <thead>
              <tr className="text-left text-[10px] text-slate-500">
                <th className="pb-1">条件</th>
                {br.forward_weeks.map((w) => <th key={w} className="pb-1 text-right">{w}周胜率</th>)}
                <th className="pb-1 text-right">不重叠观测</th>
              </tr>
            </thead>
            <tbody>
              {[br.baseline, ...br.conditions].map((cond) => (
                <tr key={cond.label} className="border-t border-slate-800/60">
                  <td className="py-1.5 text-slate-300">{cond.label}</td>
                  {cond.windows.map((w) => (
                    <td key={w.weeks} className={`py-1.5 text-right ${w.reliable ? "text-slate-300" : "text-slate-600"}`}>
                      {w.reliable && w.hit_rate != null ? `${w.hit_rate.toFixed(0)}%` : "样本不足"}
                    </td>
                  ))}
                  <td className="py-1.5 text-right text-slate-500">
                    {cond.windows.map((w) => w.independent).join(" / ")}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {([
            ["压力累积门槛（legacy，同一时点会重复进入多个门槛）", br.stress_ladder],
            ["确认累积门槛（legacy，不用于证明单调性）", br.confirmation_ladder],
          ] as const).map(([title, ladder]) => (
            <div key={title}>
              <div className="mb-1 text-[10px] text-slate-500">{title}</div>
              <div className="flex flex-wrap gap-1.5">
                {ladder.map((step) => (
                  <span key={step.threshold} className="rounded border border-slate-800 px-1.5 py-px text-[10px] text-slate-400">
                    ≥{step.threshold.toFixed(0)}：
                    {step.windows.map((w) => (w.reliable && w.hit_rate != null ? `${w.hit_rate.toFixed(0)}%` : "—")).join(" / ")}
                  </span>
                ))}
              </div>
            </div>
          ))}
          {br.disjoint_bins && (["stress", "confirmation"] as const).map((key) => (
            <div key={key}>
              <div className="mb-1 text-[10px] text-slate-500">
                {key === "stress" ? "Stress" : "Confirmation"} 互斥分箱 · {br.monotonicity?.[key]?.claim ?? "NO_STATISTICAL_CLAIM"}
              </div>
              <div className="space-y-1 text-[10px] text-slate-500">
                {br.disjoint_bins?.[key].map((bin) => (
                  <div key={bin.label}>
                    {bin.label}（raw {bin.points}）：{bin.windows.map((w) => (
                      w.reliable && w.hit_rate != null ? `${w.weeks}周 ${w.hit_rate.toFixed(0)}%/N${w.independent}`
                        : `${w.weeks}周 UNSCORABLE/N${w.independent}`
                    )).join(" · ")}
                  </div>
                ))}
                {Object.entries(br.monotonicity?.[key]?.by_weeks ?? {}).map(([weeks, item]) => (
                  <div key={weeks}>{weeks}周单调性：{item.status}（可用箱 {item.reliable_bins}）</div>
                ))}
              </div>
            </div>
          ))}
          <ul className="space-y-1 text-[10px] text-slate-500">
            {br.caveats.map((item, i) => <li key={i}>· {item}</li>)}
          </ul>
        </div>
      </details>
    </div>
  );
}

/** 相关性与重复计分声明（折叠）；v2 快照无 correlation_audit 时不渲染 */
function CorrelationPanel({ audit }: { audit: CorrelationAudit }) {
  const strongTotal = audit.groups.reduce((sum, g) => sum + g.strong_pairs, 0);
  return (
    <details className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
      <summary className="cursor-pointer text-[12px] font-medium text-slate-300 hover:text-white">
        相关性与重复计分声明
        <span className="ml-2 text-[10px] font-normal text-slate-500">
          {strongTotal} 对子信号高度相关（|ρ| ≥ 0.70），它们不是彼此独立的证据
        </span>
      </summary>
      <div className="mt-3 space-y-3 text-[11px]">
        <div className="text-[10px] text-slate-500">
          在最近 {audit.window_days} 天的重叠交易日上实测。把高相关的两个指标分别计入
          「支持底部的证据」等于把一份证据数了两遍——这是模型自身也无法避免的结构性
          局限，因此在此显式声明。
        </div>
        {audit.groups.map((group) => (
          <div key={group.key} className="rounded border border-slate-800/80 bg-slate-950/40 p-2.5">
            <div className="text-slate-300">
              {group.label}
              <span className="ml-2 text-[10px] text-slate-500">最大 |ρ| {group.max_abs_rho.toFixed(2)}</span>
            </div>
            <div className="mt-0.5 text-[10px] text-slate-500">{group.note}</div>
            <div className="mt-1.5 flex flex-wrap gap-1.5">
              {group.pairs.map((pair) => (
                <span key={`${pair.a}-${pair.b}`}
                  className={`rounded border px-1.5 py-px text-[10px] ${
                    Math.abs(pair.rho) >= 0.7
                      ? "border-rose-900/70 bg-rose-950/30 text-rose-300/90"
                      : "border-slate-800 text-slate-500"}`}>
                  {pair.a} ↔ {pair.b} {pair.rho >= 0 ? "+" : ""}{pair.rho.toFixed(2)}
                </span>
              ))}
            </div>
          </div>
        ))}
        {(audit.structural_redundancies ?? []).map((item) => (
          <div key={item.topic} className="text-slate-400">
            <span className="text-slate-300">{item.topic}</span>：{item.basis} → {item.conclusion}
          </div>
        ))}
        <div className="space-y-1">
          <div className="text-slate-300">跨层重复使用清单</div>
          {audit.cross_layer_overlaps.map((item) => (
            <div key={item.topic} className="text-[10px] text-slate-500">
              · <span className="text-slate-400">{item.topic}</span>：{item.usage}。{item.note}
            </div>
          ))}
        </div>
      </div>
    </details>
  );
}

function splitHistoryVersions(items: HistoryItem[]): HistoryItem[][] {
  const segments: HistoryItem[][] = [];
  for (const item of items) {
    const version = `${item.model_id ?? item.algorithm_version ?? "legacy"}|${item.data_policy_id ?? "legacy"}`;
    const lastSegment = segments.length ? segments[segments.length - 1] : undefined;
    const previous = lastSegment?.[lastSegment.length - 1];
    const previousVersion = previous
      ? `${previous.model_id ?? previous.algorithm_version ?? "legacy"}|${previous.data_policy_id ?? "legacy"}`
      : null;
    if (!segments.length || version !== previousVersion) segments.push([]);
    segments[segments.length - 1].push(item);
  }
  return segments;
}

function QuadrantChart({ history, current }: { history: HistoryItem[]; current: Snapshot }) {
  const W = 260, H = 220, PAD = 30;
  const x = (stress: number) => PAD + (stress / 100) * (W - 2 * PAD);
  const y = (conf: number) => H - PAD - (conf / 100) * (H - 2 * PAD);
  const trail = history
    .filter((h) => h.stress?.score != null && h.confirmation?.score != null)
    .slice(-90);
  const trailSegments = splitHistoryVersions(trail);
  const cs = current.stress?.score ?? null;
  const cc = current.confirmation?.score ?? null;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full max-w-[320px]">
      {/* 象限分割：stress=55 / confirmation=35、65 */}
      <rect x={PAD} y={PAD} width={W - 2 * PAD} height={H - 2 * PAD}
        className="fill-slate-950 stroke-slate-800" strokeWidth={1} />
      <line x1={x(55)} y1={PAD} x2={x(55)} y2={H - PAD} className="stroke-slate-700" strokeDasharray="3 3" />
      <line x1={PAD} y1={y(35)} x2={W - PAD} y2={y(35)} className="stroke-slate-700" strokeDasharray="3 3" />
      <line x1={PAD} y1={y(65)} x2={W - PAD} y2={y(65)} className="stroke-slate-700" strokeDasharray="3 3" />
      <text x={x(25)} y={y(15)} className="fill-slate-600 text-[8px]" textAnchor="middle">压力不足</text>
      <text x={x(78)} y={y(15)} className="fill-rose-500/70 text-[8px]" textAnchor="middle">高压/确认不足</text>
      <text x={x(78)} y={y(50)} className="fill-amber-500/70 text-[8px]" textAnchor="middle">早期确认</text>
      <text x={x(78)} y={y(83)} className="fill-emerald-500/70 text-[8px]" textAnchor="middle">多项确认</text>
      {trailSegments.map((segment, index) => segment.length >= 2 && (
        <polyline key={`${segment[0].day}-${index}`}
          points={segment.map((h) => `${x(h.stress!.score)},${y(h.confirmation!.score!)}`).join(" ")}
          className="fill-none stroke-sky-500/40" strokeWidth={1.2} />
      ))}
      {cs != null && cc != null && (
        <circle cx={x(cs)} cy={y(cc)} r={5} className="fill-sky-400 stroke-slate-950" strokeWidth={1.5} />
      )}
      <text x={W / 2} y={H - 8} className="fill-slate-500 text-[9px]" textAnchor="middle">Bottom Stress →</text>
      <text x={10} y={H / 2} className="fill-slate-500 text-[9px]" textAnchor="middle"
        transform={`rotate(-90 10 ${H / 2})`}>Confirmation →</text>
    </svg>
  );
}

function HistoryChart({ history }: { history: HistoryItem[] }) {
  const W = 600, H = 160, PAD = 24;
  const items = history.filter((h) => h.stress?.score != null);
  if (items.length < 2) {
    return <div className="text-[11px] text-slate-600">历史快照不足（每日新增一条，运行数日后出现曲线）</div>;
  }
  const x = (i: number) => PAD + (i / (items.length - 1)) * (W - 2 * PAD);
  const y = (v: number) => H - PAD - (v / 100) * (H - 2 * PAD);
  const segments = splitHistoryVersions(items);
  const line = (segment: HistoryItem[], pick: (h: HistoryItem) => number | null | undefined) =>
    segment.map((h) => {
      const v = pick(h);
      return v == null ? null : `${x(items.indexOf(h))},${y(v)}`;
    }).filter(Boolean).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <rect x={PAD} y={PAD} width={W - 2 * PAD} height={H - 2 * PAD}
        className="fill-slate-950 stroke-slate-800" strokeWidth={1} />
      {[25, 50, 75].map((g) => (
        <line key={g} x1={PAD} y1={y(g)} x2={W - PAD} y2={y(g)} className="stroke-slate-800/70" strokeDasharray="2 4" />
      ))}
      {segments.map((segment, index) => (
        <g key={`${segment[0].day}-${index}`}>
          <polyline points={line(segment, (h) => h.stress?.score)} className="fill-none stroke-rose-400" strokeWidth={1.5} />
          <polyline points={line(segment, (h) => h.confirmation?.score)} className="fill-none stroke-emerald-400" strokeWidth={1.5} />
        </g>
      ))}
      <text x={PAD} y={12} className="fill-rose-400 text-[9px]">— Stress</text>
      <text x={PAD + 60} y={12} className="fill-emerald-400 text-[9px]">— Confirmation</text>
      <text x={PAD} y={H - 6} className="fill-slate-600 text-[8px]">{items[0].day}</text>
      <text x={W - PAD} y={H - 6} className="fill-slate-600 text-[8px]" textAnchor="end">
        {items[items.length - 1].day}
      </text>
    </svg>
  );
}

/** 折叠区块右上角的复制小按钮；失败时提示手动全选（HTTP 环境剪贴板可能受限） */
function CopyInlineButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "ok" | "fail">("idle");
  const onClick = useCallback(async (e: React.MouseEvent) => {
    // summary 内嵌按钮：阻止触发 details 展开/收起
    e.preventDefault();
    e.stopPropagation();
    const ok = await copyTextToClipboard(text);
    setState(ok ? "ok" : "fail");
    setTimeout(() => setState("idle"), 2500);
  }, [text]);
  return (
    <button type="button" onClick={onClick} disabled={!text}
      className={`rounded border px-2 py-0.5 text-[10px] transition ${
        state === "ok" ? "border-emerald-600 bg-emerald-900/40 text-emerald-300"
        : state === "fail" ? "border-rose-600 bg-rose-900/40 text-rose-300"
        : "border-slate-700 bg-slate-900/60 text-slate-400 hover:border-slate-500 disabled:opacity-40"}`}>
      {state === "ok" ? "✓ 已复制" : state === "fail" ? "复制失败·请展开后手动全选" : "📋 复制"}
    </button>
  );
}

/** AI 证据包透明化面板：内容直接可见 + 一键复制，剪贴板受限时可手动全选 */
function EvidencePackPanel({ snapshot }: { snapshot: Snapshot }) {
  const [pack, setPack] = useState("");
  const [packErr, setPackErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(`${API_BASE}/api/bottom-model/evidence-pack?detail=compact`);
        if (!res.ok) throw new Error(String(res.status));
        const text = await res.text();
        if (!cancelled) { setPack(text); setPackErr(""); }
      } catch {
        if (!cancelled) setPackErr("证据包加载失败，可稍后刷新重试");
      }
    })();
    return () => { cancelled = true; };
  }, [snapshot.day]);

  const rawJson = useMemo(() => JSON.stringify(snapshot, null, 2), [snapshot]);
  const rows = [
    { key: "pack", label: "证据包 · 数据+分析指令（Markdown，直接粘贴给 AI）", content: pack, err: packErr },
    { key: "raw", label: "原始数据 JSON（当日快照全量字段）", content: rawJson, err: "" },
  ];

  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
      <div className="mb-1 text-[12px] font-medium text-slate-300">AI 证据包</div>
      <div className="mb-3 text-[10px] text-slate-500">
        展开可查看全文；点复制后粘贴到任意 AI 对话即可分析。若复制失败（HTTP 环境剪贴板受限），展开后手动全选复制。
      </div>
      <div className="space-y-2 text-xs">
        {rows.map((row) => (
          <details key={row.key} className="rounded-lg border border-slate-800 bg-slate-950/40">
            <summary className="flex cursor-pointer items-center justify-between gap-2 px-3 py-2 text-slate-300 hover:text-white">
              <span>{row.label}{row.content ? `（${row.content.length} 字符）` : row.err ? "" : "（加载中…）"}</span>
              <CopyInlineButton text={row.content} />
            </summary>
            {row.err
              ? <div className="border-t border-slate-800 p-3 text-[11px] text-rose-400">{row.err}</div>
              : (
                <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words border-t border-slate-800 p-3 text-[10px] text-slate-500">
                  {row.content || "加载中…"}
                </pre>
              )}
          </details>
        ))}
      </div>
    </div>
  );
}

function CopyEvidenceButton() {
  const [state, setState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const onCopy = useCallback(async () => {
    setState("loading");
    try {
      const res = await fetch(`${API_BASE}/api/bottom-model/evidence-pack?detail=compact`);
      if (!res.ok) throw new Error(String(res.status));
      // HTTP + IP 直连部署下 navigator.clipboard 不可用，走降级复制
      const ok = await copyTextToClipboard(await res.text());
      if (!ok) throw new Error("copy_failed");
      setState("done");
      setTimeout(() => setState("idle"), 2500);
    } catch {
      setState("error");
      setTimeout(() => setState("idle"), 2500);
    }
  }, []);
  return (
    <button type="button" onClick={onCopy} disabled={state === "loading"}
      className={`rounded-md border px-3 py-1.5 text-[12px] transition ${
        state === "done" ? "border-emerald-600 bg-emerald-900/40 text-emerald-300"
        : state === "error" ? "border-rose-600 bg-rose-900/40 text-rose-300"
        : "border-sky-700 bg-sky-900/30 text-sky-300 hover:border-sky-500"}`}>
      {state === "loading" ? "生成中…" : state === "done" ? "✓ 已复制" : state === "error" ? "复制失败" : "📋 复制证据包给 AI"}
    </button>
  );
}

export default function BottomModelPage() {
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [runtimeHealth, setRuntimeHealth] = useState<Record<string, unknown> | null>(null);
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);

  const fetchHealth = useCallback(async (): Promise<Record<string, unknown> | null> => {
    try {
      const res = await fetch(`${API_BASE}/api/bottom-model/health`);
      return res.ok ? await res.json() : null;
    } catch {
      return null;
    }
  }, []);

  const load = useCallback(async () => {
    try {
      const [snapRes, histRes, health] = await Promise.all([
        fetch(`${API_BASE}/api/bottom-model/snapshot`),
        fetch(`${API_BASE}/api/bottom-model/history?limit=400`),
        fetchHealth(),
      ]);
      setRuntimeHealth(health);
      if (!snapRes.ok) {
        if (snapRes.status === 503) {
          // 区分"采集进行中 / 服务未启动 / 等待调度"——首轮冷采集受
          // Coinglass 限流约束，可达十余分钟，期间快照接口持续 503
          if (health?.run_in_progress) {
            setError("首轮采集进行中（受数据源限流约束，约需 5-15 分钟），完成后本页自动刷新");
          } else if (health && health.running === false) {
            setError("底部模型服务未启动（后端仍在暖机，或该模块被禁用）");
          } else {
            setError("模型尚未就绪（等待首轮每日采集；可点「手动运行」立即采集）");
          }
        } else {
          setError(`加载失败：${snapRes.status}`);
        }
        return;
      }
      const snap: Snapshot = await snapRes.json();
      setSnapshot(snap);
      if (histRes.ok) {
        const all: HistoryItem[] = (await histRes.json()).items ?? [];
        setHistory(all.filter((item) => {
          if (snap.model_id && snap.data_policy_id) {
            return item.model_id === snap.model_id && item.data_policy_id === snap.data_policy_id;
          }
          return item.algorithm_version === snap.algorithm_version;
        }));
      }
      setError("");
    } catch {
      setError("无法连接后端");
    }
  }, [fetchHealth]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 120_000);
    return () => clearInterval(timer);
  }, [load]);

  const triggerRun = useCallback(async () => {
    setRunning(true);
    try {
      await fetch(`${API_BASE}/api/bottom-model/run`, { method: "POST" });
      // 轮询后端运行状态直到本轮结束（首轮冷采集可达十余分钟），再取快照
      const deadline = Date.now() + 20 * 60_000;
      while (Date.now() < deadline) {
        await new Promise((resolve) => setTimeout(resolve, 5_000));
        const health = await fetchHealth();
        if (health && !health.run_in_progress) break;
      }
      await load();
    } finally {
      setRunning(false);
    }
  }, [fetchHealth, load]);

  // bottom-v5 直接消费后端与因子层同口径的 EQ 加权维度；本地聚合仅用于旧快照回退。
  const legacyDirectDemand = snapshot
    ? legacyDemandDimension(snapshot, ["coinbase_premium", "spot_net_taker", "etf_momentum"])
    : { score: null, eq: null };
  const legacyLiquidityAmmo = snapshot
    ? legacyDemandDimension(snapshot, ["stablecoin_growth", "exchange_outflow"])
    : { score: null, eq: null };
  const directDemand = snapshot?.demand_dimensions?.direct_spot_demand
    ? {
        score: snapshot.demand_dimensions.direct_spot_demand.score,
        eq: snapshot.demand_dimensions.direct_spot_demand.evidence_quality,
      }
    : legacyDirectDemand;
  const liquidityAmmo = snapshot?.demand_dimensions?.liquidity_ammunition
    ? {
        score: snapshot.demand_dimensions.liquidity_ammunition.score,
        eq: snapshot.demand_dimensions.liquidity_ammunition.evidence_quality,
      }
    : legacyLiquidityAmmo;
  const lastRun = runtimeHealth?.last_run_summary as Record<string, unknown> | null | undefined;
  const lastRunBlocked = lastRun?.snapshot_persisted === false;
  const dq = snapshot?.data_quality;
  const dqIssues = useMemo(() => {
    if (!dq) return 0;
    return dq.missing.length + dq.stale.length + Object.keys(dq.failed_fetches ?? {}).length;
  }, [dq]);

  return (
    // bottom-model-page：globals.css 白名单标记，恢复 body 滚动（主页大屏默认 overflow hidden）
    <div className="bottom-model-page min-h-screen bg-slate-950 px-4 py-5 text-slate-200">
      <div className="mx-auto max-w-6xl space-y-5">
        {/* 头部 */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-3">
              <Link href="/" className="text-[12px] text-slate-500 hover:text-slate-300">← 主页</Link>
              <h1 className="text-lg font-semibold text-slate-100">BTC 熊市底部证据与验证模型</h1>
            </div>
            <div className="mt-1 text-[11px] text-slate-500">
              日级慢变量 · 规则引擎（无 AI 参与）· 数据日 {snapshot?.day ?? "—"} · {snapshot?.algorithm_version ?? ""}
              {dqIssues > 0 && <span className="ml-2 text-amber-400">⚠ {dqIssues} 项数据质量问题（见页底）</span>}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button type="button" onClick={triggerRun} disabled={running}
              className="rounded-md border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-[12px] text-slate-300 hover:border-slate-500 disabled:opacity-50">
              {running ? "运行中…" : "↻ 手动运行"}
            </button>
            <CopyEvidenceButton />
          </div>
        </div>

        {error && (
          <div className="rounded-md border border-amber-800/60 bg-amber-950/40 px-4 py-3 text-[12px] text-amber-300">{error}</div>
        )}

        {snapshot && (
          <div className="rounded-md border border-amber-800/60 bg-amber-950/30 px-4 py-3 text-[11px] text-amber-200">
            <span className="font-semibold">验证结论：{snapshot.validation_status ?? "INSUFFICIENT_EVIDENCE"}</span>
            <span className="ml-2 text-amber-300/80">
              当前输出是规则分数与历史描述频率，不是已校准底部概率；probability = {snapshot.prediction?.probability ?? "null"}。
            </span>
            <div className="mt-1 text-[10px] text-slate-400">
              数据状态 {snapshot.quality_status ?? "LEGACY"}
              {(snapshot.blocking_reasons?.length ?? 0) > 0 && ` · 阻断：${snapshot.blocking_reasons?.join("、")}`}
              {` · 审计匹配 ${snapshot.audit_match?.status ?? (snapshot.audit_id ? "LEGACY" : "NO_MATCHING_AUDIT")}`}
              {snapshot.audit_id && ` · ${snapshot.audit_id}`}
              {snapshot.model_id && ` · ${snapshot.model_id}/${snapshot.data_policy_id}`}
            </div>
          </div>
        )}

        {snapshot && lastRunBlocked && (
          <div className="rounded-md border border-rose-900/60 bg-rose-950/30 px-4 py-3 text-[11px] text-rose-200">
            当前展示的是最后有效快照；最近一轮因数据问题未覆盖它。
            <span className="ml-2 text-rose-300/80">
              阻断原因：{Array.isArray(lastRun?.blocking_reasons)
                ? (lastRun.blocking_reasons as string[]).join("、") : "INVALID_DATA"}
            </span>
          </div>
        )}

        {snapshot && (
          <>
            {/* 小白结论横幅 */}
            <VerdictBanner snapshot={snapshot} />

            {/* 四仪表盘：压力极端不等于底部——把"卖方是否卖完""需求是否接管"
                与压力并列，分歧本身比单一综合分更有信息量 */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              <ScoreDial label="市场压力（有多惨）" score={snapshot.stress?.score ?? null}
                eq={snapshot.stress?.evidence_quality ?? null}
                sub={`Δ7d ${fmtSigned(snapshot.delta.stress_7d)} · Δ30d ${fmtSigned(snapshot.delta.stress_30d)}`} />
              <ScoreDial label="卖方衰竭（卖完了吗）" score={snapshot.seller_exhaustion?.score ?? null}
                sub={snapshot.seller_exhaustion
                  ? Object.entries(snapshot.seller_exhaustion.components)
                    .map(([k, v]) => `${EXHAUSTION_LABEL[k] ?? k} ${v.toFixed(0)}`).join(" · ")
                  : "数据不足"} />
              <ScoreDial label="直接现货需求（谁在买）" score={directDemand.score}
                eq={directDemand.eq}
                sub="Coinbase 溢价 · 现货 taker · ETF 流" />
              <ScoreDial label="流动性弹药（能不能买）" score={liquidityAmmo.score}
                eq={liquidityAmmo.eq}
                sub="稳定币与交易所余额，仅作弹药/持币代理，不代表已进场" />
              <ScoreDial label="改善确认（开始好转吗）" score={snapshot.confirmation.score}
                eq={snapshot.confirmation.evidence_quality ?? null}
                sub={`假底惩罚前 ${fmt(snapshot.confirmation.score_before_penalty)} · Δ7d ${fmtSigned(snapshot.delta.confirmation_7d)}`} />
            </div>

            <div className="grid gap-3 md:grid-cols-[1fr_1fr]">
              <div className={`rounded-lg border px-4 py-3 ${QUADRANT_TONE[snapshot.quadrant.key] ?? QUADRANT_TONE.unknown}`}>
                <div className="text-[11px] opacity-70">四象限状态</div>
                <div className="mt-1 text-xl font-semibold">{snapshot.quadrant.label}</div>
                <div className="mt-1 text-[10px] opacity-70">{snapshot.quadrant.note}</div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3 text-[11px]">
                <div className="text-slate-500">价格上下文</div>
                <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-slate-300">
                  <div>现价 <span className="text-slate-100">{fmtValue(snapshot.price_context.price)}</span></div>
                  <div>200W 均线 {fmtValue(snapshot.price_context.ma_200w)}</div>
                  <div>STH 成本 {fmtValue(snapshot.price_context.sth_realized_price)}</div>
                  <div>LTH 成本 {fmtValue(snapshot.price_context.lth_realized_price)}</div>
                </div>
              </div>
            </div>

            {/* 四象限图 + 历史曲线 */}
            <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">四象限轨迹（近 90 天）</div>
                <QuadrantChart history={history} current={snapshot} />
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">历史评分曲线</div>
                <HistoryChart history={history} />
              </div>
            </div>

            {/* 六因子明细 */}
            <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
              <div className="mb-1 text-[12px] font-medium text-slate-300">六因子明细</div>
              <div className="mb-3 text-[10px] text-slate-500">
                评分 0-100，越高越符合历史底部特征；分位为 3y/5y/全历史混合（窗口不足自动退化）。
                EQ 为证据质量，已作为子信号聚合权重——EQ 低于 50 的行会淡化并标注原因。
              </div>
              <div className="grid gap-4 lg:grid-cols-2">
                {snapshot.factors.map((factor) => (
                  <div key={factor.key} className="rounded-md border border-slate-800/80 bg-slate-950/50 p-3">
                    <div className="mb-2 flex items-center justify-between gap-2">
                      <div className="text-[12px] font-medium text-slate-200">
                        {factor.label}
                        <span className="ml-2 text-[10px] text-slate-500">权重 {(factor.weight * 100).toFixed(0)}% · 覆盖 {(factor.coverage * 100).toFixed(0)}%</span>
                      </div>
                      <div className="flex items-center gap-2">
                        <EqBadge eq={factor.evidence_quality ?? null} />
                        <div className={`text-lg font-semibold ${scoreTone(factor.score)}`}>
                          {factor.score == null ? "弃权" : factor.score.toFixed(1)}
                        </div>
                      </div>
                    </div>
                    <table className="w-full text-[11px]">
                      <tbody>
                        {factor.sub_signals.map((sub) => {
                          const weak = sub.evidence_quality != null && sub.evidence_quality < 50;
                          return (
                            <tr key={sub.key} className={`border-t border-slate-800/60 ${weak ? "opacity-60" : ""}`}>
                              <td className="py-1 pr-2 text-slate-400" title={sub.note}>
                                {sub.label}
                                {weak && (
                                  <span className="ml-1 rounded border border-rose-900/60 px-1 text-[9px] text-rose-400/80"
                                    title={`${sub.eq_note || ""}｜${EQ_HINT}`}>窗口不足一个周期</span>
                                )}
                              </td>
                              <td className="py-1 pr-2 text-right text-slate-300">{fmtValue(sub.value)}</td>
                              <td className="py-1 pr-2 text-right text-slate-500">{sub.percentile == null ? "—" : `${sub.percentile.toFixed(0)}分位`}</td>
                              <td className="py-1 pr-2 text-right text-slate-500" title={sub.eq_note || EQ_HINT}>
                                {sub.evidence_quality == null ? "—" : `EQ ${sub.evidence_quality.toFixed(0)}`}
                              </td>
                              <td className={`py-1 text-right font-medium ${scoreTone(sub.score)}`}>{sub.score == null ? "—" : sub.score.toFixed(0)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                ))}
              </div>
            </div>

            {/* 确认信号 + 假底过滤器 */}
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">
                  确认信号（快变量）
                  <span className="ml-2 text-[10px] font-normal text-slate-500">按 EQ 加权汇总，短窗口指标自动降权</span>
                </div>
                <div className="space-y-1.5">
                  {snapshot.confirmation.checks.map((check) => (
                    <div key={check.key} className="flex items-start justify-between gap-2 text-[11px]">
                      <span className="text-slate-400">{check.label}
                        {check.state && <span className="ml-1 rounded border border-slate-700 px-1 text-[9px] text-slate-500">{check.state}</span>}
                        {check.note && <span className="ml-1 text-[10px] text-slate-600">{check.note}</span>}
                      </span>
                      <span className="flex shrink-0 items-center gap-1.5">
                        <EqBadge eq={check.evidence_quality ?? null} />
                        <span className={`font-medium ${scoreTone(check.score)}`}>
                          {check.status === "UNSCORABLE" ? "UNSCORABLE" : check.score == null ? "—" : check.score.toFixed(0)}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">
                  假底过滤器
                  <span className="ml-2 text-[10px] text-slate-500">触发项对 Confirmation 施加惩罚（不否决 Stress）</span>
                </div>
                <div className="space-y-1.5">
                  {(snapshot.fake_bottom_filter.evaluations ?? snapshot.fake_bottom_filter.triggers.map((item) => ({
                    ...item, status: "TRIGGERED" as const, coverage: 1, inputs: {}, thresholds: {},
                  }))).map((item) => (
                    <div key={item.key} className={`rounded border px-2 py-1.5 text-[11px] ${
                      item.status === "TRIGGERED" ? "border-rose-800/50 bg-rose-950/30"
                        : item.status === "UNSCORABLE" ? "border-amber-900/50 bg-amber-950/20"
                        : "border-slate-800 bg-slate-950/30"
                    }`}>
                      <span className={item.status === "TRIGGERED" ? "font-medium text-rose-300" : "text-slate-400"}>
                        {item.label} · {item.status} · 覆盖 {((item.coverage ?? 0) * 100).toFixed(0)}%
                        {item.status === "TRIGGERED" ? `（-${item.penalty}）` : ""}
                      </span>
                      <span className="ml-1 text-slate-500">{item.note}</span>
                      <div className="mt-0.5 text-[9px] text-slate-600">
                        输入 {JSON.stringify(item.inputs ?? {})} · 阈值 {JSON.stringify(item.thresholds ?? {})}
                      </div>
                    </div>
                  ))}
                  {(snapshot.fake_bottom_filter.evaluations?.length ?? snapshot.fake_bottom_filter.triggers.length) === 0 && (
                    <div className="text-[11px] text-amber-400/80">没有检查明细，不能确认过滤器覆盖</div>
                  )}
                </div>
              </div>
            </div>

            {/* 反证清单 */}
            {snapshot.counter_evidence.clusters ? (
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">
                  去重后的八维对抗证据
                  <span className="ml-2 text-[10px] font-normal text-slate-500">每个经济维度最多计一次，成员仅供展开核查</span>
                </div>
                <div className="grid gap-2 md:grid-cols-2">
                  {snapshot.counter_evidence.clusters.map((cluster) => (
                    <details key={cluster.key} className="rounded border border-slate-800 bg-slate-950/40 p-2.5">
                      <summary className="cursor-pointer text-[11px] text-slate-300">
                        {cluster.label}
                        <span className={`ml-2 ${
                          cluster.stance === "supporting" ? "text-emerald-400"
                            : cluster.stance === "opposing" ? "text-rose-400"
                            : cluster.stance === "mixed" ? "text-amber-400" : "text-slate-500"
                        }`}>{cluster.stance}</span>
                        <span className="ml-2 text-[10px] text-slate-600">
                          主证据 {cluster.primary?.label ?? "数据不足"}
                          {cluster.primary?.score != null ? ` ${cluster.primary.score.toFixed(0)}` : ""}
                        </span>
                      </summary>
                      <div className="mt-2 space-y-1 text-[10px] text-slate-500">
                        {cluster.members.map((item, index) => (
                          <div key={`${item.layer}-${item.key}-${index}`}>
                            · {item.label}：{item.status === "UNSCORABLE" ? "UNSCORABLE" : item.score?.toFixed(0) ?? "—"}
                            {item.direction ? ` · ${item.direction}` : ""}{item.note ? ` · ${item.note}` : ""}
                          </div>
                        ))}
                      </div>
                    </details>
                  ))}
                </div>
              </div>
            ) : (
              <div className="grid gap-4 lg:grid-cols-2">
                <div className="rounded-lg border border-emerald-900/50 bg-emerald-950/20 p-4">
                  <div className="mb-2 text-[12px] font-medium text-emerald-300">支持证据（legacy）</div>
                  <ul className="space-y-1 text-[11px] text-slate-400">
                    {snapshot.counter_evidence.supporting.map((item, i) => <li key={i}>· {item}</li>)}
                  </ul>
                </div>
                <div className="rounded-lg border border-rose-900/50 bg-rose-950/20 p-4">
                  <div className="mb-2 text-[12px] font-medium text-rose-300">反对证据（legacy）</div>
                  <ul className="space-y-1 text-[11px] text-slate-400">
                    {snapshot.counter_evidence.opposing.map((item, i) => <li key={i}>· {item}</li>)}
                  </ul>
                </div>
              </div>
            )}

            {/* 历史频率层（v4 起） */}
            {snapshot.base_rate && <BaseRatePanel br={snapshot.base_rate} />}

            {/* 历史类比 */}
            <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
              <div className="mb-2 text-[12px] font-medium text-slate-300">
                历史底部类比
                <span className="ml-2 text-[10px] font-normal text-slate-500">display_only · 仅挑选已知底部，存在选择偏差，不参与评分</span>
              </div>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-left text-[10px] text-slate-500">
                    <th className="pb-1">历史底部</th><th className="pb-1 text-right">相似度</th>
                    <th className="pb-1 text-right">当年 Stress</th><th className="pb-1 text-right">共同因子</th>
                    <th className="pb-1 text-right">数据可比性</th>
                    <th className="pb-1 pl-3">备注</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.analogs.map((analog) => (
                    <tr key={analog.day} className="border-t border-slate-800/60">
                      <td className="py-1.5 text-slate-300">{analog.day} {analog.label}</td>
                      <td className={`py-1.5 text-right font-medium ${scoreTone(analog.similarity)}`}>{fmt(analog.similarity, 0)}</td>
                      <td className="py-1.5 text-right text-slate-400">{fmt(analog.past_stress ?? null)}</td>
                      <td className="py-1.5 text-right text-slate-500">{analog.common_factors.length}/6</td>
                      <td className={`py-1.5 text-right ${RELIABILITY_TONE[analog.reliability ?? ""] ?? "text-slate-500"}`}>
                        {RELIABILITY_LABEL[analog.reliability ?? ""] ?? "—"}
                      </td>
                      <td className="py-1.5 pl-3 text-[10px] text-slate-500">{analog.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* 相关性与重复计分声明（v3 起） */}
            {snapshot.correlation_audit && <CorrelationPanel audit={snapshot.correlation_audit} />}

            {/* AI 证据包透明化 */}
            <EvidencePackPanel snapshot={snapshot} />

            {/* 数据质量 */}
            {dq && dqIssues > 0 && (
              <div className="rounded-lg border border-amber-900/50 bg-amber-950/20 p-4 text-[11px]">
                <div className="mb-2 text-[12px] font-medium text-amber-300">数据质量</div>
                {dq.missing.length > 0 && <div className="text-slate-400">缺失指标：{dq.missing.join("、")}</div>}
                {dq.stale.length > 0 && (
                  <div className="mt-1 text-slate-400">
                    滞后：{dq.stale.map((s) => `${s.metric}（${s.behind_days}天）`).join("、")}
                  </div>
                )}
                {Object.keys(dq.failed_fetches ?? {}).length > 0 && (
                  <div className="mt-1 text-slate-400">
                    采集失败：{Object.entries(dq.failed_fetches).map(([k, v]) => `${k}（${v}）`).join("、")}
                  </div>
                )}
              </div>
            )}

            <div className="pb-4 text-center text-[10px] text-slate-600">
              当前评级：研究型状态指标 · 任何评分都不是概率或交易指令 · 每日 UTC 01:00 自动更新
              <br />
              历史曲线仅连接同一 model_id + data_policy_id；旧版本和不同数据政策的分数不直接连线比较
            </div>
          </>
        )}
      </div>
    </div>
  );
}
