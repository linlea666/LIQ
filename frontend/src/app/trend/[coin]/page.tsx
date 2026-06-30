"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";
import { useTrendWebSocket } from "@/hooks/useTrendWebSocket";

type Quality = { valid: boolean; age_sec?: number | null; points: number; reason: string; status?: "fresh" | "stale" | "pending" | "missing" };
type Timeframe = { timeframe: string; score: number; direction: string; price_volume_score: number; orderflow_score: number; oi_participation_score: number; spot_confirms: boolean; oi_interpretation: string; quality: Quality };
type FlowWindow = { window: string; buy_usd: number; sell_usd: number; net_usd: number; net_ratio: number; historical_percentile?: number | null };
type ActiveFlow = { market: string; semantics: string; windows: FlowWindow[]; cvd_consistent?: boolean | null; quality: Quality };
type Contribution = { exchange: string; balance_btc: number; change_1d_btc: number; change_7d_btc: number; change_30d_btc: number };
type ChartPoint = { ts: number; price?: number | null; balance_btc: number; net_change_btc?: number | null };
type TransferWindow = { window: string; inflow_btc: number; outflow_btc: number; netflow_btc: number; net_ratio: number; inflow_percentile_365d?: number | null; outflow_percentile_365d?: number | null; abs_net_percentile_365d?: number | null; same_sign_days: number };
type TransferPoint = { ts: number; inflow_btc: number; outflow_btc: number; netflow_btc: number };
type Snapshot = {
  coin: string; ts: number; state: string; direction: string; core_score: number; confidence: number;
  modifier_total: number; consecutive_core_confirmations: number; confirmation_target: number; disclaimer: string;
  ai_review: string; ai_review_reason: string;
  modifier_breakdown: { funding_applied: number; wallet_market_bias: number; wallet_applied: number; etf_applied: number; total: number; wallet_cross_source_status: string };
  timeframes: Record<string, Timeframe>; active_flows: Record<string, ActiveFlow>;
  wallet_flow: { source_granularity: string; total_balance_btc: number; change_1d_btc?: number | null; change_3d_btc?: number | null; change_7d_btc?: number | null; change_30d_btc?: number | null; consecutive_direction_days: number; robust_zscore_90d?: number | null; dominant_exchange_ratio: number; contributions: Contribution[]; chart: ChartPoint[]; exchange_charts: Record<string, ChartPoint[]>; confidence_modifier: number; modifier_reason: string; quality: Quality; caveat: string };
  exchange_transfer_flow: { scope: string; source_granularity: string; latest_date_ts?: number | null; windows: TransferWindow[]; chart: TransferPoint[]; activity_regime: string; cross_source_status: string; coinglass_7d_abs_percentile?: number | null; score_weight: number; quality: Quality; caveat: string };
  funding: { binance_rate?: number | null; okx_rate?: number | null; oi_weighted_rate?: number | null; avg_24h?: number | null; avg_3d?: number | null; avg_7d?: number | null; same_sign_settlements: number; percentile_30d?: number | null; basis_pct?: number | null; crowding: string; confidence_modifier: number; modifier_reason: string; quality: Quality };
  etf_flow: { net_1d_usd?: number | null; net_3_sessions_usd?: number | null; net_5_sessions_usd?: number | null; confidence_modifier: number; quality: Quality };
  footprint: { enabled: boolean; score_weight: number; available: boolean; availability_14d_pct?: number | null; ablation_validated: boolean; promotion_eligible: boolean; quality: Quality; note: string };
  data_quality: Quality; source_diagnostics: { operational_limit_per_min?: number; provider_limit_per_min?: number; requests_last_60s?: number; queue_depth?: number; looknode?: { status?: string; last_error?: string } };
};

