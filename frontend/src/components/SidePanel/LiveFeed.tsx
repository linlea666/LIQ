"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatUSD, formatPrice, formatPct } from "@/lib/format";

export default function LiveFeed() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const funding = data?.funding;
  const basis = data?.basis;
  const orderbook = data?.orderbook;
  const levels = data?.levels;

  return (
    <div className="space-y-4">
      {/* Funding & Basis */}
      <div className="bg-slate-800/50 rounded-lg p-3">
        <h4 className="text-xs font-semibold text-slate-400 mb-2">资金费率 & 溢价</h4>
        {funding ? (
          <div className="space-y-1 text-xs">
            <Row label="OKX费率" value={funding.okx_rate !== null ? `${(funding.okx_rate * 100).toFixed(4)}%` : "N/A"} />
            <Row label="Binance费率" value={funding.binance_rate !== null ? `${(funding.binance_rate * 100).toFixed(4)}%` : "N/A"} />
            <Row label="解读" value={funding.interpretation} color="#eab308" />
          </div>
        ) : <div className="text-xs text-slate-600">等待数据...</div>}

        {basis && (
          <div className="mt-2 pt-2 border-t border-slate-700/50 space-y-1 text-xs">
            <Row label="期现溢价" value={formatPct(basis.basis_pct)} />
            <Row label="状态" value={basis.interpretation} />
          </div>
        )}
      </div>

      {/* Order Book Walls */}
      <div className="bg-slate-800/50 rounded-lg p-3">
        <h4 className="text-xs font-semibold text-slate-400 mb-2">订单簿大单</h4>
        {orderbook ? (
          <div className="space-y-2">
            <div>
              <div className="text-[10px] text-red-400 mb-1">卖墙 ▼</div>
              {orderbook.ask_walls.slice(0, 3).map((w, i) => (
                <div key={i} className="flex justify-between text-xs text-slate-400">
                  <span>{formatPrice(w.price, coin)}</span>
                  <span className="text-red-400">{w.size.toFixed(1)} ({formatUSD(w.size_usd)})</span>
                </div>
              ))}
              {orderbook.ask_walls.length === 0 && <div className="text-xs text-slate-600">无大卖单</div>}
            </div>
            <div>
              <div className="text-[10px] text-green-400 mb-1">买墙 ▲</div>
              {orderbook.bid_walls.slice(0, 3).map((w, i) => (
                <div key={i} className="flex justify-between text-xs text-slate-400">
                  <span>{formatPrice(w.price, coin)}</span>
                  <span className="text-green-400">{w.size.toFixed(1)} ({formatUSD(w.size_usd)})</span>
                </div>
              ))}
              {orderbook.bid_walls.length === 0 && <div className="text-xs text-slate-600">无大买单</div>}
            </div>
          </div>
        ) : <div className="text-xs text-slate-600">等待数据...</div>}
      </div>

      {/* AI Levels Summary */}
      {levels && (
        <div className="bg-slate-800/50 rounded-lg p-3">
          <h4 className="text-xs font-semibold text-slate-400 mb-2">AI 关键价位</h4>
          <div className="space-y-1 text-xs">
            {levels.resistances.slice(0, 3).map((r) => (
              <div key={r.label} className="flex justify-between">
                <span className="text-red-400">{r.label}</span>
                <span className="text-white">{formatPrice(r.price, coin)}</span>
                <span className="text-slate-500 text-[10px] max-w-[40%] truncate">{r.sources.join(", ")}</span>
              </div>
            ))}
            <div className="border-t border-slate-700/50 my-1" />
            {levels.supports.slice(0, 3).map((s) => (
              <div key={s.label} className="flex justify-between">
                <span className="text-green-400">{s.label}</span>
                <span className="text-white">{formatPrice(s.price, coin)}</span>
                <span className="text-slate-500 text-[10px] max-w-[40%] truncate">{s.sources.join(", ")}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

function Row({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex justify-between">
      <span className="text-slate-500">{label}</span>
      <span style={{ color: color || "#e2e8f0" }}>{value}</span>
    </div>
  );
}
