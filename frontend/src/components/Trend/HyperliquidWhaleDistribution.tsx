"use client";

import { useEffect, useMemo, useState } from "react";

import { API_BASE } from "@/lib/constants";

type SymbolName = "BTC" | "ETH";
type RangePct = 10 | 25 | 50;
type PositionSide = "long" | "short";
type LadderKind = "entry" | "liquidation";

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

type SideBucketInsight = {
  bucket: PriceBucket;
  side: PositionSide;
  notionalUsd: number;
  count: number;
  avgLeverage: number;
};

type WhaleInsights = {
  longEntry: SideBucketInsight | null;
  shortEntry: SideBucketInsight | null;
  upperShortLiquidation: SideBucketInsight | null;
  lowerLongLiquidation: SideBucketInsight | null;
  upperShortNear5Usd: number;
  lowerLongNear5Usd: number;
};

const RANGE_OPTIONS: RangePct[] = [10, 25, 50];
const BEGINNER_INSIGHT_RANGE_PCT = 25;
const RANGE_LABELS: Record<RangePct, string> = {
  10: "近场 ±10%",
  25: "常用 ±25%",
  50: "全景 ±50%",
};

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
  const liquidationTotal = (asset?.valid_liquidation_price_count ?? 0) + (asset?.invalid_liquidation_price_count ?? 0);
  const liquidationCoverage = liquidationTotal > 0
    ? (asset?.valid_liquidation_price_count ?? 0) / liquidationTotal
    : 0;
  const insights = asset?.mark_price == null ? null : deriveWhaleInsights(asset);
  const sharedMaxNotional = asset == null
    ? 1
    : maxVisibleSideNotional(
      [...asset.entry_buckets, ...asset.liquidation_buckets],
      rangePct,
    );

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
                {RANGE_LABELS[value]}
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
            <Metric label="爆仓价覆盖率" value={`${(liquidationCoverage * 100).toFixed(1)}%`} sub={`${asset.valid_liquidation_price_count}/${liquidationTotal} 个仓位`} />
            <Metric label="未提供爆仓价" value={`${asset.invalid_liquidation_price_count} 个`} tone={asset.invalid_liquidation_price_count ? "text-amber-400" : "text-slate-100"} sub="已从爆仓图排除" />
          </div>

          {insights && <BeginnerSummary asset={asset} insights={insights} />}

          {!asset.quality.valid && (
            <div className="mt-3 rounded-lg border border-amber-800/60 bg-amber-950/25 px-3 py-2 text-xs text-amber-300">
              当前展示的是最后一次可用快照：{qualityText(asset.quality)}。过期数据只供回看，不应用于判断当前价格风险。
            </div>
          )}

          <div className="mt-4 rounded-lg border border-blue-900/50 bg-blue-950/20 px-3 py-2 text-xs leading-5 text-blue-200">
            <span className="font-medium">读图规则：</span>图上方是价格上涨方向，图下方是价格下跌方向。绿色只表示多头仓位，不等于支撑；红色只表示空头仓位，不等于阻力。
          </div>

          <div className="mt-4 grid gap-4 xl:grid-cols-2">
            <PriceLadder
              kind="entry"
              title="开仓成本分布"
              note="巨鲸当前持仓的平均开仓成本密集区"
              buckets={asset.entry_buckets}
              markPrice={asset.mark_price}
              rangePct={rangePct}
              binSizePct={asset.bin_size_pct}
              maxNotional={sharedMaxNotional}
            />
            <PriceLadder
              kind="liquidation"
              title="估算爆仓价分布"
              note="交叉保证金价格会随全账户权益动态变化"
              buckets={asset.liquidation_buckets}
              markPrice={asset.mark_price}
              rangePct={rangePct}
              binSizePct={asset.bin_size_pct}
              maxNotional={sharedMaxNotional}
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

