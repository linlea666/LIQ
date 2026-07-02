"use client";

import { useEffect, useMemo, useState } from "react";

import { API_BASE } from "@/lib/constants";

type SymbolName = "BTC" | "ETH";
type RangePct = 10 | 25 | 50;

type Quality = {
  valid: boolean;
  age_sec?: number | null;
  points: number;
  reason: string;
  status: "fresh" | "stale" | "pending" | "missing";
};

type PriceBucket = {
  price_from: number;
  price_to: number;
  price_mid: number;
  distance_from_mark_pct: number;
  long_notional_usd: number;
  short_notional_usd: number;
  long_count: number;
  short_count: number;
  long_avg_leverage: number;
  short_avg_leverage: number;
};

type AssetDistribution = {
  symbol: SymbolName;
  mark_price?: number | null;
  as_of_ts?: number | null;
  bin_size_pct: number;
  position_count: number;
  long_count: number;
  short_count: number;
  long_notional_usd: number;
  short_notional_usd: number;
  valid_entry_price_count: number;
  invalid_entry_price_count: number;
  valid_liquidation_price_count: number;
  invalid_liquidation_price_count: number;
  entry_buckets: PriceBucket[];
  liquidation_buckets: PriceBucket[];
  quality: Quality;
  caveats: string[];
};

type DistributionResponse = {
  source: string;
  sample_scope: string;
  fetched_at_ts?: number | null;
  score_weight: 0;
  assets: Record<SymbolName, AssetDistribution>;
};

const RANGE_OPTIONS: RangePct[] = [10, 25, 50];

export function HyperliquidWhaleDistribution() {
  const [payload, setPayload] = useState<DistributionResponse | null>(null);
  const [selectedSymbol, setSelectedSymbol] = useState<SymbolName>("BTC");
  const [rangePct, setRangePct] = useState<RangePct>(25);
  const [error, setError] = useState("");

  useEffect(() => {
    let disposed = false;
    const load = async () => {
      try {
        const response = await fetch(
          `${API_BASE}/api/trend/hyperliquid-whale-distributions`,
          { cache: "no-store" },
        );
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const next = await response.json() as DistributionResponse;
        if (!disposed) {
          setPayload(next);
          setError("");
        }
      } catch (cause) {
        if (!disposed) {
          setError(cause instanceof Error ? cause.message : "巨鲸仓位统计加载失败");
        }
      }
    };
    void load();
    const timer = window.setInterval(() => void load(), 60_000);
    return () => {
      disposed = true;
      window.clearInterval(timer);
    };
  }, []);

  const asset = payload?.assets[selectedSymbol] ?? null;
  const totalNotional = (asset?.long_notional_usd ?? 0) + (asset?.short_notional_usd ?? 0);
  const longShare = totalNotional > 0 ? (asset?.long_notional_usd ?? 0) / totalNotional : 0;

  return (
    <section className="rounded-xl border border-violet-900/60 bg-gradient-to-br from-slate-900/75 to-violet-950/15 p-4 md:p-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h2 className="font-semibold text-slate-100">Hyperliquid 巨鲸仓位价格分布</h2>
          <p className="mt-0.5 text-[11px] text-slate-500">官方巨鲸池当前仓位 · 开仓成本与估算爆仓价 · 固定零权重</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1">
            {(["BTC", "ETH"] as const).map((symbol) => (
              <button
                key={symbol}
                type="button"
                onClick={() => setSelectedSymbol(symbol)}
                className={`rounded px-4 py-1 text-xs ${selectedSymbol === symbol ? "bg-violet-600 text-white" : "text-slate-400 hover:text-slate-200"}`}
              >
                {symbol}
              </button>
            ))}
          </div>
          <div className="flex rounded-lg border border-slate-700 bg-slate-950 p-1">
            {RANGE_OPTIONS.map((value) => (
              <button
                key={value}
                type="button"
                onClick={() => setRangePct(value)}
                className={`rounded px-2.5 py-1 text-[10px] ${rangePct === value ? "bg-slate-700 text-white" : "text-slate-500 hover:text-slate-300"}`}
              >
                ±{value}%
              </button>
            ))}
          </div>
        </div>
      </div>

      {error && (
        <div className="mt-3 rounded border border-amber-800/60 bg-amber-950/25 px-3 py-2 text-xs text-amber-300">
          本模块刷新失败：{error}。主趋势与资金流不受影响。
        </div>
      )}

      {!asset ? (
        <div className="mt-4 flex h-72 items-center justify-center rounded-lg border border-slate-800 text-xs text-slate-600">正在读取巨鲸仓位统计…</div>
      ) : asset.mark_price == null || asset.position_count === 0 ? (
        <div className="mt-4 flex h-72 items-center justify-center rounded-lg border border-slate-800 text-xs text-slate-500">
          {qualityText(asset.quality)}
        </div>
      ) : (
        <>
          <div className="mt-4 grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-6">
            <Metric label="当前标记价" value={money(asset.mark_price)} />
            <Metric label="巨鲸仓位" value={`${asset.position_count} 个`} />
            <Metric label="多头名义仓位" value={compactUsd(asset.long_notional_usd)} tone="text-emerald-400" sub={`${asset.long_count} 个 · ${(longShare * 100).toFixed(1)}%`} />
            <Metric label="空头名义仓位" value={compactUsd(asset.short_notional_usd)} tone="text-rose-400" sub={`${asset.short_count} 个 · ${((1 - longShare) * 100).toFixed(1)}%`} />
            <Metric label="有效爆仓价" value={`${asset.valid_liquidation_price_count} 个`} />
            <Metric label="无有效爆仓价" value={`${asset.invalid_liquidation_price_count} 个`} tone={asset.invalid_liquidation_price_count ? "text-amber-400" : "text-slate-100"} />
          </div>

          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <PriceLadder
              title="开仓成本分布"
              note="巨鲸当前持仓的平均开仓成本密集区"
              buckets={asset.entry_buckets}
              markPrice={asset.mark_price}
              rangePct={rangePct}
              binSizePct={asset.bin_size_pct}
            />
            <PriceLadder
              title="估算爆仓价分布"
              note="交叉保证金价格会随全账户权益动态变化"
              buckets={asset.liquidation_buckets}
              markPrice={asset.mark_price}
              rangePct={rangePct}
              binSizePct={asset.bin_size_pct}
            />
          </div>

          <div className="mt-3 flex flex-wrap justify-between gap-2 text-[10px] text-slate-500">
            <span>{qualityText(asset.quality)} · 价格分桶 {asset.bin_size_pct.toFixed(1)}% · 评分权重 {payload?.score_weight ?? 0}</span>
            <span>数据时间：{asset.as_of_ts ? new Date(asset.as_of_ts * 1000).toLocaleString() : "—"}</span>
          </div>
          <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/55 p-3 text-[10px] leading-5 text-slate-500">
            {(asset.caveats.length ? asset.caveats : ["该统计仅供仓位结构观察，不构成交易建议。"]).map((item) => <div key={item}>• {item}</div>)}
          </div>
        </>
      )}
    </section>
  );
}

