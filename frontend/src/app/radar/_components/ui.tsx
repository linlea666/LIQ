"use client";

/**
 * 雷达控制台 · 共享展示组件
 *
 * 这里的组件都在执行同一条界面纪律：**任何数值旁边都要能看出它有多可信**。
 *
 * 一个只显示"机会 87"的卡片会让人立刻行动；
 * 同一个卡片如果同时显示"数据完整度 31%"，人会先去查为什么。
 * 界面设计在这里不是美观问题，而是风控问题。
 */

import Link from "next/link";
import type { ReactNode } from "react";

import type { RadarScores, RadarState, RadarToken } from "@/lib/radarTypes";

// ── 状态 ──────────────────────────────────────────────────────────────────

const STATE_STYLE: Record<RadarState, { label: string; cls: string }> = {
  DISCOVERED: { label: "已发现", cls: "border-slate-700 bg-slate-900/60 text-slate-400" },
  WATCHING: { label: "观察中", cls: "border-sky-800 bg-sky-950/40 text-sky-300" },
  S0: { label: "S0 苗头", cls: "border-cyan-800 bg-cyan-950/40 text-cyan-300" },
  S1: { label: "S1 成形", cls: "border-emerald-700 bg-emerald-950/40 text-emerald-300" },
  S2: { label: "S2 确认", cls: "border-emerald-500 bg-emerald-900/50 text-emerald-200" },
  MOMENTUM: { label: "动量", cls: "border-amber-600 bg-amber-950/40 text-amber-300" },
  DISTRIBUTION: { label: "派发中", cls: "border-orange-700 bg-orange-950/40 text-orange-300" },
  DORMANT: { label: "沉寂", cls: "border-slate-700 bg-slate-900/60 text-slate-500" },
  DEAD: { label: "已死", cls: "border-slate-800 bg-slate-950/60 text-slate-600" },
  BLOCKED: { label: "已拦截", cls: "border-rose-800 bg-rose-950/40 text-rose-300" },
};

export function StateBadge({ state }: { state: RadarState | string }) {
  const style = STATE_STYLE[state as RadarState] ?? {
    label: state,
    cls: "border-slate-700 bg-slate-900/60 text-slate-400",
  };
  return (
    <span className={`rounded border px-1.5 py-0.5 text-[10px] whitespace-nowrap ${style.cls}`}>
      {style.label}
    </span>
  );
}

// ── 评分 ──────────────────────────────────────────────────────────────────

function scoreColor(value: number, invert: boolean): string {
  const v = invert ? 100 - value : value;
  if (v >= 75) return "bg-emerald-500";
  if (v >= 55) return "bg-lime-500";
  if (v >= 35) return "bg-amber-500";
  return "bg-rose-500";
}

export function ScoreBar({
  label,
  value,
  invert = false,
  hint,
}: {
  label: string;
  value: number;
  /** rug_risk / distribution 是"越低越好"，颜色必须反过来。 */
  invert?: boolean;
  hint?: string;
}) {
  return (
    <div className="flex items-center gap-2" title={hint}>
      <span className="w-14 shrink-0 text-[10px] text-slate-500">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-800">
        <div
          className={`h-full rounded-full ${scoreColor(value, invert)}`}
          style={{ width: `${Math.max(0, Math.min(100, value))}%` }}
        />
      </div>
      <span className="w-8 shrink-0 text-right text-[11px] tabular-nums text-slate-300">
        {value.toFixed(0)}
      </span>
    </div>
  );
}

/**
 * 五维评分整体展示。
 *
 * 刻意不提供"只显示机会分"的选项：五个维度是一个判断的完整表述，
 * 拆开来看每一个都会误导。
 */
export function ScorePanel({ scores }: { scores: RadarScores }) {
  return (
    <div className="space-y-1">
      <ScoreBar label="机会" value={scores.opportunity} hint="上涨潜力的综合评估" />
      <ScoreBar
        label="置信"
        value={scores.confidence}
        hint="多少个独立信号互相印证——低置信意味着结论建立在单一来源上"
      />
      <ScoreBar
        label="完整度"
        value={scores.data_quality}
        hint="我们拿到了多少字段、有多新。低完整度下所有其他分数都要打折看"
      />
      <ScoreBar label="跑路风险" value={scores.rug_risk} invert hint="越低越好" />
      <ScoreBar label="派发度" value={scores.distribution} invert hint="越低越好" />
    </div>
  );
}

// ── 数据可信度标注 ────────────────────────────────────────────────────────

