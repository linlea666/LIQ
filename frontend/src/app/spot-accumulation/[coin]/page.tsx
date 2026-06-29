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
};
type Health = {
  status: string;
  recovery_required: boolean;
  recovery_errors: string[];
  last_evaluation_error?: string | null;
  policy_version: number;
  schema_version: number;
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
    () => snapshot?.opportunities.filter((item) => ["eligible", "accepted"].includes(item.status)) || [],
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
    <main className="min-h-screen bg-slate-950 px-4 py-5 text-slate-100 lg:px-8">
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

        {draft && config && (
          <ConfigEditor
            draft={draft} config={config} preview={preview} saving={savingConfig}
            mutate={mutateDraft} onPreview={previewConfig} onConfirm={confirmConfig}
          />
        )}

        {snapshot ? (
          <>
            <section className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
              <Tile label="BTC现价" value={`$${money(snapshot.facts.price)}`} />
              <Tile label="周期ATH / 回撤" value={`$${money(snapshot.facts.cycle_ath)} / -${snapshot.facts.drawdown_pct.toFixed(1)}%`} />
              <Tile label="持有BTC" value={snapshot.portfolio.total_btc.toFixed(8)} />
              <Tile label="综合均价" value={snapshot.portfolio.average_cost_usdt ? `$${money(snapshot.portfolio.average_cost_usdt)}` : "尚未建仓"} />
              <Tile label="可用现金" value={`${money(snapshot.portfolio.total_cash_usdt)} U`} />
            </section>

            <section className="grid gap-4 lg:grid-cols-[1.2fr_1fr]">
              <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
                <h2 className="font-semibold">三层证据</h2>
                <div className="mt-4 grid grid-cols-3 gap-3">
                  <Score label="V 估值" value={snapshot.facts.scores.valuation} color="bg-amber-500" />
                  <Score label="M 资金" value={snapshot.facts.scores.capital_flow} color="bg-sky-500" />
                  <Score label="A 承接" value={snapshot.facts.scores.acceptance} color="bg-emerald-500" />
                </div>
                <div className="mt-4 rounded-lg bg-slate-950/70 p-3 text-sm text-slate-400">{snapshot.next_action}</div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {snapshot.facts.evidence.map((text) => <Chip key={text} text={text} tone="good" />)}
                  {snapshot.facts.hard_vetoes.map((text) => <Chip key={text} text={text} tone="bad" />)}
                </div>
              </div>
              <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
                <div className="flex justify-between"><h2 className="font-semibold">数据质量</h2><span>{(snapshot.facts.data_quality.completeness * 100).toFixed(0)}%</span></div>
                <Quality label="缺失" values={snapshot.facts.data_quality.missing_sources} />
                <Quality label="过期" values={snapshot.facts.data_quality.stale_sources} />
                {snapshot.ai_explanation && <div className="mt-4 rounded-lg border border-violet-900/60 bg-violet-950/30 p-3 text-sm text-violet-200">{snapshot.ai_explanation}</div>}
              </div>
            </section>

            <EvidenceMatrix facts={snapshot.facts.metric_facts} />

            <section>
              <h2 className="mb-3 text-lg font-semibold">资金分桶</h2>
              <div className="grid gap-3 md:grid-cols-3">
                {(["core", "swing", "tail"] as Bucket[]).map((bucket) => {
                  const pos = snapshot.portfolio.buckets[bucket];
                  return <div key={bucket} className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
                    <div className="flex justify-between"><span>{BUCKET_LABEL[bucket]}</span><span className="text-xs text-slate-500">预留 {money(snapshot.budget_reserved_usdt[bucket])} U</span></div>
                    <div className="mt-3 text-2xl font-semibold">{money(pos.cash_usdt)} U</div>
                    <div className="mt-2 text-xs text-slate-400">BTC {pos.btc_quantity.toFixed(8)} · 均价 {pos.average_cost_usdt ? `$${money(pos.average_cost_usdt)}` : "—"}</div>
                  </div>;
                })}
              </div>
            </section>

            <section>
              <div className="mb-3 flex justify-between"><h2 className="text-lg font-semibold">动态机会</h2><span className="text-xs text-slate-500">当前可处理 {active.length} 项</span></div>
              <div className="grid gap-3 xl:grid-cols-2">
                {snapshot.opportunities.slice(0, 40).map((item) => <OpportunityCard
                  key={item.opportunity_id} item={item}
                  onAccept={() => void decide(item, "accepted")}
                  onSkip={() => void decide(item, "skipped")}
                  onFill={() => setSelected(item)}
                />)}
              </div>
            </section>
          </>
        ) : <div className="rounded-xl border border-amber-800 bg-amber-950/20 p-5 text-amber-300">行情快照不可用；配置、健康状态和账本仍可独立查看。</div>}

        <Ledger events={events} coin={coin} reload={load} setError={setError} />
      </div>
      {selected && <FillDialog coin={coin} opportunity={selected} close={() => setSelected(null)} reload={load} setError={setError} />}
      {manualFill && <FillDialog coin={coin} close={() => setManualFill(false)} reload={load} setError={setError} />}
    </main>
  );
}

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

function OpportunityCard({ item, onAccept, onSkip, onFill }: { item: Opportunity; onAccept: () => void; onSkip: () => void; onFill: () => void }) {
  const actionable = ["eligible", "accepted"].includes(item.status);
  const remaining = Math.max(0, item.allocation_usdt - item.filled_usdt);
  return <article className={`rounded-xl border p-4 ${actionable ? "border-emerald-700/60 bg-emerald-950/20" : "border-slate-800 bg-slate-900/70"}`}>
    <div className="flex justify-between gap-3"><div><div className="font-semibold">{STAGE_LABEL[item.stage] || item.stage}</div><div className="mt-1 text-xs text-slate-500">{BUCKET_LABEL[item.bucket]} · {item.status} · 策略v{item.policy_version}{item.batch_sequence ? ` · 批次${item.batch_sequence}` : ""}</div></div><div className="text-right"><div className="text-lg font-semibold">{money(remaining)} U</div><div className="text-xs text-slate-500">剩余 / 总额 {money(item.allocation_usdt)} U</div></div></div>
    {item.status === "accepted" && item.grace_expires_at && <GraceCountdown expiresAt={item.grace_expires_at} />}
    <div className="mt-2 text-xs text-slate-500">价格区 ${money(item.price_zone_low)}–${money(item.price_zone_high)}</div>
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
