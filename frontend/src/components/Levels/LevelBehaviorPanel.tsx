"use client";

/**
 * 关键位行为评估面板（V3-M1/M2.5 行为验证层 · 2026-04）
 *
 * 设计纪律（与后端 BehaviorEval docstring 同步）：
 *   1. 独立成区展示，不污染原 explain_chips
 *   2. M1/M2.5 阶段为纯观测：仅展示，不影响交易决策
 *   3. 旧 state machine 决定"事件是否发生"；本面板回答"事件多可信"
 *   4. M2.5 双轨：旧 4 函数（生产中）vs 新 V2 增强函数（影子）并列对比
 *      数据驱动 → M3 回测决定切换；切换前**两条路径都不动**
 *
 * 视觉布局：
 *   ┌─────────────────────────────────────────────┐
 *   │ 🧠 行为评估                                  │
 *   │ [真突破] 信心 78%                            │
 *   │ 状态信心度 ▓▓▓▓▓░ 78                         │
 *   │ ─────────────────────────                   │
 *   │ ⚖ V1 vs V2 双轨（M2.5 观测）                 │
 *   │  反弹质量    V1: 主动     V2: ▓▓▓░░ 64       │
 *   │  突破阶段    V1: stage 2  V2: stage 2        │
 *   │ ─────────────────────────                   │
 *   │ 突破有效性 ▓▓▓▓▓░ 78  ← 6 大行为分           │
 *   │ ─────────────────────────                   │
 *   │ ⚠ 冲突预警: V1 主动 / V2 被动（建议关注）     │
 *   │ ─────────────────────────                   │
 *   │ [chip] [chip] ...                           │
 *   └─────────────────────────────────────────────┘
 */

import { useState } from "react";
import type { BehaviorEval } from "@/lib/types";

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

/** 6 大行为分的中文标签（按 lv.state 决定显哪几条）。
 *  M2.5 新增的 V2 影子字段不在此 map 中——它们由独立"V1 vs V2 双轨"块渲染。
 */
type CoreScoreKey =
  | "breakout_validity"
  | "retest_quality"
  | "selloff_continuation_risk"
  | "capitulation_bottom_score"
  | "flip_confirmation"
  | "false_break_risk";

