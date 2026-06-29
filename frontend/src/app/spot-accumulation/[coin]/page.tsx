"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { useSpotAccumulationWebSocket } from "@/hooks/useSpotAccumulationWebSocket";
import { API_BASE } from "@/lib/constants";

type Bucket = "core" | "swing" | "tail";
type Scores = { valuation: number; capital_flow: number; acceptance: number };
type MetricFact = {
  value: number | boolean | string | null;
  source_timestamp: number;
  freshness: string;
  parse_status: string;
  included_in_score: boolean;
  score: number | null;
  source: string;
};
type Position = {
  bucket: Bucket;
  cash_usdt: number;
  btc_quantity: number;
  average_cost_usdt: number;
  realized_pnl_usdt: number;
};
type Opportunity = {
  opportunity_id: string;
  stage: string;
  bucket: Bucket;
  allocation_usdt: number;
  reserved_usdt: number;
  filled_usdt: number;
  status: string;
  price_zone_low: number;
  price_zone_high: number;
  trigger_price: number;
  scores: Scores;
  reasons: string[];
  blocked_by: string[];
  structural_stop?: number | null;
  target_price?: number | null;
  expected_rr?: number | null;
  policy_version: number;
  batch_id?: string | null;
  batch_sequence?: number | null;
  accepted_at?: number | null;
  grace_expires_at?: number | null;
};
type LedgerEvent = {
  event_id: string;
  event_type: "fill" | "reversal";
  side?: "buy" | "sell";
  bucket?: Bucket;
  quantity_btc: number;
  price_usdt: number;
  fee_usdt: number;
  executed_at: number;
  note: string;
  reverses_event_id?: string | null;
  policy_override: boolean;
  policy_version: number;
};
type Thresholds = Record<string, { v: number; m: number; a: number }>;
type AccumulationConfig = {
  schema_version: number;
  policy_version: number;
  initial_capital_usdt: number;
  core_ratio: number;
  swing_ratio: number;
  tail_ratio: number;
  insurance_ratio: number;
  core_stage_ratios: Record<string, number>;
  core_thresholds: Thresholds;
  tail_extreme_v: number;
  tail_extreme_a: number;
  tail_catch_up_v: number;
  tail_catch_up_m: number;
  tail_catch_up_a: number;
  min_price_gap_ratio: number;
  atr_gap_multiplier: number;
  acceptance_grace_seconds: number;
  weekly_reclaim_weeks: number;
  max_swing_loss_ratio: number;
  min_swing_rr: number;
  cycle_ath_override?: number | null;
  email_notifications: boolean;
  ai_explanation_enabled: boolean;
  core_budget_usdt: number;
  swing_budget_usdt: number;
  tail_budget_usdt: number;
  max_swing_loss_usdt: number;
};
type ConfigPreview = {
  preview_hash: string | null;
  expected_policy_version: number;
  changed: boolean;
  config: AccumulationConfig;
  budget_changes: Record<Bucket, { before: number; after: number }>;
  invalidated_opportunity_ids: string[];
  errors: string[];
};
type DecisionSummary = {
  state: "blocked" | "conditional" | "eligible" | "accepted" | "complete";
  headline: string;
  detail: string;
  opportunity_id?: string | null;
  stage?: string | null;
  bucket?: Bucket | null;
  amount_usdt?: number | null;
  price_low?: number | null;
  price_high?: number | null;
  estimated_btc?: number | null;
  blockers: string[];
  grace_expires_at?: number | null;
  updated_at: number;
};
type ConditionalLadderItem = {
  stage: string;
  target_usdt: number;
  filled_usdt: number;
  remaining_usdt: number;
  planned_usdt?: number;
  cash_shortfall_usdt?: number;
  status: "waiting_anchor" | "waiting_event" | "conditional" | "eligible" | "accepted" | "partial" | "filled";
  pricing_mode?: "price_ladder" | "event_driven";
  is_actionable: boolean;
  opportunity_id?: string | null;
  reference_price_low?: number | null;
  reference_price_high?: number | null;
  reference_price_mid?: number | null;
  anchor_source: string;
  anchor_label: string;
  support_trust?: number | null;
  blockers: string[];
  invalidation_reasons: string[];
  historical_quantity_btc?: number;
  historical_average_price?: number | null;
  estimated_btc?: number | null;
  projected_total_btc?: number | null;
  projected_average_cost?: number | null;
  projected_cash_remaining: number;
  projected_core_cash_remaining?: number;
  projected_total_cash_remaining?: number;
};
type SupportMapItem = {
  support_id: string;
  price_low: number;
  price_high: number;
  price_mid: number;
  distance_pct: number;
  binance_spot_usd: number;
  coinbase_spot_usd: number;
  spot_wall_usd: number;
  absorption_usd: number;
  absorption_bar_count: number;
  absorption_age_hours?: number | null;
  persistence_1h: number;
  persistence_8h: number;
  max_usd_1h: number;
  max_usd_8h: number;
  support_trust: number;
  support_strength: number;
  support_fragility: number;
  dominant_role: string;
  label: string;
  wall_source_timestamp?: number;
  wall_fresh?: boolean;
  absorption_source_timestamp?: number;
  absorption_fresh?: boolean;
  source_timestamp: number;
  is_fresh: boolean;
  anchor_eligible: boolean;
  evidence: string[];
};
type Snapshot = {
  timestamp: number;
  facts: {
    price: number;
    cycle_ath: number;
    drawdown_pct: number;
    scores: Scores;
    evidence: string[];
    hard_vetoes: string[];
    metric_facts: Record<string, MetricFact>;
    data_quality: {
      completeness: number;
      stale_sources: string[];
      missing_sources: string[];
      can_open_new_opportunity: boolean;
    };
  };
  portfolio: {
    initial_capital_usdt: number;
    total_cash_usdt: number;
    total_btc: number;
    average_cost_usdt: number;
    buckets: Record<Bucket, Position>;
  };
  opportunities: Opportunity[];
  budget_reserved_usdt: Record<Bucket, number>;
  next_action: string;
  warnings: string[];
  ai_explanation?: string | null;
  decision_summary?: DecisionSummary | null;
  conditional_ladder?: ConditionalLadderItem[];
  spot_support_map?: SupportMapItem[];
  view_warnings?: string[];
};
type Health = {
  status: string;
  recovery_required: boolean;
  recovery_errors: string[];
  last_evaluation_error?: string | null;
  policy_version: number;
  schema_version: number;
  view_degraded?: boolean;
  view_warnings?: string[];
};

const CORE_STAGES = ["insurance", "value_1", "deep_value", "capitulation", "bottom_confirmed"];
const STAGE_LABEL: Record<string, string> = {
  insurance: "踏空保险", value_1: "价值一档", deep_value: "深度价值",
  capitulation: "恐慌出清", bottom_confirmed: "底部确认",
  tail_extreme: "极端尾部", tail_catch_up: "右侧纠错", swing: "波段机动",
};
const BUCKET_LABEL: Record<Bucket, string> = {
  core: "长期核心", swing: "波段机动", tail: "尾部/纠错",
};

