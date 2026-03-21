"use client";

import { useMarketStore } from "@/stores/marketStore";
import { COLORS } from "@/lib/constants";

export default function WaterfallChart() {
  const data = useMarketStore((s) => s.data[s.coin]);
  const waterfall = data?.waterfall;

  if (!waterfall) {
    return <div className="flex items-center justify-center h-64 text-slate-500">等待数据汇总...</div>;
  }

  const maxAbs = Math.max(
    ...waterfall.items.map((i) => Math.abs(i.contribution_pct)),
    1
  );

  return (
    <div>
      <h3 className="text-sm font-semibold text-slate-300 mb-4">多空力量归因</h3>

      <div className="space-y-2">
        {waterfall.items.map((item) => {
          const isPositive = item.contribution_pct > 0;
          const width = (Math.abs(item.contribution_pct) / maxAbs) * 100;
          const color = isPositive ? COLORS.bullish : COLORS.bearish;

          return (
            <div key={item.factor_id} className="flex items-center gap-2">
              <span className="text-xs text-slate-500 w-16 text-right shrink-0">
                {item.factor_name}
              </span>
              <div className="flex-1 flex items-center h-6">
                {!isPositive && (
                  <div className="flex-1 flex justify-end">
                    <div
                      className="h-5 rounded-l"
                      style={{ width: `${width}%`, backgroundColor: color, opacity: 0.8 }}
                    />
                  </div>
                )}
                <div className="w-px h-6 bg-slate-600 shrink-0" />
                {isPositive && (
                  <div className="flex-1">
                    <div
                      className="h-5 rounded-r"
                      style={{ width: `${width}%`, backgroundColor: color, opacity: 0.8 }}
                    />
                  </div>
                )}
              </div>
              <span className="text-xs w-14 text-right shrink-0" style={{ color }}>
                {item.contribution_pct > 0 ? "+" : ""}{item.contribution_pct.toFixed(1)}%
              </span>
            </div>
          );
        })}
      </div>

      <div className="mt-4 flex justify-between text-xs border-t border-slate-700 pt-3">
        <span className="text-green-400">看多合计: +{waterfall.bullish_total.toFixed(1)}%</span>
        <span className="font-semibold text-slate-300">
          净偏向: <span style={{ color: waterfall.net_bias > 0 ? COLORS.bullish : waterfall.net_bias < 0 ? COLORS.bearish : COLORS.neutral }}>
            {waterfall.net_label} ({waterfall.net_bias > 0 ? "+" : ""}{waterfall.net_bias.toFixed(1)}%)
          </span>
        </span>
        <span className="text-red-400">看空合计: {waterfall.bearish_total.toFixed(1)}%</span>
      </div>
    </div>
  );
}
