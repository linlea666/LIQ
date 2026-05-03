"use client";

/**
 * 短线信号子路由布局
 *
 *   - 初始化 ScalpWebSocket（连接 socket.io，监听三个 scalp_* 事件）
 *   - 首次加载 active / history / config / stats / calibration
 *   - 每 30s 自动重拉 active（保险，主要靠 WS 推送）
 *   - 顶部导航：看板 / 策略管理 / 策略对比
 */

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useEffect } from "react";

import { useScalpWebSocket } from "@/hooks/useScalpWebSocket";
import { useScalpStore } from "@/stores/scalpStore";

const NAV_ITEMS = [
  { href: "/scalp", label: "看板", exact: true },
  { href: "/scalp/strategies", label: "策略管理" },
  { href: "/scalp/compare", label: "策略对比" },
];

export default function ScalpLayout({ children }: { children: React.ReactNode }) {
  useScalpWebSocket();

  const refreshAll = useScalpStore((s) => s.refreshAll);
  const error = useScalpStore((s) => s.error);
  const setError = useScalpStore((s) => s.setError);
  const wsConnected = useScalpStore((s) => s.wsConnected);
  const config = useScalpStore((s) => s.config);

  useEffect(() => {
    refreshAll();
    const timer = setInterval(() => {
      useScalpStore.getState().loadActive();
    }, 30_000);
    return () => clearInterval(timer);
  }, [refreshAll]);

  return (
    <div className="scalp-page min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-sm">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex items-center gap-3">
            <Link href="/" className="text-xs text-slate-400 hover:text-slate-200">
              ← 主面板
            </Link>
            <span className="text-lg font-bold text-amber-400">⚡ 短线信号</span>
            <span className="text-[11px] text-slate-500">
              预测合约（10/30/60min）· 仅测试统计 · 不实盘下单
            </span>
            <span
              className={[
                "rounded-full border px-2 py-0.5 text-[10px]",
                wsConnected
                  ? "border-emerald-700 bg-emerald-950/50 text-emerald-300"
                  : "border-slate-700 bg-slate-900/50 text-slate-500",
              ].join(" ")}
              title={wsConnected ? "WebSocket 已连接" : "WebSocket 未连接"}
            >
              {wsConnected ? "● 实时" : "○ 离线"}
            </span>
          </div>
          <ScalpNav />
        </div>
      </header>

      {/* 测试模式横条（始终显示） */}
      {config?.test_mode && (
        <div className="border-b border-amber-900/40 bg-amber-950/30">
          <div className="mx-auto max-w-7xl px-4 py-1.5 text-center text-[11px] text-amber-300">
            ⚠ {config.test_mode_banner_text}
          </div>
        </div>
      )}

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

function ScalpNav() {
  const pathname = usePathname();
  return (
    <nav className="flex items-center gap-1 rounded-md border border-slate-800 bg-slate-900/70 p-0.5">
      {NAV_ITEMS.map((item) => {
        const active = item.exact ? pathname === item.href : pathname.startsWith(item.href);
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
