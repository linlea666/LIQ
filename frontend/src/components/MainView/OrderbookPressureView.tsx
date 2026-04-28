"use client";

import { useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { formatCnUsd, formatPrice } from "@/lib/format";
import type {
  OrderbookPressureSnapshot,
  PressureWall,
  WallLabel,
  WallSource,
} from "@/lib/types";
import LiquidityWallCard from "./LiquidityWallCard";
import OrderbookPressureCard from "./OrderbookPressureCard";

/**
 * 挂单压力监测器主视图（顶级 Tab · 盘口订单流仪表盘）
 *
 * 重构（2026-04）后定位：辅助参考工具，不再产出独立 snipe 信号。
 *   - 数据源分层：≤4% 走 5min 订单簿热力图；4-12% 走大单 lifecycle
 *   - 中性标签：wall_ask/wall_bid/wall_vanished/wall_broken（不再判真假）
 *   - 强度等级：S/A/B/C 按 USD 绝对阈值（30M/10M/3M/500K）
 *   - 数据来源（depth_5m / large_orders）以徽章呈现，挂单时长显式标注
 *
 * 视觉一致性：复用 StrongLevelsCard 的桶定义 / 星级 / 进度条
 * （提取复用思路而非代码复用——OP/KL 数据模型不同，硬复用会造成不相关耦合）
 */

// ── 配置常量 ─────────────────────────────────────────────────────────────

type Bucket = "near" | "mid" | "far";

const BUCKET_DEFS: Array<{
  key: Bucket;
  minPct: number;
  maxPct: number;
  label: string;
  hint: string;
  dotColor: string;
  textColor: string;
  bgColor: string;
}> = [
  {
    key: "near",
    minPct: 0.0,
    maxPct: 1.5,
    label: "近距",
    hint: "0~1.5% 5min 订单簿撮合面（depth_5m）",
    dotColor: "bg-sky-400",
    textColor: "text-sky-300",
    bgColor: "bg-sky-500/10",
  },
  {
    key: "mid",
    minPct: 1.5,
    maxPct: 4.0,
    label: "中距",
    hint: "1.5~4% 5min 订单簿撮合面（depth_5m）",
    dotColor: "bg-violet-400",
    textColor: "text-violet-300",
    bgColor: "bg-violet-500/10",
  },
  {
    key: "far",
    minPct: 4.0,
    maxPct: 12.0,
    label: "远距",
    hint: "4~12% 大单 lifecycle（large_orders）· 精确知挂单时长",
    dotColor: "bg-orange-400",
    textColor: "text-orange-300",
    bgColor: "bg-orange-500/10",
  },
];

const LABEL_STYLES: Record<WallLabel, { text: string; bg: string; fg: string; hint: string }> = {
  wall_ask: {
    text: "卖方挂单",
    bg: "bg-red-500/15",
    fg: "text-red-300",
    hint: "上方挂单墙，潜在阻力位（不判真假，按强度参考）",
  },
  wall_bid: {
    text: "买方挂单",
    bg: "bg-green-500/15",
    fg: "text-green-300",
    hint: "下方挂单墙，潜在支撑位（不判真假，按强度参考）",
  },
  wall_vanished: {
    text: "已消失",
    bg: "bg-slate-700/40",
    fg: "text-slate-400",
    hint: "上轮存在、本轮消失（撤单/吃单未区分）",
  },
  wall_broken: {
    text: "已穿越",
    bg: "bg-purple-500/15",
    fg: "text-purple-300",
    hint: "价格已穿越该墙",
  },
};

const SOURCE_STYLES: Record<WallSource, { text: string; bg: string; fg: string; hint: string }> = {
  depth_5m: {
    text: "5m订单簿",
    bg: "bg-blue-500/15",
    fg: "text-blue-300",
    hint: "数据源：5min 订单簿热力图（撮合面真实压力）",
  },
  large_orders: {
    text: "巨鲸大单",
    bg: "bg-amber-500/15",
    fg: "text-amber-300",
    hint: "数据源：单笔 ≥ $1M 大单 lifecycle（精确知挂单时长）",
  },
};

// ── 工具函数 ─────────────────────────────────────────────────────────────

function tierRank(t: string): number {
  if (t === "S") return 4;
  if (t === "A") return 3;
  if (t === "B") return 2;
  return 1;
}

function tierToStars(tier: string): number {
  if (tier === "S") return 5;
  if (tier === "A") return 4;
  if (tier === "B") return 3;
  return 2;
}

function formatTime(tsSec: number): string {
  if (!tsSec) return "-";
  const d = new Date(tsSec * 1000);
  return d.toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

function formatHoldingAge(sec: number): string {
  if (!sec || sec <= 0) return "";
  if (sec >= 86400) return `已挂 ${Math.floor(sec / 86400)} 天`;
  if (sec >= 3600) return `已挂 ${Math.floor(sec / 3600)} 小时`;
  if (sec >= 60) return `已挂 ${Math.floor(sec / 60)} 分钟`;
  return `已挂 ${sec} 秒`;
}

interface Picked {
  wall: PressureWall | null;
  bucket: Bucket;
}

/**
 * 按距离段分桶选位：每桶独立选 tier 最高 + score 最高的代表。
 */
function pickByBuckets(walls: PressureWall[], side: "ask" | "bid"): Picked[] {
  const sameSide = walls.filter((w) => w.side === side);
  return BUCKET_DEFS.map((b) => {
    const inBucket = sameSide.filter((w) => {
      const abs = Math.abs(w.distance_pct);
      return abs >= b.minPct && abs < b.maxPct;
    });
    if (inBucket.length === 0) return { wall: null, bucket: b.key };
    const sorted = [...inBucket].sort((a, c) => {
      const rd = tierRank(c.strength_tier) - tierRank(a.strength_tier);
      if (rd !== 0) return rd;
      const sd = c.strength_score - a.strength_score;
      if (sd !== 0) return sd;
      return Math.abs(a.distance_pct) - Math.abs(c.distance_pct);
    });
    return { wall: sorted[0], bucket: b.key };
  });
}

function getBucketDef(key: Bucket) {
  return BUCKET_DEFS.find((b) => b.key === key)!;
}

// ── 主视图 ───────────────────────────────────────────────────────────────

export default function OrderbookPressureView() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const snap = data?.orderbook_pressure;

  if (!snap) {
    return (
      <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg px-4 py-6 text-center text-sm text-slate-500">
        🧱 挂单压力监测器：等待数据接通（首次轮询 ~90 秒）...
        <div className="text-xs text-slate-600 mt-1">
          切换币种后会触发激活，请稍等
        </div>
      </div>
    );
  }

  // M1+M2 升级路径：有 wall_zones 时优先展示墙区视图（更直接回答 6 大诉求）
  const hasWallZones = (snap.walls_above?.length || 0) + (snap.walls_below?.length || 0) > 0;
  const isWarming = snap.data_quality === "warming";

  return (
    <div className="space-y-3">
      <Banner />
      <Header snap={snap} coin={coin} />
      {hasWallZones || isWarming ? (
        <LiquidityWallCard
          walls_above={snap.walls_above || []}
          walls_below={snap.walls_below || []}
          crowding={snap.crowding_global || null}
          isWarming={isWarming}
          historyWindowMinutes={snap.history_window_minutes || 60}
          historySize={snap.sample_count_depth_history || 0}
          lastPrice={snap.last_price}
          coin={coin}
        />
      ) : (
        <StrongPressureCard walls={snap.walls || []} price={snap.last_price} coin={coin} />
      )}
      <DetailDrawer />
      <Footer snap={snap} />
    </div>
  );
}

// ── 横幅：明确"辅助参考"定位 ────────────────────────────────────────────

function Banner() {
  return (
    <div className="bg-blue-950/30 border border-blue-700/40 rounded-lg px-3 py-2 text-[11px] text-blue-200/80 leading-relaxed">
      <span className="font-semibold text-blue-200">📊 辅助参考工具</span>
      <span className="text-blue-300/60"> · </span>
      仅展示当前盘口订单流的强度分级（不判真假、不发独立信号）。建议与「关键位」「市场行为分析」配合使用：
      <span className="text-blue-300/80"> S/A 级</span> 是值得关注的强压力，
      <span className="text-slate-400">B/C 级</span> 仅作背景信息。
    </div>
  );
}

// ── Header（顶部摘要） ───────────────────────────────────────────────────

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
          <span className="text-base">🧱</span>
          <h3 className="text-sm font-semibold text-slate-200">挂单压力监测器</h3>
          <span className="text-[10px] text-slate-500">
            {snap.walls.length} 个堆 · 现价{" "}
            <span className="font-mono text-slate-300">
              {formatPrice(snap.last_price, coin)}
            </span>
          </span>
        </div>
        <span className="text-[10px] text-slate-500">更新 {formatTime(snap.ts_sec)}</span>
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

// ── 强卖/强买墙卡（按桶分组，仿 StrongLevelsCard） ──────────────────────

function StrongPressureCard({
  walls, price, coin,
}: {
  walls: PressureWall[]; price: number; coin: string;
}) {
  const [tab, setTab] = useState<"ask" | "bid">("ask");

  const picked = useMemo(() => pickByBuckets(walls, tab), [walls, tab]);
  const validCount = picked.filter((p) => p.wall !== null).length;

  const titleCn = tab === "ask" ? "卖方挂单墙" : "买方挂单墙";
  const sideColor = tab === "ask" ? "text-red-400" : "text-green-400";
  const barColor = tab === "ask" ? "bg-red-500" : "bg-green-500";
  const bgTint = tab === "ask" ? "bg-red-950/10" : "bg-green-950/10";

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg overflow-hidden">
      {/* 顶部：标题 + Tab */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-700 bg-slate-800/80">
        <div className="flex items-center gap-2">
          <span className="text-base">🧱</span>
          <h3 className="text-sm font-semibold text-slate-200">{titleCn}</h3>
          <span className="text-[10px] text-slate-500">近 · 中 · 远 各 1 位</span>
        </div>
        <div className="flex gap-1 bg-slate-900/60 rounded-md p-0.5">
          <button
            type="button"
            onClick={() => setTab("ask")}
            className={`px-2.5 py-0.5 text-xs rounded transition-colors ${
              tab === "ask"
                ? "bg-red-500/20 text-red-300 font-medium"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            卖方
          </button>
          <button
            type="button"
            onClick={() => setTab("bid")}
            className={`px-2.5 py-0.5 text-xs rounded transition-colors ${
              tab === "bid"
                ? "bg-green-500/20 text-green-300 font-medium"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            买方
          </button>
        </div>
      </div>

      {/* 距离段说明条 */}
      <div className="px-4 py-1.5 bg-slate-900/40 border-b border-slate-700/60 flex items-center gap-3 text-[10px] text-slate-500">
        <span>💡 距离段：</span>
        {BUCKET_DEFS.map((b) => (
          <span key={b.key} className="inline-flex items-center gap-1" title={b.hint}>
            <span className={`w-1.5 h-1.5 rounded-full ${b.dotColor}`} />
            <span className={b.textColor}>{b.label}</span>
            <span className="text-slate-600">{b.minPct}-{b.maxPct}%</span>
          </span>
        ))}
      </div>

      {/* 卡片内容 */}
      <div className={`divide-y divide-slate-700/50 ${bgTint}`}>
        {validCount === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-slate-500">
            ±12% 内暂无满足条件的{titleCn}
            <br />
            <span className="text-[10px] text-slate-600">
              （wall_min_usd=$500K 阈值过滤；价格可能处于挂单稀疏区）
            </span>
          </div>
        ) : (
          picked.map((p, i) => (
            <BucketBlock
              key={`${tab}-${p.bucket}-${p.wall?.price_mid ?? "empty"}-${i}`}
              picked={p}
              price={price}
              coin={coin}
              sideColor={sideColor}
              barColor={barColor}
            />
          ))
        )}
      </div>
    </div>
  );
}

function BucketBlock({
  picked, price, coin, sideColor, barColor,
}: {
  picked: Picked;
  price: number;
  coin: string;
  sideColor: string;
  barColor: string;
}) {
  const bucketDef = getBucketDef(picked.bucket);

  if (picked.wall === null) {
    return (
      <div className="px-4 py-2 flex items-center gap-2 opacity-60">
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] ${bucketDef.bgColor} ${bucketDef.textColor} shrink-0`}
          title={bucketDef.hint}
        >
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${bucketDef.dotColor} mr-1 align-middle`} />
          {bucketDef.label}
        </span>
        <span className="text-[11px] text-slate-500">该距离段暂无挂单墙（跳过）</span>
      </div>
    );
  }

  return (
    <WallBlock
      wall={picked.wall}
      bucketDef={bucketDef}
      price={price}
      coin={coin}
      sideColor={sideColor}
      barColor={barColor}
    />
  );
}

