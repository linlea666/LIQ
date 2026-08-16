"use client";

/**
 * 运维页 · 系统自身的健康
 *
 * 这一页的读者是排障的人，不是找币的人。它回答的问题是
 * "雷达自己有没有病"，而不是"哪个币值得看"。
 *
 * 最重要的一块是**限流状态**：币安接口一旦开始返回 429，
 * 采集频率会自动下调，表现是"最近没有新币"——
 * 与真的没有新币完全无法区分，除非在这里能看到 429 计数在涨。
 */

import { useState } from "react";

import {
  exportUrl,
  getConfig,
  getDiagnostics,
  getDiagnosticsBundle,
  listEvents,
} from "@/lib/radarApi";

import { Card, Empty, ErrorBanner, Stat, ago, clock, duration } from "../_components/ui";
import { usePoll } from "../_components/usePoll";

const SEVERITIES = ["", "info", "warning", "error", "critical"];
const EXPORTS = ["alerts", "outcomes", "rejections", "milestones", "kpi"];

export default function OpsPage() {
  const [severity, setSeverity] = useState("");
  const [bundle, setBundle] = useState<Record<string, unknown> | null>(null);
  const [bundleBusy, setBundleBusy] = useState(false);

  const diag = usePoll(getDiagnostics, 15_000);
  const events = usePoll(
    () => listEvents({ severity: severity || undefined, since_hours: 24, limit: 200 }),
    20_000,
    [severity],
  );
  const config = usePoll(getConfig, 300_000);

  const scheduler = (diag.data?.scheduler ?? {}) as Record<string, unknown>;
  const collectors = (diag.data?.collectors ?? {}) as Record<string, unknown>;
  const email = (diag.data?.email ?? {}) as Record<string, unknown>;
  const health = diag.data?.health;

  return (
    <div className="space-y-3">
      {diag.error && <ErrorBanner error={diag.error} onRetry={diag.refresh} />}

      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 lg:grid-cols-6">
        <Stat
          label="服务状态"
          value={health?.status === "ok" ? "正常" : "降级"}
          tone={health?.status === "ok" ? "good" : "bad"}
        />
        <Stat label="运行时长" value={duration(health?.uptime_sec)} />
        <Stat
          label="内存"
          value={health?.rss_mb ? `${health.rss_mb.toFixed(0)}MB` : "—"}
          tone={(health?.rss_mb ?? 0) > 400 ? "warn" : "default"}
          hint="容器上限 512MB"
        />
        <Stat
          label="429 计数"
          value={String(scheduler.rate_limited_total ?? "—")}
          tone={Number(scheduler.rate_limited_total ?? 0) > 0 ? "warn" : "good"}
          hint="被限流时采集会自动降频，表现和'真的没有新币'完全一样——只能靠这个数字区分"
        />
        <Stat
          label="实际 RPM"
          value={String(scheduler.actual_rpm ?? "—")}
          hint="与配置上限的差距反映真实压力"
        />
        <Stat
          label="待发邮件"
          value={String(email.pending ?? "—")}
          tone={Number(email.pending ?? 0) > 5 ? "warn" : "default"}
          hint="持续堆积说明 SMTP 有问题，警报正在静静烂在队列里"
        />
      </div>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card title="调度器" extra={<span className="text-[10px] text-slate-500">
          {diag.updatedAt ? `更新于 ${ago(diag.updatedAt)}` : ""}
        </span>}>
          <KeyValues data={scheduler} />
        </Card>

        <Card title="采集器">
          <KeyValues data={collectors} />
        </Card>
      </div>

      <Card
        title="事件流"
        extra={
          <select
            value={severity}
            onChange={(e) => setSeverity(e.target.value)}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-[11px] text-slate-300"
          >
            {SEVERITIES.map((s) => (
              <option key={s} value={s}>
                {s || "全部级别"}
              </option>
            ))}
          </select>
        }
      >
        {events.error && <ErrorBanner error={events.error} onRetry={events.refresh} />}
        {(events.data?.items.length ?? 0) === 0 ? (
          <Empty text={events.loading ? "加载中…" : "24 小时内没有事件"} />
        ) : (
          <div className="max-h-[28rem] overflow-auto">
            <table className="w-full text-[11px]">
              <thead className="sticky top-0 bg-slate-900">
                <tr className="border-b border-slate-800 text-left text-[10px] text-slate-500">
                  <th className="px-2 py-1.5">时间</th>
                  <th className="px-2 py-1.5">级别</th>
                  <th className="px-2 py-1.5">类型</th>
                  <th className="px-2 py-1.5">模块</th>
                  <th className="px-2 py-1.5">摘要</th>
                  <th className="px-2 py-1.5">关联 ID</th>
                </tr>
              </thead>
              <tbody>
                {events.data?.items.map((event) => (
                  <tr key={event.event_id} className="border-b border-slate-900">
                    <td className="px-2 py-1.5 whitespace-nowrap text-slate-500">
                      {clock(event.occurred_at)}
                    </td>
                    <td className="px-2 py-1.5">
                      <span
                        className={`rounded border px-1.5 py-0.5 text-[9px] ${
                          event.severity === "critical" || event.severity === "error"
                            ? "border-rose-800 bg-rose-950/40 text-rose-300"
                            : event.severity === "warning"
                              ? "border-amber-800 bg-amber-950/40 text-amber-300"
                              : "border-slate-700 bg-slate-900/60 text-slate-400"
                        }`}
                      >
                        {event.severity}
                      </span>
                    </td>
                    <td className="px-2 py-1.5 text-slate-300">{event.event_type}</td>
                    <td className="px-2 py-1.5 text-slate-500">{event.module ?? "—"}</td>
                    <td className="px-2 py-1.5 text-slate-300">{event.summary ?? "—"}</td>
                    <td
                      className="px-2 py-1.5 text-[9px] text-slate-600"
                      title="同一次采集周期内的所有事件共享它，用来串起完整因果链"
                    >
                      {event.correlation_id?.slice(0, 8) ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid gap-3 xl:grid-cols-2">
        <Card
          title="诊断包"
          extra={
            <button
              disabled={bundleBusy}
              onClick={async () => {
                setBundleBusy(true);
                try {
                  setBundle(await getDiagnosticsBundle());
                } catch (err) {
                  setBundle({ error: err instanceof Error ? err.message : String(err) });
                } finally {
                  setBundleBusy(false);
                }
              }}
              className="rounded border border-slate-700 bg-slate-900 px-2.5 py-1 text-[11px] text-slate-300 hover:bg-slate-800 disabled:opacity-50"
            >
              {bundleBusy ? "生成中…" : "一键生成"}
            </button>
          }
        >
          <p className="mb-2 text-[10px] text-slate-500">
            一次性抓取健康、调度、表体积、近期错误和待发邮件。
            分十次手工拼凑会拿到不同时刻的状态，最后拼出一幅并不存在的图景。
          </p>
          {bundle ? (
            <pre className="max-h-80 overflow-auto rounded bg-slate-950/60 p-2 text-[10px] text-slate-400">
              {JSON.stringify(bundle, null, 2)}
            </pre>
          ) : (
            <Empty text="尚未生成" />
          )}
        </Card>

        <div className="space-y-3">
          <Card title="数据导出">
            <div className="flex flex-wrap gap-2">
              {EXPORTS.map((dataset) => (
                <a
                  key={dataset}
                  href={exportUrl(dataset, 720)}
                  target="_blank"
                  rel="noreferrer"
                  className="rounded border border-slate-700 bg-slate-900 px-2.5 py-1 text-[11px] text-sky-300 hover:bg-slate-800"
                >
                  {dataset} · 30 天
                </a>
              ))}
            </div>
            <p className="mt-2 text-[10px] text-slate-500">
              导出内含策略指纹。数据一旦脱离系统，指纹是唯一能说明
              「这是哪套参数产出的」的东西。
            </p>
          </Card>

          <Card title="当前生效配置">
            {config.data ? (
              <pre className="max-h-64 overflow-auto rounded bg-slate-950/60 p-2 text-[10px] text-slate-400">
                {JSON.stringify(config.data, null, 2)}
              </pre>
            ) : (
              <Empty text={config.loading ? "加载中…" : "读取失败"} />
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

function KeyValues({ data }: { data: Record<string, unknown> }) {
  const entries = Object.entries(data);
  if (entries.length === 0) return <Empty text="无数据" />;
  return (
    <div className="grid grid-cols-2 gap-1.5 text-[11px]">
      {entries.map(([key, value]) => (
        <div key={key} className="rounded border border-slate-800 bg-slate-950/40 px-2 py-1">
          <div className="text-[9px] text-slate-500">{key}</div>
          <div className="truncate tabular-nums text-slate-200" title={render(value)}>
            {render(value)}
          </div>
        </div>
      ))}
    </div>
  );
}

function render(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "object") return JSON.stringify(value);
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(2);
  return String(value);
}
