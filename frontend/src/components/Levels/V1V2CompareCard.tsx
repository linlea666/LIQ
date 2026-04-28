"use client";

/**
 * V1 vs V2 单维度对比卡片（V3-M3 · 2026-04）
 *
 * 数据流：
 *   parent fetch /api/key-levels/v1v2-stats/{coin} → ComparisonStats →
 *   本卡片：双柱对比 + 显著性 + 混淆矩阵
 *
 * 设计：
 *   - 三种视觉状态：
 *     · 样本不足 (n<30)：灰色卡片 + "需更多数据"提示
 *     · V2 显著优于 V1：绿色高亮 + ✅ 标记
 *     · V1/V2 持平或 V2 较差：中性显示
 *   - 4 项指标横向并列（accuracy / precision / recall / f1），每项双柱
 *   - 卡片底部展开"混淆矩阵"以满足审计需求
 */

import { useState } from "react";
import type { ComparisonStats } from "@/lib/types";

const DIMENSION_TITLE: Record<string, string> = {
  bounce_quality: "反弹质量",
  breakout_stage: "突破阶段",
  fake_break: "假破回收",
};

const DIMENSION_DESC: Record<string, string> = {
  bounce_quality:
    "V1：proactive/passive 死阈值分类  ·  V2：z-score / percentile 自适应 0-1 连续",
  breakout_stage:
    "V1：固定 15min/1.5h 时间窗  ·  V2：按 timeframe 自适应缩放（1D=24×、1W=168×）",
  fake_break:
    "V1：state==fake_break 布尔事件  ·  V2：长影线 + 双根确认 0-1 连续",
};

const MIN_SAMPLES_TRUSTED = 30;

