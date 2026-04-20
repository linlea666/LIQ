"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type {
  AIFactorMatrix,
  AIFactorSection,
  AITraderReport,
  AITraderReportResponse,
  AITradingPlan,
  AgreementWithMath,
  SignalDirection,
} from "@/lib/types";

/**
 * P1.4 · AITraderMatrixCard（AI 交易员完整看盘表）
 *
 * 职责：
 *   - 展示 AI 主模块（L7）作为"独立交易员"的结构化报告
 *   - 7 板块 AIFactorMatrix（A-G）折叠面板
 *   - AI 交易计划（主 + 备选）
 *   - 关键位解读 · 叙事影响 · agreement 徽章
 *
 * 数据：
 *   - 轮询 /api/ai-trader-report/{coin}（AI 每小时一次，UI 8s 轮询探测更新）
 */

const POLL_INTERVAL_MS = 8000;

const AGREEMENT_LABEL: Record<AgreementWithMath, { text: string; color: string; icon: string }> = {
  agree: { text: "与数学引擎一致", color: "bg-green-500/15 text-green-300 border-green-500/30", icon: "✓" },
  caution: { text: "与数学引擎存在偏差", color: "bg-yellow-500/15 text-yellow-300 border-yellow-500/30", icon: "⚠" },
  disagree: { text: "与数学引擎分歧", color: "bg-orange-500/15 text-orange-300 border-orange-500/30", icon: "⚖" },
};

const BIAS_CHIP: Record<SignalDirection, string> = {
  bullish: "text-green-300 bg-green-500/10",
  bearish: "text-red-300 bg-red-500/10",
  neutral: "text-slate-300 bg-slate-600/20",
  potential_reversal: "text-purple-300 bg-purple-500/10",
};

const BIAS_CN: Record<SignalDirection, string> = {
  bullish: "偏多",
  bearish: "偏空",
  neutral: "中性",
  potential_reversal: "潜在反转",
};

const RESONANCE_DOT: Record<string, string> = {
  high: "bg-orange-400",
  medium: "bg-yellow-400",
  low: "bg-slate-500",
};

const DIRECTION_CN: Record<string, string> = {
  long: "做多",
  short: "做空",
  wait: "观望",
  avoid: "回避",
};

