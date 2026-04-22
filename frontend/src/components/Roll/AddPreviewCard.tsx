"use client";

/**
 * AddPreviewCard · 加仓预演卡片
 *
 * 结构：
 *   [ Header ]    intensity + mode + final_margin_usd 高亮
 *   [ Flow 行 ]   ideal → × intensity → × gates → final（含 shrink_reason）
 *   [ Gates 行 ]  A/B/C 三道闸门状态（actual vs required）
 *   [ Before/After 表 ] 关键指标前后对比
 *   [ 操作 ]     确认加仓（执行 add）· 用户覆盖加仓（override）· 说明
 *
 * 关键产品约束（再次强调在 UI 层）：
 *   - final_margin_usd < min_add_margin_usd → 按钮禁用，不允许直接执行
 *   - intensity = reject 时，不提供确认按钮；但若 action=add 上游没给 preview 则不显示此卡
 *   - 用户覆盖按钮前置 confirm 二次确认，并提示即将触发覆盖行为熔断统计
 */

import { useState } from "react";

import { useRollStore } from "@/stores/rollStore";
import type {
  RollPlan,
  RollSignal,
  SafetyGates,
  UserPosition,
} from "@/lib/rollTypes";
import { IntensityBadge } from "./SignalBadges";

interface Props {
  position: UserPosition;
  signal: RollSignal;
  plan: RollPlan | undefined;
  onExecuted?: () => void;
}

