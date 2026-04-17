"use client";

import { useMarketStore } from "@/stores/marketStore";
import {
  biasBrief,
  directionBrief,
  eventBrief,
  macdMomentumBrief,
  regimeBrief,
} from "@/lib/structureBrief";

/**
 * 关键位页顶部统一"结构导航条"
 *
 * 把分散在各 Tab / 模块的结构性判断合并到一眼可看的徽章条，避免用户
 * 在不同 Tab 间来回对比。
 *
 * 数据渠道：
 *   - rangeSignal.ms_*            → 1h 结构方向 / 事件 / 操作偏置 / 置信度
 *   - kl.bull_bear_line.regime    → 日/周线中长期基调（老 structure_summary 统一并入这里）
 *   - rangeSignal.macd_daily_*    → MACD 动能状态（从箱体 Tab 合并过来）
 *
 * 设计原则：
 *   - 任何字段不就绪 → 对应 chip 不渲染（不占位、不报错）
 *   - hover 显 tooltip，小白也能读懂
 *   - 顶部徽章是"视觉主体"，其他模块文字描述应避免重复"市场结构"字眼
 */
export default function MarketStructureBadge() {
  const data = useMarketStore((s) => s.data[s.coin]);
  const rs = data?.range_signal;
  const kl = data?.key_levels_v2;

  if (!rs || !rs.ms_direction) {
    return null;
  }

  const direction = directionBrief(rs.ms_direction);
  const event = eventBrief(rs.ms_event);
  const bias = biasBrief(rs.ms_bias);
  const regime = regimeBrief(kl?.bull_bear_line?.current_regime);
  const macd = macdMomentumBrief(
    rs.macd_daily_above_zero,
    rs.macd_daily_hist_rising,
  );
  const confidence = rs.ms_confidence ?? 0;

  if (!direction && !event && !bias && !regime && !macd) return null;

  return (
    <div className="bg-slate-800/60 border border-slate-600 rounded-lg px-4 py-2.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className="text-[10px] text-slate-500 shrink-0"
          title="短期 1h 结构 + 中长期日/周基调 + 日线动能三视角合一"
        >
          🎯 结构导航
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

        <span className="h-4 w-px bg-slate-600 mx-1" aria-hidden />

        {regime && (
          <span
            className={`px-2 py-0.5 rounded text-[11px] cursor-help ${regime.bg ?? ""} ${regime.color}`}
            title={regime.hint}
          >
            {regime.label}
          </span>
        )}

        {macd && (
          <span
            className={`px-2 py-0.5 rounded text-[11px] cursor-help ${macd.bg ?? ""} ${macd.color}`}
            title={macd.hint}
          >
            {macd.label}
          </span>
        )}

        {confidence > 0 && (
          <span
            className="ml-auto text-[10px] text-slate-400 cursor-help"
            title={`1h 结构置信度：基于摆动点数量 / 趋势一致性 / 事件新鲜度 / 结构宽度综合评分 = ${(confidence * 100).toFixed(0)}%`}
          >
            1h 置信 {(confidence * 100).toFixed(0)}%
          </span>
        )}
      </div>
    </div>
  );
}
