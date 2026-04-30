"use client";

import { useMarketStore } from "@/stores/marketStore";
import { API_BASE } from "@/lib/constants";

export default function AIButton() {
  const coin = useMarketStore((s) => s.coin);
  const strategicLoading = useMarketStore((s) => s.strategicLoading);
  const strategicAvailable = useMarketStore((s) => s.strategicAvailable);
  const setStrategicLoading = useMarketStore((s) => s.setStrategicLoading);
  const setStrategicError = useMarketStore((s) => s.setStrategicError);
  const setAIPanelOpen = useMarketStore((s) => s.setAIPanelOpen);

  const handleClick = async () => {
    setAIPanelOpen(true);

    if (!strategicAvailable) {
      setStrategicError(
        "Strategic 主 AI 不可用：请在后端配置 API Key，并确认 arbiter 可加载",
      );
      return;
    }

    setStrategicLoading(true);
    try {
      const res = await fetch(
        `${API_BASE}/api/strategic/run?coin=${encodeURIComponent(coin)}`,
        { method: "POST" },
      );
      if (!res.ok && res.status !== 409) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
    } catch (e: unknown) {
      let msg = "Strategic 请求失败";
      if (e instanceof Error) {
        if (e.message.includes("Failed to fetch")) {
          msg = "无法连接后端服务，请检查网络或后端是否运行";
        } else {
          msg = e.message;
        }
      }
      setStrategicError(msg);
    }
  };

  return (
    <button
      onClick={handleClick}
      disabled={strategicLoading}
      className={`px-4 py-1.5 rounded-lg text-sm font-semibold transition-all ${
        strategicLoading
          ? "bg-slate-700 text-slate-400 cursor-wait"
          : !strategicAvailable
            ? "bg-slate-700 text-slate-500 hover:bg-slate-600 cursor-pointer"
            : "bg-blue-600 hover:bg-blue-500 text-white cursor-pointer"
      }`}
    >
      {strategicLoading ? "⏳ Strategic…" : "🤖 Strategic"}
    </button>
  );
}
