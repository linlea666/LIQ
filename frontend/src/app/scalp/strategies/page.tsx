"use client";

/**
 * 策略管理页 · /scalp/strategies
 *
 * 完整版配置面板：
 *   - 全局开关（enabled / coin / horizon）
 *   - 三策略 grid（每张卡独立 enable / 阈值 / 冷却 / 备注）
 *   - 通知（browser / email + 阈值）
 *   - 提交保存（diff patch · 仅传 dirty 字段）
 */

import StrategyConfigPanel from "@/components/Scalp/StrategyConfigPanel";

export default function ScalpStrategiesPage() {
  return (
    <div className="space-y-3">
      <header>
        <h1 className="text-lg font-semibold text-slate-100">策略管理</h1>
        <p className="mt-1 text-[12px] text-slate-500">
          建议进度：先启用一个策略观察 24-48h（≥30 单），按命中率调阈值或加策略；
          所有改动立即热更新到引擎，无需重启
        </p>
      </header>
      <StrategyConfigPanel />
    </div>
  );
}
