"use client";

import { useMarketStore } from "@/stores/marketStore";
import { formatPrice } from "@/lib/format";
import type { KeyLevel, KeyLevelSignal, KeyLevelSnapshot } from "@/lib/types";

const STATE_LABELS: Record<string, { text: string; color: string }> = {
  idle: { text: "待观察", color: "text-slate-500" },
  approaching: { text: "正接近", color: "text-yellow-400" },
  testing: { text: "正测试", color: "text-amber-400" },
  swept: { text: "已扫取", color: "text-red-400" },
  bounced: { text: "已反弹", color: "text-green-400" },
  broken: { text: "已突破", color: "text-red-500" },
  flipped: { text: "已翻转", color: "text-purple-400" },
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
  const kl = data?.key_levels;

  if (!kl || kl.levels.length === 0) {
    return (
      <div className="flex items-center justify-center h-64 text-slate-500">
        等待关键位数据...
      </div>
    );
  }

  const price = data?.ticker?.last ?? 0;
  const activeSignals = kl.signals.filter(
    (s) => s.confidence === "A" || s.confidence === "B"
  );

  return (
    <div className="space-y-4 max-w-4xl">
      <PlainSummary kl={kl} price={price} coin={coin} />
      <Summary kl={kl} price={price} coin={coin} />
      {activeSignals.length > 0 && (
        <SignalCards signals={activeSignals} coin={coin} />
      )}
      <LevelTable levels={kl.levels} price={price} coin={coin} />
    </div>
  );
}

function PlainSummary({
  kl,
  price,
  coin,
}: {
  kl: KeyLevelSnapshot;
  price: number;
  coin: string;
}) {
  const aSignals = kl.signals.filter((s) => s.confidence === "A");
  const bSignals = kl.signals.filter((s) => s.confidence === "B");
  const activeLevels = kl.levels.filter((l) => l.state !== "idle");
  const nearSupport = activeLevels
    .filter((l) => l.side === "support")
    .sort((a, b) => b.price - a.price)[0];
  const nearResist = activeLevels
    .filter((l) => l.side === "resistance")
    .sort((a, b) => a.price - b.price)[0];
  const highCascade = kl.levels.filter((l) => l.cascade_risk > 0.7);

  let title = "";
  let desc = "";
  let borderColor = "border-slate-600";

  if (aSignals.length > 0) {
    const s = aSignals[0];
    const isLong = s.action.includes("long");
    title = isLong ? "发现做多机会" : "发现做空机会";
    borderColor = isLong ? "border-green-500/50" : "border-red-500/50";
    const slStr = s.stop_loss ? `，止损设在${formatPrice(s.stop_loss, coin)}` : "";
    desc = `${formatPrice(s.level_price, coin)}处的关键位出现了高置信信号。简单说：这个价位的流动性已经被"洗"过了，${isLong ? "空头弹药消耗殆尽，反弹概率较高" : "多头弹药消耗殆尽，回落概率较高"}。如果打算${isLong ? "做多" : "做空"}，可以关注这个位置${slStr}。`;
  } else if (bSignals.length > 0) {
    title = "初步信号，等待确认";
    borderColor = "border-yellow-500/40";
    desc = `${formatPrice(bSignals[0].level_price, coin)}附近出现了反弹/受阻迹象，但还没有流动性扫取确认（B级信号），建议再等等看，不急着入场。`;
  } else if (nearSupport || nearResist) {
    title = "正在接近关键位，留意机会";
    borderColor = "border-blue-500/40";
    const parts: string[] = [];
    if (nearResist) {
      const stateText = nearResist.state === "testing" ? "正在被测试" : "正在靠近";
      parts.push(`上方${formatPrice(nearResist.price, coin)}有一道阻力${stateText}`);
    }
    if (nearSupport) {
      const stateText = nearSupport.state === "testing" ? "正在被测试" : "正在靠近";
      parts.push(`下方${formatPrice(nearSupport.price, coin)}有一道支撑${stateText}`);
    }
    desc = `价格${parts.join("，")}。等价格到达后观察它的反应——是被弹回来还是直接穿过去——再做决定。`;
  } else {
    title = "暂无明确信号，耐心等待";
    desc = `当前价格${formatPrice(price, coin)}离所有关键支撑和阻力位都比较远，这种时候不适合盲目开单。等价格走到关键位附近再找机会，好的交易都是等出来的。`;
  }

  return (
    <div className={`bg-slate-800/60 border ${borderColor} rounded-lg p-4`}>
      <div className="flex items-center gap-2 mb-1.5">
        <span className="text-xs bg-slate-700/80 text-slate-300 px-2 py-0.5 rounded">
          白话解读
        </span>
        <span className="text-sm font-semibold text-slate-200">{title}</span>
      </div>
      <p className="text-sm text-slate-300 leading-relaxed">{desc}</p>
      {highCascade.length > 0 && (
        <p className="text-xs text-orange-400 mt-2">
          注意：有{highCascade.length}个关键位存在级联风险，一旦被突破可能引发连锁清算（瀑布行情），止损一定要设好。
        </p>
      )}
    </div>
  );
}

