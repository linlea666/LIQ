"use client";

/**
 * 代币详情 · 一个币的完整档案
 *
 * 包含它的历史轨迹、每一次警报、每一次被拦截，以及最终结局。
 *
 * 拒绝记录和警报记录并列展示，是这一页最重要的设计：
 * 一个只显示"我们为什么看好它"的页面无法用于改进系统。
 * 只有同时看到"我们曾经因为 Top10 太集中拦下它，而它后来涨了 8 倍"，
 * 才能发现阈值的问题。
 */

import { useParams } from "next/navigation";

import { getTokenDetail } from "@/lib/radarApi";
import type { RadarSnapshot } from "@/lib/radarTypes";

import {
  Card,
  CHAIN_NAME,
  Empty,
  ErrorBanner,
  QualityFlag,
  ScorePanel,
  StateBadge,
  Stat,
  ago,
  clock,
  duration,
  num,
  pct,
  price,
  usd,
} from "../../../_components/ui";
import { usePoll } from "../../../_components/usePoll";

export default function TokenDetailPage() {
  // Next 16 里 page 的 params 是 Promise；useParams 在客户端组件里
  // 返回同步对象，是这个场景下更直接的读法
  const params = useParams<{ chain: string; address: string }>();
  const chain = params?.chain ?? "";
  const address = params?.address ?? "";

  const { data, error, loading, updatedAt, refresh } = usePoll(
    () => getTokenDetail(chain, address),
    20_000,
    [chain, address],
  );

  if (error) return <ErrorBanner error={error} onRetry={refresh} />;
  if (!data) return <Empty text={loading ? "加载中…" : "未收录该代币"} />;

  const { identity, live, quality, snapshots, alerts, milestones, outcomes, rejections } = data;

  return (
    <div className="space-y-3">
      {/* 身份 */}
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <div className="flex items-center gap-2">
              <h1 className="text-lg font-bold text-slate-100">
                {identity.symbol || "未知代币"}
              </h1>
              {live && <StateBadge state={live.state} />}
              <span className="rounded border border-slate-700 px-1.5 py-0.5 text-[10px] text-slate-400">
                {CHAIN_NAME[identity.chain_id] ?? identity.chain_id}
              </span>
              {!identity.in_memory && (
                <span
                  className="rounded border border-slate-700 bg-slate-900/60 px-1.5 py-0.5 text-[10px] text-slate-500"
                  title="已从内存淘汰，只剩历史记录。它可能已经沉寂，也可能只是被内存水位挤出去了"
                >
                  仅历史
                </span>
              )}
            </div>
            <div className="mt-1 font-mono text-[10px] text-slate-500">
              {identity.contract_address}
            </div>
          </div>
          <span className="text-[10px] text-slate-500">
            {updatedAt ? `更新于 ${ago(updatedAt)}` : ""}
          </span>
        </div>
      </Card>

      {live && (
        <>
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-7">
            <Stat label="价格" value={price(live.price)} />
            <Stat
              label="市值"
              value={usd(live.market_cap)}
              tone={live.mc_source === "computed" ? "warn" : "default"}
              hint={
                live.mc_source === "computed"
                  ? "推算值：接口未直接提供，我们用供应量×价格算出来的，可能偏离数倍"
                  : "接口直接提供"
              }
            />
            <Stat label="流动性" value={usd(live.liquidity)} />
            <Stat label="持币人" value={num(live.holders)} />
            <Stat
              label="Top10 占比"
              value={live.top10_percent === null ? "—" : `${live.top10_percent.toFixed(1)}%`}
              tone={(live.top10_percent ?? 0) > 50 ? "warn" : "default"}
            />
            <Stat label="聪明钱" value={num(live.smart_money_count)} />
            <Stat label="币龄" value={duration(live.age_sec)} />
          </div>

          <div className="grid gap-3 lg:grid-cols-[320px_1fr]">
            <Card title="五维评分">
              <ScorePanel scores={live.scores} />
              <div className="mt-3">
                <QualityFlag token={live} />
              </div>
              {live.risk.gate_reasons.length > 0 && (
                <ul className="mt-2 space-y-1">
                  {live.risk.gate_reasons.map((reason, index) => (
                    <li
                      key={index}
                      className="rounded border border-rose-900/60 bg-rose-950/30 px-2 py-1 text-[10px] text-rose-300"
                    >
                      {reason}
                    </li>
                  ))}
                </ul>
              )}
              {quality && (
                <div className="mt-3 space-y-1 border-t border-slate-800 pt-2 text-[10px] text-slate-500">
                  <div>观测次数：{quality.observation_count}</div>
                  <div>历史深度：{quality.history_depth}</div>
                  <div className="pt-1">
                    字段来源（
                    <span title="reported 是接口直接给的；computed 是我们算的">
                      来源不同精度差别很大
                    </span>
                    ）：
                  </div>
                  {Object.entries(quality.field_source).map(([field, source]) => (
                    <div key={field} className="pl-2">
                      {field}: <span className="text-slate-400">{source}</span>
                    </div>
                  ))}
                </div>
              )}
            </Card>

            <Card title={`历史轨迹（${snapshots.length} 帧）`}>
              {snapshots.length === 0 ? (
                <Empty text="尚无历史快照" />
              ) : (
                <Sparkline snapshots={snapshots} />
              )}
            </Card>
          </div>
        </>
      )}

      <div className="grid gap-3 xl:grid-cols-2">
        {/* 警报 */}
        <Card title={`警报记录（${alerts.length}）`}>
          {alerts.length === 0 ? (
            <Empty text="从未对该币发出警报" />
          ) : (
            <table className="w-full text-[11px]">
              <thead>
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">时间</th>
                  <th className="px-2 py-1.5">类型</th>
                  <th className="px-2 py-1.5 text-right">机会</th>
                  <th className="px-2 py-1.5 text-right">完整度</th>
                  <th className="px-2 py-1.5">复核</th>
                </tr>
              </thead>
              <tbody>
                {alerts.map((alert) => (
                  <tr key={alert.alert_id} className="border-b border-slate-900">
                    <td className="px-2 py-1.5 whitespace-nowrap text-slate-400">
                      {clock(alert.created_at)}
                    </td>
                    <td className="px-2 py-1.5">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[9px] ${
                          alert.is_near_miss
                            ? "border-slate-700 text-slate-500"
                            : "border-emerald-700 text-emerald-300"
                        }`}
                      >
                        {alert.alert_kind}
                        {alert.is_near_miss ? " 差一点" : ""}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
                      {alert.opportunity?.toFixed(0) ?? "—"}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                      {alert.data_quality?.toFixed(0) ?? "—"}
                    </td>
                    <td className="px-2 py-1.5 text-[10px] text-slate-500">
                      {alert.review_state ?? "NEW"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        {/* 拒绝记录 —— 与警报并列，这是改进系统的唯一入口 */}
        <Card
          title={`拦截记录（${rejections.length}）`}
          extra={
            <span
              className="text-[10px] text-slate-500"
              title="只看'为什么看好'无法改进系统。必须同时看见'为什么拦下'"
            >
              反事实依据
            </span>
          }
        >
          {rejections.length === 0 ? (
            <Empty text="从未被拦截" />
          ) : (
            <table className="w-full text-[11px]">
              <thead>
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">时间</th>
                  <th className="px-2 py-1.5">规则</th>
                  <th className="px-2 py-1.5 text-right">实际</th>
                  <th className="px-2 py-1.5 text-right">阈值</th>
                  <th className="px-2 py-1.5">说明</th>
                </tr>
              </thead>
              <tbody>
                {rejections.map((row, index) => (
                  <tr key={index} className="border-b border-slate-900">
                    <td className="px-2 py-1.5 whitespace-nowrap text-slate-400">
                      {clock(row.occurred_at)}
                    </td>
                    <td className="px-2 py-1.5 text-slate-300">{row.rule}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-rose-300">
                      {num(row.actual_value, 2)}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                      {num(row.threshold_value, 2)}
                    </td>
                    <td className="px-2 py-1.5 text-slate-500">{row.actual_text ?? "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        {/* 结局 */}
        <Card title="追踪结局">
          {outcomes.length === 0 ? (
            <Empty text="尚无追踪结果" />
          ) : (
            <div className="space-y-2">
              {outcomes.map((outcome) => (
                <div
                  key={outcome.alert_id}
                  className="rounded border border-slate-800 bg-slate-950/40 p-2"
                >
                  <div className="mb-1.5 flex items-center justify-between text-[10px] text-slate-500">
                    <span>信号于 {clock(outcome.signal_at)}</span>
                    <span>{outcome.is_final ? "已定案" : "追踪中"}</span>
                  </div>
                  <div className="grid grid-cols-3 gap-1.5 text-[11px]">
                    <Mini
                      label="原始峰值"
                      value={
                        outcome.peak_multiple == null
                          ? "—"
                          : `${outcome.peak_multiple.toFixed(2)}x`
                      }
                      hint="含插针，通常卖不到"
                    />
                    <Mini
                      label="持续峰值"
                      value={
                        outcome.sustained_ath_price == null || outcome.signal_price == null
                          ? "—"
                          : `${(outcome.sustained_ath_price / outcome.signal_price).toFixed(2)}x`
                      }
                      hint="剔除插针后的真实高点"
                    />
                    <Mini
                      label="流动性折算"
                      value={
                        outcome.liq_adjusted_multiple == null
                          ? "—"
                          : `${outcome.liq_adjusted_multiple.toFixed(2)}x`
                      }
                      hint="考虑滑点后能落袋的倍数"
                    />
                    <Mini
                      label="最大回撤"
                      value={outcome.mae_pct == null ? "—" : `${outcome.mae_pct.toFixed(1)}%`}
                    />
                    <Mini label="到 2x" value={duration(outcome.time_to_2x_sec)} />
                    <Mini
                      label="领先时间"
                      value={duration(outcome.lead_time_sec)}
                      hint="比上币安热门榜早多久——雷达的全部价值"
                    />
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>

        {/* 里程碑 */}
        <Card title="市值里程碑">
          {milestones.length === 0 ? (
            <Empty text="未达到任何里程碑" />
          ) : (
            <ul className="space-y-1">
              {milestones.map((milestone, index) => (
                <li
                  key={index}
                  className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/40 px-2 py-1.5 text-[11px]"
                >
                  <span className="font-semibold text-emerald-300">
                    {usd(milestone.milestone_usd)}
                  </span>
                  <span className="text-slate-400">{clock(milestone.occurred_at)}</span>
                  <span
                    className="text-slate-500"
                    title="从代币发行到达成该市值所用时间"
                  >
                    上线后 {duration(milestone.token_age_sec)}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </div>
  );
}

function Mini({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded border border-slate-800 px-2 py-1" title={hint}>
      <div className="text-[9px] text-slate-500">{label}</div>
      <div className="tabular-nums text-slate-200">{value}</div>
    </div>
  );
}

/**
 * 轻量走势图。
 *
 * 用 SVG 手绘而不是引图表库：这一页只需要看趋势形状，
 * 为此多打包一个图表库对一个每天访问几次的页面并不划算。
 * 价格与机会分叠加显示——两条线背离时（价涨分跌）通常意味着派发。
 */
function Sparkline({ snapshots }: { snapshots: RadarSnapshot[] }) {
  const points = snapshots.filter((s) => s.price !== null);
  if (points.length < 2) return <Empty text="数据点不足，无法绘制" />;

  const width = 800;
  const height = 160;
  const prices = points.map((p) => p.price as number);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);
  const range = maxPrice - minPrice || 1;

  const toX = (index: number) => (index / (points.length - 1)) * width;
  const priceY = (value: number) => height - ((value - minPrice) / range) * (height - 10) - 5;
  const scoreY = (value: number) => height - (value / 100) * (height - 10) - 5;

  const pricePath = points
    .map((p, i) => `${i === 0 ? "M" : "L"}${toX(i).toFixed(1)},${priceY(p.price as number).toFixed(1)}`)
    .join(" ");
  // 保留原始下标再过滤，两条线的横轴才对得上；
  // 先过滤再重新编号会让机会分曲线相对价格曲线整体平移
  const oppPath = points
    .map((p, index) => ({ index, opportunity: p.opportunity }))
    .filter((p): p is { index: number; opportunity: number } => p.opportunity !== null)
    .map((p, i) => `${i === 0 ? "M" : "L"}${toX(p.index).toFixed(1)},${scoreY(p.opportunity).toFixed(1)}`)
    .join(" ");

  const first = points[0];
  const last = points[points.length - 1];
  const change = ((last.price as number) / (first.price as number) - 1) * 100;

  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-3 text-[10px]">
        <span className="flex items-center gap-1">
          <span className="inline-block h-0.5 w-4 bg-sky-400" /> 价格
        </span>
        <span className="flex items-center gap-1">
          <span className="inline-block h-0.5 w-4 bg-emerald-400" /> 机会分
        </span>
        <span className="text-slate-500">
          {clock(first.observed_at)} → {clock(last.observed_at)}
        </span>
        <span className={change >= 0 ? "text-emerald-400" : "text-rose-400"}>
          区间 {pct(change)}
        </span>
        <span className="text-slate-600" title="两条线背离（价涨分跌）通常意味着派发已经开始">
          背离即派发信号
        </span>
      </div>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-40 w-full rounded bg-slate-950/60"
        preserveAspectRatio="none"
      >
        <path d={pricePath} fill="none" stroke="#38bdf8" strokeWidth={1.5} />
        {oppPath && (
          <path
            d={oppPath}
            fill="none"
            stroke="#34d399"
            strokeWidth={1.2}
            strokeDasharray="3 2"
          />
        )}
      </svg>
      <div className="mt-1 flex justify-between text-[9px] text-slate-600">
        <span>{price(minPrice)}</span>
        <span>{price(maxPrice)}</span>
      </div>
    </div>
  );
}