function PriceLadder({
  title,
  note,
  buckets,
  markPrice,
  rangePct,
  binSizePct,
}: {
  title: string;
  note: string;
  buckets: PriceBucket[];
  markPrice: number;
  rangePct: RangePct;
  binSizePct: number;
}) {
  const visible = useMemo(
    () => buckets.filter((bucket) => Math.abs(bucket.distance_from_mark_pct) <= rangePct),
    [buckets, rangePct],
  );
  const hidden = useMemo(
    () => buckets.filter((bucket) => Math.abs(bucket.distance_from_mark_pct) > rangePct),
    [buckets, rangePct],
  );
  const maxNotional = Math.max(
    1,
    ...visible.flatMap((bucket) => [bucket.long_notional_usd, bucket.short_notional_usd]),
  );
  const thickest = visible.reduce<PriceBucket | null>((best, bucket) => {
    if (!best) return bucket;
    const total = bucket.long_notional_usd + bucket.short_notional_usd;
    const bestTotal = best.long_notional_usd + best.short_notional_usd;
    return total > bestTotal ? bucket : best;
  }, null);
  const hiddenLong = hidden.reduce((sum, bucket) => sum + bucket.long_notional_usd, 0);
  const hiddenShort = hidden.reduce((sum, bucket) => sum + bucket.short_notional_usd, 0);

  const width = 640;
  const height = 420;
  const center = width / 2;
  const top = 25;
  const bottom = 395;
  const halfBarWidth = 245;
  const plotHeight = bottom - top;
  const yForDistance = (distance: number) => top + (rangePct - distance) / (rangePct * 2) * plotHeight;
  const barHeight = Math.max(2, binSizePct / (rangePct * 2) * plotHeight * 0.82);
  const ticks = [rangePct, rangePct / 2, 0, -rangePct / 2, -rangePct];

  return (
    <div className="rounded-xl border border-slate-800 bg-slate-950/55 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div><div className="text-sm font-medium text-slate-200">{title}</div><div className="text-[10px] text-slate-600">{note}</div></div>
        <div className="text-right text-[10px] text-slate-500">
          <div>最厚区：{thickest ? `${money(thickest.price_from)}–${money(thickest.price_to)}` : "当前范围无数据"}</div>
          <div>图外：多 {compactUsd(hiddenLong)} · 空 {compactUsd(hiddenShort)}</div>
        </div>
      </div>
      <div className="mt-3 flex justify-center gap-5 text-[10px]"><span className="text-emerald-400">← 多头厚度</span><span className="text-violet-300">现价中轴</span><span className="text-rose-400">空头厚度 →</span></div>
      <svg viewBox={`0 0 ${width} ${height}`} className="mt-1 h-[360px] w-full" role="img" aria-label={`${title}，现价上下${rangePct}%`}>
        <rect x="65" y={top} width={halfBarWidth} height={plotHeight} fill="#052e16" opacity="0.12" />
        <rect x={center} y={top} width={halfBarWidth} height={plotHeight} fill="#4c0519" opacity="0.12" />
        {ticks.map((distance) => {
          const y = yForDistance(distance);
          const price = markPrice * (1 + distance / 100);
          return <g key={distance}><line x1="55" y1={y} x2="575" y2={y} stroke={distance === 0 ? "#a78bfa" : "#1e293b"} strokeWidth={distance === 0 ? 2 : 1} strokeDasharray={distance === 0 ? "5 4" : undefined} /><text x="5" y={y + 4} fill={distance === 0 ? "#c4b5fd" : "#64748b"} fontSize="11">{money(price)}</text><text x="582" y={y + 4} fill="#475569" fontSize="10">{distance > 0 ? "+" : ""}{distance}%</text></g>;
        })}
        {visible.map((bucket) => {
          const y = yForDistance(bucket.distance_from_mark_pct) - barHeight / 2;
          const longWidth = bucket.long_notional_usd / maxNotional * halfBarWidth;
          const shortWidth = bucket.short_notional_usd / maxNotional * halfBarWidth;
          const tooltip = `${money(bucket.price_from)}–${money(bucket.price_to)} | 多头 ${compactUsd(bucket.long_notional_usd)} / ${bucket.long_count}个 / 均杠杆 ${bucket.long_avg_leverage.toFixed(1)}x | 空头 ${compactUsd(bucket.short_notional_usd)} / ${bucket.short_count}个 / 均杠杆 ${bucket.short_avg_leverage.toFixed(1)}x`;
          return <g key={`${bucket.price_from}-${bucket.price_to}`}><title>{tooltip}</title>{longWidth > 0 && <rect x={center - longWidth} y={y} width={longWidth} height={barHeight} fill="#10b981" opacity="0.82" />}{shortWidth > 0 && <rect x={center} y={y} width={shortWidth} height={barHeight} fill="#f43f5e" opacity="0.82" />}</g>;
        })}
        <line x1={center} y1={top} x2={center} y2={bottom} stroke="#a78bfa" strokeWidth="1" opacity="0.8" />
      </svg>
      {visible.length === 0 && <div className="-mt-52 mb-40 text-center text-xs text-slate-600">当前价格范围内没有有效仓位</div>}
      <p className="mt-1 text-[10px] text-slate-600">柱宽按当前图内最大单侧名义仓位线性缩放；悬停柱体查看金额、数量和平均杠杆。</p>
    </div>
  );
}

function Metric({ label, value, sub, tone = "text-slate-100" }: { label: string; value: string; sub?: string; tone?: string }) {
  return <div className="rounded-lg bg-slate-950/70 p-3"><div className="text-[10px] text-slate-500">{label}</div><div className={`mt-1 text-sm font-medium ${tone}`}>{value}</div>{sub && <div className="mt-1 text-[9px] text-slate-600">{sub}</div>}</div>;
}

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

function qualityText(quality: Quality) {
  if (quality.status === "pending") return `加载中 · ${quality.reason}`;
  if (quality.valid) return `数据有效${quality.age_sec == null ? "" : ` · ${Math.round(quality.age_sec / 60)}分钟前`}`;
  return `${quality.status === "stale" ? "数据已过期" : "数据不可用"} · ${quality.reason || "等待数据恢复"}`;
}
