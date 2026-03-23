"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatUSD, formatPrice } from "@/lib/format";
import { useEffect, useState, type MouseEvent } from "react";
import { API_BASE } from "@/lib/constants";
import type { LiquidationMap } from "@/lib/types";

/** 单条柱高度 3px，取中线用于命中 */
const LIQ_BAR_CENTER_OFFSET = 1.5;
/** 纵向命中半径 */
const LIQ_HIT_RADIUS_PX = 10;
const TOOLTIP_WIDTH_PX = 288;
/** 用于视口内纵向夹紧（translateY(-50%) 近似半高） */
const TOOLTIP_HALF_HEIGHT_EST = 190;

type LiqBandRow = { price: number; usd: number; lev: string };

function findBandsAtChartY(
  y: number,
  shortBands: LiqBandRow[],
  longBands: LiqBandRow[],
  toY: (price: number) => number
): { side: "short" | "long"; band: LiqBandRow; dist: number }[] {
  const hits: { side: "short" | "long"; band: LiqBandRow; dist: number }[] = [];
  for (const b of shortBands) {
    const cy = toY(b.price) + LIQ_BAR_CENTER_OFFSET;
    const d = Math.abs(y - cy);
    if (d <= LIQ_HIT_RADIUS_PX) hits.push({ side: "short", band: b, dist: d });
  }
  for (const b of longBands) {
    const cy = toY(b.price) + LIQ_BAR_CENTER_OFFSET;
    const d = Math.abs(y - cy);
    if (d <= LIQ_HIT_RADIUS_PX) hits.push({ side: "long", band: b, dist: d });
  }
  hits.sort((a, b) => a.dist - b.dist);
  const seen = new Set<string>();
  const out: typeof hits = [];
  for (const h of hits) {
    const key = `${h.side}-${h.band.price}-${h.band.lev}-${h.band.usd}`;
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(h);
  }
  return out;
}

/**
 * 锚在柱旁：与渲染逻辑一致（柱宽 = usd/maxUsd * 45% 容器宽），
 * 空头柱向右延伸 → 提示框贴在柱末端外侧；多头柱向左延伸 → 贴在柱起点外侧。
 */
function computeAnchoredTooltipPosition(
  chartRect: DOMRect,
  primary: { side: "short" | "long"; band: LiqBandRow },
  maxUsd: number,
  toY: (price: number) => number
): { left: number; anchorTop: number } {
  const w = chartRect.width;
  const x0 = chartRect.left;
  const barFrac = (primary.band.usd / maxUsd) * 0.45;
  let left: number;

  if (primary.side === "short") {
    const barEndX = x0 + w * (0.5 + barFrac);
    left = barEndX + 8;
    if (left + TOOLTIP_WIDTH_PX > window.innerWidth - 8) {
      left = barEndX - TOOLTIP_WIDTH_PX - 8;
    }
  } else {
    const barStartX = x0 + w * (0.5 - barFrac);
    left = barStartX - TOOLTIP_WIDTH_PX - 8;
    if (left < 8) {
      left = barStartX + 8;
    }
  }

  left = Math.max(8, Math.min(left, window.innerWidth - TOOLTIP_WIDTH_PX - 8));

  const cy =
    chartRect.top + toY(primary.band.price) + LIQ_BAR_CENTER_OFFSET;
  const anchorTop = Math.max(
    8 + TOOLTIP_HALF_HEIGHT_EST,
    Math.min(cy, window.innerHeight - 8 - TOOLTIP_HALF_HEIGHT_EST)
  );

  return { left, anchorTop };
}

