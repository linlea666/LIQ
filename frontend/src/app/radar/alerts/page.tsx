"use client";

/**
 * 警报页 · 发出的信号与它们的实际结局
 *
 * 关键设计：**结局列永远和警报并排显示**。
 *
 * 一个只列出警报的页面会让人产生"系统报了很多好机会"的错觉，
 * 因为记忆天然偏向成功案例。把峰值倍数直接放在每一行，
 * 才能诚实地面对"报了 40 个，其中 31 个最高只到 1.2 倍"。
 *
 * Near-Miss 默认隐藏：它们从未发出信号，混进列表会让人
 * 误以为系统报过——但可以一键打开做阈值研究。
 */

import { useCallback, useState } from "react";

import { getAlertDetail, listAlerts, reviewAlert } from "@/lib/radarApi";
import type { RadarAlert } from "@/lib/radarTypes";

import {
  Card,
  Empty,
  ErrorBanner,
  TokenLink,
  ago,
  clock,
  duration,
  num,
  price,
  usd,
} from "../_components/ui";
import { usePoll } from "../_components/usePoll";

const KINDS = ["", "S0", "S1", "S2", "MOMENTUM", "DISTRIBUTION"];
const REVIEW_STATES = ["NEW", "REVIEWED", "TRACKING", "CLOSED"];

