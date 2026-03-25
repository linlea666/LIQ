"use client";

import { useMarketStore } from "@/stores/marketStore";
import { API_BASE } from "@/lib/constants";

export default function AIButton() {
  const coin = useMarketStore((s) => s.coin);
  const aiLoading = useMarketStore((s) => s.aiLoading);
  const aiAvailable = useMarketStore((s) => s.aiAvailable);
  const setAILoading = useMarketStore((s) => s.setAILoading);
  const setAIError = useMarketStore((s) => s.setAIError);
  const setAIPanelOpen = useMarketStore((s) => s.setAIPanelOpen);

  const handleClick = async () => {
    setAIPanelOpen(true);

    if (!aiAvailable) {
      setAIError("AI 未配置：请在后端 .env 中设置 DEEPSEEK_API_KEY 或 OPENAI_API_KEY");
      return;
    }

    setAILoading(true);
    try {
      const res = await fetch(`${API_BASE}/api/ai/analyze/${coin}`, {
        method: "POST",
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      // POST 立即返回 {status:"processing"}，结果通过 WebSocket 推送
    } catch (e: unknown) {
      let msg = "分析请求失败";
      if (e instanceof Error) {
        if (e.message.includes("Failed to fetch")) {
          msg = "无法连接后端服务，请检查网络或后端是否运行";
        } else {
          msg = e.message;
        }
      }
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
          : !aiAvailable
          ? "bg-slate-700 text-slate-500 hover:bg-slate-600 cursor-pointer"
          : "bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
      }`}
    >
      {aiLoading ? "⏳ 分析中..." : "🤖 AI 分析"}
    </button>
  );
}
