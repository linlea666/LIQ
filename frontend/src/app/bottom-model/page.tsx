"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";

type SubSignal = {
  key: string; label: string; weight: number; ok: boolean;
  score: number | null; value: number | null; percentile: number | null; note: string;
};
type Factor = {
  key: string; label: string; weight: number; score: number | null;
  coverage: number; sub_signals: SubSignal[];
};
type Check = { key: string; label: string; ok: boolean; score: number | null; note: string };
type Trigger = { key: string; label: string; penalty: number; note: string };
type Analog = {
  day: string; label: string; similarity: number | null;
  common_factors: string[]; past_stress?: number | null; note: string;
};
type Snapshot = {
  day: string; ts: number; algorithm_version: string;
  price_context: { price: number | null; ma_200w: number | null; sth_realized_price: number | null; lth_realized_price: number | null };
  stress: { score: number; active_weight: number; abstained: string[] } | null;
  confirmation: { score: number | null; score_before_penalty: number | null; checks: Check[] };
  fake_bottom_filter: { triggers: Trigger[]; total_penalty: number };
  quadrant: { key: string; label: string; note: string };
  seller_exhaustion: { score: number; components: Record<string, number> } | null;
  factors: Factor[];
  counter_evidence: { supporting: string[]; opposing: string[] };
  analogs: Analog[];
  delta: { stress_7d: number | null; stress_30d: number | null; confirmation_7d: number | null; confirmation_30d: number | null };
  data_quality: { ok: boolean; missing: string[]; stale: { metric: string; last_day: string; behind_days: number }[]; failed_fetches: Record<string, string> };
};
type HistoryItem = {
  day: string;
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
const QUADRANT_TONE: Record<string, string> = {
  bear_market: "border-slate-600 bg-slate-800/60 text-slate-300",
  panic_flush: "border-rose-700/60 bg-rose-900/30 text-rose-300",
  basing: "border-amber-700/60 bg-amber-900/30 text-amber-300",
  confirmed_recovery: "border-emerald-700/60 bg-emerald-900/30 text-emerald-300",
  unknown: "border-slate-700 bg-slate-900 text-slate-500",
};

function ScoreDial({ label, score, sub }: { label: string; score: number | null; sub?: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3">
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className={`mt-1 text-3xl font-semibold ${scoreTone(score)}`}>{fmt(score)}</div>
      {sub && <div className="mt-1 text-[10px] text-slate-500">{sub}</div>}
    </div>
  );
}

function QuadrantChart({ history, current }: { history: HistoryItem[]; current: Snapshot }) {
  const W = 260, H = 220, PAD = 30;
  const x = (stress: number) => PAD + (stress / 100) * (W - 2 * PAD);
  const y = (conf: number) => H - PAD - (conf / 100) * (H - 2 * PAD);
  const trail = history
    .filter((h) => h.stress?.score != null && h.confirmation?.score != null)
    .slice(-90);
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
      <text x={x(25)} y={y(15)} className="fill-slate-600 text-[8px]" textAnchor="middle">熊市进行</text>
      <text x={x(78)} y={y(15)} className="fill-rose-500/70 text-[8px]" textAnchor="middle">恐慌出清</text>
      <text x={x(78)} y={y(50)} className="fill-amber-500/70 text-[8px]" textAnchor="middle">筑底改善</text>
      <text x={x(78)} y={y(83)} className="fill-emerald-500/70 text-[8px]" textAnchor="middle">确认恢复</text>
      {trail.length >= 2 && (
        <polyline
          points={trail.map((h) => `${x(h.stress!.score)},${y(h.confirmation!.score!)}`).join(" ")}
          className="fill-none stroke-sky-500/40" strokeWidth={1.2}
        />
      )}
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
  const line = (pick: (h: HistoryItem) => number | null | undefined) =>
    items.map((h, i) => {
      const v = pick(h);
      return v == null ? null : `${x(i)},${y(v)}`;
    }).filter(Boolean).join(" ");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full">
      <rect x={PAD} y={PAD} width={W - 2 * PAD} height={H - 2 * PAD}
        className="fill-slate-950 stroke-slate-800" strokeWidth={1} />
      {[25, 50, 75].map((g) => (
        <line key={g} x1={PAD} y1={y(g)} x2={W - PAD} y2={y(g)} className="stroke-slate-800/70" strokeDasharray="2 4" />
      ))}
      <polyline points={line((h) => h.stress?.score)} className="fill-none stroke-rose-400" strokeWidth={1.5} />
      <polyline points={line((h) => h.confirmation?.score)} className="fill-none stroke-emerald-400" strokeWidth={1.5} />
      <text x={PAD} y={12} className="fill-rose-400 text-[9px]">— Stress</text>
      <text x={PAD + 60} y={12} className="fill-emerald-400 text-[9px]">— Confirmation</text>
      <text x={PAD} y={H - 6} className="fill-slate-600 text-[8px]">{items[0].day}</text>
      <text x={W - PAD} y={H - 6} className="fill-slate-600 text-[8px]" textAnchor="end">
        {items[items.length - 1].day}
      </text>
    </svg>
  );
}

