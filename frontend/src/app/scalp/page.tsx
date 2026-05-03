"use client";

/**
 * 短线信号主看板
 *
 *   - 顶部状态条：启用策略 / 当前 coin / horizon / 测试模式 / 通知开关
 *   - Active 信号卡片网格（Step 13 详细卡片）
 *   - 快捷统计：今日命中率 / 总信号数 / 启用策略数
 *   - 最近 20 条历史信号简表
 *   - 浏览器通知授权按钮
 */

import { useMemo } from "react";

import SignalCard from "@/components/Scalp/SignalCard";
import SignalHistoryTable from "@/components/Scalp/SignalHistoryTable";
import { requestNotificationPermission } from "@/hooks/useScalpWebSocket";
import { useScalpStore } from "@/stores/scalpStore";
import { STRATEGY_META } from "@/lib/scalpTypes";

export default function ScalpDashboardPage() {
  const active = useScalpStore((s) => s.active);
  const history = useScalpStore((s) => s.history);
  const stats = useScalpStore((s) => s.stats);
  const config = useScalpStore((s) => s.config);
  const cancelSignal = useScalpStore((s) => s.cancelSignal);

  const enabledStrategies = useMemo(() => {
    if (!config) return [];
    return Object.entries(config.strategies)
      .filter(([, sc]) => sc.enabled)
      .map(([name]) => name);
  }, [config]);

  // 今日历史（UTC+8 当日 0 点起）
  const todayCutoff = useMemo(() => {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    return Math.floor(d.getTime() / 1000);
  }, []);

  const todayHistory = useMemo(
    () => history.filter((h) => h.created_at >= todayCutoff),
    [history, todayCutoff],
  );

  const todayWon = todayHistory.filter((h) => h.outcome === "won").length;
  const todayLost = todayHistory.filter((h) => h.outcome === "lost").length;
  const todayWinRate =
    todayWon + todayLost > 0 ? todayWon / (todayWon + todayLost) : null;

  return (
    <div className="space-y-4">
      {/* Top status bar */}
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
        <StatTile
          label="启用策略"
          value={
            enabledStrategies.length > 0
              ? enabledStrategies
                  .map((n) => STRATEGY_META[n as keyof typeof STRATEGY_META]?.shortCn ?? n)
                  .join(" / ")
              : "全部停用"
          }
          accent={enabledStrategies.length > 0 ? "#22c55e" : "#94a3b8"}
        />
        <StatTile
          label="当前活跃"
          value={`${active.length} 条`}
          accent={active.length > 0 ? "#0ea5e9" : "#475569"}
        />
        <StatTile
          label="今日命中率"
          value={
            todayWinRate !== null
              ? `${(todayWinRate * 100).toFixed(1)}%（${todayWon}胜${todayLost}负）`
              : `— / 0 已结算`
          }
          accent={todayWinRate !== null && todayWinRate >= 5 / 9 ? "#22c55e" : "#f59e0b"}
        />
        <StatTile
          label="累计命中率"
          value={
            stats && stats.total_won + stats.total_lost > 0
              ? `${(stats.global_win_rate * 100).toFixed(1)}% （${stats.total_signals} 单）`
              : "— / 累计 0 单"
          }
          accent={stats && stats.global_win_rate >= 5 / 9 ? "#22c55e" : "#f59e0b"}
        />
      </div>

      {/* 浏览器通知授权（首次使用时显示） */}
      {typeof window !== "undefined" &&
        "Notification" in window &&
        Notification.permission === "default" && (
          <div className="flex items-center justify-between gap-3 rounded-lg border border-sky-700/40 bg-sky-950/30 px-4 py-3 text-[12px]">
            <div>
              <div className="font-medium text-sky-200">开启浏览器通知？</div>
              <div className="mt-1 text-sky-400/80">
                高置信信号将弹出系统级提醒（仅本浏览器，可随时关闭）
              </div>
            </div>
            <button
              onClick={async () => {
                const ok = await requestNotificationPermission();
                if (ok) {
                  // 触发 re-render（next render 会自然反映 permission）
                  useScalpStore.setState({});
                }
              }}
              className="rounded bg-sky-700 px-3 py-1.5 text-white hover:bg-sky-600"
            >
              开启通知
            </button>
          </div>
        )}

      {/* Active signals */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">当前活跃信号</h2>
          <span className="text-[11px] text-slate-500">
            {active.length === 0 ? "暂无活跃信号 · 满足条件时自动产生" : `共 ${active.length} 条`}
          </span>
        </div>
        {active.length === 0 ? (
          <div className="rounded-lg border border-dashed border-slate-700 bg-slate-900/40 px-4 py-12 text-center text-[12px] text-slate-500">
            暂无活跃信号
            <div className="mt-1 text-[11px] text-slate-600">
              引擎每 30s 评估一次，仅在触发条件 + Veto 通过 + 置信≥阈值 时产生
            </div>
          </div>
        ) : (
          <div className="grid gap-3 lg:grid-cols-2">
            {active.map((sig) => (
              <SignalCard
                key={sig.signal_id}
                signal={sig}
                onCancel={() => cancelSignal(sig.signal_id)}
              />
            ))}
          </div>
        )}
      </section>

      {/* Recent history */}
      <section className="space-y-2">
        <div className="flex items-center justify-between">
          <h2 className="text-sm font-semibold text-slate-300">最近历史</h2>
          <span className="text-[11px] text-slate-500">
            最多展示 20 条 · 完整历史见{" "}
            <a href="/scalp/compare" className="text-sky-400 hover:underline">
              策略对比
            </a>
          </span>
        </div>
        <SignalHistoryTable signals={history.slice(0, 20)} />
      </section>
    </div>
  );
}

function StatTile({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent: string;
}) {
  return (
    <div
      className="rounded-lg border border-slate-800 bg-slate-900/60 px-3 py-2"
      style={{ borderLeft: `3px solid ${accent}` }}
    >
      <div className="text-[11px] text-slate-500">{label}</div>
      <div className="mt-0.5 truncate text-[14px] font-medium text-slate-100">{value}</div>
    </div>
  );
}
