"use client";

/**
 * 历史信号简表 · 倒序展示
 *
 * 列：时间 · 策略 · 方向 · horizon · 参考价 · 结算价 · 置信 · 结果 · 盈亏率
 */

import { STRATEGY_META, type ScalpSignal } from "@/lib/scalpTypes";

interface Props {
  signals: ScalpSignal[];
}

export default function SignalHistoryTable({ signals }: Props) {
  if (signals.length === 0) {
    return (
      <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 px-4 py-8 text-center text-[12px] text-slate-500">
        暂无历史信号
      </div>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800 bg-slate-900/40">
      <table className="w-full text-[11px]">
        <thead className="border-b border-slate-800 bg-slate-900/80 text-slate-400">
          <tr>
            <Th>时间</Th>
            <Th>策略</Th>
            <Th>方向</Th>
            <Th align="right">horizon</Th>
            <Th align="right">参考价</Th>
            <Th align="right">结算价</Th>
            <Th align="right">置信</Th>
            <Th>结果</Th>
            <Th align="right">盈亏</Th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s) => (
            <Row key={s.signal_id} signal={s} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Row({ signal: s }: { signal: ScalpSignal }) {
  const meta = STRATEGY_META[s.strategy];
  const isUp = s.direction === "up";
  const dirColor = isUp ? "#22c55e" : "#ef4444";
  const outcomeBadge = outcomeOf(s);
  const pnl = s.outcome === "won" ? 0.8 : s.outcome === "lost" ? -1.0 : 0;
  const pnlColor = pnl > 0 ? "#22c55e" : pnl < 0 ? "#ef4444" : "#94a3b8";

  return (
    <tr className="border-b border-slate-800/50 transition hover:bg-slate-800/40">
      <Td className="font-mono text-slate-400">{formatDateTime(s.created_at)}</Td>
      <Td>
        <span className="mr-1">{meta?.emoji}</span>
        <span className="text-slate-300">{meta?.shortCn ?? s.strategy}</span>
      </Td>
      <Td>
        <span style={{ color: dirColor }}>{isUp ? "↑ 涨" : "↓ 跌"}</span>
      </Td>
      <Td align="right" className="font-mono text-slate-400">
        {s.horizon_min}min
      </Td>
      <Td align="right" className="font-mono text-slate-200">
        ${formatPrice(s.reference_price)}
      </Td>
      <Td align="right" className="font-mono text-slate-200">
        {s.settlement_price !== null ? `$${formatPrice(s.settlement_price)}` : "—"}
      </Td>
      <Td align="right" className="font-mono text-slate-300">
        {s.confidence}
      </Td>
      <Td>
        <span
          className="rounded px-1.5 py-0.5 text-[10px]"
          style={{ background: outcomeBadge.bg, color: outcomeBadge.color }}
        >
          {outcomeBadge.text}
        </span>
      </Td>
      <Td align="right" className="font-mono" style={{ color: pnlColor }}>
        {pnl > 0 ? "+" : ""}
        {pnl.toFixed(2)}
      </Td>
    </tr>
  );
}

function Th({
  children,
  align = "left",
}: {
  children: React.ReactNode;
  align?: "left" | "right";
}) {
  return (
    <th
      className={`px-2 py-2 text-${align} font-medium uppercase tracking-wider`}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = "left",
  className,
  style,
}: {
  children: React.ReactNode;
  align?: "left" | "right";
  className?: string;
  style?: React.CSSProperties;
}) {
  return (
    <td className={`px-2 py-1.5 text-${align} ${className ?? ""}`} style={style}>
      {children}
    </td>
  );
}

function outcomeOf(s: ScalpSignal): { text: string; bg: string; color: string } {
  if (s.state === "active") return { text: "● 进行中", bg: "#0c4a6e", color: "#7dd3fc" };
  if (s.state === "expired_won") return { text: "✓ 命中", bg: "#14532d", color: "#86efac" };
  if (s.state === "expired_lost") return { text: "✗ 落空", bg: "#7f1d1d", color: "#fca5a5" };
  if (s.state === "expired_push") return { text: "= 持平", bg: "#3f3f46", color: "#d4d4d8" };
  // P0-4: cancelled 显示 shadow 结果（如已结算）
  if (s.shadow_outcome === "won") return { text: "⊘→✓ 影子赢", bg: "#1c4532", color: "#86efac" };
  if (s.shadow_outcome === "lost") return { text: "⊘→✗ 影子输", bg: "#3f1f25", color: "#fca5a5" };
  if (s.shadow_outcome === "push") return { text: "⊘→= 影子平", bg: "#3f3f46", color: "#d4d4d8" };
  return { text: "⊘ 取消", bg: "#374151", color: "#9ca3af" };
}

function formatDateTime(ts: number): string {
  const d = new Date(ts * 1000);
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  const h = String(d.getHours()).padStart(2, "0");
  const mi = String(d.getMinutes()).padStart(2, "0");
  return `${m}-${day} ${h}:${mi}`;
}

function formatPrice(p: number): string {
  if (p >= 1000) return p.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return p.toFixed(p >= 1 ? 4 : 6);
}
