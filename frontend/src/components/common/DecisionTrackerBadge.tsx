"use client";

import { useEffect, useRef, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type {
  DecisionRecord,
  DecisionStatus,
  DecisionSummary,
  OverallHealth,
} from "@/lib/types";

/**
 * P1.6 · DecisionTrackerBadge · D1-D17 架构决策全景灯
 *
 * 职责：
 *   - 紧凑徽章显示 D1-D17 整体健康度（ok / warn / failed 计数）
 *   - 点击展开 Popover：17 个决策点详情表格（状态 / metrics / 最近更新）
 *   - 30s 轮询 /api/health/decisions
 */

const POLL_INTERVAL_MS = 30_000;

const STATUS_STYLE: Record<DecisionStatus, { dot: string; text: string; label: string }> = {
  ok:          { dot: "bg-green-400",  text: "text-green-300",  label: "✓" },
  warn:        { dot: "bg-yellow-400", text: "text-yellow-300", label: "!" },
  failed:      { dot: "bg-red-500",    text: "text-red-300",    label: "✗" },
  in_progress: { dot: "bg-blue-400",   text: "text-blue-300",   label: "…" },
  pending:     { dot: "bg-slate-500",  text: "text-slate-400",  label: "·" },
  skipped:     { dot: "bg-slate-600",  text: "text-slate-500",  label: "-" },
};

const OVERALL_STYLE: Record<OverallHealth, { emoji: string; color: string; text: string }> = {
  all_ok:    { emoji: "🟢", color: "text-green-300",  text: "全部正常" },
  partial:   { emoji: "🔵", color: "text-blue-300",   text: "部分待启动" },
  degraded:  { emoji: "🟡", color: "text-yellow-300", text: "部分降级" },
  unhealthy: { emoji: "🔴", color: "text-red-300",    text: "存在故障" },
};

export default function DecisionTrackerBadge() {
  const [summary, setSummary] = useState<DecisionSummary | null>(null);
  const [open, setOpen] = useState(false);
  const [lastErr, setLastErr] = useState("");
  const [nowSec, setNowSec] = useState<number>(0);
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;

    const fetchOnce = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/health/decisions`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j: DecisionSummary = await r.json();
        if (cancelled) return;
        setSummary(j);
        setNowSec(Math.floor(Date.now() / 1000));
        setLastErr("");
      } catch (e) {
        if (cancelled) return;
        setLastErr(e instanceof Error ? e.message : "fetch error");
      }
    };

    fetchOnce();
    const t = setInterval(fetchOnce, POLL_INTERVAL_MS);
    // 每 10s tick 一次 nowSec，让 Popover 里的 "xxs ago" 活起来
    const tick = setInterval(() => {
      if (!cancelled) setNowSec(Math.floor(Date.now() / 1000));
    }, 10_000);
    return () => {
      cancelled = true;
      clearInterval(t);
      clearInterval(tick);
    };
  }, []);

  // 点击外部关闭 popover
  useEffect(() => {
    if (!open) return;
    const onClick = (ev: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(ev.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  if (!summary) {
    return (
      <span className="text-slate-600" title={lastErr || "等待决策追踪器..."}>
        ⏳ D1-D17
      </span>
    );
  }

  const counts = countByStatus(summary.decisions);
  const overall = OVERALL_STYLE[summary.overall_health] ?? OVERALL_STYLE.partial;

  return (
    <div ref={containerRef} className="relative inline-flex">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 px-2 py-0.5 rounded transition-colors ${
          open
            ? "bg-slate-700/70 text-slate-200"
            : "hover:bg-slate-800/70 text-slate-400"
        }`}
        title={`D1-D17 · ${overall.text}`}
      >
        <span>{overall.emoji}</span>
        <span className={`font-semibold ${overall.color}`}>D1-D17</span>
        <span className="text-[10.5px] text-slate-400">
          <span className="text-green-400">{counts.ok}</span>
          <span className="text-slate-600">/</span>
          <span>{summary.decisions.length}</span>
          {counts.warn > 0 && (
            <span className="ml-1 text-yellow-400">!{counts.warn}</span>
          )}
          {counts.failed > 0 && (
            <span className="ml-1 text-red-400">✗{counts.failed}</span>
          )}
        </span>
      </button>

      {open && (
        <DecisionPopover
          summary={summary}
          counts={counts}
          nowSec={nowSec}
          onClose={() => setOpen(false)}
        />
      )}
    </div>
  );
}

