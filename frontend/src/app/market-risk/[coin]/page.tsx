"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type { ContextItem, EvidenceItem, MarketFactor, MarketRiskIntelligence, MarketRiskReady, RiskDirection, RiskStage, SourceQuality } from "@/lib/marketRiskTypes";

const STAGE_LABEL: Record<RiskStage, string> = { normal: "正常", watch: "观察", warning: "预警", critical: "临界", cooldown: "冷却", resolved: "已结束" };
const STANCE = {
  observe_long: { label: "观察偏多", tone: "border-emerald-700/60 bg-emerald-950/45 text-emerald-100" },
  observe_short: { label: "观察偏空", tone: "border-rose-700/60 bg-rose-950/45 text-rose-100" },
  wait: { label: "等待", tone: "border-amber-700/60 bg-amber-950/35 text-amber-100" },
} as const;
const BAND_LABEL = { unavailable: "不可判断", weak: "弱", medium: "中", strong: "强" } as const;
const FACTOR_STATUS = { normal: "正常", unusual: "异常", extreme: "极端", missing: "数据不可用", conflict: "方向冲突" } as const;

function formatTime(ts: number): string {
  if (!ts) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(ts * 1000));
}

function directionLabel(direction: RiskDirection | "neutral"): string {
  if (direction === "up") return "偏上行";
  if (direction === "down") return "偏下行";
  if (direction === "mixed") return "多空冲突";
  if (direction === "neutral") return "中性";
  return "方向未知";
}

function qualityLabel(quality: SourceQuality): string {
  if (quality.decision_usable) return "可参与判断";
  if (quality.continuity === "gap") return "已断流";
  if (quality.freshness === "stale") return "已过期";
  if (quality.validity === "invalid") return "口径无效";
  return "暂不可用";
}

function readableReason(reason: string): string {
  if (reason.startsWith("stale_age_")) return `数据过期（${reason.slice(10)}）`;
  if (reason.startsWith("pit_")) return "发现未来数据，已被时间门禁拒绝";
  const labels: Record<string, string> = {
    source_unavailable: "数据源不可用", missing_as_of: "缺少数据时间", source_gap: "数据流不连续",
    window_incomplete: "统计窗口不完整", current_oi_observation_unavailable: "当前 OI 观测缺失",
    closed_5m_candle_unavailable: "已收盘 5 分钟 K 线不足", cvd_window_stale_or_unclosed: "CVD 过期或尚未收盘",
  };
  return labels[reason] ?? reason;
}

