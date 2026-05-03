"use client";

/**
 * Calibration 图 · 预测置信度 vs 实际命中率
 *
 * 设计：
 *   - X 轴：predicted 置信度区间中点（50/55/60/.../100）
 *   - Y 轴：actual_win_rate（0~1）
 *   - 对角虚线：完美校准（y=x/100）
 *   - 横向虚线：盈亏平衡 win_rate ≈ 55.6%
 *   - 点大小：sample_size（log 缩放）
 *   - 解读：
 *     - 点在对角线上 = 模型校准良好（高置信确实更准）
 *     - 点在对角线之上 = 高估，但实际更准（保守）
 *     - 点在对角线之下 = 高估，实际不准（过度自信，需调阈值）
 *
 * 直接用 SVG（无外部依赖，避免 d3 重）—— d3 后续如需 zoom/tooltip 可换
 */

import { useMemo } from "react";

import { BREAK_EVEN_WIN_RATE, type CalibrationCurve } from "@/lib/scalpTypes";
import { useScalpStore } from "@/stores/scalpStore";

const CHART_WIDTH = 480;
const CHART_HEIGHT = 320;
const PAD_L = 50;
const PAD_R = 16;
const PAD_T = 16;
const PAD_B = 36;

export default function CalibrationChart() {
  const calibration = useScalpStore((s) => s.calibration);
  const calibrationLoading = useScalpStore((s) => s.calibrationLoading);
  const loadCalibration = useScalpStore((s) => s.loadCalibration);

  if (!calibration) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-500">
        加载校准数据...
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-[13px] font-semibold text-slate-200">置信度校准曲线</h3>
        <div className="flex items-center gap-2 text-[11px]">
          <span className="text-slate-500">
            样本：<span className="font-mono text-slate-300">{calibration.sample_size_total}</span>
          </span>
          <button
            onClick={() => loadCalibration(true)}
            disabled={calibrationLoading}
            className="rounded border border-sky-700/50 bg-sky-950/30 px-2 py-0.5 text-[10px] text-sky-300 hover:bg-sky-900/40 disabled:opacity-50"
          >
            {calibrationLoading ? "..." : "🔄 重算"}
          </button>
        </div>
      </div>

      {calibration.sample_size_total === 0 ? (
        <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 p-12 text-center text-[12px] text-slate-500">
          暂无样本 · 至少需要 1 条结算信号才能展示
        </div>
      ) : (
        <ChartSvg curve={calibration} />
      )}

      <div className="rounded border border-slate-800 bg-slate-900/30 p-3 text-[11px] text-slate-400">
        <div className="mb-1 font-semibold text-slate-300">如何解读？</div>
        <ul className="space-y-0.5 list-disc pl-4">
          <li>
            <span className="text-slate-200">点位接近对角线</span>：模型校准良好，"置信 80" 信号约 80% 命中
          </li>
          <li>
            <span className="text-slate-200">点位低于对角线</span>：过度自信，应调高对应阈值或降权该策略
          </li>
          <li>
            <span className="text-emerald-300">点位高于盈亏平衡线 ({(BREAK_EVEN_WIN_RATE * 100).toFixed(1)}%)</span>：
            该置信桶整体盈利
          </li>
        </ul>
      </div>
    </div>
  );
}

