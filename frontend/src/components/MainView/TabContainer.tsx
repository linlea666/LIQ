"use client";

import { useMarketStore } from "@/stores/marketStore";
import LiquidationMapView from "./LiquidationMapView";
import CVDOIChart from "./CVDOIChart";
import WaterfallChart from "./WaterfallChart";

const TABS = [
  { id: "liquidation", label: "清算地图" },
  { id: "cvd_oi", label: "CVD + OI" },
  { id: "waterfall", label: "数据总览" },
] as const;

export default function TabContainer() {
  const activeTab = useMarketStore((s) => s.activeTab);
  const setActiveTab = useMarketStore((s) => s.setActiveTab);

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex border-b border-slate-700">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-2 text-sm font-medium transition-all border-b-2 ${
              activeTab === tab.id
                ? "text-blue-400 border-blue-400"
                : "text-slate-500 border-transparent hover:text-slate-300"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="flex-1 p-4 overflow-auto">
        {activeTab === "liquidation" && <LiquidationMapView />}
        {activeTab === "cvd_oi" && <CVDOIChart />}
        {activeTab === "waterfall" && <WaterfallChart />}
      </div>
    </div>
  );
}
