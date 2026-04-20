"use client";

/**
 * TrendExhaustionView · 趋势动能 / 衰竭 / 反转侦测（v2 白话版）
 *
 * 设计原则（与后端 trend_exhaustion.py v2 对齐）：
 *   - 小白一眼看懂：顶部一张"白话卡"，三行结构：
 *         Line1: 方向 emoji + "还在涨，动能健康" 等口语化结论
 *         Line2: "顺势持有或加仓都可以" 行动建议
 *         Line3: MTF 溯源（折叠在"为什么?"里）
 *   - 进阶/专业：展开三周期分解 + 子项分数
 *   - 震荡/极端 regime：顶部一条醒目横条说"当前没有趋势别做方向单"
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { useMarketStore } from "@/stores/marketStore";
import type {
  TEConsensusLevel,
  TEDirection,
  TEExhaustionState,
  TEOverallAction,
  TERegime,
  TrendExhaustionState,
} from "@/lib/types";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 枚举 → 中文 & 配色
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const STATE_MAP: Record<
  TEExhaustionState,
  { label: string; color: string; bg: string; emoji: string }
> = {
  healthy_continuation: {
    label: "健康续航",
    color: "text-green-300",
    bg: "bg-green-500/10 border-green-500/40",
    emoji: "▲",
  },
  momentum_fading: {
    label: "动能减速",
    color: "text-amber-300",
    bg: "bg-amber-500/10 border-amber-500/40",
    emoji: "◆",
  },
  exhaustion_warn: {
    label: "衰竭警戒",
    color: "text-orange-300",
    bg: "bg-orange-500/10 border-orange-500/40",
    emoji: "▼",
  },
  structural_reversal: {
    label: "结构反转",
    color: "text-red-300",
    bg: "bg-red-500/10 border-red-500/40",
    emoji: "⇋",
  },
  neutral: {
    label: "观望",
    color: "text-slate-400",
    bg: "bg-slate-500/10 border-slate-500/40",
    emoji: "·",
  },
};

const CONSENSUS_MAP: Record<TEConsensusLevel, { label: string; color: string }> = {
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

const REGIME_CN: Record<TERegime, string> = {
  trend_up: "上升趋势",
  trend_down: "下降趋势",
  range: "箱体震荡",
  squeeze: "蓄力收敛",
  high_vol_chop: "高波动无序",
  extreme: "极端行情",
};

const TF_LABEL: Record<string, string> = { "1h": "1小时", "4h": "4小时", "1d": "日线" };

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 白话备选（后端可能没推 overall_plain_cn 时用前端 fallback）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const DIR_EMOJI: Record<TEDirection, string> = { up: "📈", down: "📉", flat: "⏸" };

function fallbackPlain(
  direction: TEDirection,
  state: TEExhaustionState,
  vetoed: boolean,
): { plain: string; tip: string } {
  if (vetoed) {
    return {
      plain: "当前是震荡/极端行情，没有趋势",
      tip: "不要做趋势单，空仓或等方向明朗",
    };
  }
  const emoji = DIR_EMOJI[direction] ?? "";
  const dirWord = direction === "up" ? "涨" : direction === "down" ? "跌" : "";
  if (!dirWord) {
    return { plain: "方向未定，等行情明朗", tip: "空仓观望" };
  }
  switch (state) {
    case "healthy_continuation":
      return {
        plain: `${emoji} 还在${dirWord}，动能健康`,
        tip: direction === "up"
          ? "顺势持有或加仓都可以"
          : "顺势持空或加空都可以",
      };
    case "momentum_fading":
      return {
        plain: `${emoji} 还在${dirWord}，但动能在变慢`,
        tip: direction === "up" ? "已有仓位减半，别再追高" : "已有空单减半，别再追空",
      };
    case "exhaustion_warn":
      return {
        plain: `${emoji} ${dirWord}不动了，${direction === "up" ? "多" : "空"}头要竭`,
        tip: "有仓位建议离场观望",
      };
    case "structural_reversal":
      return {
        plain: `${emoji} ${direction === "up" ? "顶" : "底"}部已确认，方向切换`,
        tip: direction === "up" ? "清仓，别扛单" : "清空单，别扛单",
      };
    default:
      return {
        plain: `${emoji} ${dirWord}势方向，信号不清晰`,
        tip: "保持观望或轻仓跟随",
      };
  }
}

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
  state,
  expanded,
  onToggle,
}: {
  state: TrendExhaustionState;
  expanded: boolean;
  onToggle: () => void;
}) {
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
            <span className="text-[10px] text-slate-500">· {state.state_age_min}m</span>
          )}
          {typeof state.confirmed_ticks === "number" && state.confirmed_ticks < 2 && (
            (state.state === "exhaustion_warn" || state.state === "structural_reversal") && (
              <span className="text-[10px] text-amber-300/80">[待二次确认]</span>
            )
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
  const [showWhy, setShowWhy] = useState(false);
  const toggle = (tf: string) => setExpanded((e) => ({ ...e, [tf]: !e[tf] }));

  const tfCards = useMemo(() => {
    if (!te) return [] as TrendExhaustionState[];
    const list: TrendExhaustionState[] = [];
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

  const direction: TEDirection = te.overall_direction ?? "flat";
  const vetoed = te.regime_vetoed === true;
  const overallMeta = STATE_MAP[te.overall_state];
  const consensusMeta = CONSENSUS_MAP[te.consensus_level];
  const actionMeta = ACTION_MAP[te.overall_action];

  const plain =
    te.overall_plain_cn && te.overall_plain_cn.trim().length > 0
      ? te.overall_plain_cn
      : fallbackPlain(direction, te.overall_state, vetoed).plain;
  const tip =
    te.overall_tip_cn && te.overall_tip_cn.trim().length > 0
      ? te.overall_tip_cn
      : fallbackPlain(direction, te.overall_state, vetoed).tip;

  return (
    <div className="space-y-4">
      {/* ── [1] 震荡/极端 veto 顶部横条 ──────────────────────── */}
      {vetoed && te.regime && (
        <div className="rounded-lg border-2 border-purple-500/60 bg-purple-500/10 p-3 flex items-center gap-3">
          <span className="text-2xl">⚠</span>
          <div className="flex-1">
            <div className="text-base font-semibold text-purple-200">
              {REGIME_CN[te.regime] ?? te.regime}：当前没有趋势，别做方向单
            </div>
            <div className="text-xs text-purple-300/80 mt-0.5">
              趋势模块已暂停输出，避免在假行情里给错方向。等 regime 切回 trend 再参考。
            </div>
          </div>
        </div>
      )}

      {/* ── [2] 小白白话卡（三行） ──────────────────────────── */}
      <div className={`rounded-xl border-2 p-4 ${overallMeta.bg} transition-all`}>
        {/* Line 1：白话结论（超大） */}
        <div className="text-2xl font-bold leading-tight text-slate-100">
          {plain}
        </div>
        {/* Line 2：白话行动 */}
        <div className={`mt-2 text-base font-medium ${actionMeta.color}`}>
          💡 {tip}
        </div>
        {/* Line 3：MTF 元数据（小号字，可折叠 "为什么?" 查看） */}
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          <span className={`px-2 py-0.5 rounded ${overallMeta.color} bg-slate-900/50`}>
            {overallMeta.emoji} {overallMeta.label}
          </span>
          <span className={`px-2 py-0.5 rounded ${consensusMeta.color} bg-slate-900/50`}>
            {consensusMeta.label}
          </span>
          {te.overall_position_pct > 0 && (
            <span className="px-2 py-0.5 rounded text-slate-400 bg-slate-900/50">
              参考仓位 {Math.round(te.overall_position_pct * 100)}%
            </span>
          )}
          {te.regime && !vetoed && (
            <span className="px-2 py-0.5 rounded text-slate-500 bg-slate-900/30">
              regime·{REGIME_CN[te.regime] ?? te.regime}
            </span>
          )}
          <button
            onClick={() => setShowWhy((v) => !v)}
            className="ml-auto text-[11px] text-slate-400 hover:text-slate-200 underline underline-offset-2"
          >
            {showWhy ? "收起依据" : "为什么?"}
          </button>
        </div>
        {showWhy && (
          <div className="mt-2 text-[11px] text-slate-400 leading-relaxed border-t border-slate-700/40 pt-2">
            {te.overall_reason_cn}
          </div>
        )}
        {te.data_quality === "insufficient" && (
          <div className="mt-2 text-[11px] text-amber-300/80">
            ⚠ 数据未齐（{te.missing_inputs.join(", ") || "样本累积中"}），当前仅作参考
          </div>
        )}
      </div>

      {/* ── [3] 三周期分解（进阶/专业模式或有数据时展示） ─────── */}
      {(displayMode === "pro" || tfCards.length > 0) && !vetoed && (
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
                state={s}
                expanded={!!expanded[s.tf]}
                onToggle={() => toggle(s.tf)}
              />
            ))}
          </div>
        </div>
      )}

      {/* ── [4] 日报入口 + 方法论注释 ─────────────────────────── */}
      <div className="flex items-start justify-between gap-3 border-t border-slate-800 pt-2">
        <div className="text-[11px] text-slate-600 leading-relaxed flex-1">
          方法论：regime-aware 三维打分 × MTF 共识 × 硬门闸（需连续 2 次才出警戒）。
          动能 = MACD/价格斜率/RSI/FVG；参与度 = CVD/OI 踩踏/CB 溢价/资金费率；
          衰竭 = TD/背离/Fib 扩展/清算簇磁吸。
          不预测点位，只回答「续航 vs 衰竭」。震荡/极端 regime 会自动暂停方向性结论。
        </div>
        <Link
          href="/te/report"
          className="shrink-0 whitespace-nowrap rounded-md border border-slate-700 bg-slate-900/60 px-2.5 py-1 text-[11px] text-slate-300 hover:border-blue-500 hover:text-blue-300 transition"
          title="查看准确率日报（事后打标 + AI 复核模板）"
        >
          📊 准确率日报
        </Link>
      </div>
    </div>
  );
}
