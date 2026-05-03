"use client";

/**
 * 策略对比 Dashboard · 横向对照三策略表现
 *
 * 字段：总信号 / 命中数 / 命中率 / 期望收益 / 是否盈利 / 最近信号
 * 进阶维度（按 confidence / regime / hour 分桶）展开折叠
 *
 * 数据来源：store.stats（GlobalStats）—— 用户可点 "实时重算" 强刷
 */

import { useState } from "react";

import {
  BREAK_EVEN_WIN_RATE,
  REGIME_META,
  STRATEGY_META,
  wilson95,
  type GlobalStats,
  type StrategyStats,
} from "@/lib/scalpTypes";
import { useScalpStore } from "@/stores/scalpStore";
import SampleSizeBadge from "./SampleSizeBadge";

export default function StrategyComparePanel() {
  const stats = useScalpStore((s) => s.stats);
  const statsLoading = useScalpStore((s) => s.statsLoading);
  const loadStats = useScalpStore((s) => s.loadStats);

  if (!stats) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-500">
        加载统计数据...
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <ActionBar
        stats={stats}
        loading={statsLoading}
        onRecompute={() => loadStats(true)}
      />
      {stats.total_signals === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-12 text-center text-[12px] text-slate-500">
          暂无历史信号 · 至少需要 1 条结算信号才能展示统计
        </div>
      ) : (
        <>
          <SummaryRow stats={stats} />
          <StrategyRows stats={stats} />
        </>
      )}
    </div>
  );
}

function ActionBar({
  stats,
  loading,
  onRecompute,
}: {
  stats: GlobalStats;
  loading: boolean;
  onRecompute: () => void;
}) {
  return (
    <div className="flex items-center justify-between rounded-lg border border-slate-700 bg-slate-900/95 px-4 py-2.5">
      <div className="flex items-center gap-3 text-[12px]">
        <span className="font-semibold text-slate-200">策略对比</span>
        <span className="text-slate-500">|</span>
        <span className="text-slate-400">
          总信号 <span className="font-mono text-slate-200">{stats.total_signals}</span>
        </span>
        <span className="text-slate-500">|</span>
        <span className="text-slate-400">
          全局命中率：
          <span
            className="font-mono"
            style={{
              color: stats.global_win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b",
            }}
          >
            {(stats.global_win_rate * 100).toFixed(1)}%
          </span>
        </span>
        <span className="text-slate-500">|</span>
        <span className="text-[10px] text-slate-500">
          盈亏平衡：{(BREAK_EVEN_WIN_RATE * 100).toFixed(1)}% (赔率 0.8:1)
        </span>
      </div>
      <button
        onClick={onRecompute}
        disabled={loading}
        className="rounded border border-sky-700/50 bg-sky-950/30 px-3 py-1 text-[11px] text-sky-300 hover:bg-sky-900/40 disabled:opacity-50"
      >
        {loading ? "重算中..." : "🔄 实时重算"}
      </button>
    </div>
  );
}

function SummaryRow({ stats }: { stats: GlobalStats }) {
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-5">
      <KpiTile label="总信号" value={stats.total_signals.toLocaleString()} accent="#0ea5e9" />
      <KpiTile
        label="命中"
        value={stats.total_won.toLocaleString()}
        accent="#22c55e"
      />
      <KpiTile label="落空" value={stats.total_lost.toLocaleString()} accent="#ef4444" />
      <KpiTile
        label="命中率"
        value={`${(stats.global_win_rate * 100).toFixed(1)}%`}
        accent={stats.global_win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b"}
      />
      <KpiTile
        label="单次期望收益"
        value={
          stats.global_expected_return >= 0
            ? `+${(stats.global_expected_return * 100).toFixed(2)}%`
            : `${(stats.global_expected_return * 100).toFixed(2)}%`
        }
        accent={stats.global_expected_return >= 0 ? "#22c55e" : "#ef4444"}
      />
    </div>
  );
}

function KpiTile({ label, value, accent }: { label: string; value: string; accent: string }) {
  return (
    <div
      className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2"
      style={{ borderLeft: `3px solid ${accent}` }}
    >
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className="mt-0.5 text-[16px] font-semibold text-slate-100">{value}</div>
    </div>
  );
}

