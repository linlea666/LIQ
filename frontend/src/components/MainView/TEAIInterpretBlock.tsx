"use client";

/**
 * TrendExhaustion · AI 深度解读区块（DeepSeek Reasoner 驱动）
 *
 * 定位：AI 是规则引擎的**审计员 + 再判断者**（不是翻译器）
 *   - 规则只是初步整理数据，AI 拿 sub 原始读数 + 关键位快照再判一遍
 *   - 有权推翻规则的 overall_direction；必须引用具体数值证据
 *
 * 架构：fire-and-forget + WebSocket 推送
 *   1. 点击按钮 → POST /api/te/ai_interpret/{coin} → 秒回
 *   2. 后端跑 Reasoner（30-120s），完成后 push `te_ai_result`
 *   3. 失败：后端 push `te_ai_error`，前端在 store 里标红
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";
import type {
  TEAIAlignment,
  TEAIScenario,
  TEBreakLikelihood,
  TELevelProjection,
  TEMomentumDirection,
  TEMomentumQuality,
  TEPrimaryTrend,
  TETradeBias,
  TETradeDirection,
  TETradeStrength,
  TETrendAssessment,
} from "@/lib/types";

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 元数据映射
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export const ALIGNMENT_META: Record<
  TEAIAlignment,
  { label: string; bg: string; text: string; desc: string }
> = {
  agree: {
    label: "AI 确认",
    bg: "bg-emerald-500/15 border-emerald-500/50",
    text: "text-emerald-300",
    desc: "AI 同意规则的方向与状态判断",
  },
  partial_disagree: {
    label: "AI 有补充",
    bg: "bg-amber-500/15 border-amber-500/50",
    text: "text-amber-300",
    desc: "方向一致但时机或力度 AI 有不同看法",
  },
  strong_disagree: {
    label: "AI 推翻规则·请警惕",
    bg: "bg-rose-500/15 border-rose-500/50",
    text: "text-rose-300",
    desc: "AI 认为规则的方向或状态判错了",
  },
  neutral: {
    label: "AI 独立观察",
    bg: "bg-cyan-500/15 border-cyan-500/50",
    text: "text-cyan-300",
    desc: "AI 既不赞成也不反对规则，给出独立视角",
  },
  insufficient: {
    label: "证据不足",
    bg: "bg-slate-500/15 border-slate-500/40",
    text: "text-slate-300",
    desc: "数据不足或矛盾太多，AI 不敢下判",
  },
};

export const SCENARIO_META: Record<TEAIScenario, { label: string; color: string }> = {
  trend_continuation: { label: "顺势续航", color: "text-emerald-300" },
  bear_rebound: { label: "熊市反弹", color: "text-orange-300" },
  bull_pullback: { label: "牛市回调", color: "text-blue-300" },
  reversal_early: { label: "反转早期", color: "text-amber-300" },
  reversal_confirmed: { label: "反转确认", color: "text-rose-300" },
  choppy_range: { label: "震荡观望", color: "text-purple-300" },
  unclear: { label: "场景不清", color: "text-slate-400" },
};

const PRIMARY_TREND_META: Record<
  TEPrimaryTrend,
  { label: string; icon: string; color: string }
> = {
  uptrend: { label: "上涨趋势", icon: "📈", color: "text-emerald-300" },
  downtrend: { label: "下跌趋势", icon: "📉", color: "text-rose-300" },
  sideways: { label: "横盘震荡", icon: "⚖️", color: "text-purple-300" },
  transition: { label: "过渡阶段", icon: "🔀", color: "text-amber-300" },
};

const MOMENTUM_QUALITY_META: Record<
  TEMomentumQuality,
  { label: string; color: string; bar: number }
> = {
  fuel_full: { label: "动能充足", color: "text-emerald-300", bar: 100 },
  fuel_adequate: { label: "动能尚可", color: "text-emerald-400", bar: 75 },
  fuel_fading: { label: "动能衰减", color: "text-amber-300", bar: 40 },
  fuel_exhausted: { label: "动能耗尽", color: "text-rose-400", bar: 15 },
  unclear: { label: "动能不清", color: "text-slate-400", bar: 50 },
};

const MOMENTUM_DIRECTION_META: Record<
  TEMomentumDirection,
  { label: string; color: string }
> = {
  accelerating: { label: "↑ 加速", color: "text-emerald-400" },
  stable: { label: "→ 稳定", color: "text-sky-300" },
  decelerating: { label: "↓ 减速", color: "text-amber-400" },
  unclear: { label: "— 未知", color: "text-slate-500" },
};

const BREAK_LIKELIHOOD_META: Record<
  TEBreakLikelihood,
  { label: string; color: string; bar: number }
> = {
  very_likely: { label: "很可能突破", color: "text-emerald-300", bar: 85 },
  likely: { label: "可能突破", color: "text-emerald-400", bar: 65 },
  uncertain: { label: "未定", color: "text-slate-300", bar: 50 },
  unlikely: { label: "难以突破", color: "text-amber-400", bar: 30 },
  very_unlikely: { label: "很难突破", color: "text-rose-400", bar: 15 },
  insufficient: { label: "证据不足", color: "text-slate-500", bar: 0 },
};

const TRADE_DIRECTION_META: Record<
  TETradeDirection,
  { label: string; color: string; bg: string; icon: string }
> = {
  long: {
    label: "试做多",
    color: "text-emerald-200",
    bg: "bg-emerald-500/15 border-emerald-500/50",
    icon: "🟢",
  },
  short: {
    label: "试做空",
    color: "text-rose-200",
    bg: "bg-rose-500/15 border-rose-500/50",
    icon: "🔴",
  },
  neutral: {
    label: "观望中性",
    color: "text-slate-300",
    bg: "bg-slate-500/15 border-slate-500/40",
    icon: "⚪",
  },
  avoid: {
    label: "回避交易",
    color: "text-purple-200",
    bg: "bg-purple-500/15 border-purple-500/50",
    icon: "⛔",
  },
};

const TRADE_STRENGTH_META: Record<TETradeStrength, string> = {
  probe: "小仓试探",
  standard: "标准仓位",
  strong: "强信号",
  none: "—",
};

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 小组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.round(Math.max(0, Math.min(1, value)) * 100);
  const color =
    value >= 0.7
      ? "bg-emerald-400"
      : value >= 0.5
      ? "bg-amber-400"
      : "bg-rose-400";
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 rounded-full bg-slate-800 overflow-hidden max-w-[120px]">
        <div
          className={`h-full ${color} transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10px] font-mono text-slate-400">{pct}%</span>
    </div>
  );
}

function MomentumGauge({
  quality,
}: {
  quality: TEMomentumQuality;
}) {
  const meta = MOMENTUM_QUALITY_META[quality];
  const barColor =
    meta.bar >= 70
      ? "bg-emerald-500"
      : meta.bar >= 40
      ? "bg-amber-500"
      : "bg-rose-500";
  return (
    <div className="flex items-center gap-2 flex-1 min-w-0">
      <span className={`text-xs font-semibold ${meta.color} shrink-0`}>
        {meta.label}
      </span>
      <div className="flex-1 h-1.5 rounded-full bg-slate-800 overflow-hidden max-w-[100px]">
        <div
          className={`h-full ${barColor} transition-all`}
          style={{ width: `${meta.bar}%` }}
        />
      </div>
    </div>
  );
}

function BreakLikelihoodBar({
  likelihood,
  conviction,
}: {
  likelihood: TEBreakLikelihood;
  conviction: number;
}) {
  const meta = BREAK_LIKELIHOOD_META[likelihood];
  // 优先用 conviction（AI 量化副字段），未给时用档位默认值
  const pct = Math.round(
    Math.max(0, Math.min(1, conviction)) * 100 || meta.bar,
  );
  const barColor =
    pct >= 70 ? "bg-emerald-500" : pct >= 40 ? "bg-slate-400" : "bg-rose-500";
  return (
    <div className="flex items-center gap-2">
      <span className={`text-xs font-semibold ${meta.color}`}>
        {meta.label}
      </span>
      <div className="flex-1 h-2 rounded-full bg-slate-800 overflow-hidden max-w-[160px]">
        <div
          className={`h-full ${barColor} transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-[10px] font-mono text-slate-500">{pct}%</span>
    </div>
  );
}

export function formatCacheAge(sec: number): string {
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  return `${Math.floor(sec / 3600)}h 前`;
}

function formatPrice(v: number | null | undefined): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return "—";
  return `$${v.toLocaleString("en-US", { maximumFractionDigits: 2 })}`;
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 主组件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export default function TEAIInterpretBlock({ coin }: { coin: string }) {
  const result = useMarketStore((s) => s.teAiByCoin[coin.toUpperCase()] ?? null);
  const loading = useMarketStore(
    (s) => s.teAiLoadingByCoin[coin.toUpperCase()] ?? false,
  );
  const err = useMarketStore(
    (s) => s.teAiErrorByCoin[coin.toUpperCase()] ?? "",
  );
  const setTEAILoading = useMarketStore((s) => s.setTEAILoading);
  const setTEAIError = useMarketStore((s) => s.setTEAIError);

  const [showReasoning, setShowReasoning] = useState(false);
  const [waitedSec, setWaitedSec] = useState(0);

  const startTsRef = useRef(0);
  useEffect(() => {
    if (!loading) {
      setWaitedSec(0);
      return;
    }
    startTsRef.current = Date.now();
    const timer = setInterval(() => {
      setWaitedSec(Math.round((Date.now() - startTsRef.current) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [loading]);

  const trigger = useCallback(
    async (force: boolean) => {
      if (!coin) return;
      const c = coin.toUpperCase();
      setTEAILoading(c, true);
      setTEAIError(c, null);
      try {
        const url = `${API_BASE}/api/te/ai_interpret/${c}${
          force ? "?force=true" : ""
        }`;
        const r = await fetch(url, { method: "POST", cache: "no-store" });
        if (!r.ok) {
          const txt = await r.text();
          throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
        }
        const j = (await r.json()) as {
          status: "processing" | "cached" | "inflight" | "error";
          message?: string;
        };
        if (j.status === "error") {
          setTEAIError(c, j.message || "AI 触发失败");
        }
      } catch (e) {
        setTEAIError(c, (e as Error).message || "AI 解读失败");
      }
    },
    [coin, setTEAILoading, setTEAIError],
  );

  const align = result ? ALIGNMENT_META[result.alignment_with_rules] : null;
  const scenario = result ? SCENARIO_META[result.scenario] : null;

  const buttonLabel = useMemo(() => {
    if (loading) return `🤖 AI 思考中… ${waitedSec}s`;
    if (!result) return "🤖 用 AI 深度解读当前信号";
    if (result.cache_hit)
      return `🤖 已解读（${formatCacheAge(
        result.from_cache_age_sec,
      )}·点击重新解读）`;
    return "🤖 重新解读";
  }, [loading, waitedSec, result]);

  return (
    <div className="rounded-xl border border-slate-700/60 bg-slate-900/40 p-3">
      {/* 标题 + 按钮 */}
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold text-slate-200">
            🤖 AI 深度解读
          </span>
          <span className="text-[10px] text-slate-500">
            DeepSeek Reasoner · 审计规则 + 关键位再判断
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => trigger(!!result)}
            disabled={loading}
            className={`rounded-md border px-2.5 py-1 text-xs font-medium transition disabled:opacity-50 ${
              result
                ? "border-slate-700 bg-slate-900 hover:border-blue-500 text-slate-200"
                : "border-blue-500/60 bg-blue-500/10 hover:bg-blue-500/20 text-blue-300"
            }`}
          >
            {buttonLabel}
          </button>
        </div>
      </div>

      {/* 初始提示 */}
      {!result && !loading && !err && (
        <div className="mt-3 text-[12px] text-slate-500 leading-relaxed">
          AI 作为<span className="text-slate-300">规则的审计员 + 再判断者</span>，基于 sub 原始读数 +
          关键位 S/A 级强位 + 牛熊分界 + 挤压带，输出：<br />
          <span className="text-slate-400">①</span> 趋势评估（可推翻规则的方向判断）<br />
          <span className="text-slate-400">②</span> 关键位投射（突破可能性分档 + 量化置信度）<br />
          <span className="text-slate-400">③</span> 矛盾消解 + 陷阱 + 触发条件<br />
          <span className="text-slate-400">④</span> 交易倾向（方向 + 区间 + 失效位 + 时间窗）
        </div>
      )}

      {/* 加载中 */}
      {loading && (
        <div className="mt-3 space-y-1.5">
          <div className="text-[12px] text-slate-400 animate-pulse">
            正在审计规则 + 读取关键位 + 推理矛盾场景…（Reasoner 思考典型 30-90s）
          </div>
          <div className="flex items-center gap-2 text-[11px] text-slate-500">
            <span>已等待 {waitedSec}s</span>
            <div className="flex-1 h-1 rounded-full bg-slate-800 overflow-hidden max-w-[240px]">
              <div
                className="h-full bg-blue-500/60 transition-all"
                style={{ width: `${Math.min(100, (waitedSec / 90) * 100)}%` }}
              />
            </div>
            <span className="text-slate-600">WebSocket 推送到达即显示</span>
          </div>
        </div>
      )}

      {/* 错误 */}
      {err && !loading && (
        <div className="mt-3 rounded-md border border-rose-700/50 bg-rose-900/20 px-3 py-2 text-xs text-rose-200">
          ⚠ {err}
        </div>
      )}

      {/* 结果渲染 */}
      {result && !loading && !result.error && (
        <div className="mt-3 space-y-3">
          {/* ① 主结论卡：对齐标签 + 场景 + 置信度 + 一句话 */}
          {align && scenario && (
            <div className={`rounded-lg border-2 p-3 ${align.bg}`}>
              <div className="flex items-center justify-between flex-wrap gap-2 mb-2">
                <div className="flex items-center gap-2 flex-wrap">
                  <span
                    className={`px-2 py-0.5 rounded text-[11px] font-semibold ${align.text} bg-slate-900/50 border border-current/40`}
                    title={align.desc}
                  >
                    {align.label}
                  </span>
                  <span
                    className={`px-2 py-0.5 rounded text-[11px] ${scenario.color} bg-slate-900/50`}
                  >
                    {scenario.label}
                  </span>
                </div>
                <ConfidenceBar value={result.confidence} />
              </div>
              <div className="text-base font-semibold text-slate-100 leading-snug">
                {result.summary_cn || "（AI 未给出结论）"}
              </div>
              {result.alignment_reason && (
                <div className="mt-2 text-[11px] text-slate-400 leading-relaxed">
                  <span className={align.text}>对齐理由：</span>
                  {result.alignment_reason}
                </div>
              )}
            </div>
          )}

          {/* ② 趋势评估卡（新） */}
          {result.trend_assessment && (
            <TrendAssessmentCard ta={result.trend_assessment} />
          )}

          {/* ③ 关键位投射卡（新） */}
          {result.level_projection &&
            result.level_projection.direction_tested !== "none" && (
              <LevelProjectionCard lp={result.level_projection} />
            )}

          {/* ④ 交易倾向卡（neutral/avoid 也显示，让 AI 立场对用户可见） */}
          {result.trade_bias && <TradeBiasCard tb={result.trade_bias} />}

          {/* ⑤ 矛盾消解 */}
          {result.conflict_resolution && (
            <div className="rounded-md border border-slate-700/60 bg-slate-900/40 p-2.5">
              <div className="text-[11px] font-semibold text-slate-300 mb-1">
                🧩 矛盾消解
              </div>
              <div className="text-[12px] text-slate-300 leading-relaxed">
                {result.conflict_resolution}
              </div>
            </div>
          )}

          {/* ⑥ 陷阱提醒 */}
          {result.traps.length > 0 && (
            <div className="rounded-md border border-rose-700/40 bg-rose-900/10 p-2.5">
              <div className="text-[11px] font-semibold text-rose-300 mb-1">
                ⚠ 陷阱提醒
              </div>
              <ul className="space-y-0.5 text-[12px] text-rose-100/90 leading-relaxed">
                {result.traps.map((t, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-rose-400">•</span>
                    <span>{t}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ⑦ 触发条件 */}
          {result.triggers_to_watch.length > 0 && (
            <div className="rounded-md border border-emerald-700/40 bg-emerald-900/10 p-2.5">
              <div className="text-[11px] font-semibold text-emerald-300 mb-1">
                🎯 等待这些信号再行动
              </div>
              <ul className="space-y-0.5 text-[12px] text-emerald-100/90 leading-relaxed">
                {result.triggers_to_watch.map((t, i) => (
                  <li key={i} className="flex gap-2">
                    <span className="text-emerald-400">•</span>
                    <span>{t}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* ⑧ AI 独立观察（新，可空） */}
          {result.independent_view && (
            <div className="rounded-md border border-cyan-700/40 bg-cyan-900/10 p-2.5">
              <div className="text-[11px] font-semibold text-cyan-300 mb-1">
                💭 AI 的独立观察
              </div>
              <div className="text-[12px] text-cyan-100/90 leading-relaxed">
                {result.independent_view}
              </div>
            </div>
          )}

          {/* ⑨ 综合行动建议 */}
          {result.action_suggestion && (
            <div className="rounded-md border border-blue-700/40 bg-blue-900/10 p-2.5">
              <div className="text-[11px] font-semibold text-blue-300 mb-1">
                💡 综合行动建议
              </div>
              <div className="text-[12px] text-blue-100/90 leading-relaxed">
                {result.action_suggestion}
              </div>
            </div>
          )}

          {/* ⑩ 思考链（折叠） */}
          {result.reasoning && (
            <div className="pt-1">
              <button
                onClick={() => setShowReasoning((v) => !v)}
                className="text-[11px] text-slate-500 hover:text-slate-300 underline underline-offset-2"
              >
                {showReasoning ? "收起思考过程" : "看 AI 是怎么想的（思考链）"}
              </button>
              {showReasoning && (
                <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-slate-950/80 border border-slate-800 p-2 text-[10px] leading-relaxed text-slate-400 whitespace-pre-wrap">
                  {result.reasoning}
                </pre>
              )}
            </div>
          )}

          {/* ⑪ 元信息 */}
          <div className="flex items-center justify-between pt-2 border-t border-slate-800 text-[10px] text-slate-600">
            <span>
              {result.model} · in {result.tokens_in}t · out{" "}
              {result.tokens_out}t
              {result.reasoning_tokens > 0 &&
                ` · reasoning ${result.reasoning_tokens}t`}
            </span>
            <span>
              {result.cache_hit
                ? `缓存命中（${formatCacheAge(result.from_cache_age_sec)}）`
                : `${(result.latency_ms / 1000).toFixed(1)}s`}
            </span>
          </div>

          {/* ⑫ 免责声明 */}
          <div className="mt-1 rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5 text-[10px] text-slate-500 leading-relaxed">
            ⚠ AI 解读仅供参考，基于当前这一刻的读数 + 关键位做推理，不代表未来走势。最终决策请结合实盘自行判断。
          </div>
        </div>
      )}
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 子卡片：趋势评估
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function TrendAssessmentCard({ ta }: { ta: TETrendAssessment }) {
  const pt = PRIMARY_TREND_META[ta.primary_trend];
  const md = MOMENTUM_DIRECTION_META[ta.momentum_direction];
  return (
    <div className="rounded-md border border-slate-700/60 bg-slate-900/40 p-2.5">
      <div className="text-[11px] font-semibold text-slate-300 mb-1.5">
        🧭 趋势评估（AI 独立判断）
      </div>
      <div className="flex items-center gap-3 flex-wrap text-sm">
        <div className={`flex items-center gap-1.5 ${pt.color} font-semibold`}>
          <span>{pt.icon}</span>
          <span>{pt.label}</span>
        </div>
        <div className="h-4 w-px bg-slate-700" />
        <MomentumGauge quality={ta.momentum_quality} />
        <div className="h-4 w-px bg-slate-700" />
        <span className={`text-xs font-semibold ${md.color}`}>{md.label}</span>
      </div>
      {ta.health_summary_cn && (
        <div className="mt-2 text-[12px] text-slate-200 leading-relaxed">
          {ta.health_summary_cn}
        </div>
      )}
      {ta.evidence_cn && (
        <div className="mt-1 text-[11px] text-slate-500 leading-relaxed">
          <span className="text-slate-400">证据：</span>
          {ta.evidence_cn}
        </div>
      )}
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 子卡片：关键位投射
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function LevelProjectionCard({ lp }: { lp: TELevelProjection }) {
  const directionLabel =
    lp.direction_tested === "resistance"
      ? "测试阻力"
      : lp.direction_tested === "support"
      ? "测试支撑"
      : lp.direction_tested === "both"
      ? "双向测试"
      : "—";
  return (
    <div className="rounded-md border border-indigo-700/40 bg-indigo-900/10 p-2.5">
      <div className="flex items-center justify-between flex-wrap gap-2 mb-1.5">
        <div className="text-[11px] font-semibold text-indigo-300">
          🎯 关键位投射
        </div>
        <div className="flex items-center gap-2 text-[11px] text-slate-400">
          <span>
            {directionLabel}
            <span className="ml-1.5 font-mono text-slate-200">
              {formatPrice(lp.target_level)}
            </span>
          </span>
        </div>
      </div>
      <BreakLikelihoodBar
        likelihood={lp.break_likelihood}
        conviction={lp.break_conviction}
      />
      {lp.reasoning_cn && (
        <div className="mt-2 text-[11px] text-slate-400 leading-relaxed">
          <span className="text-slate-300">理由：</span>
          {lp.reasoning_cn}
        </div>
      )}
      {(lp.if_break_cn || lp.if_fail_cn) && (
        <div className="mt-2 grid grid-cols-1 sm:grid-cols-2 gap-1.5 text-[11px]">
          {lp.if_break_cn && (
            <div className="rounded bg-emerald-900/20 border border-emerald-700/30 px-2 py-1 text-emerald-200">
              <span className="text-emerald-400">✓ 若突破：</span>
              {lp.if_break_cn}
            </div>
          )}
          {lp.if_fail_cn && (
            <div className="rounded bg-rose-900/20 border border-rose-700/30 px-2 py-1 text-rose-200">
              <span className="text-rose-400">✗ 若失败：</span>
              {lp.if_fail_cn}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 子卡片：交易倾向
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

export function TradeBiasCard({ tb }: { tb: TETradeBias }) {
  const meta = TRADE_DIRECTION_META[tb.direction];
  const isStandby = tb.direction === "neutral" || tb.direction === "avoid";
  // 只有真正"有入场倾向"时才显示三栏（入场/失效/时间窗）
  const hasActionable =
    !isStandby && (tb.entry_zone_cn || tb.invalidation_cn || tb.timeframe_cn);

  // standby 态的标题措辞更明确，避免用户误以为"有单可开"
  const title = isStandby
    ? tb.direction === "avoid"
      ? `${meta.icon} AI 建议：回避交易`
      : `${meta.icon} AI 建议：暂不开单 · 观望`
    : `${meta.icon} 交易倾向（AI 建议 · 非强制）`;

  return (
    <div className={`rounded-md border p-2.5 ${meta.bg}`}>
      <div className="flex items-center justify-between flex-wrap gap-2 mb-1.5">
        <div className={`text-[11px] font-semibold ${meta.color}`}>
          {title}
        </div>
        <div className="text-[11px] text-slate-400">
          <span className="mr-2">
            <span className="text-slate-500">方向：</span>
            <span className={`${meta.color} font-semibold`}>{meta.label}</span>
          </span>
          {tb.strength !== "none" && (
            <span>
              <span className="text-slate-500">强度：</span>
              <span className="text-slate-200">
                {TRADE_STRENGTH_META[tb.strength]}
              </span>
            </span>
          )}
        </div>
      </div>
      {hasActionable && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-1.5 text-[11px]">
          {tb.entry_zone_cn && (
            <div className="rounded bg-slate-900/60 px-2 py-1">
              <div className="text-[10px] text-slate-500">入场区</div>
              <div className="text-slate-200">{tb.entry_zone_cn}</div>
            </div>
          )}
          {tb.invalidation_cn && (
            <div className="rounded bg-slate-900/60 px-2 py-1">
              <div className="text-[10px] text-slate-500">失效位</div>
              <div className="text-rose-200">{tb.invalidation_cn}</div>
            </div>
          )}
          {tb.timeframe_cn && (
            <div className="rounded bg-slate-900/60 px-2 py-1">
              <div className="text-[10px] text-slate-500">时间窗</div>
              <div className="text-slate-200">{tb.timeframe_cn}</div>
            </div>
          )}
        </div>
      )}
      {/* standby 态：即便没有入场区也显示失效位（作为"回到交易区的触发点"） */}
      {isStandby && tb.invalidation_cn && !hasActionable && (
        <div className="text-[11px] text-slate-400">
          <span className="text-slate-500">观察失效位：</span>
          <span className="text-rose-300">{tb.invalidation_cn}</span>
        </div>
      )}
      {tb.why_cn && (
        <div className="mt-2 text-[11px] text-slate-400 leading-relaxed">
          <span className="text-slate-300">理由：</span>
          {tb.why_cn}
        </div>
      )}
    </div>
  );
}

