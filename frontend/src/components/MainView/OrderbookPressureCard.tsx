"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatCnUsd, formatPrice } from "@/lib/format";
import type {
  OrderbookPressureSnapshot,
  PressureWall,
  WallLabel,
  WallSource,
} from "@/lib/types";

/**
 * 挂单压力监测器明细卡（高阶视图，OrderbookPressureView 折叠区子组件）
 *
 * 重构（2026-04）后定位：盘口订单流仪表盘 · 完整 wall 明细
 *   - 顶部摘要：top_resistance / top_support（取 S/A 级最近）
 *   - 主表格：完整 wall 列表（side / 距离 / size / source / 时长 / tier）
 *   - 不再显示 confidence 数值、change_kind、真假语义
 */

const LABEL_STYLES: Record<WallLabel, { text: string; bg: string; fg: string }> = {
  wall_ask: { text: "卖方挂单", bg: "bg-red-500/15", fg: "text-red-300" },
  wall_bid: { text: "买方挂单", bg: "bg-green-500/15", fg: "text-green-300" },
  wall_vanished: { text: "已消失", bg: "bg-slate-700/40", fg: "text-slate-400" },
  wall_broken: { text: "已穿越", bg: "bg-purple-500/15", fg: "text-purple-300" },
};

const SOURCE_STYLES: Record<WallSource, { text: string; bg: string; fg: string }> = {
  depth_5m: { text: "5m订单簿", bg: "bg-blue-500/15", fg: "text-blue-300" },
  large_orders: { text: "巨鲸大单", bg: "bg-amber-500/15", fg: "text-amber-300" },
};

const TIER_STYLES: Record<string, { bg: string; fg: string }> = {
  S: { bg: "bg-amber-500/20", fg: "text-amber-400" },
  A: { bg: "bg-red-500/15", fg: "text-red-400" },
  B: { bg: "bg-blue-500/15", fg: "text-blue-400" },
  C: { bg: "bg-slate-600/30", fg: "text-slate-400" },
};

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

function formatHoldingAge(sec: number): string {
  if (!sec || sec <= 0) return "—";
  if (sec >= 86400) return `${Math.floor(sec / 86400)}天`;
  if (sec >= 3600) return `${Math.floor(sec / 3600)}h`;
  if (sec >= 60) return `${Math.floor(sec / 60)}m`;
  return `${sec}s`;
}

export default function OrderbookPressureCard() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const snap = data?.orderbook_pressure;

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
      <WallsTable
        title="上方卖方挂单（潜在阻力）"
        walls={askWalls}
        coin={coin}
        emptyHint="±12% 内未发现卖单堆"
      />
      <WallsTable
        title="下方买方挂单（潜在支撑）"
        walls={bidWalls}
        coin={coin}
        emptyHint="±12% 内未发现买单堆"
      />
      <Footer snap={snap} />
    </div>
  );
}

function Header({ snap, coin }: { snap: OrderbookPressureSnapshot; coin: string }) {
  const strongAsk = snap.walls.filter(
    (w) => w.side === "ask" && (w.strength_tier === "S" || w.strength_tier === "A"),
  ).length;
  const strongBid = snap.walls.filter(
    (w) => w.side === "bid" && (w.strength_tier === "S" || w.strength_tier === "A"),
  ).length;

  let borderColor = "border-slate-600";
  if (strongAsk > 0 && strongBid > 0) borderColor = "border-amber-500/50";
  else if (strongAsk > 0) borderColor = "border-red-500/50";
  else if (strongBid > 0) borderColor = "border-green-500/50";

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
          <span className="text-slate-500">最近强阻力（S/A）：</span>
          <span className="text-red-400 font-mono">
            {snap.top_resistance ? formatPrice(snap.top_resistance, coin) : "—"}
          </span>
          {strongAsk > 0 && (
            <span className="ml-2 text-[10px] text-red-400/70">×{strongAsk}</span>
          )}
        </div>
        <div>
          <span className="text-slate-500">最近强支撑（S/A）：</span>
          <span className="text-green-400 font-mono">
            {snap.top_support ? formatPrice(snap.top_support, coin) : "—"}
          </span>
          {strongBid > 0 && (
            <span className="ml-2 text-[10px] text-green-400/70">×{strongBid}</span>
          )}
        </div>
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
        {walls.slice(0, 8).map((w, idx) => {
          const labelStyle = LABEL_STYLES[w.label];
          const sourceStyle = SOURCE_STYLES[w.source];
          const tierStyle = TIER_STYLES[w.strength_tier] ?? TIER_STYLES.C;
          return (
            <div
              key={`${w.side}-${w.price_mid}-${idx}`}
              className="flex items-center gap-2 px-3 py-2 text-xs hover:bg-slate-700/20"
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
              <span className="w-20 text-slate-300 font-mono">{formatCnUsd(w.size_usd)}</span>
              <span className={`text-[10px] px-1.5 py-0.5 rounded ${sourceStyle.bg} ${sourceStyle.fg}`}>
                {sourceStyle.text}
              </span>
              <span
                className="text-[10px] text-slate-400 w-12 text-center"
                title="挂单时长（仅 large_orders 路径有意义）"
              >
                {w.source === "large_orders" ? formatHoldingAge(w.holding_avg_age_sec) : "—"}
              </span>
              <span className={`text-[11px] px-1.5 py-0.5 rounded ${labelStyle.bg} ${labelStyle.fg}`}>
                {labelStyle.text}
              </span>
              <span
                className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${tierStyle.bg} ${tierStyle.fg}`}
                title={`强度等级：${w.strength_tier}（按 USD 阈值：S ≥ $30M / A ≥ $10M / B ≥ $3M / C ≥ $500K）`}
              >
                {w.strength_tier}
              </span>
              <div className="ml-auto flex items-center gap-1.5">
                {w.has_active_whale && (
                  <span
                    title={`该 wall 含 ${w.large_order_count} 笔活跃大单`}
                    className="text-[10px] px-1 py-0.5 rounded bg-amber-500/20 text-amber-300"
                  >
                    🐳 ×{w.large_order_count}
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
        large_orders 墙{" "}
        <span className="text-slate-300">{snap.sample_count_large_orders_walls}</span>
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
