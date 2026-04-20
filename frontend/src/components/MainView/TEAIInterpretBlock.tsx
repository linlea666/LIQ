"use client";

/**
 * TrendExhaustion · AI 深度解读区块（DeepSeek Reasoner 驱动）
 *
 * 架构对齐主 AI 模式（fire-and-forget + WebSocket 推送）：
 *   1. 点击按钮 → POST /api/te/ai_interpret/{coin} → 秒回 processing/cached/inflight
 *   2. 若 cached：后端同步 push `te_ai_result`，前端 store 即刻更新
 *   3. 否则：后端跑 Reasoner（30-120s），完成后 push `te_ai_result`
 *   4. 失败：后端 push `te_ai_error`，前端在 store 里标红
 *   5. 订阅币种时：若有近期缓存，后端会 replay 一次 `te_ai_result`
 *
 * 本组件**只从 store 读**，不再做 HTTP 轮询。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/constants";
import { useMarketStore } from "@/stores/marketStore";
import type { TEAIAlignment, TEAIScenario } from "@/lib/types";

const ALIGNMENT_META: Record<
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
    label: "AI 不同意·请警惕",
    bg: "bg-rose-500/15 border-rose-500/50",
    text: "text-rose-300",
    desc: "AI 认为规则的方向或状态判错了",
  },
  insufficient: {
    label: "证据不足",
    bg: "bg-slate-500/15 border-slate-500/40",
    text: "text-slate-300",
    desc: "数据不足或矛盾太多，AI 不敢下判",
  },
};

const SCENARIO_META: Record<TEAIScenario, { label: string; color: string }> = {
  trend_continuation: { label: "顺势续航", color: "text-emerald-300" },
  bear_rebound: { label: "熊市反弹", color: "text-orange-300" },
  bull_pullback: { label: "牛市回调", color: "text-blue-300" },
  reversal_early: { label: "反转早期", color: "text-amber-300" },
  reversal_confirmed: { label: "反转确认", color: "text-rose-300" },
  choppy_range: { label: "震荡观望", color: "text-purple-300" },
  unclear: { label: "场景不清", color: "text-slate-400" },
};

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

function formatCacheAge(sec: number): string {
  if (sec < 60) return `${sec}s 前`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m 前`;
  return `${Math.floor(sec / 3600)}h 前`;
}

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

  // 等待计时器（从按下按钮开始计；结果到达 loading 自动变 false → 停）
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
        // cached / inflight：后端已同步 push 或即将 push，状态由 WS 驱动
        // processing：等 WS 推
        // error：直接展示
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
            DeepSeek Reasoner · 结果通过 WebSocket 推送
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
          AI 只回答规则给不了的 3 类问题：<br />
          <span className="text-slate-400">①</span> 各周期/因子冲突时，真相最可能是什么场景？<br />
          <span className="text-slate-400">②</span> 如果按规则行动，最容易踩的坑是哪些？<br />
          <span className="text-slate-400">③</span> 还需要等哪些信号来确认或推翻？
        </div>
      )}

      {/* 加载中占位（WS 推送前的 UX） */}
      {loading && (
        <div className="mt-3 space-y-1.5">
          <div className="text-[12px] text-slate-400 animate-pulse">
            正在读取多周期数据 + 推理矛盾场景…（Reasoner 思考典型 20-90s）
          </div>
          <div className="flex items-center gap-2 text-[11px] text-slate-500">
            <span>已等待 {waitedSec}s</span>
            <div className="flex-1 h-1 rounded-full bg-slate-800 overflow-hidden max-w-[240px]">
              <div
                className="h-full bg-blue-500/60 transition-all"
                style={{ width: `${Math.min(100, (waitedSec / 60) * 100)}%` }}
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
          {/* ① 主结论 + 对齐标签 + 置信度 */}
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
              {result.action_suggestion && (
                <div className="mt-1.5 text-sm text-blue-300">
                  💡 {result.action_suggestion}
                </div>
              )}
              {result.alignment_reason && (
                <div className="mt-2 text-[11px] text-slate-400 leading-relaxed">
                  <span className={align.text}>AI 对齐规则：</span>
                  {result.alignment_reason}
                </div>
              )}
            </div>
          )}

          {/* ② 矛盾消解 */}
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

          {/* ③ 陷阱提醒 */}
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

          {/* ④ 触发条件 */}
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

          {/* ⑤ 思考过程（折叠） */}
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

          {/* ⑥ 元信息 */}
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
        </div>
      )}
    </div>
  );
}