function BeginnerSummary({ asset, insights }: { asset: AssetDistribution; insights: WhaleInsights }) {
  const nearMax = Math.max(1, insights.upperShortNear5Usd, insights.lowerLongNear5Usd);
  const balanceText = nearRiskSentence(
    insights.upperShortNear5Usd,
    insights.lowerLongNear5Usd,
  );
  return (
    <div className="mt-4 rounded-xl border border-violet-800/50 bg-violet-950/15 p-3 md:p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-violet-100">新手先看这 4 个位置</h3>
          <p className="mt-1 text-[10px] text-slate-500">从现价 ±25% 常用范围提取：先看成本在哪里，再看上涨和下跌分别可能触发哪一侧爆仓；这些位置不是交易指令。</p>
        </div>
        <div className="rounded bg-slate-950/70 px-2 py-1 text-xs text-violet-200">
          Hyperliquid 标记价 {asset.mark_price == null ? "—" : money(asset.mark_price)}
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <InsightCard
          label="多头金额最厚开仓区"
          insight={insights.longEntry}
          markPrice={asset.mark_price}
          tone="emerald"
          empty="暂无有效多头开仓价"
        />
        <InsightCard
          label="空头金额最厚开仓区"
          insight={insights.shortEntry}
          markPrice={asset.mark_price}
          tone="rose"
          empty="暂无有效空头开仓价"
        />
        <InsightCard
          label="上涨重点看：金额最厚空头爆仓区"
          insight={insights.upperShortLiquidation}
          markPrice={asset.mark_price}
          tone="amber"
          empty="现价上方暂无有效空头爆仓价"
          help="上涨接近时，空头被迫平仓可能放大涨速"
        />
        <InsightCard
          label="下跌重点看：金额最厚多头爆仓区"
          insight={insights.lowerLongLiquidation}
          markPrice={asset.mark_price}
          tone="rose"
          empty="现价下方暂无有效多头爆仓价"
          help="下跌接近时，多头被迫平仓可能放大跌速"
        />
      </div>
      <div className="mt-3 rounded-lg border border-slate-800 bg-slate-950/60 p-3">
        <div className="flex flex-wrap items-center justify-between gap-2 text-xs">
          <span className="font-medium text-slate-200">现价附近 ±5% 的潜在清算燃料</span>
          <span className="text-[10px] text-slate-500">只比较当前暴露，不预测先涨还是先跌</span>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2">
          <ExposureBar
            label="上方空头爆仓暴露"
            value={insights.upperShortNear5Usd}
            max={nearMax}
            color="bg-amber-500"
          />
          <ExposureBar
            label="下方多头爆仓暴露"
            value={insights.lowerLongNear5Usd}
            max={nearMax}
            color="bg-rose-500"
          />
        </div>
        <p className="mt-3 text-xs leading-5 text-slate-400">{balanceText}</p>
      </div>
    </div>
  );
}

function InsightCard({
  label,
  insight,
  markPrice,
  tone,
  empty,
  help,
}: {
  label: string;
  insight: SideBucketInsight | null;
  markPrice?: number | null;
  tone: "emerald" | "rose" | "amber";
  empty: string;
  help?: string;
}) {
  const colors = {
    emerald: "border-emerald-900/60 text-emerald-300",
    rose: "border-rose-900/60 text-rose-300",
    amber: "border-amber-900/60 text-amber-300",
  };
  if (!insight) {
    return <div className={`rounded-lg border bg-slate-950/65 p-3 ${colors[tone]}`}><div className="text-[10px] text-slate-500">{label}</div><div className="mt-2 text-xs text-slate-500">{empty}</div></div>;
  }
  return (
    <div className={`rounded-lg border bg-slate-950/65 p-3 ${colors[tone]}`}>
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className="mt-1 text-sm font-semibold">{money(insight.bucket.price_from)}–{money(insight.bucket.price_to)}</div>
      <div className="mt-1 text-[10px] text-slate-400">
        距现价 {distanceText(insight.bucket.price_mid, markPrice)} · {compactUsd(insight.notionalUsd)} · {insight.count} 个仓位
      </div>
      {help && <div className="mt-2 text-[10px] leading-4 text-slate-500">{help}</div>}
    </div>
  );
}

