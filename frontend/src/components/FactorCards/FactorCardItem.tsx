"use client";

import type { FactorCard } from "@/lib/types";
import PercentileBar from "@/components/common/PercentileBar";
import { COLORS } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";

interface Props {
  card: FactorCard;
}

const DIR_COLORS = {
  bullish: COLORS.bullish,
  bearish: COLORS.bearish,
  neutral: COLORS.neutral,
};

export default function FactorCardItem({ card }: Props) {
  const displayMode = useMarketStore((s) => s.displayMode);
  const color = DIR_COLORS[card.direction] || COLORS.neutral;
  const bgOpacity = card.direction === "neutral" ? "bg-slate-800/60" : card.direction === "bullish" ? "bg-emerald-950/40" : "bg-red-950/40";

  return (
    <div className={`${bgOpacity} border border-slate-700/50 rounded-lg p-3 min-w-[140px] flex-1`}>
      <div className="flex items-center justify-between mb-1">
        <span className="text-[10px] text-slate-500 font-medium">{card.id}</span>
        <span className="text-[10px] text-slate-600">{card.name}</span>
      </div>

      <div className="text-lg font-bold mb-0.5" style={{ color }}>
        {card.value}
      </div>

      {displayMode !== "beginner" && (
        <div className="text-[11px] text-slate-400 mb-1.5 truncate">
          {card.sub_text}
        </div>
      )}

      <PercentileBar value={card.percentile} height={3} />

      <div className="text-[11px] mt-1 font-medium" style={{ color }}>
        {card.summary}
      </div>
    </div>
  );
}
