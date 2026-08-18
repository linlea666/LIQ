"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { API_BASE } from "@/lib/constants";
import { formatCnUsd } from "@/lib/format";

/**
 * 资金流视图（P2 · orderflow 小时/日桶 + P3 whale 明细 + P4 价格维度）
 *
 * 数据来自后端本地聚合（/api/orderflow/{coin}/hourly|daily|whales|whale-summary），
 * 零 Coinglass 配额：
 *   - 顶部大卡：本窗口净流入（红绿 + 箭头，第一眼看懂方向）+ 实际覆盖标注
 *   - 分时：近 72 小时 taker 买卖 USD 柱状（买卖分列），柱内深色段 = 鲸鱼单笔大额部分
 *   - 日历：近 90 天日桶（北京时区日界），同口径
 *   - 每行尾部：该桶成交价区间（Binance 单源，悬停看收盘价）
 *   - 鲸鱼多周期统计：1h/2h/4h/24h 滚动窗口买卖金额 + 均价 + 净向（小白参考锚点）
 *   - 鲸鱼动态：近 24h 单笔 ≥ 阈值的主动成交流水（跟随现货/合约切换）
 * 覆盖度 < 100% 的桶整行灰化并注明"数据完整度 X%"；完全无 taker 数据的桶
 * 显示"无数据"而非 +0（断档不插值、不伪装成买卖平衡）。
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
  whale_buy_qty?: number;
  whale_sell_qty?: number;
  price_high?: number;
  price_low?: number;
  price_close?: number;
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

type WhaleWindow = {
  hours: number;
  buy_usd: number;
  sell_usd: number;
  net_usd: number;
  buy_count: number;
  sell_count: number;
  buy_vwap: number;
  sell_vwap: number;
  price_min: number;
  price_max: number;
  covered: boolean;
};

type WhaleSummary = {
  available: boolean;
  data_age_sec: number;
  windows: WhaleWindow[];
  h24_bucket: {
    buy_usd: number;
    sell_usd: number;
    net_usd: number;
    buy_vwap: number;
    sell_vwap: number;
    price_low: number;
    price_high: number;
  };
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

/** 价格显示：≥1000 保留整数千分位，<1000 保留 2 位（SOL 量级） */
function fmtPx(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "—";
  if (v >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return v.toFixed(2);
}

/** 行内价格区间的紧凑格式（64,777 → 64.8k，节省列宽；悬停有精确值） */
function fmtPxCompact(v: number): string {
  if (!Number.isFinite(v) || v <= 0) return "—";
  if (v >= 10000) return `${(v / 1000).toFixed(1)}k`;
  if (v >= 1000) return v.toLocaleString("en-US", { maximumFractionDigits: 0 });
  return v.toFixed(1);
}

export default function OrderflowView() {
  const coin = useMarketStore((s) => s.coin);
  const currentPrice = useMarketStore((s) => s.data[s.coin]?.ticker?.last);
  const [mode, setMode] = useState<ViewMode>("hourly");
  const [market, setMarket] = useState<MarketMode>("spot");
  const [rows, setRows] = useState<FlowRow[]>([]);
  const [whales, setWhales] = useState<WhaleRow[]>([]);
  const [summary, setSummary] = useState<WhaleSummary | null>(null);
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

  const loadSummary = useCallback(async () => {
    try {
      const r = await fetch(
        `${API_BASE}/api/orderflow/${coin}/whale-summary?market=${market}`,
      );
      if (!r.ok) return;
      setSummary((await r.json()) as WhaleSummary);
    } catch {
      // best-effort，同上
    }
  }, [coin, market]);

  useEffect(() => {
    load();
    loadWhales();
    loadSummary();
    const timer = setInterval(() => {
      load();
      loadWhales();
      loadSummary();
    }, 60000);
    return () => clearInterval(timer);
  }, [load, loadWhales, loadSummary]);

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
      covered: 0,
    };
    for (const r of ordered) {
      t.buy += r.taker_buy_usd || 0;
      t.sell += r.taker_sell_usd || 0;
      t.net += r.net_usd || 0;
      t.largeBid += r.large_executed_bid_usd || 0;
      t.largeAsk += r.large_executed_ask_usd || 0;
      t.whaleBuy += r.whale_buy_usd || 0;
      t.whaleSell += r.whale_sell_usd || 0;
      if (r.coverage_pct > 0) t.covered += 1;
    }
    return t;
  }, [ordered]);

  const windowLabel = mode === "hourly" ? "近 72 小时" : "近 90 天";
  const windowSize = mode === "hourly" ? 72 : 90;
  const windowUnit = mode === "hourly" ? "小时" : "天";
  // 当前市场的鲸鱼流水（跟随现货/合约切换，与上方开关口径一致）
  const marketWhales = useMemo(
    () => whales.filter((w) => w.market === market),
    [whales, market],
  );

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

      {/* ── 非 BTC 提示（后台完整轮询只常驻 BTC，其他币仅查看时积累）── */}
      {coin !== "BTC" && (
        <div className="rounded-md border border-amber-500/30 bg-amber-500/10 px-3 py-2 text-[11px] text-amber-300/90">
          {coin} 的 taker 数据仅在被查看时积累，历史覆盖不完整（断档桶已灰化标注）；
          完整连续数据目前仅 BTC 支持。
        </div>
      )}

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
            {totals.covered > 0 && totals.covered < windowSize && (
              <span className="ml-1 text-[10px] text-amber-400/80">
                （实际覆盖 {totals.covered}/{windowSize} {windowUnit}）
              </span>
            )}
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
        />
        <SummaryCard
          label="🐳 鲸鱼主动成交（买/卖）"
          hint="单笔 ≥ 阈值（$50 万或大数量）的主动成交累计（Binance 单源）。深色柱段与之对应"
          value={totals.whaleBuy}
          value2={totals.whaleSell}
        />
      </div>

      {/* ── 鲸鱼多周期统计（1h/2h/4h/24h 滚动窗口）── */}
      <WhaleSummaryCard
        summary={summary}
        market={market}
        currentPrice={currentPrice}
      />

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
            // coverage=0：该桶完全没有 taker 数据（只有鲸鱼/价格增量列），
            // 显示"无数据"而非绿色 +0，避免被误读为"买卖平衡"
            const noTaker = r.coverage_pct <= 0;
            const buyW = noTaker ? 0 : Math.max(1, (r.taker_buy_usd / maxSide) * 100);
            const sellW = noTaker ? 0 : Math.max(1, (r.taker_sell_usd / maxSide) * 100);
            // 柱内深色段：鲸鱼成交占该侧 taker 量的比例（跨源近似，>100% 时封顶）
            const whaleBuyPct = r.taker_buy_usd > 0
              ? Math.min(100, (r.whale_buy_usd / r.taker_buy_usd) * 100) : 0;
            const whaleSellPct = r.taker_sell_usd > 0
              ? Math.min(100, (r.whale_sell_usd / r.taker_sell_usd) * 100) : 0;
            const gap = r.coverage_pct < 0.999;
            const whaleTotal = (r.whale_buy_usd || 0) + (r.whale_sell_usd || 0);
            const pLow = r.price_low || 0;
            const pHigh = r.price_high || 0;
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
                {noTaker ? (
                  <span className="w-24 shrink-0 text-right font-mono text-slate-600">
                    无数据
                  </span>
                ) : (
                  <span
                    className={`w-24 shrink-0 text-right font-mono ${
                      r.net_usd >= 0 ? "text-green-400" : "text-red-400"
                    }`}
                  >
                    {r.net_usd >= 0 ? "+" : ""}
                    {formatCnUsd(r.net_usd)}
                  </span>
                )}
                {/* 该桶成交价区间（Binance 单源；旧桶无数据显示 —）*/}
                <span
                  className="w-24 shrink-0 text-right font-mono text-[10px] text-slate-500"
                  title={
                    pHigh > 0
                      ? `价格区间 ${fmtPx(pLow)} – ${fmtPx(pHigh)}${(r.price_close || 0) > 0 ? ` · 收盘 ${fmtPx(r.price_close || 0)}` : ""}`
                      : "该桶暂无价格数据（功能上线后开始积累）"
                  }
                >
                  {pHigh > 0 ? `${fmtPxCompact(pLow)}–${fmtPxCompact(pHigh)}` : "—"}
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

      {/* ── 鲸鱼动态（近 24h 单笔明细，跟随现货/合约切换）── */}
      <WhaleFeed whales={marketWhales} market={market} currentPrice={currentPrice} />

      <p className="text-[11px] text-slate-600 leading-relaxed">
        口径：taker 主动成交 USD（Coinglass 聚合多所 5m bar，本地按小时/日沉淀）；
        鲸鱼成交与价格区间为 Binance 单源（aggTrade），柱内深色段占比为跨源近似值；
        「◆」为 ≥1M 挂单 lifecycle 的 executed 增量（买单被吃 = 下方承接兑现）。
        整行变淡 = 该桶数据有断档（不插值）；「无数据」= 该桶无 taker 观测。
        {mode === "hourly" ? " 时间为本地时区。" : " 日界为北京时区（UTC+8）。"}
      </p>
    </div>
  );
}

