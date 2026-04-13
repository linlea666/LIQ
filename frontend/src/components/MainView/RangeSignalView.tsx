"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";

export default function RangeSignalView() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const rs = data?.range_signal;

  if (!rs) {
    return (
      <div className="flex items-center justify-center h-64 text-slate-500">
        等待箱体信号数据...
      </div>
    );
  }

  const hasBox =
    rs.range_upper != null &&
    rs.range_lower != null &&
    rs.range_upper > rs.range_lower;
  const boxWidth = hasBox ? rs.range_upper! - rs.range_lower! : 0;
  const price = data?.ticker?.last ?? 0;

  return (
    <div className="space-y-4 max-w-4xl">
      {/* Plain Language Summary — 小白一眼看懂 */}
      <PlainSummary rs={rs} price={price} coin={coin} hasBox={hasBox} />

      {/* Signal Grade Hero */}
      {rs.signal_grade ? (
        <SignalHero
          grade={rs.signal_grade}
          direction={rs.signal_direction}
          reason={rs.signal_reason}
          sweepConfirmed={rs.sweep_confirmed}
          cpsAligned={rs.cps_aligned}
        />
      ) : (
        <Card>
          <div className="flex items-center gap-3">
            <span className="text-2xl">⏸️</span>
            <div>
              <div className="text-sm font-bold text-slate-400">当前无箱体信号</div>
              <div className="text-xs text-slate-600">
                价格处于箱体中间区域，或数据不足以生成信号
              </div>
            </div>
          </div>
        </Card>
      )}

      {/* Box Range Visualization */}
      {hasBox && (
        <Card>
          <h3 className="text-sm font-bold text-white mb-3 flex items-center gap-2">
            <span className="text-base">📦</span>箱体区间
          </h3>
          <BoxVisualization
            upper={rs.range_upper!}
            lower={rs.range_lower!}
            upperSource={rs.range_upper_source}
            lowerSource={rs.range_lower_source}
            price={price}
            positionPct={rs.price_position_pct}
            positionLabel={rs.price_position}
            coin={coin}
          />
        </Card>
      )}

      {/* MA + MACD Grid */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <h3 className="text-sm font-bold text-white mb-3 flex items-center gap-2">
            <span className="text-base">📈</span>均线状态
          </h3>
          <div className="space-y-2">
            <MARow label="MA60 (日线)" value={rs.ma60_daily} coin={coin} price={price} />
            <MARow label="MA120 (日线)" value={rs.ma120_daily} coin={coin} price={price} />
            <MARow label="MA60 (周线)" value={rs.ma60_weekly} coin={coin} price={price} />
          </div>
          {hasBox && (
            <div className="mt-3 pt-3 border-t border-slate-700/50 text-xs text-slate-500">
              箱体宽度: {formatPrice(boxWidth, coin)} ({((boxWidth / rs.range_lower!) * 100).toFixed(1)}%)
            </div>
          )}
        </Card>

        <Card>
          <h3 className="text-sm font-bold text-white mb-3 flex items-center gap-2">
            <span className="text-base">📊</span>MACD 状态 (日线)
          </h3>
          {rs.macd_daily_above_zero != null ? (
            <div className="space-y-3">
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-400">零轴位置</span>
                <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                  rs.macd_daily_above_zero
                    ? "bg-green-900/50 text-green-400"
                    : "bg-red-900/50 text-red-400"
                }`}>
                  {rs.macd_daily_above_zero ? "零轴上方 (多头区)" : "零轴下方 (空头区)"}
                </span>
              </div>
              <div className="flex items-center justify-between">
                <span className="text-xs text-slate-400">柱状图方向</span>
                <span className={`text-xs font-medium ${
                  rs.macd_daily_hist_rising ? "text-green-400" : "text-red-400"
                }`}>
                  {rs.macd_daily_hist_rising ? "▲ 上升（动能增强）" : "▼ 下降（动能减弱）"}
                </span>
              </div>
              {rs.macd_daily_histogram != null && (
                <div className="flex items-center justify-between">
                  <span className="text-xs text-slate-400">柱状图值</span>
                  <span className={`text-xs font-mono ${
                    rs.macd_daily_histogram >= 0 ? "text-green-400" : "text-red-400"
                  }`}>
                    {rs.macd_daily_histogram >= 0 ? "+" : ""}{rs.macd_daily_histogram.toFixed(2)}
                  </span>
                </div>
              )}
              <MACDBias aboveZero={rs.macd_daily_above_zero} histRising={rs.macd_daily_hist_rising} />
            </div>
          ) : (
            <div className="text-xs text-slate-600">MACD 数据不足</div>
          )}
        </Card>
      </div>

      {/* Unfilled Wicks */}
      {(rs.unfilled_wick_low != null || rs.unfilled_wick_high != null) && (
        <Card>
          <h3 className="text-sm font-bold text-white mb-3 flex items-center gap-2">
            <span className="text-base">🕯️</span>未填补影线
          </h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {rs.unfilled_wick_low != null && (
              <WickCard
                label="下影线缺口"
                price={rs.unfilled_wick_low}
                coin={coin}
                currentPrice={price}
                direction="below"
              />
            )}
            {rs.unfilled_wick_high != null && (
              <WickCard
                label="上影线缺口"
                price={rs.unfilled_wick_high}
                coin={coin}
                currentPrice={price}
                direction="above"
              />
            )}
          </div>
        </Card>
      )}

      {/* Confluence Summary */}
      <Card>
        <h3 className="text-sm font-bold text-white mb-3 flex items-center gap-2">
          <span className="text-base">🔗</span>共振因子
        </h3>
        <div className="flex flex-wrap gap-2">
          <ConfluenceTag
            label="流动性清扫确认"
            active={rs.sweep_confirmed}
            activeColor="text-yellow-400 bg-yellow-900/30 border-yellow-700/50"
          />
          <ConfluenceTag
            label="CPS 周期对齐"
            active={rs.cps_aligned}
            activeColor="text-blue-400 bg-blue-900/30 border-blue-700/50"
          />
          <ConfluenceTag
            label="MACD 方向一致"
            active={
              rs.signal_direction === "long"
                ? rs.macd_daily_above_zero === true
                : rs.signal_direction === "short"
                ? rs.macd_daily_above_zero === false
                : false
            }
            activeColor="text-green-400 bg-green-900/30 border-green-700/50"
          />
          <ConfluenceTag
            label="箱体已形成"
            active={hasBox}
            activeColor="text-purple-400 bg-purple-900/30 border-purple-700/50"
          />
        </div>
        {rs.signal_grade && (
          <div className="mt-3 pt-3 border-t border-slate-700/50 text-xs text-slate-500">
            共振数: {[
              rs.sweep_confirmed,
              rs.cps_aligned,
              rs.signal_direction === "long" ? rs.macd_daily_above_zero === true : rs.signal_direction === "short" ? rs.macd_daily_above_zero === false : false,
              hasBox,
            ].filter(Boolean).length} / 4
          </div>
        )}
      </Card>
    </div>
  );
}

/* ── Sub-components ── */

function Card({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-4">
      {children}
    </div>
  );
}

import type { RangeSignalData } from "@/lib/types";

function PlainSummary({
  rs, price, coin, hasBox,
}: {
  rs: RangeSignalData;
  price: number;
  coin: string;
  hasBox: boolean;
}) {
  const lines: string[] = [];
  let emoji = "🔍";
  let headlineColor = "text-slate-300";

  if (!hasBox) {
    emoji = "⏳";
    lines.push("箱体还没形成，暂时没有可操作的信号，先观望。");
  } else if (rs.signal_grade === "A" && rs.signal_direction === "long") {
    emoji = "🟢";
    headlineColor = "text-green-400";
    lines.push(`价格已经跌到箱体底部附近（${formatPrice(rs.range_lower!, coin)}），而且下方的杠杆仓位已经被扫掉了——说明"最后一拨止损"已经触发，空头弹药耗尽。`);
    lines.push("这是高质量的做多机会（A级），适合在这附近挂单接货。");
    if (rs.cps_aligned) lines.push("大周期也处于震荡区，箱体策略此时最有效。");
  } else if (rs.signal_grade === "A" && rs.signal_direction === "short") {
    emoji = "🔴";
    headlineColor = "text-red-400";
    lines.push(`价格反弹到了箱体顶部附近（${formatPrice(rs.range_upper!, coin)}），但日线MACD在零轴下方——说明整体趋势偏弱，反弹到均线压力位就是做空的好时机。`);
    lines.push("这是高质量的做空机会（A级），适合在阻力位挂空单。");
    if (rs.cps_aligned) lines.push("大周期也处于震荡区，箱体策略此时最有效。");
  } else if (rs.signal_grade === "B" && rs.signal_direction === "long") {
    emoji = "🟡";
    headlineColor = "text-yellow-400";
    lines.push(`价格在箱体底部附近（${formatPrice(rs.range_lower!, coin)}），有支撑的迹象。`);
    lines.push("但还没看到下方流动性被清扫的确认信号——建议等一等，等「扫完再接」会更安全。现在做多风险偏高。");
  } else if (rs.signal_grade === "B" && rs.signal_direction === "short") {
    emoji = "🟡";
    headlineColor = "text-yellow-400";
    lines.push(`价格在箱体顶部附近（${formatPrice(rs.range_upper!, coin)}），有阻力的迹象。`);
    lines.push("但MACD还在零轴上方（多头趋势中），逆势做空风险大——建议轻仓或等MACD翻到零轴下方再加仓。");
  } else {
    lines.push(`价格在箱体中间区域（上沿 ${hasBox ? formatPrice(rs.range_upper!, coin) : "?"} / 下沿 ${hasBox ? formatPrice(rs.range_lower!, coin) : "?"}），既不靠近顶也不靠近底。`);
    lines.push("中间地带没有明确方向，最好的策略是：什么都不做，耐心等价格走到边界再出手。");
    emoji = "⏸️";
  }

  // Wick gap plain language
  if (rs.unfilled_wick_low != null) {
    const dist = price > 0 ? ((rs.unfilled_wick_low - price) / price * 100).toFixed(1) : "?";
    lines.push(`下方 ${formatPrice(rs.unfilled_wick_low, coin)}（距当前${dist}%）有一根没被填补的下影线——价格像磁铁一样容易被吸引回去。`);
  }
  if (rs.unfilled_wick_high != null) {
    const dist = price > 0 ? ((rs.unfilled_wick_high - price) / price * 100).toFixed(1) : "?";
    lines.push(`上方 ${formatPrice(rs.unfilled_wick_high, coin)}（距当前+${dist}%）有一根没被填补的上影线——价格可能会被吸引上去。`);
  }

  const headline =
    rs.signal_grade === "A"
      ? `${rs.signal_direction === "long" ? "做多" : "做空"}好时机！(A级高确信)`
      : rs.signal_grade === "B"
      ? `${rs.signal_direction === "long" ? "做多" : "做空"}机会初现，但需确认 (B级)`
      : hasBox
      ? "观望中 — 等价格到箱体边界再动手"
      : "数据积累中 — 暂无操作建议";

  return (
    <Card>
      <div className="flex gap-3">
        <span className="text-3xl shrink-0">{emoji}</span>
        <div className="min-w-0">
          <h3 className={`text-base font-bold mb-2 ${headlineColor}`}>
            {headline}
          </h3>
          <div className="space-y-1.5">
            {lines.map((line, i) => (
              <p key={i} className="text-sm text-slate-400 leading-relaxed">{line}</p>
            ))}
          </div>
        </div>
      </div>
    </Card>
  );
}

function SignalHero({
  grade, direction, reason, sweepConfirmed, cpsAligned,
}: {
  grade: string;
  direction: string | null;
  reason: string;
  sweepConfirmed: boolean;
  cpsAligned: boolean;
}) {
  const isLong = direction === "long";
  const gradeColor = grade === "A"
    ? "from-yellow-500/20 to-yellow-600/5 border-yellow-600/40"
    : "from-blue-500/15 to-blue-600/5 border-blue-600/30";
  const gradeBadge = grade === "A"
    ? "bg-yellow-500 text-black"
    : "bg-blue-500 text-white";
  const dirColor = isLong ? "text-green-400" : "text-red-400";
  const dirLabel = isLong ? "做多" : direction === "short" ? "做空" : "—";
  const dirIcon = isLong ? "🟢" : "🔴";

  return (
    <Card>
      <div className={`-m-4 p-4 rounded-xl bg-gradient-to-r ${gradeColor} border`}>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <span className={`text-lg font-black px-3 py-1 rounded-lg ${gradeBadge}`}>
              {grade}
            </span>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm">{dirIcon}</span>
                <span className={`text-lg font-bold ${dirColor}`}>{dirLabel}信号</span>
              </div>
              <div className="text-xs text-slate-400 mt-0.5 max-w-md">{reason}</div>
            </div>
          </div>
          <div className="flex gap-2">
            {sweepConfirmed && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-yellow-900/40 text-yellow-400 border border-yellow-700/50">
                Sweep ✓
              </span>
            )}
            {cpsAligned && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-blue-900/40 text-blue-400 border border-blue-700/50">
                CPS ✓
              </span>
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}

function BoxVisualization({
  upper, lower, upperSource, lowerSource, price, positionPct, positionLabel, coin,
}: {
  upper: number;
  lower: number;
  upperSource: string;
  lowerSource: string;
  price: number;
  positionPct: number;
  positionLabel: string;
  coin: string;
}) {
  const clampedPct = Math.max(0, Math.min(100, positionPct));
  const positionColor =
    positionLabel === "near_upper" ? "text-red-400" :
    positionLabel === "near_lower" ? "text-green-400" :
    "text-yellow-400";
  const positionText =
    positionLabel === "near_upper" ? "接近上沿 (阻力区)" :
    positionLabel === "near_lower" ? "接近下沿 (支撑区)" :
    "中间区域";

  return (
    <div className="space-y-3">
      {/* Upper bound */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-red-400 text-xs font-bold">▲ 上沿</span>
          <span className="text-white font-medium text-sm">{formatPrice(upper, coin)}</span>
        </div>
        <span className="text-[10px] text-slate-600">{upperSource}</span>
      </div>

      {/* Progress bar */}
      <div className="relative">
        <div className="h-8 bg-slate-800 rounded-lg overflow-hidden border border-slate-700/50">
          <div className="absolute inset-0 rounded-lg opacity-20"
            style={{
              background: "linear-gradient(to right, #22c55e, #eab308, #ef4444)",
            }}
          />
          {/* Price marker */}
          <div
            className="absolute top-0 h-full w-0.5 bg-white z-10"
            style={{ left: `${clampedPct}%` }}
          >
            <div className="absolute -top-5 left-1/2 -translate-x-1/2 whitespace-nowrap text-[10px] text-white font-bold bg-slate-800 px-1.5 py-0.5 rounded border border-slate-600">
              {formatPrice(price, coin)}
            </div>
          </div>
          {/* Zone labels */}
          <div className="absolute left-2 top-1/2 -translate-y-1/2 text-[9px] text-green-500/60 font-medium">
            支撑区
          </div>
          <div className="absolute right-2 top-1/2 -translate-y-1/2 text-[9px] text-red-500/60 font-medium">
            阻力区
          </div>
        </div>
      </div>

      {/* Lower bound */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-green-400 text-xs font-bold">▼ 下沿</span>
          <span className="text-white font-medium text-sm">{formatPrice(lower, coin)}</span>
        </div>
        <span className="text-[10px] text-slate-600">{lowerSource}</span>
      </div>

      {/* Position summary */}
      <div className="flex items-center justify-between pt-2 border-t border-slate-700/50">
        <span className="text-xs text-slate-500">当前位置</span>
        <span className={`text-xs font-bold ${positionColor}`}>
          {positionText} ({clampedPct.toFixed(0)}%)
        </span>
      </div>
    </div>
  );
}

function MARow({ label, value, coin, price }: { label: string; value: number | null; coin: string; price: number }) {
  const isAbove = price > 0 && value != null && price > value;
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-slate-800 last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      {value != null ? (
        <div className="flex items-center gap-2">
          <span className="text-xs text-white font-medium">{formatPrice(value, coin)}</span>
          <span className={`text-[10px] px-1.5 py-0.5 rounded ${
            isAbove
              ? "bg-green-900/30 text-green-400"
              : "bg-red-900/30 text-red-400"
          }`}>
            价格{isAbove ? "上方" : "下方"}
          </span>
        </div>
      ) : (
        <span className="text-xs text-slate-600">—</span>
      )}
    </div>
  );
}

function MACDBias({ aboveZero, histRising }: { aboveZero: boolean | null; histRising: boolean | null }) {
  if (aboveZero == null) return null;

  let bias: string;
  let color: string;
  if (aboveZero && histRising) {
    bias = "强势多头：零轴上方 + 动能增强";
    color = "text-green-400 bg-green-900/30";
  } else if (aboveZero && !histRising) {
    bias = "多头衰减：零轴上方 + 动能转弱";
    color = "text-yellow-400 bg-yellow-900/30";
  } else if (!aboveZero && !histRising) {
    bias = "强势空头：零轴下方 + 动能增强";
    color = "text-red-400 bg-red-900/30";
  } else {
    bias = "空头衰减：零轴下方 + 动能转弱";
    color = "text-orange-400 bg-orange-900/30";
  }

  return (
    <div className={`mt-2 text-xs px-3 py-2 rounded-lg ${color}`}>
      {bias}
    </div>
  );
}

function WickCard({
  label, price, coin, currentPrice, direction,
}: {
  label: string;
  price: number;
  coin: string;
  currentPrice: number;
  direction: "above" | "below";
}) {
  const dist = currentPrice > 0
    ? ((price - currentPrice) / currentPrice * 100).toFixed(2)
    : "—";
  const icon = direction === "below" ? "⬇️" : "⬆️";

  return (
    <div className="bg-slate-800 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-sm">{icon}</span>
        <span className="text-xs text-slate-400">{label}</span>
      </div>
      <div className="text-white font-medium text-sm">{formatPrice(price, coin)}</div>
      <div className="text-[10px] text-slate-500 mt-1">
        距当前价 {dist}%
      </div>
    </div>
  );
}

function ConfluenceTag({
  label, active, activeColor,
}: {
  label: string;
  active: boolean;
  activeColor: string;
}) {
  return (
    <span className={`text-xs px-3 py-1.5 rounded-lg border transition-all ${
      active
        ? activeColor
        : "text-slate-600 bg-slate-800/50 border-slate-700/30"
    }`}>
      {active ? "✓ " : "✗ "}{label}
    </span>
  );
}
