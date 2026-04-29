"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";
import { API_BASE } from "@/lib/constants";
import type {
  KeyLevelV2,
  KeyLevelSignal,
  KeyLevelSnapshotV2,
  BullBearLine,
  BreakoutZone,
  LiqMagnetLevel,
  DataFreshness,
} from "@/lib/types";
import Link from "next/link";
import { useState, useEffect } from "react";
import StrongLevelsCard from "./StrongLevelsCard";
import MarketStructureBadge from "./MarketStructureBadge";
import ExecutionPlanCard from "./ExecutionPlanCard";
import FinalDecisionCard from "./FinalDecisionCard";
import RegimeChip from "./RegimeChip";
import LifecyclePanel from "./LifecyclePanel";

const STATE_LABELS: Record<string, { text: string; color: string }> = {
  idle: { text: "待观察", color: "text-slate-500" },
  approaching: { text: "正接近", color: "text-yellow-400" },
  testing: { text: "正测试", color: "text-amber-400" },
  swept: { text: "已扫取", color: "text-red-400" },
  bounced: { text: "已反弹", color: "text-green-400" },
  broken: { text: "已突破", color: "text-red-500" },
  flipped: { text: "已翻转", color: "text-purple-400" },
};

const TIER_STYLES: Record<string, { bg: string; text: string }> = {
  S: { bg: "bg-amber-500/20", text: "text-amber-400" },
  A: { bg: "bg-red-500/15", text: "text-red-400" },
  B: { bg: "bg-blue-500/15", text: "text-blue-400" },
  C: { bg: "bg-slate-500/15", text: "text-slate-400" },
};

const ACTION_LABELS: Record<string, string> = {
  snipe_long: "狙击做多",
  snipe_short: "狙击做空",
  flip_long: "翻转做多",
  flip_short: "翻转做空",
  scalp_long: "⚡日内做多",
  scalp_short: "⚡日内做空",
  wait_sweep: "等待扫取",
  wait_approach: "等待接近",
};

export default function KeyLevelView() {
  const coin = useMarketStore((s) => s.coin);
  const data = useMarketStore((s) => s.data[s.coin]);
  const kl = data?.key_levels_v2;

  if (!kl || kl.levels.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-slate-500">
        等待关键位数据...
      </div>
    );
  }

  const price = kl.current_price || data?.ticker?.last || 0;
  const activeSignals = kl.signals.filter(
    (s) => s.confidence === "A" || s.confidence === "B"
  );

  const totalCount = kl.levels.length;

  return (
    <div className="space-y-4 max-w-4xl">
      <MarketStructureBadge />
      {/* M3 · F2 · 当前 regime chip（KL snapshot 内嵌的 regime 字段） */}
      <RegimeChip kl={kl} />
      {/* P1.4 · L7.5 双引擎融合最终决策（置顶，代表对外最终结论） */}
      <FinalDecisionCard coin={coin} />
      {/* D06 · 数学引擎 L4 执行计划（红绿灯 + 仓位 + 一句话） */}
      <ExecutionPlanCard coin={coin} />
      <StructureSummary kl={kl} coin={coin} />
      <BacktestStatsCard coin={coin} />
      <KLHistoryLinks coin={coin} />
      {kl.bull_bear_line && (
        <BullBearCard bb={kl.bull_bear_line} price={price} coin={coin} />
      )}
      {kl.breakout_zone && <BreakoutCard zone={kl.breakout_zone} coin={coin} />}
      <StrongLevelsCard levels={kl.levels} price={price} coin={coin} />
      <MagnetChannelCard magnets={kl.magnet_levels ?? []} price={price} coin={coin} />
      <PriceRuler levels={kl.levels} price={price} coin={coin} />
      {activeSignals.length > 0 ? (
        <SignalCards signals={activeSignals} coin={coin} />
      ) : (
        <div className="bg-slate-800/30 border border-slate-700/50 rounded-lg px-4 py-3 text-center text-xs text-slate-500">
          暂无高确定性交易信号，等待关键位状态变化...
        </div>
      )}
      <LevelList levels={kl.levels} price={price} coin={coin} totalCount={totalCount} />
      <div className="text-center pt-2 pb-4">
        <Link
          href={`/levels/${coin}`}
          target="_blank"
          className="inline-flex items-center gap-2 px-4 py-2 bg-slate-700/60 hover:bg-slate-600/60 border border-slate-600 rounded-lg text-sm text-slate-300 transition-colors"
        >
          查看完整分析（含全部 {totalCount} 个关键位）
          <span className="text-xs text-slate-500">↗</span>
        </Link>
      </div>
    </div>
  );
}

// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
// M1 新增：数据新鲜度状态徽标 + 清算磁铁通道卡片
// ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

