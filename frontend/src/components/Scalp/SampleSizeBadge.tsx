/**
 * SampleSizeBadge · P0-8
 *
 * 在显示「命中率 / hit_probability」附近，强制配套显示样本量四档：
 *   cold_start  N<30   灰色   "冷启动"
 *   early       30-100 琥珀  "样本不足"
 *   mid         100-300 青色 "校准中"
 *   mature      300+   绿色  "可信"
 *
 * 使用场景：
 *   - SignalCard 命中率行
 *   - StrategyComparePanel 各策略 win_rate 行
 *   - CalibrationChart 各点 tooltip
 */

import {
  SAMPLE_TIER_BADGE_COLOR,
  SAMPLE_TIER_LABEL,
  tierForSampleSize,
} from "@/lib/scalpTypes";

interface Props {
  n: number;
  /** 是否显示 N 数字（默认显示） */
  showCount?: boolean;
  /** 紧凑模式：仅圆点 + 数字 */
  compact?: boolean;
  className?: string;
}

export default function SampleSizeBadge({
  n,
  showCount = true,
  compact = false,
  className = "",
}: Props) {
  const tier = tierForSampleSize(n);
  const color = SAMPLE_TIER_BADGE_COLOR[tier];
  const label = SAMPLE_TIER_LABEL[tier];

  if (compact) {
    return (
      <span
        className={`inline-flex items-center gap-1 ${className}`}
        title={label}
      >
        <span
          className="w-1.5 h-1.5 rounded-full inline-block"
          style={{ backgroundColor: color }}
        />
        {showCount && (
          <span className="text-[10px] text-slate-400 font-mono">N={n}</span>
        )}
      </span>
    );
  }

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded text-[10px] font-medium ${className}`}
      style={{
        backgroundColor: `${color}1a`, // ~10% alpha
        color,
        border: `1px solid ${color}40`,
      }}
      title={`${label} · 样本数 = ${n}`}
    >
      <span
        className="w-1.5 h-1.5 rounded-full inline-block"
        style={{ backgroundColor: color }}
      />
      {label.split(" ")[0]}
      {showCount && <span className="font-mono opacity-80">N={n}</span>}
    </span>
  );
}