function StrategyRows({ stats }: { stats: GlobalStats }) {
  if (stats.by_strategy.length === 0) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-500">
        各策略均未生成信号
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {stats.by_strategy.map((row) => (
        <StrategyRowCard key={row.strategy} row={row} />
      ))}
    </div>
  );
}

function StrategyRowCard({ row }: { row: StrategyStats }) {
  const meta = STRATEGY_META[row.strategy];
  const [expanded, setExpanded] = useState(false);

  // P0-8 Wilson 95% CI（仅展示，不替代主指标）
  const decided = row.won + row.lost;
  const ci = wilson95(row.won, decided);

  return (
    <div
      className="rounded-lg border border-slate-700/60 bg-slate-900/50"
      style={{ borderLeft: `3px solid ${meta?.color ?? "#64748b"}` }}
    >
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left hover:bg-slate-800/30"
      >
        <div className="flex items-center gap-2">
          <span className="text-base">{meta?.emoji ?? "•"}</span>
          <span className="text-[13px] font-semibold text-slate-100">
            {meta?.shortCn ?? row.strategy}
          </span>
          <span className="text-[10px] text-slate-500">{meta?.displayCn}</span>
          <SampleSizeBadge n={decided} />
        </div>
        <div className="flex items-center gap-4 text-[11px]">
          <Stat label="信号" value={row.total} />
          <Stat label="命中" value={row.won} accent="#22c55e" />
          <Stat label="落空" value={row.lost} accent="#ef4444" />
          <span title={`Wilson 95% CI: [${(ci.lo * 100).toFixed(1)}%, ${(ci.hi * 100).toFixed(1)}%]`}>
            <Stat
              label="命中率"
              value={
                decided === 0
                  ? "—"
                  : `${(row.win_rate * 100).toFixed(1)}% [${(ci.lo * 100).toFixed(1)}–${(ci.hi * 100).toFixed(1)}]`
              }
              accent={row.win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b"}
            />
          </span>
          <Stat
            label="单次期望"
            value={
              row.expected_return_per_signal >= 0
                ? `+${(row.expected_return_per_signal * 100).toFixed(2)}%`
                : `${(row.expected_return_per_signal * 100).toFixed(2)}%`
            }
            accent={row.is_profitable ? "#22c55e" : "#ef4444"}
          />
          {row.shadow_total > 0 && (
            <Stat
              label="影子"
              value={
                row.shadow_win_rate !== null
                  ? `${(row.shadow_win_rate * 100).toFixed(0)}% (N=${row.shadow_total})`
                  : `(N=${row.shadow_total})`
              }
              accent="#9ca3af"
            />
          )}
          <span className="ml-2 text-[16px] text-slate-500">{expanded ? "−" : "+"}</span>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-slate-800 px-4 py-3 space-y-3">
          <SubBuckets row={row} />
          {row.shadow_total > 0 && <ShadowBreakdown row={row} />}
        </div>
      )}
    </div>
  );
}

function ShadowBreakdown({ row }: { row: StrategyStats }) {
  /** P0-4：双口径展示——cancelled 信号若不取消会怎样 */
  const kindCn: Record<string, string> = {
    regime_flip: "Regime 翻转",
    data_stale: "数据 stale",
    blackswan: "黑天鹅",
    manual: "手动",
    conflict: "冲突",
    unknown: "未知",
  };
  const breakdown = row.shadow_breakdown_by_kind;
  return (
    <div>
      <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
        Shadow 双口径（取消信号回测）
      </div>
      <div className="text-[11px] text-slate-400 mb-2">
        若不取消，命中率：
        <span
          className="font-mono ml-1"
          style={{
            color: (row.shadow_win_rate ?? 0) >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b",
          }}
        >
          {row.shadow_win_rate !== null
            ? `${(row.shadow_win_rate * 100).toFixed(1)}%`
            : "—"}
        </span>
        <span className="ml-1 text-slate-500">(N={row.shadow_total})</span>
      </div>
      <div className="flex flex-wrap gap-2 text-[10px]">
        {Object.entries(breakdown).map(([kind, n]) => (
          <span
            key={kind}
            className="rounded bg-slate-800 px-1.5 py-0.5 text-slate-300"
          >
            {kindCn[kind] ?? kind}：{n}
          </span>
        ))}
      </div>
    </div>
  );
}

