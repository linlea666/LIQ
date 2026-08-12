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
};
type Factor = {
  key: string; label: string; weight: number; score: number | null;
  coverage: number; sub_signals: SubSignal[]; evidence_quality?: number | null;
};
type Check = {
  key: string; label: string; ok: boolean; score: number | null; note: string;
  evidence_quality?: number | null;
};
type Trigger = { key: string; label: string; penalty: number; note: string };
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
type Snapshot = {
  day: string; ts: number; algorithm_version: string;
  price_context: { price: number | null; ma_200w: number | null; sth_realized_price: number | null; lth_realized_price: number | null };
  stress: { score: number; active_weight: number; abstained: string[]; evidence_quality?: number | null } | null;
  confirmation: { score: number | null; score_before_penalty: number | null; checks: Check[]; evidence_quality?: number | null };
  evidence_quality?: { stress: number | null; confirmation: number | null; overall: number | null };
  correlation_audit?: CorrelationAudit;
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
  { key: "bear_market", label: "熊市进行" },
  { key: "panic_flush", label: "恐慌出清" },
  { key: "basing", label: "筑底改善" },
  { key: "confirmed_recovery", label: "确认恢复" },
] as const;

// 结论措辞刻意写成"陈述已发生的事实 + 尚未发生的事实"，不预测概率：
// 历史大底样本仅 4 个，模型无法支撑"概率上升"这类断言。
const VERDICT: Record<string, string> = {
  bear_market: "还没到底部区域",
  panic_flush: "正在恐慌出清（左侧酝酿期）",
  basing: "压力已到极端，改善尚未确认",
  confirmed_recovery: "压力极端 + 改善信号共振",
  unknown: "数据不足，暂无法判断",
};

/** 三段式陈述：压力 / 卖方衰竭 / 需求，各自只说当前观测到的事实 */
function verdictStatements(snapshot: Snapshot): string[] {
  const stress = snapshot.stress?.score ?? null;
  const exhaustion = snapshot.seller_exhaustion?.score ?? null;
  const demand = snapshot.factors.find((f) => f.key === "demand")?.score ?? null;
  const stage = snapshot.confirmation.checks.find((c) => c.key === "structure_stage");
  const lines: string[] = [];
  lines.push(
    stress == null ? "市场压力：数据不足"
      : stress >= 70 ? `市场压力 ${stress.toFixed(0)}：已达历史极端区（多项估值/投降指标处于历史低分位）`
      : stress >= 55 ? `市场压力 ${stress.toFixed(0)}：已进入极端区，但未及历次大底的最深处`
      : `市场压力 ${stress.toFixed(0)}：尚未进入历史极端区`,
  );
  lines.push(
    exhaustion == null ? "卖方衰竭：数据不足"
      : exhaustion >= 70 ? `卖方衰竭 ${exhaustion.toFixed(0)}：抛压已显著枯竭`
      : exhaustion >= 45 ? `卖方衰竭 ${exhaustion.toFixed(0)}：抛压在减弱，但尚未确认枯竭`
      : `卖方衰竭 ${exhaustion.toFixed(0)}：抛压仍未衰竭`,
  );
  lines.push(
    demand == null ? "流动性/需求：数据不足"
      : demand >= 60 ? `流动性/需求 ${demand.toFixed(0)}：新增买盘已在接管`
      : demand >= 40 ? `流动性/需求 ${demand.toFixed(0)}：弹药在积累，但买盘尚未接管`
      : `流动性/需求 ${demand.toFixed(0)}：新需求尚未接管`,
  );
  if (stage?.note) lines.push(`价格结构：${stage.note}`);
  return lines;
}