export default function MarketRiskPage({ params }: PageProps<"/market-risk/[coin]">) {
  const { coin: routeCoin } = use(params);
  const coin = routeCoin.toUpperCase();
  const [data, setData] = useState<MarketRiskIntelligence | null>(null);
  const [readiness, setReadiness] = useState<MarketRiskReady | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const [response, readyResponse] = await Promise.all([
        fetch(`${API_BASE}/api/market-risk/${coin}/intelligence`, { cache: "no-store" }),
        fetch(`${API_BASE}/api/market-risk/ready`, { cache: "no-store" }),
      ]);
      if (!response.ok) throw new Error(response.status === 503 ? "情报室正在暖机" : `接口错误 ${response.status}`);
      setData(await response.json() as MarketRiskIntelligence);
      if (readyResponse.ok) setReadiness(await readyResponse.json() as MarketRiskReady);
      setError("");
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "读取失败");
    } finally {
      setLoading(false);
    }
  }, [coin]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const evidence = useMemo(() => [...(data?.incident.evidence ?? [])].sort((a, b) => b.event_time - a.event_time), [data]);
  const stance = data ? STANCE[data.decision_support.stance] : STANCE.wait;
  const trends = data?.context.market_overview?.trend_horizons ?? {};

  return (
    <main className="market-risk-page min-h-screen overflow-x-clip bg-slate-950 text-slate-100">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/95 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-3">
          <div className="min-w-0">
            <div className="flex items-center gap-3">
              <Link href="/" className="shrink-0 text-sm text-slate-400 hover:text-white">← 返回</Link>
              <h1 className="truncate text-base font-semibold sm:text-lg">LIQ {coin} 开仓决策情报室</h1>
            </div>
            <p className="mt-1 hidden text-xs text-slate-500 sm:block">只提供观察方向、证据与失效条件；不计算仓位、不自动下单。</p>
          </div>
          <button onClick={() => void refresh()} className="shrink-0 rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300">刷新</button>
        </div>
      </header>

      <div className="mx-auto max-w-7xl space-y-4 p-4 sm:p-5">
        {loading && <Notice>正在读取最新物化快照…</Notice>}
        {error && <Notice tone="amber">{error}</Notice>}
        {data && (
          <>
            <Notice tone={data.mode === "shadow" ? "violet" : "slate"}>
              当前模式：{data.mode === "shadow" ? "shadow 研究" : data.mode === "production_read_only" ? "生产只读" : "生产预警"}。
              {data.mode === "shadow" && " 修复后的样本需重新累计并通过准入，当前不会发送市场风险邮件。"}
            </Notice>

            {readiness && (
              <section className="rounded-xl border border-slate-800 bg-slate-900/45 p-4 text-xs">
                <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">生产切换进度</h2><span className="text-slate-400">当前只满足：{readiness.ready_for_mode}</span></div>
                <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                  <GateMetric label="修复后 Shadow" value={`${(readiness.governed_shadow_age_sec / 86400).toFixed(1)} 天`} ok={readiness.governed_shadow_age_sec >= 14 * 86400} />
                  <GateMetric label="24h PIT 违规" value={String(readiness.pit_violations_24h)} ok={readiness.pit_violations_24h === 0} />
                  <GateMetric label="核心覆盖率" value={readiness.snapshot_count_24h ? `${(readiness.core_coverage_24h * 100).toFixed(1)}%` : "暖机中"} ok={readiness.core_coverage_24h >= 0.9} />
                  <GateMetric label="队列丢弃" value={String(readiness.raw_queue_dropped)} ok={readiness.raw_queue_dropped === 0} />
                  <GateMetric label="新文件预测" value={`${Math.round(readiness.raw_store.projected_files_per_day ?? 0)}/日`} ok={(readiness.raw_store.projected_files_per_day ?? 0) <= 2000} />
                  <GateMetric label="RSS p95" value={`${readiness.rss_p95_gib.toFixed(2)} GiB / ${Math.floor(readiness.rss_observation_age_sec / 3600)}h 样本`} ok={readiness.rss_observation_age_sec >= 86400 && readiness.rss_p95_gib > 0 && readiness.rss_p95_gib <= 1.3 && readiness.rss_slope_mib_per_hour <= 2} />
                </div>
                {!!readiness.blockers.length && <div className="mt-3 text-amber-300/80">未通过：{readiness.blockers.join("；")}</div>}
              </section>
            )}

            <section className={`rounded-xl border p-5 ${stance.tone}`}>
              <div className="text-xs opacity-70">当前一句话结论</div>
              <div className="mt-2 flex flex-wrap items-end gap-3"><div className="text-3xl font-bold">{stance.label}</div><div className="pb-1 text-sm opacity-80">证据强度：{BAND_LABEL[data.decision_support.strength_band]}</div></div>
              <p className="mt-3 text-sm leading-6">{data.decision_support.summary}</p>
              <div className="mt-3 text-xs opacity-70">execution_eligible = false · 这不是开仓指令</div>
            </section>

            <section className="grid gap-3 lg:grid-cols-2">
              <ReasonPanel title="为什么这样判断" items={data.decision_support.supporting_evidence} empty="当前没有足够的同向计分证据。" tone="emerald" />
              <ReasonPanel title="反方证据" items={data.decision_support.opposing_evidence} empty="当前没有达到异常门槛的反向证据；不代表反方风险为零。" tone="rose" />
            </section>
            <section className="grid gap-3 lg:grid-cols-2">
              <ReasonPanel title="数据是否可信" items={data.decision_support.blockers} empty="核心数据目前通过可用性与时间一致性门禁。" tone={data.decision_support.blockers.length ? "amber" : "emerald"} />
              <ReasonPanel title="这份观察何时失效" items={data.decision_support.invalidation_conditions} empty="—" tone="slate" />
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/45 p-4">
              <div className="flex flex-wrap items-center justify-between gap-2"><h2 className="text-sm font-semibold">市场全景 · 已收盘周期</h2><span className="text-xs text-slate-500">决策时间 {formatTime(data.decision_time)}</span></div>
              <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
                {["1m", "5m", "15m", "1h", "4h", "1d"].map((period) => {
                  const item = trends[period];
                  const available = item?.availability === "available" && item.change_pct !== null;
                  return <div key={period} className="rounded-lg border border-slate-800 bg-slate-950/55 p-3"><div className="text-xs text-slate-500">{period}</div><div className={`mt-1 text-lg font-semibold ${item?.direction === "up" ? "text-emerald-300" : item?.direction === "down" ? "text-rose-300" : "text-slate-400"}`}>{available ? `${item.change_pct! >= 0 ? "+" : ""}${item.change_pct!.toFixed(2)}%` : "不可用"}</div><div className="mt-1 text-[10px] text-slate-600">{available ? "已收盘" : "不作推断"}</div></div>;
                })}
              </div>
            </section>

            <section>
              <div className="mb-2 flex items-center justify-between gap-2"><h2 className="text-sm font-semibold">普通异常与证据因子</h2><span className="text-xs text-slate-500">正常 ≠ 数据不可用 ≠ 仅展示</span></div>
              <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{data.factors.map((factor) => <FactorCard key={factor.factor_id} factor={factor} />)}</div>
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/45 p-4">
              <h2 className="text-sm font-semibold">机构与慢周期背景</h2>
              <div className="mt-3 grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                <ContextCard title="ETF" item={data.context.etf} /><ContextCard title="期权" item={data.context.options} /><ContextCard title="BTC 原生链" item={data.context.native_btc_onchain} />
                <ContextCard title="稳定币" item={data.context.stablecoin} /><ContextCard title="CFTC 机构期货" item={data.context.institutional_futures} /><ContextCard title="交易所流入流出" item={data.context.exchange_flows} />
                <ContextCard title="机构实体交叉验证" item={data.context.institutional_entities} />
              </div>
            </section>

            <section className="grid gap-4 lg:grid-cols-[1.35fr_1fr]">
              <div className="min-w-0 rounded-xl border border-slate-800 bg-slate-900/45"><div className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">详细证据时间线</div><div className="max-h-[44rem] divide-y divide-slate-800/70 overflow-y-auto">{evidence.length ? evidence.map((item) => <EvidenceRow key={item.evidence_id} item={item} />) : <div className="p-5 text-sm text-slate-500">当前没有达到异常门槛的证据；市场可以处于正常状态。</div>}</div></div>
              <div className="min-w-0 rounded-xl border border-slate-800 bg-slate-900/45"><div className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">数据质量</div><div className="max-h-[44rem] divide-y divide-slate-800/70 overflow-y-auto">{Object.values(data.incident.source_quality).map((quality) => <QualityRow key={quality.source_id} quality={quality} />)}</div></div>
            </section>

            <section className="rounded-xl border border-slate-800 bg-slate-900/45 p-4 text-xs text-slate-400">
              <div className="flex flex-wrap gap-x-5 gap-y-2"><span>最后确认阶段：{STAGE_LABEL[data.confirmed_incident.stage]} · {directionLabel(data.confirmed_incident.direction)}</span><span>确认时间：{formatTime(data.confirmed_incident.confirmed_at)}</span><span>{data.confirmed_incident.frozen ? `已冻结 ${data.confirmed_incident.frozen_age_sec}s` : "未冻结"}</span></div>
              <div className="mt-2 break-all text-slate-600">incident {data.confirmed_incident.incident_id ?? "—"} · calibration {data.incident.calibration_version}</div>
            </section>
          </>
        )}
      </div>
    </main>
  );
}

