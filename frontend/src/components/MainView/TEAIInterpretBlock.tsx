"use client";

/**
 * TrendExhaustion · AI 解读区块（DeepSeek Reasoner 驱动）
 *
 * 设计原则：
 *   - 按钮常驻，但默认不亮；有缓存时显示缓存年龄
 *   - 三段式渲染：结论 + 矛盾消解 / 陷阱 / 触发条件 + AI 对齐规则的标签
 *   - 思考过程（reasoning_content）默认折叠，点"看 AI 怎么想的"展开
 *   - 错误兜底：AI 未配置 / 超时都直接展示 error 文案，不 crash
 *
 * 与规则引擎的关系：这是**补充层**，规则侧结论仍然主导。
 * AI alignment_with_rules 会以颜色标签提醒用户"AI 是否认同规则"。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/constants";

type AlignmentKind =
  | "agree"
  | "partial_disagree"
  | "strong_disagree"
  | "insufficient";

type ScenarioKind =
  | "trend_continuation"
  | "bear_rebound"
  | "bull_pullback"
  | "reversal_early"
  | "reversal_confirmed"
  | "choppy_range"
  | "unclear";

interface TEAIInterpretation {
  coin: string;
  ts: number;
  signal_fingerprint: string;
  model: string;
  cache_hit: boolean;
  from_cache_age_sec: number;
  latency_ms: number;
  tokens_in: number;
  tokens_out: number;
  reasoning_tokens: number;
  summary_cn: string;
  scenario: ScenarioKind;
  conflict_resolution: string;
  traps: string[];
  triggers_to_watch: string[];
  action_suggestion: string;
  confidence: number;
  alignment_with_rules: AlignmentKind;
  alignment_reason: string;
  reasoning: string;
  error: string | null;
  raw_text: string;
}

const ALIGNMENT_META: Record<
  AlignmentKind,
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

const SCENARIO_META: Record<ScenarioKind, { label: string; color: string }> = {
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

// 轮询配置
const POLL_INTERVAL_MS = 2500;
const MAX_WAIT_MS = 180_000; // Reasoner 最长允许 3 分钟

// 后端响应形状（pending 时字段少）
type BackendResp =
  | (TEAIInterpretation & { status: "done" | "error" })
  | {
      status: "pending";
      signal_fingerprint: string;
      coin: string;
      message: string;
      eta_sec: number;
    };

export default function TEAIInterpretBlock({ coin }: { coin: string }) {
  const [result, setResult] = useState<TEAIInterpretation | null>(null);
  const [loading, setLoading] = useState(false);
  const [waitedSec, setWaitedSec] = useState(0);
  const [err, setErr] = useState("");
  const [showReasoning, setShowReasoning] = useState(false);

  // 取消标志（切币种 / 组件卸载时中断轮询）
  const abortRef = useRef(false);

  // 等待计时器
  useEffect(() => {
    if (!loading) return;
    const t0 = Date.now();
    setWaitedSec(0);
    const timer = setInterval(() => {
      setWaitedSec(Math.round((Date.now() - t0) / 1000));
    }, 1000);
    return () => clearInterval(timer);
  }, [loading]);

  // 切币种时中断正在轮询的请求
  useEffect(() => {
    abortRef.current = true;
    setResult(null);
    setErr("");
    setLoading(false);
    abortRef.current = false;
  }, [coin]);

  const fetchInterpret = useCallback(
    async (force = false) => {
      if (!coin) return;
      abortRef.current = false;
      setLoading(true);
      setErr("");

      const startTs = Date.now();
      try {
        // 第一次请求带 force（若用户点的是"重新解读"），后续轮询永远不带
        let firstCall = true;
        while (!abortRef.current) {
          if (Date.now() - startTs > MAX_WAIT_MS) {
            throw new Error(
              `AI 思考超时（${Math.round(MAX_WAIT_MS / 1000)}s），请稍后重试`,
            );
          }
          const url = `${API_BASE}/api/te/ai_interpret/${coin}${
            firstCall && force ? "?force=true" : ""
          }`;
          firstCall = false;
          const r = await fetch(url, { cache: "no-store" });
          if (!r.ok) {
            const txt = await r.text();
            throw new Error(`HTTP ${r.status}: ${txt.slice(0, 200)}`);
          }
          const j = (await r.json()) as BackendResp;

          if (j.status === "pending") {
            await new Promise((resolve) =>
              setTimeout(resolve, POLL_INTERVAL_MS),
            );
            continue;
          }

          // done 或 error
          const full = j as TEAIInterpretation & { status: string };
          setResult(full);
          if (full.error) setErr(full.error);
          return;
        }
      } catch (e) {
        setErr((e as Error).message || "AI 解读失败");
      } finally {
        setLoading(false);
      }
    },
    [coin],
  );

  const align = result ? ALIGNMENT_META[result.alignment_with_rules] : null;
  const scenario = result ? SCENARIO_META[result.scenario] : null;

  const buttonLabel = useMemo(() => {
    if (loading) return `🤖 AI 思考中… ${waitedSec}s`;
    if (!result) return "🤖 用 AI 深度解读当前信号";
    if (result.cache_hit)
      return `🤖 已解读（${formatCacheAge(
        result.from_cache_age_sec,
      )}·点击重新思考）`;
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
            DeepSeek Reasoner · 仅补充规则给不了的层次
          </span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => fetchInterpret(!!result)}
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

      {/* 加载中占位 */}
      {loading && (
        <div className="mt-3 space-y-1.5">
          <div className="text-[12px] text-slate-400 animate-pulse">
            正在读取多周期数据 + 推理矛盾场景…（Reasoner 首次调用典型 20-60s）
          </div>
          <div className="flex items-center gap-2 text-[11px] text-slate-500">
            <span>已等待 {waitedSec}s</span>
            <div className="flex-1 h-1 rounded-full bg-slate-800 overflow-hidden max-w-[240px]">
              <div
                className="h-full bg-blue-500/60 transition-all"
                style={{
                  width: `${Math.min(100, (waitedSec / 60) * 100)}%`,
                }}
              />
            </div>
            <span className="text-slate-600">{">60s 属于正常"}</span>
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

      {/* AI 返回 error 且非 HTTP 错误 */}
      {result?.error && !loading && (
        <div className="mt-3 rounded-md border border-amber-700/50 bg-amber-900/20 px-3 py-2 text-xs text-amber-200">
          ⚠ AI 解读失败：{result.error}
        </div>
      )}
    </div>
  );
}
