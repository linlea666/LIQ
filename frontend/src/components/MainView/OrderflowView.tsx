"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { API_BASE } from "@/lib/constants";
import { formatCnUsd } from "@/lib/format";

/**
 * 资金流视图（P2 · orderflow 小时/日桶）
 *
 * 数据来自后端本地聚合（/api/orderflow/{coin}/hourly|daily），零 Coinglass 配额：
 *   - 分时：近 72 小时 taker 买卖 USD 柱状（买卖分列）+ 净额 + 大单被动成交
 *   - 日历：近 90 天日桶，同口径
 * coverage_pct < 1 的桶以警示色点标注（数据断档，不插值）。
 */

type FlowRow = {
  coin: string;
  market: "spot" | "futures";
  hour_ts?: number;
  day_key?: string;
  taker_buy_usd: number;
  taker_sell_usd: number;
  net_usd: number;
  large_executed_bid_usd: number;
  large_executed_ask_usd: number;
  whale_buy_usd: number;
  whale_sell_usd: number;
  samples?: number;
  hours_covered?: number;
  coverage_pct: number;
};

type ViewMode = "hourly" | "daily";
type MarketMode = "spot" | "futures";

function tsLabel(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:00`;
}

function dayLabel(key: string): string {
  return `${key.slice(4, 6)}-${key.slice(6, 8)}`;
}

export default function OrderflowView() {
  const coin = useMarketStore((s) => s.coin);
  const [mode, setMode] = useState<ViewMode>("hourly");
  const [market, setMarket] = useState<MarketMode>("spot");
  const [rows, setRows] = useState<FlowRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const url =
        mode === "hourly"
          ? `${API_BASE}/api/orderflow/${coin}/hourly?market=${market}&hours=72`
          : `${API_BASE}/api/orderflow/${coin}/daily?market=${market}&days=90`;
      const r = await fetch(url);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const data = await r.json();
      setRows((data.rows || []) as FlowRow[]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "加载失败");
    } finally {
      setLoading(false);
    }
  }, [coin, mode, market]);

  useEffect(() => {
    load();
    const timer = setInterval(load, 60000);
    return () => clearInterval(timer);
  }, [load]);

  // 时间正序展示（API 返回倒序）
  const ordered = useMemo(() => [...rows].reverse(), [rows]);

  const maxSide = useMemo(() => {
    let m = 0;
    for (const r of ordered) {
      m = Math.max(m, r.taker_buy_usd, r.taker_sell_usd);
    }
    return m || 1;
  }, [ordered]);

  const totals = useMemo(() => {
    const t = {
      buy: 0, sell: 0, net: 0,
      largeBid: 0, largeAsk: 0, whaleBuy: 0, whaleSell: 0,
    };
    for (const r of ordered) {
      t.buy += r.taker_buy_usd;
      t.sell += r.taker_sell_usd;
      t.net += r.net_usd;
      t.largeBid += r.large_executed_bid_usd;
      t.largeAsk += r.large_executed_ask_usd;
      t.whaleBuy += r.whale_buy_usd;
      t.whaleSell += r.whale_sell_usd;
    }
    return t;
  }, [ordered]);

  return (
    <div className="space-y-4">
      {/* ── 控制条 ── */}
      <div className="flex items-center gap-3 flex-wrap">
        <div className="flex rounded-md overflow-hidden border border-slate-700">
          {(["hourly", "daily"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMode(m)}
              className={`px-3 py-1 text-xs font-medium ${
                mode === m
                  ? "bg-blue-500/20 text-blue-300"
                  : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {m === "hourly" ? "分时 · 72h" : "日历 · 90d"}
            </button>
          ))}
        </div>
        <div className="flex rounded-md overflow-hidden border border-slate-700">
          {(["spot", "futures"] as const).map((m) => (
            <button
              key={m}
              onClick={() => setMarket(m)}
              className={`px-3 py-1 text-xs font-medium ${
                market === m
                  ? "bg-emerald-500/20 text-emerald-300"
                  : "text-slate-500 hover:text-slate-300"
              }`}
            >
              {m === "spot" ? "现货" : "合约"}
            </button>
          ))}
        </div>
        {loading && <span className="text-xs text-slate-500">加载中…</span>}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>

      {/* ── 窗口汇总 ── */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2 text-xs">
        <SummaryCard label="主动买入" value={totals.buy} tone="green" />
        <SummaryCard label="主动卖出" value={totals.sell} tone="red" />
        <SummaryCard
          label="净流入"
          value={totals.net}
          tone={totals.net >= 0 ? "green" : "red"}
          signed
        />
        <SummaryCard
          label={`大单被动成交（买/卖）`}
          value={totals.largeBid}
          value2={totals.largeAsk}
          tone="slate"
        />
      </div>
      {(totals.whaleBuy > 0 || totals.whaleSell > 0) && (
        <div className="text-xs text-slate-400">
          鲸鱼主动成交：买 {formatCnUsd(totals.whaleBuy)} / 卖 {formatCnUsd(totals.whaleSell)}
        </div>
      )}

      {/* ── 柱状列表 ── */}
      {ordered.length === 0 && !loading ? (
        <div className="flex items-center justify-center h-40 text-slate-500 text-sm">
          暂无数据（聚合器每 5 分钟落一次桶，新部署需等待累积）
        </div>
      ) : (
        <div className="space-y-1">
          {ordered.map((r) => {
            const key = mode === "hourly" ? String(r.hour_ts) : r.day_key!;
            const label =
              mode === "hourly" ? tsLabel(r.hour_ts || 0) : dayLabel(r.day_key || "");
            const buyW = Math.max(1, (r.taker_buy_usd / maxSide) * 100);
            const sellW = Math.max(1, (r.taker_sell_usd / maxSide) * 100);
            const gap = r.coverage_pct < 0.999;
            return (
              <div key={`${r.market}-${key}`} className="flex items-center gap-2 text-xs">
                <span className="w-20 shrink-0 text-slate-500 font-mono">{label}</span>
                {/* 卖（左）*/}
                <div className="flex-1 flex justify-end">
                  <div
                    className="h-4 bg-red-500/50 rounded-sm"
                    style={{ width: `${sellW}%` }}
                    title={`卖 ${formatCnUsd(r.taker_sell_usd)}`}
                  />
                </div>
                {/* 买（右）*/}
                <div className="flex-1">
                  <div
                    className="h-4 bg-green-500/50 rounded-sm"
                    style={{ width: `${buyW}%` }}
                    title={`买 ${formatCnUsd(r.taker_buy_usd)}`}
                  />
                </div>
                <span
                  className={`w-24 shrink-0 text-right font-mono ${
                    r.net_usd >= 0 ? "text-green-400" : "text-red-400"
                  }`}
                >
                  {r.net_usd >= 0 ? "+" : ""}
                  {formatCnUsd(r.net_usd)}
                </span>
                {(r.large_executed_bid_usd > 0 || r.large_executed_ask_usd > 0) && (
                  <span
                    className="w-6 shrink-0 text-center text-amber-400"
                    title={`大单被动成交 买 ${formatCnUsd(r.large_executed_bid_usd)} / 卖 ${formatCnUsd(r.large_executed_ask_usd)}`}
                  >
                    ◆
                  </span>
                )}
                {gap && (
                  <span
                    className="w-3 shrink-0 text-amber-500"
                    title={`数据覆盖 ${(r.coverage_pct * 100).toFixed(0)}%（有断档）`}
                  >
                    ●
                  </span>
                )}
              </div>
            );
          })}
        </div>
      )}

      <p className="text-[11px] text-slate-600 leading-relaxed">
        口径：taker 主动成交 USD（Coinglass 聚合多所 5m bar，本地按小时/日沉淀）；
        「大单被动成交 ◆」为 ≥1M 挂单 lifecycle 的 executed 增量（买单被吃 = 下方承接兑现）。
        ● 表示该桶数据有断档（coverage &lt; 100%），不做插值。
      </p>
    </div>
  );
}

function SummaryCard({
  label, value, value2, tone, signed,
}: {
  label: string;
  value: number;
  value2?: number;
  tone: "green" | "red" | "slate";
  signed?: boolean;
}) {
  const toneCls =
    tone === "green" ? "text-green-400" : tone === "red" ? "text-red-400" : "text-slate-300";
  return (
    <div className="rounded-md border border-slate-700/60 bg-slate-800/40 px-3 py-2">
      <div className="text-slate-500">{label}</div>
      <div className={`font-mono font-semibold ${toneCls}`}>
        {signed && value >= 0 ? "+" : ""}
        {formatCnUsd(value)}
        {value2 !== undefined && (
          <span className="text-slate-500"> / {formatCnUsd(value2)}</span>
        )}
      </div>
    </div>
  );
}
