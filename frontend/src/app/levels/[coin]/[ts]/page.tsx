"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type { KeyLevelSnapshotV2, KeyLevelV2, KeyLevelSignal } from "@/lib/types";

const STATE_LABELS: Record<string, { text: string; color: string }> = {
  idle: { text: "待观察", color: "text-slate-500" },
  approaching: { text: "正接近", color: "text-yellow-400" },
  testing: { text: "正测试", color: "text-amber-400" },
  swept: { text: "已扫取", color: "text-red-400" },
  bounced: { text: "已反弹", color: "text-green-400" },
  broken: { text: "已突破", color: "text-red-500" },
  flipped: { text: "已翻转", color: "text-purple-400" },
};

const TIER_STYLES: Record<string, { bg: string; text: string }> = {
  S: { bg: "bg-amber-500/20", text: "text-amber-400" },
  A: { bg: "bg-red-500/15", text: "text-red-400" },
  B: { bg: "bg-blue-500/15", text: "text-blue-400" },
  C: { bg: "bg-slate-500/15", text: "text-slate-400" },
};

const ACTION_LABELS: Record<string, string> = {
  snipe_long: "狙击做多",
  snipe_short: "狙击做空",
  flip_long: "翻转做多",
  flip_short: "翻转做空",
  wait_sweep: "等待扫取",
  wait_approach: "等待接近",
};

