"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type {
  ConsensusLevel,
  EngineBrief,
  FinalDecision,
  FinalDecisionResponse,
  RecommendedAction,
  TrafficLight,
} from "@/lib/types";

/**
 * P1.4 · FinalDecisionCard（双引擎融合层主视图）
 *
 * 职责：
 *   - 展示 L7.5 融合层输出 FinalDecision —— 对外主决策视图
 *   - 顶部：traffic_light + consensus_stars + headline + final_score
 *   - 中段：数学引擎 vs AI 引擎 并列简报对比（双分数条）
 *   - 底部：recommended_action + 融合入场参数 + 分歧提示 + 安全护栏
 *
 * 数据：
 *   - 轮询 /api/final-decision/{coin} (6s 与 execution-plan 同步节奏)
 */

const POLL_INTERVAL_MS = 6000;

const LIGHT_STYLE: Record<TrafficLight, { chip: string; ring: string; text: string; label: string }> = {
  green: { chip: "bg-green-500/25 text-green-300", ring: "border-green-500/50", text: "text-green-300", label: "🟢 执行" },
  yellow: { chip: "bg-yellow-500/25 text-yellow-300", ring: "border-yellow-500/50", text: "text-yellow-300", label: "🟡 谨慎" },
  orange: { chip: "bg-orange-500/25 text-orange-300", ring: "border-orange-500/50", text: "text-orange-300", label: "🟠 减仓/等待" },
  red: { chip: "bg-red-500/25 text-red-300", ring: "border-red-500/50", text: "text-red-300", label: "🔴 回避" },
  gray: { chip: "bg-slate-600/20 text-slate-400", ring: "border-slate-600", text: "text-slate-400", label: "⚪ 数据中" },
};

const CONSENSUS_LABEL: Record<ConsensusLevel, { text: string; color: string }> = {
  strong: { text: "强共识", color: "text-emerald-300" },
  agree: { text: "一致", color: "text-green-300" },
  math_lead: { text: "数学引擎主导", color: "text-blue-300" },
  ai_lead: { text: "AI 主导", color: "text-purple-300" },
  conflict: { text: "分歧", color: "text-orange-300" },
  both_wait: { text: "双方观望", color: "text-slate-300" },
};

const ACTION_LABEL: Record<RecommendedAction, { text: string; color: string }> = {
  execute: { text: "执行", color: "text-green-300" },
  reduce_size: { text: "减仓", color: "text-yellow-300" },
  wait: { text: "观望", color: "text-slate-300" },
  avoid: { text: "回避", color: "text-red-300" },
};

const BIAS_CN: Record<string, string> = {
  bullish: "多",
  bearish: "空",
  neutral: "中",
  potential_reversal: "反转",
};