export default function LiquidationMapView() {
  const coin = useMarketStore((s) => s.coin);
  const ticker = useMarketStore((s) => s.data[s.coin]?.ticker);
  const [liqData, setLiqData] = useState<LiquidationMap | null>(null);
  const [activeLeverage, setActiveLeverage] = useState<string>("all");
  const [activeCycle, setActiveCycle] = useState<string>("24h");
  const [liqTooltip, setLiqTooltip] = useState<{
    left: number;
    anchorTop: number;
    hits: { side: "short" | "long"; band: LiqBandRow }[];
    overflow: number;
  } | null>(null);

  useEffect(() => {
    const fetchLiq = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/liquidation/${coin}?cycle=${activeCycle}`);
        if (res.ok) setLiqData(await res.json());
      } catch {
        /* handled by health status */
      }
    };
    fetchLiq();
    const timer = setInterval(fetchLiq, 30000);
    return () => clearInterval(timer);
  }, [coin, activeCycle]);

  useEffect(() => {
    queueMicrotask(() => setLiqTooltip(null));
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
      shortBands: group.short_bands.map((b) => ({
        price: (b.price_from + b.price_to) / 2,
        usd: b.turnover_usd,
        lev: group.leverage,
      })),
      longBands: group.long_bands.map((b) => ({
        price: (b.price_from + b.price_to) / 2,
        usd: b.turnover_usd,
        lev: group.leverage,
      })),
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

  const handleMapPointerMove = (e: MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const raw = findBandsAtChartY(y, shortBands, longBands, toY);
    if (raw.length === 0) {
      setLiqTooltip(null);
      return;
    }
    const maxRows = 12;
    const hits = raw.slice(0, maxRows).map(({ side, band }) => ({ side, band }));
    const overflow = raw.length - hits.length;
    const primary = raw[0];
    const { left, anchorTop } = computeAnchoredTooltipPosition(rect, primary, maxUsd, toY);
    setLiqTooltip({ left, anchorTop, hits, overflow });
  };

  const handleMapPointerLeave = () => setLiqTooltip(null);

  const clearTooltip = () => setLiqTooltip(null);

  return (
    <div>
      <div className="flex items-center gap-2 mb-4">
        <span className="text-sm text-slate-400">周期:</span>
        {["24h", "7d"].map((c) => (
          <button
            key={c}
            onClick={() => {
              setActiveCycle(c);
              clearTooltip();
            }}
            className={`px-2 py-0.5 text-xs rounded ${
              activeCycle === c ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-400 hover:text-white"
            }`}
          >
            {c}
          </button>
        ))}
        <span className="mx-2 border-l border-slate-700 h-4" />
        <span className="text-sm text-slate-400">杠杆筛选:</span>
        {leverages.map((l) => (
          <button
            key={l}
            onClick={() => {
              setActiveLeverage(l);
              clearTooltip();
            }}
            className={`px-2 py-0.5 text-xs rounded ${
              activeLeverage === l ? "bg-blue-600 text-white" : "bg-slate-700 text-slate-400 hover:text-white"
            }`}
          >
            {l === "all" ? "全部" : `${l}x`}
          </button>
        ))}
        <span className="ml-auto text-xs text-slate-500">
          失衡比: {liqData.imbalance_ratio.toFixed(2)}
          {liqData.imbalance_ratio > 1.2
            ? " (偏向上扫)"
            : liqData.imbalance_ratio < 0.8
              ? " (偏向下扫)"
              : " (均衡)"}
        </span>
      </div>

      <div
        className="relative cursor-default overflow-hidden rounded-lg bg-slate-900"
        style={{ height: mapHeight }}
        onMouseMove={handleMapPointerMove}
        onMouseLeave={handleMapPointerLeave}
      >
        <div
          className="pointer-events-none absolute left-0 right-0 z-10 border-t border-dashed border-yellow-500/60"
          style={{ top: toY(currentPrice) }}
        >
          <span className="absolute -top-4 right-2 rounded bg-slate-900/90 px-1 text-xs text-yellow-400">
            {formatPrice(currentPrice, coin)}
          </span>
        </div>

        {shortBands.map((b, i) => {
          const barWidth = (b.usd / maxUsd) * 45;
          return (
            <div
              key={`s-${i}`}
              className="pointer-events-none absolute right-[50%] h-[3px] rounded-r"
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

        {longBands.map((b, i) => {
          const barWidth = (b.usd / maxUsd) * 45;
          return (
            <div
              key={`l-${i}`}
              className="pointer-events-none absolute h-[3px] rounded-l"
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

        <div className="pointer-events-none absolute bottom-2 left-4 text-[10px] text-green-400/70">◄ 多头清算</div>
        <div className="pointer-events-none absolute bottom-2 right-4 text-[10px] text-red-400/70">空头清算 ►</div>
      </div>

      <p className="mt-1.5 px-0.5 text-[10px] text-slate-500">
        提示：在图表区域沿彩色柱上下移动鼠标，提示框会锚定在该柱旁，显示清算参考价与规模（红=空头侧，绿=多头侧）。
      </p>

      {liqTooltip && (
        <div
          className="fixed z-[100] w-72 max-h-[min(420px,70vh)] overflow-y-auto rounded-lg border border-slate-600 bg-slate-900/95 px-3 py-2.5 text-xs shadow-xl backdrop-blur-sm pointer-events-none"
          style={{
            left: liqTooltip.left,
            top: liqTooltip.anchorTop,
            transform: "translateY(-50%)",
            width: TOOLTIP_WIDTH_PX,
          }}
          role="tooltip"
        >
          {liqTooltip.hits.length > 1 && (
            <p className="mb-2 border-b border-slate-700 pb-2 leading-snug text-slate-400">
              该高度附近共有{" "}
              <span className="font-medium text-white">{liqTooltip.hits.length + liqTooltip.overflow}</span>{" "}
              条清算柱（多为不同杠杆叠加），请逐条对照：
            </p>
          )}
          <ul className="space-y-2">
            {liqTooltip.hits.map((h, i) => (
              <li
                key={`${h.side}-${h.band.price}-${h.band.lev}-${i}`}
                className="rounded border border-slate-700/80 bg-slate-800/50 p-2"
              >
                <div className={`font-semibold ${h.side === "short" ? "text-red-400" : "text-green-400"}`}>
                  {h.side === "short" ? "空头清算" : "多头清算"} · {h.band.lev}x
                </div>
                <div className="mt-1 text-sm font-medium tabular-nums text-white">
                  清算参考价：{formatPrice(h.band.price, coin)}
                </div>
                <div className="mt-0.5 tabular-nums text-slate-300">
                  清算规模（估算）：{formatUSD(h.band.usd)}
                </div>
                <p className="mt-1.5 text-[10px] leading-snug text-slate-500">
                  {h.side === "short"
                    ? "读图提示：价格若上行接近此区域，上方空头仓位可能更易被集中清算（俗称「上扫」）。"
                    : "读图提示：价格若下行接近此区域，下方多头仓位可能更易被集中清算（俗称「下扫」）。"}
                </p>
              </li>
            ))}
          </ul>
          {liqTooltip.overflow > 0 && (
            <p className="mt-2 text-[10px] text-amber-400/90">
              另有 {liqTooltip.overflow} 条已折叠，可缩小杠杆筛选范围后查看。
            </p>
          )}
          <p className="mt-2 border-t border-slate-700 pt-2 text-[10px] leading-snug text-slate-500">
            「参考价」取价格区间中点，仅辅助读图；实盘以交易所与订单为准。
          </p>
        </div>
      )}

      <div className="mt-3 grid grid-cols-2 gap-3 text-xs">
        <div>
          <div className="mb-1 font-medium text-red-400">上方空头清算密集区</div>
          {liqData.clusters_above.slice(0, 3).map((c, i) => (
            <div key={i} className="flex justify-between text-slate-400">
              <span>
                {formatPrice(c.price_from, coin)}-{formatPrice(c.price_to, coin)}
              </span>
              <span className="text-red-400">
                {formatUSD(c.total_usd)} ({c.dominant_leverage}x)
              </span>
            </div>
          ))}
        </div>
        <div>
          <div className="mb-1 font-medium text-green-400">下方多头清算密集区</div>
          {liqData.clusters_below.slice(0, 3).map((c, i) => (
            <div key={i} className="flex justify-between text-slate-400">
              <span>
                {formatPrice(c.price_from, coin)}-{formatPrice(c.price_to, coin)}
              </span>
              <span className="text-green-400">
                {formatUSD(c.total_usd)} ({c.dominant_leverage}x)
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
