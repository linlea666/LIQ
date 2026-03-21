"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice, formatPct } from "@/lib/format";
import { COLORS } from "@/lib/constants";

export default function PriceBar() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const ticker = data?.ticker;

  if (!ticker) {
    return (
      <div className="flex items-center gap-3">
        <span className="text-2xl font-bold text-white">{coin}/USDT</span>
        <span className="text-slate-500 text-sm">等待数据...</span>
      </div>
    );
  }

  const isUp = ticker.change_24h >= 0;
  const changeColor = isUp ? COLORS.bullish : COLORS.bearish;

  return (
    <div className="flex items-center gap-4">
      <span className="text-lg font-semibold text-slate-400">{coin}/USDT</span>
      <span className="text-2xl font-bold text-white">
        {formatPrice(ticker.last, coin)}
      </span>
      <span className="text-sm font-medium" style={{ color: changeColor }}>
        {isUp ? "+" : ""}{formatPrice(ticker.change_24h, coin)}{" "}
        ({formatPct(ticker.change_pct_24h)})
      </span>
      <div className="flex gap-3 text-xs text-slate-500 ml-2">
        <span>24h高 {formatPrice(ticker.high_24h, coin)}</span>
        <span>24h低 {formatPrice(ticker.low_24h, coin)}</span>
      </div>
    </div>
  );
}
