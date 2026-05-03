"use client";

/**
 * 信号详情 · /scalp/signals/[id]
 *
 * 优先从 store 找（active / history），找不到再走 API（getSignalById）。
 * 完整展示：主卡片 + 状态时间轴 + 结算详情 + Veto 通过列表 + 全部 Evidence
 */

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";

import SignalCard from "@/components/Scalp/SignalCard";
import { getSignalById } from "@/lib/scalpApi";
import { STRATEGY_META, type ScalpSignal } from "@/lib/scalpTypes";
import { useScalpStore } from "@/stores/scalpStore";

export default function ScalpSignalDetailPage() {
  const params = useParams<{ id: string }>();
  const id = params?.id ?? "";
  const router = useRouter();

  const active = useScalpStore((s) => s.active);
  const history = useScalpStore((s) => s.history);
  const cancelSignal = useScalpStore((s) => s.cancelSignal);

  const [signal, setSignal] = useState<ScalpSignal | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    const found = active.find((s) => s.signal_id === id) || history.find((s) => s.signal_id === id);
    if (found) {
      setSignal(found);
      return;
    }
    setLoading(true);
    getSignalById(id)
      .then((s) => setSignal(s))
      .catch((e) => setError((e as Error).message))
      .finally(() => setLoading(false));
  }, [id, active, history]);

  if (loading) {
    return <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-[12px] text-slate-500">加载信号详情...</div>;
  }
  if (error) {
    return (
      <div className="space-y-3">
        <Link href="/scalp" className="text-[12px] text-slate-400 hover:text-slate-200">
          ← 返回看板
        </Link>
        <div className="rounded-md border border-rose-700/40 bg-rose-950/40 px-3 py-2 text-[12px] text-rose-200">
          ⚠ 加载失败：{error}
        </div>
      </div>
    );
  }
  if (!signal) {
    return (
      <div className="space-y-3">
        <Link href="/scalp" className="text-[12px] text-slate-400 hover:text-slate-200">
          ← 返回看板
        </Link>
        <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-[12px] text-slate-500">
          信号不存在
        </div>
      </div>
    );
  }

  const meta = STRATEGY_META[signal.strategy];

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <Link href="/scalp" className="text-[12px] text-slate-400 hover:text-slate-200">
          ← 返回看板
        </Link>
        <span className="text-[10px] font-mono text-slate-500">{signal.signal_id}</span>
      </div>

      <h1 className="text-lg font-semibold text-slate-100">
        {meta?.emoji} {meta?.shortCn} · {signal.coin} {signal.direction === "up" ? "看涨 ↑" : "看跌 ↓"} · {signal.horizon_min}min
      </h1>

      {/* 主卡 · 复用 SignalCard 组件 */}
      <SignalCard
        signal={signal}
        onCancel={
          signal.state === "active"
            ? async () => {
                await cancelSignal(signal.signal_id);
                router.push("/scalp");
              }
            : undefined
        }
      />

      {/* Veto 通过列表 */}
      {signal.veto_check_passed.length > 0 && (
        <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
          <h3 className="mb-2 text-[12px] font-semibold text-slate-300">Veto 检查通过</h3>
          <div className="flex flex-wrap gap-1.5 text-[10px]">
            {signal.veto_check_passed.map((v) => (
              <span
                key={v}
                className="rounded bg-emerald-900/30 px-1.5 py-0.5 text-emerald-300"
              >
                ✓ {v}
              </span>
            ))}
          </div>
        </section>
      )}

      {/* 全部 Evidence */}
      {signal.evidence.length > 0 && (
        <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
          <h3 className="mb-2 text-[12px] font-semibold text-slate-300">完整证据链</h3>
          <ul className="space-y-1.5 text-[12px]">
            {signal.evidence.map((ev, i) => (
              <li key={i} className="flex items-start gap-2">
                <span
                  className="mt-0.5 rounded px-1.5 py-0.5 text-[10px] uppercase tracking-wider"
                  style={{
                    color: ev.weight === "high" ? "#fca5a5" : ev.weight === "medium" ? "#fcd34d" : "#94a3b8",
                    background:
                      ev.weight === "high" ? "rgba(127,29,29,0.4)" : ev.weight === "medium" ? "rgba(120,53,15,0.4)" : "rgba(51,65,85,0.4)",
                  }}
                >
                  {ev.dimension} · {ev.weight}
                </span>
                <span className="flex-1 text-slate-200">{ev.observation}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* 状态时间线 */}
      {signal.state_history.length > 0 && (
        <section className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
          <h3 className="mb-2 text-[12px] font-semibold text-slate-300">状态变更时间线</h3>
          <ol className="space-y-1.5 text-[11px]">
            {signal.state_history.map((tr, i) => (
              <li key={i} className="flex items-center gap-3">
                <span className="font-mono text-slate-500">{formatDateTime(tr.ts)}</span>
                <span className="text-slate-400">
                  {tr.from_state} → <span className="text-slate-200">{tr.to_state}</span>
                </span>
                {tr.note && <span className="text-slate-500">· {tr.note}</span>}
              </li>
            ))}
          </ol>
        </section>
      )}

      {/* 结算详情（已结算时） */}
      {signal.outcome && (
        <section
          className="rounded-lg border p-4"
          style={{
            borderColor: signal.outcome === "won" ? "#15803d" : signal.outcome === "lost" ? "#b91c1c" : "#52525b",
            background: signal.outcome === "won" ? "rgba(20,83,45,0.1)" : signal.outcome === "lost" ? "rgba(127,29,29,0.1)" : "rgba(51,65,85,0.2)",
          }}
        >
          <h3 className="mb-2 text-[12px] font-semibold text-slate-200">结算详情</h3>
          <dl className="grid grid-cols-2 gap-2 text-[12px]">
            <Item label="结果">
              <span
                className="font-semibold"
                style={{ color: signal.outcome === "won" ? "#86efac" : signal.outcome === "lost" ? "#fca5a5" : "#d4d4d8" }}
              >
                {signal.outcome === "won" ? "✓ 命中" : signal.outcome === "lost" ? "✗ 落空" : "= 持平"}
              </span>
            </Item>
            <Item label="结算时间">
              {signal.settled_at ? formatDateTime(signal.settled_at) : "—"}
            </Item>
            <Item label="参考价（建仓）">${signal.reference_price.toLocaleString()}</Item>
            <Item label="结算价（到期）">
              {signal.settlement_price !== null ? `$${signal.settlement_price.toLocaleString()}` : "—"}
            </Item>
            <Item label="价格变动">
              {signal.settlement_price !== null
                ? formatChange(signal.settlement_price - signal.reference_price, signal.reference_price)
                : "—"}
            </Item>
            <Item label="盈亏率（0.8:1 赔率）">
              <span
                className="font-mono"
                style={{ color: signal.outcome === "won" ? "#22c55e" : signal.outcome === "lost" ? "#ef4444" : "#94a3b8" }}
              >
                {signal.outcome === "won" ? "+0.80" : signal.outcome === "lost" ? "-1.00" : "0.00"}
              </span>
            </Item>
          </dl>
          {signal.settlement_note && (
            <div className="mt-2 rounded bg-slate-950/40 p-2 text-[11px] text-slate-400">
              {signal.settlement_note}
            </div>
          )}
        </section>
      )}
    </div>
  );
}

function Item({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wider text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-slate-200">{children}</dd>
    </div>
  );
}

function formatDateTime(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function formatChange(diff: number, base: number): string {
  const pct = (diff / base) * 100;
  const absDiff = Math.abs(diff).toLocaleString(undefined, { maximumFractionDigits: 2 });
  const sign = diff >= 0 ? "+" : "-";
  return `${sign}$${absDiff} (${sign}${Math.abs(pct).toFixed(2)}%)`;
}
