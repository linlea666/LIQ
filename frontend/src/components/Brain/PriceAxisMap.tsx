"use client";

/**
 * 价格轴地图（W4-T1 阶段 3 升级）
 * ------------------------------------------------------------
 * 修复用户反馈"价格轴显示太密集看的眼花缭乱"。
 *
 * 改动：
 *   1. 重要度过滤：按距现价 + dominant_role 过滤
 *      - 距 ≤ 1%：全部显示（短线核心关注区）
 *      - 1-3%：non-other 全显，other 只在与上一个保留 zone 间距 > 0.4% 时显示
 *      - > 3%：只显示强角色（spot_defense / liquidation_magnet / contested）
 *   2. 同角色相邻合并：mid 间距 < 0.3% 当前价 且 dominant_role 相同 → 合并
 *   3. 文字垂直偏移 + 引导线：贪心防碰撞算法（minGap=22px）
 *   4. 角色颜色梯度增强：弱角色透明度 ↓ / 强角色 ↑
 *   5. 选中 zone 永不被过滤（避免点击后消失）
 */

import { useMemo } from "react";
import type { BrainPriceZone, BrainDominantRole } from "@/lib/types";
import { ROLE_COLORS } from "./types";
import { formatPrice } from "@/lib/format";

interface Props {
  zones: BrainPriceZone[];
  lastPrice: number;
  atr: number;
  coin: string;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

const VIEW_W = 300;
const PADDING_TOP = 28;
const PADDING_BOTTOM = 28;
const AXIS_X = 110;
const ZONE_X = 116;
const ZONE_W = 178;
const LABEL_MIN_GAP = 22; // 标签最小垂直间距（含 1 行价格 + 1 行角色文字）

// 强角色（远距离也保留）
const STRONG_ROLES: ReadonlySet<BrainDominantRole> = new Set([
  "spot_defense",
  "liquidation_magnet",
  "contested",
]);

// 角色重要度排序（合并冲突时取重要度更高者作为代表）
const ROLE_PRIORITY: Record<BrainDominantRole, number> = {
  spot_defense: 5,
  liquidation_magnet: 5,
  contested: 4,
  futures_target: 3,
  key_level_only: 2,
  other: 1,
};

// 角色透明度梯度（越强越显眼）
const ROLE_FILL_OPACITY: Record<BrainDominantRole, number> = {
  spot_defense: 0.26,
  liquidation_magnet: 0.26,
  contested: 0.24,
  futures_target: 0.18,
  key_level_only: 0.14,
  other: 0.09,
};

function chooseStep(range: number, atr: number): number {
  const candidates = [atr * 2, atr, atr * 0.5, range / 8].filter((x) => x > 0);
  candidates.sort((a, b) => a - b);
  for (const c of candidates) {
    const ticks = range / c;
    if (ticks >= 4 && ticks <= 12) return c;
  }
  return Math.max(atr || range / 8, 1);
}

// ─────────────────────────────────────────────────────────────────
// 过滤 + 合并：把屏幕上的 zone 数量降到合理水平
// ─────────────────────────────────────────────────────────────────
interface MergedZone extends BrainPriceZone {
  /** 该合并 zone 包含的原始 zone_id（保留供选中匹配） */
  member_ids: string[];
}

function filterAndMerge(
  zones: BrainPriceZone[],
  lastPrice: number,
  selectedId: string | null,
): MergedZone[] {
  if (!zones.length) return [];

  // 阶段 3.1 过滤：距离 + 角色双维度
  const minSeparationOther = lastPrice * 0.004; // other 类至少 0.4% 间隔
  const filtered: BrainPriceZone[] = [];
  // 选中 zone 永不过滤
  const sorted = [...zones].sort(
    (a, b) => Math.abs(a.distance_pct) - Math.abs(b.distance_pct),
  );
  let lastOtherPrice = -Infinity;
  for (const z of sorted) {
    const isSelected = z.zone_id === selectedId;
    const dist = Math.abs(z.distance_pct);
    if (isSelected) {
      filtered.push(z);
      continue;
    }
    if (dist > 3.0 && !STRONG_ROLES.has(z.dominant_role)) continue;
    if (z.dominant_role === "other") {
      if (dist > 1.0 && Math.abs(z.price_mid - lastOtherPrice) < minSeparationOther) continue;
      lastOtherPrice = z.price_mid;
    }
    filtered.push(z);
  }

  // 阶段 3.1 合并：同角色相邻 + mid 间距 < 0.3% 当前价 → 合并代表
  const sortByPrice = [...filtered].sort((a, b) => a.price_mid - b.price_mid);
  const merged: MergedZone[] = [];
  const mergeTol = lastPrice * 0.003;
  for (const z of sortByPrice) {
    const last = merged[merged.length - 1];
    if (
      last &&
      last.dominant_role === z.dominant_role &&
      Math.abs(z.price_mid - last.price_mid) < mergeTol &&
      z.zone_id !== selectedId &&
      !last.member_ids.includes(selectedId ?? "__none__")
    ) {
      // 合并到 last：取更"代表性"的 zone 作为主显示，并集 price_low/high
      const incomingPriority =
        Math.max(z.support_trust, z.resistance_trust) +
        ROLE_PRIORITY[z.dominant_role] * 0.01;
      const lastPriority =
        Math.max(last.support_trust, last.resistance_trust) +
        ROLE_PRIORITY[last.dominant_role] * 0.01;
      const repr = incomingPriority > lastPriority ? z : last;
      const merged_z: MergedZone = {
        ...repr,
        price_low: Math.min(last.price_low, z.price_low),
        price_high: Math.max(last.price_high, z.price_high),
        price_mid: (last.price_mid + z.price_mid) / 2,
        member_ids: [...last.member_ids, z.zone_id],
      };
      merged[merged.length - 1] = merged_z;
    } else {
      merged.push({ ...z, member_ids: [z.zone_id] });
    }
  }
  return merged;
}

// ─────────────────────────────────────────────────────────────────
// 文字垂直偏移防碰撞（贪心算法 + 引导线）
// ─────────────────────────────────────────────────────────────────
interface ZoneLayout {
  z: MergedZone;
  yMid: number; // zone box 中心 y
  yLabel: number; // 文字 y（可能 != yMid）
}

function layoutLabels(
  zones: MergedZone[],
  yOf: (price: number) => number,
  innerH: number,
): ZoneLayout[] {
  const items = zones.map((z) => ({ z, yMid: yOf(z.price_mid), yLabel: yOf(z.price_mid) }));
  // 按 yMid 升序贪心排版，从上到下推
  items.sort((a, b) => a.yMid - b.yMid);
  for (let i = 1; i < items.length; i++) {
    const prevEnd = items[i - 1].yLabel + LABEL_MIN_GAP;
    if (items[i].yLabel < prevEnd) items[i].yLabel = prevEnd;
  }
  // 防止最后一个标签溢出底部，反向推
  const maxY = innerH + PADDING_TOP - 4;
  for (let i = items.length - 1; i > 0; i--) {
    if (items[i].yLabel > maxY) items[i].yLabel = maxY;
    const prevMaxLabel = items[i].yLabel - LABEL_MIN_GAP;
    if (items[i - 1].yLabel > prevMaxLabel) items[i - 1].yLabel = prevMaxLabel;
  }
  return items;
}

// ─────────────────────────────────────────────────────────────────
// 主组件
// ─────────────────────────────────────────────────────────────────
export default function PriceAxisMap({
  zones, lastPrice, atr, coin, selectedId, onSelect,
}: Props) {
  const merged = useMemo(
    () => filterAndMerge(zones, lastPrice, selectedId),
    [zones, lastPrice, selectedId],
  );
  const filteredCount = merged.length;
  const rawCount = zones.length;

  const { vmin, vmax, height } = useMemo(() => {
    if (!merged.length) {
      const span = Math.max(atr * 8, lastPrice * 0.02, 1);
      return { vmin: lastPrice - span, vmax: lastPrice + span, height: 480 };
    }
    let lo = Math.min(lastPrice, ...merged.map((z) => z.price_low));
    let hi = Math.max(lastPrice, ...merged.map((z) => z.price_high));
    const pad = Math.max((hi - lo) * 0.06, atr || 0);
    lo -= pad;
    hi += pad;
    return { vmin: lo, vmax: hi, height: Math.max(360, merged.length * 28 + 240) };
  }, [merged, lastPrice, atr]);

  const range = Math.max(vmax - vmin, 1e-6);
  const innerH = height - PADDING_TOP - PADDING_BOTTOM;
  const yOf = (price: number) =>
    PADDING_TOP + ((vmax - price) / range) * innerH;

  const layouts = useMemo(
    () => layoutLabels(merged, yOf, innerH),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [merged, vmin, vmax, height],
  );

  const step = chooseStep(range, atr);
  const ticks: number[] = [];
  if (step > 0) {
    const start = Math.ceil(vmin / step) * step;
    for (let p = start; p <= vmax; p += step) ticks.push(p);
  }

  return (
    <div className="h-full overflow-y-auto">
      {/* 阶段 3 顶栏：显示密度信息（让用户知道有过滤+合并发生） */}
      {filteredCount < rawCount && (
        <div className="px-2 pt-1 text-[9px] tabular-nums text-slate-500">
          已合并 / 折叠 {rawCount - filteredCount} 个低重要度区（&gt; 3% 仅强角色 · 同色相邻合并）
        </div>
      )}
      <svg
        width="100%"
        viewBox={`0 0 ${VIEW_W} ${height}`}
        preserveAspectRatio="xMinYMin meet"
        className="block"
      >
        <defs>
          <linearGradient id="axisLine" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="#1e293b" />
            <stop offset="50%" stopColor="#334155" />
            <stop offset="100%" stopColor="#1e293b" />
          </linearGradient>
        </defs>
        <line
          x1={AXIS_X}
          y1={PADDING_TOP}
          x2={AXIS_X}
          y2={height - PADDING_BOTTOM}
          stroke="url(#axisLine)"
          strokeWidth={2}
        />
        {ticks.map((t) => {
          const y = yOf(t);
          return (
            <g key={t}>
              <line x1={AXIS_X - 5} y1={y} x2={AXIS_X} y2={y} stroke="#475569" strokeWidth={1} />
              <text
                x={AXIS_X - 9}
                y={y + 4}
                textAnchor="end"
                fontSize={11}
                fill="#94a3b8"
                style={{ fontFamily: "ui-monospace,SFMono-Regular,monospace" }}
              >
                {Math.round(t).toLocaleString("en-US")}
              </text>
            </g>
          );
        })}

        {layouts.map(({ z, yMid, yLabel }) => {
          const yHi = yOf(z.price_high);
          const yLo = yOf(z.price_low);
          const h = Math.max(Math.abs(yLo - yHi), 14);
          const role = ROLE_COLORS[z.dominant_role] ?? ROLE_COLORS.other;
          const fillOpacity = ROLE_FILL_OPACITY[z.dominant_role] ?? 0.16;
          const selected = z.member_ids.includes(selectedId ?? "__none__");
          const labelOffsetY = yLabel - yMid; // 用于决定是否需要画引导线
          const needGuide = Math.abs(labelOffsetY) > 4;
          return (
            <g
              key={z.zone_id}
              onClick={() => onSelect(z.zone_id)}
              style={{ cursor: "pointer" }}
            >
              <line
                x1={AXIS_X}
                y1={yMid}
                x2={ZONE_X}
                y2={yMid}
                stroke={role.hex}
                strokeWidth={1.2}
                opacity={0.65}
              />
              <rect
                x={ZONE_X}
                y={yHi}
                width={ZONE_W}
                height={h}
                fill={role.hex}
                fillOpacity={selected ? fillOpacity * 1.8 : fillOpacity}
                stroke={role.hex}
                strokeOpacity={selected ? 1 : 0.55}
                strokeWidth={selected ? 2 : 1}
                rx={3}
              />
              {needGuide && (
                <line
                  x1={ZONE_X + 4}
                  y1={yMid}
                  x2={ZONE_X + 4}
                  y2={yLabel}
                  stroke={role.hex}
                  strokeWidth={0.8}
                  strokeDasharray="2 2"
                  opacity={0.7}
                />
              )}
              <text
                x={ZONE_X + 8}
                y={yLabel - 3}
                fontSize={12}
                fontWeight={600}
                fill="#f1f5f9"
                style={{ fontFamily: "ui-monospace,SFMono-Regular,monospace" }}
              >
                {formatPrice(z.price_mid, coin)}
                {z.member_ids.length > 1 && (
                  <tspan fill="#94a3b8" fontSize={10}>
                    {` ×${z.member_ids.length}`}
                  </tspan>
                )}
              </text>
              <text
                x={ZONE_X + 8}
                y={yLabel + 11}
                fontSize={11}
                fill={role.hex}
                fontWeight={500}
              >
                {role.label}
                <tspan fill="#94a3b8" fontWeight={400}>
                  {` · ${z.distance_pct >= 0 ? "+" : ""}${z.distance_pct.toFixed(2)}%`}
                </tspan>
              </text>
            </g>
          );
        })}

        {/* current price line */}
        {(() => {
          const y = yOf(lastPrice);
          return (
            <g>
              <line
                x1={0}
                x2={VIEW_W}
                y1={y}
                y2={y}
                stroke="#f43f5e"
                strokeWidth={1.5}
                strokeDasharray="5 3"
              />
              <rect x={2} y={y - 10} width={92} height={20} rx={4} fill="#f43f5e" fillOpacity={0.92} />
              <text
                x={8}
                y={y + 4}
                fontSize={12}
                fill="#fff"
                style={{ fontFamily: "ui-monospace,SFMono-Regular,monospace", fontWeight: 700 }}
              >
                {formatPrice(lastPrice, coin)}
              </text>
            </g>
          );
        })()}
      </svg>
    </div>
  );
}