function ExposureBar({ label, value, max, color }: { label: string; value: number; max: number; color: string }) {
  return (
    <div>
      <div className="flex justify-between text-[10px]"><span className="text-slate-500">{label}</span><span className="text-slate-200">{compactUsd(value)}</span></div>
      <div className="mt-1 h-2 overflow-hidden rounded bg-slate-800"><div className={`h-full ${color}`} style={{ width: `${value / max * 100}%` }} /></div>
    </div>
  );
}

function PriceLadder({
  kind,
  title,
  note,
  buckets,
  markPrice,
  rangePct,
  binSizePct,
  maxNotional,
}: {
  kind: LadderKind;
  title: string;
  note: string;
  buckets: PriceBucket[];
  markPrice: number;
  rangePct: RangePct;
  binSizePct: number;
  maxNotional: number;
}) {
  const [selectedKey, setSelectedKey] = useState("");
  const visible = useMemo(
    () => buckets.filter((bucket) => Math.abs(bucket.distance_from_mark_pct) <= rangePct),
    [buckets, rangePct],
  );
  const hidden = useMemo(
    () => buckets.filter((bucket) => Math.abs(bucket.distance_from_mark_pct) > rangePct),
    [buckets, rangePct],
  );
  const longPeak = strongestSideBucket(
    visible,
    "long",
    kind === "liquidation" ? (bucket) => bucket.price_mid < markPrice : undefined,
  );
  const shortPeak = strongestSideBucket(
    visible,
    "short",
    kind === "liquidation" ? (bucket) => bucket.price_mid > markPrice : undefined,
  );
  const selectedBucket = visible.find((bucket) => bucketKey(bucket) === selectedKey)
    ?? strongestCombinedBucket(visible);
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
          <div className="text-emerald-400">多头最厚：{peakSummary(longPeak)}</div>
          <div className="text-rose-400">空头最厚：{peakSummary(shortPeak)}</div>
          <div>当前范围外未显示：多 {compactUsd(hiddenLong)} · 空 {compactUsd(hiddenShort)}</div>
        </div>
      </div>
      <div className="mt-3 flex flex-wrap justify-center gap-x-5 gap-y-1 text-[10px]"><span className="text-emerald-400">← 多头厚度</span><span className="text-violet-300">现价中轴</span><span className="text-rose-400">空头厚度 →</span><span className="w-full text-center text-slate-600">上方＝价格上涨方向 · 下方＝价格下跌方向</span></div>
      <svg viewBox={`0 0 ${width} ${height}`} className="mt-1 h-[360px] w-full" role="img" aria-label={`${title}，现价上下${rangePct}%`}>
        <rect x="65" y={top} width={halfBarWidth} height={plotHeight} fill="#052e16" opacity="0.12" />
        <rect x={center} y={top} width={halfBarWidth} height={plotHeight} fill="#4c0519" opacity="0.12" />
        {ticks.map((distance) => {
          const y = yForDistance(distance);
          const price = markPrice * (1 + distance / 100);
          return <g key={distance}><line x1="55" y1={y} x2="575" y2={y} stroke={distance === 0 ? "#a78bfa" : "#1e293b"} strokeWidth={distance === 0 ? 2 : 1} strokeDasharray={distance === 0 ? "5 4" : undefined} /><text x="5" y={y + 4} fill={distance === 0 ? "#c4b5fd" : "#64748b"} fontSize="11">{money(price)}</text><text x="582" y={y + 4} fill="#475569" fontSize="10">{distance > 0 ? "+" : ""}{distance}%</text></g>;
        })}
        {visible.map((bucket) => {
          const key = bucketKey(bucket);
          const y = yForDistance(bucket.distance_from_mark_pct) - barHeight / 2;
          const longWidth = bucket.long_notional_usd / maxNotional * halfBarWidth;
          const shortWidth = bucket.short_notional_usd / maxNotional * halfBarWidth;
          const tooltip = `${money(bucket.price_from)}–${money(bucket.price_to)} | 多头 ${compactUsd(bucket.long_notional_usd)} / ${bucket.long_count}个 / 均杠杆 ${bucket.long_avg_leverage.toFixed(1)}x | 空头 ${compactUsd(bucket.short_notional_usd)} / ${bucket.short_count}个 / 均杠杆 ${bucket.short_avg_leverage.toFixed(1)}x`;
          const selectBucket = () => setSelectedKey(key);
          return <g key={key} role="button" tabIndex={0} aria-label={tooltip} onClick={selectBucket} onKeyDown={(event) => { if (event.key === "Enter" || event.key === " ") selectBucket(); }} className="cursor-pointer"><title>{tooltip}</title>{longWidth > 0 && <rect x={center - longWidth} y={y} width={longWidth} height={barHeight} fill="#10b981" opacity="0.82" stroke={selectedBucket === bucket ? "#d1fae5" : "none"} strokeWidth="1" />}{shortWidth > 0 && <rect x={center} y={y} width={shortWidth} height={barHeight} fill="#f43f5e" opacity="0.82" stroke={selectedBucket === bucket ? "#ffe4e6" : "none"} strokeWidth="1" />}</g>;
        })}
        <line x1={center} y1={top} x2={center} y2={bottom} stroke="#a78bfa" strokeWidth="1" opacity="0.8" />
      </svg>
      {visible.length === 0 && <div className="-mt-52 mb-40 text-center text-xs text-slate-600">当前价格范围内没有有效仓位</div>}
      {selectedBucket && <SelectedBucketDetails bucket={selectedBucket} markPrice={markPrice} />}
      <p className="mt-2 text-[10px] text-slate-600">两张图共用同一柱宽尺度，可直接比较金额厚度；点击、轻触或悬停价格带查看详情。</p>
    </div>
  );
}

