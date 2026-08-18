"use client";

import { useEffect, useMemo, useState } from "react";

import { API_BASE } from "@/lib/constants";

/**
 * 流动性总览：把全市场清算簇、Hyperliquid 巨鲸清算桶、巨鲸 Top 头寸清算价、
 * 关键位/磁力位叠加到同一根价格轴上，并用规则生成一句话结论。
 * 纯前端聚合：三个数据源相互独立降级，任一失败只隐藏对应图层。
 */

type RangePct = 3 | 5 | 10;
type Cycle = "1d" | "7d";

type LiqCluster = {
  price_center: number;
  price_from: number;
  price_to: number;
  total_usd: number;
  side: "long" | "short";
  distance_pct: number;
};

type VacuumZone = { price_from: number; price_to: number; midpoint: number; note: string };

type LiquidationMap = {
  coin: string;
  ts: number;
  clusters_above: LiqCluster[];
  clusters_below: LiqCluster[];
  vacuum_zones: VacuumZone[];
  imbalance_ratio: number;
};

type WhaleBucket = {
  price_mid: number;
  price_from: number;
  price_to: number;
  distance_from_mark_pct: number;
  long_notional_usd: number;
  short_notional_usd: number;
  long_count: number;
  short_count: number;
};

type WhaleTopPosition = {
  address: string;
  side: "long" | "short";
  notional_usd: number;
  liq_price?: number | null;
  leverage: number;
  distance_to_liq_pct?: number | null;
};

type WhaleAsset = {
  mark_price?: number | null;
  liquidation_buckets: WhaleBucket[];
  top_longs?: WhaleTopPosition[];
  top_shorts?: WhaleTopPosition[];
  quality: { valid: boolean; status: string };
};

type WhaleDistributions = { assets: Record<string, WhaleAsset> };

type KeyLevelLine = { price: number; kind: "support" | "resistance" | "magnet"; label: string };

type KeyLevelSnapshot = {
  ts: number;
  current_price: number;
  nearest_strong_support?: { price: number } | number | null;
  nearest_strong_resistance?: { price: number } | number | null;
  magnet_levels?: { price: number; note?: string }[];
};

const RANGE_OPTIONS: RangePct[] = [3, 5, 10];