function ChartSvg({ curve }: { curve: CalibrationCurve }) {
  const innerW = CHART_WIDTH - PAD_L - PAD_R;
  const innerH = CHART_HEIGHT - PAD_T - PAD_B;

  // X 轴：0~100；Y 轴：0~1
  const xScale = (v: number) => PAD_L + (v / 100) * innerW;
  const yScale = (v: number) => PAD_T + innerH - v * innerH;

  // 点
  const points = useMemo(
    () =>
      curve.points.map((p) => {
        const xMid = (p.predicted_min + p.predicted_max) / 2;
        const radius = Math.min(8, Math.max(3, Math.sqrt(p.sample_size) * 1.2));
        return { ...p, xMid, radius };
      }),
    [curve.points],
  );

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-950/40 p-3">
      <svg
        width={CHART_WIDTH}
        height={CHART_HEIGHT}
        className="block"
        role="img"
        aria-label="calibration chart"
      >
        {/* 网格 */}
        {[0, 0.25, 0.5, 0.75, 1].map((y) => (
          <line
            key={`gy-${y}`}
            x1={PAD_L}
            x2={PAD_L + innerW}
            y1={yScale(y)}
            y2={yScale(y)}
            stroke="#1e293b"
            strokeDasharray="2,4"
          />
        ))}
        {[0, 25, 50, 75, 100].map((x) => (
          <line
            key={`gx-${x}`}
            x1={xScale(x)}
            x2={xScale(x)}
            y1={PAD_T}
            y2={PAD_T + innerH}
            stroke="#1e293b"
            strokeDasharray="2,4"
          />
        ))}

        {/* 完美校准对角线 */}
        <line
          x1={xScale(0)}
          y1={yScale(0)}
          x2={xScale(100)}
          y2={yScale(1)}
          stroke="#475569"
          strokeWidth={1}
          strokeDasharray="4,4"
        />
        <text
          x={xScale(95)}
          y={yScale(0.94)}
          fontSize={10}
          fill="#475569"
          textAnchor="end"
        >
          完美校准 y=x
        </text>

        {/* 盈亏平衡线 */}
        <line
          x1={xScale(0)}
          y1={yScale(BREAK_EVEN_WIN_RATE)}
          x2={xScale(100)}
          y2={yScale(BREAK_EVEN_WIN_RATE)}
          stroke="#f59e0b"
          strokeWidth={1}
          strokeDasharray="6,3"
          opacity={0.5}
        />
        <text
          x={xScale(2)}
          y={yScale(BREAK_EVEN_WIN_RATE) - 4}
          fontSize={10}
          fill="#f59e0b"
        >
          盈亏平衡 {(BREAK_EVEN_WIN_RATE * 100).toFixed(1)}%
        </text>

        {/* 数据点 + 折线 */}
        {points.length > 1 && (
          <polyline
            fill="none"
            stroke="#0ea5e9"
            strokeWidth={1.5}
            opacity={0.5}
            points={points.map((p) => `${xScale(p.xMid)},${yScale(p.actual_win_rate)}`).join(" ")}
          />
        )}
        {points.map((p, i) => {
          const fill = p.actual_win_rate >= BREAK_EVEN_WIN_RATE ? "#22c55e" : "#ef4444";
          return (
            <g key={i}>
              <circle
                cx={xScale(p.xMid)}
                cy={yScale(p.actual_win_rate)}
                r={p.radius}
                fill={fill}
                stroke="#0f172a"
                strokeWidth={1.5}
              />
              <title>
                {`置信 [${p.predicted_min}, ${p.predicted_max}) · n=${p.sample_size} · 实际胜率 ${(p.actual_win_rate * 100).toFixed(1)}%`}
              </title>
            </g>
          );
        })}

        {/* X 轴 */}
        <line
          x1={PAD_L}
          x2={PAD_L + innerW}
          y1={PAD_T + innerH}
          y2={PAD_T + innerH}
          stroke="#475569"
        />
        {[0, 25, 50, 75, 100].map((x) => (
          <text
            key={`xt-${x}`}
            x={xScale(x)}
            y={PAD_T + innerH + 16}
            fontSize={10}
            fill="#94a3b8"
            textAnchor="middle"
          >
            {x}
          </text>
        ))}
        <text
          x={PAD_L + innerW / 2}
          y={CHART_HEIGHT - 4}
          fontSize={11}
          fill="#cbd5e1"
          textAnchor="middle"
        >
          预测置信度
        </text>

        {/* Y 轴 */}
        <line x1={PAD_L} x2={PAD_L} y1={PAD_T} y2={PAD_T + innerH} stroke="#475569" />
        {[0, 0.25, 0.5, 0.75, 1].map((y) => (
          <text
            key={`yt-${y}`}
            x={PAD_L - 8}
            y={yScale(y) + 3}
            fontSize={10}
            fill="#94a3b8"
            textAnchor="end"
          >
            {(y * 100).toFixed(0)}%
          </text>
        ))}
        <text
          x={12}
          y={PAD_T + innerH / 2}
          fontSize={11}
          fill="#cbd5e1"
          textAnchor="middle"
          transform={`rotate(-90, 12, ${PAD_T + innerH / 2})`}
        >
          实际命中率
        </text>
      </svg>
    </div>
  );
}
