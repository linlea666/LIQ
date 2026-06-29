"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

import { API_BASE } from "@/lib/constants";

type Scores = { valuation: number; capital_flow: number; acceptance: number };
type Position = {
  bucket: "core" | "swing" | "tail";
  cash_usdt: number;
  btc_quantity: number;
  average_cost_usdt: number;
  realized_pnl_usdt: number;
};
type Opportunity = {
  opportunity_id: string;
  stage: string;
  bucket: "core" | "swing" | "tail";
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
};
type LedgerEvent = {
  event_id: string;
  event_type: "fill" | "reversal";
  side?: "buy" | "sell";
  bucket?: string;
  quantity_btc: number;
  price_usdt: number;
  fee_usdt: number;
  executed_at: number;
  note: string;
  reverses_event_id?: string | null;
  policy_override: boolean;
};
type AccumulationConfig = {
  initial_capital_usdt: number;
  core_budget_usdt: number;
  swing_budget_usdt: number;
  tail_budget_usdt: number;
  max_swing_loss_usdt: number;
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
    realized_pnl_usdt: number;
    core_bonus_from_swing_usdt: number;
    buckets: Record<"core" | "swing" | "tail", Position>;
  };
  opportunities: Opportunity[];
  budget_reserved_usdt: Record<string, number>;
  next_action: string;
  warnings: string[];
  ai_explanation?: string | null;
};

const STAGE_LABEL: Record<string, string> = {
  insurance: "踏空保险",
  value_1: "价值一档",
  deep_value: "深度价值",
  capitulation: "恐慌出清",
  bottom_confirmed: "底部确认",
  tail_extreme: "极端尾部",
  tail_catch_up: "右侧纠错",
  swing: "波段机动",
};

const BUCKET_LABEL = { core: "长期核心", swing: "波段机动", tail: "尾部/纠错" };

function money(value: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 2 }).format(value || 0);
}

async function api(path: string, init?: RequestInit) {
  const response = await fetch(`${API_BASE}${path}`, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(init?.headers || {}) },
    ...init,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText }));
    throw new Error(body.detail || `HTTP ${response.status}`);
  }
  return response.json();
}

