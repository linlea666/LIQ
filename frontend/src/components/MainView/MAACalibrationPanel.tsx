"use client";

/**
 * MAACalibrationPanel · Market Action Analyzer 事后评估面板（Phase 5）
 *
 * 展示三块：
 *   1. Horizon 命中率（T+4h / T+8h / T+24h）
 *   2. Confidence 分桶校准（理想：高分桶准确率 ≥ 整体准确率）
 *   3. Per-scenario 准确率（24h）
 *
 * 数据源：GET /api/market-action/eval?coin=... · 由 engine 每 30 分钟刷新一次缓存。
 * 初次进入页面懒加载；点击"刷新"按钮强制 refresh=1（后端会实时重算）。
 */

import { useEffect, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import type { MAACalibrationBucket, MAAHorizonStats, MAAScenario } from "@/lib/types";

const SCENARIO_LABEL: Record<MAAScenario, string> = {
  trend_continuation_up: "上行延续",
  trend_continuation_down: "下行延续",
  short_squeeze_up: "空头挤压",
  long_squeeze_down: "多头挤压",
  fake_breakout_up: "假突破",
  fake_breakdown_down: "假跌破",
  exhaustion_top: "顶部衰竭",
  exhaustion_bottom: "底部衰竭",
  range_bound: "区间震荡",
};

function AccuracyBar({ value, color = "#22c55e" }: { value: number | null; color?: string }) {
  if (value == null) {
    return <span className="text-xs text-slate-600 italic">—</span>;
  }
  const pct = Math.max(0, Math.min(100, value));
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1.5 bg-slate-800 rounded overflow-hidden min-w-[60px]">
        <div
          className="h-full transition-all"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      <span className="text-xs font-mono font-semibold text-slate-200 w-12 text-right">
        {pct.toFixed(1)}%
      </span>
    </div>
  );
}

function HorizonRow({ stat }: { stat: MAAHorizonStats }) {
  const color =
    stat.accuracy_pct == null
      ? "#64748b"
      : stat.accuracy_pct >= 60
      ? "#22c55e"
      : stat.accuracy_pct >= 45
      ? "#3b82f6"
      : stat.accuracy_pct >= 30
      ? "#eab308"
      : "#ef4444";
  return (
    <div className="flex items-center gap-3 text-[12px]">
      <span className="w-12 font-mono text-slate-400">T+{stat.horizon}</span>
      <div className="flex-1">
        <AccuracyBar value={stat.accuracy_pct} color={color} />
      </div>
      <span className="text-[10px] font-mono text-slate-500 w-28 text-right">
        {stat.correct}✓ / {stat.wrong}✗ · 中性 {stat.neutral}
      </span>
    </div>
  );
}

function CalibrationRow({ b, baseline }: { b: MAACalibrationBucket; baseline: number | null }) {
  const diff =
    baseline != null && b.accuracy_pct != null ? b.accuracy_pct - baseline : null;
  const diffColor =
    diff == null ? "#64748b" : diff >= 5 ? "#22c55e" : diff <= -5 ? "#ef4444" : "#94a3b8";
  return (
    <div className="flex items-center gap-3 text-[11px]">
      <span className="w-14 font-mono text-slate-400">{b.range}</span>
      <div className="flex-1">
        <AccuracyBar value={b.accuracy_pct} />
      </div>
      <span className="text-[10px] font-mono text-slate-500 w-10 text-right">
        n={b.sample_size}
      </span>
      <span
        className="text-[10px] font-mono w-12 text-right"
        style={{ color: diffColor }}
        title="相对 24h 整体准确率的偏差"
      >
        {diff == null ? "—" : `${diff > 0 ? "+" : ""}${diff.toFixed(1)}`}
      </span>
    </div>
  );
}

