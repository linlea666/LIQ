"use client";

/**
 * /roll 总览页
 *
 *   - 列出所有 active 持仓：币种 / 方向 / 杠杆 / 均价 / 浮盈 / 最新信号摘要
 *   - 对每条展示当前 confidence_score + action + headline_cn
 *   - 点击进入单仓详情（Step 7 接入卡片）
 *   - 顶部 CTA：新建计划
 */

import Link from "next/link";
import { useMemo } from "react";

import {
  ActionBadge,
  IntensityBadge,
  UrgencyBadge,
} from "@/components/Roll/SignalBadges";
import { useRollStore } from "@/stores/rollStore";
import type { RollSignal, UserPosition } from "@/lib/rollTypes";

export default function RollOverviewPage() {
  const positions = useRollStore((s) => s.positions);
  const loading = useRollStore((s) => s.positionsLoading);
  const signalsByPosition = useRollStore((s) => s.signalsByPosition);
  const plansById = useRollStore((s) => s.plansById);

  const activePositions = useMemo(
    () => positions.filter((p) => p.status === "active"),
    [positions],
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold">我的滚仓计划</h1>
          <p className="mt-1 text-[12px] text-slate-500">
            引擎每 10 秒评估一次活跃持仓，动作建议与前瞻窗口会通过 WS 实时推送。
          </p>
        </div>
        <Link
          href="/roll/new"
          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-500"
        >
          + 新建计划
        </Link>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40">
        {loading && activePositions.length === 0 && (
          <div className="py-10 text-center text-[12px] text-slate-500">
            加载中…
          </div>
        )}
        {!loading && activePositions.length === 0 && <EmptyState />}
        {activePositions.length > 0 && (
          <ul className="divide-y divide-slate-800">
            {activePositions.map((p) => (
              <PositionRow
                key={p.id}
                position={p}
                signal={signalsByPosition[p.id]}
                templateId={plansById[p.plan_id]?.template_id}
              />
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="py-16 text-center">
      <p className="text-lg text-slate-300">还没有活跃的滚仓计划</p>
      <p className="mt-2 text-[12px] text-slate-500">
        选择一个策略模板（肥仔派 / 李法师派 / 自定义）建仓即可，引擎会自动跟踪并在关键时刻提醒。
      </p>
      <Link
        href="/roll/new"
        className="mt-4 inline-block rounded-md border border-emerald-600/60 px-4 py-2 text-[13px] text-emerald-300 transition hover:bg-emerald-900/30"
      >
        立即创建 →
      </Link>
    </div>
  );
}

interface RowProps {
  position: UserPosition;
  signal: RollSignal | undefined;
  templateId: string | undefined;
}

function PositionRow({ position, signal, templateId }: RowProps) {
  const pnlPct = signal?.unrealized_pnl_pct ?? 0;
  const price = signal?.current_price ?? position.entry_price;
  const effLev = signal?.effective_leverage ?? position.leverage;

  return (
    <li>
      <Link
        href={`/roll/${position.id}`}
        className="block px-4 py-3 transition hover:bg-slate-800/40"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3">
            <SideBadge side={position.side} />
            <div>
              <div className="flex items-baseline gap-2">
                <span className="text-base font-semibold">{position.coin}</span>
                <span className="text-[11px] text-slate-500">
                  {position.margin_mode === "isolated" ? "逐仓" : "全仓"} · {position.leverage}x
                </span>
                {templateId && (
                  <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                    {templateId}
                  </span>
                )}
              </div>
              <div className="mt-0.5 text-[11px] text-slate-500">
                均价 {position.entry_price.toLocaleString()} · 现价{" "}
                {price.toLocaleString()} · 有效杠杆 {effLev.toFixed(2)}x
              </div>
            </div>
          </div>

          <div className="flex items-center gap-4">
            <PnlPill pct={pnlPct} />
            {signal ? <SignalSummary signal={signal} /> : (
              <span className="text-[11px] text-slate-500">等待引擎首轮评估…</span>
            )}
          </div>
        </div>

        {signal?.forward_windows?.length ? (
          <div className="mt-2 flex flex-wrap gap-1.5">
            {signal.forward_windows.map((fw, i) => (
              <span
                key={i}
                className="rounded border border-sky-700/50 bg-sky-950/40 px-2 py-0.5 text-[10px] text-sky-300"
                title={fw.hint_cn}
              >
                🔮 {fw.hint_cn.slice(0, 24)}
                {fw.hint_cn.length > 24 ? "…" : ""}
              </span>
            ))}
          </div>
        ) : null}
      </Link>
    </li>
  );
}

function SideBadge({ side }: { side: "long" | "short" }) {
  const isLong = side === "long";
  return (
    <div
      className={[
        "flex h-8 w-8 items-center justify-center rounded-full text-[11px] font-bold",
        isLong
          ? "bg-emerald-900/40 text-emerald-400"
          : "bg-rose-900/40 text-rose-400",
      ].join(" ")}
    >
      {isLong ? "多" : "空"}
    </div>
  );
}

function PnlPill({ pct }: { pct: number }) {
  const up = pct >= 0;
  return (
    <div
      className={[
        "rounded px-2 py-0.5 text-[13px] font-mono",
        up
          ? "bg-emerald-950/60 text-emerald-300"
          : "bg-rose-950/60 text-rose-300",
      ].join(" ")}
    >
      {up ? "+" : ""}
      {pct.toFixed(2)}%
    </div>
  );
}

function SignalSummary({ signal }: { signal: RollSignal }) {
  return (
    <div className="flex flex-col items-end gap-0.5">
      <div className="flex items-center gap-2">
        <ActionBadge action={signal.action} />
        {signal.action === "add" && signal.add_intensity !== "reject" && (
          <IntensityBadge intensity={signal.add_intensity} />
        )}
        <UrgencyBadge urgency={signal.urgency} />
        <span className="font-mono text-[11px] text-slate-400">
          conf {signal.confidence_score.toFixed(0)}
        </span>
      </div>
      {signal.headline_cn && (
        <span className="max-w-[280px] truncate text-[11px] text-slate-500">
          {signal.headline_cn}
        </span>
      )}
    </div>
  );
}
