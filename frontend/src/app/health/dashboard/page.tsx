"use client";

/**
 * P2.2 · Decision Health Dashboard
 *
 * - 全量 17 项决策点表格（ok/warn/fail/pending）
 * - 非健康项置顶 + 详情展开
 * - 事件时间线
 * - 自动 5s 刷新
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type {
  HealthEvent,
  HealthEventKind,
  HealthSummaryResponse,
} from "@/lib/types";

const KIND_LABEL: Record<HealthEventKind, string> = {
  degrade: "降级",
  escalate: "升级",
  recover: "恢复",
  "de-escalate": "回落",
  change: "变化",
};

const OVERALL_COLOR: Record<string, string> = {
  ok: "text-emerald-300 border-emerald-500/40 bg-emerald-500/10",
  warn: "text-amber-300 border-amber-500/40 bg-amber-500/10",
  fail: "text-rose-300 border-rose-500/40 bg-rose-500/10",
  pending: "text-slate-300 border-slate-500/40 bg-slate-500/10",
};

type AllDecision = {
  id: string;
  title: string;
  owner_module: string;
  success_criteria: string;
  status: string;
  detail: string;
  metrics: Record<string, unknown>;
  last_update_ts: number;
  ok_count: number;
  warn_count: number;
  fail_count: number;
};

export default function HealthDashboardPage() {
  const [summary, setSummary] = useState<HealthSummaryResponse | null>(null);
  const [all, setAll] = useState<AllDecision[]>([]);
  const [err, setErr] = useState<string>("");

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const [s, a] = await Promise.all([
          fetch(`${API_BASE}/api/health/summary`, { cache: "no-store" }),
          fetch(`${API_BASE}/api/health/decisions`, { cache: "no-store" }),
        ]);
        if (!s.ok || !a.ok) throw new Error(`HTTP ${s.status}/${a.status}`);
        const sJson = (await s.json()) as HealthSummaryResponse;
        const aJson = await a.json();
        if (!alive) return;
        setSummary(sJson);
        setAll((aJson.decisions || []) as AllDecision[]);
        setErr("");
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
    };
    tick();
    const t = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (err && !summary) {
    return (
      <div className="p-6 text-rose-300">
        加载失败：{err}
        <div className="mt-4">
          <Link href="/" className="text-sky-400 underline">
            返回首页
          </Link>
        </div>
      </div>
    );
  }

  if (!summary) {
    return <div className="p-6 text-slate-400">加载中…</div>;
  }

  // 非健康项置顶
  const sorted = [...all].sort((a, b) => {
    const ord = (s: string) =>
      ({ failed: 0, warn: 1, pending: 2, ok: 3 }[s] ?? 4);
    return ord(a.status) - ord(b.status) || a.id.localeCompare(b.id);
  });

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-4">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-sky-400 text-sm hover:underline">
              ← 返回首页
            </Link>
            <h1 className="text-lg font-semibold">D1-D17 决策健康面板</h1>
          </div>
          <div
            className={`rounded-md border px-3 py-1 text-sm ${
              OVERALL_COLOR[summary.overall] || OVERALL_COLOR.pending
            }`}
          >
            整体：{summary.overall.toUpperCase()}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-6">
        {/* 统计卡 */}
        <section className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <StatCard label="健康" value={summary.counts.green} tone="emerald" />
          <StatCard label="告警" value={summary.counts.yellow} tone="amber" />
          <StatCard label="失败" value={summary.counts.red} tone="rose" />
          <StatCard
            label="待启动"
            value={summary.counts.pending}
            tone="slate"
          />
        </section>

        {/* 降级项 */}
        {summary.degraded.length > 0 && (
          <section className="mt-6">
            <h2 className="mb-2 text-sm font-semibold text-slate-300">
              非健康项（{summary.degraded.length}）
            </h2>
            <div className="grid gap-2 md:grid-cols-2">
              {summary.degraded.map((d) => (
                <div
                  key={d.id}
                  className="rounded-md border border-slate-800 bg-slate-900/60 p-3"
                >
                  <div className="flex items-center justify-between">
                    <div className="text-sm font-semibold">
                      <span
                        className={
                          d.status === "failed"
                            ? "text-rose-400"
                            : "text-amber-400"
                        }
                      >
                        ● {d.id}
                      </span>{" "}
                      <span className="text-slate-300">{d.title}</span>
                    </div>
                    <span className="text-[10px] text-slate-500">
                      {d.stuck_sec > 0 ? `已停滞 ${d.stuck_sec}s` : "刚发生"}
                    </span>
                  </div>
                  {d.detail && (
                    <div className="mt-1 text-[11px] text-slate-400">
                      {d.detail}
                    </div>
                  )}
                  {Object.keys(d.metrics).length > 0 && (
                    <div className="mt-1 text-[10px] text-slate-500">
                      {Object.entries(d.metrics)
                        .slice(0, 4)
                        .map(([k, v]) => `${k}=${String(v)}`)
                        .join(" · ")}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </section>
        )}

        {/* 全量表 */}
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-300">全量 D1-D17</h2>
          <div className="overflow-hidden rounded-md border border-slate-800">
            <table className="w-full text-[11px]">
              <thead className="bg-slate-900/70 text-slate-400">
                <tr>
                  <th className="px-3 py-2 text-left">ID</th>
                  <th className="px-3 py-2 text-left">标题</th>
                  <th className="px-3 py-2 text-left">模块</th>
                  <th className="px-3 py-2 text-left">状态</th>
                  <th className="px-3 py-2 text-right">ok / warn / fail</th>
                  <th className="px-3 py-2 text-left">近因</th>
                  <th className="px-3 py-2 text-right">上次更新</th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((d) => (
                  <tr
                    key={d.id}
                    className="border-t border-slate-800 hover:bg-slate-800/30"
                  >
                    <td className="px-3 py-2 font-mono">{d.id}</td>
                    <td className="px-3 py-2">{d.title}</td>
                    <td className="px-3 py-2 text-slate-500">
                      {d.owner_module}
                    </td>
                    <td className="px-3 py-2">
                      <StatusChip status={d.status} />
                    </td>
                    <td className="px-3 py-2 text-right text-slate-500">
                      <span className="text-emerald-400">{d.ok_count}</span>
                      <span className="mx-0.5">·</span>
                      <span className="text-amber-400">{d.warn_count}</span>
                      <span className="mx-0.5">·</span>
                      <span className="text-rose-400">{d.fail_count}</span>
                    </td>
                    <td className="px-3 py-2 text-slate-400">{d.detail || "—"}</td>
                    <td className="px-3 py-2 text-right text-slate-600">
                      {d.last_update_ts > 0
                        ? new Date(d.last_update_ts * 1000).toLocaleTimeString(
                            "zh-CN",
                            { hour12: false },
                          )
                        : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        {/* 事件时间线 */}
        <section className="mt-6">
          <h2 className="mb-2 text-sm font-semibold text-slate-300">
            最近事件（{summary.events.length}）
          </h2>
          {summary.events.length === 0 ? (
            <div className="rounded-md border border-slate-800 bg-slate-900/50 p-4 text-[11px] text-slate-500">
              暂无降级事件
            </div>
          ) : (
            <div className="space-y-1">
              {summary.events.map((e, i) => (
                <EventRow key={`${e.ts}-${e.id}-${i}`} evt={e} />
              ))}
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function StatCard({
  label,
  value,
  tone,
}: {
  label: string;
  value: number;
  tone: "emerald" | "amber" | "rose" | "slate";
}) {
  const toneCls: Record<string, string> = {
    emerald: "text-emerald-300 bg-emerald-500/10 border-emerald-500/30",
    amber: "text-amber-300 bg-amber-500/10 border-amber-500/30",
    rose: "text-rose-300 bg-rose-500/10 border-rose-500/30",
    slate: "text-slate-300 bg-slate-500/10 border-slate-500/30",
  };
  return (
    <div className={`rounded-md border p-3 ${toneCls[tone]}`}>
      <div className="text-[11px] opacity-80">{label}</div>
      <div className="mt-1 text-2xl font-bold">{value}</div>
    </div>
  );
}

function StatusChip({ status }: { status: string }) {
  const map: Record<string, { cls: string; text: string }> = {
    ok: { cls: "text-emerald-400", text: "OK" },
    warn: { cls: "text-amber-400", text: "WARN" },
    failed: { cls: "text-rose-400", text: "FAIL" },
    pending: { cls: "text-slate-500", text: "PENDING" },
  };
  const s = map[status] || map.pending;
  return <span className={`font-semibold ${s.cls}`}>● {s.text}</span>;
}

function EventRow({ evt }: { evt: HealthEvent }) {
  const dt = new Date(evt.ts * 1000);
  const label = KIND_LABEL[evt.kind] || evt.kind;
  const color =
    evt.kind === "degrade" || evt.kind === "escalate"
      ? "text-rose-400"
      : evt.kind === "recover"
      ? "text-emerald-400"
      : "text-slate-300";
  return (
    <div className="flex items-center justify-between rounded-md border border-slate-800 bg-slate-900/40 px-3 py-1.5 text-[11px]">
      <div>
        <span className={`font-semibold ${color}`}>
          {label} · {evt.id}
        </span>
        <span className="ml-2 text-slate-400">{evt.title}</span>
        <span className="ml-2 text-slate-600">
          {evt.from} → {evt.to}
        </span>
        {evt.detail && (
          <span className="ml-2 text-slate-500">· {evt.detail}</span>
        )}
      </div>
      <div className="text-slate-600">
        {dt.toLocaleTimeString("zh-CN", { hour12: false })}
      </div>
    </div>
  );
}
