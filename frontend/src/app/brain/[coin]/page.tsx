"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";
import type { BrainPriceZone } from "@/lib/types";

function roleBadges(z: BrainPriceZone): string[] {
  const r = z.roles;
  const out: string[] = [];
  if (r.key_level) out.push("关键位");
  if (r.spot_supply_wall) out.push("现货供需墙");
  if (r.futures_liquidity_wall) out.push("合约流动性墙");
  if (r.liquidation_magnet) out.push("清算磁铁");
  if (r.coinbase_confluence) out.push("Coinbase");
  return out;
}

export default function TradingBrainPage() {
  const params = useParams();
  const coinParam = (params.coin as string)?.toUpperCase() ?? "BTC";

  const snap = useMarketStore((s) => s.tradingBrainByCoin[coinParam] ?? null);
  const loading = useMarketStore((s) => s.tradingBrainLoadingByCoin[coinParam] ?? false);
  const err = useMarketStore((s) => s.tradingBrainErrorByCoin[coinParam] ?? null);
  const loadTradingBrain = useMarketStore((s) => s.loadTradingBrain);

  useEffect(() => {
    loadTradingBrain(coinParam).catch(() => undefined);
    const t = setInterval(() => {
      loadTradingBrain(coinParam).catch(() => undefined);
    }, 45_000);
    return () => clearInterval(t);
  }, [coinParam, loadTradingBrain]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <header className="sticky top-0 z-10 border-b border-slate-800 bg-slate-900/90 px-4 py-3 backdrop-blur">
        <div className="mx-auto flex max-w-5xl flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-3">
            <Link
              href="/"
              className="text-[12px] text-slate-500 hover:text-slate-300"
            >
              ← 仪表盘
            </Link>
            <h1 className="text-sm font-semibold text-slate-100">
              交易大脑 · <span className="text-blue-400">{coinParam}</span>
            </h1>
          </div>
          {snap && (
            <div className="text-[11px] text-slate-500">
              现价 {formatPrice(snap.last_price)} · ATR {snap.atr}
              {loading ? " · 刷新中…" : ""}
            </div>
          )}
        </div>
      </header>

      <main className="mx-auto max-w-5xl space-y-4 px-4 py-4">
        {err && (
          <div className="rounded-md border border-rose-900/60 bg-rose-950/30 px-3 py-2 text-[12px] text-rose-200">
            {err}
          </div>
        )}

        {!snap && !err && loading && (
          <p className="text-[12px] text-slate-500">加载聚合视图…</p>
        )}

        {snap && (
          <>
            <section className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
              <h2 className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                结构摘要
              </h2>
              <p className="mt-2 text-[13px] leading-relaxed text-slate-200">{snap.summary}</p>
            </section>

            <section className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
              <h2 className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                情境 · 资金
              </h2>
              <ul className="mt-2 flex flex-wrap gap-2 text-[11px]">
                {snap.context.regime && (
                  <li className="rounded border border-slate-700 bg-slate-800/80 px-2 py-1">
                    Regime：{snap.context.regime}
                  </li>
                )}
                {snap.context.regime_description && (
                  <li className="rounded border border-slate-700 bg-slate-800/80 px-2 py-1 max-w-full">
                    {snap.context.regime_description}
                  </li>
                )}
                {snap.context.cvd_contract_trend && (
                  <li className="rounded border border-slate-700 px-2 py-1">
                    CVD合约：{snap.context.cvd_contract_trend}
                  </li>
                )}
                {snap.context.cvd_spot_trend && (
                  <li className="rounded border border-slate-700 px-2 py-1">
                    CVD现货：{snap.context.cvd_spot_trend}
                  </li>
                )}
                {snap.context.oi_delta_1h_pct != null && (
                  <li className="rounded border border-slate-700 px-2 py-1">
                    OI 1h：{snap.context.oi_delta_1h_pct.toFixed(3)}%
                  </li>
                )}
                {snap.context.funding_interpretation && (
                  <li className="rounded border border-amber-900/40 bg-amber-950/20 px-2 py-1">
                    资金费：{snap.context.funding_interpretation}
                  </li>
                )}
                {snap.context.nearest_magnet_below != null && (
                  <li className="rounded border border-slate-700 px-2 py-1">
                    近侧磁铁（下）：{formatPrice(snap.context.nearest_magnet_below)}
                  </li>
                )}
                {snap.context.nearest_magnet_above != null && (
                  <li className="rounded border border-slate-700 px-2 py-1">
                    近侧磁铁（上）：{formatPrice(snap.context.nearest_magnet_above)}
                  </li>
                )}
              </ul>
            </section>

            <section className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
              <h2 className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                数据质量
              </h2>
              <div className="mt-2 space-y-1 text-[11px] text-slate-400">
                <div>
                  流动性墙：
                  <span className="text-slate-200">
                    {snap.data_quality.liquidity_wall_quality || "—"}
                  </span>
                  {snap.data_quality.usd_usdt_basis_pct != null &&
                    ` · USD/USDT 基差 ${snap.data_quality.usd_usdt_basis_pct.toFixed(4)}%`}
                </div>
                {snap.data_quality.overall_freshness_score != null && (
                  <div>
                    关键位新鲜度评分：{snap.data_quality.overall_freshness_score.toFixed(2)}
                  </div>
                )}
                {snap.data_quality.stale_sources?.length > 0 && (
                  <div>陈旧源：{snap.data_quality.stale_sources.join(", ")}</div>
                )}
                {snap.data_quality.missing_sources?.length > 0 && (
                  <div>缺失源：{snap.data_quality.missing_sources.join(", ")}</div>
                )}
                {snap.data_quality.notes.map((n) => (
                  <div key={n} className="text-amber-200/90">
                    {n}
                  </div>
                ))}
              </div>
            </section>

            <section className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
              <h2 className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                价格区（由近及远）
              </h2>
              <ul className="mt-3 space-y-3">
                {snap.zones.map((z) => (
                  <li
                    key={z.zone_id}
                    className="rounded-md border border-slate-800 bg-slate-950/60 p-3"
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-2">
                      <span className="text-[13px] font-medium text-slate-100">
                        {formatPrice(z.price_mid)}{" "}
                        <span className="text-[11px] font-normal text-slate-500">
                          [{z.price_low.toFixed(0)} – {z.price_high.toFixed(0)}]
                        </span>
                      </span>
                      <span className="text-[11px] text-slate-500">
                        距现价 {z.distance_pct > 0 ? "+" : ""}
                        {z.distance_pct.toFixed(2)}%
                      </span>
                    </div>
                    <div className="mt-1 text-[12px] text-slate-300">{z.dominant_label}</div>
                    <div className="mt-1 flex flex-wrap gap-1">
                      {roleBadges(z).map((b) => (
                        <span
                          key={b}
                          className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400"
                        >
                          {b}
                        </span>
                      ))}
                    </div>
                    <div className="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] text-slate-500 sm:grid-cols-4">
                      <span>支撑信任 {z.support_trust.toFixed(2)}</span>
                      <span>阻力信任 {z.resistance_trust.toFixed(2)}</span>
                      <span>扫单吸引 {z.sweep_attractiveness.toFixed(2)}</span>
                      <span>打穿风险评分 {z.break_through_risk.toFixed(2)}</span>
                    </div>
                    {z.layer_notes.length > 0 && (
                      <ul className="mt-2 list-inside list-disc text-[11px] text-slate-500">
                        {z.layer_notes.map((ln) => (
                          <li key={ln}>{ln}</li>
                        ))}
                      </ul>
                    )}
                    <ul className="mt-2 space-y-1 border-t border-slate-800/80 pt-2 text-[11px] text-slate-400">
                      {z.evidence.slice(0, 8).map((e) => (
                        <li key={e} className="leading-snug">
                          {e}
                        </li>
                      ))}
                    </ul>
                  </li>
                ))}
              </ul>
            </section>

            <section className="grid gap-3 sm:grid-cols-2">
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[11px]">
                <h3 className="font-medium text-slate-400">排行：支撑信任</h3>
                <ol className="mt-2 list-decimal space-y-0.5 pl-4 text-slate-500">
                  {snap.rankings.support_trust.map((id) => (
                    <li key={id} className="truncate font-mono text-[10px]">
                      {id}
                    </li>
                  ))}
                </ol>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[11px]">
                <h3 className="font-medium text-slate-400">排行：阻力信任</h3>
                <ol className="mt-2 list-decimal space-y-0.5 pl-4 text-slate-500">
                  {snap.rankings.resistance_trust.map((id) => (
                    <li key={id} className="truncate font-mono text-[10px]">
                      {id}
                    </li>
                  ))}
                </ol>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[11px]">
                <h3 className="font-medium text-slate-400">排行：扫单相关</h3>
                <ol className="mt-2 list-decimal space-y-0.5 pl-4 text-slate-500">
                  {snap.rankings.sweep_targets.map((id) => (
                    <li key={id} className="truncate font-mono text-[10px]">
                      {id}
                    </li>
                  ))}
                </ol>
              </div>
              <div className="rounded-lg border border-slate-800 bg-slate-900/50 p-3 text-[11px]">
                <h3 className="font-medium text-slate-400">排行：打穿风险评分</h3>
                <ol className="mt-2 list-decimal space-y-0.5 pl-4 text-slate-500">
                  {snap.rankings.break_through_risk.map((id) => (
                    <li key={id} className="truncate font-mono text-[10px]">
                      {id}
                    </li>
                  ))}
                </ol>
              </div>
            </section>

            <section className="rounded-lg border border-slate-800 bg-slate-900/50 p-3">
              <h2 className="text-[11px] font-medium uppercase tracking-wide text-slate-500">
                流动性墙事件（近 30 分钟）
              </h2>
              <ul className="mt-2 max-h-64 space-y-1 overflow-y-auto text-[11px]">
                {snap.events.length === 0 && (
                  <li className="text-slate-600">暂无近期事件</li>
                )}
                {snap.events.map((e) => (
                  <li
                    key={`${e.ts}-${e.zone_id}-${e.message}`}
                    className="border-b border-slate-800/60 py-1 text-slate-400 last:border-0"
                  >
                    <span className="text-slate-500">
                      {new Date(e.ts * 1000).toLocaleTimeString("zh-CN")}
                    </span>{" "}
                    [{e.layer}] {e.message}
                    {e.price_mid ? ` · ${formatPrice(e.price_mid)}` : ""}
                  </li>
                ))}
              </ul>
            </section>

            <p className="pb-8 text-center text-[10px] text-slate-600">
              本页为市场结构与流动性辅助视图；不构成任何交易指令或投顾建议。
            </p>
          </>
        )}
      </main>
    </div>
  );
}
