"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatCnUsd, formatPct, formatPrice } from "@/lib/format";
import { useEffect, useMemo, useState, type MouseEvent } from "react";
import { API_BASE } from "@/lib/constants";
import type { LiqBand, LiqCluster, LiquidationMap } from "@/lib/types";

/* ════════════════════════════════════════════════════════════════════
 * 清算地图视图（双视图组合 · 直显式信息密度）
 *
 * 重构动机：
 *   旧版用单一柱状图 + 完全依赖 hover 才能看到价位/金额，对小白极不友好。
 *   本版核心：信息直显，hover 仅做"加强"。
 *
 * 布局（自上而下）：
 *   ① TopControls       周期切换 + ⚙ 杠杆高级面板（默认折叠）
 *   ② PressureBalanceCard  一句话结论 + 双色多空压力条
 *   ③ Main 双栏：
 *        左 DensityChart  价格分布密度图（无文字、纯视觉）
 *        右 KeyLevelStack Top5+5 关键价位卡片栈（直接显示价/金额）
 *   ④ VacuumZones       清算真空区（瞬间穿越点）
 *   ⑤ HoverDetailCard   悬浮密度图时的详细弹层（保留旧体验作加强）
 *
 * 单位：金额一律用 formatCnUsd（"亿/万" 两档，雪球/东财风格），不用 M/K。
 * ══════════════════════════════════════════════════════════════════ */

type LiqBandRow = { price: number; usd: number; lev: string };

interface KeyLevel {
  side: "short" | "long";
  priceFrom: number;
  priceTo: number;
  priceCenter: number;
  totalUsd: number;
  dominantLev: string;
  distancePct: number;
  intensity: number; // 0-1，相对最大值
}

const CYCLES = ["1d", "7d", "30d"] as const;
const LEVERAGES = ["all", "10", "25", "50", "100"];
const TOP_N_PER_SIDE = 5;
const DENSITY_HEIGHT = 360;
const HIT_RADIUS_PX = 12;
const TOOLTIP_WIDTH_PX = 288;
const TOOLTIP_HALF_HEIGHT_EST = 190;

/* ──────────────────────────────────────────────
 * 工具函数
 * ────────────────────────────────────────────── */

/**
 * 把后端 imbalance_ratio 翻译成一句话方向结论。
 *
 * 与后端语义对齐：
 *   processors/liquidation.py:52
 *     imbalance = total_above / total_below
 *   即：上方空头清算簇总额 ÷ 下方多头清算簇总额
 *
 *   ratio > 1.25 → 上方空头压力 > 下方多头 → 价格上行更易触发「上扫」
 *   ratio < 0.8  → 下方多头压力 > 上方空头 → 价格下行更易触发「下扫」
 *   其余        → 均衡，方向中性
 */
function imbalanceVerdict(ratio: number): {
  text: string;
  side: "long" | "short" | "balanced";
  emoji: string;
} {
  if (ratio >= 1.25) {
    return {
      text: "上方空头清算压力更重，价格若继续上行，可能引发「上扫」集中清算",
      side: "short",
      emoji: "⬆",
    };
  }
  if (ratio <= 0.8) {
    return {
      text: "下方多头清算压力更重，价格若继续下行，可能引发「下扫」集中清算",
      side: "long",
      emoji: "⬇",
    };
  }
  return {
    text: "多空清算压力相近，方向中性，关注突破方向",
    side: "balanced",
    emoji: "≈",
  };
}

function buildKeyLevels(
  clusters: LiqCluster[],
  side: "short" | "long",
  maxUsd: number,
): KeyLevel[] {
  return clusters.slice(0, TOP_N_PER_SIDE).map((c) => ({
    side,
    priceFrom: c.price_from,
    priceTo: c.price_to,
    priceCenter: c.price_center || (c.price_from + c.price_to) / 2,
    totalUsd: c.total_usd,
    dominantLev: c.dominant_leverage || "all",
    distancePct: c.distance_pct,
    intensity: maxUsd > 0 ? c.total_usd / maxUsd : 0,
  }));
}