function money(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value || 0);
}

function compactUsd(value: number) {
  if (value >= 100_000_000) return `${(value / 100_000_000).toFixed(2)}亿`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(value >= 1_000_000 ? 0 : 1)}万`;
  return money(value);
}

function errorText(body: unknown, fallback: string) {
  if (typeof body === "string") return body;
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (detail && typeof detail === "object" && "message" in detail) {
      return String((detail as { message: unknown }).message);
    }
  }
  return fallback;
}

async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(errorText(body, `HTTP ${response.status}`));
  return body as T;
}

function configPatch(config: AccumulationConfig) {
  return {
    initial_capital_usdt: config.initial_capital_usdt,
    core_ratio: config.core_ratio,
    swing_ratio: config.swing_ratio,
    tail_ratio: config.tail_ratio,
    insurance_ratio: config.insurance_ratio,
    core_stage_ratios: config.core_stage_ratios,
    core_thresholds: config.core_thresholds,
    tail_extreme_v: config.tail_extreme_v,
    tail_extreme_a: config.tail_extreme_a,
    tail_catch_up_v: config.tail_catch_up_v,
    tail_catch_up_m: config.tail_catch_up_m,
    tail_catch_up_a: config.tail_catch_up_a,
    min_price_gap_ratio: config.min_price_gap_ratio,
    atr_gap_multiplier: config.atr_gap_multiplier,
    acceptance_grace_seconds: config.acceptance_grace_seconds,
    weekly_reclaim_weeks: config.weekly_reclaim_weeks,
    max_swing_loss_ratio: config.max_swing_loss_ratio,
    min_swing_rr: config.min_swing_rr,
    cycle_ath_override: config.cycle_ath_override || null,
    email_notifications: config.email_notifications,
    ai_explanation_enabled: config.ai_explanation_enabled,
  };
}

export default function SpotAccumulationPage() {
  const params = useParams<{ coin: string }>();
  const coin = (params.coin || "BTC").toUpperCase();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<LedgerEvent[]>([]);
  const [config, setConfig] = useState<AccumulationConfig | null>(null);
  const [draft, setDraft] = useState<AccumulationConfig | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [preview, setPreview] = useState<ConfigPreview | null>(null);
  const [configDirty, setConfigDirty] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Opportunity | null>(null);
  const [manualFill, setManualFill] = useState(false);
  const [explaining, setExplaining] = useState(false);
  const [savingConfig, setSavingConfig] = useState(false);
  const [viewMode, setViewMode] = useState<"novice" | "professional">("novice");

  const load = useCallback(async () => {
    const [snap, ledger, currentConfig, currentHealth] = await Promise.allSettled([
      api<Snapshot>(`/api/spot-accumulation/${coin}/snapshot`),
      api<{ events: LedgerEvent[] }>(`/api/spot-accumulation/${coin}/ledger`),
      api<AccumulationConfig>("/api/spot-accumulation/config"),
      api<Health>(`/api/spot-accumulation/${coin}/health`),
    ]);
    const errors: string[] = [];
    if (snap.status === "fulfilled") setSnapshot(snap.value);
    else errors.push(`快照：${snap.reason instanceof Error ? snap.reason.message : "加载失败"}`);
    if (ledger.status === "fulfilled") setEvents(ledger.value.events || []);
    else errors.push(`账本：${ledger.reason instanceof Error ? ledger.reason.message : "加载失败"}`);
    if (currentConfig.status === "fulfilled") {
      setConfig(currentConfig.value);
      if (!configDirty) setDraft(currentConfig.value);
    } else errors.push(`配置：${currentConfig.reason instanceof Error ? currentConfig.reason.message : "加载失败"}`);
    if (currentHealth.status === "fulfilled") setHealth(currentHealth.value);
    setError(errors.join("；"));
    setLoading(false);
  }, [coin, configDirty]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);
  useSpotAccumulationWebSocket(coin, load);

  const active = useMemo(
    () => snapshot?.opportunities.filter((item) => ["observing", "eligible", "accepted"].includes(item.status)) || [],
    [snapshot],
  );
  const history = useMemo(
    () => snapshot?.opportunities.filter((item) => !["observing", "eligible", "accepted"].includes(item.status)) || [],
    [snapshot],
  );

  function mutateDraft(mutator: (next: AccumulationConfig) => void) {
    setDraft((current) => {
      if (!current) return current;
      const next = structuredClone(current);
      mutator(next);
      return next;
    });
    setConfigDirty(true);
    setPreview(null);
  }

  async function previewConfig(event: FormEvent) {
    event.preventDefault();
    if (!draft || !config) return;
    setSavingConfig(true);
    try {
      const result = await api<ConfigPreview>("/api/spot-accumulation/config/preview", {
        method: "POST",
        body: JSON.stringify({ expected_policy_version: config.policy_version, ...configPatch(draft) }),
      });
      setPreview(result);
      setError(result.errors?.join("；") || "");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置预览失败");
    } finally {
      setSavingConfig(false);
    }
  }

  async function confirmConfig() {
    if (!draft || !config || !preview?.preview_hash || preview.errors.length) return;
    setSavingConfig(true);
    try {
      const updated = await api<AccumulationConfig>("/api/spot-accumulation/config", {
        method: "PATCH",
        body: JSON.stringify({
          expected_policy_version: config.policy_version,
          preview_hash: preview.preview_hash,
          ...configPatch(draft),
        }),
      });
      setConfig(updated);
      setDraft(updated);
      setConfigDirty(false);
      setPreview(null);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "配置保存失败");
    } finally {
      setSavingConfig(false);
    }
  }

  async function decide(item: Opportunity, decision: "accepted" | "skipped") {
    try {
      await api(`/api/spot-accumulation/${coin}/opportunities/${item.opportunity_id}/decision`, {
        method: "POST", body: JSON.stringify({ decision }),
      });
      await load();
      if (decision === "accepted") setSelected(item);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "操作失败");
    }
  }

  async function explain() {
    setExplaining(true);
    try {
      await api(`/api/spot-accumulation/${coin}/explain`, { method: "POST" });
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "AI解释失败");
    } finally {
      setExplaining(false);
    }
  }

  if (coin !== "BTC") return <div className="p-8 text-rose-300">首版仅支持 BTC。</div>;
  if (loading) return <div className="p-8 text-slate-400">正在装配现货抄底事实…</div>;

  return (
    <main className="h-dvh overflow-y-auto overscroll-contain bg-slate-950 px-4 py-5 text-slate-100 lg:px-8">
      <div className="mx-auto max-w-[1500px] space-y-5">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-3">
              <Link href="/" className="text-sm text-sky-400">← LIQ</Link>
              <span className="rounded border border-amber-700/60 bg-amber-950/40 px-2 py-0.5 text-xs text-amber-300">手工确认 · 不自动下单</span>
              {health && <span className="text-xs text-slate-500">策略 v{health.policy_version} · {health.status}</span>}
            </div>
            <h1 className="mt-2 text-2xl font-bold">BTC 现货动态抄底</h1>
            <p className="mt-1 text-sm text-slate-400">可配置动态预算；规则控制建议额度，AI只解释，不改变资金状态。</p>
          </div>
          <div className="flex gap-2">
            <button onClick={() => setManualFill(true)} className="rounded-lg bg-sky-700 px-4 py-2 text-sm">手工录入买卖</button>
            <button onClick={explain} disabled={explaining || !snapshot} className="rounded-lg bg-violet-700 px-4 py-2 text-sm disabled:opacity-50">{explaining ? "解释中…" : "AI解释"}</button>
          </div>
        </header>

        {error && <div className="rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</div>}
        {health?.recovery_required && <HealthFailure health={health} />}
        <nav className="flex w-fit rounded-xl border border-slate-800 bg-slate-900 p-1">
          <button onClick={() => setViewMode("novice")} className={`rounded-lg px-5 py-2 text-sm ${viewMode === "novice" ? "bg-sky-700 text-white" : "text-slate-400"}`}>小白版</button>
          <button onClick={() => setViewMode("professional")} className={`rounded-lg px-5 py-2 text-sm ${viewMode === "professional" ? "bg-slate-700 text-white" : "text-slate-400"}`}>专业版</button>
        </nav>

        {viewMode === "novice" ? (
          snapshot ? <NoviceDashboard
            snapshot={snapshot}
            onAccept={(item) => void decide(item, "accepted")}
            onSkip={(item) => void decide(item, "skipped")}
            onFill={setSelected}
          /> : <Unavailable />
        ) : <>
          {draft && config && <ConfigEditor
            draft={draft} config={config} preview={preview} saving={savingConfig}
            mutate={mutateDraft} onPreview={previewConfig} onConfirm={confirmConfig}
          />}
          {snapshot ? <ProfessionalDashboard
            snapshot={snapshot}
            active={active}
            history={history}
            onAccept={(item) => void decide(item, "accepted")}
            onSkip={(item) => void decide(item, "skipped")}
            onFill={setSelected}
          /> : <Unavailable />}
          <Ledger events={events} coin={coin} reload={load} setError={setError} />
        </>}
      </div>
      {selected && <FillDialog coin={coin} opportunity={selected} close={() => setSelected(null)} reload={load} setError={setError} />}
      {manualFill && <FillDialog coin={coin} close={() => setManualFill(false)} reload={load} setError={setError} />}
    </main>
  );
}

function Unavailable() {
  return <div className="rounded-xl border border-amber-800 bg-amber-950/20 p-5 text-amber-300">行情快照不可用；配置、健康状态和账本仍可独立查看。</div>;
}

function NoviceDashboard({ snapshot, onAccept, onSkip, onFill }: {
  snapshot: Snapshot;
  onAccept: (item: Opportunity) => void;
  onSkip: (item: Opportunity) => void;
  onFill: (item: Opportunity) => void;
}) {
  const decision = snapshot.decision_summary;
  const ladder = snapshot.conditional_ladder || [];
  const opportunityById = new Map(snapshot.opportunities.map((item) => [item.opportunity_id, item]));
  const decisionOpportunity = decision?.opportunity_id
    ? opportunityById.get(decision.opportunity_id)
    : undefined;
  const actionable = decision?.state === "eligible" || decision?.state === "accepted";
  const heroTone = actionable
    ? "border-emerald-700/60 bg-emerald-950/30"
    : decision?.state === "complete"
      ? "border-sky-700/60 bg-sky-950/30"
      : "border-amber-700/60 bg-amber-950/20";
  return <div className="space-y-5">
    <section className={`rounded-2xl border p-5 lg:p-7 ${heroTone}`}>
      <div className="flex flex-wrap items-start justify-between gap-5">
        <div className="max-w-3xl">
          <div className="text-xs font-medium tracking-widest text-slate-400">系统当前结论</div>
          <h2 className={`mt-2 text-3xl font-bold ${actionable ? "text-emerald-300" : "text-amber-300"}`}>{decision?.headline || "当前不买"}</h2>
          <p className="mt-2 text-sm leading-6 text-slate-300">{decision?.detail || snapshot.next_action}</p>
          <div className="mt-4 flex flex-wrap gap-2">
            {(decision?.blockers || []).map((text) => <Chip key={text} text={text} tone="bad" />)}
          </div>
        </div>
        <div className="min-w-[280px] rounded-xl border border-slate-700/70 bg-slate-950/60 p-4">
          <div className="text-xs text-slate-500">{actionable ? "本次允许手工买入" : "下一档条件计划（尚未授权）"}</div>
          <div className="mt-2 text-2xl font-bold">{money(decision?.amount_usdt || 0)} U</div>
          <div className="mt-2 text-sm text-slate-300">
            {decision?.price_low && decision?.price_high
              ? `$${money(decision.price_low)} – $${money(decision.price_high)}`
              : "等待可靠价格锚"}
          </div>
          {decision?.estimated_btc != null && <div className="mt-1 text-xs text-slate-500">约 {decision.estimated_btc.toFixed(8)} BTC</div>}
          {decision?.state === "accepted" && decision.grace_expires_at && <GraceCountdown expiresAt={decision.grace_expires_at} />}
          {decisionOpportunity && <div className="mt-4 flex gap-2">
            {decisionOpportunity.status === "eligible" && <button onClick={() => onAccept(decisionOpportunity)} className="rounded bg-emerald-700 px-3 py-1.5 text-xs">接受建议</button>}
            {decisionOpportunity.status === "accepted" && <button onClick={() => onFill(decisionOpportunity)} className="rounded bg-sky-700 px-3 py-1.5 text-xs">录入成交</button>}
            <button onClick={() => onSkip(decisionOpportunity)} className="rounded bg-slate-700 px-3 py-1.5 text-xs">跳过</button>
          </div>}
        </div>
      </div>
      <div className="mt-5 grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
        <MiniTile label="BTC现价" value={`$${money(snapshot.facts.price)}`} />
        <MiniTile label="周期回撤" value={`-${snapshot.facts.drawdown_pct.toFixed(1)}%`} />
        <MiniTile label="已持有" value={`${snapshot.portfolio.total_btc.toFixed(6)} BTC`} />
        <MiniTile label="当前均价" value={snapshot.portfolio.average_cost_usdt ? `$${money(snapshot.portfolio.average_cost_usdt)}` : "尚未建仓"} />
        <MiniTile label="可用现金" value={`${money(snapshot.portfolio.total_cash_usdt)} U`} />
      </div>
    </section>

    {!!snapshot.view_warnings?.length && <div className="rounded-xl border border-amber-800 bg-amber-950/30 p-3 text-sm text-amber-300">展示层已降级：{snapshot.view_warnings.join("；")}。核心机会、预算和账本仍正常。</div>}

    <section className="grid gap-3 md:grid-cols-3">
      <PlainScore label="便宜程度 V" help="当前价格是否进入长期低估区" value={snapshot.facts.scores.valuation} color="bg-amber-500" />
      <PlainScore label="资金进场 M" help="ETF、净流和稳定币是否支持买入" value={snapshot.facts.scores.capital_flow} color="bg-sky-500" />
      <PlainScore label="现货承接 A" help="是否有真实现货买盘接住卖压" value={snapshot.facts.scores.acceptance} color="bg-emerald-500" />
    </section>

    <section className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div><h2 className="text-lg font-semibold">五档动态抄底计划</h2><p className="mt-1 text-xs text-slate-400">条件价会随结构变化；只有绿色“可买/已接受”才是规则授权，其他档位不占用资金。</p></div>
        <span className="text-xs text-slate-500">更新 {new Date(snapshot.timestamp * 1000).toLocaleString()}</span>
      </div>
      <div className="mt-4 overflow-x-auto">
        <table className="w-full min-w-[1450px] text-left text-sm">
          <thead className="text-xs text-slate-500"><tr><th className="p-3">档位</th><th>历史真实成交</th><th>下一次条件参考</th><th>当前可规划</th><th>预计BTC</th><th>累计BTC/均价</th><th>核心/总现金</th><th>当前状态/还差什么</th><th>操作</th></tr></thead>
          <tbody>{ladder.map((row) => {
            const opportunity = row.opportunity_id ? opportunityById.get(row.opportunity_id) : undefined;
            return <tr key={row.stage} className="border-t border-slate-800 align-top">
              <td className="p-3"><div className="font-medium">{STAGE_LABEL[row.stage] || row.stage}</div><div className="mt-1 text-xs text-slate-600">目标 {money(row.target_usdt)} U · 待完成 {money(row.remaining_usdt)} U</div><div className="mt-1 text-xs text-slate-600">{row.pricing_mode === "event_driven" ? "事件触发定价" : "价格阶梯"}</div></td>
              <td className="py-3"><div>{row.historical_average_price ? `$${money(row.historical_average_price)}` : "尚无成交"}</div><div className="mt-1 text-xs text-slate-600">{money(row.filled_usdt)} U · {(row.historical_quantity_btc || 0).toFixed(8)} BTC</div></td>
              <td className="py-3"><div className={row.is_actionable ? "font-semibold text-emerald-300" : "text-slate-200"}>{row.reference_price_mid ? `$${money(row.reference_price_low || row.reference_price_mid)} – $${money(row.reference_price_high || row.reference_price_mid)}` : row.pricing_mode === "event_driven" ? "等待事件触发" : "等待结构位"}</div><div className="mt-1 max-w-[220px] text-xs text-slate-500">{row.anchor_label || row.invalidation_reasons[0]}</div><div className="mt-1 text-xs text-slate-600">{row.anchor_source || "—"}</div></td>
              <td className="py-3"><div className="font-semibold">{money(row.planned_usdt ?? row.remaining_usdt)} U</div>{(row.cash_shortfall_usdt || 0) > 0 && <div className="mt-1 text-xs text-rose-400">缺口 {money(row.cash_shortfall_usdt || 0)} U</div>}</td>
              <td className="py-3">{row.estimated_btc != null ? row.estimated_btc.toFixed(8) : "—"}</td>
              <td className="py-3"><div>{row.projected_total_btc != null ? row.projected_total_btc.toFixed(8) : "—"}</div><div className="mt-1 text-xs text-slate-500">均价 {row.projected_average_cost ? `$${money(row.projected_average_cost)}` : "—"}</div></td>
              <td className="py-3"><div>核心 {money(row.projected_core_cash_remaining ?? row.projected_cash_remaining)} U</div><div className="mt-1 text-xs text-slate-500">总计 {money(row.projected_total_cash_remaining ?? row.projected_cash_remaining)} U</div></td>
              <td className="max-w-[280px] py-3 pr-3"><StatusBadge status={row.status} /><div className="mt-2 text-xs leading-5 text-slate-500">{row.blockers.slice(0, 2).join("；") || row.invalidation_reasons.join("；") || "条件已满足"}</div></td>
              <td className="py-3">{opportunity && row.status === "eligible" ? <div className="flex gap-2"><button onClick={() => onAccept(opportunity)} className="rounded bg-emerald-700 px-3 py-1.5 text-xs">接受</button><button onClick={() => onSkip(opportunity)} className="rounded bg-slate-700 px-3 py-1.5 text-xs">跳过</button></div> : opportunity && row.status === "accepted" ? <button onClick={() => onFill(opportunity)} className="rounded bg-sky-700 px-3 py-1.5 text-xs">录入成交</button> : <span className="text-xs text-slate-600">不可执行</span>}</td>
            </tr>;
          })}</tbody>
        </table>
      </div>
      {ladder.length === 0 && <div className="py-8 text-center text-sm text-slate-500">服务端尚未生成条件阶梯，请等待下一轮快照。</div>}
    </section>

    <SupportRanking items={snapshot.spot_support_map || []} />

    <section className="grid gap-4 lg:grid-cols-2">
      <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
        <div className="flex justify-between"><h2 className="font-semibold">数据是否可靠</h2><span>{(snapshot.facts.data_quality.completeness * 100).toFixed(0)}%</span></div>
        <Quality label="缺失" values={snapshot.facts.data_quality.missing_sources} />
        <Quality label="过期" values={snapshot.facts.data_quality.stale_sources} />
      </div>
      <div className="rounded-xl border border-violet-900/60 bg-violet-950/20 p-4">
        <h2 className="font-semibold text-violet-200">规则解释</h2>
        <p className="mt-2 text-sm leading-6 text-slate-400">挂单墙是可撤销意图，Footprint吸收是已经成交的事实，两者不会混为同一指标。条件计划仅用于准备资金，AI不会改变价格、额度或机会状态。</p>
        {snapshot.ai_explanation && <div className="mt-3 text-sm text-violet-200">{snapshot.ai_explanation}</div>}
      </div>
    </section>
  </div>;
}

function SupportRanking({ items }: { items: SupportMapItem[] }) {
  const [sort, setSort] = useState<"trust" | "thickness" | "persistence" | "distance">("trust");
  const sorted = useMemo(() => [...items].sort((a, b) => {
    if (sort === "thickness") return b.spot_wall_usd - a.spot_wall_usd;
    if (sort === "persistence") return b.persistence_8h - a.persistence_8h;
    if (sort === "distance") return Math.abs(a.distance_pct) - Math.abs(b.distance_pct);
    return b.support_trust - a.support_trust
      || ((b.absorption_fresh ?? b.is_fresh) ? b.absorption_usd : 0)
      - ((a.absorption_fresh ?? a.is_fresh) ? a.absorption_usd : 0);
  }), [items, sort]);
  return <section className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
    <div className="flex flex-wrap items-end justify-between gap-3">
      <div><h2 className="text-lg font-semibold">现货承接排行榜</h2><p className="mt-1 text-xs text-slate-400">仅展示现价下方5%内的近场事实；远端熊市阶梯使用关键位和估值锚。</p></div>
      <label className="text-xs text-slate-400">排序<select value={sort} onChange={(event) => setSort(event.target.value as typeof sort)} className="ml-2 rounded border border-slate-700 bg-slate-950 px-3 py-2 text-slate-200"><option value="trust">综合可信度</option><option value="thickness">现货墙厚度</option><option value="persistence">持续时间</option><option value="distance">距离现价</option></select></label>
    </div>
    <div className="mt-4 overflow-x-auto"><table className="w-full min-w-[1250px] text-left text-sm"><thead className="text-xs text-slate-500"><tr><th className="p-3">排名/价格</th><th>Binance挂单</th><th>Coinbase挂单</th><th>真实成交吸收</th><th>持续/历史峰值</th><th>强度/可信/脆弱</th><th>判断与证据</th><th>双源时间</th></tr></thead><tbody>{sorted.map((item, index) => <tr key={item.support_id} className="border-t border-slate-800 align-top"><td className="p-3"><div className="font-semibold">#{index + 1} · ${money(item.price_mid)}</div><div className="text-xs text-slate-500">距现价 {item.distance_pct.toFixed(2)}%</div></td><td className="py-3">{compactUsd(item.binance_spot_usd)} U</td><td className="py-3">{compactUsd(item.coinbase_spot_usd)} U</td><td className="py-3"><div>{compactUsd(item.absorption_usd)} U</div><div className="text-xs text-slate-600">{item.absorption_bar_count ? `${item.absorption_bar_count}根足迹` : "暂无吸收"}</div></td><td className="py-3"><div>持续 1h {(item.persistence_1h * 100).toFixed(0)}% · 8h {(item.persistence_8h * 100).toFixed(0)}%</div><div className="mt-1 text-xs text-slate-600">峰值 1h {compactUsd(item.max_usd_1h)} · 8h {compactUsd(item.max_usd_8h)}</div></td><td className="py-3"><div>强度 {(item.support_strength * 100).toFixed(0)}% · 可信 {(item.support_trust * 100).toFixed(0)}%</div><div className={item.support_fragility >= 0.6 ? "mt-1 text-xs text-rose-400" : "mt-1 text-xs text-slate-600"}>脆弱 {(item.support_fragility * 100).toFixed(0)}%</div></td><td className="max-w-[260px] py-3"><span className={`rounded px-2 py-1 text-xs ${item.label.includes("可能被扫") ? "bg-rose-950 text-rose-300" : item.anchor_eligible ? "bg-emerald-950 text-emerald-300" : "bg-slate-800 text-slate-300"}`}>{item.label}</span><div className="mt-2 text-xs leading-5 text-slate-500">{item.evidence.slice(0, 2).join("；") || "暂无附加证据"}</div></td><td className="py-3"><FreshLine label="挂单" fresh={item.wall_fresh ?? item.is_fresh} timestamp={item.wall_source_timestamp || item.source_timestamp} /><FreshLine label="吸收" fresh={item.absorption_fresh ?? item.is_fresh} timestamp={item.absorption_source_timestamp || item.source_timestamp} /></td></tr>)}</tbody></table></div>
    {sorted.length === 0 && <div className="py-8 text-center text-sm text-slate-500">当前没有新鲜的近场现货承接区，不能据此生成买价。</div>}
  </section>;
}

function ProfessionalDashboard({ snapshot, active, history, onAccept, onSkip, onFill }: {
  snapshot: Snapshot;
  active: Opportunity[];
  history: Opportunity[];
  onAccept: (item: Opportunity) => void;
  onSkip: (item: Opportunity) => void;
  onFill: (item: Opportunity) => void;
}) {
  const waitingEventStages = new Set(
    (snapshot.conditional_ladder || [])
      .filter((row) => row.pricing_mode === "event_driven" && row.status === "waiting_event")
      .map((row) => row.stage),
  );
  return <div className="space-y-5">
    <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
      <Tile label="BTC现价" value={`$${money(snapshot.facts.price)}`} />
      <Tile label="周期ATH / 回撤" value={`$${money(snapshot.facts.cycle_ath)} / -${snapshot.facts.drawdown_pct.toFixed(1)}%`} />
      <Tile label="持有BTC" value={snapshot.portfolio.total_btc.toFixed(8)} />
      <Tile label="综合均价" value={snapshot.portfolio.average_cost_usdt ? `$${money(snapshot.portfolio.average_cost_usdt)}` : "尚未建仓"} />
      <Tile label="可用现金" value={`${money(snapshot.portfolio.total_cash_usdt)} U`} />
    </section>
    <section className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
      <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4"><h2 className="font-semibold">三层证据</h2><div className="mt-4 grid grid-cols-3 gap-3"><Score label="V 估值" value={snapshot.facts.scores.valuation} color="bg-amber-500" /><Score label="M 资金" value={snapshot.facts.scores.capital_flow} color="bg-sky-500" /><Score label="A 承接" value={snapshot.facts.scores.acceptance} color="bg-emerald-500" /></div><div className="mt-4 rounded-lg bg-slate-950/70 p-3 text-sm text-slate-400">{snapshot.next_action}</div><div className="mt-3 flex flex-wrap gap-2">{snapshot.facts.evidence.map((text) => <Chip key={text} text={text} tone="good" />)}{snapshot.facts.hard_vetoes.map((text) => <Chip key={text} text={text} tone="bad" />)}</div></div>
      <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4"><div className="flex justify-between"><h2 className="font-semibold">数据质量</h2><span>{(snapshot.facts.data_quality.completeness * 100).toFixed(0)}%</span></div><Quality label="缺失" values={snapshot.facts.data_quality.missing_sources} /><Quality label="过期" values={snapshot.facts.data_quality.stale_sources} />{snapshot.ai_explanation && <div className="mt-4 rounded-lg border border-violet-900/60 bg-violet-950/30 p-3 text-sm text-violet-200">{snapshot.ai_explanation}</div>}</div>
    </section>
    <EvidenceMatrix facts={snapshot.facts.metric_facts} />
    <section><h2 className="mb-3 text-lg font-semibold">资金分桶</h2><div className="grid gap-3 md:grid-cols-3">{(["core", "swing", "tail"] as Bucket[]).map((bucket) => { const pos = snapshot.portfolio.buckets[bucket]; return <div key={bucket} className="rounded-xl border border-slate-800 bg-slate-900/70 p-4"><div className="flex justify-between"><span>{BUCKET_LABEL[bucket]}</span><span className="text-xs text-slate-500">预留 {money(snapshot.budget_reserved_usdt[bucket])} U</span></div><div className="mt-3 text-2xl font-semibold">{money(pos.cash_usdt)} U</div><div className="mt-2 text-xs text-slate-400">BTC {pos.btc_quantity.toFixed(8)} · 均价 {pos.average_cost_usdt ? `$${money(pos.average_cost_usdt)}` : "—"}</div></div>; })}</div></section>
    <section><div className="mb-3 flex justify-between"><h2 className="text-lg font-semibold">当前活动机会</h2><span className="text-xs text-slate-500">{active.length} 项</span></div><div className="grid gap-3 xl:grid-cols-2">{active.map((item) => <OpportunityCard key={item.opportunity_id} item={item} waitingForEvent={waitingEventStages.has(item.stage)} onAccept={() => onAccept(item)} onSkip={() => onSkip(item)} onFill={() => onFill(item)} />)}{active.length === 0 && <div className="text-sm text-slate-500">当前没有活动机会。</div>}</div></section>
    <details className="rounded-xl border border-slate-800 bg-slate-900/40 p-4"><summary className="cursor-pointer text-sm text-slate-300">历史机会（{history.length}）</summary><div className="mt-4 grid gap-3 xl:grid-cols-2">{history.slice(0, 100).map((item) => <OpportunityCard key={item.opportunity_id} item={item} onAccept={() => onAccept(item)} onSkip={() => onSkip(item)} onFill={() => onFill(item)} />)}</div></details>
  </div>;
}

function MiniTile({ label, value }: { label: string; value: string }) { return <div className="rounded-lg bg-slate-950/50 p-3"><div className="text-xs text-slate-500">{label}</div><div className="mt-1 font-semibold">{value}</div></div>; }
function PlainScore({ label, help, value, color }: { label: string; help: string; value: number; color: string }) { return <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4"><div className="flex justify-between"><div><div className="font-medium">{label}</div><div className="mt-1 text-xs text-slate-500">{help}</div></div><div className="text-2xl font-bold">{value.toFixed(0)}</div></div><div className="mt-4 h-2 overflow-hidden rounded bg-slate-800"><div className={`h-full ${color}`} style={{ width: `${value}%` }} /></div></div>; }
function FreshLine({ label, fresh, timestamp }: { label: string; fresh: boolean; timestamp: number }) { return <div className="mb-1 text-xs"><span className={fresh ? "text-emerald-400" : "text-amber-400"}>{label} {timestamp ? fresh ? "新鲜" : "过期" : "无数据"}</span><span className="ml-1 text-slate-600">{timestamp ? new Date(timestamp * 1000).toLocaleTimeString() : "—"}</span></div>; }
function StatusBadge({ status }: { status: ConditionalLadderItem["status"] }) { const labels: Record<ConditionalLadderItem["status"], string> = { waiting_anchor: "等待结构位", waiting_event: "等待事件触发", conditional: "条件观察", eligible: "可买", accepted: "已接受", partial: "部分成交", filled: "已完成" }; const tone = status === "eligible" || status === "accepted" ? "bg-emerald-950 text-emerald-300" : status === "filled" ? "bg-sky-950 text-sky-300" : "bg-slate-800 text-slate-300"; return <span className={`rounded px-2 py-1 text-xs ${tone}`}>{labels[status]}</span>; }

function ConfigEditor({ draft, config, preview, saving, mutate, onPreview, onConfirm }: {
  draft: AccumulationConfig; config: AccumulationConfig; preview: ConfigPreview | null; saving: boolean;
  mutate: (fn: (next: AccumulationConfig) => void) => void;
  onPreview: (event: FormEvent) => void; onConfirm: () => void;
}) {
  const setNumber = (key: keyof AccumulationConfig, value: number) => mutate((next) => { (next[key] as number) = value; });
  return <section className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
    <div className="flex justify-between"><div><h2 className="font-semibold">策略配置 · 当前 v{config.policy_version}</h2><p className="mt-1 text-xs text-slate-400">先预览预算和活动机会影响，再二次确认保存。</p></div></div>
    <form onSubmit={onPreview} className="mt-4 space-y-4">
      <div className="grid gap-3 md:grid-cols-4">
        <NumberField label="总资金 USDT" value={draft.initial_capital_usdt} onChange={(v) => setNumber("initial_capital_usdt", v)} />
        <PercentField label="核心比例" value={draft.core_ratio} onChange={(v) => setNumber("core_ratio", v)} />
        <PercentField label="波段比例" value={draft.swing_ratio} onChange={(v) => setNumber("swing_ratio", v)} />
        <PercentField label="尾部比例" value={draft.tail_ratio} onChange={(v) => setNumber("tail_ratio", v)} />
      </div>
      <div className="overflow-x-auto"><table className="w-full text-sm"><thead className="text-xs text-slate-500"><tr><th className="p-2 text-left">核心档位</th><th>预算%</th><th>V</th><th>M</th><th>A</th></tr></thead><tbody>
        {CORE_STAGES.map((stage) => <tr key={stage} className="border-t border-slate-800"><td className="p-2">{STAGE_LABEL[stage]}</td><td><MiniNumber value={draft.core_stage_ratios[stage] * 100} onChange={(v) => mutate((next) => { next.core_stage_ratios[stage] = v / 100; if (stage === "insurance") next.insurance_ratio = v / 100; })} /></td>{(["v", "m", "a"] as const).map((axis) => <td key={axis}><MiniNumber value={draft.core_thresholds[stage][axis]} onChange={(v) => mutate((next) => { next.core_thresholds[stage][axis] = v; })} /></td>)}</tr>)}
      </tbody></table></div>
      <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6">
        <NumberField label="尾部极端 V" value={draft.tail_extreme_v} onChange={(v) => setNumber("tail_extreme_v", v)} />
        <NumberField label="尾部极端 A" value={draft.tail_extreme_a} onChange={(v) => setNumber("tail_extreme_a", v)} />
        <NumberField label="右侧 V" value={draft.tail_catch_up_v} onChange={(v) => setNumber("tail_catch_up_v", v)} />
        <NumberField label="右侧 M" value={draft.tail_catch_up_m} onChange={(v) => setNumber("tail_catch_up_m", v)} />
        <NumberField label="右侧 A" value={draft.tail_catch_up_a} onChange={(v) => setNumber("tail_catch_up_a", v)} />
        <NumberField label="周线确认数" value={draft.weekly_reclaim_weeks} onChange={(v) => setNumber("weekly_reclaim_weeks", v)} />
        <PercentField label="跨批价格间距" value={draft.min_price_gap_ratio} onChange={(v) => setNumber("min_price_gap_ratio", v)} />
        <NumberField label="ATR倍数" value={draft.atr_gap_multiplier} onChange={(v) => setNumber("atr_gap_multiplier", v)} />
        <NumberField label="接受宽限(分钟)" value={draft.acceptance_grace_seconds / 60} onChange={(v) => setNumber("acceptance_grace_seconds", Math.round(v * 60))} />
        <PercentField label="波段单笔风险" value={draft.max_swing_loss_ratio} onChange={(v) => setNumber("max_swing_loss_ratio", v)} />
        <NumberField label="最低盈亏比" value={draft.min_swing_rr} onChange={(v) => setNumber("min_swing_rr", v)} />
        <NumberField label="周期ATH覆盖" value={draft.cycle_ath_override || 0} onChange={(v) => mutate((next) => { next.cycle_ath_override = v > 0 ? v : null; })} />
      </div>
      <div className="flex flex-wrap gap-4 text-sm"><Check label="AI解释" checked={draft.ai_explanation_enabled} onChange={(v) => mutate((next) => { next.ai_explanation_enabled = v; })} /><Check label="邮件提醒" checked={draft.email_notifications} onChange={(v) => mutate((next) => { next.email_notifications = v; })} /></div>
      <button disabled={saving} className="rounded bg-sky-700 px-4 py-2 text-sm disabled:opacity-50">{saving ? "校验中…" : "预览配置变更"}</button>
    </form>
    {preview && <div className="mt-4 rounded-lg border border-amber-800/60 bg-amber-950/20 p-4 text-sm">
      {preview.errors.length ? <div className="text-rose-300">{preview.errors.join("；")}</div> : <>
        <div className="font-medium">二次确认</div>
        <div className="mt-2 text-slate-400">核心 {money(preview.budget_changes.core.before)} → {money(preview.budget_changes.core.after)} U · 波段 {money(preview.budget_changes.swing.before)} → {money(preview.budget_changes.swing.after)} U · 尾部 {money(preview.budget_changes.tail.before)} → {money(preview.budget_changes.tail.after)} U</div>
        <div className="mt-1 text-amber-300">保存将失效 {preview.invalidated_opportunity_ids.length} 个活动机会，历史成交不修改。</div>
        <button type="button" onClick={onConfirm} disabled={saving || !preview.changed} className="mt-3 rounded bg-amber-700 px-4 py-2 disabled:opacity-50">确认保存并升级策略版本</button>
      </>}
    </div>}
  </section>;
}

function EvidenceMatrix({ facts }: { facts: Record<string, MetricFact> }) {
  return <section><h2 className="mb-3 text-lg font-semibold">完整证据矩阵</h2><div className="overflow-x-auto rounded-xl border border-slate-800"><table className="w-full text-left text-xs"><thead className="bg-slate-900 text-slate-500"><tr><th className="p-3">指标</th><th>原始值</th><th>来源/时间</th><th>状态</th><th>参与评分</th><th>得分</th></tr></thead><tbody>{Object.entries(facts).map(([name, fact]) => <tr key={name} className="border-t border-slate-800"><td className="p-3 text-slate-300">{name}</td><td>{String(fact.value ?? "—")}</td><td>{fact.source || "—"}<div className="text-slate-600">{fact.source_timestamp ? new Date(fact.source_timestamp * 1000).toLocaleString() : "无时间"}</div></td><td>{fact.freshness} / {fact.parse_status}</td><td className={fact.included_in_score ? "text-emerald-400" : "text-amber-400"}>{fact.included_in_score ? "是" : "否，仅参考"}</td><td>{fact.score ?? "—"}</td></tr>)}</tbody></table></div></section>;
}

function OpportunityCard({ item, waitingForEvent = false, onAccept, onSkip, onFill }: { item: Opportunity; waitingForEvent?: boolean; onAccept: () => void; onSkip: () => void; onFill: () => void }) {
  const actionable = ["eligible", "accepted"].includes(item.status);
  const remaining = Math.max(0, item.allocation_usdt - item.filled_usdt);
  return <article className={`rounded-xl border p-4 ${actionable ? "border-emerald-700/60 bg-emerald-950/20" : "border-slate-800 bg-slate-900/70"}`}>
    <div className="flex justify-between gap-3"><div><div className="font-semibold">{STAGE_LABEL[item.stage] || item.stage}</div><div className="mt-1 text-xs text-slate-500">{BUCKET_LABEL[item.bucket]} · {item.status} · 策略v{item.policy_version}{item.batch_sequence ? ` · 批次${item.batch_sequence}` : ""}</div></div><div className="text-right"><div className="text-lg font-semibold">{money(remaining)} U</div><div className="text-xs text-slate-500">剩余 / 总额 {money(item.allocation_usdt)} U</div></div></div>
    {item.status === "accepted" && item.grace_expires_at && <GraceCountdown expiresAt={item.grace_expires_at} />}
    <div className="mt-2 text-xs text-slate-500">{waitingForEvent ? "等待事件触发，当前不展示参考价格" : <>{item.status === "observing" ? "观察价格（不是买入授权）" : "执行价格区"} ${money(item.price_zone_low)}–${money(item.price_zone_high)}</>}</div>
    {item.expected_rr && <div className="mt-2 text-xs text-sky-300">RR {item.expected_rr.toFixed(2)} · 止损 ${money(item.structural_stop || 0)} · 目标 ${money(item.target_price || 0)}</div>}
    <div className="mt-3 flex flex-wrap gap-1.5">{item.blocked_by.map((text) => <Chip key={text} text={text} tone="bad" />)}{item.reasons.slice(0, 3).map((text) => <Chip key={text} text={text} tone="good" />)}</div>
    {actionable && <div className="mt-4 flex gap-2">{item.status === "eligible" && <button onClick={onAccept} className="rounded bg-emerald-700 px-3 py-1.5 text-xs">接受建议</button>}{item.status === "accepted" && <button onClick={onFill} className="rounded bg-sky-700 px-3 py-1.5 text-xs">录入成交</button>}<button onClick={onSkip} className="rounded bg-slate-700 px-3 py-1.5 text-xs">跳过</button></div>}
  </article>;
}

function GraceCountdown({ expiresAt }: { expiresAt: number }) {
  const [now, setNow] = useState(() => Math.floor(Date.now() / 1000));
  useEffect(() => { const timer = setInterval(() => setNow(Math.floor(Date.now() / 1000)), 1_000); return () => clearInterval(timer); }, []);
  const left = Math.max(0, expiresAt - now);
  return <div className="mt-2 text-xs text-amber-300">执行宽限 {Math.floor(left / 60)}:{String(left % 60).padStart(2, "0")}</div>;
}

function FillDialog({ coin, opportunity, close, reload, setError }: { coin: string; opportunity?: Opportunity; close: () => void; reload: () => Promise<void>; setError: (value: string) => void }) {
  const remaining = opportunity ? Math.max(0, opportunity.allocation_usdt - opportunity.filled_usdt) : 0;
  const initialPrice = opportunity?.trigger_price || 0;
  const initialFee = opportunity ? remaining / 1001 : 0;
  const [side, setSide] = useState<"buy" | "sell">("buy");
  const [bucket, setBucket] = useState<Bucket>(opportunity?.bucket || "core");
  const [price, setPrice] = useState(String(initialPrice || ""));
  const [quantity, setQuantity] = useState(opportunity && initialPrice ? String((remaining - initialFee) / initialPrice) : "");
  const [fee, setFee] = useState(opportunity ? String(initialFee) : "0");
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setSaving(true);
    try {
      await api(`/api/spot-accumulation/${coin}/fills`, { method: "POST", body: JSON.stringify({
        client_event_id: globalThis.crypto.randomUUID(), side, bucket,
        quantity_btc: Number(quantity), price_usdt: Number(price), fee_usdt: Number(fee),
        opportunity_id: opportunity?.opportunity_id,
        note: opportunity ? STAGE_LABEL[opportunity.stage] || opportunity.stage : "通用手工成交",
      }) });
      close(); await reload();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "成交保存失败"); } finally { setSaving(false); }
  }
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"><form onSubmit={submit} className="w-full max-w-md space-y-4 rounded-xl border border-slate-700 bg-slate-900 p-5"><div className="flex justify-between"><h3 className="font-semibold">{opportunity ? `录入成交 · ${STAGE_LABEL[opportunity.stage]}` : "通用手工成交"}</h3><button type="button" onClick={close}>✕</button></div>{!opportunity && <div className="grid grid-cols-2 gap-3"><label className="text-xs">方向<select value={side} onChange={(e) => setSide(e.target.value as "buy" | "sell")} className="mt-1 w-full rounded bg-slate-800 p-2"><option value="buy">买入</option><option value="sell">卖出</option></select></label><label className="text-xs">分桶<select value={bucket} onChange={(e) => setBucket(e.target.value as Bucket)} className="mt-1 w-full rounded bg-slate-800 p-2"><option value="core">核心</option><option value="swing">波段</option><option value="tail">尾部</option></select></label></div>}<NumberInput label="成交价" value={price} setValue={setPrice} /><NumberInput label="BTC数量" value={quantity} setValue={setQuantity} /><NumberInput label="手续费 USDT" value={fee} setValue={setFee} />{!opportunity && <div className="text-xs text-amber-300">无关联机会的成交会明确记为策略外；核心卖出同样标记为策略外。</div>}<button disabled={saving} className="w-full rounded bg-sky-700 py-2 disabled:opacity-50">{saving ? "保存中…" : "确认成交"}</button></form></div>;
}

function Ledger({ events, coin, reload, setError }: { events: LedgerEvent[]; coin: string; reload: () => Promise<void>; setError: (value: string) => void }) {
  async function reverse(event: LedgerEvent) {
    if (!globalThis.confirm("确认用冲正事件撤销这笔成交？原记录不会删除。")) return;
    try { await api(`/api/spot-accumulation/${coin}/fills/${event.event_id}/reverse`, { method: "POST", body: JSON.stringify({ client_event_id: globalThis.crypto.randomUUID(), note: "前端手工冲正" }) }); await reload(); } catch (cause) { setError(cause instanceof Error ? cause.message : "冲正失败"); }
  }
  const reversed = new Set(events.filter((event) => event.event_type === "reversal").map((event) => event.reverses_event_id));
  return <section><h2 className="mb-3 text-lg font-semibold">成交审计账本</h2><div className="overflow-x-auto rounded-xl border border-slate-800"><table className="w-full text-left text-sm"><thead className="bg-slate-900 text-xs text-slate-500"><tr><th className="p-3">时间</th><th>类型</th><th>分桶</th><th>数量</th><th>价格</th><th>手续费</th><th>版本</th><th>操作</th></tr></thead><tbody>{events.slice().reverse().map((event) => <tr key={event.event_id} className="border-t border-slate-800"><td className="p-3 text-xs text-slate-400">{new Date(event.executed_at * 1000).toLocaleString()}</td><td>{event.event_type === "reversal" ? "冲正" : event.side === "buy" ? "买入" : "卖出"}{event.policy_override && <span className="ml-1 text-amber-400">策略外</span>}</td><td>{event.bucket || "—"}</td><td>{event.quantity_btc ? event.quantity_btc.toFixed(8) : "—"}</td><td>{event.price_usdt ? `$${money(event.price_usdt)}` : "—"}</td><td>{money(event.fee_usdt)}</td><td>v{event.policy_version}</td><td>{event.event_type === "fill" && !reversed.has(event.event_id) && <button onClick={() => void reverse(event)} className="text-xs text-rose-400">冲正</button>}</td></tr>)}{events.length === 0 && <tr><td colSpan={8} className="p-8 text-center text-slate-600">尚无真实成交</td></tr>}</tbody></table></div></section>;
}

function Tile({ label, value }: { label: string; value: string }) { return <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4"><div className="text-xs text-slate-500">{label}</div><div className="mt-2 text-lg font-semibold">{value}</div></div>; }
function Score({ label, value, color }: { label: string; value: number; color: string }) { return <div><div className="flex justify-between text-xs"><span>{label}</span><span>{value.toFixed(1)}</span></div><div className="mt-2 h-2 overflow-hidden rounded bg-slate-800"><div className={`h-full ${color}`} style={{ width: `${value}%` }} /></div></div>; }
function Chip({ text, tone }: { text: string; tone: "good" | "bad" }) { return <span className={`rounded px-2 py-1 text-xs ${tone === "good" ? "bg-emerald-950 text-emerald-300" : "bg-rose-950 text-rose-300"}`}>{text}</span>; }
function Quality({ label, values }: { label: string; values: string[] }) { return <div className="mt-3 text-xs"><span className="text-slate-500">{label}：</span><span className={values.length ? "text-amber-300" : "text-slate-600"}>{values.length ? values.join("、") : "无"}</span></div>; }
function HealthFailure({ health }: { health: Health }) { return <div className="rounded-lg border border-rose-800 bg-rose-950/30 p-4 text-sm text-rose-300"><div className="font-medium">恢复锁定：停止生成和接受机会</div><div className="mt-1">{health.recovery_errors.join("；") || health.last_evaluation_error}</div></div>; }
function NumberField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) { return <label className="text-xs text-slate-400">{label}<input value={Number.isFinite(value) ? value : ""} onChange={(e) => onChange(Number(e.target.value))} type="number" step="any" className="mt-1 w-full rounded border border-slate-700 bg-slate-950 p-2 text-white" /></label>; }
function PercentField({ label, value, onChange }: { label: string; value: number; onChange: (value: number) => void }) { return <NumberField label={`${label} %`} value={value * 100} onChange={(v) => onChange(v / 100)} />; }
function MiniNumber({ value, onChange }: { value: number; onChange: (value: number) => void }) { return <input value={value} onChange={(e) => onChange(Number(e.target.value))} type="number" step="any" className="m-1 w-20 rounded bg-slate-950 p-1.5 text-center" />; }
function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) { return <label className="flex items-center gap-2"><input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} />{label}</label>; }
function NumberInput({ label, value, setValue }: { label: string; value: string; setValue: (value: string) => void }) { return <label className="block text-xs text-slate-400">{label}<input value={value} onChange={(e) => setValue(e.target.value)} type="number" step="any" required className="mt-1 w-full rounded bg-slate-800 p-2 text-white" /></label>; }
