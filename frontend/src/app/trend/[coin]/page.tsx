"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";
import { useTrendWebSocket } from "@/hooks/useTrendWebSocket";

type Quality = { valid: boolean; age_sec?: number | null; points: number; reason: string; as_of_ts?: number | null; fetched_at_ts?: number | null; status?: "fresh" | "stale" | "pending" | "missing" };
type Timeframe = { timeframe: string; score: number; direction: string; price_volume_score: number; orderflow_score: number; oi_participation_score: number; spot_confirms: boolean; oi_interpretation: string; quality: Quality };
type FlowWindow = { window: string; buy_usd: number; sell_usd: number; net_usd: number; net_ratio: number; historical_percentile?: number | null };
type ActiveFlow = { market: string; semantics: string; windows: FlowWindow[]; cvd_consistent?: boolean | null; quality: Quality };
type Contribution = { exchange: string; balance_btc: number; change_1d_btc: number; change_7d_btc: number; change_30d_btc: number };
type ChartPoint = { ts: number; price?: number | null; balance_btc: number; net_change_btc?: number | null };
type Snapshot = {
  coin: string; ts: number; state: string; direction: string; core_score: number; confidence: number;
  modifier_total: number; consecutive_core_confirmations: number; confirmation_target: number; disclaimer: string; ai_review: string;
  timeframes: Record<string, Timeframe>; active_flows: Record<string, ActiveFlow>;
  wallet_flow: { source_granularity: string; total_balance_btc: number; change_1d_btc?: number | null; change_3d_btc?: number | null; change_7d_btc?: number | null; change_30d_btc?: number | null; consecutive_direction_days: number; robust_zscore_90d?: number | null; dominant_exchange_ratio: number; contributions: Contribution[]; chart: ChartPoint[]; exchange_charts: Record<string, ChartPoint[]>; confidence_modifier: number; modifier_reason: string; quality: Quality; caveat: string };
  funding: { binance_rate?: number | null; okx_rate?: number | null; oi_weighted_rate?: number | null; avg_24h?: number | null; avg_3d?: number | null; avg_7d?: number | null; same_sign_settlements: number; percentile_30d?: number | null; basis_pct?: number | null; crowding: string; confidence_modifier: number; modifier_reason: string; quality: Quality };
  etf_flow: { net_1d_usd?: number | null; net_3_sessions_usd?: number | null; net_5_sessions_usd?: number | null; confidence_modifier: number; quality: Quality };
  footprint: { enabled: boolean; score_weight: number; available: boolean; availability_14d_pct?: number | null; ablation_validated: boolean; promotion_eligible: boolean; quality: Quality; note: string };
  data_quality: Quality; source_diagnostics: { operational_limit_per_min?: number; provider_limit_per_min?: number; requests_last_60s?: number; queue_depth?: number };
};

const STATE_CN: Record<string, string> = {
  data_invalid: "数据无效", range: "区间震荡", bullish_watch: "多头观察", bearish_watch: "空头观察",
  bullish_candidate: "多头候选", bearish_candidate: "空头候选", bullish_confirmed: "多头确认",
  bearish_confirmed: "空头确认", weakening: "趋势减弱", reversal_watch: "反转观察", reversal_confirmed: "反转确认",
};

