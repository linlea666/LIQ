"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";
import type {
  KeyLevelV2,
  KeyLevelSignal,
  KeyLevelSnapshotV2,
  BullBearLine,
  BreakoutZone,
} from "@/lib/types";
import Link from "next/link";

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
      <StructureSummary kl={kl} price={price} coin={coin} />
      {kl.bull_bear_line && (
        <BullBearCard bb={kl.bull_bear_line} price={price} coin={coin} />
      )}
      {kl.breakout_zone && <BreakoutCard zone={kl.breakout_zone} coin={coin} />}
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

function StructureSummary({
  kl,
  price,
  coin,
}: {
  kl: KeyLevelSnapshotV2;
  price: number;
  coin: string;
}) {
  const aSignals = kl.signals.filter((s) => s.confidence === "A");
  let title = kl.structure_summary || "分析中...";
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
        <div className="flex items-center gap-2">
          <span className="text-xs bg-slate-700/80 text-slate-300 px-2 py-0.5 rounded">
            市场结构
          </span>
          <span className="text-sm font-semibold text-slate-200">{title}</span>
        </div>
        <span className="text-xs text-slate-500">
          追踪 {kl.levels.length} 个关键位 · 活跃 {kl.active_count}
        </span>
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

  return (
    <div className="bg-slate-800/30 border border-slate-700 rounded-lg p-4">
      <h3 className="text-sm font-semibold text-slate-300 mb-3">
        价格标尺
      </h3>
      <div className="flex flex-col items-stretch">
        {resistances.reverse().map((lv, i) => (
          <RulerRow key={`r-${i}`} level={lv} coin={coin} side="resistance" />
        ))}

        <div className="flex items-center my-2 gap-2">
          <div className="flex-1 h-px bg-yellow-500/60" />
          <span className="text-sm font-bold text-yellow-400 font-mono whitespace-nowrap">
            当前 {formatPrice(price, coin)}
          </span>
          <div className="flex-1 h-px bg-yellow-500/60" />
        </div>

        {supports.map((lv, i) => (
          <RulerRow key={`s-${i}`} level={lv} coin={coin} side="support" />
        ))}
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
        const isLong =
          sig.action === "snipe_long" || sig.action === "flip_long";
        const borderColor =
          sig.confidence === "A"
            ? isLong
              ? "border-green-500/60"
              : "border-red-500/60"
            : "border-yellow-500/40";
        const bgColor =
          sig.confidence === "A" ? "bg-slate-800/80" : "bg-slate-800/50";

        return (
          <div
            key={i}
            className={`${bgColor} border ${borderColor} rounded-lg p-3`}
          >
            <div className="flex items-center gap-2 mb-2">
              <span
                className={`px-1.5 py-0.5 rounded text-xs font-bold ${
                  sig.confidence === "A"
                    ? "bg-amber-500/20 text-amber-400"
                    : "bg-blue-500/20 text-blue-400"
                }`}
              >
                {sig.confidence}级
              </span>
              <span
                className={`text-sm font-medium ${
                  isLong ? "text-green-400" : "text-red-400"
                }`}
              >
                {ACTION_LABELS[sig.action] ?? sig.action}
              </span>
              <span className="text-xs text-slate-500">
                @{formatPrice(sig.level_price, coin)}
              </span>
            </div>
            <p className="text-xs text-slate-300 mb-2">{sig.reason}</p>
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
                    <span className="text-red-400">
                      {formatPrice(sig.stop_loss, coin)}
                    </span>
                  </span>
                )}
                {sig.tp1 != null && (
                  <span>
                    TP1:{" "}
                    <span className="text-green-400">
                      {formatPrice(sig.tp1, coin)}
                    </span>
                  </span>
                )}
                {sig.rr_ratio != null && (
                  <span>
                    R:R={" "}
                    <span className="text-amber-400">
                      1:{sig.rr_ratio.toFixed(1)}
                    </span>
                  </span>
                )}
              </div>
            )}
            {sig.warnings.length > 0 && (
              <div className="mt-2 space-y-1">
                {sig.warnings.map((w, j) => (
                  <p key={j} className="text-xs text-orange-400">
                    {w}
                  </p>
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

  return (
    <tr
      className={`border-b border-slate-800/50 ${
        level.state !== "idle" ? "bg-slate-700/20" : tierDepth
      }`}
    >
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
  );
}
