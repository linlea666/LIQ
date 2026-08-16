"use client";

/**
 * 扫描器 · 全量代币表格
 *
 * 表格而不是卡片：这一页的用途是横向比较几十上百个币，
 * 卡片布局会让人一次只能看到 6 个，被迫在滚动中做记忆比较。
 *
 * 每一行都带完整度列，而且它排在机会分**右边紧邻**的位置——
 * 视线扫过机会分时必然经过它，很难忽略。
 */

import { useState } from "react";

import { listTokens } from "@/lib/radarApi";
import type { RadarToken } from "@/lib/radarTypes";

import {
  Card,
  CHAIN_NAME,
  Empty,
  ErrorBanner,
  StateBadge,
  TokenLink,
  ago,
  duration,
  num,
  pct,
  price,
  usd,
} from "../_components/ui";
import { usePoll } from "../_components/usePoll";

const STATES = [
  "", "WATCHING", "S0", "S1", "S2", "MOMENTUM", "DISTRIBUTION", "BLOCKED", "DEAD",
];
const SORTS = [
  { value: "opportunity", label: "机会分" },
  { value: "market_cap", label: "市值" },
  { value: "age", label: "最新" },
  { value: "holders", label: "持币人" },
] as const;

export default function ScannerPage() {
  const [state, setState] = useState("");
  const [chain, setChain] = useState("");
  const [minOpportunity, setMinOpportunity] = useState(0);
  const [sort, setSort] = useState<(typeof SORTS)[number]["value"]>("opportunity");
  const [hideDegraded, setHideDegraded] = useState(false);

  const { data, error, loading, updatedAt, refresh } = usePoll(
    () =>
      listTokens({
        state: state || undefined,
        chain_id: chain || undefined,
        min_opportunity: minOpportunity,
        sort,
        limit: 300,
      }),
    20_000,
    [state, chain, minOpportunity, sort],
  );

  const rows = (data?.items ?? []).filter(
    (token) => !hideDegraded || !token.quality_degraded,
  );

  return (
    <div className="space-y-3">
      <Card
        title="筛选"
        extra={
          <span className="text-[10px] text-slate-500">
            {updatedAt ? `更新于 ${ago(updatedAt)}` : ""} · 共 {data?.total ?? 0} 个
          </span>
        }
      >
        <div className="flex flex-wrap items-center gap-3 text-[12px]">
          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">状态</span>
            <select
              value={state}
              onChange={(e) => setState(e.target.value)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            >
              {STATES.map((s) => (
                <option key={s} value={s}>
                  {s || "全部"}
                </option>
              ))}
            </select>
          </label>

          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">链</span>
            <select
              value={chain}
              onChange={(e) => setChain(e.target.value)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            >
              <option value="">全部</option>
              <option value="56">BSC</option>
              <option value="CT_501">Solana</option>
            </select>
          </label>

          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">机会分 ≥</span>
            <input
              type="number"
              min={0}
              max={100}
              value={minOpportunity}
              onChange={(e) => setMinOpportunity(Number(e.target.value) || 0)}
              className="w-16 rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            />
          </label>

          <label className="flex items-center gap-1.5">
            <span className="text-slate-500">排序</span>
            <select
              value={sort}
              onChange={(e) => setSort(e.target.value as typeof sort)}
              className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-200"
            >
              {SORTS.map((s) => (
                <option key={s.value} value={s.value}>
                  {s.label}
                </option>
              ))}
            </select>
          </label>

          <label
            className="flex items-center gap-1.5 text-slate-400"
            title="隐藏数据不完整的币。注意：被隐藏的不等于不好，只是我们看不清"
          >
            <input
              type="checkbox"
              checked={hideDegraded}
              onChange={(e) => setHideDegraded(e.target.checked)}
              className="accent-emerald-500"
            />
            隐藏降级数据
          </label>
        </div>
      </Card>

      {error && <ErrorBanner error={error} onRetry={refresh} />}

      <Card title={`代币列表（${rows.length}）`}>
        {rows.length === 0 ? (
          <Empty text={loading ? "加载中…" : "没有符合条件的代币"} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-[11px]">
              <thead>
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">代币</th>
                  <th className="px-2 py-1.5">状态</th>
                  <th className="px-2 py-1.5 text-right">机会</th>
                  <th
                    className="px-2 py-1.5 text-right"
                    title="低完整度下机会分不可信——这一列紧邻机会分是刻意的"
                  >
                    完整度
                  </th>
                  <th className="px-2 py-1.5 text-right">置信</th>
                  <th className="px-2 py-1.5 text-right">跑路风险</th>
                  <th className="px-2 py-1.5 text-right">价格</th>
                  <th className="px-2 py-1.5 text-right">市值</th>
                  <th className="px-2 py-1.5 text-right">流动性</th>
                  <th className="px-2 py-1.5 text-right">持币人</th>
                  <th className="px-2 py-1.5 text-right">Top10</th>
                  <th className="px-2 py-1.5 text-right">聪明钱</th>
                  <th className="px-2 py-1.5 text-right">1h</th>
                  <th className="px-2 py-1.5 text-right">币龄</th>
                  <th className="px-2 py-1.5">标注</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((token) => (
                  <Row
                    key={`${token.chain_id}-${token.contract_address}`}
                    token={token}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}

function scoreCls(value: number, invert = false): string {
  const v = invert ? 100 - value : value;
  if (v >= 75) return "text-emerald-300";
  if (v >= 55) return "text-lime-300";
  if (v >= 35) return "text-amber-300";
  return "text-rose-300";
}

function Row({ token }: { token: RadarToken }) {
  return (
    <tr className="border-b border-slate-900 hover:bg-slate-900/40">
      <td className="px-2 py-1.5">
        <TokenLink
          chainId={token.chain_id}
          address={token.contract_address}
          symbol={token.symbol}
          className="font-medium"
        />
        <span className="ml-1 text-[9px] text-slate-600">
          {CHAIN_NAME[token.chain_id] ?? token.chain_id}
        </span>
      </td>
      <td className="px-2 py-1.5">
        <StateBadge state={token.state} />
      </td>
      <td className={`px-2 py-1.5 text-right tabular-nums ${scoreCls(token.scores.opportunity)}`}>
        {token.scores.opportunity.toFixed(0)}
      </td>
      <td className={`px-2 py-1.5 text-right tabular-nums ${scoreCls(token.scores.data_quality)}`}>
        {token.scores.data_quality.toFixed(0)}
      </td>
      <td className={`px-2 py-1.5 text-right tabular-nums ${scoreCls(token.scores.confidence)}`}>
        {token.scores.confidence.toFixed(0)}
      </td>
      <td
        className={`px-2 py-1.5 text-right tabular-nums ${scoreCls(token.scores.rug_risk, true)}`}
      >
        {token.scores.rug_risk.toFixed(0)}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {price(token.price)}
      </td>
      <td
        className="px-2 py-1.5 text-right tabular-nums text-slate-300"
        title={token.mc_source === "computed" ? "推算值，接口未直接提供" : undefined}
      >
        {usd(token.market_cap)}
        {token.mc_source === "computed" && <span className="text-slate-600">*</span>}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {usd(token.liquidity)}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {num(token.holders)}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {token.top10_percent === null ? "—" : `${token.top10_percent.toFixed(0)}%`}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-300">
        {num(token.smart_money_count)}
      </td>
      <td
        className={`px-2 py-1.5 text-right tabular-nums ${
          (token.pct_change_1h ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
        }`}
      >
        {pct(token.pct_change_1h)}
      </td>
      <td className="px-2 py-1.5 text-right tabular-nums text-slate-400">
        {duration(token.age_sec)}
      </td>
      <td className="px-2 py-1.5">
        <div className="flex gap-1">
          {token.quality_degraded && (
            <span
              className="rounded border border-amber-800 px-1 text-[9px] text-amber-400"
              title="部分数据组已过期或缺失"
            >
              降级
            </span>
          )}
          {token.risk.gate_blocked && (
            <span
              className="rounded border border-rose-800 px-1 text-[9px] text-rose-400"
              title={token.risk.gate_reasons.join("；")}
            >
              拦截
            </span>
          )}
          {!token.risk.audit_checked && (
            <span
              className="rounded border border-slate-700 px-1 text-[9px] text-slate-500"
              title="尚未查询审计——不等于审计通过"
            >
              未审
            </span>
          )}
        </div>
      </td>
    </tr>
  );
}