export function QualityFlag({ token }: { token: RadarToken }) {
  const flags: ReactNode[] = [];
  if (token.quality_degraded) {
    flags.push(
      <span
        key="q"
        title="部分数据组已过期或缺失，评分基于不完整输入"
        className="rounded border border-amber-700 bg-amber-950/40 px-1.5 py-0.5 text-[10px] text-amber-300"
      >
        数据降级
      </span>,
    );
  }
  if (token.mc_source === "computed") {
    flags.push(
      <span
        key="mc"
        title="市值是我们用供应量×价格推算的，接口未直接提供；供应量口径差异可能让它偏离真实值数倍"
        className="rounded border border-slate-700 bg-slate-900/60 px-1.5 py-0.5 text-[10px] text-slate-400"
      >
        市值推算
      </span>,
    );
  }
  if (!token.risk.audit_checked) {
    flags.push(
      <span
        key="a"
        title="尚未查询合约审计——注意这不等于审计通过"
        className="rounded border border-slate-700 bg-slate-900/60 px-1.5 py-0.5 text-[10px] text-slate-400"
      >
        未审计查询
      </span>,
    );
  }
  if (token.risk.gate_blocked) {
    flags.push(
      <span
        key="g"
        title={token.risk.gate_reasons.join("；")}
        className="rounded border border-rose-700 bg-rose-950/40 px-1.5 py-0.5 text-[10px] text-rose-300"
      >
        风险门拦截
      </span>,
    );
  }
  if (!flags.length) return null;
  return <div className="flex flex-wrap gap-1">{flags}</div>;
}

// ── 通用容器 ──────────────────────────────────────────────────────────────

export function Card({
  title,
  extra,
  children,
  className = "",
}: {
  title?: ReactNode;
  extra?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section
      className={`rounded-lg border border-slate-800 bg-slate-900/50 p-3 ${className}`}
    >
      {(title || extra) && (
        <header className="mb-2 flex items-center justify-between gap-2">
          <h2 className="text-[13px] font-semibold text-slate-200">{title}</h2>
          {extra}
        </header>
      )}
      {children}
    </section>
  );
}

export function Stat({
  label,
  value,
  tone = "default",
  hint,
}: {
  label: string;
  value: ReactNode;
  tone?: "default" | "good" | "warn" | "bad";
  hint?: string;
}) {
  const toneCls = {
    default: "text-slate-200",
    good: "text-emerald-300",
    warn: "text-amber-300",
    bad: "text-rose-300",
  }[tone];
  return (
    <div className="rounded border border-slate-800 bg-slate-950/40 px-2.5 py-2" title={hint}>
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`mt-0.5 text-[15px] font-semibold tabular-nums ${toneCls}`}>{value}</div>
    </div>
  );
}

export function Empty({ text = "暂无数据" }: { text?: string }) {
  return (
    <div className="py-8 text-center text-[12px] text-slate-600">{text}</div>
  );
}

export function ErrorBanner({ error, onRetry }: { error: string; onRetry?: () => void }) {
  return (
    <div className="flex items-center justify-between gap-3 rounded-md border border-rose-600/40 bg-rose-950/40 px-3 py-2 text-[12px] text-rose-200">
      <span>⚠ 雷达服务无响应：{error}</span>
      {onRetry && (
        <button
          onClick={onRetry}
          className="rounded bg-rose-800/50 px-2 py-0.5 text-[11px] hover:bg-rose-700/70"
        >
          重试
        </button>
      )}
    </div>
  );
}

export function TokenLink({
  chainId,
  address,
  symbol,
  className = "",
}: {
  chainId: string;
  address: string;
  symbol?: string | null;
  className?: string;
}) {
  return (
    <Link
      href={`/radar/token/${encodeURIComponent(chainId)}/${encodeURIComponent(address)}`}
      className={`text-slate-100 hover:text-sky-300 hover:underline ${className}`}
      title={address}
    >
      {symbol || `${address.slice(0, 6)}…${address.slice(-4)}`}
    </Link>
  );
}

// ── 格式化 ────────────────────────────────────────────────────────────────

export const CHAIN_NAME: Record<string, string> = { "56": "BSC", CT_501: "Solana" };

/** null 显示为"—"而不是 0：区分"没有"和"不知道"。 */
export function num(value: number | null | undefined, digits = 0, suffix = ""): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  return value.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }) + suffix;
}

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e9) return `$${(value / 1e9).toFixed(2)}B`;
  if (abs >= 1e6) return `$${(value / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${value.toFixed(2)}`;
}

export function price(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  if (value === 0) return "$0";
  if (value < 0.0001) return `$${value.toExponential(2)}`;
  if (value < 1) return `$${value.toPrecision(4)}`;
  return `$${value.toFixed(4)}`;
}

export function pct(value: number | null | undefined, digits = 1): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}秒`;
  if (s < 3600) return `${Math.floor(s / 60)}分`;
  if (s < 86400) return `${Math.floor(s / 3600)}时${Math.floor((s % 3600) / 60)}分`;
  return `${Math.floor(s / 86400)}天${Math.floor((s % 86400) / 3600)}时`;
}

export function clock(ms: number | null | undefined): string {
  if (!ms) return "—";
  return new Date(ms).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export function ago(ms: number | null | undefined): string {
  if (!ms) return "—";
  return duration((Date.now() - ms) / 1000) + "前";
}