function MetricBar({
  label,
  v1,
  v2,
  format = "pct",
}: {
  label: string;
  v1: number;
  v2: number;
  format?: "pct" | "raw";
}) {
  const v1Pct = format === "pct" ? Math.round(v1 * 100) : v1;
  const v2Pct = format === "pct" ? Math.round(v2 * 100) : v2;
  const better = v2 > v1 + 0.005;
  const worse = v2 < v1 - 0.005;

  return (
    <div className="space-y-1">
      <div className="flex items-center justify-between text-[11px]">
        <span className="text-slate-400">{label}</span>
        <span
          className={`font-mono ${
            better ? "text-emerald-300" : worse ? "text-rose-300" : "text-slate-400"
          }`}
        >
          {format === "pct" ? `${(v2 - v1 >= 0 ? "+" : "")}${((v2 - v1) * 100).toFixed(1)}%` : (v2 - v1).toFixed(3)}
        </span>
      </div>
      {/* V1 bar */}
      <div className="flex items-center gap-2 text-[10px]">
        <span className="text-slate-600 w-6 shrink-0">V1</span>
        <div className="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
          <div
            className="h-full bg-slate-500 rounded-full"
            style={{ width: `${Math.min(100, v1Pct)}%` }}
          />
        </div>
        <span className="text-slate-400 font-mono w-10 text-right">
          {format === "pct" ? `${v1Pct}%` : v1.toFixed(3)}
        </span>
      </div>
      {/* V2 bar */}
      <div className="flex items-center gap-2 text-[10px]">
        <span className="text-slate-600 w-6 shrink-0">V2</span>
        <div className="flex-1 h-2 bg-slate-800 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full ${
              better ? "bg-emerald-500" : worse ? "bg-rose-500" : "bg-sky-500"
            }`}
            style={{ width: `${Math.min(100, v2Pct)}%` }}
          />
        </div>
        <span
          className={`font-mono w-10 text-right ${
            better ? "text-emerald-300" : worse ? "text-rose-300" : "text-slate-300"
          }`}
        >
          {format === "pct" ? `${v2Pct}%` : v2.toFixed(3)}
        </span>
      </div>
    </div>
  );
}

function ConfusionGrid({ cm }: { cm: ComparisonStats["v1"] }) {
  return (
    <div className="grid grid-cols-2 gap-1 text-[10px] text-slate-400">
      <div className="bg-emerald-900/20 px-2 py-1 rounded">
        TP <span className="font-mono text-slate-200">{cm.tp}</span>
      </div>
      <div className="bg-rose-900/20 px-2 py-1 rounded">
        FP <span className="font-mono text-slate-200">{cm.fp}</span>
      </div>
      <div className="bg-rose-900/20 px-2 py-1 rounded">
        FN <span className="font-mono text-slate-200">{cm.fn}</span>
      </div>
      <div className="bg-emerald-900/20 px-2 py-1 rounded">
        TN <span className="font-mono text-slate-200">{cm.tn}</span>
      </div>
    </div>
  );
}

export default function V1V2CompareCard({ stats }: { stats: ComparisonStats }) {
  const [showCM, setShowCM] = useState(false);

  const dim = stats.dimension as string;
  const title = DIMENSION_TITLE[dim] ?? dim;
  const desc = DIMENSION_DESC[dim] ?? "";
  const trusted = stats.sample_size >= MIN_SAMPLES_TRUSTED;
  const sigBetter = stats.is_v2_significantly_better;

  // 判定卡片状态色
  let borderCls = "border-slate-700/40";
  let badge: { text: string; cls: string } = {
    text: "观察中",
    cls: "bg-slate-700/40 text-slate-400",
  };
  if (!trusted) {
    badge = { text: `样本不足 (n=${stats.sample_size}<${MIN_SAMPLES_TRUSTED})`, cls: "bg-slate-700/40 text-slate-500" };
  } else if (sigBetter) {
    borderCls = "border-emerald-600/40";
    badge = { text: "✅ V2 显著优于 V1", cls: "bg-emerald-500/20 text-emerald-300" };
  } else if (stats.delta_accuracy < -0.02) {
    borderCls = "border-rose-700/40";
    badge = { text: "❌ V2 较差，保留 V1", cls: "bg-rose-500/20 text-rose-300" };
  } else {
    badge = { text: "➖ 无显著差异", cls: "bg-amber-500/15 text-amber-300" };
  }

  return (
    <div className={`rounded-lg border ${borderCls} bg-slate-900/40 p-4`}>
      <div className="flex items-start justify-between mb-3 gap-3">
        <div>
          <h3 className="text-sm font-semibold text-white">{title}</h3>
          <p className="text-[11px] text-slate-500 mt-0.5 leading-tight">{desc}</p>
        </div>
        <span
          className={`px-2 py-0.5 rounded-full text-[10px] font-medium whitespace-nowrap ${badge.cls}`}
        >
          {badge.text}
        </span>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-3 mb-3">
        <MetricBar label="Accuracy" v1={stats.v1.accuracy} v2={stats.v2.accuracy} />
        <MetricBar label="F1" v1={stats.v1.f1} v2={stats.v2.f1} />
        <MetricBar label="Precision" v1={stats.v1.precision} v2={stats.v2.precision} />
        <MetricBar label="Recall" v1={stats.v1.recall} v2={stats.v2.recall} />
      </div>

      <div className="flex items-center justify-between text-[10px] text-slate-500 pt-2 border-t border-slate-700/30">
        <span>
          样本 <span className="text-slate-300 font-mono">{stats.sample_size}</span>
          {stats.ambiguous_count > 0 && (
            <span className="ml-1 text-slate-600">
              · 剔除模糊 {stats.ambiguous_count}
            </span>
          )}
        </span>
        <span>
          χ² ={" "}
          <span className="text-slate-300 font-mono">
            {stats.chi_square_stat.toFixed(2)}
          </span>{" "}
          · p ={" "}
          <span
            className={`font-mono ${
              stats.chi_square_p_value < 0.05 ? "text-emerald-300" : "text-slate-400"
            }`}
          >
            {stats.chi_square_p_value.toFixed(4)}
          </span>
        </span>
        <button
          type="button"
          onClick={() => setShowCM((s) => !s)}
          className="text-slate-500 hover:text-slate-300 transition"
        >
          {showCM ? "收起" : "混淆矩阵"}
        </button>
      </div>

      {showCM && (
        <div className="mt-2 pt-2 border-t border-slate-700/30 space-y-2">
          <div>
            <div className="text-[10px] text-slate-500 mb-1">V1 混淆矩阵</div>
            <ConfusionGrid cm={stats.v1} />
          </div>
          <div>
            <div className="text-[10px] text-slate-500 mb-1">V2 混淆矩阵</div>
            <ConfusionGrid cm={stats.v2} />
          </div>
        </div>
      )}
    </div>
  );
}
