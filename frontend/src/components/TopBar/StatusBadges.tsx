"use client";

import { useMarketStore } from "@/stores/marketStore";
import { TEMP_LABELS, PIN_RISK_COLORS } from "@/lib/constants";

export default function StatusBadges() {
  const data = useMarketStore((s) => s.data[s.coin]);
  const temp = data?.temperature;

  if (!temp) {
    return <div className="flex gap-3 text-xs text-slate-500">加载中...</div>;
  }

  const tempStyle = TEMP_LABELS[temp.label] || { emoji: "⚪", color: "#94a3b8" };
  const pinColor = PIN_RISK_COLORS[temp.pin_risk_level] || "#94a3b8";

  return (
    <div className="flex items-center gap-4 text-sm">
      <div className="flex items-center gap-1.5">
        <span className="text-slate-500">温度</span>
        <span className="font-semibold" style={{ color: tempStyle.color }}>
          {tempStyle.emoji} {temp.label}
          <span className="text-xs ml-1 opacity-70">({temp.score.toFixed(0)})</span>
        </span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className="text-slate-500">插针风险</span>
        <span className="font-semibold" style={{ color: pinColor }}>
          {temp.pin_risk_label}
        </span>
      </div>
    </div>
  );
}
