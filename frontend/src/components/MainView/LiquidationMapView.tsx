"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatUSD, formatPrice } from "@/lib/format";
import { COLORS } from "@/lib/constants";
import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type { LiquidationMap, LiqLeverageGroup } from "@/lib/types";

export default function LiquidationMapView() {
  const coin = useMarketStore((s) => s.coin);
  const ticker = useMarketStore((s) => s.data[s.coin]?.ticker);
  const [liqData, setLiqData] = useState<LiquidationMap | null>(null);
  const [activeLeverage, setActiveLeverage] = useState<string>("all");

  useEffect(() => {
    const fetchLiq = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/liquidation/${coin}?cycle=24h`);
        if (res.ok) setLiqData(await res.json());
      } catch { /* handled by health status */ }
    };
    fetchLiq();
    const timer = setInterval(fetchLiq, 30000);
    return () => clearInterval(timer);
  }, [coin]);

  if (!liqData || !ticker) {
    return <div className="flex items-center justify-center h-64 text-slate-500">等待清算地图数据...</div>;
  }

  const currentPrice = ticker.last;
  const leverages = ["all", "10", "25", "50", "100"];

  const getVisibleBands = () => {
    if (activeLeverage === "all") {
      const shortBands: { price: number; usd: number; lev: string }[] = [];
      const longBands: { price: number; usd: number; lev: string }[] = [];
      for (const g of liqData.leverage_groups) {
        for (const b of g.short_bands) {
          shortBands.push({ price: (b.price_from + b.price_to) / 2, usd: b.turnover_usd, lev: g.leverage });
        }
        for (const b of g.long_bands) {
          longBands.push({ price: (b.price_from + b.price_to) / 2, usd: b.turnover_usd, lev: g.leverage });
        }
      }
      return { shortBands, longBands };
    }
    const group = liqData.leverage_groups.find((g) => g.leverage === activeLeverage);
    if (!group) return { shortBands: [], longBands: [] };
    return {
      shortBands: group.short_bands.map((b) => ({ price: (b.price_from + b.price_to) / 2, usd: b.turnover_usd, lev: group.leverage })),
      longBands: group.long_bands.map((b) => ({ price: (b.price_from + b.price_to) / 2, usd: b.turnover_usd, lev: group.leverage })),
    };
  };

  const { shortBands, longBands } = getVisibleBands();
  const allBands = [...shortBands, ...longBands];
  const maxUsd = Math.max(...allBands.map((b) => b.usd), 1);
  const allPrices = allBands.map((b) => b.price);
  const priceMin = Math.min(...allPrices, currentPrice * 0.95);
  const priceMax = Math.max(...allPrices, currentPrice * 1.05);
  const priceRange = priceMax - priceMin || 1;

  const mapHeight = 400;
  const toY = (price: number) => ((priceMax - price) / priceRange) * mapHeight;

  return (
    <div>
      <div className="flex items-center gap-2 mb-4">
        <span className="text-sm text-slate-400">杠杆筛选:</span>
        {leverages.map((l) => (
          <button
            key={l}
            onClick={() => setActiveLeverage(l)}
            className={`px-2 py-0.5 text-xs rounded ${
              activeLeverage === l
                ? "bg-blue-600 text-white"
                : "bg-slate-700 text-slate-400 hover:text-white"
            }`}
          >
            {l === "all" ? "全部" : `${l}x`}
          </button>
        ))}
        <span className="ml-auto text-xs text-slate-500">
          失衡比: {liqData.imbalance_ratio.toFixed(2)}
          {liqData.imbalance_ratio > 1.2 ? " (偏向上扫)" : liqData.imbalance_ratio < 0.8 ? " (偏向下扫)" : " (均衡)"}
        </span>
      </div>

      <div className="relative bg-slate-900 rounded-lg overflow-hidden" style={{ height: mapHeight }}>
        {/* Current price line */}
        <div
          className="absolute left-0 right-0 border-t border-dashed border-yellow-500/60 z-10"
          style={{ top: toY(currentPrice) }}
        >
          <span className="absolute right-2 -top-4 text-xs text-yellow-400 bg-slate-900/90 px-1 rounded">
            {formatPrice(currentPrice, coin)}
          </span>
        </div>

        {/* Short liquidation bands (above price - right side) */}
        {shortBands.map((b, i) => {
          const barWidth = (b.usd / maxUsd) * 45;
          return (
            <div
              key={`s-${i}`}
              className="absolute right-[50%] h-[3px] rounded-r"
              style={{
                top: toY(b.price),
                width: `${barWidth}%`,
                left: "50%",
                backgroundColor: `rgba(239, 68, 68, ${0.3 + (b.usd / maxUsd) * 0.7})`,
              }}
              title={`空头清算 ${formatPrice(b.price, coin)}: ${formatUSD(b.usd)} (${b.lev}x)`}
            />
          );
        })}

        {/* Long liquidation bands (below price - left side) */}
        {longBands.map((b, i) => {
          const barWidth = (b.usd / maxUsd) * 45;
          return (
            <div
              key={`l-${i}`}
              className="absolute h-[3px] rounded-l"
              style={{
                top: toY(b.price),
                width: `${barWidth}%`,
                right: "50%",
                backgroundColor: `rgba(34, 197, 94, ${0.3 + (b.usd / maxUsd) * 0.7})`,
              }}
              title={`多头清算 ${formatPrice(b.price, coin)}: ${formatUSD(b.usd)} (${b.lev}x)`}
            />
          );
        })}

        {/* Labels */}
        <div className="absolute bottom-2 left-4 text-[10px] text-green-400/70">◄ 多头清算</div>
        <div className="absolute bottom-2 right-4 text-[10px] text-red-400/70">空头清算 ►</div>
      </div>

      {/* Cluster summary */}
      <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="text-red-400 font-medium mb-1">上方空头清算密集区</div>
          {liqData.clusters_above.slice(0, 3).map((c, i) => (
            <div key={i} className="text-slate-400 flex justify-between">
              <span>{formatPrice(c.price_from, coin)}-{formatPrice(c.price_to, coin)}</span>
              <span className="text-red-400">{formatUSD(c.total_usd)} ({c.dominant_leverage}x)</span>
            </div>
          ))}
        </div>
        <div>
          <div className="text-green-400 font-medium mb-1">下方多头清算密集区</div>
          {liqData.clusters_below.slice(0, 3).map((c, i) => (
            <div key={i} className="text-slate-400 flex justify-between">
              <span>{formatPrice(c.price_from, coin)}-{formatPrice(c.price_to, coin)}</span>
              <span className="text-green-400">{formatUSD(c.total_usd)} ({c.dominant_leverage}x)</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
