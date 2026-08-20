"use client";

import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type {
  EvidenceItem,
  EvidencePillar,
  MarketIncidentSnapshot,
  RiskStage,
  SourceQuality,
} from "@/lib/marketRiskTypes";

const STAGE_LABEL: Record<RiskStage, string> = {
  normal: "正常",
  watch: "观察",
  warning: "预警",
  critical: "临界",
  cooldown: "冷却",
  resolved: "已结束",
};

const STAGE_TONE: Record<RiskStage, string> = {
  normal: "border-emerald-700/50 bg-emerald-950/40 text-emerald-200",
  watch: "border-amber-700/50 bg-amber-950/40 text-amber-200",
  warning: "border-orange-600/60 bg-orange-950/50 text-orange-100",
  critical: "border-rose-500/70 bg-rose-950/60 text-rose-100",
  cooldown: "border-sky-700/50 bg-sky-950/40 text-sky-200",
  resolved: "border-slate-700 bg-slate-900 text-slate-300",
};

const PILLARS: Array<{ key: EvidencePillar; label: string; hint: string }> = [
  { key: "spot_demand", label: "现货需求", hint: "真实主动买卖、Spot CVD；正式预警必须有它" },
  { key: "leveraged_positioning", label: "杠杆结构", hint: "标准化 OI、合约主动成交、Funding" },
  { key: "liquidation_risk", label: "清算风险", hint: "已实现强平与估算密度严格分开" },
  { key: "liquidity_structure", label: "流动性结构", hint: "墙体可信度、被吃、撤单和重挂" },
  { key: "market_response", label: "市场反应", hint: "首版仅信息展示，不单独触发预警" },
  { key: "context", label: "背景上下文", hint: "ETF、期权、链上、稳定币；首版不计分" },
];

function formatTime(ts: number): string {
  if (!ts) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit",
    hour12: false,
  }).format(new Date(ts * 1000));
}

function directionLabel(direction: string): string {
  return direction === "up" ? "上行风险" : direction === "down" ? "下行风险" : direction === "mixed" ? "方向冲突" : "方向未知";
}

function qualityLabel(quality: SourceQuality): string {
  if (quality.decision_usable) return "可用于决策";
  if (quality.continuity === "gap") return "断流：禁止升级";
  if (quality.freshness === "stale") return "过期：禁止升级";
  if (quality.validity === "invalid") return "口径无效";
  return "暂不可用";
}