export default function AddPreviewCard({
  position,
  signal,
  plan,
  onExecuted,
}: Props) {
  const preview = signal.add_preview;
  const executeEvent = useRollStore((s) => s.executeEvent);
  const overrideAdd = useRollStore((s) => s.overrideAdd);
  const [busy, setBusy] = useState<null | "execute" | "override">(null);
  const [err, setErr] = useState<string | null>(null);

  if (!preview) {
    // action!=add 或引擎未生成预览，不渲染
    return null;
  }

  const gates = plan?.gates;
  const belowMinMargin =
    gates !== undefined && preview.final_margin_usd < gates.min_add_margin_usd;
  const allGatesPass =
    preview.gates.gate_a_pass &&
    preview.gates.gate_b_pass &&
    preview.gates.gate_c_pass;
  const canExecute =
    preview.intensity !== "reject" &&
    preview.final_margin_usd > 0 &&
    allGatesPass &&
    !belowMinMargin;

  const handleExecute = async () => {
    if (!canExecute || busy) return;
    setErr(null);
    setBusy("execute");
    try {
      await executeEvent(position.id, {
        kind: "add",
        price: signal.current_price,
        margin_delta_usd: preview.final_margin_usd,
        reason: `intensity=${preview.intensity}, conf=${signal.confidence_score.toFixed(1)}`,
        system_confidence: signal.confidence_score,
        system_action: signal.action,
      });
      onExecuted?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const handleOverride = async () => {
    if (busy) return;
    const warn = belowMinMargin
      ? `⚠️ 最终建议量 ${preview.final_margin_usd.toFixed(2)} USD 已低于最小加仓阈值（${gates?.min_add_margin_usd ?? "-"} USD），系统原本已放弃本次加仓。\n\n`
      : !allGatesPass
        ? "⚠️ 至少一道闸门未通过，系统原本不建议执行。\n\n"
        : "";
    const input = prompt(
      `${warn}确认要手动覆盖加仓吗？\n\n请输入本次加仓保证金（USD）：`,
      String(Math.max(preview.final_margin_usd, gates?.min_add_margin_usd ?? 10)),
    );
    if (!input) return;
    const margin = Number(input);
    if (!margin || margin <= 0) {
      setErr("覆盖加仓保证金必须 > 0");
      return;
    }
    setErr(null);
    setBusy("override");
    try {
      await overrideAdd(position.id, {
        price: signal.current_price,
        margin_delta_usd: margin,
        reason: "user_override",
        system_confidence: signal.confidence_score,
        system_action: signal.action,
      });
      onExecuted?.();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="rounded-lg border border-emerald-700/40 bg-emerald-950/20">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-emerald-900/40 px-4 py-2">
        <div className="flex items-baseline gap-3">
          <span className="text-[13px] font-semibold text-emerald-200">
            加仓预演
          </span>
          <span className="font-mono text-[11px] text-emerald-400">
            mode · {preview.mode}
          </span>
          <IntensityBadge intensity={preview.intensity} />
        </div>
        <div className="flex items-baseline gap-2">
          <span className="text-[11px] text-emerald-300/70">建议加仓</span>
          <span className="font-mono text-lg font-semibold text-emerald-200">
            {preview.final_margin_usd.toFixed(2)}
          </span>
          <span className="text-[11px] text-emerald-300/70">USD</span>
        </div>
      </header>

      {/* ── 量的流转 ── */}
      <div className="border-b border-emerald-900/30 px-4 py-3 text-[12px]">
        <FlowLine
          items={[
            { label: "理论量", value: preview.ideal_margin_usd, unit: "USD" },
            { label: `× 烈度 ${preview.intensity_multiplier.toFixed(2)}`, value: preview.after_intensity_usd, unit: "USD" },
            { label: "× 闸门", value: preview.final_margin_usd, unit: "USD", highlight: true },
          ]}
        />
        {preview.shrink_reason && (
          <div className="mt-2 rounded border border-amber-700/40 bg-amber-950/40 px-2 py-1 text-[11px] text-amber-200">
            ⚠ 缩量原因：{preview.shrink_reason}
          </div>
        )}
        {belowMinMargin && gates && (
          <div className="mt-2 rounded border border-rose-700/50 bg-rose-950/40 px-2 py-1 text-[11px] text-rose-200">
            ⛔ 最终量 {preview.final_margin_usd.toFixed(2)} USD {"<"} 最小加仓阈值 {gates.min_add_margin_usd.toFixed(2)} USD，系统已放弃本次加仓。
          </div>
        )}
      </div>

      {/* ── 三闸门 ── */}
      <div className="border-b border-emerald-900/30 px-4 py-3">
        <div className="mb-2 text-[11px] text-emerald-300/80">三道安全闸门</div>
        <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          <GateRow
            title="A · 均价距现价"
            pass={preview.gates.gate_a_pass}
            actual={preview.gates.gate_a_actual}
            required={preview.gates.gate_a_required}
            suffix="%"
            op="≥"
          />
          <GateRow
            title="B · 爆仓距现价"
            pass={preview.gates.gate_b_pass}
            actual={preview.gates.gate_b_actual}
            required={preview.gates.gate_b_required}
            suffix="%"
            op="≥"
          />
          <GateRow
            title="C · 有效杠杆"
            pass={preview.gates.gate_c_pass}
            actual={preview.gates.gate_c_actual}
            required={preview.gates.gate_c_required}
            suffix="x"
            op="≤"
          />
        </div>
      </div>

      {/* ── Before / After 对比 ── */}
      <div className="overflow-x-auto border-b border-emerald-900/30 px-4 py-3">
        <table className="w-full min-w-[540px] text-[12px]">
          <thead className="text-[10px] text-emerald-300/70">
            <tr className="text-left">
              <th className="pb-1">指标</th>
              <th className="pb-1 text-right">加仓前</th>
              <th className="pb-1 text-right">加仓后</th>
              <th className="pb-1 text-right">Δ</th>
            </tr>
          </thead>
          <tbody className="text-slate-200">
            <DiffRow
              label="均价"
              before={preview.before.avg_price}
              after={preview.after.avg_price}
              fmt={(n) => n.toLocaleString()}
            />
            <DiffRow
              label="均价距现价"
              before={preview.before.distance_to_price_pct}
              after={preview.after.distance_to_price_pct}
              fmt={(n) => `${n.toFixed(2)}%`}
            />
            <DiffRow
              label="有效杠杆"
              before={preview.before.effective_leverage}
              after={preview.after.effective_leverage}
              fmt={(n) => `${n.toFixed(2)}x`}
              worseIsHigher
            />
            <DiffRow
              label="爆仓价"
              before={preview.before.liq_price ?? NaN}
              after={preview.after.liq_price ?? NaN}
              fmt={(n) => (Number.isFinite(n) ? n.toLocaleString() : "-")}
            />
            <DiffRow
              label="爆仓距现价"
              before={preview.before.liq_distance_pct ?? NaN}
              after={preview.after.liq_distance_pct ?? NaN}
              fmt={(n) => (Number.isFinite(n) ? `${n.toFixed(2)}%` : "-")}
            />
            <DiffRow
              label="保证金"
              before={preview.before.margin_used_usd}
              after={preview.after.margin_used_usd}
              fmt={(n) => n.toFixed(2)}
              worseIsHigher
            />
            <DiffRow
              label="账户占比"
              before={preview.before.account_margin_pct}
              after={preview.after.account_margin_pct}
              fmt={(n) => `${n.toFixed(2)}%`}
              worseIsHigher
            />
          </tbody>
        </table>
      </div>

      {/* ── 建议新止损 ── */}
      {preview.suggested_new_sl !== null && (
        <div className="border-b border-emerald-900/30 px-4 py-2 text-[11px] text-sky-200">
          🔰 建议同时把止损移到{" "}
          <span className="font-mono">
            {preview.suggested_new_sl!.toLocaleString()}
          </span>
          （执行加仓成功后请在 交易所 + 详情页 手动同步）
        </div>
      )}

      {/* ── 操作区 ── */}
      <footer className="flex flex-wrap items-center justify-between gap-2 px-4 py-3">
        <div className="text-[10px] text-slate-500">
          ⚠ 引擎不会替你下单。点击「执行加仓」只是在本地记录 RollEvent，请先在交易所完成实际加仓。
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleOverride}
            disabled={busy !== null}
            className="rounded-md border border-amber-700/60 bg-amber-950/50 px-3 py-1.5 text-[12px] text-amber-200 transition hover:bg-amber-900/50 disabled:opacity-50"
            title="即使系统不建议也要加仓（会被覆盖熔断系统统计）"
          >
            {busy === "override" ? "覆盖中…" : "用户覆盖加仓"}
          </button>
          <button
            onClick={handleExecute}
            disabled={!canExecute || busy !== null}
            className="rounded-md bg-emerald-600 px-4 py-1.5 text-[12px] font-medium text-white transition hover:bg-emerald-500 disabled:cursor-not-allowed disabled:bg-slate-700 disabled:text-slate-400"
          >
            {busy === "execute" ? "记录中…" : `执行加仓 ${preview.final_margin_usd.toFixed(0)} USD`}
          </button>
        </div>
      </footer>

      {err && (
        <div className="border-t border-rose-700/40 bg-rose-950/40 px-4 py-2 text-[11px] text-rose-200">
          ❌ {err}
        </div>
      )}
    </section>
  );
}

// ── 子组件：流转线 ──────────────────────────────────

function FlowLine({
  items,
}: {
  items: { label: string; value: number; unit: string; highlight?: boolean }[];
}) {
  return (
    <div className="flex flex-wrap items-center gap-2">
      {items.map((it, i) => (
        <div key={i} className="flex items-center gap-2">
          <div
            className={[
              "rounded border px-2 py-1",
              it.highlight
                ? "border-emerald-500 bg-emerald-900/40"
                : "border-slate-700 bg-slate-900/60",
            ].join(" ")}
          >
            <div className="text-[9px] text-slate-400">{it.label}</div>
            <div
              className={[
                "font-mono text-[13px]",
                it.highlight ? "text-emerald-200" : "text-slate-200",
              ].join(" ")}
            >
              {it.value.toFixed(2)} {it.unit}
            </div>
          </div>
          {i < items.length - 1 && (
            <span className="text-slate-500">→</span>
          )}
        </div>
      ))}
    </div>
  );
}

// ── 子组件：闸门行 ─────────────────────────────────

function GateRow({
  title,
  pass,
  actual,
  required,
  suffix,
  op,
}: {
  title: string;
  pass: boolean;
  actual: number;
  required: number;
  suffix: string;
  op: "≥" | "≤";
}) {
  return (
    <div
      className={[
        "rounded border px-3 py-2",
        pass
          ? "border-emerald-700/60 bg-emerald-950/40"
          : "border-rose-700/60 bg-rose-950/40",
      ].join(" ")}
    >
      <div className="flex items-center justify-between">
        <span className="text-[11px] text-slate-200">{title}</span>
        <span className="font-mono text-[12px]">
          {pass ? (
            <span className="text-emerald-300">✓ 通过</span>
          ) : (
            <span className="text-rose-300">✗ 未通过</span>
          )}
        </span>
      </div>
      <div className="mt-1 font-mono text-[12px] text-slate-300">
        <span className={pass ? "text-emerald-200" : "text-rose-200"}>
          {actual.toFixed(2)}
          {suffix}
        </span>
        <span className="mx-1 text-slate-500">{op}</span>
        <span className="text-slate-300">
          {required.toFixed(2)}
          {suffix}
        </span>
      </div>
    </div>
  );
}

// ── 子组件：对比行 ──────────────────────────────────

function DiffRow({
  label,
  before,
  after,
  fmt,
  worseIsHigher,
}: {
  label: string;
  before: number;
  after: number;
  fmt: (n: number) => string;
  worseIsHigher?: boolean;
}) {
  const delta = after - before;
  const valid = Number.isFinite(before) && Number.isFinite(after);
  let tone = "text-slate-400";
  if (valid && Math.abs(delta) > 1e-9) {
    const worse = worseIsHigher ? delta > 0 : delta < 0;
    tone = worse ? "text-rose-300" : "text-emerald-300";
  }
  return (
    <tr className="border-t border-slate-800/70">
      <td className="py-1 text-slate-400">{label}</td>
      <td className="py-1 text-right font-mono">{fmt(before)}</td>
      <td className="py-1 text-right font-mono">{fmt(after)}</td>
      <td className={`py-1 text-right font-mono ${tone}`}>
        {valid
          ? `${delta >= 0 ? "+" : ""}${fmt(Math.abs(delta)).replace(/^-/, "")}${delta < 0 ? " ↓" : delta > 0 ? " ↑" : ""}`
          : "-"}
      </td>
    </tr>
  );
}

// 供未来引用：SafetyGates 现阶段未直接使用，保留防 treeshake
export type { SafetyGates };
