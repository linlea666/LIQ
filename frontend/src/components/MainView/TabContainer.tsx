"use client";

import { useMarketStore } from "@/stores/marketStore";
import LiquidationMapView from "./LiquidationMapView";
import CVDOIChart from "./CVDOIChart";
import WaterfallChart from "./WaterfallChart";
import MarketSummary from "./MarketSummary";
import RangeSignalView from "./RangeSignalView";
import KeyLevelView from "./KeyLevelView";
import MarketActionView from "./MarketActionView";

// Tab 顺序遵循决策链：结构 → 箱体（空间）→ 动作分析（AI 结构化判断）→ 关键位（点位）
// 动作分析 (Market Action Analyzer · MAA) 替换原「动能/衰竭」模块，基于 14 维真实
// 市场动作 + DeepSeek V4-Flash（非思考模式）产出结构化结论 + 证据矩阵 + 操作建议。
const PRO_TABS = [
  { id: "liquidation", label: "清算地图" },
  { id: "cvd_oi", label: "CVD + OI" },
  { id: "waterfall", label: "数据总览" },
  { id: "summary", label: "市场总结" },
  { id: "range_signal", label: "箱体信号" },
  { id: "market_action", label: "动作分析 ⚡" },
  { id: "key_level", label: "关键位" },
] as const;

const BEGINNER_TABS = [
  { id: "summary", label: "市场总结" },
  { id: "range_signal", label: "箱体信号" },
  { id: "market_action", label: "动作分析 ⚡" },
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
        {validTab === "market_action" && <MarketActionView />}
        {validTab === "key_level" && <KeyLevelView />}
      </div>
    </div>
  );
}