function FreshnessIndicator({ df }: { df: DataFreshness | null }) {
  if (!df) return null;
  const score = df.overall_freshness_score;
  // 颜色档位：≥90 绿 / ≥70 黄 / <70 橙
  const color =
    score >= 90
      ? "text-emerald-400 bg-emerald-500/10"
      : score >= 70
        ? "text-amber-400 bg-amber-500/10"
        : "text-orange-400 bg-orange-500/10";
  const stale = df.stale_sources.length;
  const missing = df.missing_sources.length;
  const tooltip =
    `数据健康度 ${score.toFixed(0)}/100\n` +
    (stale ? `过期源：${df.stale_sources.join(", ")}\n` : "") +
    (missing ? `缺失源：${df.missing_sources.join(", ")}` : "");
  return (
    <span
      className={`px-1.5 py-0.5 rounded text-[10px] cursor-help ${color}`}
      title={tooltip.trim()}
    >
      📊 {score.toFixed(0)}/100
      {(stale > 0 || missing > 0) && (
        <span className="ml-1 opacity-70">
          ({stale + missing} 异常)
        </span>
      )}
    </span>
  );
}

function MagnetChannelCard({
  magnets,
  price,
  coin,
}: {
  magnets: LiqMagnetLevel[];
  price: number;
  coin: string;
}) {
  if (!magnets || magnets.length === 0) return null;

  const roleConfig: Record<
    string,
    { label: string; emoji: string; color: string; bg: string }
  > = {
    downside_pain_center: {
      label: "多头痛点",
      emoji: "💥",
      color: "text-rose-300",
      bg: "bg-rose-500/10 border-rose-500/30",
    },
    upside_short_squeeze: {
      label: "空头痛点",
      emoji: "🔥",
      color: "text-orange-300",
      bg: "bg-orange-500/10 border-orange-500/30",
    },
    leverage_magnet: {
      label: "杠杆磁铁",
      emoji: "🧲",
      color: "text-purple-300",
      bg: "bg-purple-500/10 border-purple-500/30",
    },
  };

  return (
    <div className="bg-slate-800/40 border border-slate-700/50 rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs bg-slate-700/80 text-slate-300 px-2 py-0.5 rounded">
          🧲 清算磁铁通道
        </span>
        <span className="text-[10px] text-slate-500">
          独立通道 · 不参与关键位评分
        </span>
      </div>
      <div className="text-[11px] text-slate-500 mb-2">
        全市场清算痛点位 + 杠杆密度高发带；价格易被磁吸至此，仅作参考。
      </div>
      <div className="space-y-1.5">
        {magnets.slice(0, 6).map((m, i) => {
          const cfg =
            roleConfig[m.magnet_role] ?? {
              label: m.magnet_role,
              emoji: "🧲",
              color: "text-slate-300",
              bg: "bg-slate-500/10 border-slate-500/30",
            };
          return (
            <div
              key={i}
              className={`flex items-center gap-2 px-2.5 py-1.5 rounded border text-xs ${cfg.bg}`}
            >
              <span className="text-base shrink-0">{cfg.emoji}</span>
              <span className={`px-1.5 py-0.5 rounded text-[10px] ${cfg.color} bg-black/20 shrink-0`}>
                {cfg.label}
              </span>
              <span className="font-mono text-slate-200 shrink-0">
                {formatPrice(m.price, coin)}
              </span>
              <span
                className={`text-[10px] font-mono shrink-0 ${
                  m.distance_pct >= 0 ? "text-red-400" : "text-green-400"
                }`}
              >
                {m.distance_pct >= 0 ? "+" : ""}
                {m.distance_pct.toFixed(2)}%
              </span>
              <span className="flex-1 text-[10px] text-slate-400 truncate">
                {m.note ?? ""}
              </span>
            </div>
          );
        })}
      </div>
      {price > 0 && magnets.length > 6 && (
        <div className="mt-2 text-[10px] text-slate-500 text-center">
          仅显示距当前价最近的 6 个，共 {magnets.length} 个磁铁
        </div>
      )}
    </div>
  );
}


