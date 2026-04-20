"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API_BASE } from "@/lib/constants";
import type { AIDetailResponse } from "@/lib/types";
import AITraderMatrixCard from "@/components/MainView/AITraderMatrixCard";
import FinalDecisionCard from "@/components/MainView/FinalDecisionCard";

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand("copy");
    return Promise.resolve();
  } catch {
    return Promise.reject(new Error("copy failed"));
  } finally {
    document.body.removeChild(textarea);
  }
}

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

export default function AIDetailPage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";
  const ts = Number(params.ts);

  const [data, setData] = useState<AIDetailResponse | null>(null);
  const [error, setError] = useState("");
  const [copyLabel, setCopyLabel] = useState("📋 复制全文");

  useEffect(() => {
    if (!ts) return;
    fetch(`${API_BASE}/api/ai/detail/${coin}/${ts}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e) => setError(`加载失败: ${e.message}`));
  }, [coin, ts]);

  if (error) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-400 text-lg mb-4">{error}</div>
          <a href="/" className="text-blue-400 hover:text-blue-300 text-sm">← 返回大屏</a>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="animate-spin w-10 h-10 border-2 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  const sig = data.signal_summary;
  const dirLabel =
    sig?.direction === "bullish" ? "看多" :
    sig?.direction === "bearish" ? "看空" :
    sig?.direction === "neutral" ? "震荡" : "未知";
  const dirColor =
    sig?.direction === "bullish" ? "bg-green-600" :
    sig?.direction === "bearish" ? "bg-red-600" :
    "bg-yellow-600";
  const confLabel =
    sig?.confidence === "high" ? "高" :
    sig?.confidence === "medium" ? "中" :
    sig?.confidence === "low" ? "低" : "";

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300 ai-detail-page">
      {/* Header */}
      <header className="border-b border-slate-700 bg-slate-900/80 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <a href="/" className="text-blue-400 hover:text-blue-300 text-sm shrink-0">← 返回大屏</a>
            <div>
              <h1 className="text-lg font-bold text-white">🤖 {coin} AI 市场分析</h1>
              <div className="text-xs text-slate-500 mt-0.5">
                {formatFullTime(data.ts)} | 分析时价格: ${data.price_at_analysis.toLocaleString()}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className={`${dirColor} text-white text-sm font-bold px-3 py-1 rounded-full`}>
              {dirLabel}{confLabel ? ` (${confLabel})` : ""}
            </span>
            <button
              onClick={() => {
                copyToClipboard(data.raw_text)
                  .then(() => { setCopyLabel("✅ 已复制"); setTimeout(() => setCopyLabel("📋 复制全文"), 1500); })
                  .catch(() => { setCopyLabel("❌ 失败"); setTimeout(() => setCopyLabel("📋 复制全文"), 1500); });
              }}
              className="px-3 py-1 text-xs border border-slate-600 rounded hover:border-slate-400 hover:text-white transition"
            >
              {copyLabel}
            </button>
          </div>
        </div>
      </header>

      {/* Content */}
      <main className="max-w-4xl mx-auto px-6 py-6 space-y-6">
        {/* ━━━ 新版双引擎输出（P1.3+）━━━ */}
        {(data.final_decision || data.ai_trader_report) && (
          <section className="space-y-4">
            <div className="flex items-center justify-between">
              <h2 className="text-sm font-semibold text-slate-200">
                🎯 双引擎决策（L7.5 融合层）
              </h2>
              <span
                className="text-[10px] px-2 py-0.5 rounded bg-slate-800 text-slate-500 border border-slate-700"
                title={
                  data._extras_source === "live"
                    ? "来源：当前运行内存（最新一次 AI 分析）"
                    : data._extras_source === "archive"
                    ? "来源：P2.4 归档（就近 ±10min 匹配）"
                    : "来源：未命中"
                }
              >
                {data._extras_source === "live"
                  ? "live"
                  : data._extras_source === "archive"
                  ? "archive"
                  : "不可用"}
              </span>
            </div>
            {data.final_decision && (
              <FinalDecisionCard
                coin={coin}
                externalDecision={data.final_decision}
              />
            )}
            {data.ai_trader_report && (
              <AITraderMatrixCard
                coin={coin}
                externalReport={data.ai_trader_report}
              />
            )}
          </section>
        )}

        {/* ━━━ 新闻 · 地缘 · 叙事（prompt 本轮注入内容）━━━ */}
        {data.news_brief && (
          <NewsBriefCard brief={data.news_brief} />
        )}

        {/* Signal Summary Card */}
        {sig?.reason && (
          <div className={`rounded-xl p-5 border ${
            sig.direction === "bullish" ? "bg-green-950/30 border-green-800/50" :
            sig.direction === "bearish" ? "bg-red-950/30 border-red-800/50" :
            "bg-yellow-950/30 border-yellow-800/50"
          }`}>
            <div className="text-xs text-slate-500 mb-1">白话总结</div>
            <div className="text-base text-white font-medium leading-relaxed">
              {sig.reason}
            </div>
          </div>
        )}

        {/* Market Overview */}
        {data.market_overview && (
          <Card title="📊 市场格局总览">
            <RichMarkdown text={data.market_overview} />
          </Card>
        )}

        {/* Key Levels */}
        {data.key_levels.length > 0 && (
          <Card title="📍 关键价位图谱">
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead>
                  <tr className="border-b border-slate-700 text-slate-500 text-xs">
                    <th className="text-left py-2 pr-4">类型</th>
                    <th className="text-right py-2 pr-4">价位</th>
                    <th className="text-left py-2">依据</th>
                  </tr>
                </thead>
                <tbody>
                  {data.key_levels.map((l, i) => {
                    const isSupport = l.type?.includes("支撑") || l.type?.includes("support");
                    return (
                      <tr key={i} className="border-b border-slate-800/50">
                        <td className={`py-2 pr-4 font-medium ${isSupport ? "text-green-400" : "text-red-400"}`}>
                          {l.type}
                        </td>
                        <td className="py-2 pr-4 text-right text-white font-mono">{l.price}</td>
                        <td className="py-2 text-slate-400 text-xs">{l.reason}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </Card>
        )}

        {/* Trading Plan (new merged format) */}
        {data.trading_plan && (
          <>
            {data.trading_plan_entries && data.trading_plan_entries.length > 0 && (() => {
              const tiers = [
                { key: "short", label: "短线档", color: "blue", border: "border-blue-700/30" },
                { key: "mid", label: "中线档", color: "purple", border: "border-purple-700/30" },
                { key: "long", label: "远线档", color: "orange", border: "border-orange-700/30" },
              ];
              return tiers.map(({ key, label, color, border }) => {
                const items = data.trading_plan_entries!.filter(p => p.tier === key);
                if (items.length === 0) return null;
                return (
                  <Card key={key} title={`📋 ${label}`}>
                    <div className="space-y-2.5">
                      {items.map((p, i) => (
                        <div
                          key={i}
                          className={`rounded-lg border p-3 ${
                            p.direction === "long"
                              ? "border-green-700/40 bg-green-950/20"
                              : "border-red-700/40 bg-red-950/20"
                          }`}
                        >
                          <div className="flex items-center gap-2 font-semibold text-sm mb-2">
                            {p.direction === "long" ? <span className="text-green-400">📈 做多</span> : <span className="text-red-400">📉 做空</span>}
                            {p.source === "ai_inferred" && <span className="text-yellow-400 text-xs border border-yellow-700/50 rounded px-1.5 py-0.5">⚡AI推断</span>}
                          </div>
                          <div className="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-1 text-xs">
                            {p.entry != null && <div className="text-zinc-400">入场 <span className="text-white font-medium">${p.entry.toLocaleString()}</span></div>}
                            {p.stop_loss != null && <div className="text-zinc-400">止损 <span className="text-white font-medium">${p.stop_loss.toLocaleString()}</span></div>}
                            {p.tp1 != null && <div className="text-zinc-400">TP1 <span className="text-white font-medium">${p.tp1.toLocaleString()}</span></div>}
                            {p.tp2 != null && <div className="text-zinc-400">TP2 <span className="text-white font-medium">${p.tp2.toLocaleString()}</span></div>}
                          </div>
                          {p.rr != null && (
                            <div className="mt-1.5 text-xs">R:R = <span className="text-amber-400 font-bold text-sm">1:{p.rr.toFixed(1)}</span></div>
                          )}
                        </div>
                      ))}
                    </div>
                  </Card>
                );
              });
            })()}

            <Card title="📝 交易计划详细分析">
              <RichMarkdown text={data.trading_plan} />
            </Card>
          </>
        )}

        {/* Legacy: Stop Loss (old reports only) */}
        {!data.trading_plan && data.stop_loss_suggestion?.raw && (
          <Card title="🛡️ 止损安全区建议">
            <RichMarkdown text={data.stop_loss_suggestion.raw} />
          </Card>
        )}

        {/* Legacy: Sniper (old reports only) */}
        {!data.trading_plan && data.sniper_setup && (
          <Card title="🎯 狙击挂单计划（高 R:R）">
            {data.sniper_plans && data.sniper_plans.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mb-4">
                {data.sniper_plans.map((p, i) => (
                  <div
                    key={i}
                    className={`rounded-lg border p-3 text-sm ${
                      p.direction === "long"
                        ? "border-green-700/40 bg-green-950/20"
                        : "border-red-700/40 bg-red-950/20"
                    }`}
                  >
                    <div className="font-semibold mb-1">
                      {p.direction === "long" ? "📈 多单埋伏" : "📉 空单埋伏"}
                    </div>
                    <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-zinc-300">
                      {p.entry != null && <div>入场: <span className="text-white">${p.entry.toLocaleString()}</span></div>}
                      {p.stop_loss != null && <div>止损: <span className="text-white">${p.stop_loss.toLocaleString()}</span></div>}
                      {p.tp1 != null && <div>止盈1: <span className="text-white">${p.tp1.toLocaleString()}</span></div>}
                      {p.tp2 != null && <div>止盈2: <span className="text-white">${p.tp2.toLocaleString()}</span></div>}
                      {p.rr != null && <div className="col-span-2">R:R = <span className="text-amber-400 font-semibold">1:{p.rr.toFixed(1)}</span></div>}
                    </div>
                    {p.invalidation && (
                      <div className="text-xs text-zinc-500 mt-1">⛔ 失效: {p.invalidation}</div>
                    )}
                  </div>
                ))}
              </div>
            )}
            <RichMarkdown text={data.sniper_setup} />
          </Card>
        )}

        {/* Legacy: Ladder (old reports only) */}
        {!data.trading_plan && data.ladder_plan_text && (
          <Card title="🪜 阶梯埋伏计划（远距多层网）">
            <RichMarkdown text={data.ladder_plan_text} />
          </Card>
        )}

        {/* Entry Zones */}
        {data.entry_zones.length > 0 && (
          <Card title="📌 入场观察区">
            {data.entry_zones.map((z, i) => (
              <div key={i} className="mb-3 last:mb-0">
                <div className="text-sm font-medium text-blue-400 mb-1">{z.raw}</div>
                {z.details?.map((d, j) => (
                  <div key={j} className="text-sm text-slate-400 ml-3">• {d}</div>
                ))}
              </div>
            ))}
          </Card>
        )}

        {/* Risk Warnings */}
        {data.risk_warnings.length > 0 && (
          <Card title="⚠️ 风险提示">
            <div className="space-y-2">
              {data.risk_warnings.map((w, i) => (
                <div key={i} className="text-sm text-yellow-400/90 leading-relaxed">• {w}</div>
              ))}
            </div>
          </Card>
        )}

        {/* Scenarios */}
        {data.scenario_analysis.length > 0 && (
          <Card title="💡 场景推演">
            {data.scenario_analysis.map((s, i) => (
              <div key={i} className="mb-3 last:mb-0">
                <div className="text-sm font-semibold text-slate-200 mb-1">{s.label}</div>
                <div className="text-sm text-slate-400 leading-relaxed">{s.description}</div>
              </div>
            ))}
          </Card>
        )}

        {/* Data Quality Feedback */}
        {data.data_quality_feedback && (
          <Card title="🔍 数据质量与自检">
            <RichMarkdown text={data.data_quality_feedback} />
          </Card>
        )}

        {/* Footer */}
        <div className="text-center text-xs text-slate-600 py-6 border-t border-slate-800">
          LIQ 防猎杀数据大屏 · AI 分析报告 · {formatFullTime(data.ts)}
        </div>
      </main>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl overflow-hidden">
      <div className="px-5 py-3 border-b border-slate-700/50 bg-slate-800/30">
        <h2 className="text-sm font-semibold text-white">{title}</h2>
      </div>
      <div className="px-5 py-4 text-sm text-slate-400 leading-relaxed">
        {children}
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 本轮 prompt 注入的新闻简报 + 地缘 + 活跃叙事
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

interface NewsBriefPayload {
  text: string;
  version: number;
  trigger: string;
  updated_at: number;
  geo_overview: Record<string, unknown> | null;
  active_narratives: Array<Record<string, unknown>>;
}

/**
 * 后端 `NewsBriefSection` 序列化结构（backend/models/news_brief.py）。
 * 注意字段命名：后端是 `section_title_cn` / `bullets`，与前端其它 AI Matrix
 * 的 `title_cn` / `bullets_cn` 命名不同，历史原因保留以避免后端模型变更。
 * 这里同时保留旧字段的兼容读取，容忍后端字段演进。
 */
interface BriefSection {
  section_id?: "macro" | "regulatory" | "onchain" | "risk" | string;
  section_title_cn?: string;
  bullets?: string[];
  /** 兼容：如后端将来改名为 title_cn/bullets_cn 也能渲染 */
  title_cn?: string;
  bullets_cn?: string[];
}

const SECTION_ID_FALLBACK_CN: Record<string, string> = {
  macro: "宏观",
  regulatory: "监管",
  onchain: "链上",
  risk: "风险",
};

interface BriefJson {
  version?: number;
  tldr_cn?: string;
  sections?: BriefSection[];
  tracked_themes?: Array<Record<string, unknown>>;
  diff_from_prev_version?: string;
  update_trigger?: string;
}

function parseBriefJson(text: string): BriefJson | null {
  if (!text) return null;
  try {
    return JSON.parse(text) as BriefJson;
  } catch {
    return null;
  }
}

function NewsBriefCard({ brief }: { brief: NewsBriefPayload }) {
  const parsed = parseBriefJson(brief.text);
  const geo = brief.geo_overview || {};
  const narratives = brief.active_narratives || [];

  const geoLevel = Number(geo["overall_level"] ?? 0);
  const geoLabel = String(geo["overall_label"] ?? "");
  const geoEmoji = String(geo["overall_emoji"] ?? "🟢");
  const geoSummary = String(geo["overall_summary_cn"] ?? "");

  const geoColor =
    geoLevel >= 4 ? "text-red-300 bg-red-500/10 border-red-500/40" :
    geoLevel >= 3 ? "text-orange-300 bg-orange-500/10 border-orange-500/40" :
    geoLevel >= 1 ? "text-yellow-300 bg-yellow-500/10 border-yellow-500/40" :
    "text-green-300 bg-green-500/10 border-green-500/40";

  return (
    <Card title="📰 新闻 · 地缘 · 叙事（本轮 prompt 注入）">
      {/* 顶部：来源元信息 */}
      <div className="flex items-center gap-3 flex-wrap text-xs mb-3">
        <span className="text-slate-500">
          简报版本 <span className="text-white font-mono">v{brief.version || 0}</span>
        </span>
        {brief.trigger && (
          <span className="text-slate-500">
            触发 <span className="text-slate-300">{brief.trigger}</span>
          </span>
        )}
        {brief.updated_at > 0 && (
          <span className="text-slate-500">
            更新于 <span className="text-slate-300">{formatFullTime(brief.updated_at)}</span>
          </span>
        )}
      </div>

      {/* 地缘风险条 */}
      {(geoLevel > 0 || geoSummary) && (
        <div className={`border rounded-lg px-3 py-2 mb-3 ${geoColor}`}>
          <div className="text-xs font-semibold mb-0.5 flex items-center gap-2">
            <span>{geoEmoji}</span>
            <span>地缘风险 · {geoLabel || "—"}（等级 {geoLevel}/5）</span>
          </div>
          {geoSummary && <div className="text-xs opacity-90">{geoSummary}</div>}
        </div>
      )}

      {/* TL;DR */}
      {parsed?.tldr_cn && (
        <div className="bg-slate-800/40 border border-slate-700/50 rounded-lg px-3 py-2 mb-3">
          <div className="text-[11px] text-slate-500 mb-1">TL;DR</div>
          <div className="text-sm text-slate-200 leading-relaxed">{parsed.tldr_cn}</div>
        </div>
      )}

      {/* Sections（兼容后端 section_title_cn/bullets 与前端旧 title_cn/bullets_cn） */}
      {parsed?.sections && parsed.sections.length > 0 && (
        <div className="space-y-2 mb-3">
          {parsed.sections.map((sec, i) => {
            const title =
              sec.section_title_cn ||
              sec.title_cn ||
              (sec.section_id
                ? SECTION_ID_FALLBACK_CN[sec.section_id] ?? sec.section_id
                : "") ||
              `板块 ${i + 1}`;
            const bullets = sec.bullets ?? sec.bullets_cn ?? [];
            return (
              <div key={i} className="border-l-2 border-blue-500/40 pl-3">
                <div className="text-xs font-semibold text-slate-200 mb-1">
                  {title}
                </div>
                {bullets.map((b, j) => (
                  <div
                    key={j}
                    className="text-xs text-slate-400 leading-relaxed mb-0.5"
                  >
                    • {b}
                  </div>
                ))}
              </div>
            );
          })}
        </div>
      )}

      {/* 与上一版差异 */}
      {parsed?.diff_from_prev_version && (
        <div className="bg-blue-950/20 border border-blue-800/40 rounded px-3 py-2 mb-3 text-xs text-blue-200/90">
          <span className="text-blue-400 font-semibold">本版变化：</span>
          {parsed.diff_from_prev_version}
        </div>
      )}

      {/* 活跃叙事 */}
      {narratives.length > 0 && (
        <div>
          <div className="text-xs text-slate-500 mb-1.5">活跃叙事主题（{narratives.length}）</div>
          <div className="flex flex-wrap gap-1.5">
            {narratives.slice(0, 12).map((n, i) => {
              const name = String(n["name_cn"] || n["theme_id"] || "—");
              const intensity = Number(n["intensity"] ?? 0);
              const dir = String(n["direction"] ?? "neutral");
              const flip = Number(n["flip_flop_24h"] ?? 0);
              const dirColor =
                dir === "bullish" ? "border-green-700/50 text-green-300" :
                dir === "bearish" ? "border-red-700/50 text-red-300" :
                "border-slate-700 text-slate-300";
              return (
                <span
                  key={i}
                  className={`text-[11px] px-2 py-0.5 rounded-full border ${dirColor}`}
                  title={`强度 ${intensity.toFixed(2)} · 方向 ${dir}${flip > 0 ? ` · 24h 翻转 ${flip} 次` : ""}`}
                >
                  {name}
                  {flip >= 2 && <span className="ml-1 text-amber-400">⚠flip×{flip}</span>}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {/* 原始简报无法解析时的兜底 */}
      {!parsed && brief.text && (
        <details className="mt-3 text-xs text-slate-500">
          <summary className="cursor-pointer hover:text-slate-300">查看原始 brief JSON</summary>
          <pre className="mt-2 p-2 bg-slate-950 border border-slate-800 rounded overflow-x-auto text-[10px]">
            {brief.text.slice(0, 2000)}
          </pre>
        </details>
      )}
    </Card>
  );
}

function RichMarkdown({ text }: { text: string }) {
  const lines = text.split("\n");
  const elements: React.ReactNode[] = [];
  let tableRows: string[][] = [];
  let inTable = false;

  const flushTable = () => {
    if (tableRows.length === 0) return;
    const header = tableRows[0];
    const body = tableRows.slice(1);
    elements.push(
      <div key={`table-${elements.length}`} className="overflow-x-auto my-3 rounded-lg border border-slate-700/50">
        <table className="min-w-full text-sm border-collapse">
          <thead>
            <tr className="bg-slate-800/60">
              {header.map((cell, ci) => (
                <th key={ci} className="text-left py-2 px-3 text-slate-300 text-xs font-semibold whitespace-nowrap">
                  {cell.replace(/\*\*/g, "")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={ri} className={`border-t border-slate-800/40 ${ri % 2 === 0 ? "bg-slate-900/30" : ""}`}>
                {row.map((cell, ci) => (
                  <td key={ci} className="py-2 px-3 text-xs leading-relaxed align-top">
                    <InlineFormat text={cell} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
    tableRows = [];
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    const trimmed = line.trim();

    if (trimmed.startsWith("|") && trimmed.endsWith("|")) {
      if (trimmed.replace(/[\s|:-]/g, "").length === 0) {
        continue;
      }
      inTable = true;
      const cells = trimmed.slice(1, -1).split("|").map((c) => c.trim());
      tableRows.push(cells);
      continue;
    }

    if (inTable) {
      flushTable();
      inTable = false;
    }

    if (!trimmed) {
      elements.push(<div key={i} className="h-2" />);
    } else if (trimmed.startsWith("### ")) {
      elements.push(
        <h4 key={i} className="text-sm font-semibold text-slate-200 mt-4 mb-1">
          {trimmed.slice(4)}
        </h4>
      );
    } else if (trimmed.startsWith("> ")) {
      elements.push(
        <div key={i} className="border-l-2 border-blue-500 pl-3 py-1 my-2 text-slate-300 bg-blue-950/20 rounded-r">
          <InlineFormat text={trimmed.slice(2)} />
        </div>
      );
    } else if (/^\d+\.\s+/.test(trimmed)) {
      elements.push(
        <div key={i} className="ml-2 mb-1">
          <InlineFormat text={trimmed} />
        </div>
      );
    } else if (trimmed.startsWith("- ") || trimmed.startsWith("• ")) {
      elements.push(
        <div key={i} className="ml-3 mb-1">
          <InlineFormat text={trimmed} />
        </div>
      );
    } else {
      elements.push(
        <div key={i} className="mb-1">
          <InlineFormat text={trimmed} />
        </div>
      );
    }
  }

  if (inTable) flushTable();

  return <>{elements}</>;
}

function InlineFormat({ text }: { text: string }) {
  const parts = text.split(/(\*\*[^*]+\*\*)/g);
  return (
    <>
      {parts.map((part, i) => {
        if (part.startsWith("**") && part.endsWith("**")) {
          return <strong key={i} className="text-white font-semibold">{part.slice(2, -2)}</strong>;
        }
        return <span key={i}>{part}</span>;
      })}
    </>
  );
}
