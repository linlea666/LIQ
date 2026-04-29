"use client";

/**
 * 流动性墙引擎主视图卡片（M1+M2）
 *
 * 回答用户的 6 大诉求：
 *   1. 上方哪里有卖墙          → walls_above
 *   2. 下方哪里有买墙          → walls_below
 *   3. 多厚 / 多久 / 多源确认    → current_usd / max_usd_1h / persistence_minutes / exchange_count
 *   4. 增强 / 减弱 / 撤 / 吃 / 重挂 → status + trend 徽标
 *   5. OI / 清算 / Funding / 多空拥挤 → crowding_context.explain_chips（每 zone 自携）
 *   6. 打穿后下一个磁铁         → sweep_target.magnet_price + vacuum_gap + break_through_risk
 *
 * 暖机期（< 30min）展示"暖机中"提示，不显示 magnet 数据避免误导。
 */

import { useState } from "react";
import { formatCnUsd, formatPrice } from "@/lib/format";
import type {
  PositionCrowdingSnapshot,
  WallEvent,
  WallEventType,
  WallZone,
  WallZoneStatus,
  WallZoneTrend,
} from "@/lib/types";

// ── 状态徽标样式 ────────────────────────────────────────────────────────
const STATUS_STYLES: Record<WallZoneStatus, { text: string; bg: string; fg: string; hint: string }> = {
  active: {
    text: "稳定",
    bg: "bg-slate-600/30",
    fg: "text-slate-300",
    hint: "当前墙区稳定，无显著行为变化",
  },
  strengthening: {
    text: "增厚",
    bg: "bg-emerald-500/20",
    fg: "text-emerald-300",
    hint: "墙厚度持续上升（current 显著高于 1h 均值）",
  },
  weakening: {
    text: "减薄",
    bg: "bg-yellow-500/20",
    fg: "text-yellow-300",
    hint: "墙厚度持续下降（current 显著低于 1h 均值）",
  },
  removed: {
    text: "已撤",
    bg: "bg-orange-500/25",
    fg: "text-orange-300",
    hint: "近期大额限价单未成交结束（撤单风险，但不等于假单）",
  },
  consumed: {
    text: "已被吃",
    bg: "bg-red-500/25",
    fg: "text-red-300",
    hint: "已有大额限价单被市价单成交消耗（硬证据）",
  },
  reloaded: {
    text: "重挂",
    bg: "bg-violet-500/20",
    fg: "text-violet-300",
    hint: "撤单后短时间内同价位重新挂单（可能有大资金防守）",
  },
  absorbed: {
    text: "吸收守住",
    bg: "bg-cyan-500/20",
    fg: "text-cyan-300",
    hint: "被攻击但守住（与 footprint absorption_zone 共振）",
  },
  unknown: {
    text: "数据中",
    bg: "bg-slate-700/30",
    fg: "text-slate-400",
    hint: "数据不足以判定状态",
  },
};

const TREND_STYLES: Record<WallZoneTrend, { icon: string; color: string; hint: string }> = {
  new: { icon: "✨", color: "text-blue-300", hint: "首次出现（< 3 帧）" },
  strengthening: { icon: "↑", color: "text-emerald-300", hint: "current 较 1h 均值 +20% 以上" },
  weakening: { icon: "↓", color: "text-yellow-300", hint: "current 较 1h 均值 -15% 以下" },
  stable: { icon: "─", color: "text-slate-400", hint: "厚度稳定" },
};

const TIER_STYLES: Record<string, { bg: string; fg: string }> = {
  S: { bg: "bg-amber-500/20", fg: "text-amber-400" },
  A: { bg: "bg-red-500/15", fg: "text-red-400" },
  B: { bg: "bg-blue-500/15", fg: "text-blue-400" },
  C: { bg: "bg-slate-600/30", fg: "text-slate-400" },
};