function SelectedBucketDetails({ bucket, markPrice }: { bucket: PriceBucket; markPrice: number }) {
  return (
    <div className="mt-2 rounded-lg border border-slate-800 bg-slate-950/80 p-3 text-xs">
      <div className="flex flex-wrap justify-between gap-2 text-slate-300"><span>已选价格带：{money(bucket.price_from)}–{money(bucket.price_to)}</span><span>距现价 {distanceText(bucket.price_mid, markPrice)}</span></div>
      <div className="mt-2 grid grid-cols-2 gap-2">
        <div><span className="text-slate-600">多头仓位</span><div className="text-emerald-400">{compactUsd(bucket.long_notional_usd)} · {bucket.long_count} 个 · 均杠杆 {bucket.long_avg_leverage.toFixed(1)}x</div></div>
        <div><span className="text-slate-600">空头仓位</span><div className="text-rose-400">{compactUsd(bucket.short_notional_usd)} · {bucket.short_count} 个 · 均杠杆 {bucket.short_avg_leverage.toFixed(1)}x</div></div>
      </div>
    </div>
  );
}

function deriveWhaleInsights(asset: AssetDistribution): WhaleInsights {
  const markPrice = asset.mark_price ?? 0;
  const inBeginnerRange = (bucket: PriceBucket) => (
    Math.abs(bucket.distance_from_mark_pct) <= BEGINNER_INSIGHT_RANGE_PCT
  );
  return {
    longEntry: strongestSideBucket(asset.entry_buckets, "long", inBeginnerRange),
    shortEntry: strongestSideBucket(asset.entry_buckets, "short", inBeginnerRange),
    upperShortLiquidation: strongestSideBucket(
      asset.liquidation_buckets,
      "short",
      (bucket) => bucket.price_mid > markPrice && inBeginnerRange(bucket),
    ),
    lowerLongLiquidation: strongestSideBucket(
      asset.liquidation_buckets,
      "long",
      (bucket) => bucket.price_mid < markPrice && inBeginnerRange(bucket),
    ),
    upperShortNear5Usd: asset.liquidation_buckets.reduce(
      (sum, bucket) => sum + (
        bucket.distance_from_mark_pct > 0 && bucket.distance_from_mark_pct <= 5
          ? bucket.short_notional_usd
          : 0
      ),
      0,
    ),
    lowerLongNear5Usd: asset.liquidation_buckets.reduce(
      (sum, bucket) => sum + (
        bucket.distance_from_mark_pct < 0 && bucket.distance_from_mark_pct >= -5
          ? bucket.long_notional_usd
          : 0
      ),
      0,
    ),
  };
}

