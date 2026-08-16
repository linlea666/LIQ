"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { API_BASE } from "@/lib/constants";
import { formatCnUsd } from "@/lib/format";

/**
 * 资金流视图（P2 · orderflow 小时/日桶 + P3 whale 明细）
 *
 * 数据来自后端本地聚合（/api/orderflow/{coin}/hourly|daily|whales），零 Coinglass 配额：
 *   - 顶部大卡：本窗口净流入（红绿 + 箭头，第一眼看懂方向）
 *   - 分时：近 72 小时 taker 买卖 USD 柱状（买卖分列），柱内深色段 = 鲸鱼单笔大额部分
 *   - 日历：近 90 天日桶（北京时区日界），同口径
 *   - 鲸鱼动态：近 24h 单笔 ≥ 阈值的主动成交流水
 * 覆盖度 < 100% 的桶整行灰化并注明"数据完整度 X%"（断档不插值）。
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

type WhaleRow = {
  ts: number;
  market: "spot" | "futures";
  side: "buy" | "sell";
  price: number;
  qty: number;
  usd: number;
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

function timeHm(ts: number): string {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export default function OrderflowView() {
  const coin = useMarketStore((s) => s.coin);
  const [mode, setMode] = useState<ViewMode>("hourly");
  const [market, setMarket] = useState<MarketMode>("spot");
  const [rows, setRows] = useState<FlowRow[]>([]);
  const [whales, setWhales] = useState<WhaleRow[]>([]);
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

  const loadWhales = useCallback(async () => {
    try {
      const r = await fetch(`${API_BASE}/api/orderflow/${coin}/whales?limit=200`);
      if (!r.ok) return;
      const data = await r.json();
      setWhales((data.rows || []) as WhaleRow[]);
    } catch {
      // 鲸鱼明细是 best-effort（进程重启后 deque 清空），失败不打扰主视图
    }
  }, [coin]);

  useEffect(() => {
    load();
    loadWhales();
    const timer = setInterval(() => {
      load();
      loadWhales();
    }, 60000);
    return () => clearInterval(timer);
  }, [load, loadWhales]);

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
      t.buy += r.taker_buy_usd || 0;
      t.sell += r.taker_sell_usd || 0;
      t.net += r.net_usd || 0;
      t.largeBid += r.large_executed_bid_usd || 0;
      t.largeAsk += r.large_executed_ask_usd || 0;
      t.whaleBuy += r.whale_buy_usd || 0;
      t.whaleSell += r.whale_sell_usd || 0;
    }
    return t;
  }, [ordered]);

  const windowLabel = mode === "hourly" ? "近 72 小时" : "近 90 天";

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
        <span className="text-[10px] text-slate-600">
          {mode === "hourly" ? "小时桶按你的本地时区显示" : "日桶按北京时区（UTC+8）日界"}
        </span>
        {loading && <span className="text-xs text-slate-500">加载中…</span>}
        {error && <span className="text-xs text-red-400">{error}</span>}
      </div>

      {/* ── 净流入大卡（小白第一眼）＋ 明细小卡 ── */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        <div
          className={`rounded-lg border px-4 py-3 ${
            totals.net >= 0
              ? "border-green-500/30 bg-green-500/10"
              : "border-red-500/30 bg-red-500/10"
          }`}
          title="窗口内 taker 主动买入 − 主动卖出。正 = 主动买盘更强（资金净流入），负 = 主动卖压更强"
        >
          <div className="text-xs text-slate-400">
            {windowLabel}{market === "spot" ? "现货" : "合约"}净流入
          </div>
          <div
            className={`mt-0.5 text-2xl font-mono font-bold ${
              totals.net >= 0 ? "text-green-400" : "text-red-400"
            }`}
          >
            {totals.net >= 0 ? "↑ +" : "↓ "}
            {formatCnUsd(totals.net)}
          </div>
          <div className="mt-1 text-[11px] text-slate-500">
            主动买 <span className="text-green-400/80 font-mono">{formatCnUsd(totals.buy)}</span>
            {" · "}
            主动卖 <span className="text-red-400/80 font-mono">{formatCnUsd(totals.sell)}</span>
          </div>
        </div>
        <SummaryCard
          label="大单被动成交（买/卖）"
          hint="≥1M 挂单被市价单吃掉的金额。买单被吃 = 下方真有人接货（承接兑现）"
          value={totals.largeBid}
          value2={totals.largeAsk}
          tone="slate"
        />
        <SummaryCard
          label="🐳 鲸鱼主动成交（买/卖）"
          hint="单笔 ≥ 阈值（$50 万或大数量）的主动成交累计。深色柱段与之对应"
          value={totals.whaleBuy}
          value2={totals.whaleSell}
          tone="slate"
        />
      </div>

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
            // 柱内深色段：鲸鱼成交占该侧 taker 量的比例（>100% 时封顶）
            const whaleBuyPct = r.taker_buy_usd > 0
              ? Math.min(100, (r.whale_buy_usd / r.taker_buy_usd) * 100) : 0;
            const whaleSellPct = r.taker_sell_usd > 0
              ? Math.min(100, (r.whale_sell_usd / r.taker_sell_usd) * 100) : 0;
            const gap = r.coverage_pct < 0.999;
            const whaleTotal = (r.whale_buy_usd || 0) + (r.whale_sell_usd || 0);
            return (
              <div
                key={`${r.market}-${key}`}
                className={`flex items-center gap-2 text-xs rounded-sm ${gap ? "opacity-50" : ""}`}
                title={gap ? `数据完整度 ${(r.coverage_pct * 100).toFixed(0)}%（该桶有断档，数值偏低）` : undefined}
              >
                <span className="w-20 shrink-0 text-slate-500 font-mono">{label}</span>
                {/* 卖（左），柱内右侧深色段 = 鲸鱼卖 */}
                <div className="flex-1 flex justify-end">
                  <div
                    className="h-4 bg-red-500/50 rounded-sm relative overflow-hidden"
                    style={{ width: `${sellW}%` }}
                    title={`卖 ${formatCnUsd(r.taker_sell_usd)}${r.whale_sell_usd > 0 ? `（其中鲸鱼 ${formatCnUsd(r.whale_sell_usd)}）` : ""}`}
                  >
                    {whaleSellPct > 0 && (
                      <div
                        className="absolute right-0 top-0 h-full bg-red-600"
                        style={{ width: `${whaleSellPct}%` }}
                      />
                    )}
                  </div>
                </div>
                {/* 买（右），柱内左侧深色段 = 鲸鱼买 */}
                <div className="flex-1">
                  <div
                    className="h-4 bg-green-500/50 rounded-sm relative overflow-hidden"
                    style={{ width: `${buyW}%` }}
                    title={`买 ${formatCnUsd(r.taker_buy_usd)}${r.whale_buy_usd > 0 ? `（其中鲸鱼 ${formatCnUsd(r.whale_buy_usd)}）` : ""}`}
                  >
                    {whaleBuyPct > 0 && (
                      <div
                        className="absolute left-0 top-0 h-full bg-green-600"
                        style={{ width: `${whaleBuyPct}%` }}
                      />
                    )}
                  </div>
                </div>
                <span
                  className={`w-24 shrink-0 text-right font-mono ${
                    r.net_usd >= 0 ? "text-green-400" : "text-red-400"
                  }`}
                >
                  {r.net_usd >= 0 ? "+" : ""}
                  {formatCnUsd(r.net_usd)}
                </span>
                {/* 行尾固定宽备注列：🐳 鲸鱼金额 / ◆ 大单被动成交 */}
                <span className="w-16 shrink-0 text-right">
                  {whaleTotal > 0 && (
                    <span
                      className="text-cyan-300/90 font-mono text-[10px]"
                      title={`鲸鱼主动成交 买 ${formatCnUsd(r.whale_buy_usd)} / 卖 ${formatCnUsd(r.whale_sell_usd)}`}
                    >
                      🐳{formatCnUsd(whaleTotal)}
                    </span>
                  )}
                </span>
                <span className="w-4 shrink-0 text-center">
                  {(r.large_executed_bid_usd > 0 || r.large_executed_ask_usd > 0) && (
                    <span
                      className="text-amber-400"
                      title={`大单被动成交 买 ${formatCnUsd(r.large_executed_bid_usd)} / 卖 ${formatCnUsd(r.large_executed_ask_usd)}`}
                    >
                      ◆
                    </span>
                  )}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* ── 鲸鱼动态（近 24h 单笔明细，现货+合约合流）── */}
      <WhaleFeed whales={whales} />

      <p className="text-[11px] text-slate-600 leading-relaxed">
        口径：taker 主动成交 USD（Coinglass 聚合多所 5m bar，本地按小时/日沉淀）；
        柱内深色段为鲸鱼单笔大额成交占比；「◆」为 ≥1M 挂单 lifecycle 的 executed
        增量（买单被吃 = 下方承接兑现）。整行变淡 = 该桶数据有断档（不插值）。
        {mode === "hourly" ? " 时间为本地时区。" : " 日界为北京时区（UTC+8）。"}
      </p>
    </div>
  );
}