// ── 事件类型样式（对应诉求 4：增强 / 减弱 / 撤掉 / 被吃 / 重挂 / W2-T5 试盘+撤退） ──
const EVENT_STYLES: Record<WallEventType, { label: string; color: string; icon: string }> = {
  wall_appeared:     { label: "出现",   color: "text-blue-300",    icon: "✨" },
  wall_strengthened: { label: "增厚",   color: "text-emerald-300", icon: "↑" },
  wall_weakened:     { label: "减薄",   color: "text-yellow-300",  icon: "↓" },
  wall_removed:      { label: "撤掉",   color: "text-orange-300",  icon: "✗" },
  wall_consumed:     { label: "被吃",   color: "text-red-300",     icon: "🔥" },
  wall_reloaded:     { label: "重挂",   color: "text-violet-300",  icon: "↻" },
  // W2-T5：第 7 类复合事件 — 同帧既被部分吃单又被部分撤单（机构试盘 + 撤退 footprint）
  // 紫色 + 醒目图标，区分单一 consumed (红) / removed (橙)
  wall_consumed_and_removed: { label: "试盘+撤", color: "text-fuchsia-300", icon: "⚡" },
};

// ── 主入口 ──────────────────────────────────────────────────────────────
interface Props {
  walls_above: WallZone[];
  walls_below: WallZone[];
  wall_events: WallEvent[];
  crowding: PositionCrowdingSnapshot | null;
  isWarming: boolean;
  historyWindowMinutes: number;
  historySize: number;
  lastPrice: number;
  coin: string;
  /** W2-T4：顶层 USD/USDT 基差（仅 BTC 等可对比 Coinbase 的币种有值）
   *  正常 < 5bp（0.05%），≥ 5bp 时显示警示 chip */
  usdUsdtBasisPct?: number | null;
}

// Phase B：来源筛选 4 档（与 WallZoneSource 对齐）
type SourceFilter = "all" | "dual" | "spot" | "futures";

function classifyZoneSource(z: WallZone): Exclude<SourceFilter, "all"> {
  if (z.dual_source) return "dual";
  if (z.source === "spot_only") return "spot";
  return "futures";
}

function filterZonesBySource(zones: WallZone[], filter: SourceFilter): WallZone[] {
  if (filter === "all") return zones;
  return zones.filter((z) => classifyZoneSource(z) === filter);
}

export default function LiquidityWallCard({
  walls_above,
  walls_below,
  wall_events,
  crowding,
  isWarming,
  historyWindowMinutes,
  historySize,
  lastPrice,
  coin,
  usdUsdtBasisPct,
}: Props) {
  const [filter, setFilter] = useState<SourceFilter>("all");

  // 各档计数（用于 tab 数字徽标）
  const allZones = [...walls_above, ...walls_below];
  const counts = {
    all: allZones.length,
    dual: allZones.filter((z) => z.dual_source).length,
    spot: allZones.filter((z) => z.source === "spot_only").length,
    futures: allZones.filter((z) => !z.dual_source && z.source !== "spot_only").length,
  };

  const filteredAbove = filterZonesBySource(walls_above, filter);
  const filteredBelow = filterZonesBySource(walls_below, filter);

  return (
    <div className="space-y-3">
      {isWarming && <WarmingBanner historySize={historySize} historyWindowMinutes={historyWindowMinutes} />}
      {crowding && <CrowdingChips crowding={crowding} />}
      <BasisAlertChip basisPct={usdUsdtBasisPct} />
      <SourceFilterTabs filter={filter} setFilter={setFilter} counts={counts} />
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        <WallSideCard
          title="上方卖墙"
          icon="🟥"
          sideColor="text-red-400"
          accent="red"
          zones={filteredAbove}
          isWarming={isWarming}
          lastPrice={lastPrice}
          coin={coin}
          emptyText={
            filter === "all"
              ? "上方 ±12% 内暂无满足条件的卖墙"
              : `上方 ±12% 内暂无符合「${filterLabel(filter)}」的卖墙`
          }
        />
        <WallSideCard
          title="下方买墙"
          icon="🟩"
          sideColor="text-emerald-400"
          accent="emerald"
          zones={filteredBelow}
          isWarming={isWarming}
          lastPrice={lastPrice}
          coin={coin}
          emptyText={
            filter === "all"
              ? "下方 ±12% 内暂无满足条件的买墙"
              : `下方 ±12% 内暂无符合「${filterLabel(filter)}」的买墙`
          }
        />
      </div>
      {!isWarming && <WallEventsTimeline events={wall_events} coin={coin} />}
    </div>
  );
}

function filterLabel(f: SourceFilter): string {
  return ({ all: "全部", dual: "双源", spot: "仅现货", futures: "仅合约" } as const)[f];
}

