"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";
import type { RangeSignalData } from "@/lib/types";
import Link from "next/link";

const BOX_STATE_MAP: Record<string, { label: string; color: string }> = {
  none: { label: "未形成", color: "text-slate-500" },
  forming: { label: "形成中", color: "text-blue-400" },
  confirmed: { label: "已确认", color: "text-cyan-400" },
  mature: { label: "成熟", color: "text-green-400" },
  squeeze: { label: "挤压蓄力", color: "text-amber-400" },
  breaking_up: { label: "向上突破中", color: "text-green-300" },
  breaking_down: { label: "向下突破中", color: "text-red-300" },
  broken: { label: "已突破", color: "text-slate-400" },
};

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
  const price = data?.ticker?.last ?? 0;

  return (
    <div className="space-y-4 max-w-4xl">
      <PlainSummary rs={rs} price={price} coin={coin} hasBox={hasBox} />

      {rs.signal_grade ? (
        <SignalHero rs={rs} coin={coin} />
      ) : (
        <Card>
          <div className="flex items-center gap-3">
            <span className="text-2xl">⏸️</span>
            <div>
              <div className="text-sm font-bold text-slate-400">当前无箱体信号</div>
              <div className="text-xs text-slate-600">
                价格处于箱体中间区域，或箱体尚未形成
              </div>
            </div>
          </div>
        </Card>
      )}

      {hasBox && (
        <Card>
          <h3 className="text-sm font-bold text-white mb-3 flex items-center gap-2">
            📦 箱体区间
          </h3>
          <BoxVisualization
            upper={rs.range_upper!}
            lower={rs.range_lower!}
            upperSource={rs.range_upper_source}
            lowerSource={rs.range_lower_source}
            upperTier={rs.range_upper_tier}
            lowerTier={rs.range_lower_tier}
            price={price}
            positionPct={rs.price_position_pct}
            positionLabel={rs.price_position}
            coin={coin}
          />
        </Card>
      )}

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <BoxStateCard rs={rs} />
        <BreakoutCard rs={rs} />
      </div>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <Card>
          <h3 className="text-sm font-bold text-white mb-3">📈 均线状态</h3>
          <div className="space-y-2">
            <MARow label="MA60 (日线)" value={rs.ma60_daily} coin={coin} price={price} />
            <MARow label="MA120 (日线)" value={rs.ma120_daily} coin={coin} price={price} />
            <MARow label="MA60 (周线)" value={rs.ma60_weekly} coin={coin} price={price} />
          </div>
          {hasBox && (
            <div className="mt-3 pt-3 border-t border-slate-700/50 text-xs text-slate-500">
              箱体宽度: {rs.box_width_pct.toFixed(1)}%
            </div>
          )}
        </Card>

        <Card>
          <h3 className="text-sm font-bold text-white mb-3">📊 MACD 状态 (日线)</h3>
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

      {(rs.unfilled_wick_low != null || rs.unfilled_wick_high != null) && (
        <Card>
          <h3 className="text-sm font-bold text-white mb-3">🕯️ 未填补影线</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {rs.unfilled_wick_low != null && (
              <WickCard label="下影线缺口" price={rs.unfilled_wick_low} coin={coin} currentPrice={price} direction="below" />
            )}
            {rs.unfilled_wick_high != null && (
              <WickCard label="上影线缺口" price={rs.unfilled_wick_high} coin={coin} currentPrice={price} direction="above" />
            )}
          </div>
        </Card>
      )}

      <Card>
        <h3 className="text-sm font-bold text-white mb-3">🔗 共振因子</h3>
        <div className="flex flex-wrap gap-2">
          <ConfTag label="流动性清扫" active={rs.sweep_confirmed} color="text-yellow-400 bg-yellow-900/30 border-yellow-700/50" />
          <ConfTag label="CPS 周期对齐" active={rs.cps_aligned} color="text-blue-400 bg-blue-900/30 border-blue-700/50" />
          <ConfTag label="BB 挤压" active={rs.bb_squeeze} color="text-purple-400 bg-purple-900/30 border-purple-700/50" />
          <ConfTag label="OI 堆积" active={rs.oi_buildup} color="text-orange-400 bg-orange-900/30 border-orange-700/50" />
          <ConfTag label="成交量萎缩" active={rs.volume_declining} color="text-cyan-400 bg-cyan-900/30 border-cyan-700/50" />
          <ConfTag label="资金费率极端" active={rs.funding_extreme} color="text-red-400 bg-red-900/30 border-red-700/50" />
          <ConfTag
            label={`订单簿${rs.orderbook_imbalance === "bid_heavy" ? "买盘强" : rs.orderbook_imbalance === "ask_heavy" ? "卖盘强" : "均衡"}`}
            active={!!rs.orderbook_imbalance}
            color="text-green-400 bg-green-900/30 border-green-700/50"
          />
          <ConfTag label="箱体已形成" active={hasBox} color="text-indigo-400 bg-indigo-900/30 border-indigo-700/50" />
        </div>
        <div className="mt-3 pt-3 border-t border-slate-700/50 text-xs text-slate-500">
          共振数: {rs.confluence_count} / 8
        </div>
      </Card>

      <div className="text-center">
        <Link
          href={`/range/${coin}`}
          className="text-xs text-blue-400 hover:text-blue-300 underline underline-offset-4"
        >
          查看完整箱体分析 →
        </Link>
      </div>
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

function PlainSummary({ rs, price, coin, hasBox }: { rs: RangeSignalData; price: number; coin: string; hasBox: boolean }) {
  const lines: string[] = [];
  let emoji = "🔍";
  let headlineColor = "text-slate-300";
  const stateInfo = BOX_STATE_MAP[rs.box_state] || BOX_STATE_MAP.none;

  if (!hasBox || rs.box_state === "none") {
    emoji = "⏳";
    lines.push("箱体还没形成，暂时没有可操作的信号，先观望。");
  } else if (rs.box_state === "squeeze") {
    emoji = "⚡";
    headlineColor = "text-amber-400";
    lines.push(`箱体已进入挤压蓄力状态（${rs.box_age_hours.toFixed(0)}小时），波动率极度收窄，大行情即将来临。`);
    if (rs.breakout_direction_bias === "up") {
      lines.push("多方力量占优，突破大概率向上。");
    } else if (rs.breakout_direction_bias === "down") {
      lines.push("空方力量占优，突破大概率向下。");
    } else {
      lines.push("方向未明，等突破后追入比预判更安全。");
    }
  } else if (rs.signal_grade === "S" && rs.signal_direction === "long") {
    emoji = "🟢";
    headlineColor = "text-green-400";
    lines.push(`多重信号共振的做多黄金机会！价格在${rs.range_lower_tier}级箱体下沿（${formatPrice(rs.range_lower!, coin)}），流动性扫取确认 + MACD多头 + 箱体成熟。`);
  } else if (rs.signal_grade === "S" && rs.signal_direction === "short") {
    emoji = "🔴";
    headlineColor = "text-red-400";
    lines.push(`多重信号共振的做空黄金机会！价格在${rs.range_upper_tier}级箱体上沿（${formatPrice(rs.range_upper!, coin)}），MACD空头 + 流动性扫取确认 + 箱体成熟。`);
  } else if (rs.signal_grade === "A" && rs.signal_direction === "long") {
    emoji = "🟢";
    headlineColor = "text-green-400";
    lines.push(`价格到达箱体底部附近（${formatPrice(rs.range_lower!, coin)}），有高质量的做多机会。`);
  } else if (rs.signal_grade === "A" && rs.signal_direction === "short") {
    emoji = "🔴";
    headlineColor = "text-red-400";
    lines.push(`价格到达箱体顶部附近（${formatPrice(rs.range_upper!, coin)}），有高质量的做空机会。`);
  } else if (rs.signal_grade === "B") {
    emoji = "🟡";
    headlineColor = "text-yellow-400";
    const dir = rs.signal_direction === "long" ? "做多" : rs.signal_direction === "short" ? "做空" : "";
    lines.push(`${dir}机会初现，但部分确认条件缺失，建议等待更多共振信号。`);
  } else if (rs.box_state === "breaking_up" || rs.box_state === "breaking_down") {
    emoji = rs.box_state === "breaking_up" ? "🚀" : "💥";
    headlineColor = rs.box_state === "breaking_up" ? "text-green-400" : "text-red-400";
    lines.push(`箱体正在向${rs.box_state === "breaking_up" ? "上" : "下"}突破！关注突破确认，防范假突破。`);
  } else {
    lines.push(`价格在箱体中间区域（上沿 ${hasBox ? formatPrice(rs.range_upper!, coin) : "?"} / 下沿 ${hasBox ? formatPrice(rs.range_lower!, coin) : "?"}）。`);
    lines.push("中间地带没有明确方向，耐心等价格走到边界再出手。");
    emoji = "⏸️";
  }

  if (rs.unfilled_wick_low != null) {
    const dist = price > 0 ? ((rs.unfilled_wick_low - price) / price * 100).toFixed(1) : "?";
    lines.push(`下方 ${formatPrice(rs.unfilled_wick_low, coin)}（距当前${dist}%）有一根没被填补的下影线。`);
  }
  if (rs.unfilled_wick_high != null) {
    const dist = price > 0 ? ((rs.unfilled_wick_high - price) / price * 100).toFixed(1) : "?";
    lines.push(`上方 ${formatPrice(rs.unfilled_wick_high, coin)}（距当前+${dist}%）有一根没被填补的上影线。`);
  }

  const headline =
    rs.signal_grade === "S"
      ? `${rs.signal_direction === "long" ? "做多" : "做空"}黄金机会！(S级多重共振)`
      : rs.signal_grade === "A"
      ? `${rs.signal_direction === "long" ? "做多" : "做空"}好时机 (A级)`
      : rs.signal_grade === "B"
      ? `${rs.signal_direction === "long" ? "做多" : "做空"}机会初现 (B级)`
      : rs.box_state === "squeeze"
      ? "挤压蓄力中 — 大行情即将来临"
      : rs.box_state.startsWith("breaking")
      ? "突破进行中！"
      : hasBox
      ? `观望中 — 等价格到箱体边界 [${stateInfo.label}]`
      : "箱体未形成 — 暂无操作建议";

  return (
    <Card>
      <div className="flex gap-3">
        <span className="text-3xl shrink-0">{emoji}</span>
        <div className="min-w-0">
          <h3 className={`text-base font-bold mb-2 ${headlineColor}`}>{headline}</h3>
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

function SignalHero({ rs, coin }: { rs: RangeSignalData; coin: string }) {
  const isLong = rs.signal_direction === "long";
  const gradeColors: Record<string, string> = {
    S: "from-amber-500/20 to-amber-600/5 border-amber-500/50",
    A: "from-orange-500/20 to-orange-600/5 border-orange-600/40",
    B: "from-blue-500/15 to-blue-600/5 border-blue-600/30",
  };
  const gradeBadges: Record<string, string> = {
    S: "bg-amber-500 text-black",
    A: "bg-orange-500 text-white",
    B: "bg-blue-500 text-white",
  };
  const grade = rs.signal_grade || "B";

  return (
    <Card>
      <div className={`-m-4 p-4 rounded-xl bg-gradient-to-r ${gradeColors[grade] || gradeColors.B} border`}>
        <div className="flex items-center justify-between flex-wrap gap-2">
          <div className="flex items-center gap-3">
            <span className={`text-lg font-black px-3 py-1 rounded-lg ${gradeBadges[grade] || gradeBadges.B}`}>
              {grade}
            </span>
            <div>
              <div className="flex items-center gap-2">
                <span className="text-sm">{isLong ? "🟢" : "🔴"}</span>
                <span className={`text-lg font-bold ${isLong ? "text-green-400" : "text-red-400"}`}>
                  {isLong ? "做多" : "做空"}信号
                </span>
              </div>
              <div className="text-xs text-slate-400 mt-0.5 max-w-lg">{rs.signal_reason}</div>
            </div>
          </div>
          <div className="flex flex-wrap gap-1.5">
            {rs.signal_entry != null && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-slate-700/60 text-slate-300">
                入场 {formatPrice(rs.signal_entry, coin)}
              </span>
            )}
            {rs.signal_stop_loss != null && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-red-900/40 text-red-400">
                止损 {formatPrice(rs.signal_stop_loss, coin)}
              </span>
            )}
            {rs.signal_tp1 != null && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-green-900/40 text-green-400">
                目标 {formatPrice(rs.signal_tp1, coin)}
              </span>
            )}
            {rs.signal_rr_ratio != null && rs.signal_rr_ratio > 0 && (
              <span className="text-[10px] px-2 py-0.5 rounded bg-blue-900/40 text-blue-400">
                R:R {rs.signal_rr_ratio.toFixed(1)}
              </span>
            )}
          </div>
        </div>
      </div>
    </Card>
  );
}