function StructureSummary({
  kl,
  coin,
}: {
  kl: KeyLevelSnapshotV2;
  coin: string;
}) {
  const aSignals = kl.signals.filter((s) => s.confidence === "A");
  let borderColor = "border-slate-600";

  if (aSignals.length > 0) {
    const s = aSignals[0];
    const isLong = s.action.includes("long");
    borderColor = isLong ? "border-green-500/50" : "border-red-500/50";
  } else if (kl.active_count > 0) {
    borderColor = "border-yellow-500/40";
  }

  const hasTfData = kl.daily_strong_support || kl.daily_strong_resistance
    || kl.weekly_strong_support || kl.weekly_strong_resistance;

  return (
    <div className={`bg-slate-800/60 border ${borderColor} rounded-lg p-4`}>
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs bg-slate-700/80 text-slate-300 px-2 py-0.5 rounded">
          📍 最近强位
        </span>
        <div className="flex items-center gap-2 text-xs text-slate-500">
          <FreshnessIndicator df={kl.data_freshness ?? null} />
          <span>
            追踪 {kl.levels.length} 个关键位 · 活跃 {kl.active_count}
          </span>
        </div>
      </div>
      <div className="flex gap-6 text-xs text-slate-400">
        {kl.nearest_strong_support && (
          <span>
            最近强支撑:{" "}
            <span className="text-green-400 font-mono">
              {formatPrice(kl.nearest_strong_support, coin)}
            </span>
          </span>
        )}
        {kl.nearest_strong_resistance && (
          <span>
            最近强阻力:{" "}
            <span className="text-red-400 font-mono">
              {formatPrice(kl.nearest_strong_resistance, coin)}
            </span>
          </span>
        )}
      </div>
      {hasTfData && (
        <div className="mt-2.5 pt-2.5 border-t border-slate-700/50 grid grid-cols-2 gap-x-6 gap-y-1.5 text-xs">
          {(kl.daily_strong_support || kl.daily_strong_resistance) && (
            <>
              <div className="flex items-center gap-1.5">
                <span className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400 text-[10px] font-bold shrink-0">日线</span>
                <span className="text-slate-500">支撑</span>
                <span className="text-green-400 font-mono">{kl.daily_strong_support || "-"}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="px-1.5 py-0.5 rounded bg-blue-500/15 text-blue-400 text-[10px] font-bold shrink-0">日线</span>
                <span className="text-slate-500">阻力</span>
                <span className="text-red-400 font-mono">{kl.daily_strong_resistance || "-"}</span>
              </div>
            </>
          )}
          {(kl.weekly_strong_support || kl.weekly_strong_resistance) && (
            <>
              <div className="flex items-center gap-1.5">
                <span className="px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-400 text-[10px] font-bold shrink-0">周线</span>
                <span className="text-slate-500">支撑</span>
                <span className="text-green-400 font-mono">{kl.weekly_strong_support || "-"}</span>
              </div>
              <div className="flex items-center gap-1.5">
                <span className="px-1.5 py-0.5 rounded bg-purple-500/15 text-purple-400 text-[10px] font-bold shrink-0">周线</span>
                <span className="text-slate-500">阻力</span>
                <span className="text-red-400 font-mono">{kl.weekly_strong_resistance || "-"}</span>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function BullBearCard({
  bb,
  price,
  coin,
}: {
  bb: BullBearLine;
  price: number;
  coin: string;
}) {
  const isBull = bb.current_regime === "bull";
  const isBear = bb.current_regime === "bear";
  const bgColor = isBull
    ? "bg-green-950/30 border-green-700/40"
    : isBear
      ? "bg-red-950/30 border-red-700/40"
      : "bg-slate-800/50 border-slate-700";

  return (
    <div className={`border rounded-lg p-3 ${bgColor}`}>
      <div className="flex items-center gap-2 mb-2">
        <span
          className={`w-2.5 h-2.5 rounded-full ${
            isBull ? "bg-green-400" : isBear ? "bg-red-400" : "bg-yellow-400"
          }`}
        />
        <span className="text-sm font-semibold text-slate-200">
          多空分界线 —{" "}
          {isBull ? "当前偏多" : isBear ? "当前偏空" : "多空胶着"}
        </span>
      </div>
      <div className="flex flex-wrap gap-4 text-xs text-slate-400">
        {bb.sma200d && (
          <span>
            200日均线:{" "}
            <span className="text-slate-200 font-mono">
              {formatPrice(bb.sma200d, coin)}
            </span>
            <span
              className={`ml-1 ${price > bb.sma200d ? "text-green-400" : "text-red-400"}`}
            >
              {price > bb.sma200d ? "▲在上方" : "▼在下方"}
            </span>
          </span>
        )}
        {bb.bmsa_upper && bb.bmsa_lower && (
          <span>
            牛市支撑带:{" "}
            <span className="text-slate-200 font-mono">
              {formatPrice(bb.bmsa_lower, coin)}-{formatPrice(bb.bmsa_upper, coin)}
            </span>
          </span>
        )}
        {bb.ichimoku_cloud_top && bb.ichimoku_cloud_bottom && (
          <span>
            一目云层:{" "}
            <span className="text-slate-200 font-mono">
              {formatPrice(bb.ichimoku_cloud_bottom, coin)}-
              {formatPrice(bb.ichimoku_cloud_top, coin)}
            </span>
          </span>
        )}
      </div>
      {bb.regime_reason && (
        <p className="text-xs text-slate-500 mt-1.5">{bb.regime_reason}</p>
      )}
    </div>
  );
}

function BreakoutCard({
  zone,
  coin,
}: {
  zone: BreakoutZone;
  coin: string;
}) {
  if (!zone.bb_squeeze) return null;
  return (
    <div className="bg-purple-950/20 border border-purple-700/40 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-1">
        <span className="w-2 h-2 rounded-full bg-purple-400 animate-pulse" />
        <span className="text-sm font-semibold text-purple-300">
          突破蓄力中
        </span>
      </div>
      <p className="text-xs text-slate-300">{zone.note}</p>
    </div>
  );
}

function groupZones(sorted: KeyLevelV2[], price: number): { zone: boolean; items: KeyLevelV2[] }[] {
  const groups: { zone: boolean; items: KeyLevelV2[] }[] = [];
  let buf: KeyLevelV2[] = [];

  for (const lv of sorted) {
    if (buf.length === 0) {
      buf.push(lv);
      continue;
    }
    const last = buf[buf.length - 1];
    const gap = Math.abs(lv.price - last.price) / Math.max(price, 1);
    const bothStrong = ["S", "A"].includes(lv.strength_tier) && ["S", "A"].includes(last.strength_tier);
    if (gap < 0.005 && bothStrong) {
      buf.push(lv);
    } else {
      groups.push({ zone: buf.length >= 2 && ["S", "A"].includes(buf[0].strength_tier), items: [...buf] });
      buf = [lv];
    }
  }
  if (buf.length > 0) {
    groups.push({ zone: buf.length >= 2 && ["S", "A"].includes(buf[0].strength_tier), items: [...buf] });
  }
  return groups;
}

function PriceRuler({
  levels,
  price,
  coin,
}: {
  levels: KeyLevelV2[];
  price: number;
  coin: string;
}) {
  const tierRank = (t: string) => t === "S" ? 0 : t === "A" ? 1 : t === "B" ? 2 : 3;
  const resistances = levels
    .filter((l) => l.price > price && l.strength_tier !== "C")
    .sort((a, b) => tierRank(a.strength_tier) - tierRank(b.strength_tier) || a.price - b.price)
    .slice(0, 6)
    .sort((a, b) => a.price - b.price);
  const supports = levels
    .filter((l) => l.price < price && l.strength_tier !== "C")
    .sort((a, b) => tierRank(a.strength_tier) - tierRank(b.strength_tier) || b.price - a.price)
    .slice(0, 6)
    .sort((a, b) => b.price - a.price);

  const resDisplayOrder = [...resistances].reverse();
  const resGroups = groupZones(resDisplayOrder, price);
  const supGroups = groupZones(supports, price);

  return (
    <div className="bg-slate-800/30 border border-slate-700 rounded-lg p-4">
      <h3 className="text-sm font-semibold text-slate-300 mb-3">
        价格标尺
      </h3>
      <div className="flex flex-col items-stretch">
        {resGroups.map((g, gi) =>
          g.zone ? (
            <div key={`rz-${gi}`} className="border-l-2 border-red-500/40 bg-red-950/15 rounded-r pl-2 my-0.5">
              <div className="text-[9px] text-red-400/70 py-0.5">共振阻力带</div>
              {g.items.map((lv, i) => (
                <RulerRow key={`r-${gi}-${i}`} level={lv} coin={coin} side="resistance" />
              ))}
            </div>
          ) : (
            g.items.map((lv, i) => (
              <RulerRow key={`r-${gi}-${i}`} level={lv} coin={coin} side="resistance" />
            ))
          )
        )}

        <div className="flex items-center my-2 gap-2">
          <div className="flex-1 h-px bg-yellow-500/60" />
          <span className="text-sm font-bold text-yellow-400 font-mono whitespace-nowrap">
            当前 {formatPrice(price, coin)}
          </span>
          <div className="flex-1 h-px bg-yellow-500/60" />
        </div>

        {supGroups.map((g, gi) =>
          g.zone ? (
            <div key={`sz-${gi}`} className="border-l-2 border-green-500/40 bg-green-950/15 rounded-r pl-2 my-0.5">
              <div className="text-[9px] text-green-400/70 py-0.5">共振支撑带</div>
              {g.items.map((lv, i) => (
                <RulerRow key={`s-${gi}-${i}`} level={lv} coin={coin} side="support" />
              ))}
            </div>
          ) : (
            g.items.map((lv, i) => (
              <RulerRow key={`s-${gi}-${i}`} level={lv} coin={coin} side="support" />
            ))
          )
        )}
      </div>
    </div>
  );
}

function RulerRow({
  level,
  coin,
  side,
}: {
  level: KeyLevelV2;
  coin: string;
  side: "support" | "resistance";
}) {
  const tier = TIER_STYLES[level.strength_tier] || TIER_STYLES.C;
  const sideColor = side === "support" ? "text-green-400" : "text-red-400";
  const barColor = side === "support" ? "bg-green-500" : "bg-red-500";
  const barWidth = Math.min(100, level.confluence_score);
  const stateInfo = STATE_LABELS[level.state];

  return (
    <div className="flex items-center gap-2 py-1 group">
      <span className={`text-xs font-mono w-24 text-right ${sideColor}`}>
        {formatPrice(level.price, coin)}
      </span>
      <div className="flex-1 h-3 bg-slate-700/50 rounded-full overflow-hidden relative">
        <div
          className={`h-full ${barColor} rounded-full transition-all`}
          style={{ width: `${barWidth}%`, opacity: 0.3 + barWidth * 0.007 }}
        />
      </div>
      <span
        className={`text-[10px] px-1.5 py-0.5 rounded ${tier.bg} ${tier.text} font-bold w-6 text-center`}
      >
        {level.strength_tier}
      </span>
      <span className="text-[10px] text-slate-500 w-14 text-right">
        {level.distance_pct > 0 ? "+" : ""}
        {level.distance_pct.toFixed(1)}%
      </span>
      {stateInfo && level.state !== "idle" && (
        <span className={`text-[10px] ${stateInfo.color}`}>
          {stateInfo.text}
        </span>
      )}
    </div>
  );
}

// ─── 信号徽章中文 & 颜色 ────────────────────────────────────────────────
// 每个 signal_kind 对应一个"一眼看懂"的中文短标 + 色调。
// 优先使用 signal_kind；老数据缺失 signal_kind 时 fallback 到 action。
const SIGNAL_KIND_META: Record<
  string,
  { label: string; emoji: string; tone: "amber" | "emerald" | "sky" | "violet" | "slate" | "rose" }
> = {
  snipe_sweep: { label: "扫取反转", emoji: "🎯", tone: "emerald" },
  snipe_bounce: { label: "反弹确认", emoji: "💪", tone: "emerald" },
  breakout_observing: { label: "破位观望", emoji: "👀", tone: "slate" },
  breakout_retest: { label: "破位回踩", emoji: "🔁", tone: "sky" },
  breakout_continuation: { label: "破位延续", emoji: "🚀", tone: "amber" },
  fake_break_reversal: { label: "假突破反转", emoji: "⚠️", tone: "rose" },
  flip_retest: { label: "S/R 翻转", emoji: "🔄", tone: "amber" },
  scalp: { label: "日内极小止损", emoji: "⚡", tone: "violet" },
  wait_approach: { label: "前瞻观察", emoji: "🔭", tone: "slate" },
  wait_sweep: { label: "等扫流动性", emoji: "⏳", tone: "slate" },
};

const TONE_STYLES: Record<string, { chip: string; ring: string }> = {
  amber: {
    chip: "bg-amber-500/15 text-amber-300 border border-amber-500/40",
    ring: "border-amber-500/50",
  },
  emerald: {
    chip: "bg-emerald-500/15 text-emerald-300 border border-emerald-500/40",
    ring: "border-emerald-500/50",
  },
  sky: {
    chip: "bg-sky-500/15 text-sky-300 border border-sky-500/40",
    ring: "border-sky-500/50",
  },
  violet: {
    chip: "bg-violet-500/15 text-violet-300 border border-violet-500/40",
    ring: "border-violet-500/50",
  },
  slate: {
    chip: "bg-slate-600/25 text-slate-300 border border-slate-500/40",
    ring: "border-slate-600/50",
  },
  rose: {
    chip: "bg-rose-500/15 text-rose-300 border border-rose-500/40",
    ring: "border-rose-500/50",
  },
};

// confirmation key → 一句话中文短描述
const CONFIRMATION_LABELS: Record<string, string> = {
  closed_bar: "收盘确认",
  sweep_taken: "流动性已扫",
  volume_proactive: "放量主动",
  retest_in_progress: "回踩中",
  retest_done: "回踩完成",
  continuation: "延续确认",
  fake_break_reclaim: "假破回收",
  multi_fake_break: "多次守位",
  mtf_aligned: "1h 同向",
  cvd_aligned: "CVD 同向",
  flip_retest: "翻转回踩",
  pattern_pin_bar: "针形线",
  pattern_engulfing: "吞没形态",
  pattern_doji: "十字星",
  // M3 桥接：挂单压力多档信任 chip（互斥，最强优先）
  ob_strong_bid: "强买墙共振",
  ob_strong_ask: "强卖墙共振",
  ob_dual_source_bid: "💎 双源高可信支撑",
  ob_dual_source_ask: "💎 双源高可信阻力",
  ob_spot_only_bid: "💰 仅现货支撑",
  ob_spot_only_ask: "💰 仅现货阻力",
  ob_spot_confluence_bid: "💰 现货大单共振",
  ob_spot_confluence_ask: "💰 现货大单共振",
  ob_trusted_bid: "⚡ 较可信买墙",
  ob_trusted_ask: "⚡ 较可信卖墙",
  // W3-T1：Coinbase 现货共振叠加 chip（机构资金独立验证维度，与上述任一可同时出现）
  ob_coinbase_bid: "🏦 Coinbase 共振支撑",
  ob_coinbase_ask: "🏦 Coinbase 共振阻力",
  ob_wall_strengthened: "📈 该位墙增厚",
};

function labelConfirmation(key: string): string {
  if (CONFIRMATION_LABELS[key]) return CONFIRMATION_LABELS[key];
  if (key.startsWith("pattern_")) return key.replace("pattern_", "形态·");
  return key;
}

function ScoreBar({ score }: { score: number }) {
  const clamped = Math.max(0, Math.min(100, Math.round(score)));
  const color =
    clamped >= 80
      ? "bg-emerald-400"
      : clamped >= 60
        ? "bg-sky-400"
        : clamped >= 40
          ? "bg-amber-400"
          : "bg-rose-400";
  const label =
    clamped >= 80 ? "高" : clamped >= 60 ? "中高" : clamped >= 40 ? "中" : "低";
  return (
    <div className="flex items-center gap-2">
      <div
        className="relative h-1.5 w-24 rounded-full bg-slate-700/60 overflow-hidden"
        title={`置信度 ${clamped}/100（${label}）`}
      >
        <div
          className={`absolute left-0 top-0 h-full ${color} transition-all`}
          style={{ width: `${clamped}%` }}
        />
      </div>
      <span className="text-[10px] text-slate-400 tabular-nums">
        {clamped}
        <span className="text-slate-500">/100</span>
      </span>
    </div>
  );
}

function SignalCards({
  signals,
  coin,
}: {
  signals: KeyLevelSignal[];
  coin: string;
}) {
  return (
    <div className="grid gap-3 md:grid-cols-2">
      {signals.map((sig, i) => {
        const isLong = sig.action.includes("long");
        const kindKey = sig.signal_kind || sig.action;
        const meta = SIGNAL_KIND_META[kindKey] ?? {
          label: ACTION_LABELS[sig.action] ?? sig.action,
          emoji: "📍",
          tone: "slate" as const,
        };
        const tone = TONE_STYLES[meta.tone] ?? TONE_STYLES.slate;
        const dirColor = isLong ? "text-emerald-300" : "text-rose-300";

        return (
          <div
            key={i}
            className={`bg-slate-800/60 border ${tone.ring} rounded-lg p-3`}
          >
            {/* 第一行：类型徽章 + 方向 + 价格 */}
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <span
                className={`px-1.5 py-0.5 rounded text-[11px] font-semibold ${tone.chip}`}
                title={`信号类型：${meta.label}`}
              >
                {meta.emoji} {meta.label}
              </span>
              <span className={`text-sm font-medium ${dirColor}`}>
                {ACTION_LABELS[sig.action] ?? sig.action}
              </span>
              <span className="text-xs text-slate-500">
                @{formatPrice(sig.level_price, coin)}
              </span>
            </div>

            {/* 第二行：0-100 置信度分数条（取代 A/B/C 字母） */}
            <div className="mb-2">
              <ScoreBar score={sig.score ?? 0} />
            </div>

            <p className="text-xs text-slate-300 mb-2 leading-relaxed">
              {sig.reason}
            </p>

            {sig.entry_price != null && (
              <div className="flex gap-3 text-xs text-slate-400 flex-wrap">
                <span>
                  入场:{" "}
                  <span className="text-slate-200">
                    {formatPrice(sig.entry_price, coin)}
                  </span>
                </span>
                {sig.stop_loss != null && (
                  <span>
                    止损:{" "}
                    <span className="text-rose-300">
                      {formatPrice(sig.stop_loss, coin)}
                    </span>
                  </span>
                )}
                {sig.tp1 != null && (
                  <span>
                    TP1:{" "}
                    <span className="text-emerald-300">
                      {formatPrice(sig.tp1, coin)}
                    </span>
                  </span>
                )}
                {sig.rr_ratio != null && (
                  <span>
                    R:R={" "}
                    <span className="text-amber-300">
                      1:{sig.rr_ratio.toFixed(1)}
                    </span>
                  </span>
                )}
              </div>
            )}

            {/* 确认项 chip 链（✅ 一眼看懂通过了哪些确认） */}
            {sig.confirmations && sig.confirmations.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {sig.confirmations.map((c, j) => (
                  <span
                    key={j}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-300 border border-emerald-500/30"
                    title={`确认项：${c}`}
                  >
                    ✓ {labelConfirmation(c)}
                  </span>
                ))}
              </div>
            )}

            {/* 警告（risk） */}
            {sig.warnings.length > 0 && (
              <div className="mt-2 flex flex-wrap gap-1">
                {sig.warnings.map((w, j) => (
                  <span
                    key={j}
                    className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-300 border border-orange-500/30"
                  >
                    ⚠ {w}
                  </span>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function LevelList({
  levels,
  price,
  coin,
  totalCount,
}: {
  levels: KeyLevelV2[];
  price: number;
  coin: string;
  totalCount: number;
}) {
  const visible = levels.filter(
    (l) => l.strength_tier !== "C" || l.state !== "idle"
  );
  const resistances = visible
    .filter((l) => l.price > price)
    .sort((a, b) => a.price - b.price);
  const supports = visible
    .filter((l) => l.price <= price)
    .sort((a, b) => b.price - a.price);
  const hiddenCount = totalCount - visible.length;

  return (
    <div className="bg-slate-800/30 border border-slate-700 rounded-lg overflow-hidden">
      <div className="px-4 py-2 border-b border-slate-700 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-300">
          关键位追踪列表
        </h3>
        <span className="text-[10px] text-slate-500">
          从近到远 · 显示 {visible.length} 个{hiddenCount > 0 ? `（已隐藏 ${hiddenCount} 个弱级别）` : ""}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 border-b border-slate-700/50">
              <th className="px-3 py-2 text-left">价位</th>
              <th className="px-3 py-2 text-left">类型</th>
              <th className="px-3 py-2 text-center">强度</th>
              <th className="px-3 py-2 text-left">状态</th>
              <th className="px-3 py-2 text-right">距当前</th>
              <th className="px-3 py-2 text-right">共振分</th>
              <th className="px-3 py-2 text-right">级联</th>
              <th className="px-3 py-2 text-left">来源</th>
            </tr>
          </thead>
          <tbody>
            {resistances.length > 0 && (
              <tr>
                <td
                  colSpan={8}
                  className="px-3 py-1 text-[10px] text-red-400/60 bg-red-950/10"
                >
                  — 上方阻力 —
                </td>
              </tr>
            )}
            {resistances.map((lv, i) => (
              <LevelRow
                key={`r-${i}`}
                level={lv}
                coin={coin}
                price={price}
              />
            ))}
            <tr>
              <td
                colSpan={8}
                className="px-3 py-1.5 bg-yellow-500/5 border-y border-yellow-500/20"
              >
                <span className="text-xs text-yellow-400 font-mono font-bold">
                  当前价格 {formatPrice(price, coin)}
                </span>
              </td>
            </tr>
            {supports.map((lv, i) => (
              <LevelRow
                key={`s-${i}`}
                level={lv}
                coin={coin}
                price={price}
              />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function LevelRow({
  level,
  coin,
  price,
}: {
  level: KeyLevelV2;
  coin: string;
  price: number;
}) {
  const stateInfo = STATE_LABELS[level.state] ?? {
    text: level.state,
    color: "text-slate-400",
  };
  const tier = TIER_STYLES[level.strength_tier] || TIER_STYLES.C;
  const isAbove = level.price > price;
  const cascadeColor =
    level.cascade_risk > 0.7
      ? "text-red-400"
      : level.cascade_risk > 0.4
        ? "text-orange-400"
        : "text-slate-500";
  const sideColor = isAbove ? "text-red-400" : "text-green-400";

  const tierDepth =
    level.strength_tier === "S"
      ? "bg-slate-700/30"
      : level.strength_tier === "A"
        ? "bg-slate-700/20"
        : "";

  const hasLifecycle =
    Array.isArray(level.lifecycle_events) && level.lifecycle_events.length > 0;
  const rowBg = level.state !== "idle" ? "bg-slate-700/20" : tierDepth;

  return (
    <>
      <tr className={`${rowBg} ${hasLifecycle ? "" : "border-b border-slate-800/50"}`}>
        <td className="px-3 py-2 font-mono text-slate-200">
          {formatPrice(level.price, coin)}
        </td>
        <td className="px-3 py-2">
          <span className={sideColor}>
            {isAbove ? "阻力" : "支撑"}
          </span>
        </td>
        <td className="px-3 py-2 text-center">
          <span
            className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-bold ${tier.bg} ${tier.text}`}
          >
            {level.strength_tier}
          </span>
        </td>
        <td className="px-3 py-2">
          <span className={stateInfo.color}>{stateInfo.text}</span>
        </td>
        <td
          className={`px-3 py-2 text-right font-mono ${
            isAbove ? "text-red-400" : "text-green-400"
          }`}
        >
          {level.distance_pct > 0 ? "+" : ""}
          {level.distance_pct.toFixed(2)}%
        </td>
        <td className="px-3 py-2 text-right text-slate-300">
          {level.confluence_score.toFixed(0)}
        </td>
        <td className={`px-3 py-2 text-right ${cascadeColor}`}>
          {level.cascade_risk > 0
            ? `${(level.cascade_risk * 100).toFixed(0)}%`
            : "低"}
        </td>
        <td className="px-3 py-2 text-slate-500 max-w-[160px]">
          <span className="truncate block" title={level.sources.join(", ")}>
            {level.sources.slice(0, 3).join(", ")}
          </span>
        </td>
      </tr>
      {hasLifecycle && (
        <tr className={`${rowBg} border-b border-slate-800/50`}>
          <td colSpan={8} className="px-3 pb-2">
            <LifecyclePanel
              events={level.lifecycle_events}
              level_id={level.level_id}
            />
          </td>
        </tr>
      )}
    </>
  );
}


function KLHistoryLinks({ coin }: { coin: string }) {
  const [list, setList] = useState<{ ts: number; levels_count: number; price: number }[]>([]);

  useEffect(() => {
    fetch(`${API_BASE}/api/key-levels/history/${coin}?limit=5`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => {
        const snaps = d?.snapshots;
        if (Array.isArray(snaps)) {
          setList(
            snaps.map((s: any) => ({
              ts: s.ts,
              levels_count: Array.isArray(s.levels) ? s.levels.length : 0,
              price: s.current_price || 0,
            }))
          );
        }
      })
      .catch(() => {});
  }, [coin]);

  if (list.length === 0) return null;

  return (
    <div className="flex items-center gap-3 text-xs text-slate-500 flex-wrap">
      <span className="text-slate-400">历史快照:</span>
      {list.map((item) => (
        <Link
          key={item.ts}
          href={`/levels/${coin}/${item.ts}`}
          target="_blank"
          className="hover:text-blue-400 transition-colors"
        >
          {new Date(item.ts * 1000).toLocaleString("zh-CN", {
            month: "2-digit",
            day: "2-digit",
            hour: "2-digit",
            minute: "2-digit",
          })}{" "}
          ({item.levels_count}位)
        </Link>
      ))}
    </div>
  );
}

interface BtStats {
  total_signals: number;
  triggered: number;
  tp1_hit: number;
  sl_hit: number;
  pending: number;
  win_rate: number;
  avg_rr: number;
  by_source: Record<string, { total: number; tp1: number; sl: number }>;
}

function BacktestStatsCard({ coin }: { coin: string }) {
  const [stats, setStats] = useState<BtStats | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    const load = () => {
      fetch(`${API_BASE}/api/backtest/stats/${coin}`)
        .then((r) => (r.ok ? r.json() : null))
        .then((d) => {
          if (d) setStats(d);
          setLoaded(true);
        })
        .catch(() => setLoaded(true));
    };
    load();
    const t = setInterval(load, 120000);
    return () => clearInterval(t);
  }, [coin]);

  if (!loaded) return null;

  if (!stats || stats.total_signals === 0) {
    return (
      <div className="bg-slate-800/40 border border-slate-700/50 rounded-lg p-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-300">📊 信号回测统计</h3>
        <span className="text-[10px] text-slate-500">数据积累中，AI自动分析后将展示统计结果...</span>
      </div>
    );
  }

  const resolved = stats.tp1_hit + stats.sl_hit;

  return (
    <div className="bg-slate-800/40 border border-slate-700/50 rounded-lg p-4">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-slate-300">📊 信号回测统计</h3>
        <span className="text-[10px] text-slate-600">基于AI历史报告</span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
        <StatCell label="总信号" value={String(stats.total_signals)} />
        <StatCell label="已触发" value={String(stats.triggered)} />
        <StatCell
          label="胜率"
          value={resolved > 0 ? `${stats.win_rate}%` : "-"}
          color={stats.win_rate >= 50 ? "text-green-400" : stats.win_rate > 0 ? "text-red-400" : "text-slate-400"}
        />
        <StatCell label="平均R:R" value={stats.avg_rr > 0 ? `1:${stats.avg_rr}` : "-"} color="text-amber-400" />
      </div>
      {resolved > 0 && (
        <div className="mt-3 flex items-center gap-2">
          <div className="flex-1 h-2 bg-slate-700 rounded-full overflow-hidden">
            <div
              className="h-full bg-green-500 rounded-full"
              style={{ width: `${stats.win_rate}%` }}
            />
          </div>
          <span className="text-[10px] text-slate-500 shrink-0">
            {stats.tp1_hit}胜 / {stats.sl_hit}负 / {stats.pending}待定
          </span>
        </div>
      )}
      {stats.by_source && Object.keys(stats.by_source).length > 1 && (
        <div className="mt-2 flex gap-3 text-[10px] text-slate-500">
          {Object.entries(stats.by_source).map(([src, s]) => (
            <span key={src}>
              {src === "ai_inferred" ? "⚡AI" : "引擎"}: {s.total}个
              {s.tp1 + s.sl > 0 && ` (胜率${s.tp1 + s.sl > 0 ? Math.round(s.tp1 / (s.tp1 + s.sl) * 100) : 0}%)`}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function StatCell({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div className="text-center">
      <div className={`text-lg font-bold ${color || "text-white"}`}>{value}</div>
      <div className="text-[10px] text-slate-500">{label}</div>
    </div>
  );
}
