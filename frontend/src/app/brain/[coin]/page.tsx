"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import TopStrip from "@/components/Brain/TopStrip";
import PriceAxisMap from "@/components/Brain/PriceAxisMap";
import ZoneDetailCard from "@/components/Brain/ZoneDetailCard";
import OpportunityBoard from "@/components/Brain/OpportunityBoard";
import EventTimeline from "@/components/Brain/EventTimeline";
import SpotOrderBookPanel from "@/components/Brain/SpotOrderBookPanel";
import FuturesHeatmap from "@/components/Brain/FuturesHeatmap";
import { ROLE_COLORS } from "@/components/Brain/types";

export default function TradingBrainPage() {
  const params = useParams();
  const coinParam = (params.coin as string)?.toUpperCase() ?? "BTC";

  const snap = useMarketStore((s) => s.tradingBrainByCoin[coinParam] ?? null);
  const loading = useMarketStore((s) => s.tradingBrainLoadingByCoin[coinParam] ?? false);
  const err = useMarketStore((s) => s.tradingBrainErrorByCoin[coinParam] ?? null);
  const loadTradingBrain = useMarketStore((s) => s.loadTradingBrain);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [hoverZoneId, setHoverZoneId] = useState<string | null>(null);

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
    <div className="brain-page flex min-h-screen flex-col bg-slate-950 text-slate-200">
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

          {/* 第一排：价格轴 / 详情卡 / 机会雷达
              响应式：
                ≥1280px (xl)：左 280 / 右 360；≥1536px (2xl)：左 320 / 右 400
                <1280px (lg)：左 260 / 右 320 */}
          <main className="flex min-h-[420px] flex-[3] overflow-hidden">
            <section
              className={`w-[260px] shrink-0 border-r border-slate-800 bg-slate-900/30 transition xl:w-[300px] 2xl:w-[320px] ${
                hoverZoneId ? "ring-1 ring-blue-500/30" : ""
              }`}
            >
              <PriceAxisMap
                zones={snap.zones}
                lastPrice={snap.last_price}
                atr={snap.atr}
                coin={snap.coin}
                selectedId={hoverZoneId ?? selectedId}
                onSelect={setSelectedId}
              />
            </section>

            <section className="flex-1 min-w-[300px] border-r border-slate-800">
              <ZoneDetailCard
                zone={selectedZone}
                coin={snap.coin}
                spotBook={snap.spot_book}
                futBook={snap.fut_book}
              />
            </section>

            <section className="w-[320px] shrink-0 bg-slate-900/30 xl:w-[360px] 2xl:w-[400px]">
              <OpportunityBoard
                opportunities={snap.opportunities}
                coin={snap.coin}
                onSelectZone={setSelectedId}
              />
            </section>
          </main>

          {/* 第三排：50/50 双卡（现货订单簿 / 合约堆积）
              <1024px 自动堆叠为上下两块以避免压扁 */}
          <section className="flex min-h-[360px] flex-[2] shrink-0 flex-col overflow-hidden border-t border-slate-800 bg-slate-900/30 lg:flex-row">
            <div className="flex-1 min-w-0 border-b border-slate-800 lg:border-b-0 lg:border-r">
              <SpotOrderBookPanel
                spotBook={snap.spot_book}
                coin={snap.coin}
                onSelectZone={setSelectedId}
              />
            </div>
            <div className="flex-1 min-w-0">
              <FuturesHeatmap
                futBook={snap.fut_book}
                coin={snap.coin}
                onSelectZone={setSelectedId}
              />
            </div>
          </section>

          <footer className="h-[100px] shrink-0 border-t border-slate-800 bg-slate-900/30 2xl:h-[120px]">
            <EventTimeline
              events={snap.events}
              hoverZoneId={hoverZoneId}
              onHoverZone={setHoverZoneId}
            />
          </footer>
        </>
      )}
    </div>
  );
}
