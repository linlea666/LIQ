"use client";

/**
 * /roll/replay · 复盘列表页
 *
 * 展示所有已平仓持仓 + 当前活跃持仓的"截至当前"复盘摘要。
 *
 * 交互：
 *   - 顶部 Tab：已平仓 / 活跃持仓
 *   - 列表每行点击跳转详情
 *   - 列表行右侧显示 PnL、覆盖率小条、关仓类型徽章
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";

import { useRollStore } from "@/stores/rollStore";
import type { UserPosition } from "@/lib/rollTypes";

type Tab = "closed" | "active";

export default function RollReplayListPage() {
  const closedPositions = useRollStore((s) => s.closedPositions);
  const activePositions = useRollStore((s) => s.positions);
  const loadClosed = useRollStore((s) => s.loadClosedPositions);
  const closedLoading = useRollStore((s) => s.closedPositionsLoading);

  const [tab, setTab] = useState<Tab>("closed");

  useEffect(() => {
    loadClosed();
  }, [loadClosed]);

  const list = tab === "closed" ? closedPositions : activePositions;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">复盘</h1>
          <p className="mt-1 text-[12px] text-slate-500">
            查看历史持仓的完整事件流 + 覆盖率统计，帮助发现「频繁无视信号」的行为模式。
          </p>
        </div>
        <button
          onClick={() => loadClosed()}
          disabled={closedLoading}
          className="rounded-md border border-slate-700 px-3 py-1.5 text-[12px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"
        >
          {closedLoading ? "刷新中…" : "刷新"}
        </button>
      </div>

      <div className="flex items-center gap-1">
        <TabBtn
          active={tab === "closed"}
          label={`已平仓 (${closedPositions.length})`}
          onClick={() => setTab("closed")}
        />
        <TabBtn
          active={tab === "active"}
          label={`活跃中 (${activePositions.length})`}
          onClick={() => setTab("active")}
        />
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40">
        {list.length === 0 ? (
          <div className="py-10 text-center text-[12px] text-slate-500">
            {tab === "closed" ? "暂无已平仓记录" : "当前无活跃持仓"}
          </div>
        ) : (
          <ul className="divide-y divide-slate-800">
            {list.map((p) => (
              <ReplayRow key={p.id} position={p} />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function TabBtn({
  active,
  label,
  onClick,
}: {
  active: boolean;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={[
        "rounded-md border px-3 py-1.5 text-[12px] transition",
        active
          ? "border-emerald-500 bg-emerald-900/30 text-emerald-200"
          : "border-slate-700 text-slate-400 hover:border-slate-500",
      ].join(" ")}
    >
      {label}
    </button>
  );
}

function ReplayRow({ position }: { position: UserPosition }) {
  // 直接复用内嵌 events 做快速摘要；详情页才调用 loadReplay 拉完整 stats
  const followRate = useMemo(() => computeQuickFollowRate(position), [position]);
  const pnl = useMemo(() => computeQuickPnl(position), [position]);

  const closeKind = useMemo(() => {
    const last = [...position.events]
      .reverse()
      .find((e) =>
        ["close_manual", "close_sl_hit", "close_tp_hit"].includes(e.kind),
      );
    return last?.kind;
  }, [position]);

  return (
    <li>
      <Link
        href={`/roll/replay/${encodeURIComponent(position.id)}`}
        className="flex flex-wrap items-center gap-3 px-4 py-3 transition hover:bg-slate-800/50"
      >
        {/* 币种 + 方向 */}
        <div className="flex flex-col">
          <span className="text-[13px] font-semibold">{position.coin}</span>
          <span
            className={[
              "text-[10px] uppercase",
              position.side === "long" ? "text-emerald-400" : "text-rose-400",
            ].join(" ")}
          >
            {position.side} · {position.leverage}x · {position.margin_mode}
          </span>
        </div>

        {/* 时间 */}
        <div className="min-w-[140px] text-[11px] text-slate-500">
          <div>
            建仓 {new Date(position.created_at * 1000).toLocaleDateString("zh-CN")}
          </div>
          {position.closed_at ? (
            <div>
              关仓 {new Date(position.closed_at * 1000).toLocaleDateString("zh-CN")}
            </div>
          ) : (
            <div className="text-emerald-400">进行中</div>
          )}
        </div>

        {/* 事件与覆盖率 */}
        <div className="flex flex-col">
          <span className="text-[10px] text-slate-500">事件 / 覆盖率</span>
          <span className="font-mono text-[12px]">
            {position.events.length}
            <span className="mx-1 text-slate-600">·</span>
            <span
              className={
                followRate === null
                  ? "text-slate-400"
                  : followRate >= 0.6
                  ? "text-emerald-300"
                  : followRate >= 0.3
                  ? "text-amber-300"
                  : "text-rose-300"
              }
            >
              {followRate === null
                ? "无提醒"
                : `${(followRate * 100).toFixed(0)}%`}
            </span>
          </span>
        </div>

        {/* PnL */}
        <div className="ml-auto flex flex-col items-end">
          <span className="text-[10px] text-slate-500">已实现 P&L</span>
          <span
            className={[
              "font-mono text-[13px] font-semibold",
              pnl > 0
                ? "text-emerald-300"
                : pnl < 0
                ? "text-rose-300"
                : "text-slate-400",
            ].join(" ")}
          >
            {pnl > 0 ? "+" : ""}
            {pnl.toFixed(2)} USD
          </span>
        </div>

        {/* 关仓徽章 */}
        {closeKind && (
          <span
            className={[
              "rounded px-1.5 py-0.5 text-[10px]",
              closeKind === "close_sl_hit"
                ? "bg-rose-900/40 text-rose-200"
                : closeKind === "close_tp_hit"
                ? "bg-emerald-900/40 text-emerald-200"
                : "bg-slate-800 text-slate-300",
            ].join(" ")}
          >
            {closeKind === "close_sl_hit"
              ? "止损"
              : closeKind === "close_tp_hit"
              ? "止盈"
              : "手动"}
          </span>
        )}

        <span className="text-slate-500">›</span>
      </Link>
    </li>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// 轻量计算（仅列表摘要用；详情页由后端精确计算）
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

