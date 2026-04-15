"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type { RangeSignalData } from "@/lib/types";

const BOX_STATE_MAP: Record<string, { label: string; color: string; desc: string }> = {
  none: { label: "未形成", color: "text-slate-500", desc: "暂无有效箱体，价格尚未在支撑阻力之间反复震荡" },
  forming: { label: "形成中", color: "text-blue-400", desc: "价格在上下沿之间初次震荡，箱体尚需时间确认" },
  confirmed: { label: "已确认", color: "text-cyan-400", desc: "上下沿均被测试过，箱体有效性得到验证" },
  mature: { label: "成熟", color: "text-green-400", desc: "箱体已维持超过72小时，是高质量的交易结构" },
  squeeze: { label: "挤压蓄力", color: "text-amber-400", desc: "BB Squeeze触发，波动率极度收窄，大行情即将来临" },
  breaking_up: { label: "向上突破中", color: "text-green-300", desc: "价格突破上沿，可能开启新的上涨趋势" },
  breaking_down: { label: "向下突破中", color: "text-red-300", desc: "价格跌破下沿，可能开启新的下跌趋势" },
  broken: { label: "已突破", color: "text-slate-400", desc: "箱体已被突破，需要等待新箱体形成" },
};

export default function RangeDetailPage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";

  const [data, setData] = useState<RangeSignalData | null>(null);
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const fetchData = async () => {
    setRefreshing(true);
    try {
      const res = await fetch(`${API_BASE}/api/range-signal/${coin}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setData(await res.json());
      setError("");
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : "未知错误");
    } finally {
      setRefreshing(false);
    }
  };

  useEffect(() => {
    fetchData();
    const timer = setInterval(fetchData, 10_000);
    return () => clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [coin]);

  if (error) {
    return (
      <div className="range-detail-page min-h-screen bg-slate-950 p-6">
        <h1 className="text-xl font-bold text-white mb-4">{coin} 箱体分析</h1>
        <p className="text-red-400">加载失败: {error}</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="range-detail-page min-h-screen bg-slate-950 flex items-center justify-center">
        <span className="text-slate-500">加载中...</span>
      </div>
    );
  }

  const hasBox = data.range_upper != null && data.range_lower != null && data.range_upper > data.range_lower;
  const stateInfo = BOX_STATE_MAP[data.box_state] || BOX_STATE_MAP.none;

  return (
    <div className="range-detail-page min-h-screen bg-slate-950 p-4 md:p-6 pb-20">
      <div className="max-w-5xl mx-auto space-y-6">
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-xl font-bold text-white">{coin} 箱体信号分析</h1>
            <p className="text-xs text-slate-500 mt-1">
              更新时间: {new Date(data.ts * 1000).toLocaleString("zh-CN")}
            </p>
          </div>
          <button
            onClick={fetchData}
            disabled={refreshing}
            className="px-3 py-1.5 rounded-lg bg-slate-800 text-slate-300 text-xs hover:bg-slate-700 disabled:opacity-50"
          >
            {refreshing ? "刷新中..." : "刷新"}
          </button>
        </div>

        {/* Overview */}
        <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4 mb-4">
            <StatBlock label="箱体状态" value={stateInfo.label} valueColor={stateInfo.color} />
            <StatBlock label="箱体质量" value={`${data.box_quality}/100`}
              valueColor={data.box_quality >= 70 ? "text-green-400" : data.box_quality >= 40 ? "text-yellow-400" : "text-red-400"} />
            <StatBlock label="突破概率" value={`${(data.breakout_probability * 100).toFixed(0)}%`}
              valueColor={data.breakout_probability >= 0.6 ? "text-red-400" : data.breakout_probability >= 0.3 ? "text-yellow-400" : "text-green-400"} />
            <StatBlock label="共振因子" value={`${data.confluence_count}/8`} valueColor="text-cyan-400" />
          </div>
          <p className="text-xs text-slate-500">{stateInfo.desc}</p>
          {data.box_age_hours > 0 && (
            <p className="text-xs text-slate-500 mt-1">
              存续时长: {data.box_age_hours >= 24 ? `${(data.box_age_hours / 24).toFixed(1)} 天` : `${data.box_age_hours.toFixed(0)} 小时`}
              {data.box_width_pct > 0 && ` · 宽度: ${data.box_width_pct.toFixed(1)}%`}
            </p>
          )}
        </div>

        {/* Box Boundaries */}
        {hasBox && (
          <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
            <h2 className="text-sm font-bold text-white mb-4">箱体边界</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              <BoundaryCard
                side="上沿 (阻力)"
                price={data.range_upper!}
                source={data.range_upper_source}
                tier={data.range_upper_tier}
                score={data.range_upper_score}
                testCount={data.range_upper_test_count}
                coin={coin}
                sideColor="text-red-400"
              />
              <BoundaryCard
                side="下沿 (支撑)"
                price={data.range_lower!}
                source={data.range_lower_source}
                tier={data.range_lower_tier}
                score={data.range_lower_score}
                testCount={data.range_lower_test_count}
                coin={coin}
                sideColor="text-green-400"
              />
            </div>
          </div>
        )}

        {/* Signal */}
        {data.signal_grade && (
          <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
            <h2 className="text-sm font-bold text-white mb-4">交易信号</h2>
            <div className={`rounded-lg p-4 border ${
              data.signal_direction === "long" ? "border-green-700/40 bg-green-950/20" : "border-red-700/40 bg-red-950/20"
            }`}>
              <div className="flex items-center gap-3 mb-3">
                <span className={`text-lg font-black px-3 py-1 rounded-lg ${
                  data.signal_grade === "S" ? "bg-amber-500 text-black" :
                  data.signal_grade === "A" ? "bg-orange-500 text-white" :
                  "bg-blue-500 text-white"
                }`}>{data.signal_grade}</span>
                <span className={`text-lg font-bold ${
                  data.signal_direction === "long" ? "text-green-400" : "text-red-400"
                }`}>
                  {data.signal_direction === "long" ? "做多" : "做空"}信号
                </span>
              </div>
              <p className="text-sm text-slate-300 mb-3">{data.signal_reason}</p>
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                {data.signal_entry != null && (
                  <MiniStat label="入场价" value={formatPrice(data.signal_entry, coin)} />
                )}
                {data.signal_stop_loss != null && (
                  <MiniStat label="止损" value={formatPrice(data.signal_stop_loss, coin)} color="text-red-400" />
                )}
                {data.signal_tp1 != null && (
                  <MiniStat label="目标1" value={formatPrice(data.signal_tp1, coin)} color="text-green-400" />
                )}
                {data.signal_rr_ratio != null && data.signal_rr_ratio > 0 && (
                  <MiniStat label="R:R" value={data.signal_rr_ratio.toFixed(1)} color="text-blue-400" />
                )}
              </div>
            </div>
          </div>
        )}

        {/* Breakout Analysis */}
        <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
          <h2 className="text-sm font-bold text-white mb-4">突破概率分析</h2>
          <div className="space-y-3">
            <div className="flex items-center gap-3">
              <div className="flex-1">
                <div className="w-full h-3 bg-slate-700 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full transition-all ${
                      data.breakout_probability >= 0.6 ? "bg-red-500" :
                      data.breakout_probability >= 0.3 ? "bg-yellow-500" : "bg-green-500"
                    }`}
                    style={{ width: `${data.breakout_probability * 100}%` }}
                  />
                </div>
              </div>
              <span className="text-lg font-bold text-white w-14 text-right">
                {(data.breakout_probability * 100).toFixed(0)}%
              </span>
            </div>
            <div className="flex items-center justify-between text-xs">
              <span className="text-slate-400">方向偏向</span>
              <span className={
                data.breakout_direction_bias === "up" ? "text-green-400 font-bold" :
                data.breakout_direction_bias === "down" ? "text-red-400 font-bold" :
                "text-slate-400"
              }>
                {data.breakout_direction_bias === "up" ? "偏向上破" :
                 data.breakout_direction_bias === "down" ? "偏向下破" : "方向未明"}
              </span>
            </div>
            {data.breakout_reason && (
              <p className="text-xs text-slate-500">{data.breakout_reason}</p>
            )}
          </div>
        </div>

        {/* Confluence */}
        <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
          <h2 className="text-sm font-bold text-white mb-4">共振因子详情 ({data.confluence_count}/8)</h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
            <ConfCard label="流动性清扫" active={data.sweep_confirmed} desc="下方/上方清算流动性已被扫取" />
            <ConfCard label="CPS 周期对齐" active={data.cps_aligned} desc="BTC 周期评分处于 3-7 震荡区" />
            <ConfCard label="BB 挤压" active={data.bb_squeeze} desc="布林带被 Keltner 通道压缩" />
            <ConfCard label="OI 堆积" active={data.oi_buildup} desc="持仓量在箱体内持续增长 >3%" />
            <ConfCard label="成交量萎缩" active={data.volume_declining} desc="近 5 日成交量持续递减" />
            <ConfCard label="资金费率极端" active={data.funding_extreme} desc="费率绝对值 > 0.03%" />
            <ConfCard label={`订单簿偏向${
              data.orderbook_imbalance === "bid_heavy" ? "(买)" :
              data.orderbook_imbalance === "ask_heavy" ? "(卖)" : ""
            }`} active={!!data.orderbook_imbalance} desc="买卖挂单 > 1.5x 不对称" />
            <ConfCard label="箱体已形成" active={hasBox} desc="上下沿均为强共振关键位" />
          </div>
        </div>

        {/* MA + MACD */}
        <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
          <h2 className="text-sm font-bold text-white mb-4">均线 & MACD</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <div className="space-y-2">
              <MADetailRow label="日线 MA60" value={data.ma60_daily} coin={coin} />
              <MADetailRow label="日线 MA120" value={data.ma120_daily} coin={coin} />
              <MADetailRow label="周线 MA60" value={data.ma60_weekly} coin={coin} />
            </div>
            <div className="space-y-2">
              <DetailRow label="MACD 位置" value={
                data.macd_daily_above_zero === true ? "零轴上方 (多头)" :
                data.macd_daily_above_zero === false ? "零轴下方 (空头)" : "—"
              } color={data.macd_daily_above_zero ? "text-green-400" : "text-red-400"} />
              <DetailRow label="柱状图方向" value={
                data.macd_daily_hist_rising === true ? "上升 (动能增强)" :
                data.macd_daily_hist_rising === false ? "下降 (动能减弱)" : "—"
              } color={data.macd_daily_hist_rising ? "text-green-400" : "text-red-400"} />
              {data.macd_daily_histogram != null && (
                <DetailRow label="柱状图值" value={
                  `${data.macd_daily_histogram >= 0 ? "+" : ""}${data.macd_daily_histogram.toFixed(2)}`
                } color={data.macd_daily_histogram >= 0 ? "text-green-400" : "text-red-400"} />
              )}
            </div>
          </div>
        </div>

        {/* Unfilled Wicks */}
        {(data.unfilled_wick_low != null || data.unfilled_wick_high != null) && (
          <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl p-5">
            <h2 className="text-sm font-bold text-white mb-4">未填补影线</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
              {data.unfilled_wick_low != null && (
                <div className="bg-slate-800 rounded-lg p-3">
                  <div className="text-xs text-slate-400 mb-1">⬇️ 下影线缺口</div>
                  <div className="text-white font-medium">{formatPrice(data.unfilled_wick_low, coin)}</div>
                </div>
              )}
              {data.unfilled_wick_high != null && (
                <div className="bg-slate-800 rounded-lg p-3">
                  <div className="text-xs text-slate-400 mb-1">⬆️ 上影线缺口</div>
                  <div className="text-white font-medium">{formatPrice(data.unfilled_wick_high, coin)}</div>
                </div>
              )}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

/* ── Sub-components ── */

function StatBlock({ label, value, valueColor }: { label: string; value: string; valueColor: string }) {
  return (
    <div>
      <div className="text-[10px] text-slate-500 mb-0.5">{label}</div>
      <div className={`text-lg font-bold ${valueColor}`}>{value}</div>
    </div>
  );
}

function MiniStat({ label, value, color = "text-white" }: { label: string; value: string; color?: string }) {
  return (
    <div className="bg-slate-800/60 rounded-lg p-2">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`text-sm font-mono font-medium ${color}`}>{value}</div>
    </div>
  );
}

function BoundaryCard({ side, price, source, tier, score, testCount, coin, sideColor }: {
  side: string; price: number; source: string; tier: string; score: number;
  testCount: number; coin: string; sideColor: string;
}) {
  return (
    <div className="bg-slate-800/60 rounded-lg p-4">
      <div className="flex items-center gap-2 mb-2">
        <span className={`text-xs font-bold ${sideColor}`}>{side}</span>
        {tier && (
          <span className={`text-[10px] px-1.5 py-0.5 rounded font-bold ${
            tier === "S" ? "bg-amber-500/20 text-amber-400" :
            tier === "A" ? "bg-orange-500/20 text-orange-400" :
            "bg-blue-500/20 text-blue-400"
          }`}>{tier}级</span>
        )}
      </div>
      <div className="text-lg font-bold text-white font-mono mb-2">{formatPrice(price, coin)}</div>
      <div className="space-y-1 text-xs text-slate-400">
        <div>来源: {source || "—"}</div>
        <div>共振分: {score.toFixed(0)}</div>
        <div>测试次数: {testCount}</div>
      </div>
    </div>
  );
}

function ConfCard({ label, active, desc }: { label: string; active: boolean; desc: string }) {
  return (
    <div className={`rounded-lg p-3 border ${
      active
        ? "border-green-700/40 bg-green-950/20"
        : "border-slate-700/30 bg-slate-800/30"
    }`}>
      <div className="flex items-center gap-1.5 mb-1">
        <span className={`text-xs ${active ? "text-green-400" : "text-slate-600"}`}>
          {active ? "✓" : "✗"}
        </span>
        <span className={`text-xs font-medium ${active ? "text-white" : "text-slate-500"}`}>{label}</span>
      </div>
      <p className="text-[10px] text-slate-600">{desc}</p>
    </div>
  );
}

function MADetailRow({ label, value, coin }: { label: string; value: number | null; coin: string }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-slate-700/50 last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      <span className="text-xs text-white font-mono">{value != null ? formatPrice(value, coin) : "—"}</span>
    </div>
  );
}

function DetailRow({ label, value, color = "text-white" }: { label: string; value: string; color?: string }) {
  return (
    <div className="flex items-center justify-between py-1.5 border-b border-slate-700/50 last:border-0">
      <span className="text-xs text-slate-400">{label}</span>
      <span className={`text-xs font-medium ${color}`}>{value}</span>
    </div>
  );
}