/* ──────────────────────────────────────────────
 * 子组件：TopControls
 * ────────────────────────────────────────────── */

function TopControls({
  cycle,
  setCycle,
  showAdvanced,
  setShowAdvanced,
  leverage,
  setLeverage,
}: {
  cycle: string;
  setCycle: (c: string) => void;
  showAdvanced: boolean;
  setShowAdvanced: (b: boolean) => void;
  leverage: string;
  setLeverage: (l: string) => void;
}) {
  return (
    <div className="mb-3 flex flex-wrap items-center gap-2">
      <span className="text-sm text-slate-400">周期:</span>
      {CYCLES.map((c) => (
        <button
          key={c}
          onClick={() => setCycle(c)}
          className={`rounded px-2 py-0.5 text-xs ${
            cycle === c
              ? "bg-blue-600 text-white"
              : "bg-slate-700 text-slate-400 hover:text-white"
          }`}
        >
          {c}
        </button>
      ))}

      <span className="ml-2 hidden h-4 border-l border-slate-700 sm:inline" />

      <button
        onClick={() => setShowAdvanced(!showAdvanced)}
        className={`ml-auto rounded px-2 py-0.5 text-xs ${
          showAdvanced
            ? "bg-slate-600 text-white"
            : "bg-slate-800 text-slate-400 hover:text-white"
        }`}
        title="切换杠杆筛选与详细控件"
      >
        ⚙ 高级
      </button>

      {showAdvanced && (
        <div className="basis-full mt-2 flex flex-wrap items-center gap-1.5 rounded border border-slate-700 bg-slate-800/40 px-2 py-1.5">
          <span className="text-xs text-slate-400">杠杆筛选:</span>
          {LEVERAGES.map((l) => (
            <button
              key={l}
              onClick={() => setLeverage(l)}
              className={`rounded px-2 py-0.5 text-[11px] ${
                leverage === l
                  ? "bg-blue-600 text-white"
                  : "bg-slate-700 text-slate-400 hover:text-white"
              }`}
            >
              {l === "all" ? "全部" : `${l}x`}
            </button>
          ))}
          <span className="text-[10px] text-slate-500">
            （仅当数据源按杠杆细分时生效；当前后端按 all 聚合，多为参考）
          </span>
        </div>
      )}
    </div>
  );
}

/* ──────────────────────────────────────────────
 * 子组件：PressureBalanceCard · 一句话结论 + 双色条
 * ────────────────────────────────────────────── */

function PressureBalanceCard({
  longTotal,
  shortTotal,
  ratio,
}: {
  longTotal: number;
  shortTotal: number;
  ratio: number;
}) {
  const verdict = imbalanceVerdict(ratio);
  const total = longTotal + shortTotal;
  const longPct = total > 0 ? (longTotal / total) * 100 : 50;
  const shortPct = 100 - longPct;

  const accent =
    verdict.side === "long"
      ? "text-emerald-400"
      : verdict.side === "short"
        ? "text-rose-400"
        : "text-amber-400";

  return (
    <div className="mb-3 rounded-lg border border-slate-700 bg-slate-800/40 px-3 py-2.5">
      <div className="mb-2 flex items-center gap-2 text-sm">
        <span className={`text-base ${accent}`}>{verdict.emoji}</span>
        <span className={`font-medium ${accent}`}>{verdict.text}</span>
      </div>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-slate-900">
        <div
          className="bg-emerald-500/80 transition-all"
          style={{ width: `${longPct}%` }}
          title={`下方多头清算 ${formatCnUsd(longTotal)}（${longPct.toFixed(0)}%）`}
        />
        <div
          className="bg-rose-500/80 transition-all"
          style={{ width: `${shortPct}%` }}
          title={`上方空头清算 ${formatCnUsd(shortTotal)}（${shortPct.toFixed(0)}%）`}
        />
      </div>
      <div className="mt-1 flex justify-between text-[11px] tabular-nums text-slate-400">
        <span>
          <span className="text-emerald-400">下方多头</span> {formatCnUsd(longTotal)}（
          {longPct.toFixed(0)}%）
        </span>
        <span>
          <span className="text-rose-400">上方空头</span> {formatCnUsd(shortTotal)}（
          {shortPct.toFixed(0)}%）
        </span>
      </div>
      <div className="mt-1 text-[10px] text-slate-500">
        失衡比 = 上方空头 / 下方多头 = {ratio.toFixed(2)}
      </div>
    </div>
  );
}