function CopyEvidenceButton() {
  const [state, setState] = useState<"idle" | "loading" | "done" | "error">("idle");
  const onCopy = useCallback(async () => {
    setState("loading");
    try {
      const res = await fetch(`${API_BASE}/api/bottom-model/evidence-pack`);
      if (!res.ok) throw new Error(String(res.status));
      await navigator.clipboard.writeText(await res.text());
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
  const [error, setError] = useState("");
  const [running, setRunning] = useState(false);

  const load = useCallback(async () => {
    try {
      const [snapRes, histRes] = await Promise.all([
        fetch(`${API_BASE}/api/bottom-model/snapshot`),
        fetch(`${API_BASE}/api/bottom-model/history?limit=400`),
      ]);
      if (!snapRes.ok) {
        setError(snapRes.status === 503 ? "模型尚未就绪（等待首轮每日采集，或后端未启动）" : `加载失败：${snapRes.status}`);
        return;
      }
      setSnapshot(await snapRes.json());
      if (histRes.ok) setHistory((await histRes.json()).items ?? []);
      setError("");
    } catch {
      setError("无法连接后端");
    }
  }, []);

  useEffect(() => {
    load();
    const timer = setInterval(load, 120_000);
    return () => clearInterval(timer);
  }, [load]);

  const triggerRun = useCallback(async () => {
    setRunning(true);
    try {
      await fetch(`${API_BASE}/api/bottom-model/run`, { method: "POST" });
      // 采集含限流 spacing，最长数分钟；延迟刷新拿新快照
      setTimeout(() => { load(); setRunning(false); }, 20_000);
    } catch {
      setRunning(false);
    }
  }, [load]);

  const dq = snapshot?.data_quality;
  const dqIssues = useMemo(() => {
    if (!dq) return 0;
    return dq.missing.length + dq.stale.length + Object.keys(dq.failed_fetches ?? {}).length;
  }, [dq]);

  return (
    <div className="min-h-screen bg-slate-950 px-4 py-5 text-slate-200">
      <div className="mx-auto max-w-6xl space-y-5">
        {/* 头部 */}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <div className="flex items-center gap-3">
              <Link href="/" className="text-[12px] text-slate-500 hover:text-slate-300">← 主页</Link>
              <h1 className="text-lg font-semibold text-slate-100">BTC 熊市底部概率模型</h1>
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
          <>
            {/* 结论行 */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-5">
              <ScoreDial label="Bottom Stress（市场压力）" score={snapshot.stress?.score ?? null}
                sub={`Δ7d ${fmtSigned(snapshot.delta.stress_7d)} · Δ30d ${fmtSigned(snapshot.delta.stress_30d)}`} />
              <ScoreDial label="Confirmation（改善确认）" score={snapshot.confirmation.score}
                sub={`假底惩罚前 ${fmt(snapshot.confirmation.score_before_penalty)} · Δ7d ${fmtSigned(snapshot.delta.confirmation_7d)}`} />
              <div className={`rounded-lg border px-4 py-3 ${QUADRANT_TONE[snapshot.quadrant.key] ?? QUADRANT_TONE.unknown}`}>
                <div className="text-[11px] opacity-70">四象限状态</div>
                <div className="mt-1 text-xl font-semibold">{snapshot.quadrant.label}</div>
                <div className="mt-1 text-[10px] opacity-70">{snapshot.quadrant.note}</div>
              </div>
              <ScoreDial label="卖方衰竭指数" score={snapshot.seller_exhaustion?.score ?? null}
                sub={snapshot.seller_exhaustion
                  ? Object.entries(snapshot.seller_exhaustion.components).map(([k, v]) => `${k} ${v.toFixed(0)}`).join(" · ")
                  : "数据不足"} />
              <div className="rounded-lg border border-slate-800 bg-slate-900/70 px-4 py-3 text-[11px]">
                <div className="text-slate-500">价格上下文</div>
                <div className="mt-1 space-y-0.5 text-slate-300">
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
              <div className="mb-3 text-[10px] text-slate-500">评分 0-100，越高越符合历史底部特征；分位为 3y/5y/全历史混合（窗口不足自动退化）</div>
              <div className="grid gap-4 lg:grid-cols-2">
                {snapshot.factors.map((factor) => (
                  <div key={factor.key} className="rounded-md border border-slate-800/80 bg-slate-950/50 p-3">
                    <div className="mb-2 flex items-center justify-between">
                      <div className="text-[12px] font-medium text-slate-200">
                        {factor.label}
                        <span className="ml-2 text-[10px] text-slate-500">权重 {(factor.weight * 100).toFixed(0)}% · 覆盖 {(factor.coverage * 100).toFixed(0)}%</span>
                      </div>
                      <div className={`text-lg font-semibold ${scoreTone(factor.score)}`}>
                        {factor.score == null ? "弃权" : factor.score.toFixed(1)}
                      </div>
                    </div>
                    <table className="w-full text-[11px]">
                      <tbody>
                        {factor.sub_signals.map((sub) => (
                          <tr key={sub.key} className="border-t border-slate-800/60">
                            <td className="py-1 pr-2 text-slate-400" title={sub.note}>{sub.label}</td>
                            <td className="py-1 pr-2 text-right text-slate-300">{fmtValue(sub.value)}</td>
                            <td className="py-1 pr-2 text-right text-slate-500">{sub.percentile == null ? "—" : `${sub.percentile.toFixed(0)}分位`}</td>
                            <td className={`py-1 text-right font-medium ${scoreTone(sub.score)}`}>{sub.score == null ? "—" : sub.score.toFixed(0)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                ))}
              </div>
            </div>

            {/* 确认信号 + 假底过滤器 */}
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">确认信号（快变量）</div>
                <div className="space-y-1.5">
                  {snapshot.confirmation.checks.map((check) => (
                    <div key={check.key} className="flex items-center justify-between text-[11px]">
                      <span className="text-slate-400">{check.label}
                        {check.note && <span className="ml-1 text-[10px] text-slate-600">{check.note}</span>}
                      </span>
                      <span className={`font-medium ${scoreTone(check.score)}`}>{check.score == null ? "—" : check.score.toFixed(0)}</span>
                    </div>
                  ))}
                </div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
                <div className="mb-2 text-[12px] font-medium text-slate-300">
                  假底过滤器
                  <span className="ml-2 text-[10px] text-slate-500">触发项对 Confirmation 施加惩罚（不否决 Stress）</span>
                </div>
                {snapshot.fake_bottom_filter.triggers.length === 0 ? (
                  <div className="text-[11px] text-emerald-400/80">✓ 无触发</div>
                ) : (
                  <div className="space-y-1.5">
                    {snapshot.fake_bottom_filter.triggers.map((trigger) => (
                      <div key={trigger.key} className="rounded border border-rose-800/50 bg-rose-950/30 px-2 py-1.5 text-[11px]">
                        <span className="font-medium text-rose-300">{trigger.label}（-{trigger.penalty}）</span>
                        <span className="ml-1 text-rose-400/70">{trigger.note}</span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </div>

            {/* 反证清单 */}
            <div className="grid gap-4 lg:grid-cols-2">
              <div className="rounded-lg border border-emerald-900/50 bg-emerald-950/20 p-4">
                <div className="mb-2 text-[12px] font-medium text-emerald-300">支持底部的证据（{snapshot.counter_evidence.supporting.length}）</div>
                <ul className="space-y-1 text-[11px] text-slate-400">
                  {snapshot.counter_evidence.supporting.map((item, i) => <li key={i}>· {item}</li>)}
                </ul>
              </div>
              <div className="rounded-lg border border-rose-900/50 bg-rose-950/20 p-4">
                <div className="mb-2 text-[12px] font-medium text-rose-300">反对底部的证据（{snapshot.counter_evidence.opposing.length}）</div>
                <ul className="space-y-1 text-[11px] text-slate-400">
                  {snapshot.counter_evidence.opposing.length === 0
                    ? <li className="text-slate-600">（无）</li>
                    : snapshot.counter_evidence.opposing.map((item, i) => <li key={i}>· {item}</li>)}
                </ul>
              </div>
            </div>

            {/* 历史类比 */}
            <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-4">
              <div className="mb-2 text-[12px] font-medium text-slate-300">历史底部类比</div>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-left text-[10px] text-slate-500">
                    <th className="pb-1">历史底部</th><th className="pb-1 text-right">相似度</th>
                    <th className="pb-1 text-right">当年 Stress</th><th className="pb-1 text-right">共同因子</th>
                    <th className="pb-1 pl-3">备注</th>
                  </tr>
                </thead>
                <tbody>
                  {snapshot.analogs.map((analog) => (
                    <tr key={analog.day} className="border-t border-slate-800/60">
                      <td className="py-1.5 text-slate-300">{analog.day} {analog.label}</td>
                      <td className={`py-1.5 text-right font-medium ${scoreTone(analog.similarity)}`}>{fmt(analog.similarity)}</td>
                      <td className="py-1.5 text-right text-slate-400">{fmt(analog.past_stress ?? null)}</td>
                      <td className="py-1.5 text-right text-slate-500">{analog.common_factors.length}/6</td>
                      <td className="py-1.5 pl-3 text-[10px] text-slate-500">{analog.note}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

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
              历史大底样本仅 4 个，任何评分都不是交易指令 · 每日 UTC 01:00 自动更新 · 单一信号只加分不否决
            </div>
          </>
        )}
      </div>
    </div>
  );
}