function DecisionPopover({
  summary,
  counts,
  nowSec,
  onClose,
}: {
  summary: DecisionSummary;
  counts: Counts;
  nowSec: number;
  onClose: () => void;
}) {
  const updatedAgo = Math.max(0, nowSec - summary.ts);

  return (
    <div
      className="absolute bottom-full mb-2 right-0 w-[640px] max-w-[95vw] max-h-[70vh] overflow-hidden flex flex-col rounded-lg border border-slate-700 bg-slate-900 shadow-2xl z-50"
      role="dialog"
    >
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-700">
        <div className="flex items-center gap-2 text-xs">
          <span className="font-semibold text-slate-100">🏗️ D1-D17 架构决策落地</span>
          <span className="text-slate-500">· {updatedAgo}s 前</span>
        </div>
        <div className="flex items-center gap-3 text-[10.5px] text-slate-400">
          <span>
            <span className="text-green-400 font-mono">{counts.ok}</span> 正常
          </span>
          <span>
            <span className="text-yellow-400 font-mono">{counts.warn}</span> 警告
          </span>
          <span>
            <span className="text-red-400 font-mono">{counts.failed}</span> 故障
          </span>
          <span>
            <span className="text-slate-400 font-mono">{counts.pending}</span> 待启
          </span>
          <button
            type="button"
            onClick={onClose}
            className="ml-2 text-slate-500 hover:text-slate-200"
            aria-label="关闭"
          >
            ✕
          </button>
        </div>
      </div>

      <div className="overflow-y-auto">
        <table className="w-full text-[11px] text-left">
          <thead className="sticky top-0 bg-slate-900 border-b border-slate-700/70 text-slate-500">
            <tr>
              <th className="px-3 py-1.5 w-12 font-medium">ID</th>
              <th className="px-2 py-1.5 w-8 font-medium">状</th>
              <th className="px-2 py-1.5 font-medium">决策点</th>
              <th className="px-2 py-1.5 font-medium hidden sm:table-cell">指标</th>
              <th className="px-3 py-1.5 w-16 font-medium text-right">更新</th>
            </tr>
          </thead>
          <tbody>
            {summary.decisions.map((d) => (
              <DecisionRow key={d.id} record={d} nowSec={nowSec} />
            ))}
          </tbody>
        </table>
      </div>

      <div className="px-4 py-1.5 border-t border-slate-700/50 text-[10px] text-slate-500">
        每 30s 自动刷新 · 点击行外区域关闭
      </div>
    </div>
  );
}

function DecisionRow({
  record,
  nowSec,
}: {
  record: DecisionRecord;
  nowSec: number;
}) {
  const style = STATUS_STYLE[record.status] ?? STATUS_STYLE.pending;
  const ago = record.last_update_ts
    ? formatAgo(nowSec - record.last_update_ts)
    : "-";
  const metrics = compactMetrics(record.metrics);

  return (
    <tr className="border-b border-slate-800/70 hover:bg-slate-800/40">
      <td className="px-3 py-1.5 font-mono text-slate-400">{record.id}</td>
      <td className="px-2 py-1.5">
        <span
          className="inline-flex items-center justify-center w-4 h-4 rounded-full"
          title={record.status}
        >
          <span className={`w-2 h-2 rounded-full ${style.dot}`} />
        </span>
      </td>
      <td className="px-2 py-1.5">
        <div className="text-slate-200 font-medium leading-tight">{record.title}</div>
        {record.detail && (
          <div
            className="text-[10px] text-slate-500 mt-0.5 truncate max-w-[320px]"
            title={record.detail}
          >
            {record.detail}
          </div>
        )}
      </td>
      <td
        className="px-2 py-1.5 text-[10.5px] text-slate-400 font-mono hidden sm:table-cell"
        title={JSON.stringify(record.metrics)}
      >
        {metrics || "—"}
      </td>
      <td className={`px-3 py-1.5 text-right text-[10.5px] ${style.text}`}>{ago}</td>
    </tr>
  );
}

// ─── helpers ────────────────────────────────────────────

type Counts = {
  ok: number;
  warn: number;
  failed: number;
  pending: number;
  in_progress: number;
  skipped: number;
};

function countByStatus(list: DecisionRecord[]): Counts {
  const c: Counts = { ok: 0, warn: 0, failed: 0, pending: 0, in_progress: 0, skipped: 0 };
  for (const d of list) c[d.status] = (c[d.status] ?? 0) + 1;
  return c;
}

function compactMetrics(metrics: Record<string, unknown>): string {
  const entries = Object.entries(metrics ?? {});
  if (entries.length === 0) return "";
  const formatted = entries
    .slice(0, 4)
    .map(([k, v]) => `${k}=${fmtVal(v)}`)
    .join(" · ");
  const suffix = entries.length > 4 ? ` +${entries.length - 4}` : "";
  return formatted + suffix;
}

function fmtVal(v: unknown): string {
  if (v === null || v === undefined) return "-";
  if (typeof v === "number") {
    if (Number.isInteger(v)) return String(v);
    return v.toFixed(3).replace(/\.?0+$/, "");
  }
  if (typeof v === "boolean") return v ? "Y" : "N";
  const s = String(v);
  return s.length > 18 ? s.slice(0, 16) + "…" : s;
}

function formatAgo(sec: number): string {
  if (sec < 0 || !Number.isFinite(sec)) return "-";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h`;
  return `${Math.floor(sec / 86400)}d`;
}
