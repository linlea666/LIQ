"use client";

import { useState, useEffect } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { formatTime } from "@/lib/format";
import AITraderMatrixCard from "@/components/MainView/AITraderMatrixCard";

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
    return Promise.reject(new Error("execCommand copy failed"));
  } finally {
    document.body.removeChild(textarea);
  }
}

export default function AIAnalysis() {
  const aiPanelOpen = useMarketStore((s) => s.aiPanelOpen);
  const setAIPanelOpen = useMarketStore((s) => s.setAIPanelOpen);
  const aiResult = useMarketStore((s) => s.aiResult);
  const aiLoading = useMarketStore((s) => s.aiLoading);
  const aiError = useMarketStore((s) => s.aiError);
  const aiHistory = useMarketStore((s) => s.aiHistory);
  const coin = useMarketStore((s) => s.coin);
  const loadAIHistory = useMarketStore((s) => s.loadAIHistory);

  const [copyLabel, setCopyLabel] = useState("📋 复制分析文本");

  useEffect(() => {
    if (aiPanelOpen) loadAIHistory(coin);
  }, [aiPanelOpen, coin, loadAIHistory]);

  if (!aiPanelOpen) return null;

  return (
    <div className="w-[380px] border-l border-slate-700 bg-slate-900 flex flex-col h-full shrink-0 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
        <h2 className="text-sm font-semibold text-white">🤖 AI 市场分析</h2>
        <button onClick={() => setAIPanelOpen(false)} className="text-slate-500 hover:text-white text-lg">✕</button>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {aiLoading && (
          <div className="flex flex-col items-center justify-center h-48 text-slate-400">
            <div className="animate-spin w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full mb-3" />
            <span className="text-sm">正在分析市场数据...</span>
          </div>
        )}

        {aiError && !aiLoading && (
          <div className="bg-red-950/30 border border-red-800/50 rounded-lg p-3 text-sm text-red-400">
            {aiError}
          </div>
        )}

        {aiResult && !aiLoading && (
          <div className="space-y-4">
            {/* P1.4 · AI 交易员完整看盘表（7 板块 + 交易计划 + 关键位解读） */}
            <AITraderMatrixCard coin={coin} />

            <div className="flex items-center justify-between">
              <div className="text-xs text-slate-500">
                分析时间: {formatTime(aiResult.ts)} | 价格: ${aiResult.price_at_analysis.toLocaleString()}
              </div>
              <a
                href={`/ai/${aiResult.coin}/${aiResult.ts}`}
                target="_blank"
                rel="noopener noreferrer"
                className="text-xs text-blue-400 hover:text-blue-300 shrink-0 ml-2"
              >
                新页面查看 ↗
              </a>
            </div>

            {aiResult.market_overview && (
              <Section title="📊 市场格局总览">{aiResult.market_overview}</Section>
            )}

            {aiResult.key_levels.length > 0 && (
              <Section title="📍 关键价位">
                <div className="space-y-1">
                  {aiResult.key_levels.map((l, i) => (
                    <div key={i} className="flex justify-between text-xs">
                      <span className={l.type?.includes("支撑") || l.type?.includes("support") ? "text-green-400" : "text-red-400"}>
                        {l.type}
                      </span>
                      <span className="text-white font-medium">{l.price}</span>
                      <span className="text-slate-500 max-w-[40%] truncate">{l.reason}</span>
                    </div>
                  ))}
                </div>
              </Section>
            )}

            {/* New format: trading_plan with structured entries */}
            {aiResult.trading_plan ? (
              <Section title="📋 交易计划">
                {aiResult.trading_plan_entries && aiResult.trading_plan_entries.length > 0 && (
                  <div className="flex flex-col gap-2 mb-3">
                    {aiResult.trading_plan_entries.map((p, i) => {
                      const tierLabel = p.tier === "short" ? "短线" : p.tier === "mid" ? "中线" : "远线";
                      return (
                        <div
                          key={i}
                          className={`rounded border p-2 text-xs ${
                            p.direction === "long"
                              ? "border-green-700/40 bg-green-950/20"
                              : "border-red-700/40 bg-red-950/20"
                          }`}
                        >
                          <div className="flex items-center gap-1.5 font-semibold mb-1">
                            <span className="text-slate-400">[{tierLabel}]</span>
                            {p.direction === "long" ? "📈 多" : "📉 空"}
                            {p.source === "ai_inferred" && <span className="text-yellow-400">⚡AI</span>}
                          </div>
                          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-zinc-400">
                            {p.entry != null && <span>入场 <span className="text-white">${p.entry.toLocaleString()}</span></span>}
                            {p.stop_loss != null && <span>止损 <span className="text-white">${p.stop_loss.toLocaleString()}</span></span>}
                            {p.tp1 != null && <span>TP1 <span className="text-white">${p.tp1.toLocaleString()}</span></span>}
                            {p.rr != null && <span className="text-amber-400 font-semibold">R:R 1:{p.rr.toFixed(1)}</span>}
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </Section>
            ) : (
              <>
                {aiResult.stop_loss_suggestion?.raw && (
                  <Section title="🛡️ 止损安全区">
                    <Markdown text={aiResult.stop_loss_suggestion.raw} />
                  </Section>
                )}

                {aiResult.sniper_setup && (
                  <Section title="🎯 狙击挂单计划（高 R:R）">
                    {aiResult.sniper_plans && aiResult.sniper_plans.length > 0 && (
                      <div className="flex flex-col gap-2 mb-3">
                        {aiResult.sniper_plans.map((p, i) => (
                          <div
                            key={i}
                            className={`rounded border p-2 text-xs ${
                              p.direction === "long"
                                ? "border-green-700/40 bg-green-950/20"
                                : "border-red-700/40 bg-red-950/20"
                            }`}
                          >
                            <span className="font-semibold">
                              {p.direction === "long" ? "📈 多" : "📉 空"}
                            </span>
                            {p.entry != null && <span className="ml-2">入场 ${p.entry.toLocaleString()}</span>}
                            {p.stop_loss != null && <span className="ml-2 text-zinc-400">止损 ${p.stop_loss.toLocaleString()}</span>}
                            {p.rr != null && <span className="ml-2 text-amber-400">R:R 1:{p.rr.toFixed(1)}</span>}
                          </div>
                        ))}
                      </div>
                    )}
                    <Markdown text={aiResult.sniper_setup} />
                  </Section>
                )}

                {aiResult.ladder_plan_text && (
                  <Section title="🪜 阶梯埋伏计划（远距多层网）">
                    <Markdown text={aiResult.ladder_plan_text} />
                  </Section>
                )}
              </>
            )}

            {aiResult.data_quality_feedback && (
              <Section title="🔍 数据质量自检">
                <Markdown text={aiResult.data_quality_feedback} />
              </Section>
            )}

            {aiResult.entry_zones.length > 0 && (
              <Section title="📌 入场观察区">
                {aiResult.entry_zones.map((z, i) => (
                  <div key={i} className="mb-2">
                    <div className="text-xs font-medium text-blue-400">{z.raw}</div>
                    {z.details?.map((d, j) => (
                      <div key={j} className="text-xs text-slate-400 ml-2">• {d}</div>
                    ))}
                  </div>
                ))}
              </Section>
            )}

            {aiResult.risk_warnings.length > 0 && (
              <Section title="⚠️ 风险提示">
                {aiResult.risk_warnings.map((w, i) => (
                  <div key={i} className="text-xs text-yellow-400/80">• {w}</div>
                ))}
              </Section>
            )}

            {aiResult.scenario_analysis.length > 0 && (
              <Section title="💡 场景推演">
                {aiResult.scenario_analysis.map((s, i) => (
                  <div key={i} className="mb-2">
                    <div className="text-xs font-medium text-slate-300">{s.label}:</div>
                    <div className="text-xs text-slate-400">{s.description}</div>
                  </div>
                ))}
              </Section>
            )}

            <button
              onClick={() => {
                copyToClipboard(aiResult.raw_text)
                  .then(() => {
                    setCopyLabel("✅ 已复制");
                    setTimeout(() => setCopyLabel("📋 复制分析文本"), 1500);
                  })
                  .catch(() => {
                    setCopyLabel("❌ 复制失败");
                    setTimeout(() => setCopyLabel("📋 复制分析文本"), 1500);
                  });
              }}
              className="w-full py-2 text-xs text-slate-400 border border-slate-700 rounded hover:text-white hover:border-slate-500 transition"
            >
              {copyLabel}
            </button>
          </div>
        )}

        {aiHistory.length > 0 && !aiLoading && (
          <div className="mt-6 border-t border-slate-700 pt-3">
            <div className="text-xs text-slate-500 mb-2">历史分析 ({aiHistory.length})</div>
            {aiHistory.map((h, i) => {
              const sig = h.signal_summary;
              const dirLabel =
                sig?.direction === "bullish" ? "看多" :
                sig?.direction === "bearish" ? "看空" :
                sig?.direction === "neutral" ? "震荡" : "";
              const confLabel =
                sig?.confidence === "high" ? "高" :
                sig?.confidence === "medium" ? "中" :
                sig?.confidence === "low" ? "低" : "";
              return (
                <a
                  key={h.ts + "-" + i}
                  href={`/ai/${h.coin}/${h.ts}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex items-center gap-1.5 px-2 py-1.5 rounded text-xs mb-1 transition text-slate-500 hover:bg-slate-800 hover:text-slate-300"
                >
                  <span className="font-medium">{formatTime(h.ts)}</span>
                  <span>${h.price_at_analysis.toLocaleString()}</span>
                  {dirLabel && (
                    <span className={`${
                      sig?.direction === "bullish" ? "text-green-400" :
                      sig?.direction === "bearish" ? "text-red-400" :
                      "text-yellow-400"
                    }`}>
                      {dirLabel}{confLabel ? `(${confLabel})` : ""}
                    </span>
                  )}
                  <span className="ml-auto text-slate-600">↗</span>
                </a>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="text-xs font-semibold text-slate-300 mb-1.5">{title}</h3>
      <div className="bg-slate-800/50 rounded-lg p-3 text-xs text-slate-400 leading-relaxed">
        {children}
      </div>
    </div>
  );
}

function Markdown({ text }: { text: string }) {
  return (
    <div className="whitespace-pre-wrap">
      {text.split("\n").map((line, i) => (
        <div key={i}>{line}</div>
      ))}
    </div>
  );
}
