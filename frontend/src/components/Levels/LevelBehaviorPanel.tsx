"use client";

/**
 * 关键位行为评估面板（V3-M1 行为验证层 · 2026-04）
 *
 * 设计纪律（与后端 BehaviorEval docstring 同步）：
 *   1. 独立成区展示，不污染原 explain_chips
 *   2. M1 阶段为纯观测：仅展示，不影响交易决策
 *   3. 旧 state machine 决定"事件是否发生"；本面板回答"事件多可信"
 *
 * 视觉布局：
 *   ┌─────────────────────────────────────────────┐
 *   │ 🧠 行为评估                                  │
 *   │ ┌───────────────────────┐                  │
 *   │ │ [真突破] 信心 78%     │  ← 主标签 + 信心  │
 *   │ ├───────────────────────┤                  │
 *   │ │ 突破有效性  ▓▓▓▓▓░ 78│  ← 4 个相关分数   │
 *   │ │ 假突破风险  ▓░░░░░ 12│  （仅显示有意义的）│
 *   │ ├───────────────────────┤                  │
 *   │ │ 放量站稳 · CVD 同向   │  ← 行为侧 chips   │
 *   │ └───────────────────────┘                  │
 *   └─────────────────────────────────────────────┘
 */

import type { BehaviorEval, BehaviorState } from "@/lib/types";

const STATE_LABELS: Record<string, string> = {
  pending: "—",
  pending_breakout: "突破逼近",
  true_breakout: "真突破",
  healthy_retest: "健康回踩",
  failed_breakout: "假突破/失败",
  heavy_volume_breakdown: "放量破位",
  capitulation_flush: "恐慌出清候选",
  confirmed_flip: "翻转确认",
  wait_for_second_test: "等二次确认",
};

/** 不同 behavior_state 的色调（与 STATE_LABELS 对齐）。 */
const STATE_BADGE: Record<string, string> = {
  pending: "bg-slate-700/40 text-slate-400",
  pending_breakout: "bg-amber-500/20 text-amber-300",
  true_breakout: "bg-emerald-500/20 text-emerald-300",
  healthy_retest: "bg-emerald-500/20 text-emerald-300",
  failed_breakout: "bg-rose-500/20 text-rose-300",
  heavy_volume_breakdown: "bg-rose-500/25 text-rose-300",
  capitulation_flush: "bg-orange-500/20 text-orange-300",
  confirmed_flip: "bg-purple-500/20 text-purple-300",
  wait_for_second_test: "bg-yellow-500/20 text-yellow-300",
};

/** 各分数的中文标签（按 lv.state 决定显哪几条）。 */
const SCORE_LABELS: Record<keyof Omit<BehaviorEval, "behavior_state" | "state_confidence" | "explain_chips" | "components_used" | "evaluated_at">, string> = {
  breakout_validity: "突破有效性",
  retest_quality: "回踩质量",
  selloff_continuation_risk: "破位延续风险",
  capitulation_bottom_score: "恐慌出清候选",
  flip_confirmation: "翻转确认度",
  false_break_risk: "假突破风险",
};

/** 仅显示与当前 state 相关的分数（避免无意义的 0 分线）。 */
function pickRelevantScores(b: BehaviorEval, state: string): Array<[string, number]> {
  const out: Array<[string, number]> = [];
  const push = (k: keyof typeof SCORE_LABELS) => {
    const v = (b[k] ?? 0) as number;
    out.push([SCORE_LABELS[k], v]);
  };
  switch (state) {
    case "broken":
      push("breakout_validity");
      push("false_break_risk");
      push("selloff_continuation_risk");
      push("capitulation_bottom_score");
      break;
    case "flipped":
      push("flip_confirmation");
      push("breakout_validity");
      push("retest_quality");
      break;
    case "bounced":
      push("retest_quality");
      break;
    case "fake_break":
      push("false_break_risk");
      break;
    case "testing":
      push("breakout_validity");
      push("false_break_risk");
      break;
    default:
      // idle / approaching / swept：暂不展示分数（components_used 为空）
      break;
  }
  return out;
}

/** 风险类分数（risk 越高越红），其它分数（quality 越高越绿）。 */
function isRiskScore(label: string): boolean {
  return label.includes("风险");
}

function ScoreBar({ label, value }: { label: string; value: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  const isRisk = isRiskScore(label);
  // risk 高 = 红；quality 高 = 绿
  let color = "bg-slate-500";
  if (isRisk) {
    color = pct > 65 ? "bg-rose-500" : pct > 35 ? "bg-amber-500" : "bg-slate-500";
  } else {
    color = pct > 65 ? "bg-emerald-500" : pct > 35 ? "bg-amber-500" : "bg-slate-500";
  }
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="text-slate-500 w-24 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div
          className={`h-full ${color} rounded-full transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-slate-400 font-mono w-9 text-right text-[10px]">
        {pct}
      </span>
    </div>
  );
}

export default function LevelBehaviorPanel({
  behavior,
  state,
}: {
  behavior: BehaviorEval | null | undefined;
  state: string;
}) {
  if (!behavior) return null;
  // pending 且无任何分数 → 不显示（避免空区干扰）
  const scores = pickRelevantScores(behavior, state);
  const hasAnyScore = scores.some(([, v]) => v > 0);
  if (behavior.behavior_state === "pending" && !hasAnyScore && (behavior.explain_chips ?? []).length === 0) {
    return null;
  }

  const stateLabel = STATE_LABELS[behavior.behavior_state] ?? behavior.behavior_state;
  const badgeCls = STATE_BADGE[behavior.behavior_state] ?? STATE_BADGE.pending;
  const confidence = Math.max(0, Math.min(1, behavior.state_confidence ?? 0));
  const confidencePct = Math.round(confidence * 100);

  return (
    <div className="rounded-md border border-slate-700/40 bg-slate-900/40 p-3 text-xs">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] text-slate-400 flex items-center gap-2">
          <span>🧠</span>
          <span>行为评估</span>
          <span className="text-[9px] text-slate-600">观测期 · 不影响信号</span>
        </div>
        <span
          className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${badgeCls}`}
          title={`behavior_state=${behavior.behavior_state}`}
        >
          {stateLabel}
        </span>
      </div>

      {/* 信心度 */}
      {confidence > 0 && (
        <div className="flex items-center gap-2 text-[11px] mb-2">
          <span className="text-slate-500 w-24 shrink-0">状态信心度</span>
          <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
            <div
              className="h-full bg-sky-500 rounded-full transition-all"
              style={{ width: `${confidencePct}%` }}
            />
          </div>
          <span className="text-slate-300 font-mono w-9 text-right text-[10px]">
            {confidencePct}
          </span>
        </div>
      )}

      {/* 相关分数 mini bars */}
      {hasAnyScore && (
        <div className="space-y-1.5 mb-2">
          {scores.map(([label, value]) => (
            <ScoreBar key={label} label={label} value={value} />
          ))}
        </div>
      )}

      {/* 行为侧 chips */}
      {behavior.explain_chips && behavior.explain_chips.length > 0 && (
        <div className="flex flex-wrap gap-1 pt-1 border-t border-slate-700/30">
          {behavior.explain_chips.map((chip) => (
            <span
              key={chip}
              className="px-1.5 py-0.5 bg-slate-800/60 text-slate-300 rounded text-[10px]"
            >
              {chip}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
