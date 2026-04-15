"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type {
  KeyLevelSnapshotV2,
  KeyLevelV2,
  KeyLevelSignal,
  BullBearLine,
  BreakoutZone,
  FibSnapshot,
} from "@/lib/types";

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

function formatFullTime(ts: number): string {
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

export default function KeyLevelDetailPage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";

  const [data, setData] = useState<KeyLevelSnapshotV2 | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const load = () => {
    setRefreshing(true);
    fetch(`${API_BASE}/api/key-levels/${coin}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then((d) => {
        setData(d);
        setError("");
      })
      .catch((e) => setError(`加载失败: ${e.message}`))
      .finally(() => setRefreshing(false));
  };

  useEffect(() => {
    load();
    const interval = setInterval(load, 30000);
    return () => clearInterval(interval);
  }, [coin]);

  if (error && !data) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-400 text-lg mb-4">{error}</div>
          <a href="/" className="text-blue-400 hover:text-blue-300 text-sm">
            ← 返回大屏
          </a>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="animate-spin w-10 h-10 border-2 border-blue-500 border-t-transparent rounded-full" />
      </div>
    );
  }

  const price = data.current_price;
  const resistances = data.levels
    .filter((l) => l.price > price)
    .sort((a, b) => a.price - b.price);
  const supports = data.levels
    .filter((l) => l.price <= price)
    .sort((a, b) => b.price - a.price);
  const activeSignals = data.signals.filter(
    (s) => s.confidence === "A" || s.confidence === "B"
  );

  return (
    <div className="levels-detail-page min-h-screen bg-slate-950 text-slate-300">
      {/* Header */}
      <header className="border-b border-slate-700 bg-slate-900/80 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-6 py-4 flex items-center justify-between">
          <div className="flex items-center gap-4">
            <a
              href="/"
              className="text-blue-400 hover:text-blue-300 text-sm shrink-0"
            >
              ← 返回大屏
            </a>
            <div>
              <h1 className="text-lg font-bold text-white">
                {coin} 关键位全景分析
              </h1>
              <div className="text-xs text-slate-500 mt-0.5">
                {formatFullTime(data.ts)} | 当前价格:{" "}
                <span className="text-white">
                  {formatPrice(price, coin)}
                </span>{" "}
                | ATR: {formatPrice(data.atr, coin)}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-3">
            <span className="text-xs text-slate-500">
              {data.levels.length} 个关键位 · {data.active_count} 活跃
            </span>
            <button
              onClick={load}
              disabled={refreshing}
              className="px-3 py-1 text-xs border border-slate-600 rounded hover:border-slate-400 hover:text-white transition disabled:opacity-50"
            >
              {refreshing ? "刷新中..." : "刷新数据"}
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-5xl mx-auto px-6 py-6 space-y-6">
        {/* Overview */}
        <Card title="总览">
          <div className="text-base text-white font-medium mb-3">
            {data.structure_summary || "数据分析中..."}
          </div>
          <div className="flex gap-6 text-sm text-slate-400">
            {data.nearest_strong_support && (
              <span>
                最近强支撑:{" "}
                <span className="text-green-400 font-mono font-medium">
                  {formatPrice(data.nearest_strong_support, coin)}
                </span>
              </span>
            )}
            {data.nearest_strong_resistance && (
              <span>
                最近强阻力:{" "}
                <span className="text-red-400 font-mono font-medium">
                  {formatPrice(data.nearest_strong_resistance, coin)}
                </span>
              </span>
            )}
          </div>
        </Card>

        {/* Bull/Bear Line */}
        {data.bull_bear_line && (
          <BullBearDetail bb={data.bull_bear_line} price={price} coin={coin} />
        )}

        {/* Active Signals */}
        {activeSignals.length > 0 && (
          <Card title="交易信号">
            <div className="space-y-4">
              {activeSignals.map((sig, i) => (
                <SignalDetail key={i} signal={sig} coin={coin} />
              ))}
            </div>
          </Card>
        )}

        {/* Strong Resistances */}
        {resistances.length > 0 && (
          <Card title={`上方阻力位 (${resistances.length})`}>
            <LevelTable levels={resistances} coin={coin} price={price} />
          </Card>
        )}

        {/* Strong Supports */}
        {supports.length > 0 && (
          <Card title={`下方支撑位 (${supports.length})`}>
            <LevelTable levels={supports} coin={coin} price={price} />
          </Card>
        )}

        {/* Fibonacci */}
        {data.fib_snapshot && (
          <FibDetail fib={data.fib_snapshot} coin={coin} price={price} />
        )}

        {/* Breakout Zone */}
        {data.breakout_zone && data.breakout_zone.bb_squeeze && (
          <BreakoutDetail zone={data.breakout_zone} coin={coin} />
        )}

        {/* Cascade Risk */}
        <CascadeRiskSection levels={data.levels} coin={coin} />

        {/* Footer */}
        <div className="text-center text-xs text-slate-600 py-6 border-t border-slate-800">
          LIQ 防猎杀数据大屏 · 关键位全景分析 · {formatFullTime(data.ts)}
        </div>
      </main>
    </div>
  );
}

function Card({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl overflow-hidden">
      <div className="px-5 py-3 border-b border-slate-700/50 bg-slate-800/30">
        <h2 className="text-sm font-semibold text-white">{title}</h2>
      </div>
      <div className="px-5 py-4 text-sm text-slate-400 leading-relaxed">
        {children}
      </div>
    </div>
  );
}

function BullBearDetail({
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
    ? "bg-green-950/20 border-green-700/40"
    : isBear
      ? "bg-red-950/20 border-red-700/40"
      : "bg-slate-900/80 border-slate-700/50";

  return (
    <div className={`border rounded-xl overflow-hidden ${bgColor}`}>
      <div className="px-5 py-3 border-b border-slate-700/50 bg-slate-800/30 flex items-center gap-2">
        <span
          className={`w-3 h-3 rounded-full ${
            isBull ? "bg-green-400" : isBear ? "bg-red-400" : "bg-yellow-400"
          }`}
        />
        <h2 className="text-sm font-semibold text-white">
          多空分界线 —{" "}
          {isBull ? "当前偏多" : isBear ? "当前偏空" : "多空胶着"}
        </h2>
      </div>
      <div className="px-5 py-4 space-y-3">
        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
          {bb.sma200d && (
            <div className="bg-slate-800/50 rounded-lg p-3">
              <div className="text-xs text-slate-500 mb-1">200日移动均线</div>
              <div className="text-lg font-mono text-white">
                {formatPrice(bb.sma200d, coin)}
              </div>
              <div
                className={`text-xs mt-1 ${
                  price > bb.sma200d ? "text-green-400" : "text-red-400"
                }`}
              >
                价格在{price > bb.sma200d ? "上方" : "下方"}{" "}
                {(((price - bb.sma200d) / bb.sma200d) * 100).toFixed(1)}%
              </div>
            </div>
          )}
          {bb.bmsa_upper && bb.bmsa_lower && (
            <div className="bg-slate-800/50 rounded-lg p-3">
              <div className="text-xs text-slate-500 mb-1">
                牛市支撑带 (20W SMA + 21W EMA)
              </div>
              <div className="text-lg font-mono text-white">
                {formatPrice(bb.bmsa_lower, coin)} -{" "}
                {formatPrice(bb.bmsa_upper, coin)}
              </div>
              <div
                className={`text-xs mt-1 ${
                  price > bb.bmsa_upper ? "text-green-400" : price < bb.bmsa_lower ? "text-red-400" : "text-yellow-400"
                }`}
              >
                {price > bb.bmsa_upper
                  ? "价格在带上方(牛势)"
                  : price < bb.bmsa_lower
                    ? "价格在带下方(危险)"
                    : "价格在带内(观望)"}
              </div>
            </div>
          )}
          {bb.ichimoku_cloud_top && bb.ichimoku_cloud_bottom && (
            <div className="bg-slate-800/50 rounded-lg p-3">
              <div className="text-xs text-slate-500 mb-1">一目均衡云层</div>
              <div className="text-lg font-mono text-white">
                {formatPrice(bb.ichimoku_cloud_bottom, coin)} -{" "}
                {formatPrice(bb.ichimoku_cloud_top, coin)}
              </div>
              <div
                className={`text-xs mt-1 ${
                  price > bb.ichimoku_cloud_top
                    ? "text-green-400"
                    : price < bb.ichimoku_cloud_bottom
                      ? "text-red-400"
                      : "text-yellow-400"
                }`}
              >
                {price > bb.ichimoku_cloud_top
                  ? "价格在云层上方(多头)"
                  : price < bb.ichimoku_cloud_bottom
                    ? "价格在云层下方(空头)"
                    : "价格在云层中(胶着)"}
              </div>
            </div>
          )}
        </div>
        {bb.regime_reason && (
          <p className="text-xs text-slate-500">{bb.regime_reason}</p>
        )}
      </div>
    </div>
  );
}

function SignalDetail({
  signal,
  coin,
}: {
  signal: KeyLevelSignal;
  coin: string;
}) {
  const isLong =
    signal.action === "snipe_long" || signal.action === "flip_long";
  const borderColor =
    signal.confidence === "A"
      ? isLong
        ? "border-green-500/40"
        : "border-red-500/40"
      : "border-yellow-500/30";

  return (
    <div className={`border ${borderColor} rounded-lg p-4`}>
      <div className="flex items-center gap-2 mb-2">
        <span
          className={`px-2 py-0.5 rounded text-xs font-bold ${
            signal.confidence === "A"
              ? "bg-amber-500/20 text-amber-400"
              : "bg-blue-500/20 text-blue-400"
          }`}
        >
          {signal.confidence}级
        </span>
        <span
          className={`text-base font-semibold ${
            isLong ? "text-green-400" : "text-red-400"
          }`}
        >
          {ACTION_LABELS[signal.action] ?? signal.action}
        </span>
        <span className="text-sm text-slate-500">
          @{formatPrice(signal.level_price, coin)}
        </span>
      </div>
      <p className="text-sm text-slate-300 mb-3">{signal.reason}</p>

      {signal.entry_price != null && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
          <div className="bg-slate-800/50 rounded p-2">
            <div className="text-xs text-slate-500">入场价</div>
            <div className="text-white font-mono">
              {formatPrice(signal.entry_price, coin)}
            </div>
          </div>
          {signal.stop_loss != null && (
            <div className="bg-slate-800/50 rounded p-2">
              <div className="text-xs text-slate-500">止损</div>
              <div className="text-red-400 font-mono">
                {formatPrice(signal.stop_loss, coin)}
              </div>
            </div>
          )}
          {signal.tp1 != null && (
            <div className="bg-slate-800/50 rounded p-2">
              <div className="text-xs text-slate-500">目标1</div>
              <div className="text-green-400 font-mono">
                {formatPrice(signal.tp1, coin)}
              </div>
            </div>
          )}
          {signal.rr_ratio != null && (
            <div className="bg-slate-800/50 rounded p-2">
              <div className="text-xs text-slate-500">风报比</div>
              <div className="text-amber-400 font-mono">
                1:{signal.rr_ratio.toFixed(1)}
              </div>
            </div>
          )}
        </div>
      )}

      {signal.warnings.length > 0 && (
        <div className="mt-3 space-y-1">
          {signal.warnings.map((w, i) => (
            <p key={i} className="text-xs text-orange-400">
              {w}
            </p>
          ))}
        </div>
      )}
    </div>
  );
}

function LevelTable({
  levels,
  coin,
  price,
}: {
  levels: KeyLevelV2[];
  coin: string;
  price: number;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-700 text-slate-500 text-xs">
            <th className="text-left py-2 pr-3">价位</th>
            <th className="text-center py-2 pr-3">强度</th>
            <th className="text-left py-2 pr-3">状态</th>
            <th className="text-right py-2 pr-3">距当前</th>
            <th className="text-right py-2 pr-3">共振分</th>
            <th className="text-right py-2 pr-3">级联风险</th>
            <th className="text-left py-2 pr-3">时间框架</th>
            <th className="text-left py-2">来源拆解</th>
          </tr>
        </thead>
        <tbody>
          {levels.map((lv, i) => {
            const stateInfo = STATE_LABELS[lv.state] || {
              text: lv.state,
              color: "text-slate-400",
            };
            const tier = TIER_STYLES[lv.strength_tier] || TIER_STYLES.C;
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
                  lv.state !== "idle" ? "bg-slate-800/20" : ""
                }`}
              >
                <td className="py-2.5 pr-3 font-mono text-white">
                  {formatPrice(lv.price, coin)}
                </td>
                <td className="py-2.5 pr-3 text-center">
                  <span
                    className={`inline-block px-2 py-0.5 rounded text-xs font-bold ${tier.bg} ${tier.text}`}
                  >
                    {lv.strength_tier}
                  </span>
                </td>
                <td className="py-2.5 pr-3">
                  <span className={stateInfo.color}>{stateInfo.text}</span>
                </td>
                <td
                  className={`py-2.5 pr-3 text-right font-mono ${
                    lv.price > price ? "text-red-400" : "text-green-400"
                  }`}
                >
                  {lv.distance_pct > 0 ? "+" : ""}
                  {lv.distance_pct.toFixed(2)}%
                </td>
                <td className="py-2.5 pr-3 text-right text-slate-300">
                  {lv.confluence_score.toFixed(0)}
                </td>
                <td className={`py-2.5 pr-3 text-right ${cascadeColor}`}>
                  {lv.cascade_risk > 0
                    ? `${(lv.cascade_risk * 100).toFixed(0)}%`
                    : "低"}
                </td>
                <td className="py-2.5 pr-3 text-slate-500 text-xs">
                  {lv.timeframe || "-"}
                </td>
                <td className="py-2.5 text-slate-500 text-xs">
                  <span className="block truncate max-w-[300px]" title={lv.note}>
                    {lv.note || lv.sources.join(", ")}
                  </span>
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function FibDetail({
  fib,
  coin,
  price,
}: {
  fib: FibSnapshot;
  coin: string;
  price: number;
}) {
  return (
    <Card title="Fibonacci 参考位">
      <div className="mb-3 text-sm">
        <span className="text-slate-400">大波段: </span>
        <span className="text-white font-mono">
          {formatPrice(fib.swing_low, coin)}
        </span>
        <span className="text-slate-500 mx-2">→</span>
        <span className="text-white font-mono">
          {formatPrice(fib.swing_high, coin)}
        </span>
        <span className="text-slate-500 ml-2">
          ({fib.direction === "up" ? "上升波段" : "下降波段"})
        </span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
        {fib.levels
          .filter((fl) => [0.236, 0.382, 0.5, 0.618, 0.786, 1.0, 1.272, 1.618].includes(fl.ratio))
          .map((fl, i) => {
            const isKey = [0.382, 0.5, 0.618].includes(fl.ratio);
            const nearPrice = Math.abs(fl.price - price) / price < 0.02;
            return (
              <div
                key={i}
                className={`rounded p-2 ${
                  nearPrice
                    ? "bg-yellow-500/10 border border-yellow-500/30"
                    : isKey
                      ? "bg-slate-800/60"
                      : "bg-slate-800/30"
                }`}
              >
                <div className="text-xs text-slate-500">{fl.label}</div>
                <div
                  className={`font-mono ${
                    isKey ? "text-white text-sm" : "text-slate-300 text-xs"
                  }`}
                >
                  {formatPrice(fl.price, coin)}
                </div>
                {nearPrice && (
                  <span className="text-[10px] text-yellow-400">← 接近当前价</span>
                )}
              </div>
            );
          })}
      </div>
    </Card>
  );
}

function BreakoutDetail({
  zone,
  coin,
}: {
  zone: BreakoutZone;
  coin: string;
}) {
  return (
    <Card title="突破蓄力区">
      <div className="flex items-center gap-2 mb-3">
        <span className="w-2.5 h-2.5 rounded-full bg-purple-400 animate-pulse" />
        <span className="text-purple-300 font-medium">{zone.note}</span>
      </div>
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 text-sm">
        {zone.bb_upper && (
          <div className="bg-slate-800/50 rounded p-2">
            <div className="text-xs text-slate-500">BB 上轨</div>
            <div className="text-white font-mono">
              {formatPrice(zone.bb_upper, coin)}
            </div>
          </div>
        )}
        {zone.bb_lower && (
          <div className="bg-slate-800/50 rounded p-2">
            <div className="text-xs text-slate-500">BB 下轨</div>
            <div className="text-white font-mono">
              {formatPrice(zone.bb_lower, coin)}
            </div>
          </div>
        )}
        {zone.keltner_upper && (
          <div className="bg-slate-800/50 rounded p-2">
            <div className="text-xs text-slate-500">KC 上轨</div>
            <div className="text-white font-mono">
              {formatPrice(zone.keltner_upper, coin)}
            </div>
          </div>
        )}
        {zone.keltner_lower && (
          <div className="bg-slate-800/50 rounded p-2">
            <div className="text-xs text-slate-500">KC 下轨</div>
            <div className="text-white font-mono">
              {formatPrice(zone.keltner_lower, coin)}
            </div>
          </div>
        )}
      </div>
    </Card>
  );
}

function CascadeRiskSection({
  levels,
  coin,
}: {
  levels: KeyLevelV2[];
  coin: string;
}) {
  const risky = levels
    .filter((l) => l.cascade_risk > 0.3)
    .sort((a, b) => b.cascade_risk - a.cascade_risk);

  if (risky.length === 0) return null;

  return (
    <Card title="级联风险地图">
      <p className="text-xs text-slate-500 mb-3">
        以下关键位若被突破，可能触发连锁清算瀑布（级联风险
        &gt; 30%）
      </p>
      <div className="space-y-2">
        {risky.map((lv, i) => {
          const riskPct = lv.cascade_risk * 100;
          const color =
            riskPct > 70
              ? "bg-red-500"
              : riskPct > 50
                ? "bg-orange-500"
                : "bg-yellow-500";
          return (
            <div key={i} className="flex items-center gap-3">
              <span className="text-sm font-mono text-slate-200 w-24 text-right shrink-0">
                {formatPrice(lv.price, coin)}
              </span>
              <span
                className={`text-xs w-10 text-right ${
                  lv.side === "support" ? "text-green-400" : "text-red-400"
                }`}
              >
                {lv.side === "support" ? "支撑" : "阻力"}
              </span>
              <div className="flex-1 h-4 bg-slate-800 rounded-full overflow-hidden">
                <div
                  className={`h-full ${color} rounded-full transition-all`}
                  style={{ width: `${riskPct}%` }}
                />
              </div>
              <span className="text-xs text-slate-400 w-16 text-right">
                {riskPct.toFixed(0)}% ·{" "}
                {lv.cascade_layers}层
              </span>
              {lv.cascade_total_usd > 0 && (
                <span className="text-xs text-slate-500 w-20 text-right">
                  ${(lv.cascade_total_usd / 1e6).toFixed(0)}M
                </span>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
