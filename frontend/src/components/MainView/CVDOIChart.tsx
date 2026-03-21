"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatUSD } from "@/lib/format";
import { COLORS } from "@/lib/constants";

export default function CVDOIChart() {
  const data = useMarketStore((s) => s.data[s.coin]);
  const cvd = data?.cvd_contract;
  const oi = data?.oi;
  const displayMode = useMarketStore((s) => s.displayMode);

  return (
    <div className="space-y-6">
      {/* CVD Section */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-slate-300">CVD 资金流向 (合约)</h3>
          {cvd && (
            <div className="flex items-center gap-3 text-xs">
              <span style={{ color: cvd.trend === "rising" ? COLORS.bullish : cvd.trend === "declining" ? COLORS.bearish : COLORS.neutral }}>
                趋势: {cvd.trend === "rising" ? "▲ 买方主导" : cvd.trend === "declining" ? "▼ 卖方主导" : "— 多空拉锯"}
              </span>
              {cvd.has_divergence && (
                <span className="text-yellow-400 font-medium">⚠️ 背离</span>
              )}
            </div>
          )}
        </div>

        {cvd?.last_points && cvd.last_points.length > 0 ? (
          <div className="bg-slate-800/50 rounded-lg p-3">
            <MiniChart
              points={cvd.last_points.map((p) => p.cvd)}
              color={cvd.trend === "rising" ? COLORS.bullish : COLORS.bearish}
              height={120}
            />
            {displayMode !== "beginner" && (
              <div className="text-xs text-slate-500 mt-2">
                1h净流入: {formatUSD(cvd.delta_1h)} | 数据点: {cvd.last_points.length}
              </div>
            )}
          </div>
        ) : (
          <div className="bg-slate-800/50 rounded-lg p-8 text-center text-slate-500 text-sm">
            等待 CVD 数据...
          </div>
        )}
      </div>

      {/* OI Section */}
      <div>
        <div className="flex items-center justify-between mb-3">
          <h3 className="text-sm font-semibold text-slate-300">OI 未平仓合约</h3>
          {oi && (
            <div className="flex items-center gap-3 text-xs">
              <span className="text-slate-400">{formatUSD(oi.current_usd)}</span>
              <span style={{ color: oi.change_1h_pct > 0 ? COLORS.bullish : oi.change_1h_pct < 0 ? COLORS.bearish : COLORS.neutral }}>
                1h: {oi.change_1h_pct > 0 ? "+" : ""}{oi.change_1h_pct.toFixed(2)}%
              </span>
              <span className="text-slate-500">
                {oi.trend === "surging" ? "⚡杠杆急升" :
                 oi.trend === "declining" ? "💥杠杆清洗" : "📊稳定"}
              </span>
            </div>
          )}
        </div>
        <div className="bg-slate-800/50 rounded-lg p-8 text-center text-slate-500 text-sm">
          {oi ? `当前OI: ${formatUSD(oi.current_usd)} | 5m变化: ${oi.change_5m_pct.toFixed(2)}%` : "等待 OI 数据..."}
        </div>
      </div>
    </div>
  );
}

function MiniChart({ points, color, height }: { points: number[]; color: string; height: number }) {
  if (points.length < 2) return null;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const range = max - min || 1;
  const w = 100;

  const pathData = points
    .map((v, i) => {
      const x = (i / (points.length - 1)) * w;
      const y = height - ((v - min) / range) * (height - 10) - 5;
      return `${i === 0 ? "M" : "L"} ${x} ${y}`;
    })
    .join(" ");

  return (
    <svg viewBox={`0 0 ${w} ${height}`} className="w-full" style={{ height }} preserveAspectRatio="none">
      <path d={pathData} fill="none" stroke={color} strokeWidth="1.5" vectorEffect="non-scaling-stroke" />
    </svg>
  );
}
