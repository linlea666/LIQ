"use client";

import Link from "next/link";
import { useWebSocket } from "@/hooks/useWebSocket";
import PriceBar from "@/components/TopBar/PriceBar";
import CoinSelector from "@/components/TopBar/CoinSelector";
import StatusBadges from "@/components/TopBar/StatusBadges";
import DecisionHealthStrip from "@/components/TopBar/DecisionHealthStrip";
import AlertBell from "@/components/TopBar/AlertBell";
import AIButton from "@/components/TopBar/AIButton";
import CoreFactors from "@/components/FactorCards/CoreFactors";
import TabContainer from "@/components/MainView/TabContainer";
import AIAnalysis from "@/components/SidePanel/AIAnalysis";
import LiveFeed from "@/components/SidePanel/LiveFeed";
import StatusFooter from "@/components/common/StatusFooter";
import { useMarketStore } from "@/stores/marketStore";
import { useRollStore } from "@/stores/rollStore";
import { useEffect, useMemo, useRef, useState } from "react";
import { API_BASE } from "@/lib/constants";

export default function Dashboard() {
  useWebSocket();

  const displayMode = useMarketStore((s) => s.displayMode);
  const setSourceHealth = useMarketStore((s) => s.setSourceHealth);
  const setAIAvailable = useMarketStore((s) => s.setAIAvailable);
  // 让 ⋮工具 下拉中的滚仓徽章在 Dashboard 上也能显示数量
  const loadRollPositions = useRollStore((s) => s.loadPositions);

  useEffect(() => {
    const fetchHealth = async () => {
      try {
        const res = await fetch(`${API_BASE}/api/health`);
        if (res.ok) {
          const data = await res.json();
          if (data.sources) setSourceHealth(data.sources);
          if (data.ai_available !== undefined) setAIAvailable(data.ai_available);
        }
      } catch { /* silent */ }
    };
    fetchHealth();
    const timer = setInterval(fetchHealth, 10000);
    return () => clearInterval(timer);
  }, [setSourceHealth, setAIAvailable]);

  // 仅刷新滚仓 active 持仓列表用于徽章；signals 由 /roll 页内部的 WS 维持
  useEffect(() => {
    loadRollPositions("active").catch(() => undefined);
    const t = setInterval(
      () => loadRollPositions("active").catch(() => undefined),
      30_000,
    );
    return () => clearInterval(t);
  }, [loadRollPositions]);

  return (
    <div className="h-screen flex flex-col bg-slate-950">
      {/* Top Bar */}
      <header className="shrink-0 border-b border-slate-700 bg-slate-900/80 backdrop-blur-sm">
        <div className="flex items-center justify-between px-4 py-2">
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2">
              <span className="text-lg font-bold text-blue-400">🛡️ LIQ</span>
              <span className="text-[10px] text-slate-600 hidden sm:inline">防猎杀 v1.0</span>
            </div>
            <CoinSelector />
            <PriceBar />
          </div>
          <div className="flex items-center gap-4">
            <StatusBadges />
            <ModeSelector />
            <DecisionHealthStrip />
            <AlertBell />
            <ToolsMenu />
            <AIButton />
          </div>
        </div>
      </header>

      {/* Factor Cards Row (专业模式) */}
      {displayMode === "pro" && (
        <div className="shrink-0 px-4 py-2 border-b border-slate-800">
          <CoreFactors />
        </div>
      )}

      {/* Main Content */}
      <div className="flex-1 flex min-h-0">
        {/* Main View */}
        <div className="flex-1 flex flex-col min-w-0">
          <TabContainer />
        </div>

        {/* Right Side Panel */}
        <div className="w-[280px] border-l border-slate-700 bg-slate-900/50 overflow-y-auto p-3 shrink-0">
          <LiveFeed />
        </div>

        {/* AI Analysis Drawer */}
        <AIAnalysis />
      </div>

      {/* Footer */}
      <StatusFooter />
    </div>
  );
}