export default function AlertsPage() {
  const [kind, setKind] = useState("");
  const [includeNearMiss, setIncludeNearMiss] = useState(false);
  const [sinceHours, setSinceHours] = useState(72);
  const [selected, setSelected] = useState<number | null>(null);

  const { data, error, loading, updatedAt, refresh } = usePoll(
    () =>
      listAlerts({
        kind: kind || undefined,
        include_near_miss: includeNearMiss,
        since_hours: sinceHours,
        limit: 200,
      }),
    30_000,
    [kind, includeNearMiss, sinceHours],
  );

  const items = data?.items ?? [];
  const emitted = items.filter((a) => !a.is_near_miss);
  const withOutcome = emitted.filter((a) => a.peak_multiple != null);
  const winners = withOutcome.filter((a) => (a.peak_multiple ?? 0) >= 2);

  return (
    <div className="space-y-3">
      <Card
        title="筛选"
        extra={
          <span className="text-[10px] text-slate-500">
            {updatedAt ? `更新于 ${ago(updatedAt)}` : ""}
          </span>
        }
      >
        <div className="flex flex-wrap items-center gap-3 text-[12px]">
          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">类型</span>
            <select
              value={kind}
              onChange={(e) => setKind(e.target.value)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            >
              {KINDS.map((k) => (
                <option key={k} value={k}>
                  {k || "全部"}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">时间窗</span>
            <select
              value={sinceHours}
              onChange={(e) => setSinceHours(Number(e.target.value))}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            >
              <option value={24}>24 小时</option>
              <option value={72}>3 天</option>
              <option value={168}>7 天</option>
              <option value={720}>30 天</option>
            </select>
          </label>

          <label
            className="flex items-center gap-1.5 text-slate-400"
            title="Near-Miss 从未真正发出，只用于研究阈值是否设得太严"
          >
            <input
              type="checkbox"
              checked={includeNearMiss}
              onChange={(e) => setIncludeNearMiss(e.target.checked)}
              className="accent-amber-500"
            />
            含 Near-Miss
          </label>

          <div className="ml-auto flex items-center gap-3 text-[11px] text-slate-500">
            <span>发出 {emitted.length}</span>
            <span title="只统计已有追踪结果的警报。没有结果的不计入分母，否则会低估">
              有结果 {withOutcome.length}
            </span>
            <span className="text-emerald-400" title="峰值达到 2 倍以上">
              ≥2x {winners.length}
              {withOutcome.length > 0 &&
                `（${((winners.length / withOutcome.length) * 100).toFixed(0)}%）`}
            </span>
          </div>
        </div>
      </Card>

      {error && <ErrorBanner error={error} onRetry={refresh} />}

      <div className="grid gap-3 xl:grid-cols-[1fr_420px]">
        <Card title={`警报列表（${items.length}）`}>
          {items.length === 0 ? (
            <Empty text={loading ? "加载中…" : "该时间窗内没有警报"} />
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-[11px]">
                <thead>
                  <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                    <th className="px-2 py-1.5">时间</th>
                    <th className="px-2 py-1.5">代币</th>
                    <th className="px-2 py-1.5">类型</th>
                    <th className="px-2 py-1.5 text-right">机会</th>
                    <th className="px-2 py-1.5 text-right">完整度</th>
                    <th className="px-2 py-1.5 text-right">风险</th>
                    <th
                      className="px-2 py-1.5 text-right"
                      title="结局必须和警报并排——只看警报列表会高估系统表现"
                    >
                      峰值
                    </th>
                    <th className="px-2 py-1.5 text-right">当前</th>
                    <th className="px-2 py-1.5">复核</th>
                  </tr>
                </thead>
                <tbody>
                  {items.map((alert) => (
                    <AlertRow
                      key={alert.alert_id}
                      alert={alert}
                      active={selected === alert.alert_id}
                      onSelect={() => setSelected(alert.alert_id)}
                      onReviewed={refresh}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>

        <AlertDetailPanel alertId={selected} />
      </div>
    </div>
  );
}

function AlertRow({
  alert,
  active,
  onSelect,
  onReviewed,
}: {
  alert: RadarAlert;
  active: boolean;
  onSelect: () => void;
  onReviewed: () => void;
}) {
  const [saving, setSaving] = useState(false);

  const handleReview = useCallback(
    async (state: string) => {
      setSaving(true);
      try {
        await reviewAlert(alert.alert_id, state);
        onReviewed();
      } catch {
        // 复核失败不阻断浏览：下次轮询会拉回真实状态
      } finally {
        setSaving(false);
      }
    },
    [alert.alert_id, onReviewed],
  );

  const peak = alert.peak_multiple;
  return (
    <tr
      onClick={onSelect}
      className={`cursor-pointer border-b border-slate-900 hover:bg-slate-900/40 ${
        active ? "bg-slate-800/50" : ""
      } ${alert.is_near_miss ? "opacity-60" : ""}`}
    >
      <td className="px-2 py-1.5 whitespace-nowrap text-slate-400">
        {clock(alert.created_at)}
      </td>
      <td className="px-2 py-1.5">
        {alert.chain_id && alert.contract_address ? (
          <TokenLink
            chainId={alert.chain_id}
            address={alert.contract_address}
            symbol={alert.symbol}
            className="font-medium"
          />
        ) : (
          "—"
        )}
      </td>
      <td className="px-2 py-1.5">
        <span
          className={`rounded border px-1.5 py-0.5 text-[10px] ${
            alert.is_near_miss
              ? "border-slate-700 bg-slate-900/60 text-slate-500"
              : "border-emerald-700 bg-emerald-950/40 text-emerald-300"
          }`}
          title={alert.is_near_miss ? "差一点触发，未发出" : undefined}
        >
          {alert.alert_kind}
          {alert.is_near_miss ? " 差一点" : ""}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {alert.opportunity?.toFixed(0) ?? "—"}
      </td>
      <td
        className={`px-2 py-1.5 text-right tabular-nums ${
          (alert.data_quality ?? 0) < 50 ? "text-amber-400" : "text-slate-300"
        }`}
      >
        {alert.data_quality?.toFixed(0) ?? "—"}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {alert.rug_risk?.toFixed(0) ?? "—"}
      </td>
      <td
        className={`px-2 py-1.5 text-right tabular-nums ${
          peak == null ? "text-slate-600" : peak >= 2 ? "text-emerald-300" : "text-slate-400"
        }`}
        title={alert.is_final ? "已定案" : "仍在追踪中，结果可能继续变化"}
      >
        {peak == null ? "—" : `${peak.toFixed(2)}x`}
        {peak != null && !alert.is_final && <span className="text-slate-600">…</span>}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
        {alert.current_multiple == null ? "—" : `${alert.current_multiple.toFixed(2)}x`}
      </td>
      <td className="px-2 py-1.5" onClick={(e) => e.stopPropagation()}>
        <select
          value={alert.review_state ?? "NEW"}
          disabled={saving}
          onChange={(e) => handleReview(e.target.value)}
          className="rounded border border-slate-700 bg-slate-900 px-1 py-0.5 text-[10px] text-slate-300"
        >
          {REVIEW_STATES.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </td>
    </tr>
  );
}

function AlertDetailPanel({ alertId }: { alertId: number | null }) {
  const { data, error } = usePoll(
    async () => (alertId === null ? null : getAlertDetail(alertId)),
    30_000,
    [alertId],
  );

  if (alertId === null) {
    return (
      <Card title="警报详情">
        <Empty text="点击左侧任意一行查看决策依据" />
      </Card>
    );
  }
  if (error) return <Card title="警报详情"><ErrorBanner error={error} /></Card>;
  if (!data) return <Card title="警报详情"><Empty text="加载中…" /></Card>;

  const { alert, outcome, decision_snapshot: snapshot, paper_positions: positions } = data;

  return (
    <div className="space-y-3">
      <Card title="触发依据">
        {alert.factors && alert.factors.length > 0 ? (
          <ul className="space-y-1">
            {alert.factors.map((factor, index) => (
              <li
                key={index}
                className="flex items-center justify-between rounded border border-slate-800 bg-slate-950/40 px-2 py-1 text-[11px]"
              >
                <span className="text-slate-300">{String(factor.label ?? factor.name)}</span>
                <span className="tabular-nums text-slate-400">
                  {typeof factor.score === "number" ? factor.score.toFixed(1) : "—"}
                </span>
              </li>
            ))}
          </ul>
        ) : (
          <Empty text="未记录因子明细" />
        )}
        {alert.trigger && (
          <pre className="mt-2 max-h-40 overflow-auto rounded bg-slate-950/60 p-2 text-[10px] text-slate-400">
            {JSON.stringify(alert.trigger, null, 2)}
          </pre>
        )}
      </Card>

      <Card
        title="决策现场"
        extra={
          <span className="text-[10px] text-slate-500" title="判断发生的那一刻系统看到的全部输入">
            不可变快照
          </span>
        }
      >
        {snapshot ? (
          <div className="grid grid-cols-2 gap-1.5 text-[11px]">
            <Field label="价格" value={price(snapshot.price as number)} />
            <Field label="市值" value={usd(snapshot.market_cap as number)} />
            <Field label="流动性" value={usd(snapshot.liquidity as number)} />
            <Field label="持币人" value={num(snapshot.holders as number)} />
            <Field
              label="Top10"
              value={
                snapshot.top10_percent == null
                  ? "—"
                  : `${(snapshot.top10_percent as number).toFixed(1)}%`
              }
            />
            <Field label="观测时刻" value={clock(snapshot.observed_at as number)} />
          </div>
        ) : (
          <Empty text="该警报未关联快照" />
        )}
      </Card>

      <Card title="实际结局">
        {outcome ? (
          <div className="space-y-2">
            <div className="grid grid-cols-2 gap-1.5 text-[11px]">
              <Field
                label="原始峰值"
                value={
                  outcome.peak_multiple == null ? "—" : `${outcome.peak_multiple.toFixed(2)}x`
                }
                hint="含瞬时插针，通常卖不到这个价"
              />
              <Field
                label="持续峰值"
                value={
                  outcome.sustained_ath_price == null || outcome.signal_price == null
                    ? "—"
                    : `${(outcome.sustained_ath_price / outcome.signal_price).toFixed(2)}x`
                }
                hint="剔除插针后的高点，更接近真实可卖出价"
              />
              <Field
                label="流动性折算"
                value={
                  outcome.liq_adjusted_multiple == null
                    ? "—"
                    : `${outcome.liq_adjusted_multiple.toFixed(2)}x`
                }
                hint="考虑滑点后能实际落袋的倍数——纸面 10 倍常常只有 3 倍"
              />
              <Field
                label="最大回撤"
                value={outcome.mae_pct == null ? "—" : `${outcome.mae_pct.toFixed(1)}%`}
              />
              <Field label="到 2 倍用时" value={duration(outcome.time_to_2x_sec)} />
              <Field
                label="领先时间"
                value={duration(outcome.lead_time_sec)}
                hint="比它出现在币安热门榜早多久——这是雷达全部价值所在"
              />
            </div>
            <div className="text-[10px] text-slate-500">
              {outcome.is_final ? "已定案" : "追踪中，结果仍可能变化"}
              {outcome.outcome_label && ` · ${outcome.outcome_label}`}
            </div>
          </div>
        ) : (
          <Empty text="尚未产生追踪结果" />
        )}
      </Card>

      {positions.length > 0 && (
        <Card title="纸面仓位（含滑点估计）">
          <table className="w-full text-[11px]">
            <thead>
              <tr className="text-left text-[10px] text-slate-500">
                <th className="py-1">规模</th>
                <th className="py-1 text-right">估计滑点</th>
                <th className="py-1 text-right">峰值</th>
                <th className="py-1 text-right">当前</th>
              </tr>
            </thead>
            <tbody>
              {positions.map((position, index) => (
                <tr key={index} className="border-t border-slate-900">
                  <td className="py-1 text-slate-300">${position.size_usd as number}</td>
                  <td className="py-1 text-right tabular-nums text-amber-400">
                    {position.est_slippage_pct == null
                      ? "—"
                      : `${(position.est_slippage_pct as number).toFixed(1)}%`}
                  </td>
                  <td className="py-1 text-right tabular-nums text-slate-300">
                    {usd(position.peak_value_usd as number)}
                  </td>
                  <td className="py-1 text-right tabular-nums text-slate-300">
                    {usd(position.current_value_usd as number)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded border border-slate-800 bg-slate-950/40 px-2 py-1" title={hint}>
      <div className="text-[9px] text-slate-500">{label}</div>
      <div className="tabular-nums text-slate-200">{value}</div>
    </div>
  );
}