function WallBlock({
  wall, bucketDef, price, coin, sideColor, barColor,
}: {
  wall: PressureWall;
  bucketDef: (typeof BUCKET_DEFS)[number];
  price: number;
  coin: string;
  sideColor: string;
  barColor: string;
}) {
  const stars = tierToStars(wall.strength_tier);
  const labelStyle = LABEL_STYLES[wall.label];
  const sourceStyle = SOURCE_STYLES[wall.source];
  const distPct = wall.distance_pct;
  // 进度条按 tier 派生：S=100% / A=75% / B=50% / C=25%
  const barWidth =
    wall.strength_tier === "S" ? 100
    : wall.strength_tier === "A" ? 75
    : wall.strength_tier === "B" ? 50 : 25;
  const ageText = formatHoldingAge(wall.holding_avg_age_sec);

  return (
    <div className="px-4 py-3">
      {/* 第一行：距离段徽标 + 价格 + 距离 + 星级 + Tier */}
      <div className="flex items-center gap-3 mb-1.5">
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] ${bucketDef.bgColor} ${bucketDef.textColor} shrink-0 inline-flex items-center gap-1`}
          title={bucketDef.hint}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${bucketDef.dotColor}`} />
          {bucketDef.label}
        </span>
        <span className={`text-lg font-mono font-bold ${sideColor} shrink-0`}>
          {formatPrice(wall.price_mid, coin)}
        </span>
        <span
          className={`text-xs font-mono shrink-0 ${
            distPct >= 0 ? "text-red-400" : "text-green-400"
          }`}
          title="距当前价百分比"
        >
          {distPct >= 0 ? "+" : ""}{distPct.toFixed(2)}%
        </span>
        <div className="flex-1" />
        <span
          className="text-xs tracking-tighter shrink-0"
          title={`等级 ${wall.strength_tier}（按 USD 阈值：S ≥ $30M / A ≥ $10M / B ≥ $3M / C ≥ $500K）`}
        >
          {"★".repeat(stars)}
          <span className="text-slate-700">{"★".repeat(5 - stars)}</span>
        </span>
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold shrink-0 ${
            wall.strength_tier === "S"
              ? "bg-amber-500/20 text-amber-400"
              : wall.strength_tier === "A"
                ? "bg-red-500/15 text-red-400"
                : wall.strength_tier === "B"
                  ? "bg-blue-500/15 text-blue-400"
                  : "bg-slate-600/30 text-slate-400"
          }`}
        >
          {wall.strength_tier}
        </span>
      </div>

      {/* 进度条 */}
      <div className="h-1.5 bg-slate-700/50 rounded-full overflow-hidden mb-2">
        <div
          className={`h-full ${barColor} rounded-full transition-all`}
          style={{ width: `${barWidth}%`, opacity: 0.4 + barWidth * 0.006 }}
        />
      </div>

      {/* 标签 + 数据源 + 时长 + 共振徽章 */}
      <div className="flex items-center gap-1.5 flex-wrap mb-1">
        <span
          className={`px-1.5 py-0.5 rounded text-[11px] cursor-help ${labelStyle.bg} ${labelStyle.fg}`}
          title={labelStyle.hint}
        >
          {labelStyle.text}
        </span>
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] cursor-help ${sourceStyle.bg} ${sourceStyle.fg}`}
          title={sourceStyle.hint}
        >
          📡 {sourceStyle.text}
        </span>
        {ageText && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-slate-700/40 text-slate-300 cursor-help"
            title="该价位大单加权平均挂单时长（仅 large_orders 路径有意义）"
          >
            ⏱ {ageText}
          </span>
        )}
        {wall.has_active_whale && wall.source === "depth_5m" && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/20 text-amber-300 cursor-help"
            title={`该 depth_5m 墙覆盖 ${wall.large_order_count} 笔活跃大单`}
          >
            🐳 大单 ×{wall.large_order_count}
          </span>
        )}
        {wall.confluence_with_absorption && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/20 text-cyan-300 cursor-help"
            title="与 footprint absorption_zone 共振 (强度 ×1.2)"
          >
            ✓ 吸收共振
          </span>
        )}
      </div>

      {/* 数额 */}
      <div className="flex items-center gap-1.5 text-[11px]">
        <span className="text-slate-500 shrink-0">💰 数额：</span>
        <span className="font-mono text-slate-300">{formatCnUsd(wall.size_usd)}</span>
      </div>
      {wall.reason && (
        <div className="mt-1 text-[10px] text-slate-500 leading-snug">{wall.reason}</div>
      )}

      {/* 与现价相对关系 */}
      {price > 0 && (
        <div className="mt-2 pt-2 border-t border-slate-700/30 flex items-center gap-2 text-[10px] text-slate-500">
          <span>与当前价 {formatPrice(price, coin)} 相比：</span>
          <span className={distPct >= 0 ? "text-red-400" : "text-green-400"}>
            {distPct >= 0 ? "↑ 上方" : "↓ 下方"} {Math.abs(distPct).toFixed(2)}%
          </span>
        </div>
      )}
    </div>
  );
}

