"use client";

import { useMemo, useState } from "react";
import type { BrainEvent } from "@/lib/types";

interface Props {
  events: BrainEvent[];
  hoverZoneId: string | null;
  onHoverZone?: (zoneId: string | null) => void;
}

const LAYER_TONE: Record<string, string> = {
  spot: "#10b981",
  futures: "#f59e0b",
  liquidation: "#a78bfa",
  key_level: "#60a5fa",
  system: "#64748b",
};

const LAYER_CN: Record<string, string> = {
  spot: "现货",
  futures: "合约",
  liquidation: "清算",
  key_level: "关键位",
  system: "系统",
};

function fmtTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

export default function EventTimeline({ events, hoverZoneId, onHoverZone }: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const sorted = useMemo(
    () => [...events].sort((a, b) => a.ts - b.ts),
    [events],
  );

  if (sorted.length === 0) {
    return (
      <div className="flex h-full items-center justify-center text-[10px] text-slate-600">
        近期暂无墙事件
      </div>
    );
  }

  const tMin = sorted[0].ts;
  const tMax = sorted[sorted.length - 1].ts;
  const span = Math.max(tMax - tMin, 1);

  return (
    <div className="flex h-full flex-col px-3 pt-1.5 pb-1">
      <div className="flex items-baseline justify-between">
        <h4 className="text-[10px] uppercase tracking-wider text-slate-500">
          事件流（近 30min）
        </h4>
        <span className="text-[10px] text-slate-600">{sorted.length} 条</span>
      </div>

      <div className="relative mt-1 flex-1">
        <div className="absolute inset-x-0 top-1/2 h-px bg-slate-800" />
        <div className="absolute left-0 top-1/2 -translate-y-1/2 text-[9px] text-slate-600">
          {fmtTime(tMin)}
        </div>
        <div className="absolute right-0 top-1/2 -translate-y-1/2 text-[9px] text-slate-600">
          {fmtTime(tMax)}
        </div>
        <div className="relative h-full">
          {sorted.map((e, i) => {
            const x = ((e.ts - tMin) / span) * 100;
            const tone = LAYER_TONE[e.layer] ?? "#64748b";
            const linked = hoverZoneId === e.zone_id;
            const active = hoverIdx === i || linked;
            return (
              <button
                key={`${e.ts}-${i}`}
                type="button"
                onMouseEnter={() => {
                  setHoverIdx(i);
                  if (e.zone_id) onHoverZone?.(e.zone_id);
                }}
                onMouseLeave={() => {
                  setHoverIdx(null);
                  onHoverZone?.(null);
                }}
                className="absolute top-1/2 -translate-x-1/2 -translate-y-1/2"
                style={{ left: `${x}%` }}
              >
                <span
                  className="block rounded-full transition"
                  style={{
                    width: active ? 12 : 7,
                    height: active ? 12 : 7,
                    background: tone,
                    boxShadow: active ? `0 0 8px ${tone}` : "none",
                  }}
                />
                {active && (
                  <div className="absolute left-1/2 top-4 z-10 w-56 -translate-x-1/2 rounded border border-slate-700 bg-slate-900 p-1.5 text-[10px] text-slate-300 shadow-xl">
                    <div className="flex items-center justify-between">
                      <span className="text-slate-500">
                        {LAYER_CN[e.layer] ?? e.layer} · {fmtTime(e.ts)}
                      </span>
                      {e.price_mid > 0 && (
                        <span className="font-mono tabular-nums">
                          {e.price_mid.toFixed(0)}
                        </span>
                      )}
                    </div>
                    <p className="mt-0.5 leading-snug text-slate-200">{e.message}</p>
                    {e.zone_id && (
                      <p className="mt-0.5 truncate font-mono text-[9px] text-slate-600">
                        zone: {e.zone_id}
                      </p>
                    )}
                  </div>
                )}
              </button>
            );
          })}
        </div>
      </div>
    </div>
  );
}
