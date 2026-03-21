"use client";

import { useMarketStore } from "@/stores/marketStore";
import { API_BASE } from "@/lib/constants";

export default function AIButton() {
  const coin = useMarketStore((s) => s.coin);
  const aiLoading = useMarketStore((s) => s.aiLoading);
  const setAILoading = useMarketStore((s) => s.setAILoading);
  const setAIResult = useMarketStore((s) => s.setAIResult);
  const setAIError = useMarketStore((s) => s.setAIError);
  const setAIPanelOpen = useMarketStore((s) => s.setAIPanelOpen);

  const handleClick = async () => {
    setAILoading(true);
    setAIPanelOpen(true);
    try {
      const res = await fetch(`${API_BASE}/api/ai/analyze/${coin}`, { method: "POST" });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      const data = await res.json();
      setAIResult(data);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "分析失败";
      setAIError(msg);
    }
  };

  return (
    <button
      onClick={handleClick}
      disabled={aiLoading}
      className={`px-4 py-1.5 rounded-lg text-sm font-semibold transition-all ${
        aiLoading
          ? "bg-slate-700 text-slate-400 cursor-wait"
          : "bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
      }`}
    >
      {aiLoading ? "⏳ 分析中..." : "🤖 AI 分析"}
    </button>
  );
}