function ModeSelector() {
  const displayMode = useMarketStore((s) => s.displayMode);
  const setDisplayMode = useMarketStore((s) => s.setDisplayMode);
  const modes = [
    { key: "beginner" as const, label: "小白" },
    { key: "pro" as const, label: "专业" },
  ];

  return (
    <div className="flex gap-0.5 bg-slate-800 rounded-md p-0.5 text-[11px]">
      {modes.map((m) => (
        <button
          key={m.key}
          onClick={() => setDisplayMode(m.key)}
          className={`px-2 py-0.5 rounded transition-all ${
            displayMode === m.key
              ? "bg-slate-600 text-white"
              : "text-slate-500 hover:text-slate-300"
          }`}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}

/**
 * 顶栏次级入口下拉：简报 / 回放 / 滚仓
 *
 * 设计：
 *   - 收纳次要路由，避免顶栏堆按钮
 *   - 滚仓子项在「有 active 持仓」时显示绿色徽章，存在 urgent 信号时变红 + 脉冲提醒
 *   - 用 ref + outside-click 关闭（不引入 floating-ui 等重依赖）
 */
function ToolsMenu() {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const coin = useMarketStore((s) => s.coin);

  // 滚仓状态：用于在按钮上显示徽章
  const positions = useRollStore((s) => s.positions);
  const signalsByPosition = useRollStore((s) => s.signalsByPosition);
  const activeCount = positions.filter((p) => p.status === "active").length;
  const activeIds = new Set(positions.filter((p) => p.status === "active").map((p) => p.id));
  const urgentCount = Object.entries(signalsByPosition)
    .filter(([id, s]) => activeIds.has(id) && s.urgency === "urgent")
    .length;

  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (!ref.current?.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [open]);

  // 主按钮的状态点：urgent 红 / 有 active 绿 / 无 灰
  const dotTone =
    urgentCount > 0
      ? "bg-rose-500 animate-pulse"
      : activeCount > 0
      ? "bg-emerald-500"
      : "bg-slate-700";

  const items: Array<{
    href: string;
    label: string;
    icon: string;
    desc: string;
    badge?: { text: string; tone: string };
  }> = useMemo(
    () => [
      { href: "/news-brief", label: "简报", icon: "📰", desc: "AI 24h 滚动记忆锚" },
      { href: "/replay", label: "回放", icon: "📼", desc: "历史快照逐 tick 回放" },
      {
        href: `/brain/${coin}`,
        label: "交易大脑",
        icon: "🧠",
        desc: "价格区统一视图 · 证据链 · 无指令",
      },
      {
        href: "/roll",
        label: "滚仓",
        icon: "📊",
        desc: "加减仓信号管家 · 提醒模式",
        badge:
          urgentCount > 0
            ? {
                text: `${urgentCount} URGENT`,
                tone: "bg-rose-900/60 text-rose-200 border-rose-700/60",
              }
            : activeCount > 0
              ? {
                  text: `${activeCount} 持仓`,
                  tone: "bg-emerald-900/40 text-emerald-300 border-emerald-700/40",
                }
              : undefined,
      },
    ],
    [coin, urgentCount, activeCount],
  );

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition ${
          open
            ? "border-slate-500 bg-slate-800 text-slate-100"
            : "border-slate-700 bg-slate-900/60 text-slate-300 hover:border-slate-500 hover:text-slate-100"
        }`}
        title="工具入口"
      >
        <span className={`h-1.5 w-1.5 rounded-full ${dotTone}`} />
        <span>⋮ 工具</span>
      </button>
      {open && (
        <div className="absolute right-0 z-50 mt-1 w-64 overflow-hidden rounded-md border border-slate-700 bg-slate-900 shadow-lg">
          {items.map((it) => (
            <Link
              key={it.href}
              href={it.href}
              onClick={() => setOpen(false)}
              className="flex items-start gap-2 border-b border-slate-800 px-3 py-2 text-[12px] text-slate-300 last:border-b-0 hover:bg-slate-800/60 hover:text-slate-100"
            >
              <span className="mt-0.5 text-base leading-none">{it.icon}</span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="font-medium text-slate-100">{it.label}</span>
                  {it.badge && (
                    <span
                      className={`rounded border px-1 py-px text-[9px] leading-none ${it.badge.tone}`}
                    >
                      {it.badge.text}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 text-[10px] text-slate-500">{it.desc}</div>
              </div>
            </Link>
          ))}
        </div>
      )}
    </div>
  );
}
