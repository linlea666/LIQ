"use client";

/**
 * P2.2 · Decision Health 顶部徽章条
 *
 * 每 10s 轮询 /api/health/summary：
 *   - 整体颜色（ok/warn/fail）
 *   - 17 项分色计数（绿 / 黄 / 红 / 待）
 *   - 鼠标悬浮展开 warn/fail 明细
 *   - 点击跳转 /health/dashboard 详情页
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type { HealthSummaryResponse } from "@/lib/types";

const OVERALL_STYLES: Record<string, { dot: string; text: string; label: string }> = {
  ok: { dot: "bg-emerald-400", text: "text-emerald-300", label: "健康" },
  warn: { dot: "bg-amber-400", text: "text-amber-300", label: "告警" },
  fail: { dot: "bg-rose-500", text: "text-rose-300", label: "失败" },
  pending: { dot: "bg-slate-500", text: "text-slate-400", label: "启动中" },
};

export default function DecisionHealthStrip() {
  const [data, setData] = useState<HealthSummaryResponse | null>(null);
  const [err, setErr] = useState<string>("");
  const [hover, setHover] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/health/summary`, {
          cache: "no-store",
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j = (await r.json()) as HealthSummaryResponse;
        if (!alive) return;
        setData(j);
        setErr("");
      } catch (e) {
        if (alive) setErr((e as Error).message);
      }
    };
    tick();
    const t = setInterval(tick, 10000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  if (err && !data) {
    return (
      <div className="text-[11px] text-slate-500">健康摘要加载失败</div>
    );
  }
  if (!data) {
    return <div className="text-[11px] text-slate-600">D1-D17 …</div>;
  }

  const style = OVERALL_STYLES[data.overall] || OVERALL_STYLES.pending;
  const total =
    data.counts.green + data.counts.yellow + data.counts.red + data.counts.pending;

  return (
    <div
      className="relative"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <Link
        href="/health/dashboard"
        className="flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900/60 px-2 py-1 hover:border-slate-500 transition"
        title="D1-D17 决策健康"
      >
        <span className={`inline-block h-2 w-2 rounded-full ${style.dot} animate-pulse`} />
        <span className={`text-[11px] font-semibold ${style.text}`}>
          D1-D17 · {style.label}
        </span>
        <span className="flex items-center gap-1 text-[10px] text-slate-500">
          <span className="text-emerald-400">●{data.counts.green}</span>
          <span className="text-amber-400">●{data.counts.yellow}</span>
          <span className="text-rose-400">●{data.counts.red}</span>
          {data.counts.pending > 0 && (
            <span className="text-slate-500">●{data.counts.pending}</span>
          )}
          <span className="text-slate-600">/ {total}</span>
        </span>
      </Link>

      {hover && data.degraded.length > 0 && (
        <div className="absolute right-0 top-full z-40 mt-1 w-80 rounded-md border border-slate-700 bg-slate-900/95 shadow-xl backdrop-blur-sm">
          <div className="border-b border-slate-800 px-3 py-2 text-[11px] text-slate-400">
            非健康项 {data.degraded.length} / {total}
          </div>
          <div className="max-h-72 overflow-y-auto">
            {data.degraded.map((d) => (
              <div
                key={d.id}
                className="border-b border-slate-800 px-3 py-2 text-[11px] hover:bg-slate-800/40"
              >
                <div className="flex items-center justify-between">
                  <span className="font-semibold">
                    <span
                      className={
                        d.status === "failed"
                          ? "text-rose-400"
                          : "text-amber-400"
                      }
                    >
                      ●
                    </span>{" "}
                    {d.id}
                    <span className="ml-2 text-slate-400">{d.title}</span>
                  </span>
                  <span className="text-slate-600">
                    {d.stuck_sec > 0 ? `${Math.round(d.stuck_sec)}s` : "新"}
                  </span>
                </div>
                {d.detail && (
                  <div className="mt-0.5 text-slate-500">{d.detail}</div>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
