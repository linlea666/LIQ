"use client";

/**
 * RollingLadder · 滚仓阶梯时间轴
 *
 * 目标：用一条纵向时间轴把 init / add / reduce / sl_move / close_* 事件
 *      按时间顺序展开，让用户直观看到"每一次加减仓之后关键指标（均价/杠杆/爆仓/止损）
 *      是怎么被推着动的"，以及哪些是系统建议、哪些是用户覆盖。
 *
 * 数据源：eventsByPosition[id]（优先） ∪ position.events（兜底）。
 * 过滤：alert_* / gate_blocked 这类提醒不显示在主阶梯，避免噪声。
 */

import type { RollEvent, UserPosition } from "@/lib/rollTypes";

const PRIMARY_KINDS = new Set<RollEvent["kind"]>([
  "init",
  "add",
  "reduce",
  "sl_move",
  "close_manual",
  "close_sl_hit",
  "close_tp_hit",
  "user_override_add",
]);

const KIND_META: Record<
  RollEvent["kind"],
  { label: string; icon: string; tone: string }
> = {
  init: { label: "建仓", icon: "●", tone: "bg-slate-700 text-slate-200" },
  add: { label: "加仓", icon: "+", tone: "bg-emerald-800/70 text-emerald-100" },
  reduce: { label: "减仓", icon: "−", tone: "bg-rose-800/70 text-rose-100" },
  sl_move: { label: "移止损", icon: "⟂", tone: "bg-sky-800/70 text-sky-100" },
  close_manual: { label: "手动平仓", icon: "⨯", tone: "bg-slate-800 text-slate-200" },
  close_sl_hit: { label: "止损触发", icon: "⨯", tone: "bg-rose-800/70 text-rose-100" },
  close_tp_hit: { label: "止盈触发", icon: "⨯", tone: "bg-emerald-800/70 text-emerald-100" },
  user_override_add: { label: "覆盖加仓", icon: "!", tone: "bg-amber-800/70 text-amber-100" },

  // 以下为 alert/gate 事件（RollingLadder 不渲染，但 Record 需要字段完整）
  alert_add: { label: "", icon: "", tone: "" },
  alert_reduce: { label: "", icon: "", tone: "" },
  alert_close: { label: "", icon: "", tone: "" },
  alert_move_sl: { label: "", icon: "", tone: "" },
  alert_forward: { label: "", icon: "", tone: "" },
  gate_blocked: { label: "", icon: "", tone: "" },
};

interface Props {
  position: UserPosition;
  events: RollEvent[];
}

export default function RollingLadder({ position, events }: Props) {
  const ordered = [...events]
    .filter((e) => PRIMARY_KINDS.has(e.kind))
    .sort((a, b) => a.ts - b.ts);

  if (ordered.length === 0) {
    return (
      <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4 text-center text-[12px] text-slate-500">
        暂无阶梯数据 · 执行加仓/减仓/移止损后将在此展示
      </section>
    );
  }

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      <header className="border-b border-slate-800 px-4 py-2 text-[12px] font-semibold text-slate-300">
        滚仓阶梯 · {ordered.length} 步
      </header>

      <ol className="relative space-y-0 px-4 py-3">
        <div className="absolute bottom-3 left-[28px] top-3 w-px bg-slate-800" />
        {ordered.map((e, i) => {
          const meta = KIND_META[e.kind];
          return (
            <li
              key={`${e.ts}-${i}`}
              className="relative flex gap-3 py-2"
            >
              <div
                className={[
                  "z-10 flex h-7 w-7 shrink-0 items-center justify-center rounded-full text-[13px] font-bold shadow",
                  meta.tone,
                ].join(" ")}
              >
                {meta.icon}
              </div>
              <div className="min-w-0 flex-1 rounded border border-slate-800 bg-slate-950/40 p-2">
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <div className="flex items-baseline gap-2">
                    <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-300">
                      {meta.label}
                    </span>
                    <span className="font-mono text-[11px] text-slate-500">
                      {new Date(e.ts * 1000).toLocaleString("zh-CN", { hour12: false })}
                    </span>
                    {e.user_override && (
                      <span className="rounded bg-amber-900/50 px-1.5 py-0.5 text-[10px] text-amber-200">
                        覆盖
                      </span>
                    )}
                    {e.system_action && e.system_action !== e.kind && (
                      <span
                        className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] text-slate-400"
                        title={`系统当时建议动作：${e.system_action}`}
                      >
                        sys: {e.system_action}
                      </span>
                    )}
                  </div>
                  <div className="flex items-baseline gap-3 text-[11px]">
                    <span className="font-mono text-slate-300">
                      @ {e.price ? e.price.toLocaleString() : "-"}
                    </span>
                    {Math.abs(e.margin_delta_usd) > 1e-6 && (
                      <span
                        className={[
                          "font-mono",
                          e.margin_delta_usd > 0
                            ? "text-emerald-300"
                            : "text-rose-300",
                        ].join(" ")}
                      >
                        {e.margin_delta_usd > 0 ? "+" : ""}
                        {e.margin_delta_usd.toFixed(2)} USD
                      </span>
                    )}
                    {Math.abs(e.size_delta) > 1e-9 && (
                      <span className="font-mono text-slate-400">
                        Δsize {e.size_delta >= 0 ? "+" : ""}
                        {e.size_delta.toFixed(6)}
                      </span>
                    )}
                  </div>
                </div>

                <div className="mt-1 grid grid-cols-2 gap-x-4 gap-y-0.5 text-[11px] sm:grid-cols-4">
                  <Kv label="均价" value={e.avg_price_after ? e.avg_price_after.toLocaleString() : "-"} />
                  <Kv label="有效杠杆" value={e.leverage_after ? `${e.leverage_after.toFixed(2)}x` : "-"} />
                  <Kv label="爆仓价" value={e.liq_price_after ? e.liq_price_after.toLocaleString() : "-"} />
                  <Kv label="止损" value={e.sl_after != null ? e.sl_after.toLocaleString() : "-"} />
                </div>

                {e.reason && (
                  <div className="mt-1 truncate text-[11px] text-slate-500" title={e.reason}>
                    {e.reason}
                  </div>
                )}

                {e.system_confidence > 0 && (
                  <div className="mt-0.5 font-mono text-[10px] text-slate-600">
                    sys_conf={e.system_confidence.toFixed(1)}
                  </div>
                )}
              </div>
            </li>
          );
        })}
      </ol>

      <footer className="border-t border-slate-800 px-4 py-2 text-[10px] text-slate-500">
        持仓币数：
        <span className="mx-1 font-mono text-slate-300">
          {position.position_size.toFixed(6)}
        </span>
        · 保证金：
        <span className="mx-1 font-mono text-slate-300">
          {position.margin_used_usd.toFixed(2)} USD
        </span>
        · 状态：
        <span className="ml-1 font-mono text-slate-300">
          {position.status}
        </span>
      </footer>
    </section>
  );
}

function Kv({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between">
      <span className="text-slate-500">{label}</span>
      <span className="font-mono text-slate-200">{value}</span>
    </div>
  );
}