export default function FinalDecisionCard({
  coin,
  externalDecision,
}: {
  coin: string;
  /** 外部传入的 decision：传入后停用内部轮询，用于历史详情页等静态场景 */
  externalDecision?: FinalDecision | null;
}) {
  const [fetchedDecision, setFetchedDecision] = useState<FinalDecision | null>(null);
  const [fetchedReady, setFetchedReady] = useState(false);
  const [lastErr, setLastErr] = useState("");

  const useExternal = externalDecision !== undefined;

  useEffect(() => {
    if (useExternal) return;
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/final-decision/${coin}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j: FinalDecisionResponse = await r.json();
        if (cancelled) return;
        setFetchedReady(Boolean(j.ready));
        setFetchedDecision(j.decision ?? null);
        setLastErr("");
      } catch (e) {
        if (cancelled) return;
        setLastErr(e instanceof Error ? e.message : "fetch error");
      }
    };
    fetchOnce();
    const t = setInterval(fetchOnce, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [coin, useExternal]);

  const decision = useExternal ? externalDecision ?? null : fetchedDecision;
  const ready = useExternal ? Boolean(externalDecision) : fetchedReady;

  if (!ready || !decision) {
    return (
      <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg px-4 py-3 text-xs text-slate-500">
        <div className="flex items-center justify-between">
          <span className="text-slate-400">🎯 双引擎最终决策</span>
          <span>{lastErr ? `数据源异常：${lastErr}` : "等待 AI 分析首轮..."}</span>
        </div>
      </div>
    );
  }

  const light = LIGHT_STYLE[decision.traffic_light] ?? LIGHT_STYLE.gray;
  const consensus = CONSENSUS_LABEL[decision.consensus_level] ?? CONSENSUS_LABEL.agree;
  const action = ACTION_LABEL[decision.recommended_action] ?? ACTION_LABEL.wait;
  const stars = Math.max(0, Math.min(5, decision.consensus_stars));

  return (
    <div
      className={`bg-slate-800/70 border-2 ${light.ring} rounded-xl p-4 space-y-3 shadow-lg`}
      data-testid="final-decision-card"
    >
      {/* 顶部：标题 + 共识 + 星级 + 分数 */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-bold text-slate-100">🎯 双引擎最终决策</span>
          <span className={`px-2 py-0.5 rounded text-[11px] font-bold ${light.chip}`}>
            {light.label}
          </span>
          <span className={`text-[12px] font-semibold ${consensus.color}`}>
            · {consensus.text}
          </span>
          <span className="text-amber-300 tracking-wide" title={`共识星级 ${stars}/5`}>
            {"★".repeat(stars)}
            <span className="text-slate-600">{"★".repeat(5 - stars)}</span>
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-500">综合分</span>
          <span className={`font-mono text-xl font-bold ${light.text}`}>
            {decision.final_score.toFixed(1)}
          </span>
        </div>
      </div>

      {/* 头条 + 一句话 */}
      {decision.headline && (
        <div className="space-y-1">
          <div className={`text-sm font-semibold ${light.text}`}>{decision.headline}</div>
          {decision.one_liner && (
            <div className="text-[11px] text-slate-400">{decision.one_liner}</div>
          )}
        </div>
      )}

      {/* 双引擎并列简报 */}
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <EngineBriefTile brief={decision.math_brief} label="🧮 数学引擎 (L4)" tone="blue" />
        <EngineBriefTile brief={decision.ai_brief} label="🤖 AI 引擎 (L7)" tone="purple" />
      </div>

      {/* 推荐动作条 */}
      <div className="flex items-center justify-between bg-slate-900/50 border border-slate-700/50 rounded px-3 py-2">
        <div className="flex items-center gap-2 text-xs">
          <span className="text-slate-500">融合动作</span>
          <span className={`font-semibold ${action.color}`}>{action.text}</span>
          {decision.recommended_position_pct > 0 && (
            <span className="text-slate-300 font-mono">
              · 仓位 {decision.recommended_position_pct.toFixed(0)}%
            </span>
          )}
        </div>
        <div className="text-[11px] text-slate-500">
          {decision.consensus_summary_cn}
        </div>
      </div>

      {/* 融合入场参数（conflict 时不展示） */}
      {decision.consensus_level !== "conflict" && decision.entry_zone_low != null && (
        <div className="grid grid-cols-2 sm:grid-cols-5 gap-2 text-xs">
          <ParamTile
            label="融合入场"
            value={fmtZone(decision.entry_zone_low, decision.entry_zone_high, coin)}
          />
          <ParamTile
            label="止损"
            value={decision.stop_loss != null ? formatPrice(decision.stop_loss, coin) : "-"}
            accent="text-red-300"
          />
          <ParamTile
            label="TP1"
            value={decision.tp1 != null ? formatPrice(decision.tp1, coin) : "-"}
            accent="text-green-300"
          />
          <ParamTile
            label="TP2"
            value={decision.tp2 != null ? formatPrice(decision.tp2, coin) : "-"}
            accent="text-green-300"
          />
          <ParamTile
            label="盈亏比"
            value={decision.rr_ratio != null ? `1:${decision.rr_ratio.toFixed(2)}` : "-"}
            accent={
              decision.rr_ratio && decision.rr_ratio >= 2
                ? "text-green-300"
                : "text-slate-300"
            }
          />
        </div>
      )}

      {/* 分歧详情（conflict 时） */}
      {decision.consensus_level === "conflict" && decision.divergence_summary_cn && (
        <div className="rounded border border-orange-500/30 bg-orange-500/10 text-orange-200 text-xs px-3 py-2 space-y-1">
          <div className="font-semibold">⚖ 双引擎分歧</div>
          <div>{decision.divergence_summary_cn}</div>
          {decision.historical_divergence && decision.historical_divergence.sample_size >= 10 && (
            <div className="text-[11px] text-orange-300/80 pt-0.5 flex items-center gap-1.5">
              <span>
                历史样本 n={decision.historical_divergence.sample_size} · 数学胜率{" "}
                {(decision.historical_divergence.math_win_rate * 100).toFixed(0)}% · AI 胜率{" "}
                {(decision.historical_divergence.ai_win_rate * 100).toFixed(0)}%
              </span>
              <Link
                href={`/divergence/${coin}`}
                className="text-blue-300 hover:text-blue-200 underline-offset-2 hover:underline"
              >
                查看明细 →
              </Link>
            </div>
          )}
          {(!decision.historical_divergence ||
            decision.historical_divergence.sample_size < 10) && (
            <div className="text-[11px] text-orange-300/60 pt-0.5">
              <Link
                href={`/divergence/${coin}`}
                className="hover:text-orange-200 underline-offset-2 hover:underline"
              >
                查看历史分歧样本 →
              </Link>
            </div>
          )}
        </div>
      )}

      {/* 安全护栏 */}
      {decision.safety_gate_triggered && (
        <div className="rounded border border-red-500/40 bg-red-500/10 text-red-300 text-xs px-3 py-2">
          <div className="font-semibold mb-0.5">🛑 安全护栏已触发</div>
          <div className="text-[11px]">
            {decision.safety_gate_reason || "数学引擎五道护栏任一触发，融合层已强制降级"}
          </div>
        </div>
      )}

      {/* 底部元信息 */}
      <div className="flex items-center justify-between text-[10px] text-slate-500 pt-2 border-t border-slate-700/50">
        <div className="flex items-center gap-2 flex-wrap">
          {decision.active_themes_count > 0 && (
            <span>📰 活跃叙事 {decision.active_themes_count}</span>
          )}
          {decision.geo_risk_overall_level > 0 && (
            <span>
              地缘 {decision.geo_risk_label} · {decision.geo_risk_overall_level}/5
            </span>
          )}
          {decision.has_blackswan_warning && (
            <span className="text-red-400">⚠ 黑天鹅 24h</span>
          )}
        </div>
        <span>{new Date(decision.ts * 1000).toLocaleTimeString()}</span>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────

function EngineBriefTile({
  brief,
  label,
  tone,
}: {
  brief: EngineBrief;
  label: string;
  tone: "blue" | "purple";
}) {
  const toneClass =
    tone === "blue"
      ? "border-blue-500/20 bg-blue-500/5"
      : "border-purple-500/20 bg-purple-500/5";
  const scoreClass =
    brief.score >= 75
      ? "text-green-300"
      : brief.score >= 55
        ? "text-yellow-300"
        : "text-slate-300";

  return (
    <div className={`rounded border ${toneClass} px-3 py-2 space-y-1`}>
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-slate-400">{label}</span>
        <span className={`font-mono text-base font-bold ${scoreClass}`}>
          {brief.score.toFixed(0)}
        </span>
      </div>
      <ScoreBar score={brief.score} tone={tone} />
      <div className="flex items-center justify-between text-[10px] text-slate-500">
        <span>
          方向：
          <span className="text-slate-300">{BIAS_CN[brief.bias] ?? brief.bias}</span>
        </span>
        {brief.position_pct > 0 && (
          <span>仓位 {brief.position_pct.toFixed(0)}%</span>
        )}
      </div>
      {brief.summary_cn && (
        <div className="text-[10px] text-slate-400 truncate" title={brief.summary_cn}>
          {brief.summary_cn}
        </div>
      )}
    </div>
  );
}

function ScoreBar({ score, tone }: { score: number; tone: "blue" | "purple" }) {
  const pct = Math.max(0, Math.min(100, score));
  const bar = tone === "blue" ? "bg-blue-500" : "bg-purple-500";
  return (
    <div className="h-1.5 bg-slate-900/70 rounded overflow-hidden">
      <div className={`h-full ${bar}`} style={{ width: `${pct}%` }} />
    </div>
  );
}

function ParamTile({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div className="bg-slate-900/30 rounded px-2 py-1.5 border border-slate-700/40">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`font-mono text-sm mt-0.5 ${accent ?? "text-slate-200"}`}>
        {value}
      </div>
    </div>
  );
}

function fmtZone(
  low: number | null,
  high: number | null,
  coin: string,
): string {
  if (low == null && high == null) return "-";
  if (low != null && high != null && Math.abs(low - high) < 1e-6) {
    return formatPrice(low, coin);
  }
  return `${low != null ? formatPrice(low, coin) : "?"} ~ ${high != null ? formatPrice(high, coin) : "?"}`;
}
