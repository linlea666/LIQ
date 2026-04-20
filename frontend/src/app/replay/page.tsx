"use client";

/**
 * P2.4 · 历史快照回放时间轴
 *
 * 左：时间轴列表（按 coin + 近 N 天筛选）
 * 右：选中帧的四件套（AISnapshot / ExecutionPlan / AITraderReport / FinalDecision）
 *
 * 用途：事后复盘 · prompt 迭代基准 · 检查信号是否被价格走势印证
 */

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type {
  ReplayFrame,
  ReplayFrameResponse,
  ReplayListItem,
  ReplayListResponse,
} from "@/lib/types";

const COINS = ["BTC", "ETH", "SOL"];
const RANGE_OPTIONS: { label: string; seconds: number }[] = [
  { label: "近 24h", seconds: 86400 },
  { label: "近 3 天", seconds: 86400 * 3 },
  { label: "近 7 天", seconds: 86400 * 7 },
  { label: "近 30 天", seconds: 86400 * 30 },
];

export default function ReplayPage() {
  const [coin, setCoin] = useState("BTC");
  const [rangeSec, setRangeSec] = useState(86400 * 3);
  const [items, setItems] = useState<ReplayListItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState("");
  const [selectedTs, setSelectedTs] = useState<number | null>(null);
  const [frame, setFrame] = useState<ReplayFrame | null>(null);
  const [frameLoading, setFrameLoading] = useState(false);

  // 列表加载
  useEffect(() => {
    let alive = true;
    const run = async () => {
      setLoading(true);
      setErr("");
      try {
        const since = Math.floor(Date.now() / 1000) - rangeSec;
        const r = await fetch(
          `${API_BASE}/api/replay/list?coin=${coin}&since_ts=${since}&limit=500`,
          { cache: "no-store" },
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as ReplayListResponse;
        if (!alive) return;
        setItems(j.items || []);
        // 默认选第一帧
        if ((j.items || []).length > 0) {
          setSelectedTs(j.items[0].ts);
        } else {
          setSelectedTs(null);
          setFrame(null);
        }
      } catch (e) {
        if (alive) setErr((e as Error).message);
      } finally {
        if (alive) setLoading(false);
      }
    };
    run();
    return () => {
      alive = false;
    };
  }, [coin, rangeSec]);

  // 详情加载
  useEffect(() => {
    if (selectedTs === null) {
      setFrame(null);
      return;
    }
    let alive = true;
    const run = async () => {
      setFrameLoading(true);
      try {
        const r = await fetch(
          `${API_BASE}/api/replay/frame?coin=${coin}&ts=${selectedTs}`,
          { cache: "no-store" },
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as ReplayFrameResponse;
        if (alive) setFrame(j.frame);
      } catch (e) {
        if (alive) setErr((e as Error).message);
      } finally {
        if (alive) setFrameLoading(false);
      }
    };
    run();
    return () => {
      alive = false;
    };
  }, [coin, selectedTs]);

  const stats = useMemo(() => {
    const withPlan = items.filter((i) => i.has_plan).length;
    const withAI = items.filter((i) => i.has_ai_report).length;
    const withFinal = items.filter((i) => i.has_final).length;
    return { withPlan, withAI, withFinal };
  }, [items]);

  return (
    <div className="min-h-screen bg-slate-950 text-slate-200">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur">
        <div className="mx-auto flex max-w-7xl items-center justify-between px-6 py-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-sm text-sky-400 hover:underline">
              ← 返回首页
            </Link>
            <h1 className="text-lg font-semibold">📼 历史快照回放</h1>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={coin}
              onChange={(e) => setCoin(e.target.value)}
              className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm"
            >
              {COINS.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
            <select
              value={rangeSec}
              onChange={(e) => setRangeSec(parseInt(e.target.value, 10))}
              className="rounded border border-slate-700 bg-slate-800 px-2 py-1 text-sm"
            >
              {RANGE_OPTIONS.map((o) => (
                <option key={o.seconds} value={o.seconds}>
                  {o.label}
                </option>
              ))}
            </select>
          </div>
        </div>
      </header>

      <main className="mx-auto grid max-w-7xl grid-cols-12 gap-4 px-6 py-4">
        {/* 时间轴 */}
        <aside className="col-span-12 md:col-span-4">
          <div className="mb-2 flex items-center justify-between text-[11px] text-slate-500">
            <span>
              共 {items.length} 帧 · plan {stats.withPlan} · AI {stats.withAI} ·
              final {stats.withFinal}
            </span>
            {loading && <span>加载中…</span>}
          </div>
          {err && (
            <div className="mb-2 rounded border border-rose-500/40 bg-rose-500/10 p-2 text-[11px] text-rose-300">
              {err}
            </div>
          )}
          <div className="max-h-[78vh] overflow-y-auto rounded border border-slate-800">
            {items.length === 0 && !loading && (
              <div className="p-4 text-center text-[11px] text-slate-500">
                当前窗口内暂无归档
              </div>
            )}
            {items.map((it) => {
              const selected = it.ts === selectedTs;
              return (
                <button
                  key={it.ts}
                  onClick={() => setSelectedTs(it.ts)}
                  className={`block w-full border-b border-slate-800 px-3 py-2 text-left text-[11px] transition ${
                    selected
                      ? "bg-sky-600/20 border-l-2 border-l-sky-500"
                      : "hover:bg-slate-800/40"
                  }`}
                >
                  <div className="flex items-center justify-between">
                    <span className="font-mono text-slate-300">
                      {formatTs(it.ts)}
                    </span>
                    <span className="text-slate-500">
                      ${it.price_at_capture.toFixed(2)}
                    </span>
                  </div>
                  {it.ai_analysis_brief && (
                    <div className="mt-0.5 line-clamp-2 text-slate-400">
                      {it.ai_analysis_brief}
                    </div>
                  )}
                  <div className="mt-0.5 flex gap-1 text-[10px] text-slate-600">
                    {it.has_plan && (
                      <span className="rounded bg-emerald-500/15 px-1 text-emerald-400">
                        math
                      </span>
                    )}
                    {it.has_ai_report && (
                      <span className="rounded bg-violet-500/15 px-1 text-violet-400">
                        AI
                      </span>
                    )}
                    {it.has_final && (
                      <span className="rounded bg-sky-500/15 px-1 text-sky-400">
                        final
                      </span>
                    )}
                  </div>
                </button>
              );
            })}
          </div>
        </aside>

        {/* 详情 */}
        <section className="col-span-12 md:col-span-8">
          {frameLoading && (
            <div className="rounded border border-slate-800 bg-slate-900/40 p-6 text-[11px] text-slate-400">
              加载详情…
            </div>
          )}
          {!frameLoading && !frame && (
            <div className="rounded border border-slate-800 bg-slate-900/40 p-6 text-[11px] text-slate-500">
              在左侧选择一帧查看完整决策快照
            </div>
          )}
          {frame && (
            <div className="space-y-3">
              <div className="rounded border border-slate-800 bg-slate-900/50 p-3">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="text-[11px] text-slate-500">
                      {formatTs(frame.ts)} · {frame.coin}
                    </div>
                    <div className="mt-0.5 text-lg font-semibold">
                      ${frame.price_at_capture.toFixed(2)}
                    </div>
                  </div>
                  {frame.ai_analysis_brief && (
                    <div className="ml-4 max-w-md text-right text-[11px] text-slate-300">
                      {frame.ai_analysis_brief}
                    </div>
                  )}
                </div>
              </div>

              <FrameSection
                title="数学引擎 · ExecutionPlan"
                data={frame.execution_plan}
                emoji="🧮"
              />
              <FrameSection
                title="AI 交易员 · AITraderReport"
                data={frame.ai_trader_report}
                emoji="🧠"
              />
              <FrameSection
                title="融合层 · FinalDecision"
                data={frame.final_decision}
                emoji="🎯"
              />
              <FrameSection
                title="AI Snapshot（原始数据）"
                data={frame.snapshot}
                emoji="📸"
                collapsed
              />
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

function FrameSection({
  title,
  data,
  emoji,
  collapsed,
}: {
  title: string;
  data: Record<string, unknown> | null;
  emoji: string;
  collapsed?: boolean;
}) {
  const [open, setOpen] = useState(!collapsed);
  return (
    <div className="rounded border border-slate-800 bg-slate-900/50">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center justify-between px-3 py-2 text-left text-[12px] hover:bg-slate-800/40"
      >
        <span className="font-semibold">
          {emoji} {title}
        </span>
        <span className="text-slate-500">
          {data === null ? "（无）" : open ? "收起" : "展开"}
        </span>
      </button>
      {open && (
        <div className="max-h-[50vh] overflow-y-auto border-t border-slate-800 bg-slate-950/80 p-3">
          {data === null ? (
            <div className="text-[11px] text-slate-500">无</div>
          ) : (
            <pre className="whitespace-pre-wrap break-all text-[10px] text-slate-300">
              {JSON.stringify(data, null, 2)}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function formatTs(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}