function SubBuckets({ row }: { row: StrategyStats }) {
  return (
    <div className="grid gap-4 lg:grid-cols-3">
      {/* 按 confidence 分桶 */}
      <div>
        <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          按置信度分桶
        </div>
        {row.by_confidence.length === 0 ? (
          <EmptyHint />
        ) : (
          <table className="w-full text-[10px]">
            <thead className="text-slate-500">
              <tr>
                <th className="text-left">区间</th>
                <th className="text-right">N</th>
                <th className="text-right">胜率</th>
              </tr>
            </thead>
            <tbody>
              {row.by_confidence.map((b, i) => (
                <tr key={i} className="border-t border-slate-800/50">
                  <td className="py-1 font-mono text-slate-400">
                    [{b.bucket_lo}, {b.bucket_hi})
                  </td>
                  <td className="py-1 text-right text-slate-300">{b.total}</td>
                  <td
                    className="py-1 text-right font-mono"
                    style={{ color: b.win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b" }}
                  >
                    {(b.win_rate * 100).toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 按 regime 分桶 */}
      <div>
        <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          按 Regime 分桶
        </div>
        {row.by_regime.length === 0 ? (
          <EmptyHint />
        ) : (
          <table className="w-full text-[10px]">
            <thead className="text-slate-500">
              <tr>
                <th className="text-left">Regime</th>
                <th className="text-right">N</th>
                <th className="text-right">胜率</th>
              </tr>
            </thead>
            <tbody>
              {row.by_regime.map((b, i) => (
                <tr key={i} className="border-t border-slate-800/50">
                  <td
                    className="py-1"
                    style={{ color: REGIME_META[b.regime]?.color ?? "#94a3b8" }}
                  >
                    {REGIME_META[b.regime]?.displayCn ?? b.regime}
                  </td>
                  <td className="py-1 text-right text-slate-300">{b.total}</td>
                  <td
                    className="py-1 text-right font-mono"
                    style={{ color: b.win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b" }}
                  >
                    {(b.win_rate * 100).toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {/* 按小时（UTC）分布 */}
      <div>
        <div className="mb-1.5 text-[10px] uppercase tracking-wider text-slate-500">
          按 UTC 小时
        </div>
        {row.by_hour_utc.length === 0 ? (
          <EmptyHint />
        ) : (
          <HourBars items={row.by_hour_utc} />
        )}
      </div>
    </div>
  );
}

function HourBars({ items }: { items: { hour_utc: number; total: number; won: number; win_rate: number }[] }) {
  const maxTotal = Math.max(...items.map((h) => h.total), 1);
  return (
    <div className="space-y-0.5">
      {items.map((h) => {
        const pct = (h.total / maxTotal) * 100;
        const wrColor = h.win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#f59e0b";
        return (
          <div key={h.hour_utc} className="flex items-center gap-2 text-[10px]">
            <span className="w-7 shrink-0 text-right font-mono text-slate-500">
              {String(h.hour_utc).padStart(2, "0")}h
            </span>
            <div className="flex-1 overflow-hidden rounded bg-slate-800">
              <div className="h-2" style={{ width: `${pct}%`, background: wrColor }} />
            </div>
            <span className="w-12 shrink-0 text-right font-mono text-slate-300">
              {h.total}
            </span>
            <span className="w-12 shrink-0 text-right font-mono" style={{ color: wrColor }}>
              {(h.win_rate * 100).toFixed(0)}%
            </span>
          </div>
        );
      })}
    </div>
  );
}

function Stat({
  label,
  value,
  accent,
}: {
  label: string;
  value: string | number;
  accent?: string;
}) {
  return (
    <span>
      <span className="text-slate-500">{label}：</span>
      <span className="font-mono" style={{ color: accent ?? "#cbd5e1" }}>
        {value}
      </span>
    </span>
  );
}

function EmptyHint() {
  return <div className="py-2 text-[10px] text-slate-500">暂无足够样本</div>;
}
