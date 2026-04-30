"use client";

import { useEffect, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { formatTime } from "@/lib/format";
import type { StrategicReport } from "@/lib/types";
import Link from "next/link";

const DECISION_CN: Record<string, string> = {
  WAIT: "等待",
  LONG_OBSERVATION: "看多观察",
  SHORT_OBSERVATION: "看空观察",
  LONG_PLAN: "多头计划",
  SHORT_PLAN: "空头计划",
  NO_TRADE: "禁止交易",
};

function biasColor(bias: string) {
  if (bias === "bullish") return "text-green-400";
  if (bias === "bearish") return "text-red-400";
  if (bias === "conflicted") return "text-amber-400";
  return "text-slate-300";
}

export default function AIAnalysis() {
  const aiPanelOpen = useMarketStore((s) => s.aiPanelOpen);
  const setAIPanelOpen = useMarketStore((s) => s.setAIPanelOpen);
  const report = useMarketStore((s) => s.strategicReport);
  const strategicLoading = useMarketStore((s) => s.strategicLoading);
  const strategicError = useMarketStore((s) => s.strategicError);
  const strategicHistory = useMarketStore((s) => s.strategicHistory);
  const coin = useMarketStore((s) => s.coin);
  const loadStrategicHistory = useMarketStore((s) => s.loadStrategicHistory);
  const loadStrategicReport = useMarketStore((s) => s.loadStrategicReport);

  useEffect(() => {
    if (aiPanelOpen) {
      void loadStrategicHistory(coin);
      void loadStrategicReport(coin);
    }
  }, [aiPanelOpen, coin, loadStrategicHistory, loadStrategicReport]);

  if (!aiPanelOpen) return null;

  const c = coin.toUpperCase();
  const current =
    report && report.coin?.toUpperCase() === c ? report : null;

  return (
    <div className="w-[380px] border-l border-slate-700 bg-slate-900 flex flex-col h-full shrink-0 overflow-hidden">
      <div className="flex items-center justify-between px-4 py-3 border-b border-slate-700">
        <h2 className="text-sm font-semibold text-white">🤖 Strategic 主 AI</h2>
        <button
          type="button"
          onClick={() => setAIPanelOpen(false)}
          className="text-slate-500 hover:text-white text-lg"
        >
          ✕
        </button>
      </div>

      <div className="flex-1 overflow-y-auto p-4">
        {strategicLoading && (
          <div className="flex flex-col items-center justify-center h-48 text-slate-400">
            <div className="animate-spin w-8 h-8 border-2 border-blue-500 border-t-transparent rounded-full mb-3" />
            <span className="text-sm">Strategic 分析进行中…</span>
          </div>
        )}

        {strategicError && !strategicLoading && (
          <div className="bg-red-950/30 border border-red-800/50 rounded-lg p-3 text-sm text-red-400">
            {strategicError}
          </div>
        )}

        {current && !strategicLoading && (
          <StrategicSummary r={current} showLink />
        )}

        {strategicHistory.length > 0 && !strategicLoading && (
          <div className="mt-6 border-t border-slate-700 pt-3">
            <div className="text-xs text-slate-500 mb-2">
              最近报告 ({strategicHistory.length})
            </div>
            {strategicHistory.map((h, i) => (
              <Link
                key={`${h.timestamp}-${i}`}
                href={`/ai/${h.coin}/${h.timestamp}`}
                target="_blank"
                rel="noopener noreferrer"
                className="flex flex-col gap-0.5 px-2 py-1.5 rounded text-xs mb-1 transition text-slate-500 hover:bg-slate-800 hover:text-slate-300"
              >
                <span className="flex items-center gap-2">
                  <span className="font-medium">{formatTime(h.timestamp)}</span>
                  <span className={biasColor(h.bias)}>{h.bias}</span>
                  <span className="text-slate-400">
                    {DECISION_CN[h.decision] ?? h.decision}
                  </span>
                  <span className="ml-auto text-slate-600">↗</span>
                </span>
              </Link>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function StrategicSummary({
  r,
  showLink,
}: {
  r: StrategicReport;
  showLink?: boolean;
}) {
  const pct = Math.round((r.confidence ?? 0) * 100);
  const rationale = (r.confidence_rationale || "").trim();
  const phase = (r.market_phase || "").trim();
  const plan = r.primary_plan;

  return (
    <div className="space-y-3 text-xs">
      <div className="flex items-center justify-between gap-2">
        <span className="text-lg font-bold text-white">
          {DECISION_CN[r.decision] ?? r.decision}
        </span>
        {showLink && (
          <Link
            href={`/ai/${r.coin}/${r.timestamp}`}
            target="_blank"
            rel="noopener noreferrer"
            className="text-blue-400 hover:text-blue-300 shrink-0"
          >
            完整页 ↗
          </Link>
        )}
      </div>
      <div className="text-slate-500">
        {formatTime(r.timestamp)}
        {r.stale_sec != null && r.stale_sec >= 0 && (
          <span> · 龄 {r.stale_sec}s</span>
        )}
        <span> · horizon {r.horizon}</span>
        <span className={` ml-2 ${biasColor(r.bias)}`}>{r.bias}</span>
        <span className="text-slate-400"> · 置信 {pct}%</span>
      </div>
      {phase && (
        <div className="bg-slate-800/50 rounded p-2 text-slate-300">{phase}</div>
      )}
      {rationale && (
        <div>
          <div className="text-slate-500 mb-1">置信说明</div>
          <div className="text-slate-300 leading-relaxed">{rationale}</div>
        </div>
      )}
      {plan && (
        <div className="rounded border border-slate-700/60 p-2 space-y-1">
          <div className="text-slate-500">主计划 · {plan.setup_type}</div>
          <div className="text-slate-300 font-mono">
            入场 {plan.entry_zone_low} – {plan.entry_zone_high}
          </div>
          <div className="text-slate-400">{plan.hard_invalidation}</div>
        </div>
      )}
      {(r.structure_analysis || "").trim() && (
        <div>
          <div className="text-slate-500 mb-1">结构</div>
          <p className="text-slate-400 leading-relaxed whitespace-pre-wrap">
            {r.structure_analysis.slice(0, 600)}
            {r.structure_analysis.length > 600 ? "…" : ""}
          </p>
        </div>
      )}
    </div>
  );
}
