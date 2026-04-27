"use client";

/**
 * M3 · F2 · 市场 regime chip
 * 在 KeyLevelView 顶部展示当前市场状态（来自 RegimeSnapshot）。
 * 与 KeyLevelSnapshotV2.regime 字段直接对齐（后端 score_and_build_snapshot 写入）。
 *
 * 设计原则：
 *   - 一眼看懂：颜色 + 中文短标 + 置信度条
 *   - 鼠标 hover 显示完整描述（regime_description）
 *   - 数据缺失时不渲染（避免占位浪费空间）
 */

import type { KeyLevelSnapshotV2 } from "@/lib/types";

const REGIME_META: Record<
  string,
  { label: string; emoji: string; tone: string }
> = {
  trend_up: {
    label: "趋势上行",
    emoji: "📈",
    tone: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  },
  trend_down: {
    label: "趋势下行",
    emoji: "📉",
    tone: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  },
  range: {
    label: "区间震荡",
    emoji: "↔️",
    tone: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  },
  extreme_volatility: {
    label: "极端波动",
    emoji: "⚠️",
    tone: "bg-amber-500/20 text-amber-300 border-amber-500/50",
  },
  squeeze: {
    label: "波动收敛",
    emoji: "🪢",
    tone: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  },
  high_vol_chop: {
    label: "高波震荡",
    emoji: "🌪️",
    tone: "bg-orange-500/15 text-orange-300 border-orange-500/40",
  },
};

export default function RegimeChip({ kl }: { kl: KeyLevelSnapshotV2 }) {
  const regime = kl.regime || "";
  if (!regime) return null;

  const meta = REGIME_META[regime] ?? {
    label: regime,
    emoji: "🧭",
    tone: "bg-slate-600/25 text-slate-300 border-slate-500/40",
  };
  const conf = Math.max(0, Math.min(1, kl.regime_confidence ?? 0));
  const confPct = Math.round(conf * 100);
  const confLabel =
    confPct >= 70 ? "高" : confPct >= 40 ? "中" : "低";

  return (
    <div
      className={`flex items-center gap-2 px-3 py-2 rounded-lg border ${meta.tone}`}
      title={kl.regime_description || `Regime: ${regime}`}
    >
      <span className="text-base leading-none">{meta.emoji}</span>
      <span className="text-xs font-semibold whitespace-nowrap">
        市场状态：{meta.label}
      </span>
      <div className="flex items-center gap-1.5 flex-1 min-w-[80px] max-w-[180px]">
        <div className="relative h-1 rounded-full bg-black/30 flex-1 overflow-hidden">
          <div
            className="absolute left-0 top-0 h-full bg-current opacity-70"
            style={{ width: `${confPct}%` }}
          />
        </div>
        <span className="text-[10px] opacity-80 whitespace-nowrap">
          置信度 {confPct}%（{confLabel}）
        </span>
      </div>
      {kl.regime_description && (
        <span className="text-[11px] opacity-80 truncate max-w-[280px] hidden md:inline">
          · {kl.regime_description}
        </span>
      )}
    </div>
  );
}