function BoxStateCard({ rs }: { rs: RangeSignalData }) {
  const info = BOX_STATE_MAP[rs.box_state] || BOX_STATE_MAP.none;

  return (
    <Card>
      <h3 className="text-sm font-bold text-white mb-3">🏗️ 箱体状态</h3>
      <div className="space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">当前状态</span>
          <span className={`text-xs font-bold px-2 py-0.5 rounded ${info.color} bg-slate-800`}>
            {info.label}
          </span>
        </div>
        {rs.box_age_hours > 0 && (
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">存续时长</span>
            <span className="text-xs text-white">
              {rs.box_age_hours >= 24
                ? `${(rs.box_age_hours / 24).toFixed(1)} 天`
                : `${rs.box_age_hours.toFixed(0)} 小时`}
            </span>
          </div>
        )}
        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">箱体质量</span>
          <div className="flex items-center gap-2">
            <div className="w-16 h-1.5 bg-slate-700 rounded-full overflow-hidden">
              <div
                className={`h-full rounded-full ${
                  rs.box_quality >= 70 ? "bg-green-500" : rs.box_quality >= 40 ? "bg-yellow-500" : "bg-red-500"
                }`}
                style={{ width: `${rs.box_quality}%` }}
              />
            </div>
            <span className="text-xs text-slate-300">{rs.box_quality}</span>
          </div>
        </div>
        {rs.range_upper_test_count > 0 && (
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">上沿测试次数</span>
            <span className="text-xs text-white">{rs.range_upper_test_count}</span>
          </div>
        )}
        {rs.range_lower_test_count > 0 && (
          <div className="flex items-center justify-between">
            <span className="text-xs text-slate-400">下沿测试次数</span>
            <span className="text-xs text-white">{rs.range_lower_test_count}</span>
          </div>
        )}
      </div>
    </Card>
  );
}

