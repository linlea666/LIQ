"use client";

import { useMarketStore } from "@/stores/marketStore";
import {
  biasBrief,
  directionBrief,
  eventBrief,
} from "@/lib/structureBrief";

/**
 * 关键位页顶部市场结构徽章
 *
 * 数据源：RangeSignalData.ms_*（后端在 Commit 3 里把 1h MarketStructure
 * 的方向 / 事件 / 偏置 / 置信度合并到 RangeSignal 输出，前端复用即可）
 *
 * 交互：鼠标悬停每个徽章可看详细解释（小白友好）
 */
export default function MarketStructureBadge() {
  const data = useMarketStore((s) => s.data[s.coin]);
  const rs = data?.range_signal;

  if (!rs || !rs.ms_direction) {
    return null;
  }

  const direction = directionBrief(rs.ms_direction);
  const event = eventBrief(rs.ms_event);
  const bias = biasBrief(rs.ms_bias);
  const confidence = rs.ms_confidence ?? 0;

  if (!direction && !event && !bias) return null;

  return (
    <div className="bg-slate-800/60 border border-slate-600 rounded-lg px-4 py-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className="text-[10px] text-slate-500 shrink-0"
          title="来自 1h 级别 K 线的 Price-Action / SMC 结构识别"
        >
          🎯 市场结构 · 1h
        </span>

        {direction && (
          <span
            className={`px-2 py-0.5 rounded text-xs font-medium cursor-help ${direction.bg ?? ""} ${direction.color}`}
            title={direction.hint}
          >
            {direction.label}
          </span>
        )}

        {event && (
          <span
            className={`px-2 py-0.5 rounded text-[11px] cursor-help ${event.bg ?? ""} ${event.color}`}
            title={event.hint}
          >
            {event.label}
          </span>
        )}

        {bias && (
          <span
            className={`px-2 py-0.5 rounded text-xs font-medium cursor-help ${bias.bg ?? ""} ${bias.color}`}
            title={bias.hint}
          >
            {bias.label}
          </span>
        )}

        {confidence > 0 && (
          <span
            className="ml-auto text-[10px] text-slate-400 cursor-help"
            title={`基于摆动点数量 / 趋势一致性 / 事件新鲜度 / 结构宽度综合评分 = ${(confidence * 100).toFixed(0)}%`}
          >
            置信度 {(confidence * 100).toFixed(0)}%
          </span>
        )}
      </div>
    </div>
  );
}