function Notice({ children, tone = "slate" }: { children: React.ReactNode; tone?: "slate" | "amber" | "violet" }) {
  const color = tone === "amber" ? "border-amber-800/60 bg-amber-950/30 text-amber-100" : tone === "violet" ? "border-violet-800/60 bg-violet-950/30 text-violet-100" : "border-slate-800 bg-slate-900/50 text-slate-300";
  return <div className={`rounded-lg border px-4 py-3 text-sm ${color}`}>{children}</div>;
}

function GateMetric({ label, value, ok }: { label: string; value: string; ok: boolean }) {
  return <div className="rounded-lg border border-slate-800 bg-slate-950/50 p-3"><div className="text-slate-500">{label}</div><div className={`mt-1 text-base font-semibold ${ok ? "text-emerald-300" : "text-amber-300"}`}>{value}</div></div>;
}

function ReasonPanel({ title, items, empty, tone }: { title: string; items: string[]; empty: string; tone: "emerald" | "rose" | "amber" | "slate" }) {
  const dot = tone === "emerald" ? "bg-emerald-400" : tone === "rose" ? "bg-rose-400" : tone === "amber" ? "bg-amber-400" : "bg-slate-500";
  return <div className="rounded-xl border border-slate-800 bg-slate-900/45 p-4"><h2 className="text-sm font-semibold">{title}</h2><ul className="mt-3 space-y-2 text-xs leading-5 text-slate-400">{(items.length ? items : [empty]).map((item, index) => <li key={`${item}-${index}`} className="flex gap-2"><span className={`mt-2 h-1.5 w-1.5 shrink-0 rounded-full ${dot}`} /><span>{item}</span></li>)}</ul></div>;
}