export default function SpotAccumulationPage() {
  const params = useParams<{ coin: string }>();
  const coin = (params.coin || "BTC").toUpperCase();
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [events, setEvents] = useState<LedgerEvent[]>([]);
  const [config, setConfig] = useState<AccumulationConfig | null>(null);
  const [capitalInput, setCapitalInput] = useState("");
  const [savingCapital, setSavingCapital] = useState(false);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selected, setSelected] = useState<Opportunity | null>(null);
  const [explaining, setExplaining] = useState(false);

  const load = useCallback(async () => {
    try {
      const [snap, ledger, currentConfig] = await Promise.all([
        api(`/api/spot-accumulation/${coin}/snapshot`),
        api(`/api/spot-accumulation/${coin}/ledger`),
        api("/api/spot-accumulation/config"),
      ]);
      setSnapshot(snap);
      setEvents(ledger.events || []);
      setConfig(currentConfig);
      setCapitalInput((current) => current || String(currentConfig.initial_capital_usdt));
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [coin]);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), 30_000);
    return () => clearInterval(timer);
  }, [load]);

  const active = useMemo(
    () => snapshot?.opportunities.filter((item) => ["eligible", "accepted"].includes(item.status)) || [],
    [snapshot],
  );

  async function decide(item: Opportunity, decision: "accepted" | "skipped") {
    try {
      await api(`/api/spot-accumulation/${coin}/opportunities/${item.opportunity_id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision }),
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

  async function saveCapital(event: FormEvent) {
    event.preventDefault();
    const value = Number(capitalInput);
    if (!Number.isFinite(value) || value <= 0) {
      setError("抄底总资金必须大于 0");
      return;
    }
    setSavingCapital(true);
    try {
      const updated = await api("/api/spot-accumulation/config", {
        method: "PATCH",
        body: JSON.stringify({ initial_capital_usdt: value }),
      });
      setConfig(updated);
      setCapitalInput(String(updated.initial_capital_usdt));
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "资金配置保存失败");
    } finally {
      setSavingCapital(false);
    }
  }

  if (coin !== "BTC") {
    return <div className="p-8 text-rose-300">首版仅支持 BTC。</div>;
  }
  if (loading) return <div className="p-8 text-slate-400">正在装配现货抄底事实…</div>;

  return (
    <main className="min-h-screen bg-slate-950 px-4 py-5 text-slate-100 lg:px-8">
      <div className="mx-auto max-w-[1500px] space-y-5">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-3">
              <Link href="/" className="text-sm text-sky-400 hover:text-sky-300">← LIQ</Link>
              <span className="rounded border border-amber-700/60 bg-amber-950/40 px-2 py-0.5 text-xs text-amber-300">
                手工确认 · 不自动下单
              </span>
            </div>
            <h1 className="mt-2 text-2xl font-bold">BTC 现货动态抄底</h1>
            <p className="mt-1 text-sm text-slate-400">估值V × 资金M × 承接A，规则控制额度，AI只做解释。</p>
          </div>
          <button onClick={explain} disabled={explaining || !snapshot}
            className="rounded-lg bg-violet-700 px-4 py-2 text-sm font-medium hover:bg-violet-600 disabled:opacity-50">
            {explaining ? "解释中…" : "AI解释规则结果"}
          </button>
        </header>

        {error && <div className="rounded-lg border border-rose-800 bg-rose-950/40 p-3 text-sm text-rose-300">{error}</div>}

        <section className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
          <div className="flex flex-wrap items-end justify-between gap-4">
            <div>
              <h2 className="font-semibold">抄底资金配置</h2>
              <p className="mt-1 text-xs text-slate-400">
                这里只控制建议额度和手工账本上限，不连接交易所，也不会执行交易。
              </p>
              {config && (
                <p className="mt-2 text-xs text-slate-500">
                  核心 65%：{money(config.core_budget_usdt)} U · 波段 20%：{money(config.swing_budget_usdt)} U · 尾部 15%：{money(config.tail_budget_usdt)} U · 波段单笔风险上限：{money(config.max_swing_loss_usdt)} U
                </p>
              )}
            </div>
            <form onSubmit={saveCapital} className="flex items-end gap-2">
              <label className="text-xs text-slate-400">
                抄底总资金 USDT
                <input
                  value={capitalInput}
                  onChange={(event) => setCapitalInput(event.target.value)}
                  type="number"
                  min="0.01"
                  step="0.01"
                  className="mt-1 block w-44 rounded border border-slate-700 bg-slate-950 px-3 py-2 text-sm text-white"
                />
              </label>
              <button disabled={savingCapital} className="rounded bg-sky-700 px-4 py-2 text-sm hover:bg-sky-600 disabled:opacity-50">
                {savingCapital ? "保存中…" : "保存资金"}
              </button>
            </form>
          </div>
        </section>

        {snapshot && (
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
                <div className="mt-4 rounded-lg bg-slate-950/70 p-3 text-sm">
                  <div className="font-medium text-slate-200">当前结论</div>
                  <div className="mt-1 text-slate-400">{snapshot.next_action}</div>
                </div>
                <div className="mt-3 flex flex-wrap gap-2">
                  {snapshot.facts.evidence.map((text) => <Chip key={text} text={text} tone="good" />)}
                  {snapshot.facts.hard_vetoes.map((text) => <Chip key={text} text={text} tone="bad" />)}
                </div>
              </div>

              <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
                <div className="flex items-center justify-between">
                  <h2 className="font-semibold">数据质量</h2>
                  <span className={snapshot.facts.data_quality.can_open_new_opportunity ? "text-emerald-400" : "text-amber-400"}>
                    {(snapshot.facts.data_quality.completeness * 100).toFixed(0)}%
                  </span>
                </div>
                <div className="mt-3 h-2 overflow-hidden rounded bg-slate-800">
                  <div className="h-full bg-cyan-500" style={{ width: `${snapshot.facts.data_quality.completeness * 100}%` }} />
                </div>
                <Quality label="缺失" values={snapshot.facts.data_quality.missing_sources} />
                <Quality label="过期" values={snapshot.facts.data_quality.stale_sources} />
                {snapshot.ai_explanation && (
                  <div className="mt-4 rounded-lg border border-violet-900/60 bg-violet-950/30 p-3 text-sm leading-6 text-violet-200">
                    {snapshot.ai_explanation}
                  </div>
                )}
              </div>
            </section>

            <section>
              <h2 className="mb-3 text-lg font-semibold">资金分桶</h2>
              <div className="grid gap-3 md:grid-cols-3">
                {(["core", "swing", "tail"] as const).map((bucket) => {
                  const pos = snapshot.portfolio.buckets[bucket];
                  return (
                    <div key={bucket} className="rounded-xl border border-slate-800 bg-slate-900/70 p-4">
                      <div className="flex items-center justify-between">
                        <span className="font-medium">{BUCKET_LABEL[bucket]}</span>
                        <span className="text-xs text-slate-500">预留 {money(snapshot.budget_reserved_usdt[bucket] || 0)} U</span>
                      </div>
                      <div className="mt-3 text-2xl font-semibold">{money(pos.cash_usdt)} U</div>
                      <div className="mt-2 text-xs text-slate-400">BTC {pos.btc_quantity.toFixed(8)} · 均价 {pos.average_cost_usdt ? `$${money(pos.average_cost_usdt)}` : "—"}</div>
                    </div>
                  );
                })}
              </div>
            </section>

            <section>
              <div className="mb-3 flex items-center justify-between">
                <h2 className="text-lg font-semibold">动态机会</h2>
                <span className="text-xs text-slate-500">当前可处理 {active.length} 项</span>
              </div>
              <div className="grid gap-3 xl:grid-cols-2">
                {snapshot.opportunities.map((item) => (
                  <OpportunityCard key={item.opportunity_id} item={item}
                    onAccept={() => void decide(item, "accepted")}
                    onSkip={() => void decide(item, "skipped")}
                    onFill={() => setSelected(item)} />
                ))}
              </div>
            </section>

            <Ledger events={events} coin={coin} reload={load} setError={setError} />
          </>
        )}
      </div>
      {selected && <FillDialog coin={coin} item={selected} close={() => setSelected(null)} reload={load} setError={setError} />}
    </main>
  );
}

function Tile({ label, value }: { label: string; value: string }) {
  return <div className="rounded-xl border border-slate-800 bg-slate-900/70 p-4"><div className="text-xs text-slate-500">{label}</div><div className="mt-2 text-lg font-semibold">{value}</div></div>;
}

function Score({ label, value, color }: { label: string; value: number; color: string }) {
  return <div><div className="flex justify-between text-xs"><span>{label}</span><span>{value.toFixed(1)}</span></div><div className="mt-2 h-2 overflow-hidden rounded bg-slate-800"><div className={`h-full ${color}`} style={{ width: `${value}%` }} /></div></div>;
}

function Chip({ text, tone }: { text: string; tone: "good" | "bad" }) {
  return <span className={`rounded px-2 py-1 text-xs ${tone === "good" ? "bg-emerald-950 text-emerald-300" : "bg-rose-950 text-rose-300"}`}>{text}</span>;
}

function Quality({ label, values }: { label: string; values: string[] }) {
  return <div className="mt-3 text-xs"><span className="text-slate-500">{label}：</span><span className={values.length ? "text-amber-300" : "text-slate-600"}>{values.length ? values.join("、") : "无"}</span></div>;
}

function OpportunityCard({ item, onAccept, onSkip, onFill }: { item: Opportunity; onAccept: () => void; onSkip: () => void; onFill: () => void }) {
  const actionable = ["eligible", "accepted"].includes(item.status);
  return <article className={`rounded-xl border p-4 ${actionable ? "border-emerald-700/60 bg-emerald-950/20" : "border-slate-800 bg-slate-900/70"}`}>
    <div className="flex items-start justify-between gap-3"><div><div className="font-semibold">{STAGE_LABEL[item.stage] || item.stage}</div><div className="mt-1 text-xs text-slate-500">{BUCKET_LABEL[item.bucket]} · {item.status}{item.filled_usdt > 0 ? ` · 已成交 ${money(item.filled_usdt)} U` : ""}</div></div><div className="text-right"><div className="text-lg font-semibold">{money(item.allocation_usdt)} U</div><div className="text-xs text-slate-500">${money(item.price_zone_low)}–${money(item.price_zone_high)}</div></div></div>
    {item.expected_rr && <div className="mt-2 text-xs text-sky-300">RR {item.expected_rr.toFixed(2)} · 止损 ${money(item.structural_stop || 0)} · 目标 ${money(item.target_price || 0)}</div>}
    <div className="mt-3 flex flex-wrap gap-1.5">{item.blocked_by.map((text) => <Chip key={text} text={text} tone="bad" />)}{item.reasons.slice(0, 3).map((text) => <Chip key={text} text={text} tone="good" />)}</div>
    {actionable && <div className="mt-4 flex gap-2">{item.status === "eligible" && <button onClick={onAccept} className="rounded bg-emerald-700 px-3 py-1.5 text-xs hover:bg-emerald-600">接受建议</button>}<button onClick={onFill} className="rounded bg-sky-700 px-3 py-1.5 text-xs hover:bg-sky-600">录入成交</button><button onClick={onSkip} className="rounded bg-slate-700 px-3 py-1.5 text-xs hover:bg-slate-600">跳过</button></div>}
  </article>;
}

function FillDialog({ coin, item, close, reload, setError }: { coin: string; item: Opportunity; close: () => void; reload: () => Promise<void>; setError: (value: string) => void }) {
  const [price, setPrice] = useState(String(item.trigger_price));
  const [quantity, setQuantity] = useState(String(item.allocation_usdt / item.trigger_price));
  const [fee, setFee] = useState("0");
  const [saving, setSaving] = useState(false);
  async function submit(event: FormEvent) {
    event.preventDefault(); setSaving(true);
    try {
      await api(`/api/spot-accumulation/${coin}/fills`, { method: "POST", body: JSON.stringify({ client_event_id: globalThis.crypto.randomUUID(), side: "buy", bucket: item.bucket, quantity_btc: Number(quantity), price_usdt: Number(price), fee_usdt: Number(fee), opportunity_id: item.opportunity_id, note: STAGE_LABEL[item.stage] || item.stage }) });
      close(); await reload();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "成交保存失败"); } finally { setSaving(false); }
  }
  return <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4"><form onSubmit={submit} className="w-full max-w-md space-y-4 rounded-xl border border-slate-700 bg-slate-900 p-5"><div className="flex justify-between"><h3 className="font-semibold">录入真实成交 · {STAGE_LABEL[item.stage]}</h3><button type="button" onClick={close} className="text-slate-500">✕</button></div><label className="block text-xs text-slate-400">成交价<input value={price} onChange={(e) => setPrice(e.target.value)} type="number" step="any" className="mt-1 w-full rounded bg-slate-800 p-2 text-white" /></label><label className="block text-xs text-slate-400">BTC数量<input value={quantity} onChange={(e) => setQuantity(e.target.value)} type="number" step="any" className="mt-1 w-full rounded bg-slate-800 p-2 text-white" /></label><label className="block text-xs text-slate-400">手续费 USDT<input value={fee} onChange={(e) => setFee(e.target.value)} type="number" step="any" className="mt-1 w-full rounded bg-slate-800 p-2 text-white" /></label><div className="text-xs text-amber-300">提交后才会改变真实账本；建议本身不会改变持仓。</div><button disabled={saving} className="w-full rounded bg-sky-700 py-2 hover:bg-sky-600 disabled:opacity-50">{saving ? "保存中…" : "确认成交"}</button></form></div>;
}

function Ledger({ events, coin, reload, setError }: { events: LedgerEvent[]; coin: string; reload: () => Promise<void>; setError: (value: string) => void }) {
  async function reverse(event: LedgerEvent) {
    if (!globalThis.confirm("确认用冲正事件撤销这笔成交？原记录不会删除。")) return;
    try { await api(`/api/spot-accumulation/${coin}/fills/${event.event_id}/reverse`, { method: "POST", body: JSON.stringify({ client_event_id: globalThis.crypto.randomUUID(), note: "前端手工冲正" }) }); await reload(); } catch (cause) { setError(cause instanceof Error ? cause.message : "冲正失败"); }
  }
  const reversed = new Set(events.filter((e) => e.event_type === "reversal").map((e) => e.reverses_event_id));
  return <section><h2 className="mb-3 text-lg font-semibold">成交审计账本</h2><div className="overflow-x-auto rounded-xl border border-slate-800"><table className="w-full text-left text-sm"><thead className="bg-slate-900 text-xs text-slate-500"><tr><th className="p-3">时间</th><th>类型</th><th>分桶</th><th>数量</th><th>价格</th><th>手续费</th><th>操作</th></tr></thead><tbody>{events.slice().reverse().map((event) => <tr key={event.event_id} className="border-t border-slate-800"><td className="p-3 text-xs text-slate-400">{new Date(event.executed_at * 1000).toLocaleString()}</td><td>{event.event_type === "reversal" ? "冲正" : event.side === "buy" ? "买入" : "卖出"}{event.policy_override && <span className="ml-1 text-amber-400">策略外</span>}</td><td>{event.bucket || "—"}</td><td>{event.quantity_btc ? event.quantity_btc.toFixed(8) : "—"}</td><td>{event.price_usdt ? `$${money(event.price_usdt)}` : "—"}</td><td>{money(event.fee_usdt)}</td><td>{event.event_type === "fill" && !reversed.has(event.event_id) && <button onClick={() => void reverse(event)} className="text-xs text-rose-400 hover:text-rose-300">冲正</button>}</td></tr>)}{events.length === 0 && <tr><td colSpan={7} className="p-8 text-center text-slate-600">尚无真实成交</td></tr>}</tbody></table></div></section>;
}
