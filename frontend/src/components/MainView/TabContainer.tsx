"use client";

import { useMarketStore } from "@/stores/marketStore";
import LiquidationMapView from "./LiquidationMapView";
import CVDOIChart from "./CVDOIChart";
import WaterfallChart from "./WaterfallChart";
import MarketSummary from "./MarketSummary";
import RangeSignalView from "./RangeSignalView";
import KeyLevelView from "./KeyLevelView";
import TrendExhaustionView from "./TrendExhaustionView";

// Tab 顺序遵循决策链：结构 → 箱体（空间）→ 动能（续航/衰竭）→ 关键位（点位）
// 即先确认整体方向，再看"哪里打"，再看"还能不能继续打"，最后看"具体攻哪一根"。
const PRO_TABS = [
  { id: "liquidation", label: "清算地图" },
  { id: "cvd_oi", label: "CVD + OI" },
  { id: "waterfall", label: "数据总览" },
  { id: "summary", label: "市场总结" },
  { id: "range_signal", label: "箱体信号" },
  { id: "trend_exhaustion", label: "动能/衰竭" },
  { id: "key_level", label: "关键位" },
] as const;

const BEGINNER_TABS = [
  { id: "summary", label: "市场总结" },
  { id: "range_signal", label: "箱体信号" },
  { id: "trend_exhaustion", label: "动能/衰竭" },
  { id: "key_level", label: "关键位" },
  { id: "liquidation", label: "清算地图" },
] as const;

export default function TabContainer() {
  const activeTab = useMarketStore((s) => s.activeTab);
  const setActiveTab = useMarketStore((s) => s.setActiveTab);
  const displayMode = useMarketStore((s) => s.displayMode);

  const tabs = displayMode === "beginner" ? BEGINNER_TABS : PRO_TABS;
  const validTab = tabs.find((t) => t.id === activeTab) ? activeTab : tabs[0].id;

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex border-b border-slate-700">
        {tabs.map((tab) => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            className={`px-4 py-2 text-sm font-medium transition-all border-b-2 ${
              validTab === tab.id
                ? "text-blue-400 border-blue-400"
                : "text-slate-500 border-transparent hover:text-slate-300"
            }`}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <div className="flex-1 p-4 overflow-auto">
        {validTab === "liquidation" && <LiquidationMapView />}
        {validTab === "cvd_oi" && <CVDOIChart />}
        {validTab === "waterfall" && <WaterfallChart />}
        {validTab === "summary" && <MarketSummary />}
        {validTab === "range_signal" && <RangeSignalView />}
        {validTab === "trend_exhaustion" && <TrendExhaustionView />}
        {validTab === "key_level" && <KeyLevelView />}
      </div>
    </div>
  );
}
