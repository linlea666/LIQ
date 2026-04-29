/**
 * W4-T1 阶段 4 · SweepWatchTracePanel（trace 抽屉）
 *
 * 用途：
 *   - 展示后端 sweep_watch_engine 每次 build 产出的算法运行轨迹
 *   - 让用户能快速回答"为什么这个 phase / 这个分？"
 *   - 与后端 archiver（data/sweep_watch/{coin}/{date}.jsonl）配合做后验
 *
 * 布局：右侧 480px 宽抽屉，按 side 分组展示 trace_log。
 * 每条 trace：步骤名 + 命中规则 chip + 注释 + inputs/output（可折叠 JSON）。
 */
"use client";

import { useState } from "react";
import type { BrainSweepWatch, SweepSide, SweepWatchTraceEntry } from "@/lib/types";

interface Props {
  open: boolean;
  onClose: () => void;
  sweepWatch: BrainSweepWatch;
}

// ─────────────────────────────────────────────────────────────────────
// 步骤名 → 中文短标签 + tooltip 解释
// ─────────────────────────────────────────────────────────────────────
const STEP_META: Record<string, { label: string; explain: string }> = {
  select_representative: {
    label: "选代表区",
    explain: "在该侧 zones 中按 |distance| 升序选最近的强角色 zone（spot_defense / contested / liquidation_magnet / futures_target）",
  },
  phase_decision: {
    label: "态机判定",
    explain: "5 态机决策：waiting / approaching / in_sweep / swept_reclaiming / swept_continuing",
  },
  sweep_attractiveness: {
    label: "扫单吸引",
    explain: "直接复用 zone.sweep_attractiveness（不重打分）",
  },
  reversal_potential: {
    label: "反转潜力",
    explain: "派生公式 = 0.40×strength + 0.20×(1-fragility) + 0.20×data_confidence + 0.20×cvd_against",
  },
  continuation_risk: {
    label: "延续风险",
    explain: "派生公式 = 0.35×break_through_risk + 0.25×sweep_attractiveness + 0.20×cvd_alignment + 0.10×(1-data) + 0.10×fragility",
  },
  triggers_invalidations: {
    label: "触发/失效",
    explain: "按 (side, phase) 模板生成 ≤3 触发观察 + ≤2 失效条件",
  },
};