// ── Phase B：来源筛选 tabs（合约 / 现货 / 双源 三视角） ──────────────────
function SourceFilterTabs({
  filter,
  setFilter,
  counts,
}: {
  filter: SourceFilter;
  setFilter: (f: SourceFilter) => void;
  counts: Record<SourceFilter, number>;
}) {
  const TABS: Array<{ id: SourceFilter; label: string; emoji: string; hint: string; activeCls: string }> = [
    {
      id: "all",
      label: "全部",
      emoji: "📊",
      hint: "全部 zone（合约 + 现货 + 双源）",
      activeCls: "bg-slate-600/40 text-slate-100 border-slate-400/40",
    },
    {
      id: "dual",
      label: "双源高可信",
      emoji: "💎",
      hint: "现货 + 合约 5m 同价区共振 → 真买卖家与杠杆资金共同布局",
      activeCls: "bg-amber-500/30 text-amber-100 border-amber-400/40",
    },
    {
      id: "spot",
      label: "仅现货",
      emoji: "💰",
      hint: "现货独立墙：合约同价位无显著厚度，可能是真长线买卖家",
      activeCls: "bg-cyan-500/30 text-cyan-100 border-cyan-400/40",
    },
    {
      id: "futures",
      label: "仅合约",
      emoji: "⚡",
      hint: "合约源墙（无现货共振）：可能是清算磁铁 / spoof / 短期挂单",
      activeCls: "bg-orange-500/25 text-orange-100 border-orange-400/40",
    },
  ];
  return (
    <div className="flex flex-wrap items-center gap-1.5 text-[11px]">
      <span className="text-[10px] text-slate-500 mr-1">来源筛选</span>
      {TABS.map((t) => {
        const active = filter === t.id;
        const cls = active
          ? t.activeCls
          : "bg-slate-800/50 text-slate-400 border-slate-700/50 hover:bg-slate-700/40";
        return (
          <button
            key={t.id}
            type="button"
            onClick={() => setFilter(t.id)}
            title={t.hint}
            className={`px-2 py-1 rounded border transition-colors ${cls}`}
          >
            <span>{t.emoji} {t.label}</span>
            <span className="ml-1.5 font-mono text-[10px] opacity-70">{counts[t.id]}</span>
          </button>
        );
      })}
    </div>
  );
}

// ── 行为事件流（对应诉求 4：增强/减弱/撤掉/被吃/重挂） ─────────────────
function WallEventsTimeline({ events, coin }: { events: WallEvent[]; coin: string }) {
  const [open, setOpen] = useState(false);
  if (!events || events.length === 0) {
    return (
      <div className="bg-slate-800/30 border border-slate-700/50 rounded-lg px-3 py-2 text-[11px] text-slate-500">
        📜 行为事件流：1h 内无显著事件（暖机后才记录；市场平淡时正常）
      </div>
    );
  }
  // 倒序展示（最新在前）
  const sorted = [...events].sort((a, b) => b.ts_sec - a.ts_sec);
  const visible = open ? sorted : sorted.slice(0, 5);

  return (
    <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg overflow-hidden">
      <div className="px-3 py-2 border-b border-slate-700/50 flex items-center gap-2 text-[12px]">
        <span className="font-semibold text-slate-300">📜 行为事件流</span>
        <span className="text-[10px] text-slate-500">最近 {sorted.length} 条 · 倒序</span>
        <div className="flex-1" />
        {sorted.length > 5 && (
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-[11px] text-slate-400 hover:text-slate-200"
          >
            {open ? `收起（仅看前 5 条）` : `展开全部 ${sorted.length} 条`}
          </button>
        )}
      </div>
      <div className="divide-y divide-slate-700/40">
        {visible.map((e, i) => (
          <EventRow key={`${e.ts_sec}-${e.price_mid}-${i}`} event={e} coin={coin} />
        ))}
      </div>
    </div>
  );
}