const FOLLOW_WINDOW_SEC = 30 * 60;
const FOLLOW_MAP: Record<string, string[]> = {
  alert_add: ["add", "user_override_add"],
  alert_reduce: ["reduce"],
  alert_close: ["close_manual", "close_sl_hit", "close_tp_hit"],
  alert_move_sl: ["sl_move"],
};

function computeQuickFollowRate(p: UserPosition): number | null {
  const sorted = [...p.events].sort((a, b) => a.ts - b.ts);
  const used = new Set<number>();
  let totalAlerts = 0;
  let matched = 0;
  for (let ai = 0; ai < sorted.length; ai++) {
    const a = sorted[ai];
    const expects = FOLLOW_MAP[a.kind];
    if (!expects) continue;
    totalAlerts++;
    for (let ui = ai + 1; ui < sorted.length; ui++) {
      if (used.has(ui)) continue;
      const u = sorted[ui];
      if (u.ts - a.ts > FOLLOW_WINDOW_SEC) break;
      if (expects.includes(u.kind)) {
        matched++;
        used.add(ui);
        break;
      }
    }
  }
  return totalAlerts === 0 ? null : matched / totalAlerts;
}

function computeQuickPnl(p: UserPosition): number {
  const sideDir = p.side === "long" ? 1 : -1;
  let pnl = 0;
  let avg = p.entry_price;
  for (const e of [...p.events].sort((a, b) => a.ts - b.ts)) {
    if (["reduce", "close_manual", "close_sl_hit", "close_tp_hit"].includes(e.kind)) {
      const closed = Math.abs(e.size_delta);
      if (closed > 0) pnl += closed * (e.price - avg) * sideDir;
    } else if (["init", "add", "user_override_add"].includes(e.kind)) {
      if (e.avg_price_after > 0) avg = e.avg_price_after;
    }
  }
  return pnl;
}
