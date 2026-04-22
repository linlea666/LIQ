"use client";

/**
 * /roll/[id] · 单持仓详情
 *
 * 布局：
 *   [ 顶部 ]   返回 + 标题 + 危险操作（删除/平仓）
 *   [ 主列 ]   PositionCard → AddPreviewCard（action=add 时）→ SignalExplain → RollingLadder
 *
 * Step 7 新接入：
 *   - PositionCard：统一展示持仓概况 + 关键距离
 *   - AddPreviewCard：加仓时显示三闸门 + Before/After + 确认/覆盖按钮
 *   - SignalExplain：置信度 / supporting / blocking / 专属操作 / 前瞻
 *   - RollingLadder：事件阶梯
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import AddPreviewCard from "@/components/Roll/AddPreviewCard";
import PositionCard from "@/components/Roll/PositionCard";
import RollingLadder from "@/components/Roll/RollingLadder";
import SignalExplain from "@/components/Roll/SignalExplain";
import { useRollStore } from "@/stores/rollStore";

export default function RollDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? "";
  const router = useRouter();

  const positions = useRollStore((s) => s.positions);
  const signalsByPosition = useRollStore((s) => s.signalsByPosition);
  const plansById = useRollStore((s) => s.plansById);
  const eventsByPosition = useRollStore((s) => s.eventsByPosition);
  const refreshPosition = useRollStore((s) => s.refreshPosition);
  const refreshPositionEvents = useRollStore((s) => s.refreshPositionEvents);
  const refreshPositionSignal = useRollStore((s) => s.refreshPositionSignal);
  const deletePosition = useRollStore((s) => s.deletePosition);

  const position = useMemo(
    () => positions.find((p) => p.id === id),
    [positions, id],
  );
  const signal = signalsByPosition[id];
  const plan = position ? plansById[position.plan_id] : undefined;
  const events = eventsByPosition[id] || position?.events || [];

  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (!id) return;
    refreshPosition(id);
    refreshPositionEvents(id);
    refreshPositionSignal(id);
  }, [id, refreshPosition, refreshPositionEvents, refreshPositionSignal]);

  const handleRefreshAfterEvent = () => {
    refreshPosition(id);
    refreshPositionEvents(id);
  };

  const handleDelete = async () => {
    if (!position) return;
    if (
      !confirm(
        `确认删除该计划？\n\n${position.coin} ${position.side} · ${plan?.template_id || ""}\n\n此操作仅删除本地记录，不影响交易所仓位。`,
      )
    )
      return;
    setDeleting(true);
    try {
      await deletePosition(id);
      router.push("/roll");
    } finally {
      setDeleting(false);
    }
  };

  if (!position) {
    return (
      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-6 text-center text-[12px] text-slate-400">
        持仓 {id} 未找到或已删除 ·
        <Link href="/roll" className="ml-2 text-sky-400 hover:underline">
          返回总览
        </Link>
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link
            href="/roll"
            className="text-[12px] text-slate-400 hover:text-slate-200"
          >
            ← 返回总览
          </Link>
          <div className="mt-1 flex items-baseline gap-2">
            <h1 className="text-xl font-semibold">
              {position.coin} {position.side === "long" ? "多" : "空"}
            </h1>
            {plan?.name && (
              <span className="text-[12px] text-slate-400">
                「{plan.name}」
              </span>
            )}
          </div>
        </div>
        <div className="flex gap-2">
          <button
            onClick={handleDelete}
            disabled={deleting}
            className="rounded-md border border-slate-700 bg-slate-900/60 px-3 py-1.5 text-[12px] text-slate-300 transition hover:border-rose-600 hover:text-rose-200 disabled:opacity-50"
            title="仅删除本地记录"
          >
            {deleting ? "删除中…" : "删除计划"}
          </button>
        </div>
      </div>

      <PositionCard position={position} signal={signal} plan={plan} />

      {signal?.action === "add" && signal.add_preview && (
        <AddPreviewCard
          position={position}
          signal={signal}
          plan={plan}
          onExecuted={handleRefreshAfterEvent}
        />
      )}

      {signal && (
        <SignalExplain
          position={position}
          signal={signal}
          plan={plan}
          onExecuted={handleRefreshAfterEvent}
        />
      )}

      <RollingLadder position={position} events={events} />
    </div>
  );
}