const STATE_CN: Record<string, string> = {
  data_invalid: "数据无效", range: "区间震荡", bullish_watch: "多头观察（未确认）", bearish_watch: "空头观察（未确认）",
  bullish_candidate: "多头候选", bearish_candidate: "空头候选", bullish_confirmed: "多头趋势确认",
  bearish_confirmed: "空头趋势确认", weakening: "趋势减弱", reversal_watch: "反转观察", reversal_confirmed: "反转确认",
};
const DIRECTION_CN: Record<string, string> = { bullish: "偏多", bearish: "偏空", range: "震荡", invalid: "无效" };
const CROSS_CN: Record<string, string> = { confirmed: "两源一致", conflict: "两源冲突，钱包修正归零", neutral: "未达到比较门槛", unavailable: "交叉验证不可用" };
const REGIME_CN: Record<string, string> = { normal: "正常", high_inflow: "异常流入", high_outflow: "异常流出", high_turnover: "双向高周转", unknown: "未知" };

const usd = (value?: number | null) => value == null ? "—" : `${value < 0 ? "-" : ""}$${Math.abs(value) >= 1e9 ? `${(Math.abs(value) / 1e9).toFixed(2)}B` : Math.abs(value) >= 1e6 ? `${(Math.abs(value) / 1e6).toFixed(1)}M` : Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const btc = (value?: number | null, signed = true) => value == null ? "—" : `${signed && value >= 0 ? "+" : ""}${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} BTC`;
const rate = (value?: number | null) => value == null ? "—" : `${(value * 100).toFixed(4)}%`;
const tone = (value: number) => value > 0 ? "text-emerald-400" : value < 0 ? "text-rose-400" : "text-slate-400";
const walletTone = (value: number) => value > 0 ? "text-rose-400" : value < 0 ? "text-emerald-400" : "text-slate-400";
const qualityText = (quality?: Quality) => {
  if (!quality) return "数据未就绪";
  if (quality.status === "pending") return `加载中 · ${quality.reason}`;
  if (quality.valid) return `有效${quality.age_sec == null ? "" : ` · ${Math.round(quality.age_sec / 3600 * 10) / 10}小时前`}`;
  return `${quality.status === "stale" ? "已过期" : "不可用"} · ${quality.reason || "数据不可用"}`;
};

function explain(data: Snapshot) {
  if (!data.data_quality.valid || data.state === "data_invalid") return { title: "核心数据不足，暂停判断", detail: "至少一个核心周期未通过质量门，次级指标不会替代核心数据。", supports: [] as string[], conflicts: [] as string[], missing: ["等待核心数据恢复"] };
  if (data.direction === "range") return { title: "当前以区间震荡看待", detail: "多空证据没有形成足够一致的方向，暂时不能称为趋势。", supports: [] as string[], conflicts: ["1小时、4小时和日线未形成同向共振"], missing: ["等待4小时主趋势形成"] };
  const bullish = data.direction === "bullish";
  const word = bullish ? "偏多" : "偏空";
  const confirmed = data.state.includes("confirmed");
  const supports: string[] = [];
  const conflicts: string[] = [];
  const missing: string[] = [];
  for (const tf of ["1h", "4h", "1d"]) {
    const item = data.timeframes[tf];
    if (!item) continue;
    if (item.direction === data.direction) supports.push(`${tf}方向${word}${item.spot_confirms ? "，有现货确认" : ""}`);
    else conflicts.push(`${tf}${item.direction === "range" ? "仍是震荡" : `方向${DIRECTION_CN[item.direction] || item.direction}`}`);
  }
  const four = data.timeframes["4h"];
  const one = data.timeframes["1h"];
  const day = data.timeframes["1d"];
  if (!four || four.direction !== data.direction || Math.abs(four.score) < 45) missing.push("4小时主趋势分尚未达到45");
  if (!one || one.direction !== data.direction) missing.push("1小时没有同向确认");
  if (!four?.spot_confirms && !one?.spot_confirms) missing.push("缺少现货主动成交确认");
  if (day && day.direction !== "range" && day.direction !== data.direction && Math.abs(day.score) >= 45) missing.push("日线存在强反向过滤");
  return {
    title: confirmed ? `当前${word}趋势已经确认` : `当前有${word}迹象，但主趋势尚未确认`,
    detail: confirmed ? "核心周期和现货确认条件已经满足，仍需关注后续减弱或反转状态。" : "这是方向观察，不代表已经形成明确趋势，也不是交易指令。",
    supports, conflicts, missing: missing.length ? missing : ["等待连续闭合周期确认"],
  };
}

export default function TrendPage() {
  const params = useParams<{ coin: string }>();
  const coin = String(params.coin || "BTC").toUpperCase();
  const [data, setData] = useState<Snapshot | null>(null);
  const [error, setError] = useState("");
  const [exchange, setExchange] = useState("全部");
  const load = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/trend/${coin}`, { cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 503 ? "趋势模块正在准备首个快照" : `HTTP ${response.status}`);
      setData(await response.json()); setError("");
    } catch (cause) { setError(cause instanceof Error ? cause.message : "加载失败"); }
  }, [coin]);
  useTrendWebSocket(coin, load);
  useEffect(() => { load(); const timer = setInterval(load, 30_000); return () => clearInterval(timer); }, [load]);
  const filteredChart = useMemo(() => !data || exchange === "全部" ? data?.wallet_flow.chart || [] : data.wallet_flow.exchange_charts[exchange] || [], [data, exchange]);

  if (coin !== "BTC") return <main className="trend-page min-h-screen bg-slate-950 p-8 text-slate-200">该模块仅支持 BTC。 <Link href="/trend/BTC" className="text-blue-400">返回 BTC</Link></main>;
  const summary = data ? explain(data) : null;
  return (
    <main className="trend-page min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 px-4 py-3 backdrop-blur md:px-5">
        <div className="mx-auto flex max-w-[1500px] items-center justify-between gap-3">
          <div><div className="text-base font-semibold md:text-lg">BTC 原生趋势与资金流</div><div className="text-[10px] text-slate-500 md:text-[11px]">只读监控 · 不执行交易 · 不提供开仓/止盈止损建议</div></div>
          <div className="flex items-center gap-2 text-[11px] md:gap-3 md:text-xs"><span className={data?.data_quality.valid ? "text-emerald-400" : "text-rose-400"}>● {data?.data_quality.valid ? "核心趋势数据有效" : "核心数据未就绪"}</span><Link href="/" className="rounded border border-slate-700 px-2 py-1.5 hover:bg-slate-800 md:px-3">返回大屏</Link></div>
        </div>
      </header>
      <div className="mx-auto max-w-[1500px] space-y-5 p-3 md:p-5">
        {error && <div className="rounded-lg border border-amber-700/60 bg-amber-950/30 p-4 text-sm text-amber-300">{error}</div>}
        {!data || !summary ? <div className="py-24 text-center text-slate-500">正在读取首个闭合周期快照…</div> : <>
          <section className="rounded-xl border border-slate-800 bg-slate-900/60 p-4 md:p-5">
            <div className="grid gap-5 lg:grid-cols-[1.5fr_1fr]">
              <div><div className={`text-sm font-medium ${data.direction === "bullish" ? "text-emerald-400" : data.direction === "bearish" ? "text-rose-400" : "text-slate-300"}`}>{STATE_CN[data.state] || data.state}</div><h1 className="mt-2 text-xl font-semibold text-slate-100 md:text-2xl">{summary.title}</h1><p className="mt-2 text-sm leading-6 text-slate-400">{summary.detail}</p><Stage state={data.state} count={data.consecutive_core_confirmations} target={data.confirmation_target} /></div>
              <div className="grid grid-cols-2 gap-3"><Metric title="核心方向分" value={data.core_score.toFixed(1)} accent={data.core_score > 0 ? "emerald" : data.core_score < 0 ? "rose" : "slate"} sub="仅S级原生数据决定方向" /><Metric title="信号强度" value={`${data.confidence.toFixed(1)}/100`} accent="blue" sub="不是胜率；含A级可信度修正" /></div>
            </div>
            <div className="mt-4 grid gap-3 md:grid-cols-3"><Evidence title="支持当前方向" items={summary.supports} toneClass="text-emerald-400" empty="暂无强支持证据" /><Evidence title="冲突或保留意见" items={summary.conflicts} toneClass="text-amber-400" empty="暂无明显冲突" /><Evidence title="距离确认还缺什么" items={summary.missing} toneClass="text-blue-400" empty="确认条件已满足" /></div>
          </section>

          <section className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title="多周期原生趋势" note="4h是主趋势，1h负责确认，1d过滤大环境；15m只看短期脉冲" /><div className="mt-4 grid gap-3 md:grid-cols-2 xl:grid-cols-4">{["15m", "1h", "4h", "1d"].map((tf) => <TimeframeCard key={tf} item={data.timeframes[tf]} />)}</div></section>

          <section className="grid gap-5 xl:grid-cols-2"><FlowPanel title="现货主动资金流" flow={data.active_flows.spot} /><FlowPanel title="合约主动资金流" flow={data.active_flows.futures} /></section>

          <section className="rounded-xl border border-slate-800 bg-slate-900/55 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><SectionTitle title="CoinGlass 交易所BTC钱包余额" note="覆盖面较广的余额存量变化；正数代表交易所潜在可售BTC增加" /><select value={exchange} onChange={(event) => setExchange(event.target.value)} className="rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs"><option>全部</option>{data.wallet_flow.contributions.map((item) => <option key={item.exchange}>{item.exchange}</option>)}</select></div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-6"><SmallMetric label="总余额" value={`${data.wallet_flow.total_balance_btc.toLocaleString(undefined, { maximumFractionDigits: 0 })} BTC`} /><SmallMetric label="1日变化" value={btc(data.wallet_flow.change_1d_btc)} valueClass={walletTone(data.wallet_flow.change_1d_btc || 0)} /><SmallMetric label="3日变化" value={btc(data.wallet_flow.change_3d_btc)} valueClass={walletTone(data.wallet_flow.change_3d_btc || 0)} /><SmallMetric label="7日变化" value={btc(data.wallet_flow.change_7d_btc)} valueClass={walletTone(data.wallet_flow.change_7d_btc || 0)} /><SmallMetric label="30日变化" value={btc(data.wallet_flow.change_30d_btc)} valueClass={walletTone(data.wallet_flow.change_30d_btc || 0)} /><SmallMetric label="连续方向" value={`${Math.abs(data.wallet_flow.consecutive_direction_days)}天${data.wallet_flow.consecutive_direction_days > 0 ? "流入" : data.wallet_flow.consecutive_direction_days < 0 ? "流出" : "混合"}`} /></div>
            <div className="mt-4 grid gap-4 xl:grid-cols-[2fr_1fr]"><WalletChart points={filteredChart} /><div className="max-h-72 overflow-auto rounded-lg border border-slate-800"><table className="w-full text-xs"><thead className="sticky top-0 bg-slate-950 text-slate-500"><tr><th className="p-2 text-left">交易所</th><th className="p-2 text-right">余额</th><th className="p-2 text-right">1d</th><th className="p-2 text-right">7d</th></tr></thead><tbody>{data.wallet_flow.contributions.map((row) => <tr key={row.exchange} className="border-t border-slate-800"><td className="p-2">{row.exchange}</td><td className="p-2 text-right">{row.balance_btc.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td><td className={`p-2 text-right ${walletTone(row.change_1d_btc)}`}>{row.change_1d_btc.toFixed(1)}</td><td className={`p-2 text-right ${walletTone(row.change_7d_btc)}`}>{row.change_7d_btc.toFixed(1)}</td></tr>)}</tbody></table></div></div>
            <div className="mt-3 rounded bg-slate-950/70 px-3 py-2 text-xs text-slate-400">数据：{qualityText(data.wallet_flow.quality)} · 原始市场偏向 {signed(data.modifier_breakdown.wallet_market_bias)} · 实际修正 {signed(data.modifier_breakdown.wallet_applied)} · {data.wallet_flow.modifier_reason}</div>
          </section>

          <TransferPanel transfer={data.exchange_transfer_flow} />

          <section className="grid gap-5 xl:grid-cols-4">
            <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4 xl:col-span-2"><SectionTitle title="Funding + Basis 拥挤修正" note="只修正信号强度，最大+3/-8，不能决定或翻转方向" /><div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4"><SmallMetric label="Binance" value={rate(data.funding.binance_rate)} /><SmallMetric label="OKX" value={rate(data.funding.okx_rate)} /><SmallMetric label="OI加权" value={rate(data.funding.oi_weighted_rate)} /><SmallMetric label="Basis" value={data.funding.basis_pct == null ? "—" : `${data.funding.basis_pct.toFixed(4)}%`} /><SmallMetric label="24h均值" value={rate(data.funding.avg_24h)} /><SmallMetric label="3d均值" value={rate(data.funding.avg_3d)} /><SmallMetric label="7d均值" value={rate(data.funding.avg_7d)} /><SmallMetric label="30日分位" value={data.funding.percentile_30d == null ? "—" : `${data.funding.percentile_30d.toFixed(1)}%`} /></div><div className="mt-3 text-xs text-slate-400">{qualityText(data.funding.quality)} · 拥挤：{data.funding.crowding} · 实际修正 {signed(data.modifier_breakdown.funding_applied)} · {data.funding.modifier_reason}</div></div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title="BTC ETF净流" note="仅作用于1d背景；按真实交易日" /><div className="mt-4 space-y-2"><SmallMetric label="1个交易日" value={usd(data.etf_flow.net_1d_usd)} valueClass={tone(data.etf_flow.net_1d_usd || 0)} /><SmallMetric label="3个交易日" value={usd(data.etf_flow.net_3_sessions_usd)} valueClass={tone(data.etf_flow.net_3_sessions_usd || 0)} /><SmallMetric label="5个交易日" value={usd(data.etf_flow.net_5_sessions_usd)} valueClass={tone(data.etf_flow.net_5_sessions_usd || 0)} /></div><div className="mt-3 text-[10px] text-slate-500">{qualityText(data.etf_flow.quality)} · 实际修正 {signed(data.modifier_breakdown.etf_applied)}</div></div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title="B级实验指标" note="首版固定零权重" /><div className="mt-4 space-y-3 text-sm"><div className="flex justify-between"><span>Footprint</span><span className={data.footprint.available ? "text-emerald-400" : "text-rose-400"}>{!data.footprint.enabled ? "已关闭" : data.footprint.available ? "可用" : "不可用/过期/429"}</span></div><div className="flex justify-between"><span>14日可用率</span><span>{data.footprint.availability_14d_pct == null ? "—" : `${data.footprint.availability_14d_pct.toFixed(2)}%`}</span></div><div className="flex justify-between"><span>评分权重</span><span>{data.footprint.score_weight}</span></div><p className="text-xs text-slate-500">{qualityText(data.footprint.quality)} · {data.footprint.note}</p></div></div>
          </section>

          <section className="rounded-xl border border-slate-800 bg-slate-900/40 p-4"><SectionTitle title="系统与数据状态" note="供运维排查，不参与方向判断" /><div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-4"><SmallMetric label="CoinGlass过去60秒" value={`${data.source_diagnostics.requests_last_60s ?? 0}/${data.source_diagnostics.operational_limit_per_min ?? 10}次`} /><SmallMetric label="CoinGlass等待队列" value={`${data.source_diagnostics.queue_depth ?? 0}`} /><SmallMetric label="Looknode连接" value={data.source_diagnostics.looknode?.status || qualityText(data.exchange_transfer_flow.quality)} /><SmallMetric label="AI复核" value={data.ai_review === "not_run" ? "未运行/已降级" : data.ai_review} /></div><div className="mt-3 text-xs text-slate-500">A级修正合计 {signed(data.modifier_total)} · AI说明：{data.ai_review_reason || "无"} · Looknode错误：{data.source_diagnostics.looknode?.last_error || "无"}</div></section>
          <footer className="pb-8 text-center text-xs text-slate-600">{data.disclaimer} · 更新时间 {new Date(data.ts * 1000).toLocaleString()}</footer>
        </>}
      </div>
    </main>
  );
}