// ─────────────────────────────────────────────────────────────────────
// JSON pretty 折叠
// ─────────────────────────────────────────────────────────────────────
function JsonBlock({ value, label }: { value: unknown; label: string }) {
  const [open, setOpen] = useState(false);
  if (value === null || value === undefined) return null;
  // 简单值（标量）直接显示，不需要折叠
  const isScalar = typeof value !== "object";
  if (isScalar) {
    return (
      <div className="flex items-baseline gap-1.5 text-[10px]">
        <span className="text-slate-500">{label}:</span>
        <span className="tabular-nums text-slate-300">{String(value)}</span>
      </div>
    );
  }
  // 复杂对象：默认折叠
  let pretty = "";
  try {
    pretty = JSON.stringify(value, null, 2);
  } catch {
    pretty = "<unserializable>";
  }
  return (
    <div className="text-[10px]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-slate-500 hover:text-sky-300"
      >
        {open ? "▾" : "▸"} {label}（{Array.isArray(value) ? `${value.length} 项` : `${Object.keys(value as object).length} 字段`}）
      </button>
      {open && (
        <pre className="mt-1 max-h-64 overflow-auto rounded bg-slate-950/80 p-2 text-[10px] leading-snug text-slate-300">
          {pretty}
        </pre>
      )}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 单条 trace 卡片
// ─────────────────────────────────────────────────────────────────────
function TraceCard({ entry, idx }: { entry: SweepWatchTraceEntry; idx: number }) {
  const meta = STEP_META[entry.step] ?? { label: entry.step, explain: "" };
  return (
    <div className="rounded border border-slate-800 bg-slate-900/40 p-2">
      <div className="mb-1 flex items-center justify-between">
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] tabular-nums text-slate-600">#{idx + 1}</span>
          <span
            className="text-[11px] font-medium text-slate-200"
            title={meta.explain || entry.step}
          >
            {meta.label}
          </span>
          {entry.rule_hit && (
            <span
              className="rounded border border-sky-700/60 bg-sky-950/30 px-1 py-px text-[9px] text-sky-300"
              title="命中的判定规则"
            >
              {entry.rule_hit}
            </span>
          )}
        </div>
        <span className="text-[9px] text-slate-600">{entry.step}</span>
      </div>
      {entry.notes && (
        <div className="mb-1 text-[10px] leading-snug text-slate-400">{entry.notes}</div>
      )}
      <div className="space-y-1">
        <JsonBlock label="output" value={entry.output} />
        <JsonBlock label="inputs" value={entry.inputs} />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 抽屉主体
// ─────────────────────────────────────────────────────────────────────
export default function SweepWatchTracePanel({ open, onClose, sweepWatch }: Props) {
  const [activeSide, setActiveSide] = useState<SweepSide | "all">("all");
  const [copyStatus, setCopyStatus] = useState<"idle" | "ok" | "fail">("idle");

  if (!open) return null;

  const traces = sweepWatch.trace_log ?? [];
  const filtered = activeSide === "all"
    ? traces
    : traces.filter((e) => e.side === activeSide);

  const belowCount = traces.filter((e) => e.side === "below").length;
  const aboveCount = traces.filter((e) => e.side === "above").length;

  // 复制完整 sweep_watch JSON 到剪贴板。
  // 兼容 3 种环境：
  //   1. HTTPS / localhost：navigator.clipboard.writeText（异步 Promise，必须 await）
  //   2. HTTP IP / 旧浏览器：fallback 到 document.execCommand('copy') + 临时 textarea
  //   3. 全部失败：状态 fail（按钮短暂变红，让用户感知）
  const handleCopyJson = async () => {
    const text = JSON.stringify(sweepWatch, null, 2);
    let ok = false;
    try {
      if (
        typeof window !== "undefined"
        && window.isSecureContext
        && navigator.clipboard?.writeText
      ) {
        await navigator.clipboard.writeText(text);
        ok = true;
      } else {
        // 非安全上下文（HTTP/IP）→ legacy fallback
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.top = "-9999px";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        ta.setSelectionRange(0, text.length);
        ok = document.execCommand("copy");
        document.body.removeChild(ta);
      }
    } catch {
      ok = false;
    }
    setCopyStatus(ok ? "ok" : "fail");
    window.setTimeout(() => setCopyStatus("idle"), 1800);
  };

  const copyLabel =
    copyStatus === "ok" ? "已复制 ✓"
    : copyStatus === "fail" ? "复制失败"
    : "复制 JSON";
  const copyCls =
    copyStatus === "ok"
      ? "border-emerald-600 bg-emerald-950/40 text-emerald-300"
    : copyStatus === "fail"
      ? "border-rose-600 bg-rose-950/40 text-rose-300"
    : "border-slate-700 bg-slate-800/60 text-slate-300 hover:border-sky-700 hover:text-sky-300";

  return (
    <>
      {/* 半透明遮罩 */}
      <div
        className="fixed inset-0 z-40 bg-black/40 backdrop-blur-sm"
        onClick={onClose}
      />
      {/* 右侧抽屉 */}
      <aside className="fixed right-0 top-0 z-50 flex h-full w-[480px] max-w-[100vw] flex-col border-l border-slate-800 bg-slate-950 shadow-2xl">
        {/* 标题栏 */}
        <div className="flex items-center justify-between border-b border-slate-800 px-4 py-3">
          <div>
            <div className="text-[13px] font-semibold text-slate-200">扫单观察 · 运行轨迹</div>
            <div className="text-[10px] text-slate-500">
              {sweepWatch.coin} · {sweepWatch.ts_iso} · {traces.length} 条记录
            </div>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={handleCopyJson}
              className={`rounded border px-2 py-1 text-[10px] transition-colors ${copyCls}`}
              title="复制完整 sweep_watch JSON 到剪贴板（含 trace）"
            >
              {copyLabel}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="rounded border border-slate-700 bg-slate-800/60 px-2 py-1 text-[11px] text-slate-300 hover:border-rose-700 hover:text-rose-300"
              title="关闭"
            >
              关闭
            </button>
          </div>
        </div>

        {/* side 过滤 tabs */}
        <div className="flex gap-1.5 border-b border-slate-800 px-4 py-2">
          {(["all", "below", "above"] as const).map((s) => {
            const count = s === "all" ? traces.length : s === "below" ? belowCount : aboveCount;
            const label = s === "all" ? "全部" : s === "below" ? "下方" : "上方";
            const active = activeSide === s;
            return (
              <button
                key={s}
                type="button"
                onClick={() => setActiveSide(s)}
                className={`rounded border px-2 py-1 text-[10px] ${
                  active
                    ? "border-sky-600 bg-sky-950/40 text-sky-200"
                    : "border-slate-700 bg-slate-800/40 text-slate-400 hover:text-slate-200"
                }`}
              >
                {label} ({count})
              </button>
            );
          })}
        </div>

        {/* trace 列表 */}
        <div className="flex-1 space-y-2 overflow-auto px-4 py-3">
          {filtered.length === 0 ? (
            <div className="rounded border border-dashed border-slate-700 bg-slate-900/30 p-4 text-center text-[11px] text-slate-500">
              当前过滤无记录
            </div>
          ) : (
            filtered.map((entry, i) => <TraceCard key={i} entry={entry} idx={i} />)
          )}
        </div>

        {/* 底部说明 */}
        <div className="border-t border-slate-800 bg-slate-900/40 px-4 py-2 text-[10px] leading-snug text-slate-500">
          每条 trace 对应一次决策步骤。后端已落盘到 data/sweep_watch/{sweepWatch.coin}/YYYYMMDD.jsonl 供后验打分。
        </div>
      </aside>
    </>
  );
}
