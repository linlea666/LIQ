"use client";

/**
 * 关键位排序切换器（V3 全景页用）
 *
 * 三种模式：
 *   - tier:     强度优先（S→A→B→C，同 tier 比 final_score，同分比距离）—— 默认
 *   - distance: 按距当前价由近到远
 *   - cascade:  按级联风险从高到低（破位后果优先）
 */

import type { SortKey } from "@/lib/levelBrief";

const OPTIONS: Array<{ key: SortKey; label: string; hint: string }> = [
  {
    key: "tier",
    label: "按强度",
    hint: "S→A→B→C，同等级按共振分，再按距离",
  },
  {
    key: "distance",
    label: "按距离",
    hint: "由近到远，关注短线即时反应",
  },
  {
    key: "cascade",
    label: "按级联",
    hint: "级联风险从高到低，关注破位后果",
  },
];

export default function LevelSortControl({
  value,
  onChange,
}: {
  value: SortKey;
  onChange: (k: SortKey) => void;
}) {
  return (
    <div className="inline-flex items-center gap-1 bg-slate-800/60 border border-slate-700/60 rounded-md p-0.5">
      {OPTIONS.map((opt) => (
        <button
          key={opt.key}
          type="button"
          onClick={() => onChange(opt.key)}
          title={opt.hint}
          className={`px-2.5 py-1 text-[11px] rounded transition-colors ${
            value === opt.key
              ? "bg-slate-700 text-white font-medium"
              : "text-slate-400 hover:text-slate-200"
          }`}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}
