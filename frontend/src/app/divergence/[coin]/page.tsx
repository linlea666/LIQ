"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type {
  DivergenceSampleRaw,
  DivergenceStats,
  DivergenceStatsResponse,
} from "@/lib/types";

/**
 * P1.7 · 分歧回测独立调试页
 *
 * 职责：
 *   - 展示 /api/divergence-stats/{coin} 全量数据
 *   - 顶部：币种切换 + 总样本数 + 刷新按钮
 *   - 中段：按 divergence_type 聚合的胜率对比表
 *   - 底部：最近 20 条原始样本明细
 *   - 说明面板：阈值 / 窗口 / 胜方判定
 */

const SUPPORTED_COINS = ["BTC", "ETH", "SOL"] as const;
const POLL_INTERVAL_MS = 15_000;

export default function DivergencePage() {
  const params = useParams();
  const rawCoin = String(params.coin ?? "BTC").toUpperCase();
  const coin = SUPPORTED_COINS.includes(rawCoin as (typeof SUPPORTED_COINS)[number])
    ? rawCoin
    : "BTC";

  const [data, setData] = useState<DivergenceStatsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [err, setErr] = useState<string>("");
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);
  const [nowSec, setNowSec] = useState<number>(0);

  const fetchOnce = useCallback(async () => {
    try {
      setLoading(true);
      const r = await fetch(`${API_BASE}/api/divergence-stats/${coin}`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j: DivergenceStatsResponse = await r.json();
      setData(j);
      setNowSec(Math.floor(Date.now() / 1000));
      setErr("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch error");
    } finally {
      setLoading(false);
    }
  }, [coin]);

  useEffect(() => {
    fetchOnce();
    if (!autoRefresh) return;
    const t = setInterval(fetchOnce, POLL_INTERVAL_MS);
    const tick = setInterval(
      () => setNowSec(Math.floor(Date.now() / 1000)),
      10_000,
    );
    return () => {
      clearInterval(t);
      clearInterval(tick);
    };
  }, [fetchOnce, autoRefresh]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <div className="max-w-6xl mx-auto p-6 space-y-6">
        {/* ── 顶部导航 ── */}
        <header className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <Link
              href="/"
              className="text-[13px] text-blue-400 hover:text-blue-300"
            >
              ← 主页
            </Link>
            <h1 className="text-lg font-bold text-slate-100">
              ⚖ 双引擎分歧回测
            </h1>
            <span className="text-xs text-slate-500">
              D04 扩展 · 历史胜率样本
            </span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            {SUPPORTED_COINS.map((c) => (
              <Link
                key={c}
                href={`/divergence/${c}`}
                className={`px-2.5 py-1 rounded border transition-colors ${
                  c === coin
                    ? "border-blue-500/50 bg-blue-500/10 text-blue-300"
                    : "border-slate-700 text-slate-400 hover:border-slate-600"
                }`}
              >
                {c}
              </Link>
            ))}
            <label className="flex items-center gap-1 text-slate-500 ml-2">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="accent-blue-500"
              />
              自动刷新
            </label>
            <button
              type="button"
              onClick={fetchOnce}
              className="px-2 py-1 rounded border border-slate-700 hover:border-slate-500 text-slate-300"
            >
              {loading ? "⟳" : "🔄"} 刷新
            </button>
          </div>
        </header>

        {/* ── 状态栏 ── */}
        <div className="flex items-center gap-4 text-[11px] text-slate-500 border-b border-slate-800 pb-2">
          {data ? (
            <>
              <span>
                总样本：
                <span className="text-slate-200 font-mono">
                  {data.total_samples}
                </span>
              </span>
              <span>
                已结算类型：
                <span className="text-slate-200 font-mono">
                  {data.stats.length}
                </span>
              </span>
              {err && <span className="text-red-400">· {err}</span>}
            </>
          ) : err ? (
            <span className="text-red-400">数据源异常：{err}</span>
          ) : (
            <span>⏳ 加载中...</span>
          )}
        </div>

        {/* ── 统计表 ── */}
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-slate-100">
            按分歧类型聚合统计
          </h2>
          {data && data.stats.length > 0 ? (
            <div className="rounded-lg border border-slate-700 overflow-hidden">
              <table className="w-full text-xs">
                <thead className="bg-slate-900/60 text-slate-500 border-b border-slate-700">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">
                      分歧类型
                    </th>
                    <th className="px-3 py-2 text-right font-medium w-20">
                      样本
                    </th>
                    <th className="px-3 py-2 font-medium w-48">
                      数学胜率
                    </th>
                    <th className="px-3 py-2 font-medium w-48">AI 胜率</th>
                    <th className="px-3 py-2 text-right font-medium w-28">
                      Δ24h 均值
                    </th>
                    <th className="px-3 py-2 text-left font-medium">结论</th>
                  </tr>
                </thead>
                <tbody>
                  {data.stats.map((s) => (
                    <StatsRow key={s.divergence_type} s={s} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-xs text-slate-500 bg-slate-900/40 border border-slate-800 rounded px-4 py-6 text-center">
              {data ? "暂无已结算样本（分歧样本需 24h 后结算）" : "..."}
            </div>
          )}
        </section>

        {/* ── 原始样本明细 ── */}
        <section className="space-y-2">
          <h2 className="text-sm font-semibold text-slate-100">
            最近 20 条原始样本
          </h2>
          {data && data.recent_samples.length > 0 ? (
            <div className="rounded-lg border border-slate-700 overflow-x-auto">
              <table className="w-full text-[11px] min-w-[760px]">
                <thead className="bg-slate-900/60 text-slate-500 border-b border-slate-700">
                  <tr>
                    <th className="px-3 py-2 text-left font-medium">时间</th>
                    <th className="px-2 py-2 text-left font-medium">类型</th>
                    <th className="px-2 py-2 text-left font-medium">数学</th>
                    <th className="px-2 py-2 text-left font-medium">AI</th>
                    <th className="px-2 py-2 text-right font-medium">
                      记录价
                    </th>
                    <th className="px-2 py-2 text-right font-medium">
                      Δ24h
                    </th>
                    <th className="px-2 py-2 text-center font-medium">
                      数学 win
                    </th>
                    <th className="px-2 py-2 text-center font-medium">
                      AI win
                    </th>
                    <th className="px-2 py-2 text-center font-medium">状态</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent_samples.map((r) => (
                    <SampleRow key={r.sample_id} r={r} coin={coin} nowSec={nowSec} />
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="text-xs text-slate-500 bg-slate-900/40 border border-slate-800 rounded px-4 py-6 text-center">
              {data ? "暂无分歧样本（等 consensus=conflict 触发）" : "..."}
            </div>
          )}
        </section>

        {/* ── 说明面板 ── */}
        <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 text-[11px] text-slate-400 leading-relaxed space-y-1.5">
          <h3 className="text-[12px] font-semibold text-slate-200 mb-1">
            📘 回测规则说明
          </h3>
          <div>
            · <span className="text-slate-300">记录触发</span>：
            融合层 <code className="text-blue-300">consensus=conflict</code> 时登记样本（同方向对
            60min 内去重，含 math/ai bias + 记录价格）
          </div>
          <div>
            · <span className="text-slate-300">时间窗</span>：每次 recompute 推进，1h / 2h / 24h 分别采价
          </div>
          <div>
            · <span className="text-slate-300">胜方判定</span>（24h 主结算）：
            <span className="ml-1 text-green-300">bullish</span> 方向 Δ24h ≥ +0.5% 算命中；
            <span className="ml-1 text-red-300">bearish</span> 方向 Δ24h ≤ −0.5% 算命中；
            neutral/wait 不计命中
          </div>
          <div>
            · <span className="text-slate-300">双方可同时 win/lose</span>：
            同向幅度大时两边都可能命中；震荡区间 |Δ| &lt; 0.5% 两边都不命中
          </div>
          <div>
            · <span className="text-slate-300">样本门槛</span>：
            &lt;10 视为&ldquo;参考性低&rdquo;；≥10 融合层才展示 historical_divergence 提示
          </div>
          <div>
            · <span className="text-slate-300">保留策略</span>：
            pending 24h+10min 宽限；resolved 保留 14 天
          </div>
        </section>
      </div>
    </div>
  );
}

// ─── 子组件 ───────────────────────────────────────────

function StatsRow({ s }: { s: DivergenceStats }) {
  const leader =
    s.math_win_rate > s.ai_win_rate + 0.05
      ? "math"
      : s.ai_win_rate > s.math_win_rate + 0.05
        ? "ai"
        : "tie";
  const leaderColor =
    leader === "math"
      ? "text-blue-300"
      : leader === "ai"
        ? "text-purple-300"
        : "text-slate-400";

  return (
    <tr className="border-b border-slate-800/70 hover:bg-slate-800/30">
      <td className="px-3 py-2 font-mono text-slate-300">{s.divergence_type}</td>
      <td className="px-3 py-2 text-right font-mono text-slate-300">
        {s.sample_size}
      </td>
      <td className="px-3 py-2">
        <WinrateBar rate={s.math_win_rate} tone="blue" />
      </td>
      <td className="px-3 py-2">
        <WinrateBar rate={s.ai_win_rate} tone="purple" />
      </td>
      <td
        className={`px-3 py-2 text-right font-mono ${
          s.avg_delta_pct_24h >= 0 ? "text-green-300" : "text-red-300"
        }`}
      >
        {s.avg_delta_pct_24h >= 0 ? "+" : ""}
        {s.avg_delta_pct_24h.toFixed(2)}%
      </td>
      <td className={`px-3 py-2 ${leaderColor} text-[11px]`}>
        {s.winner_hint_cn}
      </td>
    </tr>
  );
}

function WinrateBar({ rate, tone }: { rate: number; tone: "blue" | "purple" }) {
  const pct = Math.max(0, Math.min(1, rate)) * 100;
  const bar = tone === "blue" ? "bg-blue-500" : "bg-purple-500";
  const text = tone === "blue" ? "text-blue-300" : "text-purple-300";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-2 bg-slate-900/80 rounded overflow-hidden">
        <div className={`h-full ${bar}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`text-[11px] font-mono ${text} w-10 text-right`}>
        {pct.toFixed(0)}%
      </span>
    </div>
  );
}

function SampleRow({
  r,
  coin,
  nowSec,
}: {
  r: DivergenceSampleRaw;
  coin: string;
  nowSec: number;
}) {
  const age = nowSec > 0 ? nowSec - r.created_ts : 0;
  const timeStr = new Date(r.created_ts * 1000).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
  const delta24h = r.delta_pct_24h;
  const outcomeStyle = {
    resolved: "text-green-300",
    pending: "text-yellow-300",
    expired: "text-slate-500",
  }[r.outcome];

  return (
    <tr className="border-b border-slate-800/70 hover:bg-slate-800/30">
      <td className="px-3 py-1.5 text-slate-400">
        <div>{timeStr}</div>
        <div className="text-[10px] text-slate-600">
          {formatAge(age)}前
        </div>
      </td>
      <td className="px-2 py-1.5 font-mono text-slate-300 text-[10.5px]">
        {r.divergence_type}
      </td>
      <td className="px-2 py-1.5">
        <BiasChip action={r.math_action} bias={r.math_bias} />
      </td>
      <td className="px-2 py-1.5">
        <BiasChip action={r.ai_action} bias={r.ai_bias} />
      </td>
      <td className="px-2 py-1.5 text-right font-mono text-slate-300">
        {formatPrice(r.price_at_record, coin)}
      </td>
      <td
        className={`px-2 py-1.5 text-right font-mono ${
          delta24h == null
            ? "text-slate-600"
            : delta24h >= 0
              ? "text-green-300"
              : "text-red-300"
        }`}
      >
        {delta24h == null
          ? "—"
          : `${delta24h >= 0 ? "+" : ""}${delta24h.toFixed(2)}%`}
      </td>
      <td className="px-2 py-1.5 text-center">
        <WinCell win={r.math_win} outcome={r.outcome} />
      </td>
      <td className="px-2 py-1.5 text-center">
        <WinCell win={r.ai_win} outcome={r.outcome} />
      </td>
      <td className={`px-2 py-1.5 text-center ${outcomeStyle}`}>
        {OUTCOME_LABEL[r.outcome] ?? r.outcome}
      </td>
    </tr>
  );
}

function BiasChip({ action, bias }: { action: string; bias: string }) {
  const actionCn =
    { long: "做多", short: "做空", wait: "观望", avoid: "回避" }[action] ??
    action;
  const color =
    bias === "bullish"
      ? "text-green-300"
      : bias === "bearish"
        ? "text-red-300"
        : "text-slate-400";
  return (
    <span className={`text-[10.5px] ${color}`}>
      {actionCn}
      <span className="text-slate-600 ml-1">({bias})</span>
    </span>
  );
}

function WinCell({
  win,
  outcome,
}: {
  win: boolean | null | undefined;
  outcome: string;
}) {
  if (outcome !== "resolved") return <span className="text-slate-600">—</span>;
  if (win === true)
    return <span className="text-green-400 font-semibold">✓</span>;
  if (win === false) return <span className="text-red-400">✗</span>;
  return <span className="text-slate-600">—</span>;
}

const OUTCOME_LABEL: Record<string, string> = {
  resolved: "已结算",
  pending: "跟踪中",
  expired: "已过期",
};

function formatAge(sec: number): string {
  if (sec <= 0) return "0s";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}
