"use client";

/**
 * 研究页 · 回答"我们的阈值是不是设错了"
 *
 * 这一页存在的理由：一个只看自己发出的警报的系统会持续自我肯定，
 * 因为它永远看不见被自己拦下的那些赢家。三块内容各回答一个问题：
 *
 *   - **拦截统计**：哪条规则杀得最多？它杀掉的都是垃圾吗？
 *   - **Near-Miss**：差一点触发的那些，后来涨了多少？
 *     如果它们的表现和真正发出的警报差不多，说明阈值定得太严。
 *   - **KPI**：按成熟队列统计的真实命中率，且样本量必须同屏显示。
 */

import { useState } from "react";

import { listKpi, listNearMiss, listRejections, rebuildKpi } from "@/lib/radarApi";

import {
  Card,
  Empty,
  ErrorBanner,
  TokenLink,
  ago,
  clock,
  num,
} from "../_components/ui";
import { usePoll } from "../_components/usePoll";

export default function ResearchPage() {
  const [sinceHours, setSinceHours] = useState(168);
  const [rule, setRule] = useState("");
  const [rebuilding, setRebuilding] = useState(false);

  const rejections = usePoll(
    () => listRejections({ since_hours: sinceHours, rule: rule || undefined, limit: 200 }),
    60_000,
    [sinceHours, rule],
  );
  const nearMiss = usePoll(
    () => listNearMiss({ since_hours: sinceHours, limit: 100 }),
    60_000,
    [sinceHours],
  );
  const kpi = usePoll(() => listKpi({ days: 30 }), 120_000);

  const nearMissItems = nearMiss.data?.items ?? [];
  const nearMissWithOutcome = nearMissItems.filter((a) => a.peak_multiple != null);
  const nearMissWinners = nearMissWithOutcome.filter((a) => (a.peak_multiple ?? 0) >= 2);

  return (
    <div className="space-y-3">
      <Card title="研究窗口">
        <div className="flex flex-wrap items-center gap-3 text-[12px]">
          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">时间窗</span>
            <select
              value={sinceHours}
              onChange={(e) => setSinceHours(Number(e.target.value))}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            >
              <option value={24}>24 小时</option>
              <option value={168}>7 天</option>
              <option value={720}>30 天</option>
              <option value={2160}>90 天</option>
            </select>
          </label>
          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">规则</span>
            <input
              value={rule}
              onChange={(e) => setRule(e.target.value)}
              placeholder="全部"
              className="w-40 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>
          <button
            disabled={rebuilding}
            onClick={async () => {
              setRebuilding(true);
              try {
                await rebuildKpi();
                kpi.refresh();
              } finally {
                setRebuilding(false);
              }
            }}
            className="ml-auto rounded border border-slate-700 bg-slate-900 px-2.5 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"
          >
            {rebuilding ? "重算中…" : "重算 KPI"}
          </button>
        </div>
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        {/* 拦截统计 */}
        <Card
          title="风险门拦截统计"
          extra={
            <span className="text-[10px] text-slate-500">
              {rejections.updatedAt ? `更新于 ${ago(rejections.updatedAt)}` : ""}
            </span>
          }
        >
          {rejections.error && <ErrorBanner error={rejections.error} onRetry={rejections.refresh} />}
          {(rejections.data?.by_rule.length ?? 0) === 0 ? (
            <Empty text={rejections.loading ? "加载中…" : "该窗口内没有拦截记录"} />
          ) : (
            <table className="w-full text-[11px]">
              <thead>
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">规则</th>
                  <th className="px-2 py-1.5">层</th>
                  <th className="px-2 py-1.5 text-right">次数</th>
                  <th className="px-2 py-1.5" />
                </tr>
              </thead>
              <tbody>
                {rejections.data?.by_rule.map((row) => (
                  <tr key={`${row.gate}-${row.rule}`} className="border-b border-slate-900">
                    <td className="px-2 py-1.5 text-slate-300">{row.rule}</td>
                    <td className="px-2 py-1.5">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[9px] ${
                          row.gate === "execution_blocker"
                            ? "border-rose-800 bg-rose-950/40 text-rose-300"
                            : "border-amber-800 bg-amber-950/40 text-amber-300"
                        }`}
                        title={
                          row.gate === "execution_blocker"
                            ? "硬拦截：命中即完全排除，且不做样本采集"
                            : "研究门：不发警报，但保留样本用于反事实分析"
                        }
                      >
                        {row.gate === "execution_blocker" ? "硬拦截" : "研究门"}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
                      {row.n}
                    </td>
                    <td className="px-2 py-1.5">
                      <button
                        onClick={() => setRule(row.rule)}
                        className="text-[10px] text-sky-400 hover:underline"
                      >
                        查看
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>

        {/* Near-Miss */}
        <Card
          title="Near-Miss 反事实"
          extra={
            nearMissWithOutcome.length > 0 && (
              <span
                className="text-[10px] text-amber-300"
                title="如果这个比例接近真正发出的警报，说明阈值可能设得太严"
              >
                ≥2x：{nearMissWinners.length}/{nearMissWithOutcome.length}（
                {((nearMissWinners.length / nearMissWithOutcome.length) * 100).toFixed(0)}%）
              </span>
            )
          }
        >
          {nearMiss.error && <ErrorBanner error={nearMiss.error} onRetry={nearMiss.refresh} />}
          {nearMissItems.length === 0 ? (
            <Empty text={nearMiss.loading ? "加载中…" : "该窗口内没有 Near-Miss"} />
          ) : (
            <div className="max-h-96 overflow-auto">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-slate-900">
                  <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                    <th className="px-2 py-1.5">时间</th>
                    <th className="px-2 py-1.5">代币</th>
                    <th className="px-2 py-1.5">差在哪</th>
                    <th className="px-2 py-1.5 text-right">机会</th>
                    <th className="px-2 py-1.5 text-right">后来峰值</th>
                  </tr>
                </thead>
                <tbody>
                  {nearMissItems.map((alert) => (
                    <tr key={alert.alert_id} className="border-b border-slate-900">
                      <td className="px-2 py-1.5 whitespace-nowrap text-slate-500">
                        {clock(alert.created_at)}
                      </td>
                      <td className="px-2 py-1.5">
                        {alert.chain_id && alert.contract_address ? (
                          <TokenLink
                            chainId={alert.chain_id}
                            address={alert.contract_address}
                            symbol={alert.symbol}
                          />
                        ) : (
                          "—"
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-slate-400">
                        {String(
                          (alert.trigger as Record<string, unknown> | null)?.missing ??
                            alert.alert_kind,
                        )}
                      </td>
                      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
                        {alert.opportunity?.toFixed(0) ?? "—"}
                      </td>
                      <td
                        className={`px-2 py-1.5 text-right tabular-nums ${
                          (alert.peak_multiple ?? 0) >= 2
                            ? "text-amber-300"
                            : "text-slate-500"
                        }`}
                        title={
                          (alert.peak_multiple ?? 0) >= 2
                            ? "这是我们错过的——值得回头看看阈值"
                            : undefined
                        }
                      >
                        {alert.peak_multiple == null
                          ? "—"
                          : `${alert.peak_multiple.toFixed(2)}x`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </Card>
      </div>

      {/* 拦截明细 */}
      <Card title={`拦截明细${rule ? `：${rule}` : ""}（${rejections.data?.total ?? 0}）`}>
        {(rejections.data?.items.length ?? 0) === 0 ? (
          <Empty text="无记录" />
        ) : (
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-slate-900">
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">时间</th>
                  <th className="px-2 py-1.5">代币</th>
                  <th className="px-2 py-1.5">规则</th>
                  <th className="px-2 py-1.5 text-right">实际值</th>
                  <th className="px-2 py-1.5 text-right">阈值</th>
                  <th className="px-2 py-1.5">说明</th>
                  <th className="px-2 py-1.5 text-right">完整度</th>
                </tr>
              </thead>
              <tbody>
                {rejections.data?.items.map((row) => (
                  <tr key={row.rejection_id} className="border-b border-slate-900">
                    <td className="px-2 py-1.5 whitespace-nowrap text-slate-500">
                      {clock(row.occurred_at)}
                    </td>
                    <td className="px-2 py-1.5">
                      {row.chain_id && row.contract_address ? (
                        <TokenLink
                          chainId={row.chain_id}
                          address={row.contract_address}
                          symbol={row.symbol}
                        />
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="px-2 py-1.5 text-slate-300">{row.rule}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-rose-300">
                      {num(row.actual_value, 2)}
                    </td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                      {num(row.threshold_value, 2)}
                    </td>
                    <td className="px-2 py-1.5 text-slate-500">{row.actual_text ?? "—"}</td>
                    <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
                      {num(row.data_quality, 0)}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* KPI */}
      <Card
        title="每日 KPI"
        extra={
          <span
            className="text-[10px] text-slate-500"
            title="只统计已到期的样本。未成熟的样本会系统性低估收益，因此被排除在外"
          >
            仅含成熟样本
          </span>
        }
      >
        {kpi.error && <ErrorBanner error={kpi.error} onRetry={kpi.refresh} />}
        {(kpi.data?.items.length ?? 0) === 0 ? (
          <Empty text={kpi.loading ? "加载中…" : "尚无 KPI 数据（需要有已定案的追踪结果）"} />
        ) : (
          <div className="max-h-96 overflow-auto">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-slate-900">
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">日期</th>
                  <th className="px-2 py-1.5">类型</th>
                  <th className="px-2 py-1.5">窗口</th>
                  <th
                    className="px-2 py-1.5 text-right"
                    title="3 个样本的 67% 和 300 个样本的 67% 不是同一个信息"
                  >
                    样本量
                  </th>
                  <th className="px-2 py-1.5">指标</th>
                  <th className="px-2 py-1.5">策略版本</th>
                </tr>
              </thead>
              <tbody>
                {kpi.data?.items.map((row, index) => (
                  <tr
                    key={`${row.stat_date}-${row.alert_kind}-${row.horizon}-${index}`}
                    className="border-b border-slate-900"
                  >
                    <td className="px-2 py-1.5 text-slate-400">{row.stat_date}</td>
                    <td className="px-2 py-1.5 text-slate-300">{row.alert_kind}</td>
                    <td className="px-2 py-1.5 text-slate-400">{row.horizon}</td>
                    <td
                      className={`px-2 py-1.5 text-right tabular-nums ${
                        row.matured_count < 10 ? "text-amber-400" : "text-slate-300"
                      }`}
                      title={row.matured_count < 10 ? "样本太少，任何比例都不可靠" : undefined}
                    >
                      {row.matured_count}
                    </td>
                    <td className="px-2 py-1.5 text-slate-400">
                      {row.payload
                        ? Object.entries(row.payload)
                            .map(([key, value]) =>
                              `${key} ${typeof value === "number" ? value.toFixed(2) : value}`,
                            )
                            .join(" · ")
                        : "—"}
                    </td>
                    <td className="px-2 py-1.5 text-[10px] text-slate-600">
                      {row.strategy_version}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
