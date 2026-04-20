"use client";

/**
 * P2.2 · 弹窗告警铃铛
 *
 * - 每 10s 拉取 /api/health/summary.events
 * - 新事件（ts 大于本地已读水位）入队 & 弹右上角 toast
 * - 点击铃铛展开最近 50 条事件列表
 * - 只在前端提示，不接外部告警通道
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/constants";
import type {
  HealthEvent,
  HealthEventKind,
  HealthSummaryResponse,
} from "@/lib/types";

const KIND_STYLES: Record<HealthEventKind, { emoji: string; color: string; label: string }> = {
  degrade: { emoji: "⚠️", color: "text-amber-300", label: "降级" },
  escalate: { emoji: "🔥", color: "text-rose-400", label: "升级" },
  recover: { emoji: "✅", color: "text-emerald-300", label: "恢复" },
  "de-escalate": { emoji: "🔄", color: "text-sky-300", label: "回落" },
  change: { emoji: "ℹ️", color: "text-slate-300", label: "变化" },
};

const LS_KEY = "liq.health.alertBell.lastReadTs";

export default function AlertBell() {
  const [events, setEvents] = useState<HealthEvent[]>([]);
  const [lastReadTs, setLastReadTs] = useState<number>(() => {
    if (typeof window === "undefined") return 0;
    const v = window.localStorage.getItem(LS_KEY);
    return v ? parseInt(v, 10) || 0 : 0;
  });
  const [open, setOpen] = useState(false);
  const [toasts, setToasts] = useState<HealthEvent[]>([]);
  // 已弹过 toast 的事件指纹，防止每 10s 轮询重复弹同一条
  const toastedKeys = useRef<Set<string>>(new Set());

  const pushToast = useCallback((evt: HealthEvent) => {
    const key = `${evt.ts}:${evt.id}:${evt.to}`;
    if (toastedKeys.current.has(key)) return;
    toastedKeys.current.add(key);
    setToasts((list) => [...list, evt].slice(-3));
    window.setTimeout(() => {
      setToasts((list) => list.filter((e) => e.ts !== evt.ts || e.id !== evt.id));
    }, 6000);
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/health/summary`, {
          cache: "no-store",
        });
        if (!r.ok) return;
        const j = (await r.json()) as HealthSummaryResponse;
        if (!alive) return;
        const evts = j.events || [];
        setEvents(evts);
        // 新增事件弹 toast（本次加载时不弹 — 只弹首次加载之后新增的）
        if (lastReadTs > 0) {
          evts.forEach((e) => {
            if (e.ts > lastReadTs) pushToast(e);
          });
        }
        // 限制 toastedKeys 集合大小，避免无限增长
        if (toastedKeys.current.size > 200) {
          const arr = Array.from(toastedKeys.current);
          toastedKeys.current = new Set(arr.slice(-100));
        }
      } catch {
        /* silent */
      }
    };
    tick();
    const t = setInterval(tick, 10000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [lastReadTs, pushToast]);

  const unread = useMemo(
    () => events.filter((e) => e.ts > lastReadTs).length,
    [events, lastReadTs],
  );

  const markAllRead = () => {
    const top = events[0]?.ts || Math.floor(Date.now() / 1000);
    setLastReadTs(top);
    if (typeof window !== "undefined") {
      window.localStorage.setItem(LS_KEY, String(top));
    }
  };

  return (
    <>
      <div className="relative">
        <button
          onClick={() => {
            setOpen((v) => !v);
            if (!open) markAllRead();
          }}
          className="relative rounded-md border border-slate-700 bg-slate-900/60 px-2 py-1 hover:border-slate-500 transition text-[12px]"
          title="健康告警"
        >
          <span>🔔</span>
          {unread > 0 && (
            <span className="absolute -right-1 -top-1 flex h-4 min-w-[16px] items-center justify-center rounded-full bg-rose-500 px-1 text-[9px] font-bold text-white">
              {unread > 99 ? "99+" : unread}
            </span>
          )}
        </button>

        {open && (
          <div className="absolute right-0 top-full z-50 mt-1 w-[360px] rounded-md border border-slate-700 bg-slate-900/95 shadow-2xl backdrop-blur-sm">
            <div className="flex items-center justify-between border-b border-slate-800 px-3 py-2">
              <span className="text-[11px] text-slate-400">
                健康事件（最近 {events.length} 条）
              </span>
              <button
                onClick={markAllRead}
                className="text-[10px] text-slate-500 hover:text-slate-300"
              >
                全部已读
              </button>
            </div>
            <div className="max-h-96 overflow-y-auto">
              {events.length === 0 && (
                <div className="px-3 py-6 text-center text-[11px] text-slate-500">
                  暂无降级事件
                </div>
              )}
              {events.map((e, i) => {
                const style = KIND_STYLES[e.kind] || KIND_STYLES.change;
                const dt = new Date(e.ts * 1000);
                return (
                  <div
                    key={`${e.ts}-${e.id}-${i}`}
                    className="border-b border-slate-800 px-3 py-2 text-[11px] hover:bg-slate-800/40"
                  >
                    <div className="flex items-center justify-between">
                      <span className={`font-semibold ${style.color}`}>
                        {style.emoji} {style.label} · {e.id}
                      </span>
                      <span className="text-slate-600">
                        {dt.toLocaleTimeString("zh-CN", { hour12: false })}
                      </span>
                    </div>
                    <div className="mt-0.5 text-slate-300">{e.title}</div>
                    <div className="text-slate-500">
                      {e.from} → {e.to}
                      {e.detail ? ` · ${e.detail}` : ""}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {/* Toasts */}
      <div className="pointer-events-none fixed right-4 top-16 z-[60] flex flex-col gap-2">
        {toasts.map((e) => {
          const style = KIND_STYLES[e.kind] || KIND_STYLES.change;
          return (
            <div
              key={`${e.ts}-${e.id}-toast`}
              className="pointer-events-auto w-72 rounded-md border border-slate-700 bg-slate-900/95 px-3 py-2 shadow-xl backdrop-blur-sm"
            >
              <div className={`text-[11px] font-semibold ${style.color}`}>
                {style.emoji} {style.label} · {e.id} {e.title}
              </div>
              {e.detail && (
                <div className="mt-0.5 text-[10px] text-slate-400">{e.detail}</div>
              )}
              <div className="mt-0.5 text-[10px] text-slate-600">
                {e.from} → {e.to}
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}