function strongestSideBucket(
  buckets: PriceBucket[],
  side: PositionSide,
  predicate?: (bucket: PriceBucket) => boolean,
): SideBucketInsight | null {
  let best: SideBucketInsight | null = null;
  for (const bucket of buckets) {
    if (predicate && !predicate(bucket)) continue;
    const notionalUsd = side === "long" ? bucket.long_notional_usd : bucket.short_notional_usd;
    if (notionalUsd <= 0 || (best && notionalUsd <= best.notionalUsd)) continue;
    best = {
      bucket,
      side,
      notionalUsd,
      count: side === "long" ? bucket.long_count : bucket.short_count,
      avgLeverage: side === "long" ? bucket.long_avg_leverage : bucket.short_avg_leverage,
    };
  }
  return best;
}

function strongestCombinedBucket(buckets: PriceBucket[]) {
  return buckets.reduce<PriceBucket | null>((best, bucket) => {
    if (!best) return bucket;
    const total = bucket.long_notional_usd + bucket.short_notional_usd;
    const bestTotal = best.long_notional_usd + best.short_notional_usd;
    return total > bestTotal ? bucket : best;
  }, null);
}

function maxVisibleSideNotional(buckets: PriceBucket[], rangePct: RangePct) {
  return Math.max(
    1,
    ...buckets
      .filter((bucket) => Math.abs(bucket.distance_from_mark_pct) <= rangePct)
      .flatMap((bucket) => [bucket.long_notional_usd, bucket.short_notional_usd]),
  );
}

function peakSummary(insight: SideBucketInsight | null) {
  if (!insight) return "当前范围无数据";
  return `${money(insight.bucket.price_from)}–${money(insight.bucket.price_to)} · ${compactUsd(insight.notionalUsd)}`;
}

function bucketKey(bucket: PriceBucket) {
  return `${bucket.price_from}-${bucket.price_to}`;
}

function distanceText(price: number, markPrice?: number | null) {
  if (!markPrice || markPrice <= 0) return "—";
  const distance = (price / markPrice - 1) * 100;
  return `${distance >= 0 ? "+" : ""}${distance.toFixed(2)}%`;
}

function nearRiskSentence(upperShortUsd: number, lowerLongUsd: number) {
  if (upperShortUsd <= 0 && lowerLongUsd <= 0) {
    return "现价上下 5% 内暂时没有有效的巨鲸爆仓价暴露。";
  }
  if (upperShortUsd > lowerLongUsd * 1.15) {
    return "现价附近，上方空头爆仓暴露更多；如果价格先上涨，潜在轧空燃料更集中，但这不代表价格一定会上涨。";
  }
  if (lowerLongUsd > upperShortUsd * 1.15) {
    return "现价附近，下方多头爆仓暴露更多；如果价格先下跌，潜在连锁清算风险更集中，但这不代表价格一定会下跌。";
  }
  return "现价上下 5% 的两侧爆仓暴露接近，暂时没有明显单侧清算燃料优势。";
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
