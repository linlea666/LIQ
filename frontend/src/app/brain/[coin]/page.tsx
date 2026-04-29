"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import TopStrip from "@/components/Brain/TopStrip";
import PriceAxisMap from "@/components/Brain/PriceAxisMap";
import ZoneDetailCard from "@/components/Brain/ZoneDetailCard";
import { ROLE_COLORS } from "@/components/Brain/types";

export default function TradingBrainPage() {
  const params = useParams();
  const coinParam = (params.coin as string)?.toUpperCase() ?? "BTC";

  const snap = useMarketStore((s) => s.tradingBrainByCoin[coinParam] ?? null);
  const loading = useMarketStore((s) => s.tradingBrainLoadingByCoin[coinParam] ?? false);
  const err = useMarketStore((s) => s.tradingBrainErrorByCoin[coinParam] ?? null);
  const loadTradingBrain = useMarketStore((s) => s.loadTradingBrain);

  const [selectedId, setSelectedId] = useState<string | null>(null);

  useEffect(() => {
    loadTradingBrain(coinParam).catch(() => undefined);
    const t = setInterval(() => {
      loadTradingBrain(coinParam).catch(() => undefined);
    }, 45_000);
    return () => clearInterval(t);
  }, [coinParam, loadTradingBrain]);

  // 默认选中距现价最近的 zone
  useEffect(() => {
    if (!snap) return;
    if (selectedId && snap.zones.some((z) => z.zone_id === selectedId)) return;
    const z0 = snap.zones[0];
    if (z0) setSelectedId(z0.zone_id);
  }, [snap, selectedId]);

  const selectedZone = useMemo(
    () => snap?.zones.find((z) => z.zone_id === selectedId) ?? null,
    [snap, selectedId],
  );

  return (
    <div className="flex h-screen flex-col bg-slate-950 text-slate-200">
      <header className="shrink-0 border-b border-slate-800 bg-slate-900/95 px-4 py-2 backdrop-blur">
        <div className="flex items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-[12px] text-slate-500 hover:text-slate-300">
              ← 仪表盘
            </Link>
            <h1 className="text-sm font-semibold text-slate-100">
              交易大脑 · <span className="text-blue-400">{coinParam}</span>
            </h1>
            <span className="text-[10px] text-slate-600">
              结构与流动性辅助视图 · 不含交易指令
            </span>
          </div>
          {snap && (
            <div className="flex items-center gap-2">
              {(["spot_defense", "futures_target", "liquidation_magnet", "contested", "key_level_only"] as const).map((r) => (
                <span key={r} className="flex items-center gap-1 text-[10px] text-slate-400">
                  <span className="inline-block h-2 w-2 rounded" style={{ background: ROLE_COLORS[r].hex }} />
                  {ROLE_COLORS[r].label}
                </span>
              ))}
            </div>
          )}
        </div>
      </header>

      {snap?.data_quality?.is_partial_ready && (
        <div className="shrink-0 border-b border-amber-900/40 bg-amber-950/20 px-4 py-1.5 text-center text-[11px] text-amber-200">
          数据未就绪：{snap.data_quality.ready_count}/{snap.data_quality.total_count} 项核心源已接入，
          暖机期间仅显示已到达的字段
        </div>
      )}

      {err && (
        <div className="shrink-0 border-b border-rose-900/40 bg-rose-950/30 px-4 py-1.5 text-center text-[11px] text-rose-200">
          {err}
        </div>
      )}

      {!snap && !err && (
        <div className="flex flex-1 items-center justify-center text-[12px] text-slate-500">
          加载交易大脑视图…
        </div>
      )}

      {snap && (
        <>
          <div className="shrink-0 border-b border-slate-800 bg-slate-900/40">
            <TopStrip snap={snap} loading={loading} />
            {snap.summary && (
              <p className="border-t border-slate-800/60 px-3 py-1.5 text-[11px] leading-relaxed text-slate-400">
                {snap.summary}
              </p>
            )}
          </div>

          <main className="flex flex-1 min-h-0 overflow-hidden">
            {/* 左：价格轴 */}
            <section className="w-[240px] shrink-0 border-r border-slate-800 bg-slate-900/30">
              <PriceAxisMap
                zones={snap.zones}
                lastPrice={snap.last_price}
                atr={snap.atr}
                coin={snap.coin}
                selectedId={selectedId}
                onSelect={setSelectedId}
              />
            </section>

            {/* 中：Zone Detail */}
            <section className="flex-1 min-w-0 border-r border-slate-800">
              <ZoneDetailCard zone={selectedZone} coin={snap.coin} />
            </section>

            {/* 右：机会雷达 placeholder（Phase 3b 接入） */}
            <section className="w-[360px] shrink-0 bg-slate-900/30 p-3">
              <h3 className="text-[10px] uppercase tracking-wider text-slate-500">
                机会雷达 · {snap.opportunities.length} 项观察区
              </h3>
              <p className="mt-2 text-[11px] text-slate-600">
                Phase 3b 即将接入：等待触发 / 已触发 / 失效冷却 三列 Kanban
              </p>
              <ul className="mt-2 space-y-1 text-[11px] text-slate-400">
                {snap.opportunities.slice(0, 6).map((o) => (
                  <li
                    key={o.setup_id}
                    className="truncate rounded border border-slate-800 px-2 py-1"
                  >
                    [{o.setup_type}] {o.coin} · 机会评分 {o.opportunity_score.toFixed(2)}
                  </li>
                ))}
              </ul>
            </section>
          </main>

          {/* 底部 timeline placeholder（Phase 3b 接入） */}
          <footer className="shrink-0 border-t border-slate-800 bg-slate-900/30 px-3 py-1 text-[10px] text-slate-600">
            事件流 · {snap.events.length} 条 · Phase 3b 接入横向 timeline
          </footer>
        </>
      )}
    </div>
  );
}
