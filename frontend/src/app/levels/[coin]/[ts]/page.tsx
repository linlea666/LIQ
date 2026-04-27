"use client";

import { useEffect, useMemo, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import { sortLevels, type SortKey } from "@/lib/levelBrief";
import LevelDetailRow from "@/components/Levels/LevelDetailRow";
import LevelSortControl from "@/components/Levels/LevelSortControl";
import RegimeChip from "@/components/MainView/RegimeChip";
import type { KeyLevelSnapshotV2, KeyLevelV2, KeyLevelSignal } from "@/lib/types";

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

const TABLE_COL_COUNT = 10;

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

  // V3：与全景页一致的交互（默认 tier-first + 全部展示）
  const [sortKey, setSortKey] = useState<SortKey>("tier");
  const [activeOnly, setActiveOnly] = useState(false);

  useEffect(() => {
    fetch(`${API_BASE}/api/key-levels/detail/${coin}/${ts}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(`加载失败: ${e.message}`));
  }, [coin, ts]);

  const tierStats = useMemo(() => {
    const out = { S: 0, A: 0, B: 0, C: 0, stale: 0, contradiction: 0 };
    if (!data) return out;
    for (const lv of data.levels) {
      const t = lv.strength_tier as keyof typeof out;
      if (t === "S" || t === "A" || t === "B" || t === "C") out[t]++;
      if (lv.is_stale) out.stale++;
      if ((lv.contradiction_penalty ?? 0) > 0) out.contradiction++;
    }
    return out;
  }, [data]);

  const { resistances, supports } = useMemo(() => {
    if (!data) return { resistances: [] as KeyLevelV2[], supports: [] as KeyLevelV2[] };
    const price = data.current_price;
    const above = data.levels.filter((l) => l.price > price);
    const below = data.levels.filter((l) => l.price <= price);
    const filterFn = (l: KeyLevelV2) =>
      activeOnly ? l.state !== "idle" || l.strength_tier !== "C" : true;
    return {
      resistances: sortLevels(above.filter(filterFn), sortKey),
      supports: sortLevels(below.filter(filterFn), sortKey),
    };
  }, [data, sortKey, activeOnly]);

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
  const totalLevels = data.levels.length;
  const visibleLevels = resistances.length + supports.length;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300">
      <header className="border-b border-slate-700 bg-slate-900/80 sticky top-0 z-10">
        <div className="max-w-6xl mx-auto px-6 py-4 flex items-center justify-between">
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
          <span className="text-xs text-slate-500">{totalLevels} 个关键位 · {data.active_count} 活跃</span>
        </div>
      </header>

      <main className="max-w-6xl mx-auto px-6 py-6 space-y-6">
        {/* V3：顶部 regime + tier 汇总 */}
        <div className="flex items-stretch gap-3 flex-wrap">
          {data.regime && (
            <div className="flex-1 min-w-[280px]">
              <RegimeChip kl={data} />
            </div>
          )}
          <div className="flex items-center gap-2 px-3 py-2 rounded-lg border border-slate-700/60 bg-slate-800/40 flex-wrap">
            <span className="text-[10px] text-slate-500">分布</span>
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-amber-500/15 text-amber-300">
              S {tierStats.S}
            </span>
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-red-500/15 text-red-300">
              A {tierStats.A}
            </span>
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-blue-500/15 text-blue-300">
              B {tierStats.B}
            </span>
            <span className="px-2 py-0.5 rounded text-[10px] font-bold bg-slate-500/15 text-slate-400">
              C {tierStats.C}
            </span>
            {tierStats.stale > 0 && (
              <span className="px-2 py-0.5 rounded text-[10px] bg-rose-500/15 text-rose-300">
                ⏳ 过期 {tierStats.stale}
              </span>
            )}
            {tierStats.contradiction > 0 && (
              <span className="px-2 py-0.5 rounded text-[10px] bg-orange-500/15 text-orange-300">
                ⚠ 矛盾 {tierStats.contradiction}
              </span>
            )}
          </div>
        </div>

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

        {/* V3：统一表格 + 排序 + 全部展示 */}
        <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl overflow-hidden">
          <div className="px-5 py-3 border-b border-slate-700/50 bg-slate-800/30 flex items-center justify-between gap-3 flex-wrap">
            <div className="flex items-center gap-3">
              <h2 className="text-sm font-semibold text-white">关键位明细（历史）</h2>
              <span className="text-[11px] text-slate-500">
                显示 {visibleLevels} / 共 {totalLevels} 位 · 点击行展开 V3 详情
              </span>
            </div>
            <div className="flex items-center gap-2 flex-wrap">
              <LevelSortControl value={sortKey} onChange={setSortKey} />
              <label className="inline-flex items-center gap-1.5 text-[11px] text-slate-400 cursor-pointer select-none">
                <input
                  type="checkbox"
                  checked={activeOnly}
                  onChange={(e) => setActiveOnly(e.target.checked)}
                  className="accent-blue-500"
                />
                仅活跃位
              </label>
            </div>
          </div>

          {resistances.length > 0 && (
            <LevelSection
              title={`上方阻力位 (${resistances.length})`}
              levels={resistances}
              coin={coin}
              price={price}
              accent="resistance"
            />
          )}
          {supports.length > 0 && (
            <LevelSection
              title={`下方支撑位 (${supports.length})`}
              levels={supports}
              coin={coin}
              price={price}
              accent="support"
            />
          )}
          {visibleLevels === 0 && (
            <div className="px-5 py-10 text-center text-sm text-slate-500">
              当前过滤条件下无可显示的关键位。
            </div>
          )}
        </div>

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

function LevelSection({
  title,
  levels,
  coin,
  price,
  accent,
}: {
  title: string;
  levels: KeyLevelV2[];
  coin: string;
  price: number;
  accent: "support" | "resistance";
}) {
  const accentTone =
    accent === "support" ? "text-green-400" : "text-red-400";
  return (
    <div className="border-t border-slate-800/60 first:border-t-0">
      <div className="px-5 py-2 bg-slate-800/20 flex items-center gap-2">
        <span className={`w-1.5 h-1.5 rounded-full ${accent === "support" ? "bg-green-400" : "bg-red-400"}`} />
        <h3 className={`text-[12px] font-medium ${accentTone}`}>{title}</h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-slate-700/60 text-slate-500 text-xs">
              <th className="text-left py-2 pl-2 pr-1 w-6"></th>
              <th className="text-left py-2 pr-3">价位</th>
              <th className="text-left py-2 pr-3">类型</th>
              <th className="text-left py-2 pr-3">强度</th>
              <th className="text-left py-2 pr-3">状态</th>
              <th className="text-right py-2 pr-3">距当前</th>
              <th className="text-right py-2 pr-3">共振分</th>
              <th className="text-right py-2 pr-3">级联风险</th>
              <th className="text-left py-2 pr-3">时间框架</th>
              <th className="text-left py-2 pr-3">为什么强</th>
            </tr>
          </thead>
          <tbody>
            {levels.map((lv) => (
              <LevelDetailRow
                key={lv.level_id ?? `${lv.side}-${lv.price}`}
                lv={lv}
                coin={coin}
                price={price}
                colCount={TABLE_COL_COUNT}
              />
            ))}
          </tbody>
        </table>
      </div>
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
