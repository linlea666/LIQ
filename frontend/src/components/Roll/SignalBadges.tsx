"use client";

/**
 * 滚仓信号徽章组件集合
 *
 * 组件：
 *   - ActionBadge          —— add / reduce / close / hold / move_sl
 *   - UrgencyBadge         —— info / attention / urgent
 *   - IntensityBadge       —— full / half / small / reject
 *   - DataQualityBadge     —— ok / partial / insufficient
 *   - ConfidenceBar        —— 0~100 置信度水平条
 *   - ReasonChip           —— supporting/blocking 中的单条 SignalRef 小徽章
 */

import type {
  AddIntensity,
  RollAction,
  RollSignal,
  SignalRef,
  Urgency,
} from "@/lib/rollTypes";

// ── ActionBadge ──────────────────────────────────────

const ACTION_LABEL: Record<RollAction, string> = {
  add: "建议加仓",
  reduce: "建议减仓",
  close: "建议离场",
  move_sl: "移动止损",
  hold: "持有观察",
};

const ACTION_TONE: Record<RollAction, string> = {
  add: "border-emerald-600/60 bg-emerald-900/40 text-emerald-200",
  reduce: "border-rose-600/60 bg-rose-900/40 text-rose-200",
  close: "border-rose-500/70 bg-rose-900/60 text-rose-100",
  move_sl: "border-sky-600/60 bg-sky-900/40 text-sky-200",
  hold: "border-slate-700 bg-slate-800/70 text-slate-300",
};

export function ActionBadge({ action }: { action: RollAction }) {
  return (
    <span
      className={[
        "inline-flex items-center rounded-md border px-2 py-0.5 text-[12px] font-medium",
        ACTION_TONE[action],
      ].join(" ")}
    >
      {ACTION_LABEL[action]}
    </span>
  );
}

// ── UrgencyBadge ─────────────────────────────────────

const URGENCY_LABEL: Record<Urgency, string> = {
  info: "常规",
  attention: "需关注",
  urgent: "紧急",
};

const URGENCY_TONE: Record<Urgency, string> = {
  info: "bg-slate-800 text-slate-300",
  attention: "bg-amber-900/50 text-amber-200",
  urgent: "bg-rose-800/70 text-rose-100",
};

export function UrgencyBadge({ urgency }: { urgency: Urgency }) {
  return (
    <span
      className={[
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide",
        URGENCY_TONE[urgency],
      ].join(" ")}
    >
      {URGENCY_LABEL[urgency]}
    </span>
  );
}

// ── IntensityBadge ───────────────────────────────────

const INTENSITY_LABEL: Record<AddIntensity, string> = {
  full: "full · 满量",
  half: "half · 半量",
  small: "small · 轻量",
  reject: "reject · 拒绝",
};

const INTENSITY_TONE: Record<AddIntensity, string> = {
  full: "bg-emerald-800/60 text-emerald-100",
  half: "bg-emerald-900/50 text-emerald-200",
  small: "bg-amber-900/50 text-amber-200",
  reject: "bg-slate-800 text-slate-400",
};

export function IntensityBadge({ intensity }: { intensity: AddIntensity }) {
  return (
    <span
      className={[
        "inline-flex items-center rounded px-1.5 py-0.5 font-mono text-[10px]",
        INTENSITY_TONE[intensity],
      ].join(" ")}
    >
      {INTENSITY_LABEL[intensity]}
    </span>
  );
}

// ── DataQualityBadge ────────────────────────────────

const QUALITY_TONE: Record<RollSignal["data_quality"], string> = {
  ok: "bg-emerald-950/60 text-emerald-300",
  partial: "bg-amber-950/60 text-amber-300",
  insufficient: "bg-rose-950/60 text-rose-300",
};

const QUALITY_LABEL: Record<RollSignal["data_quality"], string> = {
  ok: "数据完整",
  partial: "部分缺失",
  insufficient: "数据不足",
};

export function DataQualityBadge({
  quality,
  missing,
}: {
  quality: RollSignal["data_quality"];
  missing?: string[];
}) {
  const title = missing?.length
    ? `缺失：${missing.join(", ")}`
    : undefined;
  return (
    <span
      title={title}
      className={[
        "inline-flex items-center rounded px-1.5 py-0.5 text-[10px]",
        QUALITY_TONE[quality],
      ].join(" ")}
    >
      {QUALITY_LABEL[quality]}
    </span>
  );
}

// ── ConfidenceBar ───────────────────────────────────

export function ConfidenceBar({
  score,
  thresholds,
  variant = "add",
}: {
  score: number;
  thresholds?: { full: number; half: number; small?: number };
  variant?: "add" | "reduce";
}) {
  const pct = Math.max(0, Math.min(100, score));
  const fgTone =
    variant === "reduce"
      ? "from-rose-400 to-rose-600"
      : "from-emerald-400 to-emerald-600";

  const markers: { at: number; label: string }[] = [];
  if (thresholds) {
    if (thresholds.small !== undefined) {
      markers.push({ at: thresholds.small, label: "small" });
    }
    markers.push({ at: thresholds.half, label: "half" });
    markers.push({ at: thresholds.full, label: "full" });
  }

  return (
    <div className="flex flex-col gap-1">
      <div className="flex items-baseline justify-between text-[10px] text-slate-400">
        <span>{variant === "reduce" ? "减仓置信度" : "加仓置信度"}</span>
        <span className="font-mono text-slate-200">{score.toFixed(1)}</span>
      </div>
      <div className="relative h-2 w-full overflow-hidden rounded-full bg-slate-800">
        <div
          className={`absolute inset-y-0 left-0 bg-gradient-to-r ${fgTone}`}
          style={{ width: `${pct}%` }}
        />
        {markers.map((m, i) => (
          <div
            key={i}
            className="absolute top-0 h-full w-px bg-slate-500/80"
            style={{ left: `${Math.min(100, m.at)}%` }}
            title={`${m.label} ${m.at}`}
          />
        ))}
      </div>
      {markers.length > 0 && (
        <div className="relative h-3 text-[9px] text-slate-500">
          {markers.map((m, i) => (
            <span
              key={i}
              className="absolute -translate-x-1/2"
              style={{ left: `${Math.min(100, m.at)}%` }}
            >
              {m.label}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

// ── ReasonChip ──────────────────────────────────────

export function ReasonChip({
  signal: sig,
  tone,
}: {
  signal: SignalRef;
  tone: "support" | "block";
}) {
  const cls =
    tone === "support"
      ? "border-emerald-700/50 bg-emerald-950/40 text-emerald-200"
      : "border-rose-700/50 bg-rose-950/40 text-rose-200";
  const sign = sig.weight > 0 ? "+" : "";
  return (
    <div
      className={[
        "flex w-full items-start justify-between gap-2 rounded border px-2 py-1 text-[11px]",
        cls,
      ].join(" ")}
      title={sig.detail}
    >
      <div className="min-w-0">
        <div className="truncate font-medium">{sig.source}</div>
        <div className="mt-0.5 truncate text-[10px] opacity-80">{sig.read}</div>
      </div>
      <span className="shrink-0 font-mono text-[11px]">
        {sign}
        {sig.weight.toFixed(1)}
      </span>
    </div>
  );
}
