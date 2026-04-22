"use client";

/**
 * /roll/replay/[id] · 单次持仓复盘详情
 *
 * 内容：
 *   - 顶部持仓身份条（币种/方向/杠杆/计划名称/模板）
 *   - CoverageStats 三卡片
 *   - ReplayTimeline 完整事件流（支持 filter）
 */

import Link from "next/link";
import { useEffect } from "react";
import { useParams } from "next/navigation";

import { CoverageStats } from "@/components/Roll/CoverageStats";
import { ReplayTimeline } from "@/components/Roll/ReplayTimeline";
import { useRollStore } from "@/stores/rollStore";

export default function RollReplayDetailPage() {
  const params = useParams<{ id: string }>();
  const id = decodeURIComponent(params.id);

  const loadReplay = useRollStore((s) => s.loadReplay);
  const replay = useRollStore((s) => s.replaysByPosition[id]);
  const loading = useRollStore((s) => s.replayLoading[id]);

  useEffect(() => {
    loadReplay(id).catch(() => undefined);
  }, [id, loadReplay]);

  if (loading && !replay) {
    return (
      <div className="py-10 text-center text-[12px] text-slate-500">
        加载中…
      </div>
    );
  }

  if (!replay) {
    return (
      <div className="space-y-3">
        <Link
          href="/roll/replay"
          className="text-[12px] text-sky-400 hover:underline"
        >
          ← 返回列表
        </Link>
        <div className="rounded border border-rose-800/40 bg-rose-950/30 px-4 py-3 text-[12px] text-rose-200">
          加载失败或持仓不存在
        </div>
      </div>
    );
  }

  const { position, plan, events, stats } = replay;

  return (
    <div className="space-y-5">
      {/* 返回 + 身份条 */}
      <div className="flex items-center gap-3">
        <Link
          href="/roll/replay"
          className="text-[12px] text-sky-400 hover:underline"
        >
          ← 返回列表
        </Link>
        <button
          onClick={() => loadReplay(id, true)}
          disabled={loading}
          className="ml-auto rounded-md border border-slate-700 px-2 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"
        >
          {loading ? "刷新中…" : "刷新"}
        </button>
      </div>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <div className="flex flex-wrap items-baseline gap-3">
          <h1 className="text-xl font-bold">{position.coin}</h1>
          <span
            className={[
              "rounded px-2 py-0.5 text-[11px] uppercase",
              position.side === "long"
                ? "bg-emerald-900/40 text-emerald-200"
                : "bg-rose-900/40 text-rose-200",
            ].join(" ")}
          >
            {position.side}
          </span>
          <span className="text-[12px] text-slate-400">
            {position.leverage}x · {position.margin_mode}
          </span>
          <span
            className={[
              "rounded px-2 py-0.5 text-[11px]",
              position.status === "closed"
                ? "bg-slate-800 text-slate-300"
                : "bg-emerald-900/40 text-emerald-200",
            ].join(" ")}
          >
            {position.status === "closed" ? "已平仓" : "活跃中"}
          </span>
          <span className="ml-auto font-mono text-[11px] text-slate-500">
            ID: {position.id.slice(0, 8)}…
          </span>
        </div>

        <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[12px] sm:grid-cols-4">
          <KV
            label="建仓"
            value={new Date(position.created_at * 1000).toLocaleString("zh-CN", {
              hour12: false,
            })}
          />
          <KV
            label="关仓"
            value={
              position.closed_at
                ? new Date(position.closed_at * 1000).toLocaleString("zh-CN", {
                    hour12: false,
                  })
                : "—"
            }
          />
          <KV label="建仓价" value={position.entry_price.toLocaleString()} />
          <KV
            label="初始保证金"
            value={`${
              position.events.find((e) => e.kind === "init")?.margin_delta_usd.toFixed(2) ??
              "—"
            } USD`}
          />
          {plan && (
            <>
              <KV label="计划" value={plan.name || "(未命名)"} />
              <KV label="模板" value={plan.template_id} />
              <KV label="加仓模式" value={plan.add_mode} />
              <KV
                label="账户上限"
                value={`${(plan.max_margin_pct_of_account * 100).toFixed(0)}%`}
              />
            </>
          )}
        </div>

        {position.note && (
          <div className="mt-2 text-[11px] italic text-slate-400">
            备注：{position.note}
          </div>
        )}
      </div>

      <CoverageStats stats={stats} />

      <section className="rounded-lg border border-slate-800 bg-slate-900/40">
        <div className="border-b border-slate-800 px-3 py-2 text-[12px] font-semibold text-slate-300">
          事件流
        </div>
        <div className="p-3">
          <ReplayTimeline
            events={events}
            stats={stats}
            side={position.side}
            initialPrice={position.entry_price}
          />
        </div>
      </section>
    </div>
  );
}

function KV({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-slate-500">{label}</span>
      <span className="font-mono text-slate-200">{value}</span>
    </div>
  );
}
