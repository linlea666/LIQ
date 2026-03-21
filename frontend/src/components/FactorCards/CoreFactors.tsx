"use client";

import { useMarketStore } from "@/stores/marketStore";
import FactorCardItem from "./FactorCardItem";

export default function CoreFactors() {
  const data = useMarketStore((s) => s.data[s.coin]);
  const factors = data?.temperature?.factors;

  if (!factors || factors.length === 0) {
    return (
      <div className="flex gap-2 overflow-x-auto py-2">
        {Array.from({ length: 8 }).map((_, i) => (
          <div key={i} className="bg-slate-800/40 border border-slate-700/30 rounded-lg p-3 min-w-[140px] flex-1 h-[110px] animate-pulse" />
        ))}
      </div>
    );
  }

  return (
    <div className="flex gap-2 overflow-x-auto">
      {factors.map((card) => (
        <FactorCardItem key={card.id} card={card} />
      ))}
    </div>
  );
}
