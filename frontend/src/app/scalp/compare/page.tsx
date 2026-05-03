"use client";

/**
 * 策略对比 + Calibration 页 · /scalp/compare
 *
 * 上半：StrategyComparePanel（横向对比 + 子分桶）
 * 下半：CalibrationChart（置信 vs 命中率散点）
 */

import CalibrationChart from "@/components/Scalp/CalibrationChart";
import StrategyComparePanel from "@/components/Scalp/StrategyComparePanel";

export default function ScalpComparePage() {
  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-lg font-semibold text-slate-100">策略对比 · 校准</h1>
        <p className="mt-1 text-[12px] text-slate-500">
          基于历史结算信号统计 · 默认从缓存读，点击"实时重算"强制刷新
        </p>
      </header>

      <StrategyComparePanel />

      <section className="rounded-lg border border-slate-800 bg-slate-900/30 p-4">
        <CalibrationChart />
      </section>
    </div>
  );
}
