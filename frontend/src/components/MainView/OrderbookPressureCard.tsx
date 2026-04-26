"use client";

import { useMemo } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";
import type {
  OrderbookPressureSnapshot,
  OrderbookPressureSignal,
  PressureWall,
  WallChangeKind,
  WallLabel,
} from "@/lib/types";

/**
 * 挂单压力监测器卡片（Orderbook Pressure Monitor）
 *
 * 与关键位卡平级的独立 snipe 信号源：
 *   - 顶部徽章：top_resistance / top_support 一眼看真阻力 / 真支撑
 *   - 主表格：每个 wall 的 side / 距离 / size / change_kind / label / confidence
 *   - 信号区：最近 N 条 OrderbookPressureSignal（30min 同价去重后，纯新触发）
 *
 * 数据来源：
 *   - data.orderbook_pressure ← engine.py _build_payload 推送（market_update 通道）
 *   - obPressureSignalsByCoin ← WS orderbook_pressure_signal 推送
 */

const LABEL_STYLES: Record<WallLabel, { text: string; bg: string; fg: string }> = {
  real_R: { text: "真阻力", bg: "bg-red-500/20", fg: "text-red-300" },
  fake_R: { text: "假阻力(撤)", bg: "bg-slate-600/30", fg: "text-slate-300" },
  fake_R_break: { text: "假阻力(已破)", bg: "bg-slate-700/40", fg: "text-slate-400" },
  real_S: { text: "真支撑", bg: "bg-green-500/20", fg: "text-green-300" },
  fake_S: { text: "假支撑(撤)", bg: "bg-slate-600/30", fg: "text-slate-300" },
  fake_S_break: { text: "假支撑(已破)", bg: "bg-slate-700/40", fg: "text-slate-400" },
  untested: { text: "待观察", bg: "bg-slate-700/30", fg: "text-slate-500" },
};

const CHANGE_LABELS: Record<WallChangeKind, { text: string; color: string }> = {
  eaten: { text: "被吃", color: "text-amber-400" },
  cancelled: { text: "撤单", color: "text-slate-400" },
  partial: { text: "部分", color: "text-slate-400" },
  growing: { text: "堆积", color: "text-cyan-400" },
  holding: { text: "保持", color: "text-blue-300" },
  unknown: { text: "未知", color: "text-slate-500" },
};

function formatUsdShort(usd: number): string {
  if (!usd || !isFinite(usd)) return "-";
  if (usd >= 1_000_000) return `$${(usd / 1_000_000).toFixed(2)}M`;
  if (usd >= 1_000) return `$${(usd / 1_000).toFixed(0)}K`;
  return `$${usd.toFixed(0)}`;
}