/* ──────────────────────────────────────────────
 * 子组件：DensityChart · 左侧密度图（无文字、纯视觉）
 * ────────────────────────────────────────────── */

function DensityChart({
  shortBands,
  longBands,
  currentPrice,
  coin,
  toY,
  maxUsd,
  onMove,
  onLeave,
}: {
  shortBands: LiqBandRow[];
  longBands: LiqBandRow[];
  currentPrice: number;
  coin: string;
  toY: (p: number) => number;
  maxUsd: number;
  onMove: (e: MouseEvent<HTMLDivElement>) => void;
  onLeave: () => void;
}) {
  return (
    <div className="relative">
      <div className="mb-1.5 flex justify-between text-[10px] text-slate-500">
        <span className="text-emerald-400">◄ 多头清算（下方）</span>
        <span>密度分布图</span>
        <span className="text-rose-400">空头清算（上方） ►</span>
      </div>
      <div
        className="relative cursor-default overflow-hidden rounded-lg border border-slate-700 bg-slate-900"
        style={{ height: DENSITY_HEIGHT }}
        onMouseMove={onMove}
        onMouseLeave={onLeave}
      >
        {/* 中线参考 */}
        <div className="pointer-events-none absolute bottom-0 left-1/2 top-0 w-px bg-slate-700/40" />

        {/* 当前价分隔条（32px 高彩色光带） */}
        <div
          className="pointer-events-none absolute left-0 right-0 z-10"
          style={{ top: toY(currentPrice) - 16, height: 32 }}
        >
          <div className="absolute inset-x-0 top-0 h-px bg-yellow-400/80" />
          <div className="absolute inset-x-0 bottom-0 h-px bg-yellow-400/80" />
          <div className="absolute inset-0 bg-gradient-to-b from-yellow-400/0 via-yellow-400/15 to-yellow-400/0" />
          <span className="absolute right-2 top-1/2 -translate-y-1/2 rounded bg-slate-900/95 px-1.5 py-0.5 text-[11px] font-medium text-yellow-400 shadow">
            当前价 {formatPrice(currentPrice, coin)}
          </span>
        </div>

        {/* 空头侧（上方）柱条 */}
        {shortBands.map((b, i) => {
          const w = (b.usd / maxUsd) * 45;
          return (
            <div
              key={`s-${i}`}
              className="pointer-events-none absolute h-[4px] rounded-r"
              style={{
                top: toY(b.price),
                width: `${w}%`,
                left: "50%",
                backgroundColor: `rgba(244, 63, 94, ${0.55 + (b.usd / maxUsd) * 0.4})`,
              }}
            />
          );
        })}

        {/* 多头侧（下方）柱条 */}
        {longBands.map((b, i) => {
          const w = (b.usd / maxUsd) * 45;
          return (
            <div
              key={`l-${i}`}
              className="pointer-events-none absolute h-[4px] rounded-l"
              style={{
                top: toY(b.price),
                width: `${w}%`,
                right: "50%",
                backgroundColor: `rgba(16, 185, 129, ${0.55 + (b.usd / maxUsd) * 0.4})`,
              }}
            />
          );
        })}
      </div>
      <p className="mt-1 px-0.5 text-[10px] text-slate-500">
        柱越长 = 该价位清算量越大；颜色越深 = 越接近本图最大值。鼠标悬停可查具体杠杆。
      </p>
    </div>
  );
}

/* ──────────────────────────────────────────────
 * 子组件：KeyLevelStack · Top5+5 卡片栈（直显示）
 * ────────────────────────────────────────────── */

