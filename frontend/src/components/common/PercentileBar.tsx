"use client";

interface Props {
  value: number; // 0-100
  height?: number;
}

export default function PercentileBar({ value, height = 4 }: Props) {
  const clampedValue = Math.max(0, Math.min(100, value));
  const color =
    clampedValue > 80 ? "#ef4444" :
    clampedValue > 60 ? "#f97316" :
    clampedValue > 40 ? "#eab308" :
    clampedValue > 20 ? "#22c55e" :
    "#3b82f6";

  return (
    <div className="w-full rounded-full overflow-hidden" style={{ height, backgroundColor: "#334155" }}>
      <div
        className="h-full rounded-full transition-all duration-500"
        style={{ width: `${clampedValue}%`, backgroundColor: color }}
      />
    </div>
  );
}
