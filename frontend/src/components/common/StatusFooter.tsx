"use client";

import { useMarketStore } from "@/stores/marketStore";

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
        sourceHealth.map((s) => (
          <span key={s.name}>
            {STATUS_ICON[s.status] || "⚪"} {s.name}
            {s.status === "connected" && s.latency_ms > 0 && (
              <span className="text-slate-600">({s.latency_ms.toFixed(0)}ms)</span>
            )}
          </span>
        ))
      ) : (
        <span>⏳ 等待数据源连接...</span>
      )}
      <span className="ml-auto text-slate-600">LIQ 防猎杀 v1.0</span>
    </div>
  );
}