function KeyLevelCard({
  level,
  rank,
  coin,
  isTop,
}: {
  level: KeyLevel;
  rank: number;
  coin: string;
  isTop: boolean;
}) {
  const isShort = level.side === "short";
  const colorBase = isShort ? "rose" : "emerald";
  const sign = isShort ? "上方" : "下方";
  const opp = isShort ? "空头" : "多头";

  return (
    <div
      className={`relative rounded-md border px-2.5 py-2 transition-colors ${
        isTop
          ? `border-${colorBase}-500/60 bg-${colorBase}-500/10`
          : "border-slate-700 bg-slate-800/40"
      }`}
      style={{
        borderColor: isTop
          ? isShort
            ? "rgba(244, 63, 94, 0.6)"
            : "rgba(16, 185, 129, 0.6)"
          : undefined,
        backgroundColor: isTop
          ? isShort
            ? "rgba(244, 63, 94, 0.10)"
            : "rgba(16, 185, 129, 0.10)"
          : undefined,
      }}
    >
      <div className="flex items-baseline justify-between gap-2">
        <div className="flex items-baseline gap-1.5">
          <span
            className="text-[10px] tabular-nums text-slate-500"
            title={`第 ${rank} 大 ${opp} 清算密集区`}
          >
            #{rank}
          </span>
          {isTop && <span className="text-xs">⚠</span>}
          <span
            className={`tabular-nums font-semibold ${
              isShort ? "text-rose-300" : "text-emerald-300"
            }`}
          >
            {formatPrice(level.priceFrom, coin)}
            {Math.abs(level.priceTo - level.priceFrom) > 1
              ? ` ~ ${formatPrice(level.priceTo, coin)}`
              : ""}
          </span>
        </div>
        <span
          className={`tabular-nums text-xs ${
            isShort ? "text-rose-400" : "text-emerald-400"
          }`}
        >
          {formatCnUsd(level.totalUsd)}
        </span>
      </div>

      {/* 强度进度条 */}
      <div className="mt-1.5 h-1 w-full overflow-hidden rounded-full bg-slate-900/60">
        <div
          className="h-full transition-all"
          style={{
            width: `${Math.max(2, level.intensity * 100)}%`,
            backgroundColor: isShort
              ? "rgba(244, 63, 94, 0.85)"
              : "rgba(16, 185, 129, 0.85)",
          }}
        />
      </div>

      <div className="mt-1.5 flex justify-between text-[10px] text-slate-500">
        <span>
          距当前 {sign}{" "}
          <span
            className={`tabular-nums ${
              isShort ? "text-rose-400/80" : "text-emerald-400/80"
            }`}
          >
            {formatPct(level.distancePct)}
          </span>
        </span>
        <span>主力杠杆 {level.dominantLev}x</span>
      </div>

      <p className="mt-1 text-[10px] leading-snug text-slate-500">
        {isShort
          ? "价格若上行触及，该带空头集中爆仓 → 形成「上扫」动能"
          : "价格若下行触及，该带多头集中爆仓 → 形成「下扫」动能"}
      </p>
    </div>
  );
}

function KeyLevelStack({
  shortLevels,
  longLevels,
  coin,
}: {
  shortLevels: KeyLevel[];
  longLevels: KeyLevel[];
  coin: string;
}) {
  return (
    <div className="flex flex-col gap-3">
      {/* 上方空头 */}
      <div>
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="font-semibold text-rose-400">▲ 上方空头清算密集区 Top {shortLevels.length}</span>
          <span className="text-[10px] text-slate-500">价位 · 金额 · 强度</span>
        </div>
        <div className="flex flex-col gap-1.5">
          {shortLevels.length === 0 && (
            <div className="rounded border border-dashed border-slate-700 bg-slate-800/30 px-2 py-1.5 text-[11px] text-slate-500">
              暂无显著上方空头清算簇
            </div>
          )}
          {shortLevels.map((lv, i) => (
            <KeyLevelCard
              key={`s-${i}`}
              level={lv}
              rank={i + 1}
              coin={coin}
              isTop={i === 0}
            />
          ))}
        </div>
      </div>

      {/* 下方多头 */}
      <div>
        <div className="mb-1.5 flex items-center justify-between text-xs">
          <span className="font-semibold text-emerald-400">▼ 下方多头清算密集区 Top {longLevels.length}</span>
          <span className="text-[10px] text-slate-500">价位 · 金额 · 强度</span>
        </div>
        <div className="flex flex-col gap-1.5">
          {longLevels.length === 0 && (
            <div className="rounded border border-dashed border-slate-700 bg-slate-800/30 px-2 py-1.5 text-[11px] text-slate-500">
              暂无显著下方多头清算簇
            </div>
          )}
          {longLevels.map((lv, i) => (
            <KeyLevelCard
              key={`l-${i}`}
              level={lv}
              rank={i + 1}
              coin={coin}
              isTop={i === 0}
            />
          ))}
        </div>
      </div>
    </div>
  );
}

