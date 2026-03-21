"use client";

import { useWebSocket } from "@/hooks/useWebSocket";
import PriceBar from "@/components/TopBar/PriceBar";
import CoinSelector from "@/components/TopBar/CoinSelector";
import StatusBadges from "@/components/TopBar/StatusBadges";
import AIButton from "@/components/TopBar/AIButton";
import CoreFactors from "@/components/FactorCards/CoreFactors";
import TabContainer from "@/components/MainView/TabContainer";
import AIAnalysis from "@/components/SidePanel/AIAnalysis";
import LiveFeed from "@/components/SidePanel/LiveFeed";
import StatusFooter from "@/components/common/StatusFooter";
import { useMarketStore } from "@/stores/marketStore";
import { useEffect } from "react";
import { API_BASE } from "@/lib/constants";

export default function Dashboard() {
  useWebSocket();

  const setSourceHealth = useMarketStore((s) => s.setSourceHealth);

  useEffect(() => {
    const fetchHealth = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/health`);
        if (res.ok) {
          const data = await res.json();
          if (data.sources) setSourceHealth(data.sources);
        }
      } catch { /* silent */ }
    };
    fetchHealth();
    const timer = setInterval(fetchHealth, 10000);
    return () => clearInterval(timer);
  }, [setSourceHealth]);

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      {/* Top Bar */}
      <header className="shrink-0 border-b border-slate-700 bg-slate-900/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-4 py-2">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="text-lg font-bold text-blue-400">🛡️ LIQ</span>
              <span className="text-[10px] text-slate-600 hidden sm:inline">防猎杀 v1.0</span>
            </div>
            <CoinSelector />
            <PriceBar />
          </div>
          <div className="flex items-center gap-4">
            <StatusBadges />
            <ModeSelector />
            <AIButton />
          </div>
        </div>
      </header>

      {/* Factor Cards Row */}
      <div className="shrink-0 px-4 py-2 border-b border-slate-800">
        <CoreFactors />
      </div>

      {/* Main Content */}
      <div className="flex-1 flex min-h-0">
        {/* Main View */}
        <div className="flex-1 flex flex-col min-w-0">
          <TabContainer />
        </div>

        {/* Right Side Panel */}
        <div className="w-[280px] border-l border-slate-700 bg-slate-900/50 overflow-y-auto p-3 shrink-0">
          <LiveFeed />
        </div>

        {/* AI Analysis Drawer */}
        <AIAnalysis />
      </div>

      {/* Footer */}
      <StatusFooter />
    </div>
  );
}

function ModeSelector() {
  const displayMode = useMarketStore((s) => s.displayMode);
  const setDisplayMode = useMarketStore((s) => s.setDisplayMode);
  const modes = [
    { key: "beginner" as const, label: "小白" },
    { key: "advanced" as const, label: "进阶" },
    { key: "pro" as const, label: "专业" },
  ];

  return (
    <div className="flex gap-0.5 bg-slate-800 rounded-md p-0.5 text-[11px]">
      {modes.map((m) => (
        <button
          key={m.key}
          onClick={() => setDisplayMode(m.key)}
          className={`px-2 py-0.5 rounded transition-all ${
            displayMode === m.key
              ? "bg-slate-600 text-white"
              : "text-slate-500 hover:text-slate-300"
          }`}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}