function EventRow({ event, coin }: { event: WallEvent; coin: string }) {
  const style = EVENT_STYLES[event.event_type];
  const sideColor = event.side === "ask" ? "text-red-400" : "text-emerald-400";
  const sideText = event.side === "ask" ? "卖墙" : "买墙";

  // 时间格式 hh:mm
  const d = new Date(event.ts_sec * 1000);
  const hh = String(d.getHours()).padStart(2, "0");
  const mm = String(d.getMinutes()).padStart(2, "0");

  // 厚度变化
  const sizeBefore = event.size_before_usd;
  const sizeAfter = event.size_after_usd;
  let deltaText: string | null = null;
  if (sizeBefore != null && sizeAfter != null) {
    const delta = sizeAfter - sizeBefore;
    deltaText = (delta >= 0 ? "+" : "") + formatCnUsd(delta);
  } else if (sizeBefore != null && event.event_type === "wall_removed") {
    deltaText = "-" + formatCnUsd(sizeBefore);
  } else if (sizeAfter != null && event.event_type === "wall_appeared") {
    deltaText = "+" + formatCnUsd(sizeAfter);
  }

  return (
    <div className="px-3 py-1.5 flex items-center gap-2 text-[11px] hover:bg-slate-700/15 transition-colors">
      <span className="font-mono text-slate-500 shrink-0">{hh}:{mm}</span>
      <span className={`font-mono shrink-0 ${sideColor}`}>{formatPrice(event.price_mid, coin)}</span>
      <span className="text-slate-500 shrink-0">{sideText}</span>
      <span className={`shrink-0 ${style.color}`} title={event.explain}>
        {style.icon} {style.label}
      </span>
      {deltaText && (
        <span className="font-mono text-slate-400 shrink-0">{deltaText}</span>
      )}
      {event.executed_usd_value != null && event.executed_usd_value > 0 && (
        <span className="font-mono text-red-300 shrink-0" title="期间被市价单成交的金额">
          🔥 吃 {formatCnUsd(event.executed_usd_value)}
        </span>
      )}
      <span className="text-[10px] text-slate-500 truncate" title={event.explain}>
        {event.explain}
      </span>
      <div className="flex-1" />
      {event.confidence > 0 && (
        <span className="text-[10px] text-slate-500 font-mono shrink-0" title="事件置信度">
          {Math.round(event.confidence * 100)}%
        </span>
      )}
    </div>
  );
}

// ── W2-T4：USD/USDT 基差警示 chip ─────────────────────────────────────
// 正常 BTC < 5bp（0.05%）。仅当 |basis| ≥ 5bp 时显示提醒，避免噪音。
// > 30bp 表示明显基差异常（高亮 amber/red），可能是 Coinbase 数据老化或市场极端
function BasisAlertChip({ basisPct }: { basisPct: number | null | undefined }) {
  if (basisPct == null || Math.abs(basisPct) < 0.05) return null;
  const abs = Math.abs(basisPct);
  const tone = abs >= 0.3 ? "amber" : "slate";
  const cls =
    tone === "amber"
      ? "bg-amber-500/15 border-amber-500/30 text-amber-300"
      : "bg-slate-700/30 border-slate-600/40 text-slate-300";
  const direction = basisPct > 0 ? "USD 溢价" : "USDT 溢价";
  return (
    <div className={`inline-flex items-center gap-2 px-2.5 py-1 rounded-md border text-[11px] ${cls}`}>
      <span>📊</span>
      <span>
        USD/USDT 基差 <span className="font-mono">{basisPct >= 0 ? "+" : ""}{basisPct.toFixed(3)}%</span>
        <span className="text-[10px] text-slate-400 ml-1">({direction})</span>
      </span>
      <span
        className="text-[10px] text-slate-500 cursor-help"
        title={
          `Coinbase mid 与 BTCUSDT last 价格比例。正常 < 5bp（0.05%），` +
          `≥ 30bp 表示明显基差异常。Coinbase 共振判定中容差已吸收 10bp 以内的差异。`
        }
      >
        ⓘ
      </span>
    </div>
  );
}

// ── 暖机横幅 ────────────────────────────────────────────────────────────
function WarmingBanner({ historySize, historyWindowMinutes }: { historySize: number; historyWindowMinutes: number }) {
  return (
    <div className="bg-amber-950/30 border border-amber-700/40 rounded-lg px-3 py-2 text-[11px] text-amber-200/80 leading-relaxed">
      <span className="font-semibold text-amber-200">⏳ 引擎暖机中</span>
      <span className="text-amber-300/60"> · </span>
      已采集 <span className="font-mono text-amber-200">{historySize}</span> 帧历史
      （目标 {historyWindowMinutes / 5} 帧 / {historyWindowMinutes}min）。
      暖机期内不显示「持续时间」「次磁铁」等需要历史滚动支撑的数字，避免误导。
    </div>
  );
}

