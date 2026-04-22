"use client";

/**
 * SignalExplain · 信号解释板
 *
 * 结构：
 *   [ 置信度条 ]      加仓 score + 减仓 score（都画在阈值坐标上）
 *   [ 分项贡献 ]      confidence_breakdown 条形图（带符号）
 *   [ 支持 / 反对 ]   supporting / blocking 两列 SignalRef
 *   [ 减仓/止损建议 ] action=reduce/move_sl/close 时的专属操作区
 *   [ 前瞻窗口 ]      forward_windows 卡片列表
 *
 * 设计：不隐藏任何引擎原始证据 —— 系统给出动作时必须同时把"反对理由"摆出来，
 *       这是"真实教练"不骗用户的产品底线。
 */

import { useMemo, useState } from "react";

import { useRollStore } from "@/stores/rollStore";
import type {
  RollPlan,
  RollSignal,
  UserPosition,
} from "@/lib/rollTypes";
import { ConfidenceBar, ReasonChip } from "./SignalBadges";

interface Props {
  position: UserPosition;
  signal: RollSignal;
  plan: RollPlan | undefined;
  onExecuted?: () => void;
}

export default function SignalExplain({ position, signal, plan, onExecuted }: Props) {
  const executeEvent = useRollStore((s) => s.executeEvent);
  const [busy, setBusy] = useState<null | "reduce" | "move_sl" | "close">(null);
  const [err, setErr] = useState<string | null>(null);

  const thresholdsAdd = plan
    ? {
        full: plan.thresholds.full_add,
        half: plan.thresholds.half_add,
        small: plan.thresholds.small_add,
      }
    : undefined;
  const thresholdsReduce = plan
    ? {
        full: plan.thresholds.full_reduce,
        half: plan.thresholds.half_reduce,
      }
    : undefined;

  const breakdown = useMemo(() => {
    const entries = Object.entries(signal.confidence_breakdown || {});
    entries.sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
    return entries;
  }, [signal.confidence_breakdown]);

  const handleReduce = async () => {
    if (!signal.reduce_pct || busy) return;
    const pct = signal.reduce_pct;
    if (!confirm(`系统建议减仓 ${(pct * 100).toFixed(1)}%（约 ${(position.position_size * pct).toFixed(6)} ${position.coin}），确认执行？`)) return;
    setErr(null);
    setBusy("reduce");
    try {
      await executeEvent(position.id, {
        kind: "reduce",
        price: signal.current_price,
        reduce_pct: pct,
        reason: signal.headline_cn,
        system_confidence: signal.reduce_confidence,
        system_action: "reduce",
      });
      onExecuted?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleMoveSl = async () => {
    const sl = signal.suggested_new_sl;
    if (!sl || busy) return;
    if (!confirm(`确认把止损移到 ${sl.toLocaleString()} ?（${signal.sl_move_reason || "引擎建议"}）`)) return;
    setErr(null);
    setBusy("move_sl");
    try {
      await executeEvent(position.id, {
        kind: "move_sl",
        price: signal.current_price,
        new_sl: sl,
        reason: signal.sl_move_reason || "engine suggested",
      });
      onExecuted?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleClose = async () => {
    if (busy) return;
    if (!confirm(`系统建议离场。\n\n理由：${signal.headline_cn}\n\n确认平仓？`)) return;
    setErr(null);
    setBusy("close");
    try {
      await executeEvent(position.id, {
        kind: "close",
        price: signal.current_price,
        close_kind: "close_manual",
        reason: signal.headline_cn,
      });
      onExecuted?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const maxAbs = Math.max(
    1,
    ...breakdown.map(([, v]) => Math.abs(v)),
  );

  return (
    <section className="rounded-lg border border-slate-800 bg-slate-900/40">
      <header className="border-b border-slate-800 px-4 py-2 text-[12px] font-semibold text-slate-300">
        信号解释板
      </header>

      {/* ── 置信度 ── */}
      <div className="grid grid-cols-1 gap-4 border-b border-slate-800 px-4 py-3 sm:grid-cols-2">
        <ConfidenceBar
          score={signal.confidence_score}
          thresholds={thresholdsAdd}
          variant="add"
        />
        <ConfidenceBar
          score={signal.reduce_confidence}
          thresholds={thresholdsReduce}
          variant="reduce"
        />
      </div>

      {/* ── 专属操作区 ── */}
      {(signal.action === "reduce" || signal.action === "close" || signal.action === "move_sl") && (
        <div className="border-b border-slate-800 bg-slate-900/60 px-4 py-3 text-[12px]">
          {signal.action === "reduce" && signal.reduce_pct !== null && (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-rose-200">
                  建议减仓 <span className="font-mono text-base">{((signal.reduce_pct ?? 0) * 100).toFixed(1)}%</span>
                </div>
                <div className="mt-0.5 text-[11px] text-slate-400">
                  约 {(position.position_size * (signal.reduce_pct ?? 0)).toFixed(6)} {position.coin}
                </div>
              </div>
              <button
                onClick={handleReduce}
                disabled={busy !== null}
                className="rounded-md bg-rose-700 px-3 py-1.5 text-[12px] text-white transition hover:bg-rose-600 disabled:opacity-50"
              >
                {busy === "reduce" ? "记录中…" : "执行减仓"}
              </button>
            </div>
          )}

          {signal.action === "move_sl" && signal.suggested_new_sl != null && (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-sky-200">
                  建议移止损至{" "}
                  <span className="font-mono text-base">
                    {signal.suggested_new_sl.toLocaleString()}
                  </span>
                </div>
                <div className="mt-0.5 text-[11px] text-slate-400">
                  {signal.sl_move_reason || "引擎建议"}
                  {position.stop_loss != null && (
                    <>
                      {" · 当前 "}
                      <span className="font-mono">
                        {position.stop_loss.toLocaleString()}
                      </span>
                    </>
                  )}
                </div>
              </div>
              <button
                onClick={handleMoveSl}
                disabled={busy !== null}
                className="rounded-md bg-sky-700 px-3 py-1.5 text-[12px] text-white transition hover:bg-sky-600 disabled:opacity-50"
              >
                {busy === "move_sl" ? "记录中…" : "执行移止损"}
              </button>
            </div>
          )}

          {signal.action === "close" && (
            <div className="flex flex-wrap items-center justify-between gap-2">
              <div>
                <div className="text-rose-200">建议离场（系统信心足）</div>
                <div className="mt-0.5 text-[11px] text-slate-400">
                  请先在交易所平仓，再点此登记为已平仓。
                </div>
              </div>
              <button
                onClick={handleClose}
                disabled={busy !== null}
                className="rounded-md bg-rose-800 px-3 py-1.5 text-[12px] text-white transition hover:bg-rose-700 disabled:opacity-50"
              >
                {busy === "close" ? "记录中…" : "登记平仓"}
              </button>
            </div>
          )}
        </div>
      )}

      {/* ── Supporting / Blocking ── */}
      <div className="grid grid-cols-1 gap-4 border-b border-slate-800 px-4 py-3 sm:grid-cols-2">
        <div>
          <div className="mb-1 text-[11px] font-semibold text-emerald-300">
            支持信号 · {signal.supporting.length}
          </div>
          <div className="space-y-1">
            {signal.supporting.length === 0 ? (
              <div className="rounded border border-slate-800 px-2 py-1 text-[11px] text-slate-500">
                无
              </div>
            ) : (
              signal.supporting.map((s, i) => (
                <ReasonChip key={`sp-${i}`} signal={s} tone="support" />
              ))
            )}
          </div>
        </div>

        <div>
          <div className="mb-1 text-[11px] font-semibold text-rose-300">
            反对信号 · {signal.blocking.length}
          </div>
          <div className="space-y-1">
            {signal.blocking.length === 0 ? (
              <div className="rounded border border-slate-800 px-2 py-1 text-[11px] text-slate-500">
                无
              </div>
            ) : (
              signal.blocking.map((s, i) => (
                <ReasonChip key={`bk-${i}`} signal={s} tone="block" />
              ))
            )}
          </div>
        </div>
      </div>

      {/* ── Confidence Breakdown ── */}
      {breakdown.length > 0 && (
        <div className="border-b border-slate-800 px-4 py-3">
          <div className="mb-2 text-[11px] font-semibold text-slate-300">
            置信度分项贡献
          </div>
          <div className="space-y-1 text-[11px]">
            {breakdown.map(([key, val]) => {
              const pct = Math.min(100, (Math.abs(val) / maxAbs) * 100);
              const positive = val >= 0;
              return (
                <div key={key} className="grid grid-cols-[120px,1fr,60px] items-center gap-2">
                  <span className="truncate text-slate-400" title={key}>
                    {key}
                  </span>
                  <div className="relative h-2 rounded-full bg-slate-800">
                    <div
                      className={[
                        "absolute h-full rounded-full",
                        positive ? "left-1/2 bg-emerald-500/80" : "right-1/2 bg-rose-500/80",
                      ].join(" ")}
                      style={{ width: `${pct / 2}%` }}
                    />
                    <div className="absolute inset-y-0 left-1/2 w-px bg-slate-600" />
                  </div>
                  <span
                    className={[
                      "text-right font-mono",
                      positive ? "text-emerald-300" : "text-rose-300",
                    ].join(" ")}
                  >
                    {positive ? "+" : ""}
                    {val.toFixed(1)}
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* ── Forward Windows ── */}
      {signal.forward_windows.length > 0 && (
        <div className="px-4 py-3">
          <div className="mb-2 text-[11px] font-semibold text-sky-300">
            前瞻窗口 · {signal.forward_windows.length}
          </div>
          <div className="space-y-2">
            {signal.forward_windows.map((fw, i) => (
              <div
                key={i}
                className="rounded border border-sky-800/50 bg-sky-950/30 p-2 text-[11px]"
              >
                <div className="flex flex-wrap items-baseline justify-between gap-2">
                  <span className="rounded bg-sky-900/60 px-1.5 py-0.5 text-[10px] text-sky-200">
                    {fw.kind}
                  </span>
                  <span className="font-mono text-[10px] text-sky-300/80">
                    至 {new Date(fw.expires_at * 1000).toLocaleTimeString("zh-CN", { hour12: false })}
                  </span>
                </div>
                <div className="mt-1 text-sky-100">{fw.hint_cn}</div>
                {fw.related_signals.length > 0 && (
                  <div className="mt-1 flex flex-wrap gap-1 text-[10px] text-sky-200/80">
                    {fw.related_signals.map((rs, j) => (
                      <span
                        key={j}
                        className="rounded bg-slate-900/60 px-1.5 py-0.5"
                        title={rs.detail}
                      >
                        {rs.source} · {rs.read}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {err && (
        <div className="border-t border-rose-700/40 bg-rose-950/40 px-4 py-2 text-[11px] text-rose-200">
          ❌ {err}
        </div>
      )}
    </section>
  );
}