const usd = (value?: number | null) => value == null ? "—" : `${value < 0 ? "-" : ""}$${Math.abs(value) >= 1e9 ? `${(Math.abs(value) / 1e9).toFixed(2)}B` : Math.abs(value) >= 1e6 ? `${(Math.abs(value) / 1e6).toFixed(1)}M` : Math.abs(value).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
const btc = (value?: number | null) => value == null ? "—" : `${value >= 0 ? "+" : ""}${value.toLocaleString(undefined, { maximumFractionDigits: 2 })} BTC`;
const rate = (value?: number | null) => value == null ? "—" : `${(value * 100).toFixed(4)}%`;
const tone = (value: number) => value > 0 ? "text-emerald-400" : value < 0 ? "text-rose-400" : "text-slate-400";
const walletTone = (value: number) => value > 0 ? "text-rose-400" : value < 0 ? "text-emerald-400" : "text-slate-400";
const qualityText = (quality?: Quality) => !quality ? "数据未就绪" : quality.valid
  ? `有效${quality.age_sec == null ? "" : ` · ${Math.round(quality.age_sec / 60)}分钟前`}`
  : `${quality.status || "missing"} · ${quality.reason || "数据不可用"}`;

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

  const filteredChart = useMemo(() => {
    if (!data || exchange === "全部") return data?.wallet_flow.chart || [];
    return data.wallet_flow.exchange_charts[exchange] || [];
  }, [data, exchange]);

  if (coin !== "BTC") return <main className="min-h-screen bg-slate-950 p-8 text-slate-200">该模块仅支持 BTC。 <Link href="/trend/BTC" className="text-blue-400">返回 BTC</Link></main>;
  return (
    <main className="min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-20 border-b border-slate-800 bg-slate-950/90 px-5 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-[1500px] items-center justify-between">
          <div><div className="text-lg font-semibold">BTC 原生趋势与资金流</div><div className="text-[11px] text-slate-500">只读监控 · 不执行交易 · 不提供开仓/止盈止损建议</div></div>
          <div className="flex items-center gap-3 text-xs"><span className={data?.data_quality.valid ? "text-emerald-400" : "text-rose-400"}>● {data?.data_quality.valid ? "核心数据有效" : "数据未就绪"}</span><Link href="/" className="rounded border border-slate-700 px-3 py-1.5 hover:bg-slate-800">返回大屏</Link></div>
        </div>
      </header>
      <div className="mx-auto max-w-[1500px] space-y-5 p-5">
        {error && <div className="rounded-lg border border-amber-700/60 bg-amber-950/30 p-4 text-sm text-amber-300">{error}</div>}
        {!data ? <div className="py-24 text-center text-slate-500">正在读取首个闭合周期快照…</div> : <>
          <section className="grid gap-3 md:grid-cols-4">
            <Metric title="当前状态" value={STATE_CN[data.state] || data.state} accent={data.direction === "bullish" ? "emerald" : data.direction === "bearish" ? "rose" : "slate"} sub={`连续核心确认 ${data.consecutive_core_confirmations}/${data.confirmation_target}`} />
            <Metric title="核心方向分" value={data.core_score.toFixed(1)} accent={data.core_score > 0 ? "emerald" : data.core_score < 0 ? "rose" : "slate"} sub="仅 S 级原生数据决定方向" />
            <Metric title="置信度" value={`${data.confidence.toFixed(1)}%`} accent="blue" sub={`A级修正 ${data.modifier_total >= 0 ? "+" : ""}${data.modifier_total.toFixed(1)} · AI ${data.ai_review}（不可升级/反向）`} />
            <Metric title="CoinGlass 额度" value={`${data.source_diagnostics.requests_last_60s ?? 0}/${data.source_diagnostics.operational_limit_per_min ?? 10}`} accent="slate" sub={`滚动60秒 · 队列 ${data.source_diagnostics.queue_depth ?? 0} · 服务商上限${data.source_diagnostics.provider_limit_per_min ?? "—"}`} />
          </section>

          <section className="rounded-xl border border-slate-800 bg-slate-900/55 p-4">
            <SectionTitle title="多周期原生趋势" note="4h 50% · 1h 30% · 1d 20%；15m仅作短期脉冲" />
            <div className="mt-4 grid gap-3 md:grid-cols-4">{["15m", "1h", "4h", "1d"].map((tf) => <TimeframeCard key={tf} item={data.timeframes[tf]} />)}</div>
          </section>

          <section className="grid gap-5 xl:grid-cols-2">
            <FlowPanel title="现货主动资金流" flow={data.active_flows.spot} />
            <FlowPanel title="合约主动资金流" flow={data.active_flows.futures} />
          </section>

          <section className="rounded-xl border border-slate-800 bg-slate-900/55 p-4">
            <div className="flex flex-wrap items-start justify-between gap-3"><SectionTitle title="交易所 BTC 钱包流" note={`真实粒度为${data.wallet_flow.source_granularity}；不展示或伪造1小时链上流`} /><select value={exchange} onChange={(e) => setExchange(e.target.value)} className="rounded border border-slate-700 bg-slate-950 px-3 py-1.5 text-xs"><option>全部</option>{data.wallet_flow.contributions.map((x) => <option key={x.exchange}>{x.exchange}</option>)}</select></div>
            <div className="mt-4 grid gap-3 sm:grid-cols-2 lg:grid-cols-6">
              <SmallMetric label="总余额" value={`${data.wallet_flow.total_balance_btc.toLocaleString(undefined, { maximumFractionDigits: 0 })} BTC`} />
              <SmallMetric label="1日变化（+为潜在卖压）" value={btc(data.wallet_flow.change_1d_btc)} valueClass={walletTone(data.wallet_flow.change_1d_btc || 0)} />
              <SmallMetric label="3日变化（+为潜在卖压）" value={btc(data.wallet_flow.change_3d_btc)} valueClass={walletTone(data.wallet_flow.change_3d_btc || 0)} />
              <SmallMetric label="7日变化（+为潜在卖压）" value={btc(data.wallet_flow.change_7d_btc)} valueClass={walletTone(data.wallet_flow.change_7d_btc || 0)} />
              <SmallMetric label="30日变化（+为潜在卖压）" value={btc(data.wallet_flow.change_30d_btc)} valueClass={walletTone(data.wallet_flow.change_30d_btc || 0)} />
              <SmallMetric label="连续方向" value={`${Math.abs(data.wallet_flow.consecutive_direction_days)} 天${data.wallet_flow.consecutive_direction_days > 0 ? "流入" : data.wallet_flow.consecutive_direction_days < 0 ? "流出" : "混合"}`} />
            </div>
            <div className="mt-4 grid gap-4 xl:grid-cols-[2fr_1fr]"><WalletChart points={filteredChart} /><div className="max-h-72 overflow-auto rounded-lg border border-slate-800"><table className="w-full text-xs"><thead className="sticky top-0 bg-slate-950 text-slate-500"><tr><th className="p-2 text-left">交易所</th><th className="p-2 text-right">余额</th><th className="p-2 text-right">1d</th><th className="p-2 text-right">7d</th></tr></thead><tbody>{data.wallet_flow.contributions.map((row) => <tr key={row.exchange} className="border-t border-slate-800"><td className="p-2">{row.exchange}</td><td className="p-2 text-right">{row.balance_btc.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td><td className={`p-2 text-right ${tone(row.change_1d_btc)}`}>{row.change_1d_btc.toFixed(1)}</td><td className={`p-2 text-right ${tone(row.change_7d_btc)}`}>{row.change_7d_btc.toFixed(1)}</td></tr>)}</tbody></table></div></div>
            <div className="mt-3 rounded bg-slate-950/70 px-3 py-2 text-xs text-slate-400">数据：{qualityText(data.wallet_flow.quality)} · {data.wallet_flow.caveat} 90日稳健Z：{data.wallet_flow.robust_zscore_90d?.toFixed(2) ?? "—"} · 单一交易所最大贡献：{(data.wallet_flow.dominant_exchange_ratio * 100).toFixed(1)}% · 修正：{data.wallet_flow.confidence_modifier >= 0 ? "+" : ""}{data.wallet_flow.confidence_modifier.toFixed(1)}（{data.wallet_flow.modifier_reason}）</div>
          </section>

          <section className="grid gap-5 xl:grid-cols-4">
            <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4 xl:col-span-2"><SectionTitle title="Funding + Basis 拥挤修正" note="只修正置信度，最大 +3/-8，不能决定或翻转方向" /><div className="mt-4 grid grid-cols-2 gap-3 md:grid-cols-4"><SmallMetric label="Binance" value={rate(data.funding.binance_rate)} /><SmallMetric label="OKX" value={rate(data.funding.okx_rate)} /><SmallMetric label="OI加权" value={rate(data.funding.oi_weighted_rate)} /><SmallMetric label="Basis" value={data.funding.basis_pct == null ? "—" : `${data.funding.basis_pct.toFixed(4)}%`} /><SmallMetric label="24h均值" value={rate(data.funding.avg_24h)} /><SmallMetric label="3d均值" value={rate(data.funding.avg_3d)} /><SmallMetric label="7d均值" value={rate(data.funding.avg_7d)} /><SmallMetric label="30日分位" value={data.funding.percentile_30d == null ? "—" : `${data.funding.percentile_30d.toFixed(1)}%`} /></div><div className="mt-3 text-xs text-slate-400">数据：{qualityText(data.funding.quality)} · 拥挤等级：{data.funding.crowding} · 连续同号结算：{Math.abs(data.funding.same_sign_settlements)} 次{data.funding.same_sign_settlements > 0 ? "正" : data.funding.same_sign_settlements < 0 ? "负" : ""} · 修正 {data.funding.confidence_modifier >= 0 ? "+" : ""}{data.funding.confidence_modifier.toFixed(1)} · {data.funding.modifier_reason}</div></div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title="BTC ETF 净流" note="仅作用于1d背景；按真实交易日，不填充周末/节假日占位" /><div className="mt-4 space-y-2"><SmallMetric label="1个交易日" value={usd(data.etf_flow.net_1d_usd)} valueClass={tone(data.etf_flow.net_1d_usd || 0)} /><SmallMetric label="3个交易日" value={usd(data.etf_flow.net_3_sessions_usd)} valueClass={tone(data.etf_flow.net_3_sessions_usd || 0)} /><SmallMetric label="5个交易日" value={usd(data.etf_flow.net_5_sessions_usd)} valueClass={tone(data.etf_flow.net_5_sessions_usd || 0)} /></div><div className="mt-3 text-[10px] text-slate-500">数据：{qualityText(data.etf_flow.quality)} · 置信度修正 {data.etf_flow.confidence_modifier >= 0 ? "+" : ""}{data.etf_flow.confidence_modifier.toFixed(1)}</div></div>
            <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title="B级实验指标" note="首版固定零权重" /><div className="mt-4 space-y-3 text-sm"><div className="flex justify-between"><span>Footprint</span><span className={data.footprint.available ? "text-emerald-400" : "text-rose-400"}>{!data.footprint.enabled ? "已关闭" : data.footprint.available ? "可用" : "不可用/过期/429"}</span></div><div className="flex justify-between"><span>14日可用率</span><span>{data.footprint.availability_14d_pct == null ? "—" : `${data.footprint.availability_14d_pct.toFixed(2)}%`}</span></div><div className="flex justify-between"><span>消融验证</span><span>{data.footprint.ablation_validated ? "通过" : "未通过/未执行"}</span></div><div className="flex justify-between"><span>入模资格</span><span>{data.footprint.promotion_eligible ? "满足" : "不满足"}</span></div><div className="flex justify-between"><span>评分权重</span><span>{data.footprint.score_weight}</span></div><p className="text-xs text-slate-500">{qualityText(data.footprint.quality)} · {data.footprint.note}</p></div></div>
          </section>
          <footer className="pb-8 text-center text-xs text-slate-600">{data.disclaimer} · 更新时间 {new Date(data.ts * 1000).toLocaleString()}</footer>
        </>}
      </div>
    </main>
  );
}

function SectionTitle({ title, note }: { title: string; note: string }) { return <div><h2 className="font-semibold text-slate-100">{title}</h2><p className="mt-0.5 text-[11px] text-slate-500">{note}</p></div>; }
function Metric({ title, value, sub, accent }: { title: string; value: string; sub: string; accent: "emerald" | "rose" | "blue" | "slate" }) { const colors = { emerald: "text-emerald-400", rose: "text-rose-400", blue: "text-blue-400", slate: "text-slate-200" }; return <div className="rounded-xl border border-slate-800 bg-slate-900/60 p-4"><div className="text-xs text-slate-500">{title}</div><div className={`mt-1 text-2xl font-semibold ${colors[accent]}`}>{value}</div><div className="mt-1 text-[10px] text-slate-600">{sub}</div></div>; }
function SmallMetric({ label, value, valueClass = "text-slate-100" }: { label: string; value: string; valueClass?: string }) { return <div className="rounded-lg bg-slate-950/70 p-3"><div className="text-[10px] text-slate-500">{label}</div><div className={`mt-1 text-sm font-medium ${valueClass}`}>{value}</div></div>; }
function TimeframeCard({ item }: { item?: Timeframe }) { if (!item) return null; return <div className="rounded-lg border border-slate-800 bg-slate-950/60 p-3"><div className="flex items-center justify-between"><span className="font-semibold">{item.timeframe}</span><span className={`text-lg font-semibold ${tone(item.score)}`}>{item.score.toFixed(1)}</span></div><div className="mt-3 space-y-1.5 text-[11px]"><Bar label="价格/量" value={item.price_volume_score} /><Bar label="现货+合约CVD" value={item.orderflow_score} /><Bar label="OI参与" value={item.oi_participation_score} /></div><div className="mt-3 text-[10px] text-slate-500">现货确认：{item.spot_confirms ? "是" : "否"} · {item.oi_interpretation}</div></div>; }
function Bar({ label, value }: { label: string; value: number }) { return <div><div className="flex justify-between text-slate-500"><span>{label}</span><span className={tone(value)}>{value.toFixed(1)}</span></div><div className="mt-1 h-1 overflow-hidden rounded bg-slate-800"><div className={`h-full ${value >= 0 ? "bg-emerald-500" : "bg-rose-500"}`} style={{ width: `${Math.min(100, Math.abs(value))}%`, marginLeft: value < 0 ? `${100 - Math.min(100, Math.abs(value))}%` : 0 }} /></div></div>; }
function FlowPanel({ title, flow }: { title: string; flow?: ActiveFlow }) { return <div className="rounded-xl border border-slate-800 bg-slate-900/55 p-4"><SectionTitle title={title} note={flow?.semantics || "数据未就绪"} /><div className="mt-1 text-[10px] text-slate-600">{qualityText(flow?.quality)}</div><div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-5">{flow?.windows.map((row) => <div key={row.window} className="rounded-lg bg-slate-950/70 p-2 text-center"><div className="text-[10px] text-slate-500">{row.window}</div><div className={`mt-1 text-xs font-semibold ${tone(row.net_usd)}`}>{usd(row.net_usd)}</div><div className="mt-1 text-[9px] text-slate-600">Net/总量 {(row.net_ratio * 100).toFixed(1)}%</div><div className="mt-1 text-[8px] text-slate-700">B {usd(row.buy_usd)} · S {usd(row.sell_usd)}</div><div className="text-[8px] text-slate-700">P{row.historical_percentile == null ? "—" : row.historical_percentile.toFixed(1)}</div></div>)}</div><div className="mt-3 text-[10px] text-slate-500">与本地重建CVD一致：{flow?.cvd_consistent == null ? "待判断" : flow.cvd_consistent ? "是" : "否"}（仅一致性校验，不重复计权）</div></div>; }
function WalletChart({ points }: { points: ChartPoint[] }) { const usable = points.filter((p) => p.net_change_btc != null).slice(-120); if (usable.length < 2) return <div className="flex h-72 items-center justify-center rounded-lg border border-slate-800 text-xs text-slate-600">钱包日级历史不足</div>; const maxAbs = Math.max(...usable.map((p) => Math.abs(p.net_change_btc || 0)), 1); const prices = usable.map((p) => p.price).filter((p): p is number => p != null); const minP = Math.min(...prices), maxP = Math.max(...prices); const pricePath = usable.map((p, i) => { const x = (i / (usable.length - 1)) * 1000; const y = p.price == null || maxP === minP ? 130 : 20 + (1 - (p.price - minP) / (maxP - minP)) * 210; return `${i ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`; }).join(" "); return <div className="h-72 rounded-lg border border-slate-800 bg-slate-950/50 p-2"><svg viewBox="0 0 1000 260" className="h-full w-full" preserveAspectRatio="none"><line x1="0" y1="130" x2="1000" y2="130" stroke="#334155" strokeDasharray="5 5" />{usable.map((p, i) => { const width = Math.max(2, 900 / usable.length); const x = (i / usable.length) * 1000; const delta = p.net_change_btc || 0; const h = Math.abs(delta) / maxAbs * 105; return <rect key={p.ts} x={x} width={width} y={delta >= 0 ? 130 - h : 130} height={h} fill={delta >= 0 ? "#f43f5e" : "#10b981"} opacity="0.65" />; })}<path d={pricePath} fill="none" stroke="#fbbf24" strokeWidth="3" vectorEffect="non-scaling-stroke" /></svg></div>; }