function FactorCard({ factor }: { factor: MarketFactor }) {
  const role = factor.decision_role === "scoring" ? "参与判断" : factor.decision_role === "blocked" ? "被数据门禁阻断" : "仅展示";
  return <div className="min-w-0 rounded-lg border border-slate-800 bg-slate-900/55 p-3"><div className="flex items-start justify-between gap-2"><span className="text-sm font-medium">{factor.label}</span><span className="shrink-0 rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">{FACTOR_STATUS[factor.status]}</span></div><div className="mt-2 text-xs text-slate-400">{directionLabel(factor.direction)} · 强度 {BAND_LABEL[factor.strength_band]} · {role}</div><p className="mt-2 text-xs leading-5 text-slate-500">{factor.plain_summary}</p><div className="mt-2 break-all text-[10px] text-slate-600">{factor.source_ids.join(" · ") || "无可用来源"} · {formatTime(factor.as_of)}</div></div>;
}

function ContextCard({ title, item }: { title: string; item?: ContextItem }) {
  const available = item?.availability === "available";
  const facts = contextFacts(title, item);
  return <div className="min-w-0 rounded-lg border border-slate-800 bg-slate-950/50 p-3"><div className="flex items-center justify-between gap-2 text-sm"><span>{title}</span><span className={available ? "text-emerald-300" : "text-slate-600"}>{available ? "有数据" : "不可用"}</span></div>{facts.length > 0 && <div className="mt-2 space-y-1 text-xs text-slate-300">{facts.map((fact) => <div key={fact}>{fact}</div>)}</div>}<p className="mt-2 text-xs leading-5 text-slate-500">{String(item?.note ?? item?.reason ?? "当前没有可展示的 PIT 合格事实。")}</p><div className="mt-2 break-all text-[10px] text-slate-600">来源 {String(item?.source ?? "—")} · 首次看到 {formatTime(Number(item?.known_at ?? item?.published_at ?? 0))}</div></div>;
}

