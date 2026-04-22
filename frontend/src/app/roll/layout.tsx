"use client";

/**
 * 滚仓模块子路由布局
 *
 *   - 初始化 RollWebSocket（订阅所有 active position 的 roll:{id} 频道）
 *   - 首次加载 enums / templates / settings / positions
 *   - 顶部导航：总览 / 新建 / 模板 / 设置
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";

import { useRollAlerts } from "@/hooks/useRollAlerts";
import { useRollWebSocket } from "@/hooks/useRollWebSocket";
import { useRollStore } from "@/stores/rollStore";

const NAV_ITEMS = [
  { href: "/roll", label: "总览", exact: true },
  { href: "/roll/new", label: "新建", exact: true },
  { href: "/roll/replay", label: "复盘" },
  { href: "/roll/templates", label: "模板" },
  { href: "/roll/settings", label: "设置" },
];

export default function RollLayout({ children }: { children: React.ReactNode }) {
  useRollWebSocket();
  useRollAlerts();

  const refreshAll = useRollStore((s) => s.refreshAll);
  const error = useRollStore((s) => s.error);
  const setError = useRollStore((s) => s.setError);

  useEffect(() => {
    refreshAll();
    const timer = setInterval(() => {
      useRollStore.getState().loadPositions("active");
    }, 30_000);
    return () => clearInterval(timer);
  }, [refreshAll]);

  return (
    <div className="roll-page min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-xs text-slate-400 hover:text-slate-200">
              ← 主面板
            </Link>
            <span className="text-lg font-bold text-emerald-400">📊 滚仓</span>
            <span className="text-[11px] text-slate-500">
              提醒模式 · 本地 JSON 持久化 · 不下单
            </span>
          </div>
          <RollNav />
        </div>
      </header>

      {error && (
        <div className="mx-auto mt-3 flex max-w-7xl items-center justify-between gap-3 rounded-md border border-rose-600/40 bg-rose-950/40 px-3 py-2 text-[12px] text-rose-200">
          <span>⚠ {error}</span>
          <button
            onClick={() => setError(null)}
            className="rounded bg-rose-800/50 px-2 py-0.5 text-[11px] hover:bg-rose-700/70"
          >
            忽略
          </button>
        </div>
      )}

      <main className="mx-auto max-w-7xl px-4 py-4">{children}</main>
    </div>
  );
}

function RollNav() {
  const pathname = usePathname();
  return (
    <nav className="flex items-center gap-1 rounded-md border border-slate-800 bg-slate-900/70 p-0.5">
      {NAV_ITEMS.map((item) => {
        const active = item.exact
          ? pathname === item.href
          : pathname.startsWith(item.href);
        return (
          <Link
            key={item.href}
            href={item.href}
            className={[
              "rounded px-3 py-1 text-[12px] transition",
              active
                ? "bg-slate-700 text-white"
                : "text-slate-400 hover:bg-slate-800 hover:text-slate-100",
            ].join(" ")}
          >
            {item.label}
          </Link>
        );
      })}
    </nav>
  );
}
