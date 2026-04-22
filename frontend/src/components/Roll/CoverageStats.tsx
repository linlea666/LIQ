"use client";

/**
 * CoverageStats —— 复盘页顶部的聚合指标卡片
 *
 * 展示三大块：
 *   1. 生命周期与 P&L（时长 / 初始保证金 / 已实现 PnL 绝对值+百分比 / 峰值杠杆）
 *   2. 覆盖率（overall + 四种 action 各自，avg follow delay）
 *   3. 行为指标（adds / reduces / sl_moves / overrides / gate_blocks）
 *
 * 设计原则：
 *   - 以数据密度为优先，不堆叠动画；样本为 0 时显示 "—" 而非 0% 误导用户
 *   - 覆盖率条：绿 ≥ 60%，琥珀 30-60%，红 < 30%
 */

import type { ReplayStats } from "@/lib/rollTypes";

function formatDuration(sec: number): string {
  if (!sec || sec <= 0) return "—";
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  if (d > 0) return `${d}天 ${h}小时`;
  if (h > 0) return `${h}时 ${m}分`;
  return `${m} 分钟`;
}

function formatPct(x: number | null, digits = 0): string {
  if (x === null || x === undefined || Number.isNaN(x)) return "—";
  return `${(x * 100).toFixed(digits)}%`;
}

function formatSignedPct(x: number, digits = 2): string {
  const sign = x > 0 ? "+" : "";
  return `${sign}${x.toFixed(digits)}%`;
}

function formatUsd(x: number, digits = 2): string {
  const sign = x > 0 ? "+" : x < 0 ? "" : "";
  return `${sign}${x.toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: digits,
  })}`;
}

function rateTone(rate: number | null): string {
  if (rate === null) return "bg-slate-800 text-slate-400";
  if (rate >= 0.6) return "bg-emerald-900/30 text-emerald-200";
  if (rate >= 0.3) return "bg-amber-900/30 text-amber-200";
  return "bg-rose-900/30 text-rose-200";
}

export function CoverageStats({ stats }: { stats: ReplayStats }) {
  const pnl = stats.realized_pnl_usd;
  const pnlTone =
    pnl > 0 ? "text-emerald-300" : pnl < 0 ? "text-rose-300" : "text-slate-300";

  return (
    <div className="grid grid-cols-1 gap-3 lg:grid-cols-3">
      {/* ── 生命周期 & 盈亏 ── */}
      <Card title="生命周期 & 盈亏">
        <KV label="持仓时长" value={formatDuration(stats.duration_sec)} />
        <KV
          label="最终关仓"
          value={
            stats.final_close_kind
              ? ({
                  close_manual: "手动平仓",
                  close_sl_hit: "止损触发",
                  close_tp_hit: "止盈触发",
                } as Record<string, string>)[stats.final_close_kind] ??
                stats.final_close_kind
              : "未关仓"
          }
        />
        <KV
          label="已实现 P&L"
          value={
            <span className={pnlTone}>
              {formatUsd(pnl)} USD · {formatSignedPct(stats.realized_pnl_pct)}
            </span>
          }
        />
        <KV label="峰值保证金" value={`${formatUsd(stats.max_margin_used_usd)} USD`} />
        <KV
          label="峰值有效杠杆"
          value={`${stats.peak_effective_leverage.toFixed(2)} x`}
        />
      </Card>

      {/* ── 覆盖率 ── */}
      <Card title="信号覆盖率">
        <RateRow label="整体" rate={stats.follow_rate_overall} />
        <RateRow label="加仓提示" rate={stats.follow_rate_add} />
        <RateRow label="减仓提示" rate={stats.follow_rate_reduce} />
        <RateRow label="离场提示" rate={stats.follow_rate_close} />
        <RateRow label="移止损提示" rate={stats.follow_rate_move_sl} />
        <div className="mt-2 border-t border-slate-800 pt-2 text-[11px] text-slate-500">
          提醒总数：
          <span className="font-mono text-slate-300">
            {Object.values(stats.alerts_by_action).reduce((a, b) => a + b, 0)}
          </span>
          {" · "}
          平均响应：
          <span className="font-mono text-slate-300">
            {stats.avg_follow_delay_sec === null
              ? "—"
              : `${Math.round(stats.avg_follow_delay_sec / 60)} 分钟`}
          </span>
        </div>
      </Card>

      {/* ── 行为统计 ── */}
      <Card title="动作与覆盖">
        <div className="grid grid-cols-3 gap-2">
          <Tile label="加仓" value={stats.adds} tone="text-emerald-200" />
          <Tile label="减仓" value={stats.reduces} tone="text-amber-200" />
          <Tile label="移止损" value={stats.sl_moves} tone="text-sky-200" />
          <Tile label="关仓" value={stats.closes} tone="text-slate-200" />
          <Tile
            label="强覆盖"
            value={stats.overrides}
            tone="text-fuchsia-200"
            hint={
              stats.override_rate !== null
                ? `${formatPct(stats.override_rate)} 加仓为覆盖`
                : undefined
            }
          />
          <Tile
            label="闸门拦截"
            value={stats.gate_blocks}
            tone="text-rose-200"
          />
        </div>
      </Card>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 子件
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40">
      <div className="border-b border-slate-800 px-3 py-2 text-[12px] font-semibold text-slate-300">
        {title}
      </div>
      <div className="space-y-1.5 p-3">{children}</div>
    </div>
  );
}

function KV({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12px]">
      <span className="text-slate-500">{label}</span>
      <span className="font-mono text-slate-200">{value}</span>
    </div>
  );
}

function RateRow({ label, rate }: { label: string; rate: number | null }) {
  return (
    <div className="flex items-center gap-3 text-[12px]">
      <span className="w-20 shrink-0 text-slate-500">{label}</span>
      <div className="relative h-4 flex-1 overflow-hidden rounded bg-slate-800">
        {rate !== null && (
          <div
            className={[
              "absolute inset-y-0 left-0 transition-all",
              rate >= 0.6
                ? "bg-emerald-600/60"
                : rate >= 0.3
                ? "bg-amber-600/60"
                : "bg-rose-600/60",
            ].join(" ")}
            style={{ width: `${Math.max(2, rate * 100)}%` }}
          />
        )}
      </div>
      <span
        className={[
          "min-w-[3rem] rounded px-1.5 py-0.5 text-right font-mono text-[11px]",
          rateTone(rate),
        ].join(" ")}
      >
        {formatPct(rate)}
      </span>
    </div>
  );
}

function Tile({
  label,
  value,
  tone,
  hint,
}: {
  label: string;
  value: number;
  tone: string;
  hint?: string;
}) {
  return (
    <div className="rounded border border-slate-800 bg-slate-900/60 p-2">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={["text-lg font-semibold", tone].join(" ")}>{value}</div>
      {hint && <div className="text-[10px] text-slate-500">{hint}</div>}
    </div>
  );
}