// ── 全局拥挤度 chips（OI / Funding / LS / 推断仓位状态） ─────────────────
function CrowdingChips({ crowding }: { crowding: PositionCrowdingSnapshot }) {
  const oiH = crowding.oi_delta_1h_pct;
  const oiD = crowding.oi_delta_24h_pct;
  const fund = crowding.funding_now_pct;
  const fundColor =
    fund == null ? "text-slate-400" :
    fund >= 0.05 ? "text-red-300" :
    fund <= -0.02 ? "text-green-300" : "text-slate-400";
  const oiColor = (v: number | null) =>
    v == null ? "text-slate-400" :
    v >= 1 ? "text-emerald-300" :
    v <= -1 ? "text-yellow-300" : "text-slate-400";

  return (
    <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg px-3 py-2 flex items-center gap-x-3 gap-y-1.5 flex-wrap text-[11px]">
      <span className="text-slate-500 font-semibold">📊 拥挤度</span>
      {oiH != null && (
        <span title="过去 1 小时 OI 变化">
          <span className="text-slate-500">OI(1h):</span>{" "}
          <span className={`font-mono ${oiColor(oiH)}`}>
            {oiH >= 0 ? "+" : ""}{oiH.toFixed(2)}%
          </span>
        </span>
      )}
      {oiD != null && (
        <span title="过去 24 小时 OI 变化">
          <span className="text-slate-500">OI(24h):</span>{" "}
          <span className={`font-mono ${oiColor(oiD)}`}>
            {oiD >= 0 ? "+" : ""}{oiD.toFixed(2)}%
          </span>
        </span>
      )}
      {fund != null && (
        <span title="当前 funding rate">
          <span className="text-slate-500">Funding:</span>{" "}
          <span className={`font-mono ${fundColor}`}>
            {fund >= 0 ? "+" : ""}{fund.toFixed(3)}%
          </span>
        </span>
      )}
      {crowding.long_crowding_risk >= 0.6 && (
        <span className="px-1.5 py-0.5 rounded text-[10px] bg-red-500/15 text-red-300" title="多头拥挤度评分">
          多头拥挤 {Math.round(crowding.long_crowding_risk * 100)}%
        </span>
      )}
      {crowding.short_crowding_risk >= 0.6 && (
        <span className="px-1.5 py-0.5 rounded text-[10px] bg-green-500/15 text-green-300" title="空头拥挤度评分">
          空头拥挤 {Math.round(crowding.short_crowding_risk * 100)}%
        </span>
      )}
      {crowding.explain_chips.map((chip, i) => (
        <span key={i} className="px-1.5 py-0.5 rounded text-[10px] bg-slate-700/50 text-slate-300">
          {chip}
        </span>
      ))}
    </div>
  );
}

// ── 单侧墙列表卡片 ──────────────────────────────────────────────────────
function WallSideCard({
  title, icon, sideColor, accent, zones, isWarming, lastPrice, coin, emptyText,
}: {
  title: string;
  icon: string;
  sideColor: string;
  accent: "red" | "emerald";
  zones: WallZone[];
  isWarming: boolean;
  lastPrice: number;
  coin: string;
  emptyText: string;
}) {
  const headerBg = accent === "red" ? "bg-red-950/20" : "bg-emerald-950/20";
  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg overflow-hidden">
      <div className={`px-4 py-2.5 border-b border-slate-700 ${headerBg} flex items-center gap-2`}>
        <span>{icon}</span>
        <h3 className={`text-sm font-semibold ${sideColor}`}>{title}</h3>
        <span className="text-[10px] text-slate-500">{zones.length} 个墙区</span>
      </div>
      <div className="divide-y divide-slate-700/50">
        {zones.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-slate-500">{emptyText}</div>
        ) : (
          zones.map((z, i) => (
            <ZoneRow
              key={`${z.side}-${z.price_mid}-${i}`}
              zone={z}
              isWarming={isWarming}
              lastPrice={lastPrice}
              coin={coin}
              accent={accent}
            />
          ))
        )}
      </div>
    </div>
  );
}

