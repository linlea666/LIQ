"use client";

import { useEffect, useState, useCallback } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import type { AIQualityResponse, AIQualityRecord } from "@/lib/types";

/**
 * P1.8c · AI 分析质量独立调试页
 *
 * 职责：
 *   - 展示 /api/ai-quality/{coin} 全量数据
 *   - 顶部：币种切换 + 刷新 + 总样本数
 *   - 中段：核心指标卡片（命中率 / 冲突率 / 延迟 / token）
 *   - 尾部：最近 50 条原始记录明细
 *   - 说明面板：每个指标的判定口径
 */

const SUPPORTED_COINS = ["BTC", "ETH", "SOL"] as const;
const POLL_INTERVAL_MS = 30_000;

type SupportedCoin = (typeof SUPPORTED_COINS)[number];

export default function AIQualityPage() {
  const params = useParams();
  const rawCoin = String(params.coin ?? "BTC").toUpperCase();
  const coin: SupportedCoin = SUPPORTED_COINS.includes(rawCoin as SupportedCoin)
    ? (rawCoin as SupportedCoin)
    : "BTC";

  const [data, setData] = useState<AIQualityResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [err, setErr] = useState<string>("");
  const [autoRefresh, setAutoRefresh] = useState<boolean>(true);

  const fetchOnce = useCallback(async () => {
    try {
      setLoading(true);
      const r = await fetch(`${API_BASE}/api/ai-quality/${coin}?limit=50`);
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const j: AIQualityResponse = await r.json();
      setData(j);
      setErr("");
    } catch (e) {
      setErr(e instanceof Error ? e.message : "fetch error");
    } finally {
      setLoading(false);
    }
  }, [coin]);

  useEffect(() => {
    fetchOnce();
    if (!autoRefresh) return;
    const t = setInterval(fetchOnce, POLL_INTERVAL_MS);
    return () => clearInterval(t);
  }, [fetchOnce, autoRefresh]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <div className="max-w-6xl mx-auto p-6 space-y-6">
        {/* ── 顶部导航 ── */}
        <header className="flex items-center justify-between gap-3 flex-wrap">
          <div className="flex items-center gap-3">
            <Link
              href="/"
              className="text-[13px] text-blue-400 hover:text-blue-300"
            >
              ← 主页
            </Link>
            <h1 className="text-lg font-bold text-slate-100">
              🧠 AI 分析质量监控
            </h1>
            <span className="text-xs text-slate-500">
              D14 扩展 · 附录命中 / 冲突熔断
            </span>
          </div>
          <div className="flex items-center gap-2 text-xs">
            {SUPPORTED_COINS.map((c) => (
              <Link
                key={c}
                href={`/ai-quality/${c}`}
                className={`px-2.5 py-1 rounded border transition-colors ${
                  c === coin
                    ? "border-emerald-500/50 bg-emerald-500/10 text-emerald-300"
                    : "border-slate-700 text-slate-400 hover:border-slate-600"
                }`}
              >
                {c}
              </Link>
            ))}
            <label className="flex items-center gap-1 text-slate-500 ml-2">
              <input
                type="checkbox"
                checked={autoRefresh}
                onChange={(e) => setAutoRefresh(e.target.checked)}
                className="accent-emerald-500"
              />
              自动刷新
            </label>
            <button
              type="button"
              onClick={fetchOnce}
              className="px-2 py-1 rounded border border-slate-700 hover:border-slate-500 text-slate-300"
            >
              {loading ? "⟳" : "🔄"} 刷新
            </button>
          </div>
        </header>

        {/* ── 状态栏 ── */}
        <div className="flex items-center gap-4 text-[11px] text-slate-500 border-b border-slate-800 pb-2">
          {data ? (
            <>
              <span>
                样本窗口：
                <span className="text-slate-200 font-mono">
                  {data.stats.sample_size}/{data.stats.window}
                </span>
              </span>
              {data.stats.last_ts > 0 && (
                <span>
                  最新：
                  <span className="text-slate-200 font-mono">
                    {new Date(data.stats.last_ts * 1000).toLocaleString(
                      "zh-CN",
                      { hour12: false },
                    )}
                  </span>
                </span>
              )}
              {err && <span className="text-red-400">· {err}</span>}
            </>
          ) : (
            <span>{loading ? "加载中…" : err || "无数据"}</span>
          )}
        </div>

        {/* ── 趋势结论 ── */}
        {data && data.stats.sample_size > 0 && (
          <div className="rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3">
            <div className="text-[11px] text-emerald-400 mb-0.5">
              趋势提示
            </div>
            <div className="text-sm text-slate-200 leading-relaxed">
              {data.stats.trend_hint_cn}
            </div>
          </div>
        )}

        {/* ── 核心指标卡片 ── */}
        {data && data.stats.sample_size > 0 && (
          <section className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-3">
            <MetricCard
              title="AI 附录命中率"
              hint="AI JSON 成功覆写 matrix"
              value={formatPct(data.stats.ai_json_hit_rate)}
              tone={rateTone(data.stats.ai_json_hit_rate, [0.3, 0.6])}
            />
            <MetricCard
              title="AI 计划直出率"
              hint="trading_plans 来自 AI JSON"
              value={formatPct(data.stats.ai_plans_hit_rate)}
              tone={rateTone(data.stats.ai_plans_hit_rate, [0.3, 0.6])}
            />
            <MetricCard
              title="内部冲突率"
              hint="JSON/markdown 反向 → 熔断"
              value={formatPct(data.stats.internal_conflict_rate)}
              tone={rateToneReverse(
                data.stats.internal_conflict_rate,
                [0.05, 0.15],
              )}
            />
            <MetricCard
              title="Bias 一致率"
              hint="JSON bias vs markdown"
              value={formatPct(data.stats.bias_consistency_rate)}
              tone={rateTone(data.stats.bias_consistency_rate, [0.6, 0.85])}
            />
            <MetricCard
              title="与数学引擎一致"
              hint="math_agreement=agree"
              value={formatPct(data.stats.math_agreement_rate)}
              tone="neutral"
            />
            <MetricCard
              title="平均延迟"
              hint="AI 调用 wall-clock"
              value={`${(data.stats.avg_latency_ms / 1000).toFixed(1)} s`}
              tone="neutral"
            />
            <MetricCard
              title="平均推理 tokens"
              hint="thinking tokens（v4-flash 非思考模式下常为 0）"
              value={data.stats.avg_reasoning_tokens.toLocaleString()}
              tone="neutral"
            />
            <MetricCard
              title="平均覆写字段"
              hint="每次 AI 改写字段数"
              value={data.stats.avg_overlay_fields.toFixed(1)}
              tone="neutral"
            />
          </section>
        )}

        {/* ── 失败原因 top ── */}
        {data?.stats?.top_invalid_reasons?.length ? (
          <section className="rounded-md border border-slate-800 bg-slate-900/50 p-3">
            <div className="text-xs text-slate-400 mb-2">
              JSON 校验失败 top 3
            </div>
            <div className="flex flex-wrap gap-2 text-[11px]">
              {data.stats.top_invalid_reasons.map((r) => (
                <span
                  key={r.reason}
                  className="px-2 py-0.5 rounded border border-amber-500/30 bg-amber-500/5 text-amber-300"
                >
                  {r.reason} · {r.count}
                </span>
              ))}
            </div>
          </section>
        ) : null}

        {/* ── 最近记录明细 ── */}
        {data && data.recent.length > 0 && (
          <section className="rounded-md border border-slate-800 overflow-hidden">
            <div className="px-3 py-2 bg-slate-900 text-xs text-slate-400 flex items-center justify-between">
              <span>最近 {data.recent.length} 条分析明细（最新在前）</span>
              <span className="text-slate-600">
                点击 matrix/plans 标记可快速判断 AI 是否有效发力
              </span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead className="bg-slate-900/70 text-slate-500 uppercase text-[10px]">
                  <tr>
                    <th className="px-2 py-1.5 text-left">时间</th>
                    <th className="px-2 py-1.5 text-left">Matrix</th>
                    <th className="px-2 py-1.5 text-left">Plans</th>
                    <th className="px-2 py-1.5 text-left">Bias</th>
                    <th className="px-2 py-1.5 text-right">置信</th>
                    <th className="px-2 py-1.5 text-left">JSON↔MD</th>
                    <th className="px-2 py-1.5 text-left">数学引擎</th>
                    <th className="px-2 py-1.5 text-right">覆写</th>
                    <th className="px-2 py-1.5 text-right">延迟</th>
                    <th className="px-2 py-1.5 text-right">tokens</th>
                  </tr>
                </thead>
                <tbody>
                  {data.recent.map((r, i) => (
                    <RecordRow key={`${r.ts}-${i}`} r={r} />
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        )}

        {/* ── 说明面板 ── */}
        <section className="rounded-md border border-slate-800 bg-slate-900/30 p-4 text-[11px] text-slate-400 space-y-1.5">
          <div className="text-slate-300 font-semibold mb-1">
            指标判定口径
          </div>
          <div>
            · <span className="text-slate-300">Matrix 命中率</span> = AI JSON
            成功覆写 7 板块因子表的比率；目标 ≥ 60%，{"< 30%"} 需检查 prompt
            或模型输出。
          </div>
          <div>
            · <span className="text-slate-300">Plans 直出率</span>
            = trading_plans 来自 AI JSON 的比率；低表示 AI 经常未输出合法计划，走了 markdown / sniper 回退。
          </div>
          <div>
            · <span className="text-slate-300">内部冲突率</span>
            = JSON bias 与 markdown signal 反向的比率；触发熔断会强制 bias=neutral · conviction≤40。
          </div>
          <div>
            · <span className="text-slate-300">Bias 一致率</span>
            = 一致 ÷ (一致 + 冲突)，中性/缺失不计入分母。
          </div>
          <div>
            · <span className="text-slate-300">样本 &lt; 5</span>
            视为参考性低；窗口滚动最近 50 次分析。
          </div>
        </section>
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// Helpers
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

type Tone = "good" | "warn" | "bad" | "neutral";

function rateTone(rate: number, thresholds: [number, number]): Tone {
  const [bad, ok] = thresholds;
  if (rate >= ok) return "good";
  if (rate >= bad) return "warn";
  return "bad";
}

function rateToneReverse(rate: number, thresholds: [number, number]): Tone {
  const [ok, bad] = thresholds;
  if (rate <= ok) return "good";
  if (rate <= bad) return "warn";
  return "bad";
}

function formatPct(v: number): string {
  return `${(v * 100).toFixed(1)}%`;
}

function MetricCard({
  title,
  hint,
  value,
  tone,
}: {
  title: string;
  hint: string;
  value: string;
  tone: Tone;
}) {
  const toneClass =
    tone === "good"
      ? "border-emerald-500/40 bg-emerald-500/5 text-emerald-200"
      : tone === "warn"
        ? "border-amber-500/40 bg-amber-500/5 text-amber-200"
        : tone === "bad"
          ? "border-red-500/40 bg-red-500/5 text-red-200"
          : "border-slate-700 bg-slate-900/50 text-slate-200";
  return (
    <div className={`rounded-md border p-3 ${toneClass}`}>
      <div className="text-[10px] opacity-70">{hint}</div>
      <div className="text-xs font-semibold mt-0.5">{title}</div>
      <div className="text-2xl font-mono mt-1">{value}</div>
    </div>
  );
}

function MatrixBadge({ source }: { source: AIQualityRecord["matrix_source"] }) {
  const map: Record<AIQualityRecord["matrix_source"], [string, string]> = {
    ai_json: ["✅ AI", "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"],
    rule_fallback: ["规则", "bg-slate-700/40 text-slate-300 border-slate-600"],
    internal_conflict: [
      "⚠ 熔断",
      "bg-red-500/10 text-red-300 border-red-500/30",
    ],
  };
  const [label, cls] = map[source] ?? map.rule_fallback;
  return (
    <span className={`px-1.5 py-0.5 rounded border ${cls}`}>{label}</span>
  );
}

function PlansBadge({ source }: { source: AIQualityRecord["plans_source"] }) {
  const map: Record<AIQualityRecord["plans_source"], [string, string]> = {
    ai_json: ["✅ AI", "bg-emerald-500/10 text-emerald-300 border-emerald-500/30"],
    markdown: ["MD", "bg-slate-700/40 text-slate-300 border-slate-600"],
    sniper_fallback: [
      "Sniper",
      "bg-amber-500/10 text-amber-300 border-amber-500/30",
    ],
    wait_placeholder: [
      "观望",
      "bg-slate-800 text-slate-500 border-slate-700",
    ],
  };
  const [label, cls] = map[source] ?? map.markdown;
  return (
    <span className={`px-1.5 py-0.5 rounded border ${cls}`}>{label}</span>
  );
}

function BiasVsTextBadge({ v }: { v: AIQualityRecord["bias_vs_text"] }) {
  const map: Record<AIQualityRecord["bias_vs_text"], [string, string]> = {
    consistent: [
      "一致",
      "bg-emerald-500/10 text-emerald-300 border-emerald-500/30",
    ],
    conflict: ["冲突", "bg-red-500/10 text-red-300 border-red-500/30"],
    text_missing: [
      "缺 MD",
      "bg-slate-700/40 text-slate-400 border-slate-600",
    ],
    json_missing: [
      "缺 JSON",
      "bg-slate-700/40 text-slate-400 border-slate-600",
    ],
    unknown: [
      "未知",
      "bg-slate-800 text-slate-500 border-slate-700",
    ],
  };
  const [label, cls] = map[v] ?? map.unknown;
  return (
    <span className={`px-1.5 py-0.5 rounded border ${cls}`}>{label}</span>
  );
}

function MathBadge({ v }: { v: AIQualityRecord["math_agreement"] }) {
  const map: Record<AIQualityRecord["math_agreement"], [string, string]> = {
    agree: [
      "一致",
      "bg-emerald-500/10 text-emerald-300 border-emerald-500/30",
    ],
    caution: ["观望", "bg-amber-500/10 text-amber-300 border-amber-500/30"],
    disagree: ["冲突", "bg-red-500/10 text-red-300 border-red-500/30"],
    no_math_plan: [
      "无计划",
      "bg-slate-800 text-slate-500 border-slate-700",
    ],
  };
  const [label, cls] = map[v] ?? map.no_math_plan;
  return (
    <span className={`px-1.5 py-0.5 rounded border ${cls}`}>{label}</span>
  );
}

function RecordRow({ r }: { r: AIQualityRecord }) {
  const ts = r.ts
    ? new Date(r.ts * 1000).toLocaleString("zh-CN", { hour12: false })
    : "-";
  return (
    <tr className="border-t border-slate-800 hover:bg-slate-900/30">
      <td className="px-2 py-1.5 text-slate-400 font-mono whitespace-nowrap">
        {ts}
      </td>
      <td className="px-2 py-1.5">
        <MatrixBadge source={r.matrix_source} />
      </td>
      <td className="px-2 py-1.5">
        <PlansBadge source={r.plans_source} />
      </td>
      <td className="px-2 py-1.5 font-mono">{r.final_bias}</td>
      <td className="px-2 py-1.5 font-mono text-right">{r.final_conviction}</td>
      <td className="px-2 py-1.5">
        <BiasVsTextBadge v={r.bias_vs_text} />
      </td>
      <td className="px-2 py-1.5">
        <MathBadge v={r.math_agreement} />
      </td>
      <td className="px-2 py-1.5 text-right font-mono">{r.overlay_fields}</td>
      <td className="px-2 py-1.5 text-right font-mono">
        {(r.latency_ms / 1000).toFixed(1)}s
      </td>
      <td className="px-2 py-1.5 text-right font-mono">
        {r.reasoning_tokens.toLocaleString()}
      </td>
    </tr>
  );
}
