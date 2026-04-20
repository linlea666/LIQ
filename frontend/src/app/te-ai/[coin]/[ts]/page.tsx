"use client";

/**
 * TE · AI 解读详情页
 *
 * 路由：/te-ai/[coin]/[ts]
 * API：GET /api/te/ai_interpret/{coin}/detail/{ts}
 *
 * 结构复用 TEAIInterpretBlock 的三张子卡片（export 后复用），
 * 额外展示：规则侧快照对比 + 思考链（reasoning） + 元信息。
 */

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { API_BASE } from "@/lib/constants";
import type { TEAIDetailResponse } from "@/lib/types";
import {
  ALIGNMENT_META,
  SCENARIO_META,
  TrendAssessmentCard,
  LevelProjectionCard,
  TradeBiasCard,
} from "@/components/MainView/TEAIInterpretBlock";

function formatFullTime(ts: number): string {
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

function formatPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

export default function TEAIDetailPage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";
  const ts = Number(params.ts);

  const [data, setData] = useState<TEAIDetailResponse | null>(null);
  const [error, setError] = useState("");
  const [showReasoning, setShowReasoning] = useState(false);

  useEffect(() => {
    if (!ts) return;
    fetch(`${API_BASE}/api/te/ai_interpret/${coin}/detail/${ts}`, {
      cache: "no-store",
    })
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((j: TEAIDetailResponse) => setData(j))
      .catch((e) => setError(`加载失败: ${(e as Error).message}`));
  }, [coin, ts]);

  const align = useMemo(
    () => (data ? ALIGNMENT_META[data.ai.alignment_with_rules] : null),
    [data],
  );
  const scenario = useMemo(
    () => (data ? SCENARIO_META[data.ai.scenario] : null),
    [data],
  );

  if (error) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center ai-detail-page">
        <div className="text-center">
          <div className="text-rose-400 text-lg mb-4">{error}</div>
          <Link href="/" className="text-blue-400 hover:text-blue-300 text-sm">
            ← 返回大屏
          </Link>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center ai-detail-page">
        <div className="animate-spin w-10 h-10 border-2 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  const ai = data.ai;
  const rules = data.rules_snapshot || {};

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300 ai-detail-page">
      {/* Header */}
      <header className="border-b border-slate-700 bg-slate-900/80 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between flex-wrap gap-3">
          <div className="flex items-center gap-4">
            <Link
              href="/"
              className="text-blue-400 hover:text-blue-300 text-sm shrink-0"
            >
              ← 返回大屏
            </Link>
            <div>
              <h1 className="text-lg font-bold text-white">
                🤖 {coin} · TE AI 解读详情
              </h1>
              <div className="text-xs text-slate-500 mt-0.5">
                {formatFullTime(data.ts)} · 触发时价格{" "}
                {formatPrice(data.price)}
              </div>
            </div>
          </div>
          <div className="text-[11px] text-slate-500 font-mono">
            fp: {data.fingerprint}
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-6 space-y-4">
        {/* ① 主结论卡 */}
        {align && scenario && (
          <div className={`rounded-lg border-2 p-4 ${align.bg}`}>
            <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
              <div className="flex items-center gap-2 flex-wrap">
                <span
                  className={`px-2 py-0.5 rounded text-[12px] font-semibold ${align.text} bg-slate-900/50 border border-current/40`}
                  title={align.desc}
                >
                  {align.label}
                </span>
                <span
                  className={`px-2 py-0.5 rounded text-[12px] ${scenario.color} bg-slate-900/50`}
                >
                  {scenario.label}
                </span>
                {ai.confidence > 0 && (
                  <span className="text-[11px] text-slate-400 font-mono">
                    置信度 {Math.round(ai.confidence * 100)}%
                  </span>
                )}
              </div>
            </div>
            <div className="text-base font-semibold text-slate-100 leading-snug">
              {ai.summary_cn || "（AI 未给出摘要）"}
            </div>
            {ai.alignment_reason && (
              <div className="mt-2 text-[12px] text-slate-400 leading-relaxed">
                <span className={align.text}>对齐理由：</span>
                {ai.alignment_reason}
              </div>
            )}
          </div>
        )}

        {/* ② 趋势评估 */}
        {ai.trend_assessment && <TrendAssessmentCard ta={ai.trend_assessment} />}

        {/* ③ 关键位投射 */}
        {ai.level_projection &&
          ai.level_projection.direction_tested !== "none" && (
            <LevelProjectionCard lp={ai.level_projection} />
          )}

        {/* ④ 交易倾向 */}
        {ai.trade_bias && <TradeBiasCard tb={ai.trade_bias} />}

        {/* ⑤ 规则侧快照（与 AI 对比） */}
        <div className="rounded-md border border-slate-700/60 bg-slate-900/40 p-3">
          <div className="text-[11px] font-semibold text-slate-300 mb-2">
            📏 规则侧快照（当时规则引擎的候选结论）
          </div>
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2 text-[11px]">
            <div className="flex items-baseline gap-1">
              <span className="text-slate-500">方向：</span>
              <span className="text-slate-200">
                {rules.overall_direction ?? "—"}
              </span>
            </div>
            <div className="flex items-baseline gap-1">
              <span className="text-slate-500">状态：</span>
              <span className="text-slate-200">
                {rules.overall_state ?? "—"}
              </span>
            </div>
            <div className="flex items-baseline gap-1">
              <span className="text-slate-500">行动：</span>
              <span className="text-slate-200">
                {rules.overall_action ?? "—"}
              </span>
            </div>
            <div className="flex items-baseline gap-1">
              <span className="text-slate-500">共识：</span>
              <span className="text-slate-200">
                {rules.consensus_level ?? "—"}
              </span>
            </div>
            <div className="flex items-baseline gap-1">
              <span className="text-slate-500">Regime：</span>
              <span className="text-slate-200">{rules.regime ?? "—"}</span>
              {rules.regime_vetoed && (
                <span className="text-rose-300 ml-1">(vetoed)</span>
              )}
            </div>
            <div className="flex items-baseline gap-1">
              <span className="text-slate-500">建议仓位：</span>
              <span className="text-slate-200">
                {rules.overall_position_pct != null
                  ? `${Math.round(rules.overall_position_pct)}%`
                  : "—"}
              </span>
            </div>
          </div>
        </div>

        {/* ⑥ 矛盾消解 */}
        {ai.conflict_resolution && (
          <div className="rounded-md border border-slate-700/60 bg-slate-900/40 p-3">
            <div className="text-[11px] font-semibold text-slate-300 mb-1">
              🧩 矛盾消解
            </div>
            <div className="text-[13px] text-slate-200 leading-relaxed">
              {ai.conflict_resolution}
            </div>
          </div>
        )}

        {/* ⑦ 陷阱提醒 */}
        {ai.traps && ai.traps.length > 0 && (
          <div className="rounded-md border border-rose-700/40 bg-rose-900/10 p-3">
            <div className="text-[11px] font-semibold text-rose-300 mb-1">
              ⚠ 陷阱提醒
            </div>
            <ul className="space-y-1 text-[13px] text-rose-100/90 leading-relaxed">
              {ai.traps.map((t, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-rose-400">•</span>
                  <span>{t}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ⑧ 触发条件 */}
        {ai.triggers_to_watch && ai.triggers_to_watch.length > 0 && (
          <div className="rounded-md border border-emerald-700/40 bg-emerald-900/10 p-3">
            <div className="text-[11px] font-semibold text-emerald-300 mb-1">
              🎯 等待这些信号再行动
            </div>
            <ul className="space-y-1 text-[13px] text-emerald-100/90 leading-relaxed">
              {ai.triggers_to_watch.map((t, i) => (
                <li key={i} className="flex gap-2">
                  <span className="text-emerald-400">•</span>
                  <span>{t}</span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ⑨ AI 独立观察 */}
        {ai.independent_view && (
          <div className="rounded-md border border-cyan-700/40 bg-cyan-900/10 p-3">
            <div className="text-[11px] font-semibold text-cyan-300 mb-1">
              💭 AI 的独立观察
            </div>
            <div className="text-[13px] text-cyan-100/90 leading-relaxed">
              {ai.independent_view}
            </div>
          </div>
        )}

        {/* ⑩ 综合行动建议 */}
        {ai.action_suggestion && (
          <div className="rounded-md border border-blue-700/40 bg-blue-900/10 p-3">
            <div className="text-[11px] font-semibold text-blue-300 mb-1">
              💡 综合行动建议
            </div>
            <div className="text-[13px] text-blue-100/90 leading-relaxed">
              {ai.action_suggestion}
            </div>
          </div>
        )}

        {/* ⑪ 思考链（折叠） */}
        {data.reasoning && (
          <div className="rounded-md border border-slate-700/60 bg-slate-900/40 p-3">
            <button
              onClick={() => setShowReasoning((v) => !v)}
              className="text-[12px] text-slate-300 hover:text-slate-100 underline underline-offset-2"
            >
              {showReasoning
                ? "收起思考过程"
                : `看 AI 是怎么想的（思考链 ${data.reasoning.length} 字）`}
            </button>
            {showReasoning && (
              <pre className="mt-2 max-h-[600px] overflow-auto rounded-md bg-slate-950/80 border border-slate-800 p-3 text-[11px] leading-relaxed text-slate-400 whitespace-pre-wrap">
                {data.reasoning}
              </pre>
            )}
          </div>
        )}

        {/* ⑫ 元信息 */}
        <div className="flex items-center justify-between pt-2 border-t border-slate-800 text-[11px] text-slate-500">
          <span>
            {data.model} · in {data.tokens_in}t · out {data.tokens_out}t
            {data.reasoning_tokens > 0 &&
              ` · reasoning ${data.reasoning_tokens}t`}
          </span>
          <span>
            {data.cache_hit
              ? `缓存命中（${data.from_cache_age_sec}s 前生成）`
              : `${(data.latency_ms / 1000).toFixed(1)}s`}
          </span>
        </div>

        {/* ⑬ 免责声明 */}
        <div className="mt-2 rounded border border-slate-800 bg-slate-950/40 px-3 py-2 text-[11px] text-slate-500 leading-relaxed">
          ⚠ AI 解读仅供参考，基于当时这一刻的读数 + 关键位做推理，不代表未来走势。最终决策请结合实盘自行判断。
        </div>
      </main>
    </div>
  );
}
