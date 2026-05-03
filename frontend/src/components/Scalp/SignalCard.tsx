"use client";

/**
 * 信号卡片 · 完整版（Step 13）
 *
 * 字段：
 *   - 顶栏：策略 emoji + 短名 + horizon + 状态徽章 + 倒计时
 *   - 主区：方向（涨↑/跌↓）+ 参考价 + 置信 + 命中预期
 *   - 因子条：5 因子横向 progress bar
 *   - 证据：高权重 evidence 列表
 *   - 操作区：取消按钮 + 详情 link
 */

import { useEffect, useMemo, useState } from "react";

import {
  REGIME_META,
  STRATEGY_META,
  type ScalpSignal,
} from "@/lib/scalpTypes";
import SampleSizeBadge from "./SampleSizeBadge";

interface SignalCardProps {
  signal: ScalpSignal;
  onCancel?: () => void;
}

export default function SignalCard({ signal, onCancel }: SignalCardProps) {
  const meta = STRATEGY_META[signal.strategy];
  const regimeMeta = REGIME_META[signal.regime];
  const isUp = signal.direction === "up";
  const dirColor = isUp ? "#22c55e" : "#ef4444";
  const dirCn = isUp ? "看涨 ↑" : "看跌 ↓";

  const remainSec = useCountdown(signal.expiry_ts);

  const stateBadge = useMemo(() => stateBadgeOf(signal), [signal]);

  return (
    <div
      className="rounded-lg border border-slate-700/60 bg-slate-900/70 p-3"
      style={{ borderLeft: `3px solid ${meta?.color ?? "#64748b"}` }}
    >
      {/* Top */}
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-base">{meta?.emoji ?? "•"}</span>
          <span className="text-[12px] font-semibold text-slate-200">
            {meta?.shortCn ?? signal.strategy}
          </span>
          <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">
            {signal.horizon_min}min
          </span>
          <span
            className="rounded px-1.5 py-0.5 text-[10px]"
            style={{ background: stateBadge.bg, color: stateBadge.color }}
          >
            {stateBadge.text}
          </span>
        </div>
        {signal.state === "active" && remainSec !== null && (
          <span className="font-mono text-[11px] text-slate-400">
            {formatRemain(remainSec)}
          </span>
        )}
      </div>

      {/* Direction + price */}
      <div className="mt-2 flex items-baseline gap-3">
        <span className="text-xl font-bold" style={{ color: dirColor }}>
          {signal.coin} {dirCn}
        </span>
        <span className="text-[13px] text-slate-300">
          @ <strong>${formatPrice(signal.reference_price)}</strong>
        </span>
      </div>

      {/* Confidence + hit prob (P0-2/P0-8: 隐藏未校准 + 强制 SampleSizeBadge) */}
      <div className="mt-2 flex items-center gap-3 text-[11px] flex-wrap">
        <span className="text-slate-400">
          置信：<span className="text-slate-100 font-mono">{signal.confidence}</span>/100
        </span>
        <span className="inline-flex items-center gap-1.5 text-slate-400">
          命中预期：
          {signal.hit_probability !== null && signal.hit_probability_source === "calibrated" ? (
            <>
              <span className="text-slate-100 font-mono">
                {Math.round(signal.hit_probability * 100)}%
              </span>
              <SampleSizeBadge n={signal.calibration_sample_size} compact />
            </>
          ) : (
            <span
              className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300"
              title="样本不足或未校准；前端策略：不显示数字以避免误导"
            >
              未校准 (N={signal.calibration_sample_size})
            </span>
          )}
        </span>
        <span className="text-slate-400">
          多周期偏置：
          <span
            className="font-mono"
            style={{ color: signal.bias_score >= 0 ? "#22c55e" : "#ef4444" }}
          >
            {signal.bias_score >= 0 ? "+" : ""}
            {signal.bias_score.toFixed(2)}
          </span>
        </span>
      </div>

      {/* Regime + signal_id */}
      <div className="mt-1.5 flex items-center justify-between text-[10px] text-slate-500">
        <span>
          Regime:{" "}
          <span style={{ color: regimeMeta?.color ?? "#64748b" }}>
            {regimeMeta?.displayCn ?? signal.regime}
          </span>
        </span>
        <span className="font-mono">
          {signal.signal_id} · {formatTime(signal.created_at)}
        </span>
      </div>

      {/* 5 因子 progress bars */}
      <div className="mt-3 space-y-1">
        <FactorBar label="核心信号" value={signal.factor_breakdown.core_signal_strength} />
        <FactorBar label="多周期对齐" value={signal.factor_breakdown.multi_tf_alignment} />
        <FactorBar label="关键位质量" value={signal.factor_breakdown.key_level_quality} />
        <FactorBar label="数据新鲜度" value={signal.factor_breakdown.data_freshness} />
        <FactorBar
          label="历史命中率"
          value={signal.factor_breakdown.historical_winrate}
          suffix={
            <SampleSizeBadge
              n={signal.factor_breakdown.historical_winrate_sample_size}
              compact
              className="ml-1"
            />
          }
        />
      </div>

      {/* Evidence */}
      {signal.evidence.length > 0 && (
        <div className="mt-3 space-y-1 border-t border-slate-800 pt-2">
          <div className="text-[10px] uppercase tracking-wide text-slate-500">证据链</div>
          <ul className="space-y-0.5 text-[11px] text-slate-300">
            {signal.evidence.slice(0, 5).map((ev, i) => (
              <li key={i}>
                <span
                  className="mr-1.5 inline-block rounded px-1 text-[10px]"
                  style={{
                    color: weightColor(ev.weight),
                    background: weightBg(ev.weight),
                  }}
                >
                  {ev.dimension}
                </span>
                {ev.observation}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* Actions */}
      {signal.state === "active" && onCancel && (
        <div className="mt-3 flex justify-end border-t border-slate-800 pt-2">
          <button
            onClick={() => {
              if (confirm("确定取消此信号？取消后将归档为 cancelled 状态")) onCancel();
            }}
            className="rounded border border-rose-700/50 bg-rose-950/30 px-2 py-1 text-[11px] text-rose-300 hover:bg-rose-900/40"
          >
            取消信号
          </button>
        </div>
      )}
    </div>
  );
}

function FactorBar({
  label,
  value,
  suffix,
}: {
  label: string;
  value: number;
  suffix?: React.ReactNode;
}) {
  const pct = Math.max(0, Math.min(100, value * 100));
  const color =
    pct >= 70 ? "#22c55e" : pct >= 50 ? "#f59e0b" : pct >= 30 ? "#94a3b8" : "#475569";
  return (
    <div className="flex items-center gap-2 text-[10px]">
      <span className="w-16 shrink-0 text-slate-400">{label}</span>
      <div className="flex-1 overflow-hidden rounded bg-slate-800">
        <div
          className="h-1.5 transition-all"
          style={{ width: `${pct}%`, background: color }}
        />
      </div>
      <span className="w-10 shrink-0 text-right font-mono text-slate-300">
        {value.toFixed(2)}
      </span>
      {suffix}
    </div>
  );
}

function useCountdown(expiryTs: number): number | null {
  const [remain, setRemain] = useState(() =>
    Math.max(0, expiryTs - Math.floor(Date.now() / 1000)),
  );
  useEffect(() => {
    const t = setInterval(() => {
      setRemain(Math.max(0, expiryTs - Math.floor(Date.now() / 1000)));
    }, 1000);
    return () => clearInterval(t);
  }, [expiryTs]);
  return remain;
}

function formatRemain(sec: number): string {
  if (sec <= 0) return "已到期";
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return `${m}m ${String(s).padStart(2, "0")}s`;
}

function formatTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function formatPrice(p: number): string {
  if (p >= 1000) return p.toLocaleString(undefined, { maximumFractionDigits: 2 });
  return p.toFixed(p >= 1 ? 4 : 6);
}

function stateBadgeOf(signal: ScalpSignal): { text: string; bg: string; color: string } {
  if (signal.state === "active") {
    return { text: "● 进行中", bg: "#0c4a6e", color: "#7dd3fc" };
  }
  if (signal.state === "expired_won") {
    return { text: "✓ 命中", bg: "#14532d", color: "#86efac" };
  }
  if (signal.state === "expired_lost") {
    return { text: "✗ 落空", bg: "#7f1d1d", color: "#fca5a5" };
  }
  if (signal.state === "expired_push") {
    return { text: "= 持平", bg: "#3f3f46", color: "#d4d4d8" };
  }
  // P0-4: cancelled · 显示 invalidation_kind
  const kindCn: Record<string, string> = {
    regime_flip: "Regime 翻转",
    data_stale: "数据 stale",
    blackswan: "黑天鹅",
    manual: "手动",
    conflict: "冲突",
  };
  const kindText = signal.invalidation_kind ? `（${kindCn[signal.invalidation_kind] ?? signal.invalidation_kind}）` : "";
  return { text: `⊘ 已取消${kindText}`, bg: "#374151", color: "#9ca3af" };
}

function weightColor(w: string): string {
  return w === "high" ? "#dc2626" : w === "medium" ? "#d97706" : "#94a3b8";
}

function weightBg(w: string): string {
  return w === "high"
    ? "rgba(127,29,29,0.4)"
    : w === "medium"
    ? "rgba(120,53,15,0.4)"
    : "rgba(51,65,85,0.4)";
}
