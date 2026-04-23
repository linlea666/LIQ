"use client";

/**
 * SignalHistory · 最近 N 次评估记录时间线
 *
 * 目的：消除"引擎到底跑没跑"的疑虑 —— 直观展示每一轮评估的 action/urgency/
 * confidence/distance_to_liq 随时间的变化。
 *
 * 数据源：
 *   - 首屏：useEffect 调 refreshPositionSignalHistory 拉 REST
 *   - 实时：WS 推送会在 rollStore.applySignal 时自动 append 到 ring buffer
 *
 * 容量：与后端对齐（60 条），刚好覆盖约 10 分钟评估窗口。
 */

import { useEffect, useMemo } from "react";

import type { RollAction, RollSignal, Urgency } from "@/lib/rollTypes";
import { useRollStore } from "@/stores/rollStore";

interface Props {
  positionId: string;
}

// 动作到缩写/颜色的静态映射（与 pipeline 条色系对齐）
const ACTION_META: Record<
  RollAction,
  { short: string; label: string; cls: string }
> = {
  add: { short: "A", label: "加仓", cls: "bg-emerald-900/70 text-emerald-200 border-emerald-700/70" },
  reduce: { short: "R", label: "减仓", cls: "bg-amber-900/70 text-amber-200 border-amber-700/70" },
  close: { short: "X", label: "离场", cls: "bg-rose-900/70 text-rose-200 border-rose-700/70" },
  move_sl: { short: "S", label: "移止损", cls: "bg-sky-900/70 text-sky-200 border-sky-700/70" },
  hold: { short: "·", label: "观望", cls: "bg-slate-900/70 text-slate-400 border-slate-700/70" },
};

const URGENCY_RING: Record<Urgency, string> = {
  info: "",
  attention: "ring-1 ring-amber-500/60",
  urgent: "ring-1 ring-rose-500/80",
};

function formatClock(ts: number): string {
  if (!ts || !Number.isFinite(ts)) return "—";
  try {
    return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
  } catch {
    return "—";
  }
}

export default function SignalHistory({ positionId }: Props) {
  const history = useRollStore(
    (s) => s.signalHistoryByPosition[positionId] || [],
  );
  const refresh = useRollStore((s) => s.refreshPositionSignalHistory);

  useEffect(() => {
    if (positionId) refresh(positionId, 60);
  }, [positionId, refresh]);

  const items = useMemo(() => history.slice(-60), [history]);

  if (items.length === 0) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/40">
        <header className="border-b border-slate-800 px-4 py-2 text-[12px] font-semibold text-slate-300">
          最近评估时间线
        </header>
        <div className="px-4 py-6 text-center text-[11px] text-slate-500">
          尚无历史记录 · 引擎下一评估周期生成后自动填充
        </div>
      </section>
    );
  }

  // 统计：用于右上角摘要
  const counts = items.reduce<Record<RollAction, number>>(
    (acc, s) => {
      acc[s.action] = (acc[s.action] || 0) + 1;
      return acc;
    },
    { add: 0, reduce: 0, close: 0, hold: 0, move_sl: 0 },
  );

  const first = items[0];
  const last = items[items.length - 1];
  const spanSec = Math.max(0, last.ts - first.ts);
  const spanText =
    spanSec >= 60
      ? `${Math.round(spanSec / 60)} 分钟`
      : `${spanSec} 秒`;

  // 距爆仓百分比序列（缺失用 null 占位），用作趋势行的 sparkline
  const liqSeries = items.map((s) => s.distance_to_liq_pct);
  const validLiq = liqSeries.filter((v): v is number => v != null);
  const liqMin = validLiq.length ? Math.min(...validLiq) : null;
  const liqMax = validLiq.length ? Math.max(...validLiq) : null;

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-800 px-4 py-2 text-[12px]">
        <span className="font-semibold text-slate-300">最近评估时间线</span>
        <span className="text-[10.5px] text-slate-500">
          {items.length} 条 · 跨度 {spanText} ·
          {" "}
          <span className="text-emerald-300">A{counts.add}</span>{" "}
          <span className="text-amber-300">R{counts.reduce}</span>{" "}
          <span className="text-rose-300">X{counts.close}</span>{" "}
          <span className="text-sky-300">S{counts.move_sl}</span>{" "}
          <span className="text-slate-500">·{counts.hold}</span>
        </span>
      </header>

      {/* 行动条：每个单元格是一次评估 */}
      <div className="px-4 py-3">
        <div className="flex flex-wrap gap-[3px]">
          {items.map((s, i) => {
            const meta = ACTION_META[s.action];
            const ringCls = URGENCY_RING[s.urgency];
            const tip =
              `${formatClock(s.ts)} · ${meta.label}(${s.urgency})` +
              (s.headline_cn ? `\n${s.headline_cn}` : "") +
              `\n加仓分 ${s.confidence_score.toFixed(1)} · 减仓分 ${s.reduce_confidence.toFixed(1)}` +
              (s.distance_to_liq_pct != null
                ? `\n距爆仓 ${s.distance_to_liq_pct.toFixed(2)}%`
                : "");
            return (
              <span
                key={`${s.ts}-${i}`}
                title={tip}
                className={[
                  "inline-flex h-5 w-5 items-center justify-center rounded border text-[10px] font-mono",
                  meta.cls,
                  ringCls,
                ].join(" ")}
              >
                {meta.short}
              </span>
            );
          })}
        </div>

        {/* 距爆仓迷你折线：sparkline */}
        {liqMin != null && liqMax != null && (
          <div className="mt-3">
            <div className="mb-1 flex items-center justify-between text-[10.5px] text-slate-500">
              <span>距爆仓 %（时间线 · sparkline）</span>
              <span className="font-mono">
                min {liqMin.toFixed(2)} · max {liqMax.toFixed(2)} · 当前{" "}
                {last.distance_to_liq_pct != null
                  ? `${last.distance_to_liq_pct.toFixed(2)}`
                  : "—"}
              </span>
            </div>
            <Sparkline series={liqSeries} min={liqMin} max={liqMax} />
          </div>
        )}
      </div>
    </section>
  );
}

function Sparkline({
  series,
  min,
  max,
}: {
  series: (number | null)[];
  min: number;
  max: number;
}) {
  const width = 400;
  const height = 36;
  const n = series.length;
  if (n < 2) return null;
  const range = Math.max(0.01, max - min);

  const points = series
    .map((v, i) => {
      if (v == null) return null;
      const x = (i / (n - 1)) * width;
      const y = height - ((v - min) / range) * (height - 4) - 2;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .filter((p): p is string => p !== null)
    .join(" ");

  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-9 w-full rounded bg-slate-900/60"
      preserveAspectRatio="none"
    >
      <polyline
        points={points}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.5}
        className="text-sky-400"
      />
    </svg>
  );
}