function SummaryCard({
  label, hint, value, value2, tone, signed,
}: {
  label: string;
  hint?: string;
  value: number;
  value2?: number;
  tone: "green" | "red" | "slate";
  signed?: boolean;
}) {
  const toneCls =
    tone === "green" ? "text-green-400" : tone === "red" ? "text-red-400" : "text-slate-300";
  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 px-4 py-3" title={hint}>
      <div className="text-xs text-slate-500">{label}</div>
      <div className={`mt-0.5 font-mono font-semibold ${toneCls}`}>
        {signed && value >= 0 ? "+" : ""}
        {formatCnUsd(value)}
        {value2 !== undefined && (
          <span className="text-slate-500"> / {formatCnUsd(value2)}</span>
        )}
      </div>
    </div>
  );
}

// ── 鲸鱼动态流水（P3 whale 明细）─────────────────────────────────────────
function WhaleFeed({ whales }: { whales: WhaleRow[] }) {
  const [open, setOpen] = useState(false);
  if (!whales || whales.length === 0) {
    return (
      <div className="rounded-md border border-slate-700/50 bg-slate-800/30 px-3 py-2 text-[11px] text-slate-500">
        🐳 鲸鱼动态：近 24h 暂无单笔大额成交记录
        （阈值：单笔 ≥ $50 万或大数量；进程重启后明细从零累积）
      </div>
    );
  }
  const visible = open ? whales : whales.slice(0, 12);
  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 overflow-hidden">
      <div className="px-3 py-2 border-b border-slate-700/50 flex items-center gap-2 text-xs">
        <span className="font-semibold text-slate-300">🐳 鲸鱼动态</span>
        <span className="text-[10px] text-slate-500">
          近 24h 单笔大额主动成交 · {whales.length} 笔 · 最新在前
        </span>
        <div className="flex-1" />
        {whales.length > 12 && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-[11px] text-slate-400 hover:text-slate-200"
          >
            {open ? "收起" : `展开全部 ${whales.length} 笔`}
          </button>
        )}
      </div>
      <div className="divide-y divide-slate-700/30">
        {visible.map((w, i) => {
          const buy = w.side === "buy";
          return (
            <div
              key={`${w.ts}-${w.usd}-${i}`}
              className="px-3 py-1.5 flex items-center gap-2 text-[11px]"
            >
              <span className="font-mono text-slate-500 shrink-0">{timeHm(w.ts)}</span>
              <span className="text-slate-500 shrink-0">
                {w.market === "spot" ? "现货" : "合约"}
              </span>
              <span className={`shrink-0 font-semibold ${buy ? "text-green-400" : "text-red-400"}`}>
                {buy ? "鲸鱼买入" : "鲸鱼卖出"}
              </span>
              <span className={`font-mono font-semibold ${buy ? "text-green-300" : "text-red-300"}`}>
                {formatCnUsd(w.usd)}
              </span>
              <span className="text-slate-600 font-mono text-[10px]">
                @ {w.price.toLocaleString()}
              </span>
              {w.usd >= 5_000_000 && (
                <span className="px-1 py-0.5 rounded text-[9px] bg-amber-500/20 text-amber-300"
                      title="单笔 ≥ $500 万，已进入交易大脑事件流">
                  巨鲸
                </span>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
