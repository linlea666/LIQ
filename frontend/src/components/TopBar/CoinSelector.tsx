"use client";

import { SUPPORTED_COINS, type CoinType } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";

export default function CoinSelector() {
  const coin = useMarketStore((s) => s.coin);
  const setCoin = useMarketStore((s) => s.setCoin);

  return (
    <div className="flex gap-1 bg-slate-800 rounded-lg p-0.5">
      {SUPPORTED_COINS.map((c) => (
        <button
          key={c}
          onClick={() => setCoin(c as CoinType)}
          className={`px-3 py-1 text-sm rounded-md font-medium transition-all ${
            coin === c
              ? "bg-blue-600 text-white"
              : "text-slate-400 hover:text-white hover:bg-slate-700"
          }`}
        >
          {c}
        </button>
      ))}
    </div>
  );
}