function formatTime(tsSec: number): string {
  if (!tsSec) return "-";
  const d = new Date(tsSec * 1000);
  return d.toLocaleTimeString("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export default function OrderbookPressureCard() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const signalsAll = useMarketStore((s) => s.obPressureSignalsByCoin);
  const snap = data?.orderbook_pressure;
  const signals = useMemo(() => signalsAll[coin] ?? [], [signalsAll, coin]);

  if (!snap) {
    return (
      <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg px-4 py-3 text-xs text-slate-500">
        挂单压力监测器：等待数据（首次 ~90s）...
      </div>
    );
  }

  const askWalls = (snap.walls || []).filter((w) => w.side === "ask")
    .sort((a, b) => Math.abs(a.distance_pct) - Math.abs(b.distance_pct));
  const bidWalls = (snap.walls || []).filter((w) => w.side === "bid")
    .sort((a, b) => Math.abs(a.distance_pct) - Math.abs(b.distance_pct));

  return (
    <div className="space-y-3">
      <Header snap={snap} coin={coin} />
      <SignalsList signals={signals} coin={coin} />
      <WallsTable
        title="上方卖墙（潜在阻力）"
        walls={askWalls}
        coin={coin}
        emptyHint="±2% 内未发现卖单堆"
      />
      <WallsTable
        title="下方买墙（潜在支撑）"
        walls={bidWalls}
        coin={coin}
        emptyHint="±2% 内未发现买单堆"
      />
      <Footer snap={snap} />
    </div>
  );
}

function Header({ snap, coin }: { snap: OrderbookPressureSnapshot; coin: string }) {
  const realR = snap.has_real_pressure_above;
  const realS = snap.has_real_pressure_below;
  const fakeBreakUp = snap.has_fake_break_above;
  const fakeBreakDown = snap.has_fake_break_below;

  let borderColor = "border-slate-600";
  if (realR && realS) borderColor = "border-amber-500/50";
  else if (realR) borderColor = "border-red-500/50";
  else if (realS) borderColor = "border-green-500/50";
  else if (fakeBreakUp || fakeBreakDown) borderColor = "border-purple-500/40";

  return (
    <div className={`bg-slate-800/60 border ${borderColor} rounded-lg p-4`}>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <span className="text-xs bg-slate-700/80 text-slate-300 px-2 py-0.5 rounded">
            🧱 挂单压力监测器
          </span>
          <span className="text-xs text-slate-500">
            {snap.walls.length} 个堆 · 现价{" "}
            <span className="font-mono text-slate-300">
              {formatPrice(snap.last_price, coin)}
            </span>
          </span>
        </div>
        <span className="text-[10px] text-slate-500">
          更新 {formatTime(snap.ts_sec)}
        </span>
      </div>
      <div className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs text-slate-400">
        <div>
          <span className="text-slate-500">最近真阻力：</span>
          <span className="text-red-400 font-mono">
            {snap.top_resistance ? formatPrice(snap.top_resistance, coin) : "—"}
          </span>
        </div>
        <div>
          <span className="text-slate-500">最近真支撑：</span>
          <span className="text-green-400 font-mono">
            {snap.top_support ? formatPrice(snap.top_support, coin) : "—"}
          </span>
        </div>
        {fakeBreakUp && (
          <div className="col-span-2 text-purple-400">
            ⚠ 上方有假突破墙（spoof 已确认），关注主动盘是否能续推
          </div>
        )}
        {fakeBreakDown && (
          <div className="col-span-2 text-purple-400">
            ⚠ 下方有假支撑墙（spoof 已确认），关注是否继续下探
          </div>
        )}
      </div>
    </div>
  );
}

function SignalsList({
  signals, coin,
}: {
  signals: OrderbookPressureSignal[]; coin: string;
}) {
  if (signals.length === 0) {
    return (
      <div className="bg-slate-800/30 border border-slate-700/50 rounded-lg px-4 py-2 text-xs text-slate-500 text-center">
        挂单压力 snipe 信号：暂无新触发（同价位 30 min 内不重复推送）
      </div>
    );
  }
  return (
    <div className="bg-slate-800/60 border border-slate-700/60 rounded-lg p-3">
      <div className="text-xs text-slate-400 mb-2">
        🎯 最近 snipe 信号（{signals.length}/20）
      </div>
      <div className="space-y-1.5">
        {signals.slice(0, 5).map((sig, idx) => {
          const isLong = sig.side === "long";
          const labelStyle = LABEL_STYLES[sig.wall_label];
          return (
            <div
              key={`${sig.dedup_key}-${sig.ts_sec}-${idx}`}
              className={`flex items-center gap-3 px-2 py-1.5 rounded ${
                isLong ? "bg-green-900/20" : "bg-red-900/20"
              }`}
            >
              <span
                className={`text-[11px] font-bold px-1.5 py-0.5 rounded ${
                  isLong ? "bg-green-500/30 text-green-300" : "bg-red-500/30 text-red-300"
                }`}
              >
                {isLong ? "做多" : "做空"}
              </span>
              <span className={`text-[11px] px-1.5 py-0.5 rounded ${labelStyle.bg} ${labelStyle.fg}`}>
                {labelStyle.text}
              </span>
              <span className="text-xs font-mono text-slate-200">
                {formatPrice(sig.entry_price, coin)}
              </span>
              <span className="text-[11px] text-slate-500">
                SL {formatPrice(sig.stop_loss, coin)} / TP {formatPrice(sig.take_profit, coin)}
              </span>
              <span className="ml-auto text-[10px] text-slate-500">
                conf {sig.confidence} · {formatTime(sig.ts_sec)}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function WallsTable({
  title, walls, coin, emptyHint,
}: {
  title: string;
  walls: PressureWall[];
  coin: string;
  emptyHint: string;
}) {
  if (walls.length === 0) {
    return (
      <div className="bg-slate-800/30 border border-slate-700/50 rounded-lg px-4 py-2 text-xs text-slate-500">
        <span className="text-slate-400">{title}：</span>
        {emptyHint}
      </div>
    );
  }
  return (
    <div className="bg-slate-800/60 border border-slate-700/60 rounded-lg overflow-hidden">
      <div className="text-xs text-slate-300 px-3 py-2 bg-slate-700/30">{title}</div>
      <div className="divide-y divide-slate-700/40">
        {walls.slice(0, 6).map((w, idx) => {
          const labelStyle = LABEL_STYLES[w.label];
          const change = CHANGE_LABELS[w.change_kind];
          return (
            <div
              key={`${w.side}-${w.price_mid}-${idx}`}
              className="flex items-center gap-3 px-3 py-2 text-xs hover:bg-slate-700/20"
              title={w.reason}
            >
              <span className="font-mono text-slate-200 w-24">
                {formatPrice(w.price_mid, coin)}
              </span>
              <span
                className={`w-12 text-right ${
                  w.distance_pct >= 0 ? "text-red-400" : "text-green-400"
                }`}
              >
                {w.distance_pct >= 0 ? "+" : ""}
                {w.distance_pct.toFixed(2)}%
              </span>
              <span className="w-20 text-slate-300 font-mono">{formatUsdShort(w.size_usd)}</span>
              <span className={`w-12 text-[11px] ${change.color}`}>{change.text}</span>
              <span className={`text-[11px] px-1.5 py-0.5 rounded ${labelStyle.bg} ${labelStyle.fg}`}>
                {labelStyle.text}
              </span>
              <span className="text-[10px] text-slate-500 w-10 text-right">
                {w.confidence}
              </span>
              <div className="ml-auto flex items-center gap-1.5">
                {w.has_active_whale && (
                  <span
                    title="该价位仍有 ≥1 笔大单 holding"
                    className="text-[10px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-300"
                  >
                    🐳
                  </span>
                )}
                {w.confluence_with_absorption && (
                  <span
                    title="与 footprint absorption 共振"
                    className="text-[10px] px-1 py-0.5 rounded bg-cyan-500/20 text-cyan-300"
                  >
                    ✓ 吸收
                  </span>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function Footer({ snap }: { snap: OrderbookPressureSnapshot }) {
  const qualityColor =
    snap.data_quality === "ok"
      ? "text-emerald-400"
      : snap.data_quality === "partial"
        ? "text-yellow-400"
        : "text-slate-500";
  return (
    <div className="text-[10px] text-slate-500 flex flex-wrap gap-x-4 gap-y-1 px-1">
      <span>
        depth 样本 <span className="text-slate-300">{snap.sample_count_depth}</span>
      </span>
      <span>
        大单 lifecycle{" "}
        <span className="text-slate-300">{snap.sample_count_large_history}</span>
      </span>
      <span>
        数据质量 <span className={qualityColor}>{snap.data_quality}</span>
      </span>
      {snap.notes.length > 0 && (
        <span className="text-slate-500">
          notes: {snap.notes.join(", ")}
        </span>
      )}
    </div>
  );
}
