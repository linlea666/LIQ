"use client";

/**
 * ReplayTimeline —— 复盘详情页的完整事件流时间线
 *
 * 与 RollingLadder 的区别：
 *   - 包含全部 alert_* / gate_blocked 事件（RollingLadder 只显示用户动作）
 *   - 每条 alert → 最近的匹配用户动作会被"配对着色"，便于一眼看覆盖率
 *   - 支持按类型筛选（user / alert / gate）
 *
 * 布局：左侧图标 + 右侧卡片。已配对的 alert 上会加 ✓ 绿色勾，未配对显示 ⧗。
 */

import { useMemo, useState } from "react";

import type { EventKind, ReplayStats, RollEvent, Side } from "@/lib/rollTypes";

const FOLLOW_WINDOW_SEC = 30 * 60;

interface Props {
  events: RollEvent[];
  stats: ReplayStats;
  side: Side;
  initialPrice?: number;
}

const KIND_META: Record<
  EventKind,
  { label: string; icon: string; tone: string; category: "user" | "alert" | "gate" }
> = {
  init: { label: "建仓", icon: "●", tone: "text-sky-300", category: "user" },
  add: { label: "加仓", icon: "▲", tone: "text-emerald-300", category: "user" },
  reduce: { label: "减仓", icon: "▼", tone: "text-amber-300", category: "user" },
  sl_move: { label: "移止损", icon: "◆", tone: "text-sky-300", category: "user" },
  close_manual: { label: "手动平仓", icon: "✕", tone: "text-slate-300", category: "user" },
  close_sl_hit: { label: "止损触发", icon: "✕", tone: "text-rose-300", category: "user" },
  close_tp_hit: { label: "止盈触发", icon: "✕", tone: "text-emerald-300", category: "user" },
  alert_add: { label: "建议加仓", icon: "△", tone: "text-emerald-400/70", category: "alert" },
  alert_reduce: { label: "建议减仓", icon: "▽", tone: "text-amber-400/70", category: "alert" },
  alert_close: { label: "建议离场", icon: "⊗", tone: "text-slate-400", category: "alert" },
  alert_move_sl: { label: "建议移止损", icon: "◇", tone: "text-sky-400/70", category: "alert" },
  alert_forward: { label: "前瞻提示", icon: "…", tone: "text-slate-400", category: "alert" },
  gate_blocked: { label: "闸门拦截", icon: "⛔", tone: "text-rose-400", category: "gate" },
  user_override_add: {
    label: "强覆盖加仓",
    icon: "▲",
    tone: "text-fuchsia-300",
    category: "user",
  },
};

const FOLLOW_MAP: Record<string, string[]> = {
  alert_add: ["add", "user_override_add"],
  alert_reduce: ["reduce"],
  alert_close: ["close_manual", "close_sl_hit", "close_tp_hit"],
  alert_move_sl: ["sl_move"],
};

