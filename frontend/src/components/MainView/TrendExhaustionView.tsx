"use client";

/**
 * TrendExhaustionView · 趋势动能 / 衰竭 / 反转侦测
 *
 * 设计原则（与后端 trend_exhaustion.py 一致）：
 *   - 不预测具体点位，只显示"续航 / 衰减 / 衰竭 / 反转 / 观望"5 档状态。
 *   - 小白优先看：顶部大状态徽章 + 一句白话 + 建议动作；不被分数淹没。
 *   - 进阶 / 专业可展开三周期（1h / 4h / 1d）三维雷达式分数。
 *   - 样本不足时如实显示"观望，数据未齐"，不装懂。
 */

import { useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import type {
  TEConsensusLevel,
  TEExhaustionState,
  TEOverallAction,
  TrendExhaustionState,
} from "@/lib/types";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 枚举 → 中文 & 配色
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const STATE_MAP: Record<
  TEExhaustionState,
  { label: string; color: string; bg: string; emoji: string; desc: string }
> = {
  healthy_continuation: {
    label: "健康续航",
    color: "text-green-300",
    bg: "bg-green-500/10 border-green-500/40",
    emoji: "▲",
    desc: "趋势还在推进，动能和资金都在跟",
  },
  momentum_fading: {
    label: "动能减速",
    color: "text-amber-300",
    bg: "bg-amber-500/10 border-amber-500/40",
    emoji: "◆",
    desc: "推进变慢了，但还没明确反转，保守为主",
  },
  exhaustion_warn: {
    label: "衰竭警戒",
    color: "text-orange-300",
    bg: "bg-orange-500/10 border-orange-500/40",
    emoji: "▼",
    desc: "多个衰竭信号共振，该平仓或反手了",
  },
  structural_reversal: {
    label: "结构反转",
    color: "text-red-300",
    bg: "bg-red-500/10 border-red-500/40",
    emoji: "⇋",
    desc: "方向已经切换，顺新方向做",
  },
  neutral: {
    label: "观望",
    color: "text-slate-400",
    bg: "bg-slate-500/10 border-slate-500/40",
    emoji: "·",
    desc: "暂无明显方向或样本不足",
  },
};

const CONSENSUS_MAP: Record<
  TEConsensusLevel,
  { label: string; color: string }
> = {
  strong_agree: { label: "MTF 强共振", color: "text-cyan-300" },
  partial: { label: "部分一致", color: "text-amber-300" },
  conflict: { label: "MTF 分歧", color: "text-red-300" },
  neutral: { label: "信号中性", color: "text-slate-400" },
};

const ACTION_MAP: Record<TEOverallAction, { label: string; color: string }> = {
  add: { label: "可顺势加仓", color: "text-green-300" },
  hold: { label: "持仓不动", color: "text-cyan-300" },
  reduce: { label: "建议减仓", color: "text-amber-300" },
  close: { label: "建议平仓", color: "text-orange-300" },
  counter_small: { label: "小仓逆势试错", color: "text-purple-300" },
  counter_main: { label: "反手主方向", color: "text-red-300" },
  stand_aside: { label: "空仓观望", color: "text-slate-400" },
};

const TF_LABEL: Record<string, string> = {
  "1h": "1小时",
  "4h": "4小时",
  "1d": "日线",
};

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 小组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function scoreColor(score: number): string {
  if (score > 0.3) return "text-green-300";
  if (score > 0.1) return "text-green-400/70";
  if (score < -0.3) return "text-red-300";
  if (score < -0.1) return "text-orange-300";
  return "text-slate-400";
}

function ScoreBar({ score }: { score: number }) {
  // score ∈ [-1, 1]，居中 0 往两侧延伸
  const pct = Math.min(100, Math.abs(score) * 100);
  const positive = score >= 0;
  return (
    <div className="relative h-2 w-full bg-slate-800 rounded overflow-hidden">
      <div className="absolute left-1/2 top-0 bottom-0 w-px bg-slate-600" />
      <div
        className={`absolute top-0 bottom-0 ${
          positive ? "bg-green-400/60 left-1/2" : "bg-red-400/60 right-1/2"
        }`}
        style={{ width: `${pct / 2}%` }}
      />
    </div>
  );
}

function TFCard({
  tf,
  state,
  expanded,
  onToggle,
}: {
  tf: TrendExhaustionState;
  expanded: boolean;
  onToggle: () => void;
  state: TrendExhaustionState;
}) {
  void tf; // 仅为语义明确，实际读 state.tf
  const meta = STATE_MAP[state.state];
  return (
    <div className={`border rounded-lg p-3 ${meta.bg}`}>
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="text-xs font-mono text-slate-400">
            {TF_LABEL[state.tf] ?? state.tf}
          </span>
          <span className={`text-sm font-semibold ${meta.color}`}>
            {meta.emoji} {meta.label}
          </span>
          {state.state_age_min > 0 && (
            <span className="text-[10px] text-slate-500">
              · {state.state_age_min}m
            </span>
          )}
        </div>
        <div className="flex items-center gap-3">
          <span className={`text-sm font-mono ${scoreColor(state.composite_score)}`}>
            {state.composite_score >= 0 ? "+" : ""}
            {state.composite_score.toFixed(2)}
          </span>
          <button
            onClick={onToggle}
            className="text-[10px] text-slate-400 hover:text-slate-200"
          >
            {expanded ? "收起" : "展开"}
          </button>
        </div>
      </div>
      <div className="mt-1 text-xs text-slate-300">{state.reason_cn}</div>

      {/* 三维分数条 */}
      <div className="mt-2 grid grid-cols-3 gap-2 text-[10px]">
        {[
          { k: "动能", v: state.momentum_score },
          { k: "参与", v: state.participation_score },
          { k: "衰竭", v: state.exhaustion_score },
        ].map((d) => (
          <div key={d.k}>
            <div className="flex justify-between">
              <span className="text-slate-500">{d.k}</span>
              <span className={`font-mono ${scoreColor(d.v)}`}>
                {d.v >= 0 ? "+" : ""}
                {d.v.toFixed(2)}
              </span>
            </div>
            <ScoreBar score={d.v} />
          </div>
        ))}
      </div>

      {/* 展开后显示子项 */}
      {expanded && state.sub_scores.length > 0 && (
        <div className="mt-3 pt-2 border-t border-slate-700/60 space-y-1 text-xs">
          {state.sub_scores.map((s) => (
            <div key={s.key} className="flex justify-between">
              <div className="flex-1 min-w-0">
                <span className="text-slate-400">{s.name}</span>
                <span className="text-slate-500 ml-2 truncate">{s.note}</span>
              </div>
              <span className={`font-mono ml-2 ${scoreColor(s.score)}`}>
                {s.score >= 0 ? "+" : ""}
                {s.score.toFixed(2)}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 主组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export default function TrendExhaustionView() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[coin]);
  const displayMode = useMarketStore((s) => s.displayMode);
  const te = data?.trend_exhaustion;

  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const toggle = (tf: string) =>
    setExpanded((e) => ({ ...e, [tf]: !e[tf] }));

  const tfCards = useMemo(() => {
    if (!te) return [];
    const list: TrendExhaustionState[] = [];
    // 顺序：4h 中枢放中间以突出 MTF 主方向 → 1h → 1d
    if (te.tf_1h) list.push(te.tf_1h);
    if (te.tf_4h) list.push(te.tf_4h);
    if (te.tf_1d) list.push(te.tf_1d);
    return list;
  }, [te]);

  if (!te) {
    return (
      <div className="flex items-center justify-center h-48 text-slate-500 text-sm">
        等待动能数据…
      </div>
    );
  }

  const overallMeta = STATE_MAP[te.overall_state];
  const consensusMeta = CONSENSUS_MAP[te.consensus_level];
  const actionMeta = ACTION_MAP[te.overall_action];

  return (
    <div className="space-y-4">
      {/* ── 顶部：小白一眼看懂区 ─────────────────────────────── */}
      <div
        className={`rounded-xl border-2 p-4 ${overallMeta.bg} transition-all`}
      >
        <div className="flex items-center justify-between mb-2">
          <div className="flex items-center gap-3">
            <span className={`text-3xl ${overallMeta.color}`}>
              {overallMeta.emoji}
            </span>
            <div>
              <div className={`text-xl font-bold ${overallMeta.color}`}>
                {overallMeta.label}
              </div>
              <div className="text-xs text-slate-400 mt-0.5">
                {overallMeta.desc}
              </div>
            </div>
          </div>
          <div className="text-right">
            <div className={`text-sm font-semibold ${actionMeta.color}`}>
              {actionMeta.label}
            </div>
            {te.overall_position_pct > 0 && (
              <div className="text-[10px] text-slate-500 mt-0.5">
                参考仓位 {Math.round(te.overall_position_pct * 100)}%
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2 text-xs">
          <span className={`px-2 py-0.5 rounded ${consensusMeta.color} bg-slate-800/60`}>
            {consensusMeta.label}
          </span>
          <span className="text-slate-500">·</span>
          <span className="text-slate-400 truncate">{te.overall_reason_cn}</span>
        </div>
        {te.data_quality === "insufficient" && (
          <div className="mt-2 text-[11px] text-amber-300/80">
            ⚠ 数据未齐（{te.missing_inputs.join(", ") || "样本累积中"}），当前仅作参考
          </div>
        )}
      </div>

      {/* ── 中部：三周期对比（进阶/专业模式下默认展示） ─────────── */}
      {(displayMode === "pro" || tfCards.length > 0) && (
        <div className="space-y-2">
          <div className="flex items-center justify-between">
            <div className="text-xs text-slate-500">三周期分解</div>
            {displayMode === "beginner" && (
              <div className="text-[10px] text-slate-600">（进阶内容，看不懂可忽略）</div>
            )}
          </div>
          <div className="grid grid-cols-1 gap-2">
            {tfCards.map((s) => (
              <TFCard
                key={s.tf}
                tf={s}
                state={s}
                expanded={!!expanded[s.tf]}
                onToggle={() => toggle(s.tf)}
              />
            ))}
          </div>
        </div>
      )}

      {/* ── 底部：方法论注释（防止分数崇拜） ─────────────────── */}
      <div className="text-[11px] text-slate-600 border-t border-slate-800 pt-2 leading-relaxed">
        方法论：三维加权（动能 40% + 参与度 30% + 衰竭 30%）× 三周期共识。
        不预测具体顶底点位，只回答「续航还是衰竭」。
        共识级别比单周期分数更重要，冲突时一律建议观望。
      </div>
    </div>
  );
}