// ── 单个墙区行 ─────────────────────────────────────────────────────────
function ZoneRow({
  zone, isWarming, lastPrice, coin, accent,
}: {
  zone: WallZone;
  isWarming: boolean;
  lastPrice: number;
  coin: string;
  accent: "red" | "emerald";
}) {
  const [open, setOpen] = useState(false);
  const tier = TIER_STYLES[zone.strength_tier];
  const status = STATUS_STYLES[zone.status];
  const trend = TREND_STYLES[zone.trend];
  const distColor = zone.distance_pct >= 0 ? "text-red-400" : "text-emerald-400";
  const barColor = accent === "red" ? "bg-red-500" : "bg-emerald-500";

  // 进度条按 tier：S=100/A=75/B=50/C=25
  const barWidth =
    zone.strength_tier === "S" ? 100 :
    zone.strength_tier === "A" ? 75 :
    zone.strength_tier === "B" ? 50 : 25;

  const persistText = isWarming
    ? "暖机中"
    : zone.persistence_score > 0
      ? `${Math.round(zone.visible_minutes)}min`
      : "新出现";

  return (
    <div className="px-4 py-3 hover:bg-slate-700/10 transition-colors">
      {/* 第一行：价区 + 峰值 + 距离 + Tier */}
      <div className="flex items-center gap-3 mb-1.5">
        <span className={`text-base font-mono font-bold ${zone.side === "ask" ? "text-red-400" : "text-emerald-400"} shrink-0`}>
          {formatPrice(zone.price_low, coin)} – {formatPrice(zone.price_high, coin)}
        </span>
        <span className={`text-xs font-mono shrink-0 ${distColor}`} title="距当前价">
          {zone.distance_pct >= 0 ? "+" : ""}{zone.distance_pct.toFixed(2)}%
        </span>
        <div className="flex-1" />
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold shrink-0 ${tier.bg} ${tier.fg}`}
          title={`等级 ${zone.strength_tier}（USD 阈值：S ≥ $30M / A ≥ $10M / B ≥ $3M）`}
        >
          {zone.strength_tier}
        </span>
      </div>

      {/* 进度条 */}
      <div className="h-1.5 bg-slate-700/50 rounded-full overflow-hidden mb-2">
        <div
          className={`h-full ${barColor} rounded-full transition-all`}
          style={{ width: `${barWidth}%`, opacity: 0.4 + barWidth * 0.006 }}
        />
      </div>

      {/* 数据条 */}
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-y-1 text-[11px] mb-1.5">
        <span>
          <span className="text-slate-500">当前:</span>{" "}
          <span className="font-mono text-slate-300">{formatCnUsd(zone.current_usd)}</span>
        </span>
        <span>
          <span className="text-slate-500">1h峰值:</span>{" "}
          <span className="font-mono text-slate-300">{formatCnUsd(zone.max_usd_1h)}</span>
        </span>
        <span title="持续可见时长（暖机期不展示数字）">
          <span className="text-slate-500">持续:</span>{" "}
          <span className={isWarming ? "text-amber-300" : "text-slate-300"}>{persistText}</span>
        </span>
        <span title="峰值价格 + 合并 bin 数">
          <span className="text-slate-500">峰值:</span>{" "}
          <span className="font-mono text-slate-300">{formatPrice(zone.peak_price, coin)}</span>
          <span className="text-slate-500"> ({zone.bin_count})</span>
        </span>
      </div>

      {/* 徽标条 */}
      <div className="flex items-center gap-1.5 flex-wrap">
        <span className={`px-1.5 py-0.5 rounded text-[10px] cursor-help ${status.bg} ${status.fg}`} title={status.hint}>
          {status.text}
        </span>
        <span className={`text-[10px] cursor-help ${trend.color}`} title={trend.hint}>
          {trend.icon}
        </span>
        {/* Phase A：来源徽章（dual_source 优先 > spot_only > spot_lo > 仅合约 + 高 trust） */}
        {zone.dual_source ? (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-amber-400/30 text-amber-200 font-semibold"
            title={
              `💎 现货+合约双源高可信墙（trust ${Math.round(zone.trust_score * 100)}%）：` +
              `两个独立订单簿都在该价位有 ≥ 阈值的厚度——真买卖家与杠杆资金共同布局，` +
              `是当前最强单一证据。现货侧厚度 ${formatCnUsd(zone.spot_current_usd)}（峰值 ${formatCnUsd(zone.spot_max_usd_1h)}）。`
            }
          >
            💎 双源{zone.side === "ask" ? "卖墙" : "买墙"}
          </span>
        ) : zone.source === "spot_only" ? (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/20 text-cyan-200"
            title={
              `仅现货墙（trust ${Math.round(zone.trust_score * 100)}%）：` +
              `合约 5m 热力图同价位无显著厚度，仅现货订单簿堆叠——多为真买卖家长线挂单，` +
              `但可能出现"合约杠杆资金未跟进"的风险。`
            }
          >
            💰 仅现货墙
          </span>
        ) : zone.has_spot_confluence ? (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-emerald-500/20 text-emerald-200"
            title={
              `现货大单共振（trust ${Math.round(zone.trust_score * 100)}%）：` +
              `合约墙背后有现货大单 lifecycle 加持，可信度高于纯合约挂单。`
            }
          >
            💰 现货共振 ×{zone.spot_large_order_ids.length}
          </span>
        ) : zone.trust_score >= 0.65 ? (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-blue-500/20 text-blue-200"
            title={`较可信合约墙（trust ${Math.round(zone.trust_score * 100)}%）：多所共振 + 持续`}
          >
            ⚡ 较可信
          </span>
        ) : zone.trust_score < 0.55 && zone.large_order_ids.length > 0 ? (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-orange-500/15 text-orange-300"
            title={
              `仅合约源（trust ${Math.round(zone.trust_score * 100)}%）：` +
              `可能是清算磁铁、spoof 或短期墙；结合下方"如果打穿"风险解读。`
            }
          >
            ⚡ 仅合约
          </span>
        ) : null}
        {zone.exchange_count >= 2 && (
          <span className="px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/20 text-cyan-300" title="多交易所共振">
            🌐 {zone.exchange_count}所共振
          </span>
        )}
        {zone.large_order_ids.length > 0 && (
          <span className="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/15 text-amber-300" title={`覆盖 ${zone.large_order_ids.length} 笔合约大单`}>
            🐳 合约大单 ×{zone.large_order_ids.length}
          </span>
        )}
        {/* W3-T1：Coinbase 现货共振叠加徽章（与 dual_source / has_spot_confluence 可同时显示） */}
        {zone.coinbase_spot_confluence && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-blue-500/15 text-blue-300"
            title={
              `🏦 Coinbase 现货共振：机构资金独立验证维度（与 Binance/OKX 系正交）。` +
              `Coinbase 同价区累计 ${formatCnUsd(zone.coinbase_spot_usd ?? 0)}` +
              `（${zone.coinbase_num_orders ?? 0} 笔订单 ≥ 阈值）`
            }
          >
            🏦 Coinbase {formatCnUsd(zone.coinbase_spot_usd ?? 0)}
          </span>
        )}
        {/* W3-T1 + W2-T4：机构单笔大单徽章 — 区分散户聚集 vs 机构布局 */}
        {(zone.coinbase_max_single_order_usd ?? 0) >= 100_000 && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-purple-500/15 text-purple-300"
            title={
              `💼 机构单笔大单：Coinbase 同价区单笔最大订单 ` +
              `${formatCnUsd(zone.coinbase_max_single_order_usd ?? 0)}（≥ 10 万 USD/笔）。` +
              `区分"散户 N 单聚集"vs"机构孤立巨单"，是支撑/阻力可信度的硬证据`
            }
          >
            💼 机构单笔 {formatCnUsd(zone.coinbase_max_single_order_usd ?? 0)}
          </span>
        )}
        {zone.confluence_with_absorption && (
          <span className="px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/20 text-cyan-300" title="与 footprint absorption 共振">
            ✓ 吸收共振
          </span>
        )}
        {zone.wall_consumed_confidence >= 0.5 && (
          <span className="px-1.5 py-0.5 rounded text-[10px] bg-red-500/20 text-red-300" title={`已被吃置信度 ${Math.round(zone.wall_consumed_confidence * 100)}%`}>
            🔥 吃 {Math.round(zone.wall_consumed_confidence * 100)}%
          </span>
        )}
        {zone.wall_removal_risk >= 0.6 && (
          <span className="px-1.5 py-0.5 rounded text-[10px] bg-orange-500/20 text-orange-300" title={`撤单风险评分 ${zone.wall_removal_risk.toFixed(2)}（0–1 评分，不等于"假单"也不是统计概率）`}>
            ⚠ 撤单风险
          </span>
        )}
        {zone.explain_chips.map((c, i) => (
          <span key={i} className="px-1.5 py-0.5 rounded text-[10px] bg-slate-700/40 text-slate-300">
            {c}
          </span>
        ))}
      </div>

      {/* "如果打穿"折叠态预览 + 详情（暖机期不展示） */}
      {!isWarming && zone.sweep_target && (
        <div className="mt-2 pt-2 border-t border-slate-700/30">
          <button
            type="button"
            onClick={() => setOpen((v) => !v)}
            className="text-[11px] text-slate-400 hover:text-slate-200 inline-flex items-center gap-1.5 flex-wrap w-full"
          >
            <span>{open ? "▼" : "▶"}</span>
            <span>如果打穿</span>
            <span className="text-slate-600">→</span>
            <span className="text-slate-500">磁铁</span>
            <span className="font-mono text-slate-300">
              {formatPrice(zone.sweep_target.magnet_price, coin)}
            </span>
            <span className={`font-mono ${zone.sweep_target.distance_pct >= 0 ? "text-red-300" : "text-emerald-300"}`}>
              ({zone.sweep_target.distance_pct >= 0 ? "+" : ""}{zone.sweep_target.distance_pct.toFixed(2)}%)
            </span>
            <span className="text-slate-600">·</span>
            <span className="text-slate-500">风险</span>
            <span
              className={`font-mono ${
                zone.break_through_risk >= 0.7 ? "text-red-300" :
                zone.break_through_risk >= 0.4 ? "text-yellow-300" : "text-slate-300"
              }`}
            >
              {Math.round(zone.break_through_risk * 100)}%
            </span>
            {zone.sweep_target.vacuum_gap_pct >= 0.5 && (
              <span className="text-red-300/80 text-[10px]">⚠ 真空 {zone.sweep_target.vacuum_gap_pct.toFixed(2)}%</span>
            )}
          </button>
          {open && (
            <BreakThroughCard zone={zone} lastPrice={lastPrice} coin={coin} />
          )}
        </div>
      )}
    </div>
  );
}

// ── "如果打穿"卡片 ──────────────────────────────────────────────────────
function BreakThroughCard({ zone, coin }: { zone: WallZone; lastPrice: number; coin: string }) {
  const sweep = zone.sweep_target!;
  const riskColor =
    zone.break_through_risk >= 0.7 ? "text-red-300" :
    zone.break_through_risk >= 0.4 ? "text-yellow-300" : "text-slate-300";

  const dirText = sweep.direction === "below" ? "下方" : "上方";
  const targetType = zone.side === "bid" ? "多头清算磁铁" : "空头清算磁铁";

  return (
    <div className="mt-2 bg-slate-900/40 border border-slate-700/40 rounded-md p-2.5 text-[11px] space-y-1">
      <div className="flex items-center gap-2">
        <span className="text-slate-500">磁铁:</span>
        <span className="font-mono text-slate-200">{formatPrice(sweep.magnet_price, coin)}</span>
        <span className="text-slate-600">·</span>
        <span className="text-slate-400">{dirText}{targetType}</span>
        <span className="text-slate-500">
          ({sweep.distance_pct >= 0 ? "+" : ""}{sweep.distance_pct.toFixed(2)}%)
        </span>
      </div>
      <div className="flex items-center gap-2">
        <span className="text-slate-500">清算金额:</span>
        <span className="font-mono text-slate-300">{formatCnUsd(sweep.magnet_amount_usd)}</span>
      </div>
      {sweep.vacuum_gap_pct > 0 && (
        <div className="flex items-center gap-2" title="墙区到磁铁之间最大相邻 bin 价差">
          <span className="text-slate-500">真空跨度:</span>
          <span className={`font-mono ${sweep.vacuum_gap_pct >= 0.5 ? "text-red-300" : "text-slate-300"}`}>
            {sweep.vacuum_gap_pct.toFixed(2)}%
          </span>
          {sweep.vacuum_gap_pct >= 0.5 && (
            <span className="text-[10px] text-red-300/70">⚠ 真空大，易加速</span>
          )}
        </div>
      )}
      <div className="flex items-center gap-2">
        <span className="text-slate-500">击穿风险:</span>
        <span className={`font-mono ${riskColor}`}>
          {Math.round(zone.break_through_risk * 100)}%
        </span>
        <span className="text-[10px] text-slate-600">
          (墙厚下滑 + 持续不足 + 磁铁近 + 真空 + 拥挤 综合)
        </span>
      </div>
    </div>
  );
}