function VerdictBanner({ snapshot }: { snapshot: Snapshot }) {
  const qkey = snapshot.quadrant.key;
  const headline = VERDICT[qkey] ?? VERDICT.unknown;
  const stageIdx = STAGES.findIndex((s) => s.key === qkey);
  const pendingChecks = snapshot.confirmation.checks.filter(
    (c) => c.ok && (c.score ?? 0) < 100,
  );
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
              距离「底部确认」还差：{pendingChecks.map((c) => c.label).join("、")}
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
        const res = await fetch(`${API_BASE}/api/bottom-model/evidence-pack`);
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
      const res = await fetch(`${API_BASE}/api/bottom-model/evidence-pack`);
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
      const [snapRes, histRes] = await Promise.all([
        fetch(`${API_BASE}/api/bottom-model/snapshot`),
        fetch(`${API_BASE}/api/bottom-model/history?limit=400`),
      ]);
      if (!snapRes.ok) {
        if (snapRes.status === 503) {
          // 区分"采集进行中 / 服务未启动 / 等待调度"——首轮冷采集受
          // Coinglass 限流约束，可达十余分钟，期间快照接口持续 503
          const health = await fetchHealth();
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
      setSnapshot(await snapRes.json());
      if (histRes.ok) setHistory((await histRes.json()).items ?? []);
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

  const demandFactor = snapshot?.factors.find((f) => f.key === "demand");
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
            {/* 小白结论横幅 */}
            <VerdictBanner snapshot={snapshot} />

            {/* 四仪表盘：压力极端不等于底部——把"卖方是否卖完""需求是否接管"
                与压力并列，分歧本身比单一综合分更有信息量 */}
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
              <ScoreDial label="市场压力（有多惨）" score={snapshot.stress?.score ?? null}
                eq={snapshot.stress?.evidence_quality ?? null}
                sub={`Δ7d ${fmtSigned(snapshot.delta.stress_7d)} · Δ30d ${fmtSigned(snapshot.delta.stress_30d)}`} />
              <ScoreDial label="卖方衰竭（卖完了吗）" score={snapshot.seller_exhaustion?.score ?? null}
                sub={snapshot.seller_exhaustion
                  ? Object.entries(snapshot.seller_exhaustion.components)
                    .map(([k, v]) => `${EXHAUSTION_LABEL[k] ?? k} ${v.toFixed(0)}`).join(" · ")
                  : "数据不足"} />
              <ScoreDial label="流动性/需求（谁在买）" score={demandFactor?.score ?? null}
                eq={demandFactor?.evidence_quality ?? null}
                sub={demandFactor ? `覆盖 ${(demandFactor.coverage * 100).toFixed(0)}% · 稳定币弹药≠已进场买盘` : "数据不足"} />
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
                        {check.note && <span className="ml-1 text-[10px] text-slate-600">{check.note}</span>}
                      </span>
                      <span className="flex shrink-0 items-center gap-1.5">
                        <EqBadge eq={check.evidence_quality ?? null} />
                        <span className={`font-medium ${scoreTone(check.score)}`}>{check.score == null ? "—" : check.score.toFixed(0)}</span>
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
                <div className="mb-2 text-[12px] font-medium text-emerald-300">
                  支持底部的证据（{snapshot.counter_evidence.supporting.length}）
                  {/* 条数看着多，是因为同一份证据被多个指标重复表达（如估值簇），
                      不加这句提示会让读者把条数当成证据强度 */}
                  <span className="ml-2 text-[10px] font-normal text-slate-500">
                    条数 ≠ 独立证据数，见下方相关性声明
                  </span>
                </div>
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
              <div className="mb-2 text-[12px] font-medium text-slate-300">
                历史底部类比
                <span className="ml-2 text-[10px] font-normal text-slate-500">共同因子仅 3-6 个，低可信度的类比只能当线索</span>
              </div>
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="text-left text-[10px] text-slate-500">
                    <th className="pb-1">历史底部</th><th className="pb-1 text-right">相似度</th>
                    <th className="pb-1 text-right">当年 Stress</th><th className="pb-1 text-right">共同因子</th>
                    <th className="pb-1 text-right">可信度</th>
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
              历史大底样本仅 4 个，任何评分都不是交易指令 · 每日 UTC 01:00 自动更新 · 单一信号只加分不否决
              <br />
              历史曲线跨 bottom-v2 / v3 两个算法版本：v3 起子信号按证据质量加权、
              周线结构改为分阶段并只计入确认层，两版分数不完全可比
            </div>
          </>
        )}
      </div>
    </div>
  );
}