function contextFacts(title: string, item?: ContextItem): string[] {
  if (!item || item.availability !== "available") return [];
  const number = (value: unknown): number | null => typeof value === "number" && Number.isFinite(value) ? value : null;
  const compact = (value: number): string => new Intl.NumberFormat("zh-CN", { notation: "compact", maximumFractionDigits: 2 }).format(value);
  const facts: string[] = [];
  if (title === "ETF") {
    const official = item.official_ibit as Record<string, unknown> | null | undefined;
    const quantity = number(official?.bitcoin_quantity);
    const shares = number(official?.shares_outstanding);
    if (quantity !== null) facts.push(`IBIT 官方持币 ${compact(quantity)} BTC（${String(official?.as_of ?? "日期未知")}）`);
    if (shares !== null) facts.push(`流通份额 ${compact(shares)} 份`);
    const net3d = number(item.net_3d);
    if (net3d !== null) facts.push(`聚合源近 3 日净流 ${net3d >= 0 ? "+" : ""}$${compact(net3d)}`);
  } else if (title === "期权") {
    const iv = number(item.iv_atm);
    if (iv !== null) facts.push(`ATM 隐含波动率 ${iv.toFixed(2)}`);
    const term = Array.isArray(item.term_structure) ? item.term_structure.length : 0;
    const strikes = Array.isArray(item.strike_clusters) ? item.strike_clusters.length : 0;
    if (term || strikes) facts.push(`期限节点 ${term} · 主要行权价 ${strikes}`);
  } else if (title === "CFTC 机构期货") {
    const net = number(item.noncommercial_net);
    const change = number(item.noncommercial_net_change);
    if (net !== null) facts.push(`非商业净持仓 ${net >= 0 ? "+" : ""}${compact(net)} 张`);
    if (change !== null) facts.push(`本周变化 ${change >= 0 ? "+" : ""}${compact(change)} 张`);
  } else if (title === "BTC 原生链") {
    const count = Array.isArray(item.events) ? item.events.length : 0;
    if (count) facts.push(`近 24 小时 PIT 合格事件 ${count} 条`);
  }
  return facts;
}

function EvidenceRow({ item }: { item: EvidenceItem }) {
  const tone = item.direction === "up" ? "text-emerald-300" : item.direction === "down" ? "text-rose-300" : "text-slate-400";
  const band = item.raw_strength >= 1 ? "强" : item.raw_strength >= 0.5 ? "中" : "弱";
  return <div className="min-w-0 px-4 py-3"><div className="flex flex-wrap items-center gap-2 text-xs"><span className="text-slate-500 tabular-nums">{formatTime(item.event_time)}</span><span className={`font-semibold ${tone}`}>{directionLabel(item.direction)}</span><span className="max-w-full break-all rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">{item.causal_root}</span><span className="ml-auto text-slate-500">强度 {band}</span>{item.role !== "scoring" && <span className="rounded bg-sky-950 px-1.5 py-0.5 text-[10px] text-sky-300">仅信息</span>}</div><div className="mt-1 text-sm text-slate-200">{item.name}</div><div className="mt-1 text-xs leading-5 text-slate-500">{item.explanation}</div></div>;
}

function QualityRow({ quality }: { quality: SourceQuality }) {
  return <div className="min-w-0 px-4 py-3 text-xs"><div className="flex flex-wrap items-center justify-between gap-2"><span className="break-all font-medium text-slate-200">{quality.source_id}</span><span className={quality.decision_usable ? "text-emerald-300" : "text-rose-300"}>{qualityLabel(quality)}</span></div><div className="mt-1 text-slate-500">as-of {formatTime(quality.as_of)} · {quality.continuity}</div>{!!quality.reasons.length && <div className="mt-1 text-amber-300/80">{quality.reasons.map(readableReason).join("；")}</div>}</div>;
}