export default function MAACalibrationPanel() {
  const coin = useMarketStore((s) => s.coin);
  const summary = useMarketStore((s) => s.maaEvalByCoin[coin]);
  const loading = useMarketStore((s) => s.maaEvalLoadingByCoin[coin] ?? false);
  const loadMAAEval = useMarketStore((s) => s.loadMAAEval);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (!summary) {
      loadMAAEval(coin);
    }
  }, [coin, summary, loadMAAEval]);

  if (!summary) {
    return (
      <div className="bg-slate-900/30 border border-slate-800 rounded-lg p-3 text-xs text-slate-400">
        <div className="flex items-center justify-between">
          <span>📊 AI 校准</span>
          <span className="text-slate-600">
            {loading ? "计算中…" : "暂无样本（需 ≥ 4 小时累积）"}
          </span>
        </div>
      </div>
    );
  }

  const h24 = summary.horizons.find((h) => h.horizon === "24h");
  const baseline24 = h24?.accuracy_pct ?? null;

  const headline =
    summary.sample_size === 0
      ? "样本积累中"
      : baseline24 == null
      ? `样本 ${summary.sample_size} · 兑现中`
      : `样本 ${summary.sample_size} · 24h 命中 ${baseline24.toFixed(1)}%`;

  return (
    <div className="bg-slate-900/30 border border-slate-800 rounded-lg overflow-hidden">
      {/* Header */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-3 py-2 flex items-center justify-between hover:bg-slate-900/50 transition"
      >
        <div className="flex items-center gap-2">
          <span className="text-xs font-semibold text-slate-300">📊 AI 校准</span>
          <span className="text-[11px] text-slate-500">· {summary.window_days}d</span>
          <span className="text-[11px] text-blue-300">{headline}</span>
        </div>
        <div className="flex items-center gap-2">
          <span
            className="text-[10px] text-slate-500 hover:text-slate-300"
            onClick={(e) => {
              e.stopPropagation();
              loadMAAEval(coin, { refresh: true });
            }}
          >
            {loading ? "…" : "刷新"}
          </span>
          <span className="text-slate-500 text-xs">{expanded ? "▾" : "▸"}</span>
        </div>
      </button>

      {/* Body */}
      {expanded && (
        <div className="px-3 pb-3 space-y-3 border-t border-slate-800 pt-3">
          {/* Horizon 命中率 */}
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">
              Horizon 兑现率
            </div>
            <div className="space-y-1.5">
              {summary.horizons.map((h) => (
                <HorizonRow key={h.horizon} stat={h} />
              ))}
            </div>
          </div>

          {/* Confidence 校准 */}
          <div>
            <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">
              Confidence 分桶校准 · 基于 T+24h
              <span className="ml-2 text-slate-600 normal-case tracking-normal">
                {baseline24 != null && `整体 ${baseline24.toFixed(1)}%`}
              </span>
            </div>
            <div className="space-y-1">
              {summary.calibration.map((b) => (
                <CalibrationRow key={b.range} b={b} baseline={baseline24} />
              ))}
            </div>
          </div>

          {/* Per-scenario */}
          {summary.per_scenario.length > 0 && (
            <div>
              <div className="text-[10px] font-semibold uppercase tracking-wider text-slate-500 mb-1.5">
                Scenario 准确率 · T+24h
              </div>
              <div className="flex flex-wrap gap-1.5">
                {summary.per_scenario.map((s) => (
                  <span
                    key={s.scenario}
                    className="inline-flex items-center gap-1 text-[11px] px-2 py-0.5 rounded border border-slate-700 bg-slate-800/40"
                  >
                    <span className="text-slate-300">
                      {SCENARIO_LABEL[s.scenario] ?? s.scenario}
                    </span>
                    <span className="text-slate-500 font-mono text-[10px]">
                      n={s.sample_size}
                    </span>
                    <span
                      className="font-mono text-[10px] font-semibold"
                      style={{
                        color:
                          s.accuracy_pct == null
                            ? "#64748b"
                            : s.accuracy_pct >= 60
                            ? "#22c55e"
                            : s.accuracy_pct >= 40
                            ? "#eab308"
                            : "#ef4444",
                      }}
                    >
                      {s.accuracy_pct == null ? "—" : `${s.accuracy_pct.toFixed(0)}%`}
                    </span>
                  </span>
                ))}
              </div>
            </div>
          )}

          <div className="text-[10px] text-slate-600 font-mono">
            last updated · {new Date(summary.last_updated_ts * 1000).toLocaleString()}
          </div>
        </div>
      )}
    </div>
  );
}