export default function AITraderMatrixCard({ coin }: { coin: string }) {
  const [report, setReport] = useState<AITraderReport | null>(null);
  const [ready, setReady] = useState(false);
  const [lastErr, setLastErr] = useState("");

  useEffect(() => {
    let cancelled = false;
    const fetchOnce = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/ai-trader-report/${coin}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j: AITraderReportResponse = await r.json();
        if (cancelled) return;
        setReady(Boolean(j.ready));
        setReport(j.report ?? null);
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
  }, [coin]);

  if (!ready || !report) {
    return (
      <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg px-4 py-3 text-xs text-slate-500">
        <div className="flex items-center justify-between">
          <span className="text-slate-400">🧠 AI 交易员（L7）</span>
          <span>{lastErr ? `数据源异常：${lastErr}` : "等待 AI 首轮分析..."}</span>
        </div>
      </div>
    );
  }

  const agreement = AGREEMENT_LABEL[report.agreement_with_math_engine] ?? AGREEMENT_LABEL.caution;
  const biasChip = BIAS_CHIP[report.bias] ?? BIAS_CHIP.neutral;

  return (
    <div
      className="bg-slate-800/70 border border-purple-500/20 rounded-xl p-4 space-y-3 shadow-lg"
      data-testid="ai-trader-matrix-card"
    >
      {/* 顶部：AI 观点 + agreement */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-bold text-slate-100">🧠 AI 交易员 (L7)</span>
          <span className={`px-2 py-0.5 rounded text-[11px] font-bold ${biasChip}`}>
            {BIAS_CN[report.bias] ?? report.bias}
          </span>
          <span className="text-[11px] text-slate-500">
            信心 <span className="font-mono text-slate-200">{report.conviction}</span>/100
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={`text-[11px] px-2 py-0.5 rounded border ${agreement.color}`}
            title={report.agreement_notes_cn}
          >
            {agreement.icon} {agreement.text}
          </span>
          <Link
            href={`/ai-quality/${coin}`}
            className="text-[11px] text-purple-300/80 hover:text-purple-200 underline-offset-2 hover:underline"
            title="AI 分析质量监控（附录命中率 · 冲突熔断）"
          >
            📊 质量监控
          </Link>
        </div>
      </div>

      {/* 市场观点（≤3 行） */}
      {report.market_view_cn && (
        <div className="text-xs text-slate-300 bg-slate-900/40 border border-slate-700/40 rounded px-3 py-2 leading-relaxed line-clamp-3">
          {report.market_view_cn}
        </div>
      )}

      {/* 多维度看盘表（7 板块） */}
      {report.factor_matrix && <FactorMatrixBlock matrix={report.factor_matrix} />}

      {/* 交易计划 */}
      {report.trading_plans.length > 0 && (
        <TradingPlansBlock plans={report.trading_plans} coin={coin} />
      )}

      {/* 关键位解读 */}
      <KeyLevelInterpretationBlock
        interp={report.key_level_interpretation}
        coin={coin}
      />

      {/* 叙事 / 新闻 / 地缘 */}
      {(report.news_impact_summary_cn || report.geo_risk_assessment_cn ||
        report.narrative_impact.length > 0) && (
        <NarrativeBlock report={report} />
      )}

      {/* 风险提示 */}
      {report.key_risks.length > 0 && (
        <div className="rounded border border-red-500/30 bg-red-500/5 px-3 py-2">
          <div className="text-[11px] font-semibold text-red-300 mb-1">⚠ 关键风险</div>
          <ul className="list-disc list-inside text-[11px] text-red-200/80 space-y-0.5">
            {report.key_risks.slice(0, 4).map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {/* 底部元信息 */}
      <div className="flex items-center justify-between text-[10px] text-slate-500 pt-2 border-t border-slate-700/50">
        <span>
          {report.model || "AI"}
          {report.latency_ms > 0 && ` · ${(report.latency_ms / 1000).toFixed(1)}s`}
          {report.thinking_tokens > 0 && ` · 思考 ${report.thinking_tokens}tok`}
        </span>
        <span>{new Date(report.ts * 1000).toLocaleTimeString()}</span>
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 多维看盘表
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function FactorMatrixBlock({ matrix }: { matrix: AIFactorMatrix }) {
  const [openId, setOpenId] = useState<string>(matrix.sections[0]?.section_id ?? "");
  const overallChip = BIAS_CHIP[matrix.overall_bias] ?? BIAS_CHIP.neutral;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between flex-wrap gap-2">
        <span className="text-[12px] font-semibold text-slate-200">
          多维度看盘（7 板块）
        </span>
        <div className="flex items-center gap-2">
          <span className={`text-[11px] px-2 py-0.5 rounded ${overallChip}`}>
            综合 · {matrix.summary_line}
          </span>
          <span className="text-[10px] text-slate-500">
            信心 {confidenceCn(matrix.overall_confidence)}
          </span>
        </div>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-1.5">
        {matrix.sections.map((s) => (
          <SectionTab
            key={s.section_id}
            section={s}
            active={openId === s.section_id}
            onClick={() => setOpenId(openId === s.section_id ? "" : s.section_id)}
          />
        ))}
      </div>
      {openId && (
        <SectionDetail
          section={matrix.sections.find((s) => s.section_id === openId)!}
        />
      )}
    </div>
  );
}

function SectionTab({
  section,
  active,
  onClick,
}: {
  section: AIFactorSection;
  active: boolean;
  onClick: () => void;
}) {
  const chip = BIAS_CHIP[section.section_bias] ?? BIAS_CHIP.neutral;
  return (
    <button
      type="button"
      onClick={onClick}
      className={`rounded px-2 py-1.5 text-left border transition-colors ${
        active
          ? "border-purple-500/50 bg-purple-500/10"
          : "border-slate-700/40 bg-slate-900/30 hover:border-slate-600"
      }`}
    >
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-slate-300">
          {section.section_emoji} {section.section_name_cn}
        </span>
        <span className="text-[9px] text-slate-500">§{section.section_id}</span>
      </div>
      <div className={`text-[10px] mt-0.5 px-1 py-0.5 rounded inline-block ${chip}`}>
        {BIAS_CN[section.section_bias] ?? section.section_bias}
      </div>
    </button>
  );
}

function SectionDetail({ section }: { section: AIFactorSection }) {
  return (
    <div className="rounded border border-slate-700/60 bg-slate-900/40">
      <div className="px-3 py-1.5 border-b border-slate-700/40 flex items-center justify-between">
        <span className="text-[11px] text-slate-300">
          {section.section_emoji} {section.section_name_cn}
        </span>
        {section.section_summary && (
          <span className="text-[10px] text-slate-500 truncate ml-2 max-w-[60%]">
            {section.section_summary}
          </span>
        )}
      </div>
      <div className="divide-y divide-slate-800/60">
        {section.rows.map((row, i) => (
          <div
            key={i}
            className="grid grid-cols-12 gap-2 px-3 py-1.5 text-[11px] items-center"
          >
            <div className="col-span-3 text-slate-400 truncate" title={row.dimension}>
              {row.dimension}
            </div>
            <div
              className="col-span-4 text-slate-200 font-mono text-[10.5px] truncate"
              title={row.value_display}
            >
              {row.value_display}
            </div>
            <div className="col-span-4 text-slate-400 truncate" title={row.signal}>
              {row.signal}
            </div>
            <div className="col-span-1 flex items-center justify-end gap-1">
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  RESONANCE_DOT[row.resonance] ?? RESONANCE_DOT.low
                }`}
                title={`共振 ${row.resonance}`}
              />
              <span
                className={`inline-block w-4 text-center ${
                  row.direction === "bullish"
                    ? "text-green-400"
                    : row.direction === "bearish"
                      ? "text-red-400"
                      : "text-slate-500"
                }`}
              >
                {row.direction === "bullish"
                  ? "↑"
                  : row.direction === "bearish"
                    ? "↓"
                    : "·"}
              </span>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 交易计划
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function TradingPlansBlock({
  plans,
  coin,
}: {
  plans: AITradingPlan[];
  coin: string;
}) {
  const primary = plans.find((p) => p.priority === 1) ?? plans[0];
  const alternates = plans.filter((p) => p !== primary).slice(0, 2);

  return (
    <div className="space-y-2">
      <div className="text-[12px] font-semibold text-slate-200">
        🎯 AI 交易计划
      </div>
      <PlanTile plan={primary} coin={coin} isPrimary />
      {alternates.map((p, i) => (
        <PlanTile key={i} plan={p} coin={coin} isPrimary={false} />
      ))}
    </div>
  );
}

function PlanTile({
  plan,
  coin,
  isPrimary,
}: {
  plan: AITradingPlan;
  coin: string;
  isPrimary: boolean;
}) {
  const directionCn = DIRECTION_CN[plan.direction] ?? plan.direction;
  const directionColor =
    plan.direction === "long"
      ? "text-green-300"
      : plan.direction === "short"
        ? "text-red-300"
        : "text-slate-300";

  return (
    <div
      className={`rounded border px-3 py-2 space-y-1.5 ${
        isPrimary
          ? "border-purple-500/30 bg-purple-500/5"
          : "border-slate-700/40 bg-slate-900/30"
      }`}
    >
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="text-[11px] text-slate-500">
            {isPrimary ? "主计划" : `备选 #${plan.priority}`}
          </span>
          <span className="text-[11px] font-bold text-amber-300">{plan.tier_hint} 级</span>
          <span className={`text-[12px] font-semibold ${directionColor}`}>
            {directionCn}
          </span>
        </div>
        <div className="flex items-center gap-2 text-[11px]">
          <span className="text-slate-500">信心</span>
          <span className="font-mono text-slate-200">{plan.conviction}</span>
          {plan.position_suggestion_pct > 0 && (
            <span className="text-slate-300">
              · 仓位 {plan.position_suggestion_pct.toFixed(0)}%
            </span>
          )}
        </div>
      </div>
      {(plan.entry_zone_low != null || plan.stop_loss != null) && (
        <div className="grid grid-cols-4 gap-2 text-[11px]">
          <Small label="入场" value={fmtMaybe(plan.entry_zone_low, coin)} />
          <Small
            label="止损"
            value={fmtMaybe(plan.stop_loss, coin)}
            accent="text-red-300"
          />
          <Small
            label="TP1"
            value={fmtMaybe(plan.tp1, coin)}
            accent="text-green-300"
          />
          <Small
            label="盈亏比"
            value={plan.rr_ratio != null ? `1:${plan.rr_ratio.toFixed(2)}` : "-"}
          />
        </div>
      )}
      {plan.trigger_condition && (
        <div className="text-[10.5px] text-slate-400 leading-relaxed">
          触发：{plan.trigger_condition}
        </div>
      )}
      {plan.reason && isPrimary && (
        <div className="text-[10.5px] text-slate-500 leading-relaxed line-clamp-2">
          {plan.reason}
        </div>
      )}
      {plan.alignment_note && (
        <div className="text-[10px] text-slate-500 italic">
          与数学引擎：{plan.alignment_note}
        </div>
      )}
    </div>
  );
}

function Small({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div className="bg-slate-900/40 rounded px-2 py-1 border border-slate-700/40">
      <div className="text-[9.5px] text-slate-500">{label}</div>
      <div className={`font-mono text-[11px] mt-0.5 ${accent ?? "text-slate-200"}`}>
        {value}
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 关键位 / 叙事
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function KeyLevelInterpretationBlock({
  interp,
  coin,
}: {
  interp: AITraderReport["key_level_interpretation"];
  coin: string;
}) {
  const hasSupport = interp.primary_support_price != null;
  const hasResistance = interp.primary_resistance_price != null;
  const hasTrap = interp.trap_warning.length > 0;
  const hasExtras = interp.extra_levels && interp.extra_levels.length > 0;
  if (!hasSupport && !hasResistance && !hasTrap && !hasExtras) return null;

  return (
    <div className="space-y-1.5">
      <div className="text-[12px] font-semibold text-slate-200">🧭 AI 关键位解读</div>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2 text-[11px]">
        {hasSupport && (
          <LevelTile
            label="主支撑"
            price={interp.primary_support_price!}
            reason={interp.primary_support_reason}
            coin={coin}
            tone="green"
          />
        )}
        {hasResistance && (
          <LevelTile
            label="主压力"
            price={interp.primary_resistance_price!}
            reason={interp.primary_resistance_reason}
            coin={coin}
            tone="red"
          />
        )}
      </div>
      {hasTrap && (
        <div className="text-[11px] text-orange-200 bg-orange-500/10 border border-orange-500/30 rounded px-3 py-1.5">
          🎣 {interp.trap_warning}
        </div>
      )}
    </div>
  );
}

function LevelTile({
  label,
  price,
  reason,
  coin,
  tone,
}: {
  label: string;
  price: number;
  reason: string;
  coin: string;
  tone: "green" | "red";
}) {
  const color = tone === "green" ? "text-green-300" : "text-red-300";
  const border = tone === "green" ? "border-green-500/20" : "border-red-500/20";
  return (
    <div className={`rounded border ${border} bg-slate-900/40 px-3 py-1.5`}>
      <div className="flex items-center justify-between">
        <span className="text-[10.5px] text-slate-500">{label}</span>
        <span className={`font-mono text-sm ${color}`}>
          {formatPrice(price, coin)}
        </span>
      </div>
      {reason && (
        <div className="text-[10.5px] text-slate-400 mt-0.5 leading-snug">
          {reason}
        </div>
      )}
    </div>
  );
}

function NarrativeBlock({ report }: { report: AITraderReport }) {
  return (
    <div className="space-y-1.5 text-[11px]">
      {report.news_impact_summary_cn && (
        <div className="flex items-start gap-2">
          <span className="text-slate-500 shrink-0">📰</span>
          <span className="text-slate-300">{report.news_impact_summary_cn}</span>
        </div>
      )}
      {report.geo_risk_assessment_cn && (
        <div className="flex items-start gap-2">
          <span className="text-slate-500 shrink-0">🌍</span>
          <span className="text-slate-300">{report.geo_risk_assessment_cn}</span>
        </div>
      )}
      {report.narrative_impact.length > 0 && (
        <div className="pt-1 space-y-0.5">
          {report.narrative_impact.slice(0, 3).map((n, i) => (
            <div key={i} className="flex items-center gap-2 text-[10.5px]">
              <span
                className={`w-1.5 h-1.5 rounded-full ${
                  RESONANCE_DOT[n.weight_on_current_plan] ?? RESONANCE_DOT.low
                }`}
              />
              <span className="text-slate-400">
                {n.theme_name_cn || n.theme_id}
              </span>
              <span className="text-slate-500 truncate">· {n.ai_view_cn}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function fmtMaybe(v: number | null, coin: string): string {
  if (v == null) return "-";
  return formatPrice(v, coin);
}

function confidenceCn(c: "high" | "medium" | "low"): string {
  return { high: "强", medium: "中", low: "弱" }[c] ?? c;
}
