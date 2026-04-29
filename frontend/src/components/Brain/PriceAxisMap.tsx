"use client";

import { useMemo } from "react";
import type { BrainPriceZone } from "@/lib/types";
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

const PADDING_TOP = 28;
const PADDING_BOTTOM = 28;
const AXIS_X = 90;
const ZONE_X = 96;
const ZONE_W = 132;

function chooseStep(range: number, atr: number): number {
  const candidates = [atr * 2, atr, atr * 0.5, range / 8].filter((x) => x > 0);
  candidates.sort((a, b) => a - b);
  for (const c of candidates) {
    const ticks = range / c;
    if (ticks >= 4 && ticks <= 12) return c;
  }
  return Math.max(atr || range / 8, 1);
}

export default function PriceAxisMap({
  zones, lastPrice, atr, coin, selectedId, onSelect,
}: Props) {
  const { vmin, vmax, height } = useMemo(() => {
    if (!zones.length) {
      const span = Math.max(atr * 8, lastPrice * 0.02, 1);
      return { vmin: lastPrice - span, vmax: lastPrice + span, height: 480 };
    }
    let lo = Math.min(lastPrice, ...zones.map((z) => z.price_low));
    let hi = Math.max(lastPrice, ...zones.map((z) => z.price_high));
    const pad = Math.max((hi - lo) * 0.06, atr || 0);
    lo -= pad;
    hi += pad;
    return { vmin: lo, vmax: hi, height: Math.max(360, zones.length * 28 + 240) };
  }, [zones, lastPrice, atr]);

  const range = Math.max(vmax - vmin, 1e-6);
  const innerH = height - PADDING_TOP - PADDING_BOTTOM;
  const yOf = (price: number) =>
    PADDING_TOP + ((vmax - price) / range) * innerH;

  const step = chooseStep(range, atr);
  const ticks: number[] = [];
  if (step > 0) {
    const start = Math.ceil(vmin / step) * step;
    for (let p = start; p <= vmax; p += step) ticks.push(p);
  }

  return (
    <div className="h-full overflow-y-auto">
      <svg
        width="100%"
        viewBox={`0 0 240 ${height}`}
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
              <line x1={AXIS_X - 4} y1={y} x2={AXIS_X} y2={y} stroke="#475569" strokeWidth={1} />
              <text
                x={AXIS_X - 8}
                y={y + 3}
                textAnchor="end"
                fontSize={9}
                fill="#64748b"
                style={{ fontFamily: "ui-monospace,SFMono-Regular,monospace" }}
              >
                {Math.round(t).toLocaleString("en-US")}
              </text>
            </g>
          );
        })}

        {zones.map((z) => {
          const yMid = yOf(z.price_mid);
          const yHi = yOf(z.price_high);
          const yLo = yOf(z.price_low);
          const h = Math.max(Math.abs(yLo - yHi), 14);
          const role = ROLE_COLORS[z.dominant_role] ?? ROLE_COLORS.other;
          const selected = selectedId === z.zone_id;
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
                fillOpacity={selected ? 0.32 : 0.16}
                stroke={role.hex}
                strokeOpacity={selected ? 1 : 0.65}
                strokeWidth={selected ? 2 : 1}
                rx={3}
              />
              <text
                x={ZONE_X + 6}
                y={yMid - 2}
                fontSize={10}
                fill="#e2e8f0"
                style={{ fontFamily: "ui-monospace,SFMono-Regular,monospace" }}
              >
                {formatPrice(z.price_mid, coin)}
              </text>
              <text
                x={ZONE_X + 6}
                y={yMid + 10}
                fontSize={9}
                fill={role.hex}
              >
                {role.label}
                <tspan fill="#64748b">{` · ${z.distance_pct >= 0 ? "+" : ""}${z.distance_pct.toFixed(2)}%`}</tspan>
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
                x2={240}
                y1={y}
                y2={y}
                stroke="#f43f5e"
                strokeWidth={1.4}
                strokeDasharray="4 3"
              />
              <rect x={2} y={y - 9} width={70} height={16} rx={3} fill="#f43f5e" fillOpacity={0.85} />
              <text
                x={6}
                y={y + 3}
                fontSize={10}
                fill="#fff"
                style={{ fontFamily: "ui-monospace,SFMono-Regular,monospace", fontWeight: 600 }}
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