const SCORE_LABELS: Record<CoreScoreKey, string> = {
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

/** V1 反弹质量分类→中文标签 + 颜色（与旧 levelBrief.bounceQualityBrief 对齐）。 */
function v1BounceLabel(bq: string | undefined): { text: string; cls: string } | null {
  if (!bq) return null;
  if (bq === "proactive") return { text: "主动", cls: "text-emerald-300" };
  if (bq === "passive") return { text: "被动", cls: "text-amber-300" };
  return null;
}

/** V1 突破阶段（0/1/2/3）→ 中文，与旧 levelBrief.breakoutStageBrief 对齐。 */
function v1BreakoutStageLabel(stage: number | undefined): { text: string; cls: string } | null {
  if (stage === undefined || stage === null || stage === 0) return null;
  if (stage === 1) return { text: "stage 1（禁追）", cls: "text-rose-300" };
  if (stage === 2) return { text: "stage 2（观察）", cls: "text-amber-300" };
  if (stage === 3) return { text: "stage 3（已确认）", cls: "text-emerald-300" };
  return { text: `stage ${stage}`, cls: "text-slate-300" };
}

/** V2 0-1 → 主动/被动 中文（仅展示用，并不让 V2 反推 V1 分类）。 */
function v2BounceQualityHint(score: number): string {
  if (score >= 0.65) return "强主动";
  if (score >= 0.40) return "偏主动";
  if (score >= 0.20) return "偏被动";
  return "弱被动";
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
  v1,
}: {
  behavior: BehaviorEval | null | undefined;
  state: string;
  /** M2.5 双轨：来自 KeyLevelV2 的 V1 字段（用于与 V2 影子值对比） */
  v1?: {
    bounce_quality?: string;
    breakout_stage?: number;
  };
}) {
  // M3.1：普通/专家模式分层（默认普通）
  const [expert, setExpert] = useState(false);

  if (!behavior) return null;

  // M3.1：未评估 / 评估失败的特殊态
  const evalAvailable = behavior.behavior_eval_available ?? true;
  const inputQuality = behavior.input_quality ?? "ok";
  const missingInputs = behavior.missing_inputs ?? [];
  const evaluatorError = behavior.evaluator_error ?? "";

  const scores = pickRelevantScores(behavior, state);
  const hasAnyScore = scores.some(([, v]) => v > 0);
  const contradictions = behavior.contradiction_with_state ?? [];

  // M2.5 双轨：判断是否有 V1/V2 对照可显示
  const v1Bounce = v1BounceLabel(v1?.bounce_quality);
  const v2BounceScore = behavior.bounce_quality_enhanced ?? 0;
  const showBounceCompare = v1Bounce !== null || v2BounceScore > 0;
  const v1Stage = v1BreakoutStageLabel(v1?.breakout_stage);
  const v2Stage = v1BreakoutStageLabel(behavior.breakout_stage_enhanced);
  const showStageCompare = v1Stage !== null || v2Stage !== null;
  const fakeBreakStrength = behavior.fake_break_strength ?? 0;
  const showFakeBreak = fakeBreakStrength > 0;
  const dynBreakDepth = behavior.dynamic_break_depth_pct ?? 0;
  const showDynDepth = dynBreakDepth > 0 && state === "broken";
  const hasDualTrack = showBounceCompare || showStageCompare || showFakeBreak || showDynDepth;

  // pending 且无分数 + 无双轨 + 无冲突 + 无 chips + 已评估 → 整体不显示
  if (
    evalAvailable
    && behavior.behavior_state === "pending"
    && !hasAnyScore
    && !hasDualTrack
    && contradictions.length === 0
    && (behavior.explain_chips ?? []).length === 0
  ) {
    return null;
  }

  const stateLabel = STATE_LABELS[behavior.behavior_state] ?? behavior.behavior_state;
  const badgeCls = STATE_BADGE[behavior.behavior_state] ?? STATE_BADGE.pending;
  const confidence = Math.max(0, Math.min(1, behavior.state_confidence ?? 0));
  const confidencePct = Math.round(confidence * 100);

  // M3.1：未评估时只显示一个低调"未评估"卡片，不渲染分数
  if (!evalAvailable) {
    return (
      <div className="rounded-md border border-slate-700/30 bg-slate-900/30 p-2 text-[10px] text-slate-500">
        <div className="flex items-center gap-2">
          <span>🧠 行为评估</span>
          <span className="px-1.5 py-0.5 bg-slate-700/40 rounded text-slate-400">
            未评估
          </span>
          {missingInputs.length > 0 && (
            <span className="text-slate-600">缺：{missingInputs.join("/")}</span>
          )}
          {evaluatorError && (
            <span
              className="text-rose-400 truncate max-w-[180px]"
              title={evaluatorError}
            >
              ⚠ {evaluatorError}
            </span>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="rounded-md border border-slate-700/40 bg-slate-900/40 p-3 text-xs">
      <div className="flex items-center justify-between mb-2">
        <div className="text-[11px] text-slate-400 flex items-center gap-2">
          <span>🧠</span>
          <span>行为评估</span>
          <span className="text-[9px] text-slate-600">观测期 · 不影响信号</span>
          {inputQuality !== "ok" && (
            <span
              className="text-[9px] text-amber-400"
              title={`input_quality=${inputQuality}; missing=${missingInputs.join(",")}`}
            >
              · 数据{inputQuality === "partial" ? "部分缺失" : "缺失"}
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${badgeCls}`}
            title={`behavior_state=${behavior.behavior_state}`}
          >
            {stateLabel}
          </span>
          {(hasDualTrack || hasAnyScore) && (
            <button
              type="button"
              onClick={() => setExpert((s) => !s)}
              className="text-[9px] text-slate-500 hover:text-slate-300 transition"
            >
              {expert ? "收起" : "详情"}
            </button>
          )}
        </div>
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

      {/* M2.5 V1/V2 双轨对照（专家模式才展开） */}
      {expert && hasDualTrack && (
        <div className="mb-2 pb-2 border-b border-slate-700/30">
          <div className="text-[10px] text-slate-500 mb-1 flex items-center gap-1">
            <span>⚖</span>
            <span>V1 vs V2 双轨</span>
            <span className="text-slate-600">· M2.5 影子观测，不影响生产</span>
          </div>

          {showBounceCompare && (
            <div className="flex items-center gap-2 text-[11px]">
              <span className="text-slate-500 w-20 shrink-0">反弹质量</span>
              <span className="text-slate-600 w-8 text-[10px]">V1</span>
              <span className={`w-12 text-[10px] ${v1Bounce?.cls ?? "text-slate-500"}`}>
                {v1Bounce?.text ?? "—"}
              </span>
              <span className="text-slate-600 w-8 text-[10px]">V2</span>
              <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    v2BounceScore >= 0.65
                      ? "bg-emerald-500"
                      : v2BounceScore >= 0.4
                      ? "bg-amber-500"
                      : "bg-slate-600"
                  }`}
                  style={{ width: `${Math.round(v2BounceScore * 100)}%` }}
                />
              </div>
              <span
                className="text-slate-400 font-mono w-9 text-right text-[10px]"
                title={v2BounceQualityHint(v2BounceScore)}
              >
                {v2BounceScore.toFixed(2)}
              </span>
            </div>
          )}

          {showStageCompare && (
            <div className="flex items-center gap-2 text-[11px] mt-1">
              <span className="text-slate-500 w-20 shrink-0">突破阶段</span>
              <span className="text-slate-600 w-8 text-[10px]">V1</span>
              <span className={`w-32 text-[10px] ${v1Stage?.cls ?? "text-slate-500"}`}>
                {v1Stage?.text ?? "—"}
              </span>
              <span className="text-slate-600 w-8 text-[10px]">V2</span>
              <span className={`text-[10px] ${v2Stage?.cls ?? "text-slate-500"}`}>
                {v2Stage?.text ?? "—"}
              </span>
            </div>
          )}

          {showFakeBreak && (
            <div className="flex items-center gap-2 text-[11px] mt-1">
              <span className="text-slate-500 w-20 shrink-0">假破回收强度</span>
              <span className="text-slate-600 w-8 text-[10px]">V2</span>
              <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full transition-all ${
                    fakeBreakStrength >= 0.65
                      ? "bg-emerald-500"
                      : fakeBreakStrength >= 0.4
                      ? "bg-amber-500"
                      : "bg-slate-600"
                  }`}
                  style={{ width: `${Math.round(fakeBreakStrength * 100)}%` }}
                />
              </div>
              <span className="text-slate-400 font-mono w-9 text-right text-[10px]">
                {fakeBreakStrength.toFixed(2)}
              </span>
            </div>
          )}

          {showDynDepth && (
            <div
              className="flex items-center gap-2 text-[11px] mt-1 text-slate-500"
              title="V1 固定阈值 0.30%；V2 = max(cfg, 0.3 × ATR%)，自适应波动率"
            >
              <span className="w-20 shrink-0">破位阈值</span>
              <span className="text-slate-600 text-[10px]">V1</span>
              <span className="text-slate-400 text-[10px] font-mono">0.30%</span>
              <span className="text-slate-600 text-[10px]">V2</span>
              <span className="text-slate-300 text-[10px] font-mono">
                {dynBreakDepth.toFixed(2)}%
              </span>
            </div>
          )}
        </div>
      )}

      {/* 相关分数 mini bars（专家模式 / 普通模式只展示前 1 项） */}
      {hasAnyScore && (
        <div className="space-y-1.5 mb-2">
          {(expert ? scores : scores.slice(0, 1)).map(([label, value]) => (
            <ScoreBar key={label} label={label} value={value} />
          ))}
          {!expert && scores.length > 1 && (
            <div className="text-[9px] text-slate-600">
              点「详情」查看 {scores.length - 1} 个其它分数
            </div>
          )}
        </div>
      )}

      {/* M2.5 冲突预警（state vs behavior 不一致；普通+专家都显示，关键预警不能藏） */}
      {contradictions.length > 0 && (
        <div className="mb-2 pt-1 border-t border-rose-700/30">
          <div className="text-[10px] text-rose-400 mb-1">⚠ state vs behavior 冲突</div>
          <ul className="space-y-0.5">
            {contradictions.map((c) => (
              <li key={c} className="text-[10px] text-rose-300/90 leading-tight">
                · {c}
              </li>
            ))}
          </ul>
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
