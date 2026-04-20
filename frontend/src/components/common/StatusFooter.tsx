"use client";

import { useMarketStore } from "@/stores/marketStore";
import DecisionTrackerBadge from "./DecisionTrackerBadge";

const STATUS_ICON: Record<string, string> = {
  connected: "🟢",
  degraded: "🟡",
  disconnected: "🔴",
};

export default function StatusFooter() {
  const sourceHealth = useMarketStore((s) => s.sourceHealth);

  return (
    <div className="h-7 bg-slate-900 border-t border-slate-700 flex items-center px-4 text-xs text-slate-500 gap-6">
      {sourceHealth.length > 0 ? (
        sourceHealth.map((s: Record<string, any>) => (
          <span key={s.name}>
            {STATUS_ICON[s.status] || "⚪"} {s.name}
            {s.status === "connected" && s.latency_ms > 0 && (
              <span className="text-slate-600"> ({s.latency_ms.toFixed(0)}ms)</span>
            )}
            {s.daily_requests !== undefined && (
              <span className="text-slate-600"> ({s.daily_requests}/{s.daily_limit} {s.usage_pct}%)</span>
            )}
            {s.cached_indices !== undefined && (
              <span className="text-slate-600"> ({s.cached_indices} indices)</span>
            )}
          </span>
        ))
      ) : (
        <span>⏳ 等待数据源连接...</span>
      )}
      <span className="ml-auto flex items-center gap-4">
        {/* P1.6 · D1-D17 架构决策全景灯（点击展开详情） */}
        <DecisionTrackerBadge />
        <span className="text-slate-600">LIQ 防猎杀 v1.0</span>
      </span>
    </div>
  );
}