/* ──────────────────────────────────────────────
 * 子组件：VacuumZones · 真空区
 * ────────────────────────────────────────────── */

function VacuumZones({
  zones,
  coin,
}: {
  zones: { price_from: number; price_to: number; midpoint: number; note: string }[];
  coin: string;
}) {
  if (!zones || zones.length === 0) return null;
  return (
    <div className="mt-3 rounded-md border border-slate-700 bg-slate-800/30 px-2.5 py-2">
      <div className="mb-1 text-xs font-medium text-amber-400">
        ⚡ 清算真空区（瞬间穿越点）
      </div>
      <div className="flex flex-col gap-0.5 text-[11px]">
        {zones.slice(0, 4).map((z, i) => (
          <div key={i} className="flex justify-between text-slate-400">
            <span className="tabular-nums">
              {formatPrice(z.price_from, coin)} ~ {formatPrice(z.price_to, coin)}
            </span>
            <span className="text-amber-400/80">
              中点 {formatPrice(z.midpoint || (z.price_from + z.price_to) / 2, coin)}
              {z.note && ` · ${z.note}`}
            </span>
          </div>
        ))}
      </div>
      <p className="mt-1 text-[10px] text-slate-500">
        真空区内清算挂单稀薄，价格易快速穿越无支撑/阻力。
      </p>
    </div>
  );
}

/* ──────────────────────────────────────────────
 * 主组件
 * ────────────────────────────────────────────── */

