"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import type {
  SMCConfirmation,
  SMCHorizon,
  SMCLiquidityPool,
  SMCSnapshot,
  SMCZone,
} from "@/lib/types";

const OBS_LABEL: Record<string, string> = {
  long_watch: "做多观察",
  short_watch: "做空观察",
  wait: "等待",
};

const STATE_LABEL: Record<string, string> = {
  candidate: "候选",
  raid_detected: "已扫流动性",
  mss_confirmed: "结构已确认",
  entry_zone_active: "观察区激活",
  invalidated: "失效",
  expired: "过期",
};

function fmtPrice(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return v >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 1 }) : v.toFixed(4);
}

function fmtPct(v?: number | null) {
  if (v === null || v === undefined || Number.isNaN(v)) return "-";
  return `${v > 0 ? "+" : ""}${v.toFixed(2)}%`;
}

function tone(snapshot: SMCSnapshot | null) {
  if (!snapshot) return "border-slate-700 text-slate-300";
  if (snapshot.observation === "long_watch") return "border-emerald-500/50 text-emerald-200";
  if (snapshot.observation === "short_watch") return "border-rose-500/50 text-rose-200";
  return "border-slate-700 text-slate-300";
}

function Section({
  title,
  children,
  className = "",
}: {
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`min-w-0 border border-slate-800 bg-slate-900/40 ${className}`}>
      <div className="border-b border-slate-800 px-3 py-2 text-[11px] font-semibold text-slate-300">
        {title}
      </div>
      <div className="p-3">{children}</div>
    </section>
  );
}

function Empty({ text }: { text: string }) {
  return <div className="py-4 text-center text-[11px] text-slate-600">{text}</div>;
}

function ZoneList({ zones }: { zones: SMCZone[] }) {
  if (!zones.length) return <Empty text="暂无候选区" />;
  return (
    <div className="space-y-2">
      {zones.slice(0, 8).map((z) => (
        <div key={z.zone_id} className="border border-slate-800 bg-slate-950/35 p-2">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="text-[12px] font-semibold text-slate-100">
                {z.kind} · {z.role}
              </div>
              <div className="mt-1 text-[11px] text-slate-500">
                {fmtPrice(z.price_from)} - {fmtPrice(z.price_to)} · {fmtPct(z.distance_pct)}
              </div>
            </div>
            <div className="shrink-0 text-right">
              <div className="text-[11px] text-slate-300">{STATE_LABEL[z.state] ?? z.state}</div>
              <div className="text-[10px] text-slate-500">强度 {Math.round(z.strength)}</div>
            </div>
          </div>
          {z.evidence[0] && (
            <div className="mt-2 truncate text-[10px] text-slate-500">{z.evidence[0]}</div>
          )}
        </div>
      ))}
    </div>
  );
}

function PoolList({ pools }: { pools: SMCLiquidityPool[] }) {
  if (!pools.length) return <Empty text="暂无流动性池" />;
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {pools.slice(0, 10).map((p) => (
        <div key={p.pool_id} className="border border-slate-800 bg-slate-950/35 p-2">
          <div className="flex items-center justify-between gap-2">
            <span className={p.side === "buy_side" ? "text-[12px] text-rose-200" : "text-[12px] text-emerald-200"}>
              {p.side === "buy_side" ? "上方买方流动性" : "下方卖方流动性"}
            </span>
            <span className="text-[10px] text-slate-500">{p.timeframe}</span>
          </div>
          <div className="mt-1 flex items-end justify-between">
            <span className="text-sm font-semibold text-slate-100">{fmtPrice(p.price)}</span>
            <span className="text-[11px] text-slate-500">{fmtPct(p.distance_pct)}</span>
          </div>
          <div className="mt-1 text-[10px] text-slate-600">{p.source} · 强度 {Math.round(p.strength)}</div>
        </div>
      ))}
    </div>
  );
}

function ConfirmationList({ items }: { items: SMCConfirmation[] }) {
  if (!items.length) return <Empty text="暂无确认项" />;
  return (
    <div className="space-y-2">
      {items.slice(0, 10).map((c, idx) => (
        <div key={`${c.source}-${idx}`} className="flex items-start justify-between gap-3 border-b border-slate-800/70 pb-2 last:border-0 last:pb-0">
          <div className="min-w-0">
            <div className="text-[12px] text-slate-200">{c.source}</div>
            <div className="mt-0.5 text-[10px] text-slate-500">{c.note}</div>
          </div>
          <div className="shrink-0 text-right">
            <div className={c.direction === "bullish" ? "text-[11px] text-emerald-300" : c.direction === "bearish" ? "text-[11px] text-rose-300" : "text-[11px] text-slate-400"}>
              {c.direction}
            </div>
            <div className="text-[10px] text-slate-500">{c.score_delta > 0 ? "+" : ""}{c.score_delta.toFixed(1)}</div>
          </div>
        </div>
      ))}
    </div>
  );
}

