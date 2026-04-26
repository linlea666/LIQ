"use client";

import { useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import { formatCnUsd, formatPrice } from "@/lib/format";
import type {
  OrderbookPressureSnapshot,
  OrderbookPressureSignal,
  PressureWall,
  WallChangeKind,
  WallLabel,
} from "@/lib/types";
import OrderbookPressureCard from "./OrderbookPressureCard";

/**
 * 挂单压力监测器主视图（顶级 Tab）
 *
 * 设计思路：
 *   - 与「关键位」Tab 平级，但聚焦短中期订单流真实压力（非历史结构位）
 *   - 视觉语言完全对齐 StrongLevelsCard：Tab 切换卖墙/买墙 + 桶分组 + 卡片化
 *   - 距离桶与 KL 一致（近 0.25-1.5% / 中 1.5-4% / 远 4-12%），让两个模块互补
 *   - 默认展示「近/中/远 各 1 位」（取桶内 confidence 最高），底部"展开详情"折叠完整列表
 *
 * 数据来源（与 OrderbookPressureCard 共用）：
 *   - data.orderbook_pressure ← market_update WS 推送
 *   - obPressureSignalsByCoin ← orderbook_pressure_signal WS 推送
 *
 * 视觉一致性：复用 StrongLevelsCard 的桶定义 / 星级 / 进度条 / 状态语言
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
    hint: "0~1.5% 当前正在交锋的撮合压力",
    dotColor: "bg-sky-400",
    textColor: "text-sky-300",
    bgColor: "bg-sky-500/10",
  },
  {
    key: "mid",
    minPct: 1.5,
    maxPct: 4.0,
    label: "中距",
    hint: "1.5~4% 日内可达决战位",
    dotColor: "bg-violet-400",
    textColor: "text-violet-300",
    bgColor: "bg-violet-500/10",
  },
  {
    key: "far",
    minPct: 4.0,
    maxPct: 12.0,
    label: "远距",
    hint: "4~12% 1-2 天可达；超出 12% 进入清算地图辖区，请查看「关键位」Tab",
    dotColor: "bg-orange-400",
    textColor: "text-orange-300",
    bgColor: "bg-orange-500/10",
  },
];

const LABEL_STYLES: Record<WallLabel, { text: string; bg: string; fg: string; hint: string }> = {
  real_R: {
    text: "真阻力",
    bg: "bg-red-500/20",
    fg: "text-red-300",
    hint: "卖墙被吃但价格被压住 → 真实阻力（高确定性）",
  },
  fake_R: {
    text: "假阻力(撤)",
    bg: "bg-slate-600/30",
    fg: "text-slate-300",
    hint: "卖墙撤单且价格还没到，疑似 spoof 操纵",
  },
  fake_R_break: {
    text: "假阻力(已破)",
    bg: "bg-slate-700/40",
    fg: "text-slate-400",
    hint: "卖墙撤单后价格已突破，spoof 已确认，墙已失效",
  },
  real_S: {
    text: "真支撑",
    bg: "bg-green-500/20",
    fg: "text-green-300",
    hint: "买墙被吃但价格守住 → 真实支撑（高确定性）",
  },
  fake_S: {
    text: "假支撑(撤)",
    bg: "bg-slate-600/30",
    fg: "text-slate-300",
    hint: "买墙撤单且价格还没到，疑似 spoof 操纵",
  },
  fake_S_break: {
    text: "假支撑(已破)",
    bg: "bg-slate-700/40",
    fg: "text-slate-400",
    hint: "买墙撤单后价格已跌穿，spoof 已确认，墙已失效",
  },
  untested: {
    text: "待观察",
    bg: "bg-slate-700/30",
    fg: "text-slate-500",
    hint: "墙存在但价格还没接近，先观察",
  },
};

const CHANGE_LABELS: Record<WallChangeKind, { text: string; color: string; hint: string }> = {
  eaten: { text: "被吃", color: "text-amber-400", hint: "被市价单吃掉为主（≥70% executed）" },
  cancelled: { text: "撤单", color: "text-slate-400", hint: "被撤单为主（≥70% canceled）" },
  partial: { text: "部分", color: "text-slate-400", hint: "撤单与被吃各占一部分" },
  growing: { text: "堆积", color: "text-cyan-400", hint: "反而在增加（有人继续堆挂单）" },
  holding: { text: "保持", color: "text-blue-300", hint: "几乎没变化，墙仍挂着" },
  unknown: { text: "未知", color: "text-slate-500", hint: "数据不足无法判断" },
};

// ── 工具函数 ─────────────────────────────────────────────────────────────

function tierRank(t: string): number {
  if (t === "S") return 3;
  if (t === "A") return 2;
  if (t === "B") return 1;
  return 0;
}

function scoreToStars(conf: number): number {
  // confidence 0-100 → 1-5 星
  return Math.max(1, Math.min(5, Math.round(conf / 20)));
}

function formatTime(tsSec: number): string {
  if (!tsSec) return "-";
  const d = new Date(tsSec * 1000);
  return d.toLocaleTimeString("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
}

interface Picked {
  wall: PressureWall | null;
  bucket: Bucket;
}

/**
 * 按距离段分桶选位：每桶独立选 tier 最高 + confidence 最高的代表。
 *
 * 选位策略与 StrongLevelsCard 完全一致，确保两个模块视觉语言对齐。
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
      const cd = c.confidence - a.confidence;
      if (cd !== 0) return cd;
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
  const signalsAll = useMarketStore((s) => s.obPressureSignalsByCoin);
  const snap = data?.orderbook_pressure;
  const signals = useMemo(() => signalsAll[coin] ?? [], [signalsAll, coin]);

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

  return (
    <div className="space-y-3">
      <Header snap={snap} coin={coin} />
      <SignalsList signals={signals} coin={coin} />
      <StrongPressureCard walls={snap.walls || []} price={snap.last_price} coin={coin} />
      <DetailDrawer />
      <Footer snap={snap} />
    </div>
  );
}

// ── Header（顶部摘要） ───────────────────────────────────────────────────

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

// ── 信号列表 ─────────────────────────────────────────────────────────────

function SignalsList({
  signals, coin,
}: {
  signals: OrderbookPressureSignal[]; coin: string;
}) {
  if (signals.length === 0) {
    return (
      <div className="bg-slate-800/30 border border-slate-700/50 rounded-lg px-4 py-2 text-xs text-slate-500 text-center">
        🎯 挂单压力 snipe 信号：暂无新触发（同价位 30 min 内不重复推送）
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
              title={sig.reason}
            >
              <span
                className={`text-[11px] font-bold px-1.5 py-0.5 rounded ${
                  isLong ? "bg-green-500/30 text-green-300" : "bg-red-500/30 text-red-300"
                }`}
              >
                {isLong ? "做多" : "做空"}
              </span>
              <span
                className={`text-[11px] px-1.5 py-0.5 rounded ${labelStyle.bg} ${labelStyle.fg}`}
                title={labelStyle.hint}
              >
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

// ── 强卖/强买墙卡（按桶分组，仿 StrongLevelsCard） ──────────────────────

function StrongPressureCard({
  walls, price, coin,
}: {
  walls: PressureWall[]; price: number; coin: string;
}) {
  const [tab, setTab] = useState<"ask" | "bid">("ask");

  const picked = useMemo(() => pickByBuckets(walls, tab), [walls, tab]);
  const validCount = picked.filter((p) => p.wall !== null).length;

  const titleCn = tab === "ask" ? "强卖墙（阻力）" : "强买墙（支撑）";
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
            卖墙
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
            买墙
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
              （wall_min_usd=$500K 双闸过滤；价格可能处于挂单稀疏区）
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
  const stars = scoreToStars(wall.confidence);
  const labelStyle = LABEL_STYLES[wall.label];
  const change = CHANGE_LABELS[wall.change_kind];
  const distPct = wall.distance_pct;
  const barWidth = Math.min(100, wall.confidence);

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
          title={`置信度 ${wall.confidence}/100 · 等级 ${wall.strength_tier}`}
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
          style={{ width: `${barWidth}%`, opacity: 0.35 + barWidth * 0.0065 }}
        />
      </div>

      {/* 标签 + 状态 + 共振徽章 */}
      <div className="flex items-center gap-1.5 flex-wrap mb-1">
        <span className="text-[10px] text-slate-500 shrink-0">📍 标签：</span>
        <span
          className={`px-1.5 py-0.5 rounded text-[11px] cursor-help ${labelStyle.bg} ${labelStyle.fg}`}
          title={labelStyle.hint}
        >
          {labelStyle.text}
        </span>
        <span
          className={`text-[11px] cursor-help ${change.color}`}
          title={change.hint}
        >
          · {change.text}
        </span>
        {wall.has_active_whale && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-amber-500/20 text-amber-300 cursor-help"
            title={`大单 lifecycle 关联：${wall.large_order_count} 笔（含 ≥1 笔 holding）`}
          >
            🐳 大单 ×{wall.large_order_count}
          </span>
        )}
        {wall.confluence_with_absorption && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/20 text-cyan-300 cursor-help"
            title="与 footprint absorption_zone 共振 (+25 confidence)"
          >
            ✓ 吸收
          </span>
        )}
        {wall.cvd_state && (
          <span
            className="text-[10px] text-slate-500"
            title="当前 CVD 1h 趋势"
          >
            CVD {wall.cvd_state}
          </span>
        )}
      </div>

      {/* 数额 + reason */}
      <div className="flex items-start gap-1.5 text-[11px]">
        <span className="text-slate-500 shrink-0">💰 数额：</span>
        <span className="font-mono text-slate-300">{formatCnUsd(wall.size_usd)}</span>
        {wall.eaten_usd > 0 && (
          <span className="text-amber-400" title="窗口内被市价单吃掉的金额">
            · 被吃 {formatCnUsd(wall.eaten_usd)}
          </span>
        )}
        {wall.cancelled_usd > 0 && (
          <span className="text-slate-400" title="窗口内被撤掉的金额">
            · 撤单 {formatCnUsd(wall.cancelled_usd)}
          </span>
        )}
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
          {open ? "▼" : "▶"} 展开完整明细（所有 wall · 信号原始流）
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
      <span className="text-slate-600">
        ⓘ 超出 ±12% 的远距压力请查看「关键位」Tab（清算地图、200日均线等）
      </span>
      {snap.notes.length > 0 && (
        <span className="text-slate-500">notes: {snap.notes.join(", ")}</span>
      )}
    </div>
  );
}
