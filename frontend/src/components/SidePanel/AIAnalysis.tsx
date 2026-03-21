"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatTime } from "@/lib/format";

export default function AIAnalysis() {
  const aiPanelOpen = useMarketStore((s) => s.aiPanelOpen);
  const setAIPanelOpen = useMarketStore((s) => s.setAIPanelOpen);
  const aiResult = useMarketStore((s) => s.aiResult);
  const aiLoading = useMarketStore((s) => s.aiLoading);
  const aiError = useMarketStore((s) => s.aiError);
  const aiHistory = useMarketStore((s) => s.aiHistory);

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
            <div className="text-xs text-slate-500">
              分析时间: {formatTime(aiResult.ts)} | 价格: ${aiResult.price_at_analysis.toLocaleString()}
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

            {aiResult.stop_loss_suggestion?.raw && (
              <Section title="🛡️ 止损安全区">
                <Markdown text={aiResult.stop_loss_suggestion.raw} />
              </Section>
            )}

            {aiResult.entry_zones.length > 0 && (
              <Section title="🎯 入场观察区">
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
              onClick={() => navigator.clipboard.writeText(aiResult.raw_text)}
              className="w-full py-2 text-xs text-slate-400 border border-slate-700 rounded hover:text-white hover:border-slate-500 transition"
            >
              📋 复制分析文本
            </button>
          </div>
        )}

        {aiHistory.length > 1 && !aiLoading && (
          <div className="mt-6 border-t border-slate-700 pt-3">
            <div className="text-xs text-slate-500 mb-2">历史分析 ({aiHistory.length})</div>
            {aiHistory.slice(1).map((h, i) => (
              <div key={i} className="text-xs text-slate-600 mb-1">
                {formatTime(h.ts)} - ${h.price_at_analysis.toLocaleString()}
              </div>
            ))}
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