export function LiquidityOverview() {
  const [liqMap, setLiqMap] = useState<LiquidationMap | null>(null);
  const [whale, setWhale] = useState<WhaleAsset | null>(null);
  const [keyLevels, setKeyLevels] = useState<KeyLevelSnapshot | null>(null);
  const [rangePct, setRangePct] = useState<RangePct>(5);
  const [cycle, setCycle] = useState<Cycle>("1d");
  const [layerErrors, setLayerErrors] = useState<string[]>([]);

  useEffect(() => {
    let disposed = false;
    const load = async () => {
      const errors: string[] = [];
      const [mapResult, whaleResult, klResult] = await Promise.allSettled([
        fetch(`${API_BASE}/api/liquidation/BTC?cycle=${cycle}`, { cache: "no-store" })
          .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))),
        fetch(`${API_BASE}/api/trend/hyperliquid-whale-distributions`, { cache: "no-store" })
          .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))),
        fetch(`${API_BASE}/api/key-levels/history/BTC?limit=1`, { cache: "no-store" })
          .then((response) => (response.ok ? response.json() : Promise.reject(new Error(`HTTP ${response.status}`)))),
      ]);
      if (disposed) return;
      if (mapResult.status === "fulfilled") setLiqMap(mapResult.value as LiquidationMap);
      else errors.push("全市场清算地图暂不可用");
      if (whaleResult.status === "fulfilled") {
        const payload = whaleResult.value as WhaleDistributions;
        setWhale(payload.assets?.BTC ?? null);
      } else errors.push("巨鲸清算分布暂不可用");
      if (klResult.status === "fulfilled") {
        const snapshots = (klResult.value as { snapshots?: KeyLevelSnapshot[] }).snapshots;
        setKeyLevels(snapshots?.[0] ?? null);
      } else errors.push("关键位数据暂不可用");
      setLayerErrors(errors);
    };
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, [cycle]);

  const markPrice = whale?.mark_price ?? keyLevels?.current_price ?? null;
  const derived = useMemo(
    () => (markPrice == null || markPrice <= 0
      ? null
      : deriveOverview(markPrice, rangePct, liqMap, whale, keyLevels)),
    [markPrice, rangePct, liqMap, whale, keyLevels],
  );

  return (
    <section className="rounded-xl border border-cyan-900/60 bg-gradient-to-br from-slate-900/75 to-cyan-950/15 p-4 md:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">流动性总览 · 最容易被清算的多空价格</h2>
          <p className="mt-0.5 text-[11px] text-slate-500">全市场清算簇 + Hyperliquid 巨鲸清算分布 + Top 头寸清算价 + 关键位，叠加在同一根价格轴上 · 仅观察，固定零权重</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1">
            {(["1d", "7d"] as const).map((value) => (
              <button key={value} type="button" onClick={() => setCycle(value)} className={`rounded px-2.5 py-1 text-[10px] ${cycle === value ? "bg-slate-700 text-white" : "text-slate-500 hover:text-slate-300"}`}>
                清算{value === "1d" ? "1天" : "7天"}周期
              </button>
            ))}
          </div>
          <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1">
            {RANGE_OPTIONS.map((value) => (
              <button key={value} type="button" onClick={() => setRangePct(value)} className={`rounded px-2.5 py-1 text-[10px] ${rangePct === value ? "bg-cyan-700 text-white" : "text-slate-500 hover:text-slate-300"}`}>
                ±{value}%
              </button>
            ))}
          </div>
        </div>
      </div>

      {layerErrors.length > 0 && (
        <div className="mt-3 rounded border border-amber-800/60 bg-amber-950/25 px-3 py-2 text-[11px] text-amber-300">
          部分图层降级：{layerErrors.join("、")}。其余图层不受影响。
        </div>
      )}

      {!derived ? (
        <div className="mt-4 flex h-64 items-center justify-center rounded-lg border border-slate-800 text-xs text-slate-600">正在汇聚清算与关键位数据…</div>
      ) : (
        <>
          <div className="mt-4 rounded-xl border border-cyan-800/50 bg-cyan-950/15 px-4 py-3">
            <div className="text-[10px] text-cyan-400/80">一句话看懂当前流动性格局</div>
            <p className="mt-1 text-sm leading-6 text-slate-200">{derived.headline}</p>
          </div>

          <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-4">
            <OverviewMetric label={`上方 ${rangePct}% 内空头清算燃料`} value={compactUsd(derived.fuelAboveUsd)} sub={derived.thickestAbove ? `最厚 ${money(derived.thickestAbove.price)}（${signedPct(derived.thickestAbove.distancePct)}）` : "范围内无密集区"} tone="text-amber-400" />
            <OverviewMetric label={`下方 ${rangePct}% 内多头清算燃料`} value={compactUsd(derived.fuelBelowUsd)} sub={derived.thickestBelow ? `最厚 ${money(derived.thickestBelow.price)}（${signedPct(derived.thickestBelow.distancePct)}）` : "范围内无密集区"} tone="text-rose-400" />
            <OverviewMetric label="最近的高危巨鲸头寸" value={derived.riskiestWhale ? `${derived.riskiestWhale.side === "long" ? "多头" : "空头"} ${money(derived.riskiestWhale.liqPrice)}` : "—"} sub={derived.riskiestWhale ? `距现价 ${signedPct(derived.riskiestWhale.distancePct)} · ${compactUsd(derived.riskiestWhale.notionalUsd)} · ${derived.riskiestWhale.leverage.toFixed(0)}x` : "暂无带清算价的 Top 头寸"} tone="text-cyan-300" />
            <OverviewMetric label="关键位参照" value={derived.nearestResistance != null && derived.nearestSupport != null ? `${money(derived.nearestSupport)} / ${money(derived.nearestResistance)}` : "—"} sub="最近强支撑 / 最近强阻力" tone="text-slate-200" />
          </div>

          <CompositeAxis
            markPrice={markPrice as number}
            rangePct={rangePct}
            clusters={derived.clustersInRange}
            whaleBuckets={derived.whaleBucketsInRange}
            pins={derived.pins}
            levelLines={derived.levelLines}
            vacuums={derived.vacuumsInRange}
          />

          <p className="mt-3 text-[10px] leading-5 text-slate-500">
            左侧＝全市场清算簇（CoinGlass 多所聚合，估算杠杆分布），右侧＝Hyperliquid 巨鲸清算桶；两侧金额尺度独立，只比较各自形状与相对厚度。◆钉点＝巨鲸 Top 头寸清算价；横线＝关键位。清算燃料表示价格到达该区域时可能被强制平仓的名义金额，不是必然发生的成交，交叉保证金清算价会随账户权益变化。
          </p>
        </>
      )}
    </section>
  );
}

