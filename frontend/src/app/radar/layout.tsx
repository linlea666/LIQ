"use client";

/**
 * 雷达控制台布局。
 *
 * 顶栏常驻两个东西，它们不是装饰：
 *   - **服务健康**：雷达挂掉时页面依然能渲染上一次数据，
 *     没有这个指示灯就无法区分"最近没有新币"和"采集早就停了"。
 *   - **策略指纹**：所有评分都是某一套阈值的产物。
 *     不显示它，就会拿今天的分数和上周的分数直接比较。
 */

import Link from "next/link";
import { usePathname } from "next/navigation";

import { getHealth } from "@/lib/radarApi";

import { usePoll } from "./_components/usePoll";

const NAV_ITEMS = [
  { href: "/radar", label: "指挥中心", exact: true },
  { href: "/radar/scanner", label: "扫描器" },
  { href: "/radar/alerts", label: "警报" },
  { href: "/radar/research", label: "研究" },
  { href: "/radar/ops", label: "运维" },
  { href: "/radar/config", label: "配置" },
];

export default function RadarLayout({ children }: { children: React.ReactNode }) {
  const { data: health, error } = usePoll(getHealth, 15_000);

  const online = !error && health?.status === "ok";
  const degraded = !error && health?.status === "degraded";

  return (
    <div className="radar-page min-h-screen bg-slate-950 text-slate-100">
      <header className="border-b border-slate-800 bg-slate-900/80 backdrop-blur-sm">
        <div className="mx-auto flex max-w-[1600px] flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <Link href="/" className="text-xs text-slate-400 hover:text-slate-200">
              ← 主面板
            </Link>
            <span className="text-lg font-bold text-emerald-400">◎ 潜力币雷达</span>
            <span className="text-[11px] text-slate-500">
              BSC · Solana · 仅研究观测，不构成任何投资建议
            </span>
            <span
              className={[
                "rounded-full border px-2 py-0.5 text-[10px]",
                online
                  ? "border-emerald-700 bg-emerald-950/50 text-emerald-300"
                  : degraded
                    ? "border-amber-700 bg-amber-950/50 text-amber-300"
                    : "border-rose-700 bg-rose-950/50 text-rose-300",
              ].join(" ")}
              title={
                error
                  ? `无法连接雷达服务：${error}`
                  : degraded
                    ? "服务在跑，但最近一轮采集没有成功——数据可能已经停止更新"
                    : "采集正常"
              }
            >
              {online ? "● 采集中" : degraded ? "◐ 降级" : "○ 离线"}
            </span>
            {health && !health.email_usable && (
              <span
                className="rounded-full border border-amber-700 bg-amber-950/50 px-2 py-0.5 text-[10px] text-amber-300"
                title="SMTP 凭据不完整，警报邮件无法送达——系统看起来一切正常，但没人会收到通知"
              >
                ✉ 邮件不可用
              </span>
            )}
          </div>

          <div className="flex items-center gap-3">
            {health && (
              <span
                className="text-[10px] text-slate-500"
                title="所有评分都是某一套阈值的产物，跨版本比较分数没有意义"
              >
                {health.version.strategy_version} · {health.version.config_hash.slice(0, 8)}
                {" · "}
                {health.tokens_in_memory} 币
                {health.rss_mb !== null && ` · ${health.rss_mb.toFixed(0)}MB`}
              </span>
            )}
            <RadarNav />
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-[1600px] px-4 py-4">{children}</main>
    </div>
  );
}

function RadarNav() {
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
