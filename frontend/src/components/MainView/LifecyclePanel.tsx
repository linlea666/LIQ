"use client";

/**
 * M3 · F2 · 关键位生命周期折叠面板
 * 直接读取 KeyLevelV2.lifecycle_events（后端已限制最多 20 条）。
 * 与 /api/key-levels/lifecycle/{coin}/{level_id} 端点功能互补：
 *   - 此处：当前快照内 level 的"近期 20 条事件"，无需额外请求
 *   - API：跨快照历史合并 + 去重（详情页使用）
 */

import { useState } from "react";
import type { LifecycleEvent } from "@/lib/types";

const EVENT_META: Record<string, { label: string; tone: string; emoji: string }> = {
  born: {
    label: "首次出现",
    tone: "text-sky-300",
    emoji: "✨",
  },
  strengthening: {
    label: "强度增强",
    tone: "text-emerald-300",
    emoji: "📈",
  },
  weakening: {
    label: "强度减弱",
    tone: "text-amber-300",
    emoji: "📉",
  },
  tier_upgraded: {
    label: "等级升级",
    tone: "text-emerald-300",
    emoji: "⬆️",
  },
  tier_downgraded: {
    label: "等级降级",
    tone: "text-amber-300",
    emoji: "⬇️",
  },
  tested: {
    label: "测试中",
    tone: "text-yellow-300",
    emoji: "🎯",
  },
  reacted: {
    label: "已反弹",
    tone: "text-emerald-400",
    emoji: "💪",
  },
  broken: {
    label: "已突破",
    tone: "text-rose-400",
    emoji: "💔",
  },
  fake_break: {
    label: "假突破",
    tone: "text-rose-300",
    emoji: "⚠️",
  },
  flipped: {
    label: "S/R 翻转",
    tone: "text-violet-300",
    emoji: "🔄",
  },
  expired: {
    label: "已失效",
    tone: "text-slate-500",
    emoji: "⏳",
  },
};

function formatRelativeTs(ts: number): string {
  if (!ts || ts <= 0) return "—";
  const now = Math.floor(Date.now() / 1000);
  const delta = now - ts;
  if (delta < 60) return `${delta}s 前`;
  if (delta < 3600) return `${Math.floor(delta / 60)}min 前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h 前`;
  return `${Math.floor(delta / 86400)}d 前`;
}

export default function LifecyclePanel({
  events,
  level_id,
}: {
  events?: LifecycleEvent[];
  level_id?: string;
}) {
  const [open, setOpen] = useState(false);
  const list = (events ?? []).slice().sort((a, b) => b.ts - a.ts);
  if (list.length === 0) return null;

  const headEvt = list[0];
  const headMeta = EVENT_META[headEvt.event_type] ?? {
    label: headEvt.event_type,
    tone: "text-slate-300",
    emoji: "📜",
  };

  return (
    <div className="mt-1 text-[11px]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1.5 px-2 py-0.5 rounded bg-slate-700/40 hover:bg-slate-600/50 border border-slate-600/40 text-slate-300 transition-colors"
        title={
          level_id
            ? `level_id: ${level_id} · 共 ${list.length} 条事件`
            : `共 ${list.length} 条事件`
        }
      >
        <span>{headMeta.emoji}</span>
        <span className={headMeta.tone}>{headMeta.label}</span>
        <span className="text-slate-500">· {formatRelativeTs(headEvt.ts)}</span>
        <span className="text-slate-500">·</span>
        <span className="text-slate-400">演化 ({list.length})</span>
        <span className="text-slate-500">{open ? "▲" : "▼"}</span>
      </button>

      {open && (
        <div className="mt-1.5 ml-1 pl-3 border-l border-slate-700 space-y-1.5">
          {list.map((evt, i) => {
            const meta = EVENT_META[evt.event_type] ?? {
              label: evt.event_type,
              tone: "text-slate-300",
              emoji: "📜",
            };
            const scoreDelta =
              (evt.score_after ?? 0) - (evt.score_before ?? 0);
            const tierChange =
              evt.tier_before && evt.tier_after && evt.tier_before !== evt.tier_after
                ? `${evt.tier_before} → ${evt.tier_after}`
                : "";
            const stateChange =
              evt.state_before && evt.state_after && evt.state_before !== evt.state_after
                ? `${evt.state_before} → ${evt.state_after}`
                : "";

            return (
              <div key={i} className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                <span className="opacity-80">{meta.emoji}</span>
                <span className={`font-medium ${meta.tone}`}>{meta.label}</span>
                <span className="text-slate-500 font-mono text-[10px]">
                  {formatRelativeTs(evt.ts)}
                </span>
                {Math.abs(scoreDelta) >= 1 && (
                  <span
                    className={
                      scoreDelta >= 0 ? "text-emerald-400" : "text-amber-400"
                    }
                  >
                    {scoreDelta >= 0 ? "+" : ""}
                    {scoreDelta.toFixed(0)} 分
                  </span>
                )}
                {tierChange && (
                  <span className="text-amber-300/80">tier {tierChange}</span>
                )}
                {stateChange && (
                  <span className="text-sky-300/80">state {stateChange}</span>
                )}
                {evt.detail && (
                  <span className="text-slate-500">· {evt.detail}</span>
                )}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