// ---------- 派生计算 ----------

type OverviewDerived = {
  headline: string;
  fuelAboveUsd: number;
  fuelBelowUsd: number;
  thickestAbove: { price: number; distancePct: number; usd: number } | null;
  thickestBelow: { price: number; distancePct: number; usd: number } | null;
  riskiestWhale: { side: "long" | "short"; liqPrice: number; distancePct: number; notionalUsd: number; leverage: number } | null;
  nearestSupport: number | null;
  nearestResistance: number | null;
  clustersInRange: LiqCluster[];
  whaleBucketsInRange: WhaleBucket[];
  pins: { key: string; rank: number; side: "long" | "short"; price: number; distancePct: number; label: string }[];
  levelLines: KeyLevelLine[];
  vacuumsInRange: VacuumZone[];
};

function deriveOverview(
  markPrice: number,
  rangePct: RangePct,
  liqMap: LiquidationMap | null,
  whale: WhaleAsset | null,
  keyLevels: KeyLevelSnapshot | null,
): OverviewDerived {
  const distancePctOf = (price: number) => (price / markPrice - 1) * 100;
  const inRange = (price: number) => Math.abs(distancePctOf(price)) <= rangePct;

  const clustersInRange = [
    ...(liqMap?.clusters_above ?? []),
    ...(liqMap?.clusters_below ?? []),
  ].filter((cluster) => inRange(cluster.price_center));

  const whaleBucketsInRange = (whale?.liquidation_buckets ?? [])
    .filter((bucket) => Math.abs(bucket.distance_from_mark_pct) <= rangePct);

  // 上/下方燃料 = 全市场簇 + 巨鲸桶（对应方向）合计。
  let fuelAboveUsd = 0;
  let fuelBelowUsd = 0;
  for (const cluster of clustersInRange) {
    if (cluster.price_center > markPrice) fuelAboveUsd += cluster.total_usd;
    else fuelBelowUsd += cluster.total_usd;
  }
  for (const bucket of whaleBucketsInRange) {
    if (bucket.distance_from_mark_pct > 0) fuelAboveUsd += bucket.short_notional_usd;
    else fuelBelowUsd += bucket.long_notional_usd;
  }

  const thickestAbove = thickestZone(clustersInRange, whaleBucketsInRange, markPrice, "above");
  const thickestBelow = thickestZone(clustersInRange, whaleBucketsInRange, markPrice, "below");

  const topPositions = [...(whale?.top_longs ?? []), ...(whale?.top_shorts ?? [])];
  let riskiestWhale: OverviewDerived["riskiestWhale"] = null;
  for (const position of topPositions) {
    if (position.liq_price == null || position.liq_price <= 0) continue;
    const distancePct = position.distance_to_liq_pct ?? distancePctOf(position.liq_price);
    if (riskiestWhale == null || Math.abs(distancePct) < Math.abs(riskiestWhale.distancePct)) {
      riskiestWhale = {
        side: position.side,
        liqPrice: position.liq_price,
        distancePct,
        notionalUsd: position.notional_usd,
        leverage: position.leverage,
      };
    }
  }

  const pins = buildPins(whale, markPrice).filter((pin) => Math.abs(pin.distancePct) <= rangePct);

  const nearestSupport = levelPrice(keyLevels?.nearest_strong_support);
  const nearestResistance = levelPrice(keyLevels?.nearest_strong_resistance);
  const levelLines: KeyLevelLine[] = [];
  if (nearestSupport != null && inRange(nearestSupport)) {
    levelLines.push({ price: nearestSupport, kind: "support", label: `强支撑 ${money(nearestSupport)}` });
  }
  if (nearestResistance != null && inRange(nearestResistance)) {
    levelLines.push({ price: nearestResistance, kind: "resistance", label: `强阻力 ${money(nearestResistance)}` });
  }
  for (const magnet of keyLevels?.magnet_levels ?? []) {
    if (magnet.price > 0 && inRange(magnet.price)) {
      levelLines.push({ price: magnet.price, kind: "magnet", label: `磁力位 ${money(magnet.price)}` });
    }
  }

  const vacuumsInRange = (liqMap?.vacuum_zones ?? []).filter((zone) => inRange(zone.midpoint));

  return {
    headline: buildHeadline({
      rangePct, fuelAboveUsd, fuelBelowUsd, thickestAbove, thickestBelow, riskiestWhale, vacuumsInRange,
    }),
    fuelAboveUsd,
    fuelBelowUsd,
    thickestAbove,
    thickestBelow,
    riskiestWhale,
    nearestSupport,
    nearestResistance,
    clustersInRange,
    whaleBucketsInRange,
    pins,
    levelLines,
    vacuumsInRange,
  };
}