function BreakoutCard({ rs }: { rs: RangeSignalData }) {
  const prob = rs.breakout_probability;
  const biasLabel = rs.breakout_direction_bias === "up" ? "偏向上破" : rs.breakout_direction_bias === "down" ? "偏向下破" : "方向未明";
  const biasColor = rs.breakout_direction_bias === "up" ? "text-green-400" : rs.breakout_direction_bias === "down" ? "text-red-400" : "text-slate-400";

  return (
    <Card>
      <h3 className="text-sm font-bold text-white mb-3">💥 突破概率</h3>
      <div className="space-y-2.5">
        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">突破概率</span>
          <span className={`text-sm font-bold ${
            prob >= 0.6 ? "text-red-400" : prob >= 0.3 ? "text-yellow-400" : "text-green-400"
          }`}>
            {(prob * 100).toFixed(0)}%
          </span>
        </div>
        <div className="w-full h-2 bg-slate-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all ${
              prob >= 0.6 ? "bg-red-500" : prob >= 0.3 ? "bg-yellow-500" : "bg-green-500"
            }`}
            style={{ width: `${prob * 100}%` }}
          />
        </div>
        <div className="flex items-center justify-between">
          <span className="text-xs text-slate-400">方向偏向</span>
          <span className={`text-xs font-medium ${biasColor}`}>{biasLabel}</span>
        </div>
        {rs.breakout_reason && (
          <p className="text-[10px] text-slate-500 mt-1">{rs.breakout_reason}</p>
        )}
      </div>
    </Card>
  );
}

function BoxVisualization({
  upper, lower, upperSource, lowerSource, upperTier, lowerTier,
  price, positionPct, positionLabel, coin,
}: {
  upper: number; lower: number; upperSource: string; lowerSource: string;
  upperTier: string; lowerTier: string;
  price: number; positionPct: number; positionLabel: string; coin: string;
}) {
  const clampedPct = Math.max(0, Math.min(100, positionPct));
  const positionColor =
    positionLabel === "near_upper" ? "text-red-400" :
    positionLabel === "near_lower" ? "text-green-400" :
    positionLabel === "above" ? "text-red-300" :
    positionLabel === "below" ? "text-green-300" :
    "text-yellow-400";
  const positionText =
    positionLabel === "near_upper" ? "接近上沿 (阻力区)" :
    positionLabel === "near_lower" ? "接近下沿 (支撑区)" :
    positionLabel === "above" ? "已突破上沿" :
    positionLabel === "below" ? "已跌破下沿" :
    "中间区域";

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-red-400 text-xs font-bold">▲ 上沿</span>
          <span className="text-white font-medium text-sm">{formatPrice(upper, coin)}</span>
          {upperTier && (
            <span className={`text-[9px] px-1 py-0.5 rounded font-bold ${
              upperTier === "S" ? "bg-amber-500/20 text-amber-400" :
              upperTier === "A" ? "bg-orange-500/20 text-orange-400" :
              "bg-blue-500/20 text-blue-400"
            }`}>{upperTier}级</span>
          )}
        </div>
        <span className="text-[10px] text-slate-600 max-w-[180px] truncate">{upperSource}</span>
      </div>

      <div className="relative">
        <div className="h-8 bg-slate-800 rounded-lg overflow-hidden border border-slate-700/50">
          <div className="absolute inset-0 rounded-lg opacity-20"
            style={{ background: "linear-gradient(to right, #22c55e, #eab308, #ef4444)" }}
          />
          <div
            className="absolute top-0 h-full w-0.5 bg-white z-10"
            style={{ left: `${clampedPct}%` }}
          >
            <div className="absolute -top-5 left-1/2 -translate-x-1/2 whitespace-nowrap text-[10px] text-white font-bold bg-slate-800 px-1.5 py-0.5 rounded border border-slate-600">
              {formatPrice(price, coin)}
            </div>
          </div>
          <div className="absolute left-2 top-1/2 -translate-y-1/2 text-[9px] text-green-500/60 font-medium">支撑区</div>
          <div className="absolute right-2 top-1/2 -translate-y-1/2 text-[9px] text-red-500/60 font-medium">阻力区</div>
        </div>
      </div>

      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-green-400 text-xs font-bold">▼ 下沿</span>
          <span className="text-white font-medium text-sm">{formatPrice(lower, coin)}</span>
          {lowerTier && (
            <span className={`text-[9px] px-1 py-0.5 rounded font-bold ${
              lowerTier === "S" ? "bg-amber-500/20 text-amber-400" :
              lowerTier === "A" ? "bg-orange-500/20 text-orange-400" :
              "bg-blue-500/20 text-blue-400"
            }`}>{lowerTier}级</span>
          )}
        </div>
        <span className="text-[10px] text-slate-600 max-w-[180px] truncate">{lowerSource}</span>
      </div>

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
            isAbove ? "bg-green-900/30 text-green-400" : "bg-red-900/30 text-red-400"
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
    bias = "强势多头：零轴上方 + 动能增强"; color = "text-green-400 bg-green-900/30";
  } else if (aboveZero && !histRising) {
    bias = "多头衰减：零轴上方 + 动能转弱"; color = "text-yellow-400 bg-yellow-900/30";
  } else if (!aboveZero && !histRising) {
    bias = "强势空头：零轴下方 + 动能增强"; color = "text-red-400 bg-red-900/30";
  } else {
    bias = "空头衰减：零轴下方 + 动能转弱"; color = "text-orange-400 bg-orange-900/30";
  }
  return <div className={`mt-2 text-xs px-3 py-2 rounded-lg ${color}`}>{bias}</div>;
}

function WickCard({ label, price, coin, currentPrice, direction }: {
  label: string; price: number; coin: string; currentPrice: number; direction: "above" | "below";
}) {
  const dist = currentPrice > 0 ? ((price - currentPrice) / currentPrice * 100).toFixed(2) : "—";
  return (
    <div className="bg-slate-800 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-1">
        <span className="text-sm">{direction === "below" ? "⬇️" : "⬆️"}</span>
        <span className="text-xs text-slate-400">{label}</span>
      </div>
      <div className="text-white font-medium text-sm">{formatPrice(price, coin)}</div>
      <div className="text-[10px] text-slate-500 mt-1">距当前价 {dist}%</div>
    </div>
  );
}

function ConfTag({ label, active, color }: { label: string; active: boolean; color: string }) {
  return (
    <span className={`text-xs px-3 py-1.5 rounded-lg border transition-all ${
      active ? color : "text-slate-600 bg-slate-800/50 border-slate-700/30"
    }`}>
      {active ? "✓ " : "✗ "}{label}
    </span>
  );
}