function Summary({
  kl,
  price,
  coin,
}: {
  kl: { levels: KeyLevel[]; signals: KeyLevelSignal[]; active_count: number };
  price: number;
  coin: string;
}) {
  const aSignals = kl.signals.filter((s) => s.confidence === "A");
  const bSignals = kl.signals.filter((s) => s.confidence === "B");
  const nearestSupport = kl.levels
    .filter((l) => l.side === "support" && l.price < price)
    .sort((a, b) => b.price - a.price)[0];
  const nearestResistance = kl.levels
    .filter((l) => l.side === "resistance" && l.price > price)
    .sort((a, b) => a.price - b.price)[0];

  const highCascade = kl.levels.filter((l) => l.cascade_risk > 0.7);

  let summaryText = "";
  if (aSignals.length > 0) {
    const s = aSignals[0];
    summaryText = `${s.confidence}级信号: ${s.reason}`;
  } else if (bSignals.length > 0) {
    const s = bSignals[0];
    summaryText = `${s.confidence}级信号: ${s.reason}`;
  } else if (kl.active_count > 0) {
    const states = kl.levels
      .filter((l) => l.state !== "idle")
      .map((l) => `${formatPrice(l.price, coin)} ${STATE_LABELS[l.state]?.text ?? l.state}`)
      .join("、");
    summaryText = `${kl.active_count}个关键位活跃: ${states}`;
  } else {
    summaryText = "所有关键位处于待观察状态，暂无明确交易信号";
  }

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg p-4">
      <div className="flex items-center justify-between mb-2">
        <h3 className="text-sm font-semibold text-slate-300">
          关键位状态机
        </h3>
        <span className="text-xs text-slate-500">
          追踪 {kl.levels.length} 个 · 活跃 {kl.active_count} 个
        </span>
      </div>
      <p className="text-sm text-slate-200 mb-3">{summaryText}</p>
      <div className="flex gap-4 text-xs text-slate-400">
        {nearestSupport && (
          <span>
            最近支撑:{" "}
            <span className="text-green-400">
              {formatPrice(nearestSupport.price, coin)}
            </span>{" "}
            ({nearestSupport.distance_pct.toFixed(1)}%)
          </span>
        )}
        {nearestResistance && (
          <span>
            最近阻力:{" "}
            <span className="text-red-400">
              {formatPrice(nearestResistance.price, coin)}
            </span>{" "}
            ({nearestResistance.distance_pct > 0 ? "+" : ""}
            {nearestResistance.distance_pct.toFixed(1)}%)
          </span>
        )}
        {highCascade.length > 0 && (
          <span className="text-orange-400">
            {highCascade.length}个高级联风险位
          </span>
        )}
      </div>
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
        const borderColor = sig.confidence === "A"
          ? isLong
            ? "border-green-500/60"
            : "border-red-500/60"
          : "border-yellow-500/40";
        const bgColor = sig.confidence === "A"
          ? "bg-slate-800/80"
          : "bg-slate-800/50";

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
                    ⚠ {w}
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

function LevelTable({
  levels,
  price,
  coin,
}: {
  levels: KeyLevel[];
  price: number;
  coin: string;
}) {
  const sorted = [...levels].sort((a, b) => b.price - a.price);

  return (
    <div className="bg-slate-800/30 border border-slate-700 rounded-lg overflow-hidden">
      <div className="px-4 py-2 border-b border-slate-700">
        <h3 className="text-sm font-semibold text-slate-300">
          关键位追踪表
        </h3>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-xs">
          <thead>
            <tr className="text-slate-500 border-b border-slate-700/50">
              <th className="px-3 py-2 text-left">价位</th>
              <th className="px-3 py-2 text-left">类型</th>
              <th className="px-3 py-2 text-left">状态</th>
              <th className="px-3 py-2 text-right">距当前</th>
              <th className="px-3 py-2 text-right">强度</th>
              <th className="px-3 py-2 text-right">测试</th>
              <th className="px-3 py-2 text-right">级联风险</th>
              <th className="px-3 py-2 text-left">来源</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((lv, i) => {
              const stateInfo = STATE_LABELS[lv.state] ?? {
                text: lv.state,
                color: "text-slate-400",
              };
              const isAbove = lv.price > price;
              const cascadeColor =
                lv.cascade_risk > 0.7
                  ? "text-red-400"
                  : lv.cascade_risk > 0.4
                    ? "text-orange-400"
                    : "text-slate-500";

              return (
                <tr
                  key={i}
                  className={`border-b border-slate-800/50 ${
                    lv.state !== "idle"
                      ? "bg-slate-700/20"
                      : ""
                  }`}
                >
                  <td className="px-3 py-2 font-mono text-slate-200">
                    {formatPrice(lv.price, coin)}
                  </td>
                  <td className="px-3 py-2">
                    <span
                      className={
                        lv.side === "support"
                          ? "text-green-400"
                          : "text-red-400"
                      }
                    >
                      {lv.side === "support" ? "支撑" : "阻力"}
                    </span>
                  </td>
                  <td className="px-3 py-2">
                    <span className={stateInfo.color}>
                      {stateInfo.text}
                    </span>
                  </td>
                  <td
                    className={`px-3 py-2 text-right font-mono ${
                      isAbove ? "text-red-400" : "text-green-400"
                    }`}
                  >
                    {lv.distance_pct > 0 ? "+" : ""}
                    {lv.distance_pct.toFixed(2)}%
                  </td>
                  <td className="px-3 py-2 text-right">
                    <StrengthDots strength={lv.strength} />
                  </td>
                  <td className="px-3 py-2 text-right text-slate-400">
                    {lv.test_count > 0 ? lv.test_count : "-"}
                  </td>
                  <td className={`px-3 py-2 text-right ${cascadeColor}`}>
                    {lv.cascade_risk > 0
                      ? `${(lv.cascade_risk * 100).toFixed(0)}%`
                      : "低"}
                  </td>
                  <td className="px-3 py-2 text-slate-500 max-w-[120px] truncate">
                    {lv.sources.slice(0, 2).join(", ")}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function StrengthDots({ strength }: { strength: number }) {
  return (
    <div className="flex gap-0.5 justify-end">
      {Array.from({ length: 5 }, (_, i) => (
        <div
          key={i}
          className={`w-1.5 h-1.5 rounded-full ${
            i < strength ? "bg-amber-400" : "bg-slate-700"
          }`}
        />
      ))}
    </div>
  );
}