function thickestZone(
  clusters: LiqCluster[],
  whaleBuckets: WhaleBucket[],
  markPrice: number,
  direction: "above" | "below",
): { price: number; distancePct: number; usd: number } | null {
  let best: { price: number; distancePct: number; usd: number } | null = null;
  const consider = (price: number, usd: number) => {
    if (usd <= 0) return;
    const isAbove = price > markPrice;
    if ((direction === "above") !== isAbove) return;
    if (best == null || usd > best.usd) {
      best = { price, distancePct: (price / markPrice - 1) * 100, usd };
    }
  };
  for (const cluster of clusters) consider(cluster.price_center, cluster.total_usd);
  for (const bucket of whaleBuckets) {
    consider(bucket.price_mid, direction === "above" ? bucket.short_notional_usd : bucket.long_notional_usd);
  }
  return best;
}

function buildPins(whale: WhaleAsset | null, markPrice: number) {
  const pins: OverviewDerived["pins"] = [];
  if (!whale) return pins;
  for (const side of ["long", "short"] as const) {
    const list = (side === "long" ? whale.top_longs : whale.top_shorts) ?? [];
    list.slice(0, 5).forEach((position, index) => {
      if (position.liq_price == null || position.liq_price <= 0) return;
      const distancePct = position.distance_to_liq_pct ?? (position.liq_price / markPrice - 1) * 100;
      pins.push({
        key: `${side}-${position.address}-${position.liq_price}`,
        rank: index + 1,
        side,
        price: position.liq_price,
        distancePct,
        label: `巨鲸${side === "long" ? "多头" : "空头"}#${index + 1} 清算价 ${money(position.liq_price)}（${signedPct(distancePct)}）· 仓位 ${compactUsd(position.notional_usd)} · ${position.leverage.toFixed(0)}x`,
      });
    });
  }
  return pins;
}

function buildHeadline(input: {
  rangePct: number;
  fuelAboveUsd: number;
  fuelBelowUsd: number;
  thickestAbove: { price: number; distancePct: number; usd: number } | null;
  thickestBelow: { price: number; distancePct: number; usd: number } | null;
  riskiestWhale: OverviewDerived["riskiestWhale"];
  vacuumsInRange: VacuumZone[];
}) {
  const { rangePct, fuelAboveUsd, fuelBelowUsd, thickestAbove, thickestBelow, riskiestWhale, vacuumsInRange } = input;
  const parts: string[] = [];
  if (fuelAboveUsd <= 0 && fuelBelowUsd <= 0) {
    parts.push(`现价上下 ${rangePct}% 内暂无明显清算燃料。`);
  } else {
    const aboveText = thickestAbove
      ? `上方约 ${compactUsd(fuelAboveUsd)} 空头清算燃料，最厚在 ${money(thickestAbove.price)} 附近`
      : `上方燃料约 ${compactUsd(fuelAboveUsd)}`;
    const belowText = thickestBelow
      ? `下方约 ${compactUsd(fuelBelowUsd)} 多头清算燃料，最厚在 ${money(thickestBelow.price)} 附近`
      : `下方燃料约 ${compactUsd(fuelBelowUsd)}`;
    parts.push(`${aboveText}；${belowText}。`);
    if (fuelAboveUsd > fuelBelowUsd * 1.3) {
      parts.push("上方燃料明显更厚：若价格上涨，被迫平仓的空头可能放大涨速（不代表一定上涨）。");
    } else if (fuelBelowUsd > fuelAboveUsd * 1.3) {
      parts.push("下方燃料明显更厚：若价格下跌，多头连环清算风险更集中（不代表一定下跌）。");
    } else {
      parts.push("上下燃料接近，暂无单侧清算优势。");
    }
  }
  if (riskiestWhale && Math.abs(riskiestWhale.distancePct) <= rangePct) {
    parts.push(`最危险的巨鲸头寸是${riskiestWhale.side === "long" ? "多头" : "空头"}（${compactUsd(riskiestWhale.notionalUsd)}），清算价 ${money(riskiestWhale.liqPrice)}，距现价仅 ${Math.abs(riskiestWhale.distancePct).toFixed(2)}%。`);
  }
  if (vacuumsInRange.length > 0) {
    parts.push(`${vacuumsInRange.map((zone) => money(zone.midpoint)).join("、")} 附近存在清算真空区，价格穿越时阻力较小。`);
  }
  return parts.join(" ");
}