const signed = (value: number) => `${value >= 0 ? "+" : ""}${value.toFixed(2)}`;
function SectionTitle({ title, note }: { title: string; note: string }) { return <div><h2 className="font-semibold text-slate-100">{title}</h2><p className="mt-0.5 text-[11px] text-slate-500">{note}</p></div>; }
function Metric({ title, value, sub, accent }: { title: string; value: string; sub: string; accent: "emerald" | "rose" | "blue" | "slate" }) { const colors = { emerald: "text-emerald-400", rose: "text-rose-400", blue: "text-blue-400", slate: "text-slate-200" }; return <div className="rounded-xl bg-slate-950/65 p-3"><div className="text-xs text-slate-500">{title}</div><div className={`mt-1 text-xl font-semibold md:text-2xl ${colors[accent]}`}>{value}</div><div className="mt-1 text-[10px] text-slate-600">{sub}</div></div>; }
function SmallMetric({ label, value, valueClass = "text-slate-100" }: { label: string; value: string; valueClass?: string }) { return <div className="rounded-lg bg-slate-950/70 p-3"><div className="text-[10px] text-slate-500">{label}</div><div className={`mt-1 text-sm font-medium ${valueClass}`}>{value}</div></div>; }
function Evidence({ title, items, toneClass, empty }: { title: string; items: string[]; toneClass: string; empty: string }) { return <div className="rounded-lg bg-slate-950/60 p-3"><div className={`text-xs font-medium ${toneClass}`}>{title}</div><div className="mt-2 space-y-1 text-xs text-slate-400">{items.length ? items.slice(0, 3).map((item) => <div key={item}>• {item}</div>) : <div>• {empty}</div>}</div></div>; }
function Stage({ state, count, target }: { state: string; count: number; target: number }) { const index = state.includes("confirmed") ? 2 : state.includes("candidate") || state.includes("reversal_watch") ? 1 : 0; return <div className="mt-4"><div className="flex items-center gap-2 text-[10px] text-slate-500">{["观察", "候选", "确认"].map((label, idx) => <div key={label} className="flex flex-1 items-center gap-2"><span className={`flex h-5 w-5 items-center justify-center rounded-full ${idx <= index ? "bg-blue-500 text-white" : "bg-slate-800"}`}>{idx + 1}</span><span>{label}</span>{idx < 2 && <span className="h-px flex-1 bg-slate-700" />}</div>)}</div><div className="mt-2 text-[10px] text-slate-600">连续核心确认：{count}/{target}</div></div>; }
function TimeframeCard({ item }: { item?: Timeframe }) { if (!item) return null; return <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3"><div className="flex items-center justify-between"><div className="flex items-center gap-2"><span className="font-semibold">{item.timeframe}</span><span className={`rounded px-1.5 py-0.5 text-[9px] ${item.direction === "bullish" ? "bg-emerald-950 text-emerald-400" : item.direction === "bearish" ? "bg-rose-950 text-rose-400" : "bg-slate-800 text-slate-400"}`}>{DIRECTION_CN[item.direction] || item.direction}</span></div><span className={`text-lg font-semibold ${tone(item.score)}`}>{item.score.toFixed(1)}</span></div><div className="mt-3 space-y-1.5 text-[11px]"><Bar label="价格/成交量" value={item.price_volume_score} /><Bar label="现货+合约主动成交" value={item.orderflow_score} /><Bar label="OI参与" value={item.oi_participation_score} /></div><div className="mt-3 text-[10px] leading-4 text-slate-500">现货确认：{item.spot_confirms ? "是" : "否"} · {item.oi_interpretation}</div></div>; }
function Bar({ label, value }: { label: string; value: number }) { return <div><div className="flex justify-between text-slate-500"><span>{label}</span><span className={tone(value)}>{value.toFixed(1)}</span></div><div className="mt-1 h-1 overflow-hidden rounded bg-slate-800"><div className={`h-full ${value >= 0 ? "bg-emerald-500" : "bg-rose-500"}`} style={{ width: `${Math.min(100, Math.abs(value))}%`, marginLeft: value < 0 ? `${100 - Math.min(100, Math.abs(value))}%` : 0 }} /></div></div>; }
function FlowPanel({ title, flow }: { title: string; flow?: ActiveFlow }) { return <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title={title} note={flow?.semantics || "数据未就绪"} /><div className="mt-1 text-[10px] text-slate-600">{qualityText(flow?.quality)}</div><div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-5">{flow?.windows.map((row) => <div key={row.window} className="rounded-lg bg-slate-950/70 p-2 text-center"><div className="text-[10px] text-slate-500">{row.window}</div><div className={`mt-1 text-xs font-semibold ${tone(row.net_usd)}`}>{row.net_usd >= 0 ? "净买 " : "净卖 "}{usd(Math.abs(row.net_usd))}</div><div className="mt-1 text-[9px] text-slate-600">净额/总量 {(row.net_ratio * 100).toFixed(1)}%</div><div className="mt-1 text-[8px] text-slate-700">买 {usd(row.buy_usd)} · 卖 {usd(row.sell_usd)}</div><div className="text-[8px] text-slate-600">{row.historical_percentile == null ? "历史样本积累中" : `历史P${row.historical_percentile.toFixed(1)}`}</div></div>)}</div><div className="mt-3 text-[10px] text-slate-500">与本地重建CVD一致：{flow?.cvd_consistent == null ? "待判断" : flow.cvd_consistent ? "是" : "否"}（只校验，不重复计权）</div></div>; }
function TransferPanel({ transfer }: { transfer: Snapshot["exchange_transfer_flow"] }) { return <section className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title="Looknode 七家主要交易所BTC链上流" note="日级真实充值/提现流；独立展示和告警，不新增趋势权重" /><div className="mt-2 flex flex-wrap gap-2 text-[10px]"><span className="rounded bg-slate-950 px-2 py-1 text-slate-400">{qualityText(transfer.quality)}</span><span className="rounded bg-slate-950 px-2 py-1 text-slate-400">活动：{REGIME_CN[transfer.activity_regime] || transfer.activity_regime}</span><span className={`rounded bg-slate-950 px-2 py-1 ${transfer.cross_source_status === "conflict" ? "text-rose-400" : transfer.cross_source_status === "confirmed" ? "text-emerald-400" : "text-slate-400"}`}>与CoinGlass：{CROSS_CN[transfer.cross_source_status] || transfer.cross_source_status}</span></div><div className="mt-4 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">{transfer.windows.map((window) => <div key={window.window} className="rounded-lg bg-slate-950/70 p-3"><div className="flex justify-between"><span className="text-xs text-slate-400">{window.window}</span><span className={`text-xs font-semibold ${walletTone(window.netflow_btc)}`}>{window.netflow_btc >= 0 ? "净流入" : "净流出"} {btc(Math.abs(window.netflow_btc), false)}</span></div><div className="mt-2 grid grid-cols-2 gap-2 text-[10px]"><div><span className="text-slate-600">流入</span><div className="text-rose-400">{btc(window.inflow_btc, false)}</div></div><div><span className="text-slate-600">流出</span><div className="text-emerald-400">{btc(window.outflow_btc, false)}</div></div></div><div className="mt-2 text-[9px] text-slate-600">净流比例 {(window.net_ratio * 100).toFixed(2)}% · {window.abs_net_percentile_365d == null ? "历史样本不足" : `净流P${window.abs_net_percentile_365d.toFixed(1)}`}</div></div>)}</div><div className="mt-4"><TransferChart points={transfer.chart} /></div><p className="mt-3 text-xs leading-5 text-slate-500">范围：{transfer.scope}。{transfer.caveat} 最新数据日：{transfer.latest_date_ts ? new Date(transfer.latest_date_ts * 1000).toLocaleDateString() : "—"}。</p></section>; }
function WalletChart({ points }: { points: ChartPoint[] }) { const usable = points.filter((point) => point.net_change_btc != null).slice(-120); if (usable.length < 2) return <EmptyChart text="钱包日级历史不足" />; const maxAbs = Math.max(...usable.map((point) => Math.abs(point.net_change_btc || 0)), 1); const prices = usable.map((point) => point.price).filter((price): price is number => price != null); const minP = Math.min(...prices), maxP = Math.max(...prices); const pricePath = usable.map((point, idx) => { const x = idx / (usable.length - 1) * 1000; const y = point.price == null || maxP === minP ? 130 : 20 + (1 - (point.price - minP) / (maxP - minP)) * 210; return `${idx ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`; }).join(" "); return <div className="h-72 rounded-lg border border-slate-800 bg-slate-950/50 p-2"><svg viewBox="0 0 1000 260" className="h-full w-full" preserveAspectRatio="none"><line x1="0" y1="130" x2="1000" y2="130" stroke="#334155" strokeDasharray="5 5" />{usable.map((point, idx) => { const width = Math.max(2, 900 / usable.length); const x = idx / usable.length * 1000; const delta = point.net_change_btc || 0; const height = Math.abs(delta) / maxAbs * 105; return <rect key={point.ts} x={x} width={width} y={delta >= 0 ? 130 - height : 130} height={height} fill={delta >= 0 ? "#f43f5e" : "#10b981"} opacity="0.65" />; })}<path d={pricePath} fill="none" stroke="#fbbf24" strokeWidth="3" vectorEffect="non-scaling-stroke" /></svg></div>; }
function TransferChart({ points }: { points: TransferPoint[] }) { const usable = points.slice(-60); if (usable.length < 2) return <EmptyChart text="Looknode日级历史不足" />; const max = Math.max(...usable.flatMap((point) => [point.inflow_btc, point.outflow_btc]), 1); return <div className="h-64 rounded-lg border border-slate-800 bg-slate-950/50 p-2"><div className="mb-1 flex justify-end gap-3 text-[9px]"><span className="text-rose-400">■ 流入（潜在卖压）</span><span className="text-emerald-400">■ 流出</span></div><svg viewBox="0 0 1000 230" className="h-[calc(100%-18px)] w-full" preserveAspectRatio="none">{usable.map((point, idx) => { const group = 1000 / usable.length; const width = Math.max(2, group * 0.34); const inHeight = point.inflow_btc / max * 210; const outHeight = point.outflow_btc / max * 210; return <g key={point.ts}><rect x={idx * group + group * 0.12} y={220 - inHeight} width={width} height={inHeight} fill="#f43f5e" opacity="0.7" /><rect x={idx * group + group * 0.52} y={220 - outHeight} width={width} height={outHeight} fill="#10b981" opacity="0.7" /></g>; })}</svg></div>; }
function EmptyChart({ text }: { text: string }) { return <div className="flex h-64 items-center justify-center rounded-lg border border-slate-800 text-xs text-slate-600">{text}</div>; }
