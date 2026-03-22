"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice, formatUSD, formatPct } from "@/lib/format";
import { API_BASE } from "@/lib/constants";
import { useEffect, useState } from "react";
import type { LiquidationMap } from "@/lib/types";

export default function MarketSummary() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const [liq24h, setLiq24h] = useState<LiquidationMap | null>(null);
  const [liq7d, setLiq7d] = useState<LiquidationMap | null>(null);

  const ticker = data?.ticker;
  const funding = data?.funding;
  const basis = data?.basis;
  const oi = data?.oi;
  const orderbook = data?.orderbook;
  const levels = data?.levels;
  const temperature = data?.temperature;
  const cvd = data?.cvd_contract;

  useEffect(() => {
    const fetchBoth = async () => {
      try {
        const [r24, r7] = await Promise.all([
          fetch(`${API_BASE}/api/liquidation/${coin}?cycle=24h`),
          fetch(`${API_BASE}/api/liquidation/${coin}?cycle=7d`),
        ]);
        if (r24.ok) setLiq24h(await r24.json());
        if (r7.ok) setLiq7d(await r7.json());
      } catch { /* silent */ }
    };
    fetchBoth();
    const timer = setInterval(fetchBoth, 30000);
    return () => clearInterval(timer);
  }, [coin]);

  if (!ticker) {
    return <div className="flex items-center justify-center h-64 text-slate-500">等待数据加载...</div>;
  }

  const fundingBias = funding
    ? funding.avg_rate > 0.0003
      ? "bearish"
      : funding.avg_rate < -0.0003
      ? "bullish"
      : "neutral"
    : "neutral";

  const orderbookBias = orderbook
    ? orderbook.bid_total_usd > orderbook.ask_total_usd * 1.5
      ? "bullish"
      : orderbook.ask_total_usd > orderbook.bid_total_usd * 1.5
      ? "bearish"
      : "neutral"
    : "neutral";

  return (
    <div className="space-y-5 max-w-4xl">
      {/* 1. 市场温度一句话 */}
      <SummaryCard>
        <h3 className="text-base font-bold text-white mb-2">📊 当前市场状态</h3>
        <div className="text-sm text-slate-300 space-y-1">
          <p>
            <span className="text-white font-medium">{coin}/USDT</span> 当前价格{" "}
            <span className="text-yellow-400 font-bold">{formatPrice(ticker.last, coin)}</span>
            ，24h 涨跌{" "}
            <span className={ticker.change_pct_24h >= 0 ? "text-green-400" : "text-red-400"}>
              {formatPct(ticker.change_pct_24h)}
            </span>
            ，最高 {formatPrice(ticker.high_24h, coin)}，最低 {formatPrice(ticker.low_24h, coin)}。
          </p>
          {temperature && (
            <p>
              市场温度 <span className="text-yellow-400 font-medium">{temperature.score}/100</span>（{temperature.label}），
              插针风险 <span className={
                temperature.pin_risk_level === "extreme" || temperature.pin_risk_level === "high"
                  ? "text-red-400 font-medium" : "text-green-400"
              }>{temperature.pin_risk_label}</span>。
            </p>
          )}
          {cvd && (
            <p>
              资金流向（1h）：{cvd.trend === "rising" ? "🟢 净流入" : cvd.trend === "falling" ? "🔴 净流出" : "⚪ 持平"}
              {cvd.has_divergence && <span className="text-yellow-400 ml-1">⚠️ 检测到 CVD-价格背离</span>}
            </p>
          )}
        </div>
      </SummaryCard>

      {/* 2. 清算密集区总结 */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <SummaryCard>
          <h3 className="text-sm font-bold text-red-400 mb-2">🔴 空头清算密集区（上方）</h3>
          <p className="text-xs text-slate-500 mb-2">价格上涨到这些区域时，空头会被清算（可能加速上涨）</p>
          <ClusterList clusters={liq24h?.clusters_above} coin={coin} label="24h" />
          {liq7d && liq7d.clusters_above.length > 0 && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <ClusterList clusters={liq7d.clusters_above} coin={coin} label="7d" />
            </>
          )}
        </SummaryCard>

        <SummaryCard>
          <h3 className="text-sm font-bold text-green-400 mb-2">🟢 多头清算密集区（下方）</h3>
          <p className="text-xs text-slate-500 mb-2">价格下跌到这些区域时，多头会被清算（可能加速下跌）</p>
          <ClusterList clusters={liq24h?.clusters_below} coin={coin} label="24h" />
          {liq7d && liq7d.clusters_below.length > 0 && (
            <>
              <div className="border-t border-slate-700 my-2" />
              <ClusterList clusters={liq7d.clusters_below} coin={coin} label="7d" />
            </>
          )}
        </SummaryCard>
      </div>

      {/* 3. 综合判断 + 止损建议 */}
      <SummaryCard>
        <h3 className="text-base font-bold text-white mb-2">🛡️ 止损位建议</h3>
        <p className="text-xs text-slate-500 mb-3">综合清算真空区 + ATR + 订单簿大单，分三档建议</p>

        {levels && levels.stop_loss_zones.length > 0 ? (
          <div className="space-y-3">
            {levels.stop_loss_zones.map((z, i) => (
              <div key={i} className="bg-slate-800 rounded-lg p-3">
                <div className="flex items-center gap-2 mb-1">
                  <span className={`text-xs font-bold px-2 py-0.5 rounded ${
                    z.direction === "long" ? "bg-green-900/50 text-green-400" : "bg-red-900/50 text-red-400"
                  }`}>
                    {z.direction === "long" ? "做多止损" : "做空止损"}
                  </span>
                  <span className="text-white font-bold">{formatPrice(z.price, coin)}</span>
                  <span className="text-xs text-slate-500">({z.atr_multiple.toFixed(1)}x ATR)</span>
                </div>
                <div className="text-xs text-slate-400">
                  区间: {formatPrice(z.zone_from, coin)} - {formatPrice(z.zone_to, coin)}
                </div>
                {z.reasons.map((r, j) => (
                  <div key={j} className="text-xs text-slate-500 ml-2">• {r}</div>
                ))}
              </div>
            ))}
          </div>
        ) : (
          <div className="text-sm text-slate-500">等待足够数据计算止损建议...</div>
        )}
      </SummaryCard>

      {/* 4. 资金费率 + 订单簿判断 */}
      <SummaryCard>
        <h3 className="text-base font-bold text-white mb-2">📡 辅助信号</h3>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3 text-sm">
          <div className="bg-slate-800 rounded-lg p-3">
            <div className="text-xs text-slate-500 mb-1">资金费率</div>
            {funding ? (
              <>
                <div className="text-white font-medium">
                  {(funding.avg_rate * 100).toFixed(4)}%
                  <span className={`ml-2 text-xs ${
                    fundingBias === "bearish" ? "text-red-400" : fundingBias === "bullish" ? "text-green-400" : "text-slate-400"
                  }`}>
                    {funding.interpretation}
                  </span>
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  {fundingBias === "bearish"
                    ? "💡 多头过度拥挤，需警惕回调"
                    : fundingBias === "bullish"
                    ? "💡 空头过度拥挤，可能向上挤压"
                    : "💡 多空均衡，无明显偏向"}
                </div>
              </>
            ) : <span className="text-slate-600">等待数据...</span>}
          </div>

          <div className="bg-slate-800 rounded-lg p-3">
            <div className="text-xs text-slate-500 mb-1">订单簿大单</div>
            {orderbook ? (
              <>
                <div className="text-white font-medium">
                  买单 {formatUSD(orderbook.bid_total_usd)} / 卖单 {formatUSD(orderbook.ask_total_usd)}
                </div>
                <div className="text-xs text-slate-500 mt-1">
                  {orderbookBias === "bullish"
                    ? "💡 买单墙明显厚于卖单，短期有支撑"
                    : orderbookBias === "bearish"
                    ? "💡 卖单墙明显厚于买单，上方有阻力"
                    : "💡 买卖单均衡，无明显倾向"}
                </div>
              </>
            ) : <span className="text-slate-600">等待数据...</span>}
          </div>

          {oi && (
            <div className="bg-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500 mb-1">持仓量变化</div>
              <div className="text-white font-medium">
                {formatUSD(oi.current_usd)}
                <span className={`ml-2 text-xs ${oi.change_1h_pct >= 0 ? "text-green-400" : "text-red-400"}`}>
                  1h {formatPct(oi.change_1h_pct)}
                </span>
              </div>
              <div className="text-xs text-slate-500 mt-1">
                {oi.trend === "surging"
                  ? "💡 持仓量激增，杠杆集中，波动可能加大"
                  : oi.trend === "declining"
                  ? "💡 持仓量下降，杠杆释放，波动趋于收敛"
                  : "💡 持仓量稳定"}
              </div>
            </div>
          )}

          {basis && (
            <div className="bg-slate-800 rounded-lg p-3">
              <div className="text-xs text-slate-500 mb-1">期现溢价</div>
              <div className="text-white font-medium">
                {formatPct(basis.basis_pct)}
                <span className="ml-2 text-xs text-slate-400">{basis.interpretation}</span>
              </div>
            </div>
          )}
        </div>
      </SummaryCard>

      {/* 5. 关键价位 */}
      {levels && (
        <SummaryCard>
          <h3 className="text-base font-bold text-white mb-2">📍 关键支撑 & 阻力位</h3>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            <div>
              <div className="text-xs font-medium text-red-400 mb-1">阻力位（上方）</div>
              {levels.resistances.slice(0, 4).map((r) => (
                <div key={r.label} className="flex justify-between text-xs py-0.5">
                  <span className="text-slate-400">{r.label}</span>
                  <span className="text-white font-medium">{formatPrice(r.price, coin)}</span>
                  <span className="text-slate-600 max-w-[40%] truncate">{r.sources.join(", ")}</span>
                </div>
              ))}
            </div>
            <div>
              <div className="text-xs font-medium text-green-400 mb-1">支撑位（下方）</div>
              {levels.supports.slice(0, 4).map((s) => (
                <div key={s.label} className="flex justify-between text-xs py-0.5">
                  <span className="text-slate-400">{s.label}</span>
                  <span className="text-white font-medium">{formatPrice(s.price, coin)}</span>
                  <span className="text-slate-600 max-w-[40%] truncate">{s.sources.join(", ")}</span>
                </div>
              ))}
            </div>
          </div>
        </SummaryCard>
      )}
    </div>
  );
}

function SummaryCard({ children }: { children: React.ReactNode }) {
  return (
    <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-4">
      {children}
    </div>
  );
}

function ClusterList({ clusters, coin, label }: {
  clusters?: { price_from: number; price_to: number; total_usd: number; dominant_leverage: string; distance_pct: number }[];
  coin: string;
  label: string;
}) {
  if (!clusters || clusters.length === 0) {
    return <div className="text-xs text-slate-600">暂无{label}数据</div>;
  }
  return (
    <div>
      <div className="text-[10px] text-slate-600 mb-1">{label}:</div>
      {clusters.slice(0, 3).map((c, i) => (
        <div key={i} className="flex items-center justify-between text-xs py-0.5">
          <span className="text-slate-400">
            {formatPrice(c.price_from, coin)} - {formatPrice(c.price_to, coin)}
          </span>
          <span className="text-yellow-400 font-medium">{formatUSD(c.total_usd)}</span>
          <span className="text-slate-500">{c.dominant_leverage}x</span>
          <span className="text-slate-600">{c.distance_pct.toFixed(1)}%</span>
        </div>
      ))}
    </div>
  );
}