export default function MarketRiskPage({ params }: PageProps<"/market-risk/[coin]">) {
  const { coin: routeCoin } = use(params);
  const coin = routeCoin.toUpperCase();
  const [snapshot, setSnapshot] = useState<MarketIncidentSnapshot | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    try {
      const response = await fetch(`${API_BASE}/api/market-risk/${coin}`, { cache: "no-store" });
      if (!response.ok) throw new Error(response.status === 503 ? "联合风险引擎正在暖机" : `接口错误 ${response.status}`);
      setSnapshot(await response.json() as MarketIncidentSnapshot);
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

  const evidence = useMemo(
    () => [...(snapshot?.evidence ?? [])].sort((a, b) => b.event_time - a.event_time),
    [snapshot],
  );

  return (
    <main className="min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/80 px-5 py-3">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4">
          <div>
            <div className="flex items-center gap-3">
              <Link href="/" className="text-sm text-slate-400 hover:text-white">← 返回</Link>
              <h1 className="text-lg font-semibold">LIQ {coin} 联合风险预警</h1>
            </div>
            <p className="mt-1 text-xs text-slate-500">资金流 + 现货成交 + 杠杆结构 + 清算 + 流动性；只给风险证据，不自动交易或计算仓位。</p>
          </div>
          <button onClick={() => void refresh()} className="rounded border border-slate-700 px-3 py-1.5 text-xs text-slate-300 hover:border-slate-500">刷新</button>
        </div>
      </header>

      <div className="mx-auto max-w-7xl space-y-4 p-5">
        {loading && <div className="rounded border border-slate-800 bg-slate-900/50 p-6 text-sm text-slate-400">正在读取后台物化快照…</div>}
        {error && <div className="rounded border border-amber-800/60 bg-amber-950/30 p-3 text-sm text-amber-200">{error}</div>}

        {snapshot && (
          <>
            {!snapshot.calibration_admitted && (
              <div className="rounded border border-violet-800/60 bg-violet-950/30 px-4 py-3 text-sm text-violet-200">
                当前为 shadow 研究模式：校准产物尚未通过样本量、Wilson 下界和最强基线门槛，因此不会发送市场风险邮件。
              </div>
            )}
            {snapshot.quality_layer === "data_degraded" && (
              <div className="rounded border border-rose-700/60 bg-rose-950/40 px-4 py-3 text-sm text-rose-100">
                数据降级：页面保留最后已知阶段，但系统禁止升级、解决和发送邮件。请先看下方具体断流/过期来源。
              </div>
            )}

            <section className="grid gap-3 md:grid-cols-4">
              <div className={`rounded-lg border p-4 ${STAGE_TONE[snapshot.stage]}`}>
                <div className="text-xs opacity-70">提前风险轨道</div>
                <div className="mt-1 text-2xl font-bold">{STAGE_LABEL[snapshot.stage]}</div>
                <div className="mt-2 text-xs">{directionLabel(snapshot.direction)}</div>
              </div>
              <Metric label="独立因果根" value={`${snapshot.independent_root_count}`} hint="同根指标只增强可信度，不重复计票" />
              <Metric label="现货确认" value={snapshot.spot_confirmed ? "已确认" : "未确认"} hint="warning 的硬门槛" />
              <Metric label="决策时间" value={formatTime(snapshot.decision_time)} hint={`watermark ${formatTime(snapshot.watermark)}`} />
            </section>

            {snapshot.research_signals.includes("derivative_led_watch") && (
              <div className="rounded border border-amber-800/60 bg-amber-950/20 px-4 py-3 text-sm text-amber-200">
                衍生品先行观察：合约/OI/Funding/清算出现异常，但现货尚未确认。只记入账本，不是正式 warning。
              </div>
            )}

            <section>
              <h2 className="mb-2 text-sm font-semibold text-slate-200">六大证据柱</h2>
              <div className="grid gap-2 md:grid-cols-3">
                {PILLARS.map(({ key, label, hint }) => {
                  const pillar = snapshot.pillars[key];
                  return (
                    <div key={key} className="rounded-lg border border-slate-800 bg-slate-900/55 p-3">
                      <div className="flex items-center justify-between">
                        <span className="text-sm font-medium">{label}</span>
                        <span className={`rounded px-1.5 py-0.5 text-[10px] ${pillar?.decision_usable ? "bg-emerald-950 text-emerald-300" : "bg-slate-800 text-slate-500"}`}>
                          {pillar?.decision_usable ? "可用" : "不可用"}
                        </span>
                      </div>
                      <div className="mt-2 text-xl font-semibold tabular-nums">{Math.round((pillar?.confidence ?? 0) * 100)}%</div>
                      <div className="mt-1 text-xs text-slate-500">{hint}</div>
                      <div className="mt-2 text-[11px] text-slate-400">因果根：{pillar?.causal_roots.join("、") || "—"}</div>
                    </div>
                  );
                })}
              </div>
            </section>

            <section className="grid gap-4 lg:grid-cols-[1.4fr_1fr]">
              <div className="rounded-lg border border-slate-800 bg-slate-900/45">
                <div className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">证据时间线</div>
                <div className="divide-y divide-slate-800/70">
                  {evidence.length ? evidence.map((item) => <EvidenceRow key={item.evidence_id} item={item} />) : (
                    <div className="p-5 text-sm text-slate-500">当前没有达到 research 阈值的证据。</div>
                  )}
                </div>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/45">
                <div className="border-b border-slate-800 px-4 py-3 text-sm font-semibold">数据质量</div>
                <div className="divide-y divide-slate-800/70">
                  {Object.values(snapshot.source_quality).map((quality) => (
                    <div key={quality.source_id} className="px-4 py-3 text-xs">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-medium text-slate-200">{quality.source_id}</span>
                        <span className={quality.decision_usable ? "text-emerald-300" : "text-rose-300"}>{qualityLabel(quality)}</span>
                      </div>
                      <div className="mt-1 text-slate-500">as-of {formatTime(quality.as_of)} · {quality.continuity}</div>
                      {!!quality.reasons.length && <div className="mt-1 text-amber-300/80">{quality.reasons.join("；")}</div>}
                    </div>
                  ))}
                </div>
              </div>
            </section>

            <section className="rounded-lg border border-slate-800 bg-slate-900/45 p-4 text-xs text-slate-400">
              <div>incident：{snapshot.incident_id ?? "—"}　episode：{snapshot.episode_id ?? "—"}</div>
              <div className="mt-1">calibration：{snapshot.calibration_version}　config：{snapshot.config_version}</div>
              <div className="mt-2 text-slate-500">ETF、期权、BTC 原生链与稳定币首版只在 Context 显示；钱包转账不等于交易所或机构在市场买卖。</div>
            </section>
          </>
        )}
      </div>
    </main>
  );
}

function Metric({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/55 p-4">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 text-lg font-semibold text-slate-100">{value}</div>
      <div className="mt-2 text-[11px] text-slate-500">{hint}</div>
    </div>
  );
}

function EvidenceRow({ item }: { item: EvidenceItem }) {
  const tone = item.direction === "up" ? "text-emerald-300" : item.direction === "down" ? "text-rose-300" : "text-slate-400";
  return (
    <div className="px-4 py-3">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="text-slate-500 tabular-nums">{formatTime(item.event_time)}</span>
        <span className={`font-semibold ${tone}`}>{directionLabel(item.direction)}</span>
        <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">{item.causal_root}</span>
        {item.role !== "scoring" && <span className="rounded bg-sky-950 px-1.5 py-0.5 text-[10px] text-sky-300">仅信息</span>}
        <span className="ml-auto tabular-nums text-slate-400">可信 {Math.round(item.confidence * 100)}%</span>
      </div>
      <div className="mt-1 text-sm text-slate-200">{item.name}</div>
      <div className="mt-1 text-xs leading-5 text-slate-500">{item.explanation}</div>
    </div>
  );
}