function levelPrice(value: { price: number } | number | null | undefined): number | null {
  if (value == null) return null;
  if (typeof value === "number") return value > 0 ? value : null;
  return value.price > 0 ? value.price : null;
}

// ---------- 合成价格轴 ----------

function CompositeAxis({
  markPrice,
  rangePct,
  clusters,
  whaleBuckets,
  pins,
  levelLines,
  vacuums,
}: {
  markPrice: number;
  rangePct: RangePct;
  clusters: LiqCluster[];
  whaleBuckets: WhaleBucket[];
  pins: OverviewDerived["pins"];
  levelLines: KeyLevelLine[];
  vacuums: VacuumZone[];
}) {
  const width = 900;
  const height = 480;
  const center = width / 2;
  const top = 25;
  const bottom = 455;
  const halfBarWidth = 320;
  const plotHeight = bottom - top;
  const yForDistance = (distance: number) => top + (rangePct - distance) / (rangePct * 2) * plotHeight;
  const yForPrice = (price: number) => yForDistance((price / markPrice - 1) * 100);
  const maxClusterUsd = Math.max(1, ...clusters.map((cluster) => cluster.total_usd));
  const maxWhaleUsd = Math.max(
    1,
    ...whaleBuckets.flatMap((bucket) => [bucket.long_notional_usd, bucket.short_notional_usd]),
  );
  const ticks = [rangePct, rangePct / 2, 0, -rangePct / 2, -rangePct];

  return (
    <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/55 p-3">
      <div className="flex flex-wrap justify-between gap-2 text-[10px]">
        <span className="text-slate-400">← 全市场清算簇（峰值 {compactUsd(maxClusterUsd)}）</span>
        <span className="text-slate-500">上方＝价格上涨方向 · 中轴＝现价 {money(markPrice)}</span>
        <span className="text-slate-400">Hyperliquid 巨鲸清算桶（峰值 {compactUsd(maxWhaleUsd)}）→</span>
      </div>
      <svg viewBox={`0 0 ${width} ${height}`} className="mt-2 h-[420px] w-full" role="img" aria-label={`流动性总览，现价上下${rangePct}%`}>
        {/* 真空区底色 */}
        {vacuums.map((zone) => {
          const yFrom = Math.min(yForPrice(zone.price_from), yForPrice(zone.price_to));
          const yTo = Math.max(yForPrice(zone.price_from), yForPrice(zone.price_to));
          return (
            <g key={zone.midpoint}>
              <title>{`清算真空区 ${money(zone.price_from)}–${money(zone.price_to)}：${zone.note}`}</title>
              <rect x={80} y={yFrom} width={width - 160} height={Math.max(2, yTo - yFrom)} fill="#0ea5e9" opacity="0.08" />
            </g>
          );
        })}
        {/* 价格刻度 */}
        {ticks.map((distance) => {
          const y = yForDistance(distance);
          const price = markPrice * (1 + distance / 100);
          return (
            <g key={distance}>
              <line x1={70} y1={y} x2={width - 70} y2={y} stroke={distance === 0 ? "#22d3ee" : "#1e293b"} strokeWidth={distance === 0 ? 2 : 1} strokeDasharray={distance === 0 ? "5 4" : undefined} />
              <text x={4} y={y + 4} fill={distance === 0 ? "#67e8f9" : "#64748b"} fontSize="12">{money(price)}</text>
              <text x={width - 58} y={y + 4} fill="#475569" fontSize="11">{distance > 0 ? "+" : ""}{distance}%</text>
            </g>
          );
        })}
        {/* 左：全市场清算簇 */}
        {clusters.map((cluster) => {
          const yFrom = Math.min(yForPrice(cluster.price_from), yForPrice(cluster.price_to));
          const yTo = Math.max(yForPrice(cluster.price_from), yForPrice(cluster.price_to));
          const barWidth = cluster.total_usd / maxClusterUsd * halfBarWidth;
          const color = cluster.side === "long" ? "#10b981" : "#f43f5e";
          return (
            <g key={`cluster-${cluster.price_center}-${cluster.side}`}>
              <title>{`全市场${cluster.side === "long" ? "多头" : "空头"}清算簇 ${money(cluster.price_from)}–${money(cluster.price_to)} · ${compactUsd(cluster.total_usd)}（${signedPct((cluster.price_center / markPrice - 1) * 100)}）`}</title>
              <rect x={center - 12 - barWidth} y={yFrom} width={barWidth} height={Math.max(3, yTo - yFrom)} fill={color} opacity="0.75" />
            </g>
          );
        })}
        {/* 右：巨鲸清算桶（多空同桶时并排显示） */}
        {whaleBuckets.map((bucket) => {
          const yMid = yForDistance(bucket.distance_from_mark_pct);
          const barHeight = Math.max(3, 0.5 / (rangePct * 2) * plotHeight * 0.82);
          const longWidth = bucket.long_notional_usd / maxWhaleUsd * halfBarWidth;
          const shortWidth = bucket.short_notional_usd / maxWhaleUsd * halfBarWidth;
          return (
            <g key={`whale-${bucket.price_mid}`}>
              <title>{`巨鲸清算桶 ${money(bucket.price_from)}–${money(bucket.price_to)} | 多头 ${compactUsd(bucket.long_notional_usd)}/${bucket.long_count}个 | 空头 ${compactUsd(bucket.short_notional_usd)}/${bucket.short_count}个`}</title>
              {bucket.long_notional_usd > 0 && <rect x={center + 12} y={yMid - barHeight / 2} width={longWidth} height={barHeight} fill="#10b981" opacity="0.75" />}
              {bucket.short_notional_usd > 0 && <rect x={center + 12 + longWidth} y={yMid - barHeight / 2} width={shortWidth} height={barHeight} fill="#f43f5e" opacity="0.75" />}
            </g>
          );
        })}
        {/* 中轴 */}
        <line x1={center} y1={top} x2={center} y2={bottom} stroke="#22d3ee" strokeWidth="1" opacity="0.7" />
        {/* 关键位横线 */}
        {levelLines.map((line) => {
          const y = yForPrice(line.price);
          const color = line.kind === "support" ? "#34d399" : line.kind === "resistance" ? "#fb7185" : "#c4b5fd";
          return (
            <g key={`${line.kind}-${line.price}`}>
              <title>{line.label}</title>
              <line x1={70} y1={y} x2={width - 70} y2={y} stroke={color} strokeWidth="1.5" strokeDasharray="8 5" opacity="0.85" />
              <text x={width - 66} y={y - 4} fill={color} fontSize="11" textAnchor="end">{line.label}</text>
            </g>
          );
        })}
        {/* 巨鲸 Top 头寸清算价钉点 */}
        {pins.map((pin) => {
          const y = yForDistance(pin.distancePct);
          const x = center + 4;
          const color = pin.side === "long" ? "#34d399" : "#fb7185";
          return (
            <g key={pin.key}>
              <title>{pin.label}</title>
              <path d={`M ${x} ${y - 6} L ${x + 5} ${y} L ${x} ${y + 6} L ${x - 5} ${y} Z`} fill={color} stroke="#0f172a" strokeWidth="1" />
            </g>
          );
        })}
      </svg>
      <div className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-slate-500">
        <span><span className="text-emerald-400">■</span> 多头清算（价格下跌触发）</span>
        <span><span className="text-rose-400">■</span> 空头清算（价格上涨触发）</span>
        <span><span className="text-emerald-400">◆</span>/<span className="text-rose-400">◆</span> 巨鲸 Top 头寸清算价</span>
        <span><span className="text-emerald-400">--</span> 强支撑 · <span className="text-rose-400">--</span> 强阻力 · <span className="text-violet-300">--</span> 磁力位</span>
        <span><span className="text-sky-400">▒</span> 清算真空区</span>
      </div>
    </div>
  );
}

function OverviewMetric({ label, value, sub, tone }: { label: string; value: string; sub?: string; tone: string }) {
  return (
    <div className="rounded-lg bg-slate-950/70 p-3">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`mt-1 text-sm font-semibold ${tone}`}>{value}</div>
      {sub && <div className="mt-1 text-[9px] text-slate-600">{sub}</div>}
    </div>
  );
}

// ---------- 格式化 ----------

function compactUsd(value: number) {
  const absolute = Math.abs(value);
  if (absolute >= 1e9) return `$${(absolute / 1e9).toFixed(2)}B`;
  if (absolute >= 1e6) return `$${(absolute / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `$${(absolute / 1e3).toFixed(1)}K`;
  return `$${absolute.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
}

function money(value: number) {
  const digits = value >= 1_000 ? 0 : value >= 10 ? 2 : 4;
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: digits })}`;
}

function signedPct(value: number) {
  return `${value >= 0 ? "+" : ""}${value.toFixed(2)}%`;
}