function formatFullTime(ts: number): string {
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleString("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}

export default function KLHistoryDetailPage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";
  const ts = Number(params.ts);

  const [data, setData] = useState<KeyLevelSnapshotV2 | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch(`${API_BASE}/api/key-levels/detail/${coin}/${ts}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(`加载失败: ${e.message}`));
  }, [coin, ts]);

  if (error) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-400 text-lg mb-4">{error}</div>
          <Link href={`/levels/${coin}`} className="text-blue-400 hover:text-blue-300 text-sm">
            ← 返回关键位
          </Link>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="animate-spin w-10 h-10 border-2 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  const price = data.current_price;
  const resistances = data.levels.filter((l) => l.price > price).sort((a, b) => a.price - b.price);
  const supports = data.levels.filter((l) => l.price <= price).sort((a, b) => b.price - a.price);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300">
      <header className="border-b border-slate-700 bg-slate-900/80 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <Link href={`/levels/${coin}`} className="text-blue-400 hover:text-blue-300 text-sm shrink-0">
              ← 返回关键位
            </Link>
            <div>
              <h1 className="text-lg font-bold text-white">{coin} 关键位历史快照</h1>
              <div className="text-xs text-slate-500 mt-0.5">
                {formatFullTime(data.ts)} | 价格: <span className="text-white">{formatPrice(price, coin)}</span>
                {" "}| ATR: {formatPrice(data.atr, coin)}
                {" "}| <span className="text-amber-400">历史快照（只读）</span>
              </div>
            </div>
          </div>
          <span className="text-xs text-slate-500">{data.levels.length} 个关键位 · {data.active_count} 活跃</span>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-6 space-y-6">
        {data.structure_summary && (
          <Card title="总览">
            <div className="text-base text-white font-medium">{data.structure_summary}</div>
          </Card>
        )}

        {data.signals.length > 0 && (
          <Card title={`交易信号 (${data.signals.length})`}>
            <div className="space-y-3">
              {data.signals.map((sig, i) => (
                <SignalCard key={i} sig={sig} coin={coin} />
              ))}
            </div>
          </Card>
        )}

        {resistances.length > 0 && (
          <Card title={`阻力位 (${resistances.length})`}>
            <LevelTable levels={resistances} coin={coin} price={price} />
          </Card>
        )}

        {supports.length > 0 && (
          <Card title={`支撑位 (${supports.length})`}>
            <LevelTable levels={supports} coin={coin} price={price} />
          </Card>
        )}

        <div className="text-center text-xs text-slate-600 py-6 border-t border-slate-800">
          LIQ 关键位历史快照 · {formatFullTime(data.ts)}
        </div>
      </main>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl overflow-hidden">
      <div className="px-5 py-3 border-b border-slate-700/50 bg-slate-800/30">
        <h2 className="text-sm font-semibold text-slate-200">{title}</h2>
      </div>
      <div className="px-5 py-4">{children}</div>
    </div>
  );
}

function SignalCard({ sig, coin }: { sig: KeyLevelSignal; coin: string }) {
  const actionLabel = ACTION_LABELS[sig.action] ?? sig.action;
  const tierStyle = TIER_STYLES[sig.confidence] ?? TIER_STYLES.C;
  const isLong = sig.action.includes("long");
  const borderColor = isLong ? "border-green-700/40" : "border-red-700/40";

  return (
    <div className={`rounded-lg border ${borderColor} p-3 text-sm`}>
      <div className="flex items-center gap-2 mb-1">
        <span className={`px-1.5 py-0.5 rounded text-xs font-bold ${tierStyle.bg} ${tierStyle.text}`}>
          {sig.confidence}
        </span>
        <span className="font-semibold text-white">{actionLabel}</span>
        <span className="text-slate-400">@ {formatPrice(sig.level_price, coin)}</span>
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-zinc-400 mt-1">
        {sig.entry_price != null && <div>入场: <span className="text-white">{formatPrice(sig.entry_price, coin)}</span></div>}
        {sig.stop_loss != null && <div>止损: <span className="text-white">{formatPrice(sig.stop_loss, coin)}</span></div>}
        {sig.tp1 != null && <div>TP1: <span className="text-white">{formatPrice(sig.tp1, coin)}</span></div>}
        {sig.tp2 != null && <div>TP2: <span className="text-white">{formatPrice(sig.tp2, coin)}</span></div>}
        {sig.rr_ratio != null && <div>R:R = <span className="text-amber-400 font-semibold">1:{sig.rr_ratio.toFixed(1)}</span></div>}
      </div>
      {sig.reason && <div className="text-xs text-slate-500 mt-1">{sig.reason}</div>}
    </div>
  );
}

function LevelTable({ levels, coin, price }: { levels: KeyLevelV2[]; coin: string; price: number }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="text-xs text-slate-500 border-b border-slate-700/50">
            <th className="text-left py-2 pr-3">级别</th>
            <th className="text-right py-2 pr-3">价位</th>
            <th className="text-right py-2 pr-3">距当前</th>
            <th className="text-left py-2 pr-3">状态</th>
            <th className="text-right py-2 pr-3">共振</th>
            <th className="text-right py-2 pr-3">来源</th>
          </tr>
        </thead>
        <tbody>
          {levels.map((lv, i) => {
            const tier = TIER_STYLES[lv.strength_tier] ?? TIER_STYLES.C;
            const state = STATE_LABELS[lv.state] ?? { text: lv.state, color: "text-slate-400" };
            const dist = ((lv.price - price) / price) * 100;
            return (
              <tr key={i} className="border-b border-slate-800/50">
                <td className="py-2 pr-3">
                  <span className={`px-1.5 py-0.5 rounded text-xs font-bold ${tier.bg} ${tier.text}`}>
                    {lv.strength_tier}
                  </span>
                </td>
                <td className="py-2 pr-3 text-right font-mono text-white">{formatPrice(lv.price, coin)}</td>
                <td className={`py-2 pr-3 text-right text-xs ${dist > 0 ? "text-red-400" : "text-green-400"}`}>
                  {dist > 0 ? "+" : ""}{dist.toFixed(2)}%
                </td>
                <td className={`py-2 pr-3 text-xs ${state.color}`}>{state.text}</td>
                <td className="py-2 pr-3 text-right text-xs">{lv.confluence_score?.toFixed(0)}</td>
                <td className="py-2 pr-3 text-right text-xs text-slate-500">{lv.source_count}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
