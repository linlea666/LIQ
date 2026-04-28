"use client";

/**
 * V2 0-1 分数分桶校准小图（V3-M3.1 · 2026-04）
 *
 * 设计：
 *   - 5 个等宽桶：[0,0.2)/(0.2,0.4)/(0.4,0.6)/(0.6,0.8)/(0.8,1.0]
 *   - 每桶 hit_rate 用柱高表示，桶宽固定
 *   - 期望趋势：高分桶 hit_rate ≥ 低分桶（弱单调）
 *   - 不单调时角标 ⚠ 提示"V2 分数判别力不足"
 *   - 桶 sample_size<3 灰显（视觉上保留位置但不计入趋势）
 */

import type { CalibrationBucketDict } from "@/lib/types";

export default function V1V2CalibrationChart({
  buckets,
  monotonic,
}: {
  buckets: CalibrationBucketDict[];
  monotonic: boolean;
}) {
  const maxHit = Math.max(0.01, ...buckets.map((b) => b.hit_rate));
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-[10px] text-slate-500">
        <span>V2 分数校准（5 桶）</span>
        {monotonic ? (
          <span className="text-emerald-400">✓ 弱单调</span>
        ) : (
          <span className="text-amber-400">⚠ 不单调（V2 分数判别力不足）</span>
        )}
      </div>
      <div className="grid grid-cols-5 gap-1">
        {buckets.map((b, i) => {
          const heightPct = Math.max(2, Math.round((b.hit_rate / maxHit) * 100));
          const hasSample = b.sample_size >= 3;
          return (
            <div key={i} className="flex flex-col items-center gap-0.5">
              <div className="w-full h-12 bg-slate-800/40 rounded-sm relative flex items-end overflow-hidden">
                <div
                  className={`w-full ${
                    hasSample ? "bg-sky-500/70" : "bg-slate-600/40"
                  }`}
                  style={{ height: `${heightPct}%` }}
                />
                {hasSample && (
                  <span className="absolute inset-x-0 top-0.5 text-[9px] text-center text-slate-100 font-mono">
                    {Math.round(b.hit_rate * 100)}%
                  </span>
                )}
              </div>
              <div className="text-[9px] text-slate-500 font-mono">
                {b.range_low.toFixed(1)}-{b.range_high.toFixed(1)}
              </div>
              <div className="text-[9px] text-slate-600 font-mono">
                n={b.sample_size}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}