function formatTs(ts: number): string {
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function formatDelay(sec: number): string {
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${Math.round((sec / 3600) * 10) / 10}h`;
}

/**
 * 贪心配对：复用后端逻辑 —— alert → 窗口内首条匹配的用户事件，
 * 已被占用的用户事件不再被其他 alert 配对。
 *
 * 返回：
 *   alertMatched[idx] = matched user event idx（-1 表示未配对）
 *   userMatchedFrom[idx] = alert idx（-1 表示该用户动作不对应 alert）
 */
function pairAlerts(events: RollEvent[]): {
  alertMatched: number[];
  userMatchedFrom: number[];
} {
  const alertMatched = new Array<number>(events.length).fill(-1);
  const userMatchedFrom = new Array<number>(events.length).fill(-1);
  const usedUser = new Set<number>();

  for (let ai = 0; ai < events.length; ai++) {
    const alert = events[ai];
    const expects = FOLLOW_MAP[alert.kind];
    if (!expects) continue;
    for (let ui = ai + 1; ui < events.length; ui++) {
      if (usedUser.has(ui)) continue;
      const cand = events[ui];
      const delay = cand.ts - alert.ts;
      if (delay > FOLLOW_WINDOW_SEC) break;
      if (expects.includes(cand.kind)) {
        alertMatched[ai] = ui;
        userMatchedFrom[ui] = ai;
        usedUser.add(ui);
        break;
      }
    }
  }
  return { alertMatched, userMatchedFrom };
}

export function ReplayTimeline({ events, stats, side, initialPrice }: Props) {
  const [filter, setFilter] = useState<"all" | "user" | "alert" | "gate">("all");

  const sorted = useMemo(
    () => [...events].sort((a, b) => a.ts - b.ts),
    [events],
  );

  const { alertMatched, userMatchedFrom } = useMemo(
    () => pairAlerts(sorted),
    [sorted],
  );

  const filtered = useMemo(
    () =>
      sorted
        .map((e, idx) => ({ e, idx }))
        .filter(({ e }) =>
          filter === "all"
            ? true
            : KIND_META[e.kind].category === filter,
        ),
    [sorted, filter],
  );

  const sideDir = side === "long" ? 1 : -1;
  const startPrice = initialPrice ?? sorted.find((e) => e.kind === "init")?.price ?? 0;

  // 运行中的平均价，供每行渲染"相对建仓位移"
  const displayCount = filtered.length;

  return (
    <div className="space-y-3">
      {/* ── 过滤 + 摘要 ── */}
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-1 text-[11px]">
          {(
            [
              ["all", "全部"],
              ["user", "用户动作"],
              ["alert", "系统提醒"],
              ["gate", "闸门"],
            ] as [typeof filter, string][]
          ).map(([v, l]) => (
            <button
              key={v}
              type="button"
              onClick={() => setFilter(v)}
              className={[
                "rounded-md border px-2 py-1 transition",
                filter === v
                  ? "border-sky-500 bg-sky-900/30 text-sky-200"
                  : "border-slate-700 text-slate-400 hover:border-slate-500",
              ].join(" ")}
            >
              {l}
            </button>
          ))}
        </div>
        <div className="text-[11px] text-slate-500">
          显示 <span className="font-mono text-slate-300">{displayCount}</span> /
          共 <span className="font-mono text-slate-300">{stats.total_events}</span> 条
        </div>
      </div>

      {/* ── 时间线 ── */}
      <ol className="relative border-l border-slate-800 pl-5">
        {filtered.map(({ e, idx }, displayIdx) => {
          const meta = KIND_META[e.kind];
          const isAlert = meta.category === "alert";
          const matchedUserIdx = isAlert ? alertMatched[idx] : -1;
          const matchedAlertIdx = userMatchedFrom[idx] ?? -1;
          const alertFollowed = isAlert && matchedUserIdx >= 0;
          const userFromAlert = !isAlert && matchedAlertIdx >= 0;

          const delay =
            alertFollowed && matchedUserIdx >= 0
              ? sorted[matchedUserIdx].ts - e.ts
              : null;

          const priceShift =
            startPrice > 0 && e.price > 0
              ? ((e.price - startPrice) / startPrice) * 100 * sideDir
              : null;

          return (
            <li
              key={`${e.ts}-${idx}`}
              className={[
                "relative mb-3 rounded-md border p-2 text-[12px]",
                isAlert
                  ? alertFollowed
                    ? "border-emerald-800/60 bg-emerald-950/20"
                    : "border-slate-800 bg-slate-900/30 opacity-80"
                  : e.kind === "gate_blocked"
                  ? "border-rose-800/50 bg-rose-950/20"
                  : e.kind === "user_override_add"
                  ? "border-fuchsia-800/50 bg-fuchsia-950/20"
                  : "border-slate-800 bg-slate-900/60",
              ].join(" ")}
            >
              {/* 左侧 dot */}
              <span
                className={[
                  "absolute -left-[30px] top-3 flex h-6 w-6 items-center justify-center rounded-full border border-slate-800 bg-slate-950 text-[12px]",
                  meta.tone,
                ].join(" ")}
                title={meta.label}
              >
                {meta.icon}
              </span>

              {/* 顶行：kind + ts + 配对徽章 */}
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="flex items-baseline gap-2">
                  <span className={["font-medium", meta.tone].join(" ")}>
                    #{displayIdx + 1} · {meta.label}
                  </span>
                  {e.user_override && e.kind === "user_override_add" && (
                    <span className="rounded bg-fuchsia-900/40 px-1.5 py-0.5 text-[10px] text-fuchsia-200">
                      强覆盖
                    </span>
                  )}
                  {isAlert &&
                    (alertFollowed ? (
                      <span className="rounded bg-emerald-900/40 px-1.5 py-0.5 text-[10px] text-emerald-200">
                        ✓ {delay !== null ? formatDelay(delay) : "已配对"}
                      </span>
                    ) : (
                      <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400">
                        ⧗ 未采纳
                      </span>
                    ))}
                  {userFromAlert && (
                    <span className="rounded bg-sky-900/30 px-1.5 py-0.5 text-[10px] text-sky-300">
                      ↳ 响应 #
                      {sorted.filter((_, i) => i <= matchedAlertIdx).length}
                    </span>
                  )}
                </div>
                <span className="font-mono text-[11px] text-slate-500">
                  {formatTs(e.ts)}
                </span>
              </div>

              {/* 详情 */}
              <div className="mt-1.5 grid grid-cols-2 gap-x-3 gap-y-0.5 font-mono text-[11px] sm:grid-cols-4">
                {e.price > 0 && (
                  <Kv label="价格">
                    {e.price.toLocaleString()}
                    {priceShift !== null && (
                      <span
                        className={[
                          "ml-1 text-[10px]",
                          priceShift >= 0
                            ? "text-emerald-400"
                            : "text-rose-400",
                        ].join(" ")}
                      >
                        {priceShift >= 0 ? "+" : ""}
                        {priceShift.toFixed(2)}%
                      </span>
                    )}
                  </Kv>
                )}
                {Math.abs(e.margin_delta_usd) > 0 && (
                  <Kv label="保证金Δ">
                    <span
                      className={
                        e.margin_delta_usd > 0
                          ? "text-emerald-300"
                          : "text-rose-300"
                      }
                    >
                      {e.margin_delta_usd > 0 ? "+" : ""}
                      {e.margin_delta_usd.toFixed(2)}
                    </span>
                  </Kv>
                )}
                {Math.abs(e.size_delta) > 0 && (
                  <Kv label="币数Δ">
                    <span
                      className={
                        e.size_delta > 0
                          ? "text-emerald-300"
                          : "text-rose-300"
                      }
                    >
                      {e.size_delta > 0 ? "+" : ""}
                      {e.size_delta.toFixed(4)}
                    </span>
                  </Kv>
                )}
                {e.avg_price_after > 0 && (
                  <Kv label="均价后">{e.avg_price_after.toLocaleString()}</Kv>
                )}
                {e.leverage_after > 0 && (
                  <Kv label="杠杆后">{e.leverage_after.toFixed(2)}x</Kv>
                )}
                {e.liq_price_after > 0 && (
                  <Kv label="爆仓价">{e.liq_price_after.toLocaleString()}</Kv>
                )}
                {e.sl_after != null && (
                  <Kv label="止损">{e.sl_after.toLocaleString()}</Kv>
                )}
                {e.system_confidence > 0 && (
                  <Kv label="系统conf">{e.system_confidence.toFixed(0)}</Kv>
                )}
              </div>

              {e.reason && (
                <div className="mt-1.5 text-[11px] text-slate-400">
                  {e.reason}
                </div>
              )}
            </li>
          );
        })}
        {filtered.length === 0 && (
          <li className="py-6 text-center text-[12px] text-slate-500">
            当前筛选下暂无事件
          </li>
        )}
      </ol>
    </div>
  );
}

function Kv({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-1">
      <span className="text-slate-500">{label}</span>
      <span className="text-slate-200">{children}</span>
    </div>
  );
}
