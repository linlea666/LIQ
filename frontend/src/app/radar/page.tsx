"use client";

/**
 * 指挥中心 · 一屏回答"现在值得看什么、系统本身可不可信"
 *
 * 页面顺序刻意如此：先系统状态，再候选，最后警报。
 * 理由是如果采集停了或数据大面积降级，下面的候选列表就没有意义——
 * 让人先看到一屏漂亮的候选、再发现数据是两小时前的，是最糟糕的信息顺序。
 */

import Link from "next/link";

import { listAlerts, listTokens, getHealth } from "@/lib/radarApi";
import type { RadarToken } from "@/lib/radarTypes";

import {
  Card,
  CHAIN_NAME,
  Empty,
  ErrorBanner,
  QualityFlag,
  ScorePanel,
  StateBadge,
  Stat,
  TokenLink,
  ago,
  duration,
  pct,
  usd,
} from "./_components/ui";
import { usePoll } from "./_components/usePoll";

export default function RadarCommandCenter() {
  const health = usePoll(getHealth, 15_000);
  const tokens = usePoll(() => listTokens({ min_opportunity: 40, limit: 24 }), 20_000);
  const alerts = usePoll(() => listAlerts({ since_hours: 24, limit: 12 }), 20_000);

  const states = tokens.data?.states ?? {};
  const items = tokens.data?.items ?? [];
  const degradedCount = items.filter((t) => t.quality_degraded).length;

  return (
    <div className="space-y-4">
      {health.error && <ErrorBanner error={health.error} onRetry={health.refresh} />}

      {/* 系统状态放最上面：候选列表的可信度完全取决于它 */}
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-7">
        <Stat
          label="采集状态"
          value={health.data?.collector_ok ? "正常" : "异常"}
          tone={health.data?.collector_ok ? "good" : "bad"}
          hint="判据是最近一轮是否真的采到数据，而不是进程是否存活"
        />
        <Stat
          label="最近采集"
          value={ago(health.data?.last_cycle_at)}
          // 用上次成功拉取的时刻做基准，而不是当下的浏览器时间：
          // 服务不可达时 Date.now() 会一直走，指标就从"服务多久没采集了"
          // 悄悄变成了"这个页面开着多久了"
          tone={
            health.updatedAt &&
            health.data?.last_cycle_at &&
            health.updatedAt - health.data.last_cycle_at > 300_000
              ? "warn"
              : "default"
          }
        />
        <Stat label="内存中代币" value={health.data?.tokens_in_memory ?? "—"} />
        <Stat
          label="内存占用"
          value={health.data?.rss_mb ? `${health.data.rss_mb.toFixed(0)}MB` : "—"}
          tone={(health.data?.rss_mb ?? 0) > 400 ? "warn" : "default"}
          hint="容器上限 512MB。超过 400MB 需要关注：OOM 不会留下任何日志"
        />
        <Stat label="运行时长" value={duration(health.data?.uptime_sec)} />
        <Stat
          label="S1+ 候选"
          value={(states.S1 ?? 0) + (states.S2 ?? 0) + (states.MOMENTUM ?? 0)}
          tone="good"
        />
        <Stat
          label="数据降级"
          value={degradedCount}
          tone={degradedCount > items.length / 3 ? "warn" : "default"}
          hint="这些币的评分建立在不完整的输入上"
        />
      </div>

      <div className="grid gap-4 lg:grid-cols-[1fr_380px]">
        {/* 候选 */}
        <Card
          title="当前候选"
          extra={
            <div className="flex items-center gap-2 text-[10px] text-slate-500">
              <span>{tokens.updatedAt ? `更新于 ${ago(tokens.updatedAt)}` : ""}</span>
              <Link href="/radar/scanner" className="text-sky-400 hover:underline">
                全部 →
              </Link>
            </div>
          }
        >
          {tokens.error && <ErrorBanner error={tokens.error} onRetry={tokens.refresh} />}
          {!tokens.error && items.length === 0 ? (
            <Empty text={tokens.loading ? "加载中…" : "当前没有机会分 ≥40 的候选"} />
          ) : (
            <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
              {items.map((token) => (
                <TokenCard key={`${token.chain_id}-${token.contract_address}`} token={token} />
              ))}
            </div>
          )}
        </Card>

        {/* 最近警报 */}
        <Card
          title="最近 24 小时警报"
          extra={
            <Link href="/radar/alerts" className="text-[10px] text-sky-400 hover:underline">
              全部 →
            </Link>
          }
        >
          {alerts.error && <ErrorBanner error={alerts.error} onRetry={alerts.refresh} />}
          {(alerts.data?.items.length ?? 0) === 0 ? (
            <Empty text={alerts.loading ? "加载中…" : "24 小时内没有发出警报"} />
          ) : (
            <ul className="space-y-1.5">
              {alerts.data?.items.map((alert) => (
                <li
                  key={alert.alert_id}
                  className="rounded border border-slate-800 bg-slate-950/40 px-2.5 py-2"
                >
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-1.5">
                      <span className="rounded border border-emerald-700 bg-emerald-950/40 px-1.5 py-0.5 text-[10px] text-emerald-300">
                        {alert.alert_kind}
                      </span>
                      {alert.chain_id && alert.contract_address && (
                        <TokenLink
                          chainId={alert.chain_id}
                          address={alert.contract_address}
                          symbol={alert.symbol}
                          className="text-[12px] font-medium"
                        />
                      )}
                    </div>
                    <span className="text-[10px] text-slate-500">{ago(alert.created_at)}</span>
                  </div>
                  <div className="mt-1 flex items-center gap-3 text-[10px] text-slate-500">
                    <span>机会 {alert.opportunity?.toFixed(0) ?? "—"}</span>
                    <span>置信 {alert.confidence?.toFixed(0) ?? "—"}</span>
                    <span title="低完整度下上面两个分数都要打折看">
                      完整度 {alert.data_quality?.toFixed(0) ?? "—"}
                    </span>
                    {alert.peak_multiple != null && (
                      <span className="ml-auto text-emerald-400">
                        峰值 {alert.peak_multiple.toFixed(2)}x
                        {!alert.is_final && (
                          <span className="text-slate-600" title="仍在追踪，最终结果未定">
                            {" "}·追踪中
                          </span>
                        )}
                      </span>
                    )}
                  </div>
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>

      {/* 状态分布 */}
      <Card title="状态机分布">
        <div className="flex flex-wrap gap-2">
          {Object.entries(states)
            .sort((a, b) => b[1] - a[1])
            .map(([state, count]) => (
              <div
                key={state}
                className="flex items-center gap-1.5 rounded border border-slate-800 bg-slate-950/40 px-2 py-1"
              >
                <StateBadge state={state} />
                <span className="text-[12px] tabular-nums text-slate-300">{count}</span>
              </div>
            ))}
          {Object.keys(states).length === 0 && <Empty text="尚无代币进入注册表" />}
        </div>
      </Card>
    </div>
  );
}

function TokenCard({ token }: { token: RadarToken }) {
  return (
    <div className="rounded border border-slate-800 bg-slate-950/40 p-2.5">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <TokenLink
            chainId={token.chain_id}
            address={token.contract_address}
            symbol={token.symbol}
            className="truncate text-[13px] font-semibold"
          />
          <div className="mt-0.5 text-[10px] text-slate-500">
            {CHAIN_NAME[token.chain_id] ?? token.chain_id} · {duration(token.age_sec)}
          </div>
        </div>
        <StateBadge state={token.state} />
      </div>

      <div className="mt-2 grid grid-cols-3 gap-1 text-[10px]">
        <div>
          <div className="text-slate-600">市值</div>
          <div className="tabular-nums text-slate-300">{usd(token.market_cap)}</div>
        </div>
        <div>
          <div className="text-slate-600">流动性</div>
          <div className="tabular-nums text-slate-300">{usd(token.liquidity)}</div>
        </div>
        <div>
          <div className="text-slate-600">1h</div>
          <div
            className={`tabular-nums ${
              (token.pct_change_1h ?? 0) >= 0 ? "text-emerald-400" : "text-rose-400"
            }`}
          >
            {pct(token.pct_change_1h)}
          </div>
        </div>
      </div>

      <div className="mt-2">
        <ScorePanel scores={token.scores} />
      </div>

      <div className="mt-2">
        <QualityFlag token={token} />
      </div>
    </div>
  );
}