export default function SMCPage() {
  const params = useParams();
  const coin = ((params.coin as string) || "BTC").toUpperCase();
  const [horizon, setHorizon] = useState<SMCHorizon>("intraday");
  const key = `${coin}:${horizon}`;

  const snap = useMarketStore((s) => s.smcByKey[key] ?? null);
  const loading = useMarketStore((s) => s.smcLoadingByKey[key] ?? false);
  const err = useMarketStore((s) => s.smcErrorByKey[key] ?? null);
  const breadth = useMarketStore((s) => s.smcMarketBreadth);
  const loadSMC = useMarketStore((s) => s.loadSMC);
  const loadBreadth = useMarketStore((s) => s.loadSMCMarketBreadth);

  useEffect(() => {
    loadSMC(coin, horizon).catch(() => undefined);
    loadBreadth().catch(() => undefined);
    const t = setInterval(() => {
      loadSMC(coin, horizon).catch(() => undefined);
      loadBreadth().catch(() => undefined);
    }, 60_000);
    return () => clearInterval(t);
  }, [coin, horizon, loadSMC, loadBreadth]);

  const activeZones = useMemo(
    () => (snap?.zones ?? []).filter((z) => z.state === "entry_zone_active"),
    [snap],
  );

  return (
    <div className="smc-page min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-900/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-[12px] text-slate-500 hover:text-slate-300">
              ← 仪表盘
            </Link>
            <h1 className="text-sm font-semibold text-slate-100">
              SMC 聪明钱分析 · <span className="text-blue-300">{coin}</span>
            </h1>
            {loading && <span className="text-[10px] text-slate-600">刷新中</span>}
          </div>
          <div className="flex rounded border border-slate-700 bg-slate-950 p-0.5">
            {(["intraday", "swing"] as SMCHorizon[]).map((h) => (
              <button
                key={h}
                onClick={() => setHorizon(h)}
                className={`px-3 py-1 text-[12px] ${horizon === h ? "bg-blue-600 text-white" : "text-slate-400 hover:text-slate-100"}`}
              >
                {h === "intraday" ? "日内" : "波段"}
              </button>
            ))}
          </div>
        </div>
      </header>

      {err && (
        <div className="border-b border-rose-900/40 bg-rose-950/30 px-4 py-2 text-center text-[12px] text-rose-200">
          {err}
        </div>
      )}

      {!snap && !err && (
        <div className="flex min-h-[60vh] items-center justify-center text-[12px] text-slate-500">
          加载 SMC 视图…
        </div>
      )}

      {snap && (
        <main className="mx-auto flex max-w-[1500px] flex-col gap-3 px-3 py-3">
          <section className={`border bg-slate-900/40 p-3 ${tone(snap)}`}>
            <div className="grid gap-3 lg:grid-cols-[1.4fr_1fr_1fr_1fr]">
              <div>
                <div className="text-[11px] text-slate-500">观察方向</div>
                <div className="mt-1 text-2xl font-semibold">{OBS_LABEL[snap.observation]}</div>
                <p className="mt-2 text-[12px] leading-relaxed text-slate-400">{snap.summary}</p>
              </div>
              <div>
                <div className="text-[11px] text-slate-500">状态</div>
                <div className="mt-1 text-lg font-semibold text-slate-100">{STATE_LABEL[snap.setup_state]}</div>
                <div className="mt-2 text-[12px] text-slate-500">现价 {fmtPrice(snap.last_price)}</div>
              </div>
              <div>
                <div className="text-[11px] text-slate-500">置信度</div>
                <div className="mt-1 text-lg font-semibold text-slate-100">{snap.confidence}/100</div>
                <div className="mt-2 h-1.5 bg-slate-800">
                  <div className="h-full bg-blue-500" style={{ width: `${snap.confidence}%` }} />
                </div>
              </div>
              <div>
                <div className="text-[11px] text-slate-500">失效价</div>
                <div className="mt-1 text-lg font-semibold text-slate-100">{fmtPrice(snap.invalidation_price)}</div>
                <div className="mt-2 text-[12px] text-slate-500">质量 {snap.data_quality.status} · {snap.data_quality.score}</div>
              </div>
            </div>
          </section>

          <div className="grid gap-3 xl:grid-cols-[1.05fr_1fr_0.95fr]">
            <Section title="候选区">
              <ZoneList zones={activeZones.length ? activeZones : snap.zones} />
            </Section>
            <Section title="流动性与扫损">
              <PoolList pools={snap.liquidity_pools} />
            </Section>
            <Section title="结构事件">
              {snap.structure.length ? (
                <div className="space-y-2">
                  {snap.structure.slice(0, 10).map((e) => (
                    <div key={e.event_id} className="flex items-start justify-between gap-3 border-b border-slate-800/70 pb-2 last:border-0 last:pb-0">
                      <div>
                        <div className="text-[12px] text-slate-200">{e.kind} · {e.direction}</div>
                        <div className="text-[10px] text-slate-500">{e.note}</div>
                      </div>
                      <div className="text-right text-[11px] text-slate-400">
                        <div>{fmtPrice(e.price)}</div>
                        <div>{e.timeframe}</div>
                      </div>
                    </div>
                  ))}
                </div>
              ) : <Empty text="暂无结构事件" />}
            </Section>
          </div>

          <div className="grid gap-3 xl:grid-cols-[1fr_1fr_1fr]">
            <Section title="订单流确认">
              <ConfirmationList items={snap.confirmations.filter((c) => !c.source.startsWith("nansen"))} />
            </Section>
            <Section title="Nansen 确认">
              <div className="mb-3 grid grid-cols-3 gap-2 text-center">
                <div className="border border-slate-800 bg-slate-950/35 p-2">
                  <div className="text-[10px] text-slate-500">状态</div>
                  <div className="text-[12px] text-slate-100">{snap.smart_money.status}</div>
                </div>
                <div className="border border-slate-800 bg-slate-950/35 p-2">
                  <div className="text-[10px] text-slate-500">偏向</div>
                  <div className="text-[12px] text-slate-100">{snap.smart_money.bias}</div>
                </div>
                <div className="border border-slate-800 bg-slate-950/35 p-2">
                  <div className="text-[10px] text-slate-500">宽度</div>
                  <div className="text-[12px] text-slate-100">{breadth ? breadth.breadth_score.toFixed(1) : "-"}</div>
                </div>
              </div>
              <ConfirmationList items={snap.confirmations.filter((c) => c.source.startsWith("nansen"))} />
            </Section>
            <Section title="反证与数据质量">
              <div className="space-y-2">
                {snap.contradictions.length ? snap.contradictions.map((c, idx) => (
                  <div key={`${c.source}-${idx}`} className="border border-slate-800 bg-slate-950/35 p-2">
                    <div className="text-[12px] text-amber-200">{c.source} · {c.severity}</div>
                    <div className="mt-1 text-[10px] text-slate-500">{c.note}</div>
                  </div>
                )) : <Empty text="暂无明确反证" />}
                <div className="border-t border-slate-800 pt-2 text-[11px] text-slate-500">
                  缺失：{snap.data_quality.missing.length ? snap.data_quality.missing.join(", ") : "无"}
                </div>
                <div className="text-[11px] text-slate-500">
                  降级：{snap.data_quality.degraded.length ? snap.data_quality.degraded.join(", ") : "无"}
                </div>
              </div>
            </Section>
          </div>

          <Section title="目标观察区">
            {snap.targets.length ? (
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {snap.targets.map((t, idx) => (
                  <div key={`${t.kind}-${idx}`} className="border border-slate-800 bg-slate-950/35 p-2">
                    <div className="flex items-center justify-between">
                      <span className="text-[12px] text-slate-200">{t.kind}</span>
                      <span className="text-[11px] text-slate-500">{fmtPct(t.distance_pct)}</span>
                    </div>
                    <div className="mt-1 text-sm font-semibold text-slate-100">{fmtPrice(t.price)}</div>
                    <div className="mt-1 text-[10px] text-slate-500">{t.note}</div>
                  </div>
                ))}
              </div>
            ) : <Empty text="暂无目标观察区" />}
          </Section>
        </main>
      )}
    </div>
  );
}