function SummaryCard({
  label, hint, value, value2,
}: {
  label: string;
  hint?: string;
  value: number;
  value2?: number;
}) {
  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 px-4 py-3" title={hint}>
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-0.5 font-mono font-semibold text-slate-300">
        {formatCnUsd(value)}
        {value2 !== undefined && (
          <span className="text-slate-500"> / {formatCnUsd(value2)}</span>
        )}
      </div>
    </div>
  );
}

// ── 鲸鱼多周期统计（1h/2h/4h/24h 滚动窗口 + 小白解读）──────────────────
function WhaleSummaryCard({
  summary, market, currentPrice,
}: {
  summary: WhaleSummary | null;
  market: MarketMode;
  currentPrice?: number;
}) {
  if (!summary || !summary.available || summary.windows.length === 0) {
    return null;
  }
  const ageHours = summary.data_age_sec / 3600;
  const b = summary.h24_bucket;

  // 小白解读：现价 vs 近 24h 鲸鱼买入均价（桶级数据，跨重启保留）
  let interpret: { text: string; tone: "green" | "red" } | null = null;
  if (currentPrice && currentPrice > 0 && b.buy_vwap > 0) {
    const diffPct = ((currentPrice - b.buy_vwap) / b.buy_vwap) * 100;
    interpret = diffPct >= 0
      ? {
          text: `现价比近 24h 鲸鱼平均买入价（$${fmtPx(b.buy_vwap)}）高 ${diffPct.toFixed(1)}%，鲸鱼买单浮盈`,
          tone: "green",
        }
      : {
          text: `现价比近 24h 鲸鱼平均买入价（$${fmtPx(b.buy_vwap)}）低 ${Math.abs(diffPct).toFixed(1)}%，已跌破鲸鱼买入成本区（抄底参考锚点）`,
          tone: "red",
        };
  }

  return (
    <div className="rounded-lg border border-slate-700/60 bg-slate-800/40 overflow-hidden">
      <div className="px-3 py-2 border-b border-slate-700/50 flex items-center gap-2 text-xs">
        <span className="font-semibold text-slate-300">🐳 鲸鱼多周期统计</span>
        <span className="text-[10px] text-slate-500">
          {market === "spot" ? "现货" : "合约"} · 滚动窗口 · 单笔 ≥ 阈值的主动成交
        </span>
        {ageHours < 24 && (
          <span className="text-[10px] text-amber-400/80">
            数据自进程启动累积 {ageHours < 1 ? `${Math.round(summary.data_age_sec / 60)} 分钟` : `${ageHours.toFixed(1)} 小时`}
          </span>
        )}
      </div>
      <div className="px-3 py-2 grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-2">
        {summary.windows.map((w) => {
          const netBuy = w.net_usd >= 0;
          const hasData = w.buy_count + w.sell_count > 0;
          return (
            <div
              key={w.hours}
              className="rounded-md border border-slate-700/40 bg-slate-900/40 px-3 py-2"
              title={
                hasData && w.price_max > 0
                  ? `成交价区间 $${fmtPx(w.price_min)} – $${fmtPx(w.price_max)} · 买 ${w.buy_count} 笔 / 卖 ${w.sell_count} 笔`
                  : undefined
              }
            >
              <div className="flex items-center justify-between">
                <span className="text-[11px] text-slate-500">
                  近 {w.hours} 小时
                  {!w.covered && (
                    <span className="text-amber-500/80" title="进程启动时长不足该窗口，数值只是下限">
                      *
                    </span>
                  )}
                </span>
                {hasData && (
                  <span
                    className={`text-[11px] font-mono font-semibold ${
                      netBuy ? "text-green-400" : "text-red-400"
                    }`}
                  >
                    {netBuy ? "净买 +" : "净卖 -"}
                    {formatCnUsd(Math.abs(w.net_usd))}
                  </span>
                )}
              </div>
              {hasData ? (
                <div className="mt-1 space-y-0.5 text-[11px] font-mono">
                  <div className="text-green-400/90">
                    买 {formatCnUsd(w.buy_usd)}
                    {w.buy_vwap > 0 && (
                      <span className="text-slate-500">（均价 ${fmtPx(w.buy_vwap)}）</span>
                    )}
                  </div>
                  <div className="text-red-400/90">
                    卖 {formatCnUsd(w.sell_usd)}
                    {w.sell_vwap > 0 && (
                      <span className="text-slate-500">（均价 ${fmtPx(w.sell_vwap)}）</span>
                    )}
                  </div>
                </div>
              ) : (
                <div className="mt-1 text-[11px] text-slate-600">窗口内无鲸鱼成交</div>
              )}
            </div>
          );
        })}
      </div>
      {interpret && (
        <div
          className={`px-3 py-2 border-t border-slate-700/50 text-[11px] ${
            interpret.tone === "green" ? "text-green-300/90" : "text-red-300/90"
          }`}
        >
          💡 {interpret.text}
          {b.price_low > 0 && b.price_high > 0 && (
            <span className="text-slate-500">
              {" "}· 近 24h 成交价区间 ${fmtPx(b.price_low)} – ${fmtPx(b.price_high)}
              {b.sell_vwap > 0 && ` · 鲸鱼卖出均价 $${fmtPx(b.sell_vwap)}`}
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ── 鲸鱼动态流水（P3 whale 明细，跟随市场切换）─────────────────────────
function WhaleFeed({
  whales, market, currentPrice,
}: {
  whales: WhaleRow[];
  market: MarketMode;
  currentPrice?: number;
}) {
  const [open, setOpen] = useState(false);
  const marketTxt = market === "spot" ? "现货" : "合约";
  if (!whales || whales.length === 0) {
    return (
      <div className="rounded-md border border-slate-700/50 bg-slate-800/30 px-3 py-2 text-[11px] text-slate-500">
        🐳 鲸鱼动态：近 24h {marketTxt}暂无单笔大额成交记录
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
          近 24h {marketTxt}单笔大额主动成交 · {whales.length} 笔 · 最新在前
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
          const dev = currentPrice && currentPrice > 0 && w.price > 0
            ? ((w.price - currentPrice) / currentPrice) * 100
            : null;
          return (
            <div
              key={`${w.ts}-${w.usd}-${i}`}
              className="px-3 py-1.5 flex items-center gap-2 text-[11px]"
            >
              <span className="font-mono text-slate-500 shrink-0">{timeHm(w.ts)}</span>
              <span className={`shrink-0 font-semibold ${buy ? "text-green-400" : "text-red-400"}`}>
                {buy ? "鲸鱼买入" : "鲸鱼卖出"}
              </span>
              <span className={`font-mono font-semibold ${buy ? "text-green-300" : "text-red-300"}`}>
                {formatCnUsd(w.usd)}
              </span>
              <span className="text-slate-600 font-mono text-[10px]">
                @ {w.price.toLocaleString()}
              </span>
              {dev !== null && Math.abs(dev) >= 0.05 && (
                <span
                  className="text-slate-500 font-mono text-[10px]"
                  title="该笔成交价相对现价的偏离"
                >
                  距现价 {dev >= 0 ? "+" : ""}{dev.toFixed(1)}%
                </span>
              )}
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
