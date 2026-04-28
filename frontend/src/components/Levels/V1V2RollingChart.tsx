"use client";

/**
 * V3-M4 P0-3 · V1/V2 对比指标 14 天滑窗折线
 *
 * 数据流：
 *   parent fetch /api/key-levels/v1v2-rolling/{coin} → V1V2RollingResponse →
 *   本组件：3 张折线图（每维度一张，画 sample_size + Bonferroni p + Δprecision）
 *
 * 设计：
 *   - 用纯 SVG 绘制（不引入 chart 库）；3 条折线叠加在同一坐标系
 *   - 横轴：anchor_ts；纵轴：归一化指标
 *   - is_v2_significantly_better 用绿点标记
 *   - 文案首要服务"今天 V2 离切换有多远"的语感
 */

import type { RollingAnchor, RollingDimensionPoint, V1V2Dimension } from "@/lib/types";

const DIMENSION_TITLE: Record<string, string> = {
  bounce_quality: "反弹质量",
  breakout_stage: "突破阶段",
  fake_break: "假破回收",
};

const M4_THRESHOLD_N = 100;
const SIG_P = 0.05;
const SIG_DELTA_PREC = 0.05;

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000);
  const month = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${month}-${day}`;
}

function MiniLine({
  values,
  width = 240,
  height = 36,
  yMin = 0,
  yMax = 1,
  strokeColor = "#38bdf8",
  thresholdY,
  thresholdColor = "#fbbf24",
  passMask,
}: {
  values: number[];
  width?: number;
  height?: number;
  yMin?: number;
  yMax?: number;
  strokeColor?: string;
  thresholdY?: number;
  thresholdColor?: string;
  passMask?: boolean[]; // 与 values 等长，true 时点画绿色（满足条件）
}) {
  if (values.length === 0) {
    return (
      <div className="flex items-center justify-center text-[10px] text-slate-600 h-9">
        无数据
      </div>
    );
  }
  const span = Math.max(yMax - yMin, 1e-9);
  const stepX = values.length > 1 ? width / (values.length - 1) : 0;
  const points = values
    .map((v, i) => {
      const cx = i * stepX;
      const cy = height - ((Math.max(yMin, Math.min(yMax, v)) - yMin) / span) * height;
      return `${cx.toFixed(1)},${cy.toFixed(1)}`;
    })
    .join(" ");

  return (
    <svg width={width} height={height} className="overflow-visible">
      {thresholdY !== undefined && (
        <line
          x1={0}
          x2={width}
          y1={height - ((thresholdY - yMin) / span) * height}
          y2={height - ((thresholdY - yMin) / span) * height}
          stroke={thresholdColor}
          strokeDasharray="2 2"
          strokeWidth={1}
          opacity={0.5}
        />
      )}
      <polyline
        points={points}
        fill="none"
        stroke={strokeColor}
        strokeWidth={1.5}
      />
      {values.map((v, i) => {
        const cx = i * stepX;
        const cy = height - ((Math.max(yMin, Math.min(yMax, v)) - yMin) / span) * height;
        const pass = passMask?.[i] === true;
        return (
          <circle
            key={i}
            cx={cx}
            cy={cy}
            r={pass ? 2.5 : 1.5}
            fill={pass ? "#34d399" : strokeColor}
            stroke={pass ? "#065f46" : "transparent"}
            strokeWidth={pass ? 0.5 : 0}
          />
        );
      })}
    </svg>
  );
}

function DimensionRow({
  title,
  points,
  anchors,
}: {
  title: string;
  points: RollingDimensionPoint[];
  anchors: RollingAnchor[];
}) {
  // 三个折线：sample_size（用 0~max 归一）/ p_bonf（0~0.2）/ delta_precision（-0.2~0.2 → 偏移 0.5 后 0~1）
  const sizes = points.map((p) => p.sample_size);
  const maxSize = Math.max(M4_THRESHOLD_N, ...sizes, 1);
  const sizeNormalized = sizes.map((s) => s / maxSize);

  const pBonfClamped = points.map((p) => Math.min(0.2, p.mcnemar_p_bonferroni));
  // 反向显示（值越低越好），所以画 0.2 - p
  const pInverted = pBonfClamped.map((p) => 0.2 - p);

  const deltaPrec = points.map((p) => p.delta_precision);

  const passMask = points.map(
    (p) =>
      p.sample_size >= M4_THRESHOLD_N
      && p.mcnemar_p_bonferroni < SIG_P
      && p.delta_precision >= SIG_DELTA_PREC,
  );

  // 最右锚点（最新）的当前态摘要
  const latest = points[points.length - 1];
  const latestPass = passMask[passMask.length - 1] ?? false;

  // 首尾时间标签
  const firstAnchor = anchors[0]?.anchor_ts;
  const lastAnchor = anchors[anchors.length - 1]?.anchor_ts;

  return (
    <div className="rounded border border-slate-700/40 bg-slate-900/40 p-3">
      <div className="flex items-center justify-between mb-2">
        <h4 className="text-[11px] font-semibold text-slate-300">{title}</h4>
        <span
          className={`px-2 py-0.5 rounded-full text-[10px] font-medium ${
            latestPass
              ? "bg-emerald-500/20 text-emerald-300"
              : "bg-slate-700/40 text-slate-400"
          }`}
        >
          {latestPass ? "✅ 当前满足切换门槛" : "⏳ 离切换还差距"}
        </span>
      </div>

      <div className="space-y-2">
        <div>
          <div className="text-[10px] text-slate-500 mb-0.5 flex items-center justify-between">
            <span>样本量（橙线 = M4 门槛 n≥{M4_THRESHOLD_N}）</span>
            <span className="font-mono text-slate-400">
              当前 {latest?.sample_size ?? 0}
            </span>
          </div>
          <MiniLine
            values={sizeNormalized}
            yMin={0}
            yMax={1}
            strokeColor="#94a3b8"
            thresholdY={M4_THRESHOLD_N / maxSize}
            passMask={passMask}
          />
        </div>

        <div>
          <div className="text-[10px] text-slate-500 mb-0.5 flex items-center justify-between">
            <span>McNemar Bonferroni p（越低越好；橙线 = 0.05 显著阈）</span>
            <span className="font-mono text-slate-400">
              当前{" "}
              {latest
                ? latest.mcnemar_p_bonferroni < 1e-4
                  ? "<0.0001"
                  : latest.mcnemar_p_bonferroni.toFixed(4)
                : "-"}
            </span>
          </div>
          {/* 倒置显示：低 p 值 = 高位 */}
          <MiniLine
            values={pInverted}
            yMin={0}
            yMax={0.2}
            strokeColor="#a78bfa"
            thresholdY={0.2 - SIG_P}
            passMask={passMask}
          />
        </div>

        <div>
          <div className="text-[10px] text-slate-500 mb-0.5 flex items-center justify-between">
            <span>Δprecision（V2-V1；橙线 = +5% 切换门槛）</span>
            <span
              className={`font-mono ${
                (latest?.delta_precision ?? 0) >= SIG_DELTA_PREC
                  ? "text-emerald-300"
                  : "text-slate-400"
              }`}
            >
              当前{" "}
              {latest
                ? `${(latest.delta_precision * 100).toFixed(1)}%`
                : "-"}
            </span>
          </div>
          <MiniLine
            values={deltaPrec}
            yMin={-0.2}
            yMax={0.2}
            strokeColor="#38bdf8"
            thresholdY={SIG_DELTA_PREC}
            passMask={passMask}
          />
        </div>
      </div>

      {firstAnchor && lastAnchor && (
        <div className="flex items-center justify-between text-[9px] text-slate-600 mt-1.5 px-1">
          <span>{fmtTs(firstAnchor)}</span>
          <span>{fmtTs(lastAnchor)}</span>
        </div>
      )}
    </div>
  );
}

export default function V1V2RollingChart({
  anchors,
  windowDays,
  stepHours,
  cacheHit,
}: {
  anchors: RollingAnchor[];
  windowDays: number;
  stepHours: number;
  cacheHit?: boolean;
}) {
  if (!anchors || anchors.length === 0) {
    return (
      <div className="rounded border border-slate-700/40 bg-slate-900/40 p-4 text-[11px] text-slate-500 text-center">
        ⏳ 历史快照不足以生成滑窗（至少需 ~{windowDays} 天的连续快照）
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-300">
          📊 14 天滑窗 · V1 vs V2 对比指标
          <span className="text-[10px] text-slate-600 ml-2 font-normal">
            （每点回看 {windowDays} 天 · 步长 {stepHours}h · 共 {anchors.length} 锚）
          </span>
        </h3>
        {cacheHit && (
          <span className="text-[9px] text-slate-600">⚡ 缓存命中</span>
        )}
      </div>

      <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
        {(["bounce_quality", "breakout_stage", "fake_break"] as V1V2Dimension[]).map(
          (dim) => (
            <DimensionRow
              key={dim}
              title={DIMENSION_TITLE[dim] ?? dim}
              points={anchors.map((a) => a[dim])}
              anchors={anchors}
            />
          ),
        )}
      </div>
    </div>
  );
}
