"use client";

/**
 * P2.2 · Decision Health 顶部徽章条（紧凑版）
 *
 * 每 10s 轮询 /api/health/summary：
 *   - 全健康：仅显示 1 个绿点 + "D17" 微标（~48px 宽）
 *   - 有异常：显示橙/红点 + 非健康数（如 "●2"），醒目
 *   - 鼠标悬浮展开 warn/fail 明细
 *   - 点击跳转 /health/dashboard 详情页
 *
 * 设计动机：旧版 "● D1-D17 · 告警 ●12 ●5 ●0 / 17" 三段式占位
 * 过大（~120×60 px），挤压顶部核心数据。新版在保留告警可见性
 * 的前提下压缩到 ~48×22 px（缩小约 85%），hover/click 全量信息依旧可达。
 */

import Link from "next/link";
import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type { HealthSummaryResponse } from "@/lib/types";

const OVERALL_STYLES: Record<
  string,
  { dot: string; text: string; ring: string; label: string }
> = {
  ok: {
    dot: "bg-emerald-400",
    text: "text-emerald-300",
    ring: "border-emerald-700/40 hover:border-emerald-500",
    label: "健康",
  },
  warn: {
    dot: "bg-amber-400",
    text: "text-amber-300",
    ring: "border-amber-600/60 hover:border-amber-400",
    label: "告警",
  },
  fail: {
    dot: "bg-rose-500",
    text: "text-rose-300",
    ring: "border-rose-600/70 hover:border-rose-400",
    label: "失败",
  },
  pending: {
    dot: "bg-slate-500",
    text: "text-slate-400",
    ring: "border-slate-700 hover:border-slate-500",
    label: "启动中",
  },
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
      <span className="text-[10px] text-slate-500" title={`健康摘要加载失败: ${err}`}>
        D?
      </span>
    );
  }
  if (!data) {
    return <span className="text-[10px] text-slate-600">D…</span>;
  }

  const style = OVERALL_STYLES[data.overall] || OVERALL_STYLES.pending;
  const total =
    data.counts.green +
    data.counts.yellow +
    data.counts.red +
    data.counts.pending;
  const unhealthy = data.counts.yellow + data.counts.red + data.counts.pending;
  const isOk = data.overall === "ok";

  return (
    <div
      className="relative"
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
    >
      <Link
        href="/health/dashboard"
        className={`flex items-center gap-1 rounded-md border bg-slate-900/60 px-1.5 py-0.5 transition ${style.ring}`}
        title={`D1-D17 · ${style.label}（绿 ${data.counts.green} / 黄 ${data.counts.yellow} / 红 ${data.counts.red} / 待 ${data.counts.pending}）`}
      >
        <span
          className={`inline-block h-1.5 w-1.5 rounded-full ${style.dot} ${
            isOk ? "" : "animate-pulse"
          }`}
        />
        {isOk ? (
          <span className="text-[10px] font-semibold text-emerald-300/90">
            D{total}
          </span>
        ) : (
          <span className={`text-[10px] font-semibold ${style.text}`}>
            ●{unhealthy}
          </span>
        )}
      </Link>

      {hover && (
        <div className="absolute right-0 top-full z-40 mt-1 w-80 rounded-md border border-slate-700 bg-slate-900/95 shadow-xl backdrop-blur-sm">
          <div className="border-b border-slate-800 px-3 py-2 text-[11px] text-slate-400 flex items-center justify-between">
            <span>
              D1-D17 · <span className={style.text}>{style.label}</span>
            </span>
            <span className="text-slate-500">
              <span className="text-emerald-400">●{data.counts.green}</span>{" "}
              <span className="text-amber-400">●{data.counts.yellow}</span>{" "}
              <span className="text-rose-400">●{data.counts.red}</span>
              {data.counts.pending > 0 && (
                <>
                  {" "}
                  <span className="text-slate-500">●{data.counts.pending}</span>
                </>
              )}
              <span className="text-slate-600"> / {total}</span>
            </span>
          </div>
          {data.degraded.length > 0 ? (
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
          ) : (
            <div className="px-3 py-3 text-[11px] text-slate-500">
              全部 {total} 项健康，点击查看详情 →
            </div>
          )}
        </div>
      )}
    </div>
  );
}