// ── 详情折叠区 ──────────────────────────────────────────────────────────

function DetailDrawer() {
  const [open, setOpen] = useState(false);
  return (
    <div className="bg-slate-800/30 border border-slate-700/40 rounded-lg overflow-hidden">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="w-full px-4 py-2 text-xs text-slate-400 hover:text-slate-200 hover:bg-slate-700/20 transition-colors flex items-center justify-between"
      >
        <span>
          {open ? "▼" : "▶"} 展开完整明细（所有 wall · 高阶视图）
        </span>
        <span className="text-[10px] text-slate-600">高阶分析视图</span>
      </button>
      {open && (
        <div className="border-t border-slate-700/40 p-3">
          <OrderbookPressureCard />
        </div>
      )}
    </div>
  );
}

// ── 底部元数据 ──────────────────────────────────────────────────────────

function Footer({ snap }: { snap: OrderbookPressureSnapshot }) {
  const qualityColor =
    snap.data_quality === "ok"
      ? "text-emerald-400"
      : snap.data_quality === "partial"
        ? "text-yellow-400"
        : snap.data_quality === "warming"
          ? "text-amber-400"
          : snap.data_quality === "stale"
            ? "text-orange-400"
            : "text-slate-500";
  const histSize = snap.sample_count_depth_history || 0;
  const histWindow = snap.history_window_minutes || 60;
  return (
    <div className="text-[10px] text-slate-500 flex flex-wrap gap-x-4 gap-y-1 px-1">
      <span>
        depth 样本 <span className="text-slate-300">{snap.sample_count_depth}</span>
      </span>
      <span title={`滚动历史：${histSize} 帧 / 目标 ${histWindow / 5} 帧`}>
        滚动历史 <span className="text-slate-300">{histSize}/{histWindow / 5}</span> 帧
      </span>
      <span>
        大单 lifecycle{" "}
        <span className="text-slate-300">{snap.sample_count_large_history}</span>
      </span>
      <span>
        墙区 <span className="text-slate-300">{(snap.walls_above?.length || 0) + (snap.walls_below?.length || 0)}</span>
      </span>
      <span>
        事件 <span className="text-slate-300">{snap.wall_events?.length || 0}</span>
      </span>
      <span>
        数据质量 <span className={qualityColor}>{snap.data_quality}</span>
      </span>
      <span className="text-slate-600">
        ⓘ 超出 ±12% 的远距压力请查看「关键位」Tab（清算地图、200日均线等）
      </span>
      {snap.notes.length > 0 && (
        <span className="text-slate-500">notes: {snap.notes.join(", ")}</span>
      )}
    </div>
  );
}
