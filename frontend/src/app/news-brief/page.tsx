"use client";

/**
 * D09 · 滚动新闻简报 · 人工对证页面
 *
 * 用途：
 *   - 人工审计主 AI prompt 里实际注入了什么新闻（对齐 ai/snapshot.py 的 news_brief_text）
 *   - 区分三种状态：
 *       ok            → 绿色徽章，渲染全部正文
 *       circuit_break → 橙色徽章，标注"上游无事件·已熔断"，明确不是故障
 *       ai_failed     → 红色徽章，AI 调用失败走 fallback
 *       unexpected_empty → 红色徽章，内容为空但非熔断/fallback（异常）
 *       warming_up    → 灰色，首启动 ~60s 简报未生成
 *   - 展示 tracked_themes（AI 记忆锚点）、diff、容量控制、生成耗时
 *
 * 数据源：GET /api/news-brief/current
 * 刷新策略：默认 15s 轮询，与 news_agent_loop 的分钟级节奏错开。
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";

import { API_BASE } from "@/lib/constants";
import type { NewsBriefCurrentResponse, NewsBriefFull, NewsBriefUIStatus } from "@/lib/types";

const STATUS_META: Record<NewsBriefUIStatus, { label: string; badge: string; note: string }> = {
  ok: {
    label: "正常",
    badge: "bg-emerald-600/20 text-emerald-300 border-emerald-600/40",
    note: "AI 基于真实事件生成简报",
  },
  circuit_break: {
    label: "熔断·上游空",
    badge: "bg-amber-600/20 text-amber-300 border-amber-600/40",
    note: "过去 24h 无新闻事件入库，已熔断，避免 AI 编造（这是保护行为，不是故障）",
  },
  ai_failed: {
    label: "AI 调用失败",
    badge: "bg-rose-600/20 text-rose-300 border-rose-600/40",
    note: "AI 本轮调用失败，已沿用上一版本简报",
  },
  unexpected_empty: {
    label: "异常·内容为空",
    badge: "bg-rose-600/20 text-rose-300 border-rose-600/40",
    note: "既非熔断也非 fallback，但内容为空，请查日志",
  },
  warming_up: {
    label: "预热中",
    badge: "bg-slate-700/40 text-slate-400 border-slate-600/40",
    note: "简报尚未生成（通常启动 ~60s 内）",
  },
};

function fmtTs(ts: number | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtRelative(ts: number | null | undefined): string {
  if (!ts) return "—";
  const diff = Math.max(0, Math.floor(Date.now() / 1000) - ts);
  if (diff < 60) return `${diff}s 前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}m 前`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h 前`;
  return `${Math.floor(diff / 86400)}d 前`;
}

export default function NewsBriefPage() {
  const [data, setData] = useState<NewsBriefCurrentResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [lastFetchedAt, setLastFetchedAt] = useState<number>(0);

  const fetchBrief = useCallback(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/news-brief/current`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body: NewsBriefCurrentResponse = await res.json();
      setData(body);
      setError("");
      setLastFetchedAt(Math.floor(Date.now() / 1000));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    fetchBrief();
    if (!autoRefresh) return;
    const t = setInterval(fetchBrief, 15000);
    return () => clearInterval(t);
  }, [fetchBrief, autoRefresh]);

  const meta = useMemo(() => STATUS_META[(data?.status ?? "warming_up") as NewsBriefUIStatus], [data?.status]);
  const brief: NewsBriefFull | undefined = data?.brief;

  const showBody = data?.ready && data.status === "ok";
  const showStaleBody = data?.ready && data.status === "ai_failed"; // fallback 场景可展示上一版本

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300">
      {/* Header */}
      <header className="border-b border-slate-700 bg-slate-900/80 px-6 py-3 flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link href="/" className="text-blue-400 hover:text-blue-300 text-sm">
            ← 返回大屏
          </Link>
          <h1 className="text-lg font-bold text-white">📰 滚动新闻简报（AI 记忆锚）</h1>
        </div>
        <div className="flex items-center gap-3 text-xs">
          <label className="flex items-center gap-1 cursor-pointer">
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="rounded"
            />
            <span>自动刷新 (15s)</span>
          </label>
          <button
            onClick={fetchBrief}
            className="px-3 py-1 bg-slate-700 hover:bg-slate-600 rounded text-white"
          >
            刷新
          </button>
          {lastFetchedAt > 0 && (
            <span className="text-slate-500">取数于 {fmtRelative(lastFetchedAt)}</span>
          )}
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-6 space-y-4">
        {/* Status banner */}
        <section
          className={`rounded-lg border px-4 py-3 flex flex-col gap-2 ${meta.badge}`}
        >
          <div className="flex items-center gap-3 text-sm font-semibold">
            <span>状态：{meta.label}</span>
            {brief && (
              <span className="text-slate-400 font-normal">
                v{brief.version} · {fmtRelative(brief.updated_at)}
              </span>
            )}
          </div>
          <p className="text-xs opacity-90">{data?.reason || meta.note}</p>
        </section>

        {loading && (
          <div className="rounded-lg border border-slate-700 bg-slate-900 p-6 text-center text-slate-500">
            加载中…
          </div>
        )}

        {error && !loading && (
          <div className="rounded-lg border border-rose-600/40 bg-rose-950/20 p-4 text-rose-300 text-sm">
            取数失败：{error}
          </div>
        )}

        {/* 熔断/预热场景：不展示正文，只展示说明 */}
        {!loading && !error && data && !showBody && !showStaleBody && (
          <div className="rounded-lg border border-slate-700 bg-slate-900 p-6">
            <p className="text-sm text-slate-400">
              当前无可渲染的简报正文。该页面只展示被注入主 AI prompt 的内容，
              以便与后端 <code className="bg-slate-800 px-1 rounded">ai/snapshot.py</code>{" "}
              中的 <code className="bg-slate-800 px-1 rounded">news_brief_text</code> 对齐。
            </p>
            {brief && brief.prev_version_updated_at ? (
              <p className="text-xs text-slate-500 mt-2">
                上一版本 v{brief.version - 1} 生成于 {fmtTs(brief.prev_version_updated_at)}
              </p>
            ) : null}
          </div>
        )}

        {/* Metadata grid */}
        {brief && (
          <section className="rounded-lg border border-slate-700 bg-slate-900 p-4 grid grid-cols-2 md:grid-cols-4 gap-3 text-xs">
            <Stat label="版本" value={`v${brief.version}`} />
            <Stat label="生成于" value={fmtTs(brief.updated_at)} />
            <Stat label="触发" value={brief.update_trigger} />
            <Stat label="基于事件数" value={brief.based_on_events_count.toString()} />
            <Stat label="覆盖窗口" value={`${brief.coverage_hours.toFixed(0)}h`} />
            <Stat label="字符" value={`${brief.char_count} / ~${brief.token_estimate}t`} />
            <Stat label="模型" value={brief.model_used || "—"} />
            <Stat
              label="生成耗时"
              value={brief.generation_cost_ms > 0 ? `${brief.generation_cost_ms}ms` : "—"}
            />
          </section>
        )}

        {/* TLDR + Sections */}
        {brief && (showBody || showStaleBody) && (
          <>
            {brief.tldr_cn && (
              <section className="rounded-lg border border-blue-700/40 bg-blue-950/20 p-4">
                <h2 className="text-xs font-semibold text-blue-300 mb-2">一句话总结 · TL;DR</h2>
                <p className="text-sm text-slate-100 leading-relaxed">{brief.tldr_cn}</p>
              </section>
            )}

            <section className="grid grid-cols-1 md:grid-cols-2 gap-3">
              {brief.sections.map((sec) => (
                <div
                  key={sec.section_id}
                  className="rounded-lg border border-slate-700 bg-slate-900 p-4"
                >
                  <div className="flex items-center justify-between mb-2">
                    <h3 className="text-sm font-semibold text-white">
                      {sec.section_title_cn || sec.section_id}
                    </h3>
                    <span className="text-[10px] text-slate-500">
                      {sec.bullets.length}/{sec.max_bullets}
                    </span>
                  </div>
                  {sec.bullets.length === 0 ? (
                    <p className="text-xs text-slate-600 italic">本板块暂无要点</p>
                  ) : (
                    <ul className="space-y-1.5 text-sm text-slate-200">
                      {sec.bullets.map((b, i) => (
                        <li key={i} className="flex gap-2">
                          <span className="text-slate-600 shrink-0">·</span>
                          <span className="leading-relaxed">{b}</span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              ))}
            </section>

            {brief.tracked_themes.length > 0 && (
              <section className="rounded-lg border border-slate-700 bg-slate-900 p-4">
                <h2 className="text-xs font-semibold text-white mb-3">
                  🧠 AI 记忆锚点 · 跟踪叙事（{brief.tracked_themes.length}）
                </h2>
                <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
                  {brief.tracked_themes.map((t) => (
                    <div
                      key={t.theme_id}
                      className="rounded border border-slate-800 bg-slate-950 px-3 py-2 text-xs"
                    >
                      <div className="flex items-center justify-between">
                        <span className="text-slate-200 font-medium">
                          {t.theme_name_cn || t.theme_id}
                        </span>
                        <span className="text-slate-500">
                          rel {(t.relevance_score * 100).toFixed(0)}%
                        </span>
                      </div>
                      {t.current_stance_cn && (
                        <p className="text-slate-400 mt-1">{t.current_stance_cn}</p>
                      )}
                      <div className="flex items-center gap-3 mt-1 text-[10px] text-slate-600">
                        <span>flip 24h: {t.flip_flop_count_24h}</span>
                        <span>{fmtRelative(t.latest_update_ts)}</span>
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            )}

            {brief.diff_from_prev_version && (
              <section className="rounded-lg border border-slate-700 bg-slate-900 p-4">
                <h2 className="text-xs font-semibold text-white mb-2">与上一版本差异（diff）</h2>
                <pre className="text-xs text-slate-300 whitespace-pre-wrap leading-relaxed font-mono">
                  {brief.diff_from_prev_version}
                </pre>
              </section>
            )}
          </>
        )}
      </main>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] text-slate-500 uppercase tracking-wide">{label}</span>
      <span className="text-slate-200 mt-0.5 break-all">{value}</span>
    </div>
  );
}
