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

type TopPosition = {
  address: string;
  side: PositionSide;
  position_size: number;
  notional_usd: number;
  entry_price?: number | null;
  liq_price?: number | null;
  leverage: number;
  margin_mode?: string | null;
  unrealized_pnl?: number | null;
  distance_to_liq_pct?: number | null;
  update_time_ts?: number | null;
};

type LiquidationPin = {
  key: string;
  rank: number;
  side: PositionSide;
  price: number;
  distancePct: number;
  label: string;
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
  top_longs?: TopPosition[];
  top_shorts?: TopPosition[];
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
  const insights = asset?.mark_price == null ? null : deriveWhaleInsights(asset, rangePct);
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

          {insights && <BeginnerSummary asset={asset} insights={insights} rangePct={rangePct} />}

          {!asset.quality.valid && (
            <div className="mt-3 rounded-lg border border-amber-800/60 bg-amber-950/25 px-3 py-2 text-xs text-amber-300">
              当前展示的是最后一次可用快照：{qualityText(asset.quality)}。过期数据只供回看，不应用于判断当前价格风险。
            </div>
          )}

          <div className="mt-4 rounded-lg border border-blue-900/50 bg-blue-950/20 px-3 py-2 text-xs leading-5 text-blue-200">
            <span className="font-medium">读图规则：</span>图上方是价格上涨方向，图下方是价格下跌方向。仓位方向由上游仓位数量正负决定，不由开仓价在现价上方或下方决定；绿色只表示多头仓位，不等于支撑，红色只表示空头仓位，不等于阻力。
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
              note="交叉保证金价格会随全账户权益动态变化；◆钉点为 Top 头寸清算价"
              buckets={asset.liquidation_buckets}
              markPrice={asset.mark_price}
              rangePct={rangePct}
              binSizePct={asset.bin_size_pct}
              maxNotional={sharedMaxNotional}
              pins={buildLiquidationPins(asset)}
            />
          </div>

          <TopPositionsCard asset={asset} />

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

function BeginnerSummary({ asset, insights, rangePct }: { asset: AssetDistribution; insights: WhaleInsights; rangePct: RangePct }) {
  const nearMax = Math.max(1, insights.upperShortNear5Usd, insights.lowerLongNear5Usd);
  const longEntryState = entryPriceState(insights.longEntry, asset.mark_price);
  const shortEntryState = entryPriceState(insights.shortEntry, asset.mark_price);
  const balanceText = nearRiskSentence(
    insights.upperShortNear5Usd,
    insights.lowerLongNear5Usd,
  );
  return (
    <div className="mt-4 rounded-xl border border-violet-800/50 bg-violet-950/15 p-3 md:p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h3 className="text-sm font-semibold text-violet-100">新手先看这 4 个位置</h3>
          <p className="mt-1 text-[10px] text-slate-500">从现价 ±{rangePct}% 当前所选范围提取：先看成本在哪里，再看上涨和下跌分别可能触发哪一侧爆仓；这些位置不是交易指令。</p>
        </div>
        <div className="rounded bg-slate-950/70 px-2 py-1 text-xs text-violet-200">
          Hyperliquid 标记价 {asset.mark_price == null ? "—" : money(asset.mark_price)}
        </div>
      </div>
      <div className="mt-3 grid gap-2 sm:grid-cols-2 xl:grid-cols-4">
        <InsightCard
          label={`多头金额最厚开仓区${longEntryState ? `（${longEntryState.label}）` : ""}`}
          insight={insights.longEntry}
          markPrice={asset.mark_price}
          tone="emerald"
          empty="暂无有效多头开仓价"
          help={longEntryState?.sentence}
        />
        <InsightCard
          label={`空头金额最厚开仓区${shortEntryState ? `（${shortEntryState.label}）` : ""}`}
          insight={insights.shortEntry}
          markPrice={asset.mark_price}
          tone="rose"
          empty="暂无有效空头开仓价"
          help={shortEntryState?.sentence}
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
        距现价 {distanceText(insight.bucket.price_mid, markPrice)} · {compactUsd(insight.notionalUsd)} · {insight.count} 个仓位{insight.avgLeverage > 0 ? ` · 均杠杆 ${insight.avgLeverage.toFixed(1)}x` : ""}
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
  pins,
}: {
  kind: LadderKind;
  title: string;
  note: string;
  buckets: PriceBucket[];
  markPrice: number;
  rangePct: RangePct;
  binSizePct: number;
  maxNotional: number;
  pins?: LiquidationPin[];
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
        {(pins ?? []).filter((pin) => Math.abs(pin.distancePct) <= rangePct).map((pin) => {
          const y = yForDistance(pin.distancePct);
          const x = pin.side === "long" ? center - 10 : center + 10;
          const color = pin.side === "long" ? "#34d399" : "#fb7185";
          return (
            <g key={pin.key}>
              <title>{pin.label}</title>
              <path
                d={`M ${x} ${y - 6} L ${x + 5} ${y} L ${x} ${y + 6} L ${x - 5} ${y} Z`}
                fill={color}
                stroke="#0f172a"
                strokeWidth="1"
              />
              <text x={pin.side === "long" ? x - 9 : x + 9} y={y + 3.5} fill={color} fontSize="10" textAnchor={pin.side === "long" ? "end" : "start"}>#{pin.rank}</text>
            </g>
          );
        })}
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

const PIN_LIMIT = 5;
const HIGH_RISK_LIQ_DISTANCE_PCT = 3;

function buildLiquidationPins(asset: AssetDistribution): LiquidationPin[] {
  const markPrice = asset.mark_price;
  if (markPrice == null || markPrice <= 0) return [];
  const pins: LiquidationPin[] = [];
  for (const side of ["long", "short"] as const) {
    const list = (side === "long" ? asset.top_longs : asset.top_shorts) ?? [];
    list.slice(0, PIN_LIMIT).forEach((position, index) => {
      if (position.liq_price == null || position.liq_price <= 0) return;
      const distancePct = position.distance_to_liq_pct
        ?? (position.liq_price / markPrice - 1) * 100;
      pins.push({
        key: `${side}-${position.address}-${position.liq_price}`,
        rank: index + 1,
        side,
        price: position.liq_price,
        distancePct,
        label: `${side === "long" ? "多头" : "空头"}#${index + 1} ${shortAddress(position.address)} · 清算价 ${money(position.liq_price)}（距现价 ${distancePct >= 0 ? "+" : ""}${distancePct.toFixed(2)}%）· 仓位 ${compactUsd(position.notional_usd)} · ${position.leverage.toFixed(0)}x`,
      });
    });
  }
  return pins;
}

function shortAddress(address: string) {
  return address.length > 12 ? `${address.slice(0, 6)}…${address.slice(-4)}` : address;
}

function TopPositionsCard({ asset }: { asset: AssetDistribution }) {
  const longs = asset.top_longs ?? [];
  const shorts = asset.top_shorts ?? [];
  if (!longs.length && !shorts.length) return null;
  return (
    <div className="mt-4 rounded-xl border border-slate-800 bg-slate-950/55 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <div className="text-sm font-medium text-slate-200">巨鲸 Top 头寸明细（按仓位价值）</div>
          <div className="text-[10px] text-slate-600">地址为链上公开数据，点击地址可复制 · 距清算 ≤{HIGH_RISK_LIQ_DISTANCE_PCT}% 标记为高危 · 交叉保证金清算价随账户权益动态变化</div>
        </div>
      </div>
      <div className="mt-3 grid gap-4 xl:grid-cols-2">
        <TopPositionsTable title={`多头 Top ${longs.length}`} positions={longs} symbol={asset.symbol} tone="text-emerald-400" />
        <TopPositionsTable title={`空头 Top ${shorts.length}`} positions={shorts} symbol={asset.symbol} tone="text-rose-400" />
      </div>
    </div>
  );
}

function TopPositionsTable({ title, positions, symbol, tone: toneClass }: { title: string; positions: TopPosition[]; symbol: SymbolName; tone: string }) {
  if (!positions.length) {
    return <div className="rounded-lg border border-slate-800 p-3 text-xs text-slate-600">该方向暂无巨鲸头寸</div>;
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full min-w-[560px] text-[11px]">
        <thead className="bg-slate-950 text-slate-500">
          <tr>
            <th className={`p-2 text-left font-medium ${toneClass}`}>{title}</th>
            <th className="p-2 text-right font-normal">数量</th>
            <th className="p-2 text-right font-normal">仓位价值</th>
            <th className="p-2 text-right font-normal">开仓价</th>
            <th className="p-2 text-right font-normal">清算价</th>
            <th className="p-2 text-right font-normal">距清算</th>
            <th className="p-2 text-right font-normal">杠杆</th>
            <th className="p-2 text-right font-normal">浮盈亏</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((position, index) => {
            const highRisk = position.distance_to_liq_pct != null
              && Math.abs(position.distance_to_liq_pct) <= HIGH_RISK_LIQ_DISTANCE_PCT;
            return (
              <tr key={`${position.address}-${index}`} className={`border-t border-slate-800 ${highRisk ? "bg-amber-950/30" : ""}`}>
                <td className="max-w-[190px] p-2">
                  <div className="flex items-center gap-1.5">
                    <span className="text-slate-600">#{index + 1}</span>
                    <AddressCell address={position.address} />
                    {highRisk && <span className="rounded bg-amber-900/70 px-1 text-[9px] text-amber-300">高危</span>}
                  </div>
                </td>
                <td className="p-2 text-right text-slate-300">{position.position_size.toLocaleString(undefined, { maximumFractionDigits: 2 })} {symbol}</td>
                <td className="p-2 text-right text-slate-200">{compactUsd(position.notional_usd)}</td>
                <td className="p-2 text-right text-slate-300">{position.entry_price == null ? "—" : money(position.entry_price)}</td>
                <td className={`p-2 text-right ${highRisk ? "font-semibold text-amber-300" : "text-slate-300"}`}>{position.liq_price == null ? "—" : money(position.liq_price)}</td>
                <td className={`p-2 text-right ${highRisk ? "font-semibold text-amber-300" : "text-slate-400"}`}>{position.distance_to_liq_pct == null ? "—" : `${position.distance_to_liq_pct >= 0 ? "+" : ""}${position.distance_to_liq_pct.toFixed(2)}%`}</td>
                <td className="p-2 text-right text-slate-300">{position.leverage > 0 ? `${position.leverage.toFixed(0)}x` : "—"}{position.margin_mode ? <span className="ml-1 text-[9px] text-slate-600">{position.margin_mode === "cross" ? "全仓" : position.margin_mode === "isolated" ? "逐仓" : position.margin_mode}</span> : null}</td>
                <td className={`p-2 text-right ${position.unrealized_pnl == null ? "text-slate-500" : position.unrealized_pnl >= 0 ? "text-emerald-400" : "text-rose-400"}`}>{position.unrealized_pnl == null ? "—" : `${position.unrealized_pnl >= 0 ? "+" : "-"}${compactUsd(Math.abs(position.unrealized_pnl))}`}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function AddressCell({ address }: { address: string }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(address);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      // 剪贴板不可用（如非 HTTPS 环境）时静默忽略
    }
  };
  return (
    <button
      type="button"
      onClick={copy}
      title={`${address}（点击复制完整地址）`}
      className="break-all text-left font-mono text-[10px] text-blue-300 hover:text-blue-200"
    >
      {copied ? "已复制 ✓" : address}
    </button>
  );
}

function deriveWhaleInsights(asset: AssetDistribution, rangePct: RangePct): WhaleInsights {
  const markPrice = asset.mark_price ?? 0;
  const inBeginnerRange = (bucket: PriceBucket) => (
    Math.abs(bucket.distance_from_mark_pct) <= rangePct
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

function entryPriceState(insight: SideBucketInsight | null, markPrice?: number | null) {
  if (!insight || !markPrice || markPrice <= 0) return null;
  const entryPrice = insight.bucket.price_mid;
  const priceMoveFromEntryPct = (markPrice / entryPrice - 1) * 100;
  const nearCost = Math.abs(priceMoveFromEntryPct) < 0.1;
  if (nearCost) {
    return {
      label: "接近成本",
      sentence: "该价格带开仓成本与当前标记价接近；仅按价格比较，未计资金费。",
    };
  }
  const profitable = insight.side === "long" ? markPrice > entryPrice : markPrice < entryPrice;
  const directionText = insight.side === "long"
    ? (profitable ? "现价高于多头成本" : "现价低于多头成本")
    : (profitable ? "现价低于空头成本" : "现价高于空头成本");
  return {
    label: profitable ? "价格浮盈" : "价格浮亏",
    sentence: `${directionText}约 ${Math.abs(priceMoveFromEntryPct).toFixed(2)}%，该价格带仓位处于价格${profitable ? "浮盈" : "浮亏"}区；仅按价格比较，未计资金费。`,
  };
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
