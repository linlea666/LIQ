"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API_BASE } from "@/lib/constants";
import type { AIAnalysisResult } from "@/lib/types";

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

  const [data, setData] = useState<AIAnalysisResult | null>(null);
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
          <Card title="📋 交易计划（三档结构）">
            {data.trading_plan_entries && data.trading_plan_entries.length > 0 && (
              <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3 mb-4">
                {data.trading_plan_entries.map((p, i) => {
                  const tierLabel = p.tier === "short" ? "短线" : p.tier === "mid" ? "中线" : "远线";
                  const tierColor = p.tier === "short" ? "text-blue-400" : p.tier === "mid" ? "text-purple-400" : "text-orange-400";
                  return (
                    <div
                      key={i}
                      className={`rounded-lg border p-3 text-sm ${
                        p.direction === "long"
                          ? "border-green-700/40 bg-green-950/20"
                          : "border-red-700/40 bg-red-950/20"
                      }`}
                    >
                      <div className="flex items-center gap-2 font-semibold mb-1">
                        <span className={tierColor}>[{tierLabel}]</span>
                        {p.direction === "long" ? "📈 做多" : "📉 做空"}
                        {p.source === "ai_inferred" && <span className="text-yellow-400 text-xs">⚡AI</span>}
                      </div>
                      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-xs text-zinc-300">
                        {p.entry != null && <div>入场: <span className="text-white">${p.entry.toLocaleString()}</span></div>}
                        {p.stop_loss != null && <div>止损: <span className="text-white">${p.stop_loss.toLocaleString()}</span></div>}
                        {p.tp1 != null && <div>止盈1: <span className="text-white">${p.tp1.toLocaleString()}</span></div>}
                        {p.tp2 != null && <div>止盈2: <span className="text-white">${p.tp2.toLocaleString()}</span></div>}
                        {p.rr != null && <div className="col-span-2">R:R = <span className="text-amber-400 font-semibold">1:{p.rr.toFixed(1)}</span></div>}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
            <RichMarkdown text={data.trading_plan} />
          </Card>
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
      <div key={`table-${elements.length}`} className="overflow-x-auto my-3">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b border-slate-600">
              {header.map((cell, ci) => (
                <th key={ci} className="text-left py-1.5 px-2 text-slate-300 text-xs font-semibold">
                  {cell.replace(/\*\*/g, "")}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={ri} className="border-b border-slate-800/50">
                {row.map((cell, ci) => (
                  <td key={ci} className="py-1.5 px-2 text-xs">
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