export default function LiquidationMapView() {
  const coin = useMarketStore((s) => s.coin);
  const ticker = useMarketStore((s) => s.data[s.coin]?.ticker);
  const [liqData, setLiqData] = useState<LiquidationMap | null>(null);
  const [activeCycle, setActiveCycle] = useState<string>("1d");
  const [activeLeverage, setActiveLeverage] = useState<string>("all");
  const [showAdvanced, setShowAdvanced] = useState<boolean>(false);
  const [hoverTip, setHoverTip] = useState<{
    left: number;
    anchorTop: number;
    hits: { side: "short" | "long"; band: LiqBandRow }[];
    overflow: number;
  } | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchLiq = async () => {
      try {
        const res = await fetch(
          `${API_BASE}/api/liquidation/${coin}?cycle=${activeCycle}`,
        );
        if (res.ok && !cancelled) setLiqData(await res.json());
      } catch {
        /* 由健康状态条统一展示 */
      }
    };
    fetchLiq();
    const timer = setInterval(fetchLiq, 30000);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [coin, activeCycle]);

  useEffect(() => {
    queueMicrotask(() => setHoverTip(null));
  }, [coin, activeCycle]);

  /* ── 数据派生（hooks 必须无条件调用，故放在 return 之前）── */
  const currentPrice = ticker?.last ?? 0;

  const { shortBands, longBands } = useMemo(() => {
    if (!liqData) return { shortBands: [], longBands: [] };
    const s: LiqBandRow[] = [];
    const l: LiqBandRow[] = [];
    const groups = liqData.leverage_groups;
    const filtered =
      activeLeverage === "all"
        ? groups
        : groups.filter((g) => g.leverage === activeLeverage);
    for (const g of filtered) {
      for (const b of g.short_bands as LiqBand[]) {
        s.push({
          price: (b.price_from + b.price_to) / 2,
          usd: b.turnover_usd,
          lev: g.leverage,
        });
      }
      for (const b of g.long_bands as LiqBand[]) {
        l.push({
          price: (b.price_from + b.price_to) / 2,
          usd: b.turnover_usd,
          lev: g.leverage,
        });
      }
    }
    return { shortBands: s, longBands: l };
  }, [liqData, activeLeverage]);

  const allBands = useMemo(() => [...shortBands, ...longBands], [shortBands, longBands]);
  const maxUsd = Math.max(...allBands.map((b) => b.usd), 1);
  const priceMin = useMemo(() => {
    if (allBands.length === 0) return currentPrice * 0.95;
    return Math.min(...allBands.map((b) => b.price), currentPrice * 0.95);
  }, [allBands, currentPrice]);
  const priceMax = useMemo(() => {
    if (allBands.length === 0) return currentPrice * 1.05;
    return Math.max(...allBands.map((b) => b.price), currentPrice * 1.05);
  }, [allBands, currentPrice]);
  const priceRange = priceMax - priceMin || 1;
  const toY = (p: number) => ((priceMax - p) / priceRange) * DENSITY_HEIGHT;

  const longTotal = useMemo(() => longBands.reduce((s, b) => s + b.usd, 0), [longBands]);
  const shortTotal = useMemo(
    () => shortBands.reduce((s, b) => s + b.usd, 0),
    [shortBands],
  );
  // 直接采用后端权威 imbalance_ratio（= 上方空头 / 下方多头），避免前端按 bands
  // 重算与后端按 clusters 计算的口径出现微小差异。
  const ratio = liqData?.imbalance_ratio ?? 1;

  const clusterMaxUsd = useMemo(() => {
    if (!liqData) return 0;
    return Math.max(
      0,
      ...liqData.clusters_above.map((c) => c.total_usd),
      ...liqData.clusters_below.map((c) => c.total_usd),
    );
  }, [liqData]);

  const shortLevels = useMemo(
    () => (liqData ? buildKeyLevels(liqData.clusters_above, "short", clusterMaxUsd) : []),
    [liqData, clusterMaxUsd],
  );
  const longLevels = useMemo(
    () => (liqData ? buildKeyLevels(liqData.clusters_below, "long", clusterMaxUsd) : []),
    [liqData, clusterMaxUsd],
  );

  /* ── 渲染分支 ── */
  if (!liqData || !ticker) {
    return (
      <div className="flex h-64 items-center justify-center text-slate-500">
        等待清算地图数据...
      </div>
    );
  }

  /* ── hover 命中 ── */
  const findHits = (y: number) => {
    const hits: { side: "short" | "long"; band: LiqBandRow; dist: number }[] = [];
    for (const b of shortBands) {
      const cy = toY(b.price) + 2;
      const d = Math.abs(y - cy);
      if (d <= HIT_RADIUS_PX) hits.push({ side: "short", band: b, dist: d });
    }
    for (const b of longBands) {
      const cy = toY(b.price) + 2;
      const d = Math.abs(y - cy);
      if (d <= HIT_RADIUS_PX) hits.push({ side: "long", band: b, dist: d });
    }
    hits.sort((a, b) => a.dist - b.dist);
    const seen = new Set<string>();
    const out: typeof hits = [];
    for (const h of hits) {
      const k = `${h.side}-${h.band.price}-${h.band.lev}`;
      if (seen.has(k)) continue;
      seen.add(k);
      out.push(h);
    }
    return out;
  };

  const handleMove = (e: MouseEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const y = e.clientY - rect.top;
    const raw = findHits(y);
    if (raw.length === 0) {
      setHoverTip(null);
      return;
    }
    const hits = raw.slice(0, 12).map(({ side, band }) => ({ side, band }));
    const overflow = raw.length - hits.length;
    const primary = raw[0];
    const w = rect.width;
    const x0 = rect.left;
    const barFrac = (primary.band.usd / maxUsd) * 0.45;
    let left =
      primary.side === "short"
        ? x0 + w * (0.5 + barFrac) + 8
        : x0 + w * (0.5 - barFrac) - TOOLTIP_WIDTH_PX - 8;
    if (primary.side === "short" && left + TOOLTIP_WIDTH_PX > window.innerWidth - 8) {
      left = x0 + w * (0.5 + barFrac) - TOOLTIP_WIDTH_PX - 8;
    }
    if (primary.side === "long" && left < 8) left = x0 + w * (0.5 - barFrac) + 8;
    left = Math.max(8, Math.min(left, window.innerWidth - TOOLTIP_WIDTH_PX - 8));
    const cy = rect.top + toY(primary.band.price) + 2;
    const anchorTop = Math.max(
      8 + TOOLTIP_HALF_HEIGHT_EST,
      Math.min(cy, window.innerHeight - 8 - TOOLTIP_HALF_HEIGHT_EST),
    );
    setHoverTip({ left, anchorTop, hits, overflow });
  };

  return (
    <div>
      <TopControls
        cycle={activeCycle}
        setCycle={(c) => {
          setActiveCycle(c);
          setHoverTip(null);
        }}
        showAdvanced={showAdvanced}
        setShowAdvanced={setShowAdvanced}
        leverage={activeLeverage}
        setLeverage={setActiveLeverage}
      />

      <PressureBalanceCard
        longTotal={longTotal}
        shortTotal={shortTotal}
        ratio={ratio}
      />

      <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
        <DensityChart
          shortBands={shortBands}
          longBands={longBands}
          currentPrice={currentPrice}
          coin={coin}
          toY={toY}
          maxUsd={maxUsd}
          onMove={handleMove}
          onLeave={() => setHoverTip(null)}
        />
        <KeyLevelStack
          shortLevels={shortLevels}
          longLevels={longLevels}
          coin={coin}
        />
      </div>

      <VacuumZones zones={liqData.vacuum_zones} coin={coin} />

      {/* hover 详细弹层（密度图加强） */}
      {hoverTip && (
        <div
          className="pointer-events-none fixed z-[100] max-h-[min(420px,70vh)] w-72 overflow-y-auto rounded-lg border border-slate-600 bg-slate-900/95 px-3 py-2.5 text-xs shadow-xl backdrop-blur-sm"
          style={{
            left: hoverTip.left,
            top: hoverTip.anchorTop,
            transform: "translateY(-50%)",
            width: TOOLTIP_WIDTH_PX,
          }}
          role="tooltip"
        >
          {hoverTip.hits.length > 1 && (
            <p className="mb-2 border-b border-slate-700 pb-2 leading-snug text-slate-400">
              该高度附近共有{" "}
              <span className="font-medium text-white">
                {hoverTip.hits.length + hoverTip.overflow}
              </span>{" "}
              条清算柱（多为不同杠杆叠加）：
            </p>
          )}
          <ul className="space-y-2">
            {hoverTip.hits.map((h, i) => (
              <li
                key={`${h.side}-${h.band.price}-${h.band.lev}-${i}`}
                className="rounded border border-slate-700/80 bg-slate-800/50 p-2"
              >
                <div
                  className={`font-semibold ${
                    h.side === "short" ? "text-rose-400" : "text-emerald-400"
                  }`}
                >
                  {h.side === "short" ? "上方空头清算" : "下方多头清算"} ·{" "}
                  {h.band.lev === "all" ? "聚合" : `${h.band.lev}x`}
                </div>
                <div className="mt-1 text-sm font-medium tabular-nums text-white">
                  价位 {formatPrice(h.band.price, coin)}
                </div>
                <div className="mt-0.5 tabular-nums text-slate-300">
                  规模 {formatCnUsd(h.band.usd)}
                </div>
              </li>
            ))}
          </ul>
          {hoverTip.overflow > 0 && (
            <p className="mt-2 text-[10px] text-amber-400/90">
              另有 {hoverTip.overflow} 条已折叠，可在 ⚙ 高级面板缩小杠杆筛选范围后查看。
            </p>
          )}
        </div>
      )}
    </div>
  );
}
