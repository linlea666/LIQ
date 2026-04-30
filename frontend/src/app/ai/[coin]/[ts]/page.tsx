"use client";

import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import Link from "next/link";
import { API_BASE } from "@/lib/constants";
import type { StrategicReport, StrategicTradingPlan } from "@/lib/types";

const DECISION_CN: Record<string, string> = {
  WAIT: "等待",
  LONG_OBSERVATION: "看多观察",
  SHORT_OBSERVATION: "看空观察",
  LONG_PLAN: "多头计划",
  SHORT_PLAN: "空头计划",
  NO_TRADE: "禁止交易",
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

function formatStaleSec(sec: number): string {
  if (sec < 0) return "—";
  if (sec < 60) return `${sec}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 86400) {
    const h = Math.floor(sec / 3600);
    const m = Math.round((sec % 3600) / 60);
    return m > 0 ? `${h}h${m}m` : `${h}h`;
  }
  return `${Math.floor(sec / 86400)}d`;
}

function copyToClipboard(text: string): Promise<void> {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  try {
    document.execCommand("copy");
    return Promise.resolve();
  } catch {
    return Promise.reject(new Error("copy failed"));
  } finally {
    document.body.removeChild(textarea);
  }
}

export default function StrategicDetailPage() {
  const params = useParams();
  const coin = (params.coin as string)?.toUpperCase() ?? "BTC";
  const ts = Number(params.ts);

  const [data, setData] = useState<StrategicReport | null>(null);
  const [error, setError] = useState("");
  const [copyLabel, setCopyLabel] = useState("📋 复制 JSON");

  useEffect(() => {
    if (!ts) return;
    fetch(`${API_BASE}/api/strategic/report/${encodeURIComponent(coin)}/${ts}`)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.json();
      })
      .then(setData)
      .catch((e: Error) => setError(`加载失败: ${e.message}`));
  }, [coin, ts]);

  if (error) {
    return (
      <div className="min-h-screen bg-slate-950 flex items-center justify-center">
        <div className="text-center">
          <div className="text-red-400 text-lg mb-4">{error}</div>
          <Link href="/" className="text-blue-400 hover:text-blue-300 text-sm">← 返回大屏</Link>
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

  const label = DECISION_CN[data.decision] ?? data.decision;
  const confPct = Math.round((data.confidence ?? 0) * 100);
  const pd = data.prompt_debug;

  return (
    <div className="min-h-screen bg-slate-950 text-slate-300">
      <header className="border-b border-slate-700 bg-slate-900/80 sticky top-0 z-10">
        <div className="max-w-4xl mx-auto px-6 py-4 flex items-center justify-between gap-4">
          <div className="flex items-center gap-4 min-w-0">
            <Link href="/" className="text-blue-400 hover:text-blue-300 text-sm shrink-0">← 返回大屏</Link>
            <div className="min-w-0">
              <h1 className="text-lg font-bold text-white truncate">🛡️ {coin} Strategic 报告</h1>
              <div className="text-xs text-slate-500 mt-0.5">
                {formatFullTime(data.timestamp)}
                {data.stale_sec != null && data.stale_sec >= 0 && (
                  <span> · 数据龄 {formatStaleSec(data.stale_sec)}</span>
                )}
              </div>
            </div>
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <span className="bg-slate-800 text-white text-xs font-bold px-3 py-1 rounded-full border border-slate-600">
              {label}
            </span>
            <span className="text-xs text-amber-300">{confPct}%</span>
            <button
              type="button"
              onClick={() => {
                copyToClipboard(JSON.stringify(data, null, 2))
                  .then(() => {
                    setCopyLabel("✅ 已复制");
                    setTimeout(() => setCopyLabel("📋 复制 JSON"), 1500);
                  })
                  .catch(() => {
                    setCopyLabel("❌ 失败");
                    setTimeout(() => setCopyLabel("📋 复制 JSON"), 1500);
                  });
              }}
              className="px-3 py-1 text-xs border border-slate-600 rounded hover:border-slate-400 hover:text-white transition"
            >
              {copyLabel}
            </button>
          </div>
        </div>
      </header>

      <main className="max-w-4xl mx-auto px-6 py-6 space-y-6">
        <div className="flex flex-wrap gap-2 text-xs text-slate-500">
          <span>horizon: <span className="text-slate-300">{data.horizon}</span></span>
          <span>bias: <span className="text-slate-300">{data.bias}</span></span>
          <span>data_quality: <span className="text-slate-300">{data.data_quality}</span></span>
        </div>

        {(data.confidence_rationale || "").trim() && (
          <Card title="置信说明">
            <p className="whitespace-pre-wrap">{data.confidence_rationale}</p>
          </Card>
        )}

        {((data.market_phase || "") + (data.cycle_position || "")).trim() && (
          <Card title="阶段与周期">
            {data.market_phase && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs">市场阶段</span>
                <p className="text-white mt-0.5">{data.market_phase}</p>
              </div>
            )}
            {data.cycle_position && (
              <div>
                <span className="text-slate-500 text-xs">周期位置</span>
                <p className="text-white mt-0.5">{data.cycle_position}</p>
              </div>
            )}
          </Card>
        )}

        {data.current_zone_assessment &&
          Object.values(data.current_zone_assessment).some((v) => v !== "" && v != null) && (
          <CurrentZoneCard zone={data.current_zone_assessment} />
        )}

        {(["structure_analysis", "flow_analysis", "macro_context"] as const).map((key) => {
          const text = (data[key] || "").trim();
          if (!text) return null;
          const titles: Record<string, string> = {
            structure_analysis: "结构",
            flow_analysis: "流量",
            macro_context: "宏观",
          };
          return (
            <Card key={key} title={titles[key]}>
              <p className="whitespace-pre-wrap leading-relaxed">{text}</p>
            </Card>
          );
        })}

        {data.primary_plan && (
          <TradingPlanCard title="主计划" plan={data.primary_plan} />
        )}
        {data.alternative_plan && (
          <TradingPlanCard title="备选计划" plan={data.alternative_plan} />
        )}

        {data.no_trade_conditions && data.no_trade_conditions.length > 0 && (
          <Card title="等待 / 不开仓条件">
            <ul className="list-disc pl-5 space-y-1">
              {data.no_trade_conditions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </Card>
        )}

        {data.alternative_scenario && (
          <Card title="对立场景（强制）">
            <p className="text-white font-medium">{data.alternative_scenario.description}</p>
            <p className="text-xs text-slate-500 mt-2">
              概率 {data.alternative_scenario.probability_pct}% · 触发: {data.alternative_scenario.trigger}
            </p>
          </Card>
        )}

        {data.evidence_matrix && (
          <Card title="冲突矩阵（摘要）">
            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2 text-xs">
              <div>多头证据: {data.evidence_matrix.long_evidence?.length ?? 0}</div>
              <div>空头证据: {data.evidence_matrix.short_evidence?.length ?? 0}</div>
              <div>等待证据: {data.evidence_matrix.wait_evidence?.length ?? 0}</div>
            </div>
            {data.evidence_matrix.contradictions &&
              data.evidence_matrix.contradictions.length > 0 && (
              <ul className="mt-3 list-disc pl-5 text-amber-300/90 space-y-1">
                {data.evidence_matrix.contradictions.map((x, i) => (
                  <li key={i}>{x}</li>
                ))}
              </ul>
            )}
          </Card>
        )}

        {data.invalidation_conditions && data.invalidation_conditions.length > 0 && (
          <Card title="计划级失效">
            <ul className="list-disc pl-5 space-y-1">
              {data.invalidation_conditions.map((c, i) => (
                <li key={i}>{c}</li>
              ))}
            </ul>
          </Card>
        )}

        {data.data_self_check && (
          <DataSelfCheckCard check={data.data_self_check} />
        )}

        {(data.macro_modifier_note || "").trim() && (
          <Card title="宏观修正">
            <p className="whitespace-pre-wrap">{data.macro_modifier_note}</p>
          </Card>
        )}

        {pd && typeof pd === "object" && (
          <PromptDebugCard debug={pd as Record<string, unknown>} />
        )}

        <div className="text-center text-xs text-slate-600 py-6 border-t border-slate-800">
          LIQ · Strategic 报告 · {formatFullTime(data.timestamp)}
        </div>
      </main>
    </div>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-slate-900/80 border border-slate-700/50 rounded-xl overflow-hidden">
      <div className="px-5 py-3 border-b border-slate-700/50 bg-slate-800/30">
        <h2 className="text-sm font-semibold text-white">{title}</h2>
      </div>
      <div className="px-5 py-4 text-sm text-slate-400 leading-relaxed">{children}</div>
    </div>
  );
}

function TradingPlanCard({ title, plan }: { title: string; plan: StrategicTradingPlan }) {
  return (
    <Card title={title}>
      <div className="space-y-3">
        <div className="text-white font-medium">{plan.setup_type}</div>
        <div className="text-xs font-mono text-slate-300">
          入场 {plan.entry_zone_low} – {plan.entry_zone_high}
        </div>
        <div>
          <span className="text-slate-500 text-xs">硬失效</span>
          <p className=" mt-1">{plan.hard_invalidation}</p>
        </div>
        {plan.trigger_conditions && plan.trigger_conditions.length > 0 && (
          <div>
            <span className="text-slate-500 text-xs">触发条件</span>
            <ul className="list-disc pl-5 mt-1 space-y-0.5">
              {plan.trigger_conditions.map((t, i) => (
                <li key={i}>{t}</li>
              ))}
            </ul>
          </div>
        )}
        {plan.targets && plan.targets.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-xs border-collapse">
              <thead>
                <tr className="text-slate-500 border-b border-slate-700">
                  <th className="text-left py-1 pr-2">目标价</th>
                  <th className="text-left py-1 pr-2">理由</th>
                  <th className="text-right py-1">RR</th>
                </tr>
              </thead>
              <tbody>
                {plan.targets.map((t, i) => (
                  <tr key={i} className="border-b border-slate-800/80">
                    <td className="py-1.5 pr-2 text-white font-mono">{t.price}</td>
                    <td className="py-1.5 pr-2">{t.reason}</td>
                    <td className="py-1.5 text-right text-amber-400">{t.rr}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </Card>
  );
}

type CurrentZoneAssessment = NonNullable<StrategicReport["current_zone_assessment"]>;
type DataSelfCheck = NonNullable<StrategicReport["data_self_check"]>;

const ZONE_ROLE_CN: Record<string, string> = {
  spot_defense: "现货防守位",
  futures_target: "合约目标位",
  liquidation_magnet: "清算磁铁",
  contested: "争夺区",
  key_level_only: "关键位",
  mid_air: "中间位置",
  other: "其它",
};

function CurrentZoneCard({ zone }: { zone: CurrentZoneAssessment }) {
  const roleLabel = zone.role ? (ZONE_ROLE_CN[zone.role] ?? zone.role) : "—";
  const above = zone.nearest_critical_above_pct;
  const below = zone.nearest_critical_below_pct;
  return (
    <Card title="当前区位">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 text-sm">
        {zone.zone_id && (
          <div>
            <span className="text-slate-500 text-xs">Zone ID</span>
            <p className="text-white font-mono mt-0.5 text-xs">{zone.zone_id}</p>
          </div>
        )}
        {zone.role && (
          <div>
            <span className="text-slate-500 text-xs">主导角色</span>
            <p className="text-white mt-0.5">{roleLabel}</p>
          </div>
        )}
        {above != null && (
          <div>
            <span className="text-slate-500 text-xs">距上方关键区</span>
            <p className="text-white font-mono mt-0.5">{above.toFixed(2)}%</p>
          </div>
        )}
        {below != null && (
          <div>
            <span className="text-slate-500 text-xs">距下方关键区</span>
            <p className="text-white font-mono mt-0.5">{below.toFixed(2)}%</p>
          </div>
        )}
      </div>
      {zone.key_conflict && (
        <div className="mt-4 pt-3 border-t border-slate-800/50">
          <span className="text-slate-500 text-xs">关键冲突</span>
          <p className="text-amber-300/90 mt-1">{zone.key_conflict}</p>
        </div>
      )}
    </Card>
  );
}

function DataSelfCheckCard({ check }: { check: DataSelfCheck }) {
  const missing = check.missing ?? [];
  const stale = check.stale ?? [];
  const provisional = check.provisional ?? [];
  return (
    <Card title="数据自检">
      <div className="space-y-3">
        {check.hard_stop_triggered && (
          <div className="px-3 py-2 bg-red-950/40 border border-red-800/50 rounded text-red-300 text-xs">
            ⚠ hard_stop_triggered = true · 数据严重不可信，已强制 NO_TRADE
          </div>
        )}
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 text-xs">
          <ChipList label="missing" items={missing} tone="rose" />
          <ChipList label="stale" items={stale} tone="amber" />
          <ChipList label="provisional" items={provisional} tone="slate" />
        </div>
        {check.confidence_penalty_reason && (
          <div className="pt-3 border-t border-slate-800/50">
            <span className="text-slate-500 text-xs">置信打折原因</span>
            <p className="text-amber-300/90 text-xs mt-1 whitespace-pre-wrap">
              {check.confidence_penalty_reason}
            </p>
          </div>
        )}
      </div>
    </Card>
  );
}

function ChipList({
  label,
  items,
  tone,
}: {
  label: string;
  items: string[];
  tone: "rose" | "amber" | "slate";
}) {
  const toneCls = {
    rose: "bg-rose-950/30 border-rose-800/40 text-rose-300/90",
    amber: "bg-amber-950/30 border-amber-800/40 text-amber-300/90",
    slate: "bg-slate-800/40 border-slate-700/40 text-slate-300/80",
  }[tone];
  return (
    <div>
      <div className="text-slate-500 mb-1.5">
        {label}（{items.length}）
      </div>
      {items.length === 0 ? (
        <span className="text-slate-600">—</span>
      ) : (
        <div className="flex flex-wrap gap-1.5">
          {items.map((it, i) => (
            <span
              key={i}
              className={`inline-block px-2 py-0.5 rounded border text-[10px] font-mono ${toneCls}`}
            >
              {it}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function PromptDebugCard({ debug }: { debug: Record<string, unknown> }) {
  const system = String(debug.system ?? "");
  const user = String(debug.user ?? "");
  const raw = String(debug.ai_raw_response ?? "");
  const parseOk = debug.parse_ok !== false;
  const parseError = debug.parse_error != null ? String(debug.parse_error) : "";

  return (
    <Card title="Prompt 透明化">
      <div className="space-y-3 text-xs">
        <div className="flex flex-wrap gap-2 text-slate-500">
          {debug.model != null && <span>model: {String(debug.model)}</span>}
          {debug.latency_ms != null && (
            <span>latency: {String(debug.latency_ms)} ms</span>
          )}
          <span className={parseOk ? "text-green-400" : "text-red-400"}>
            parse_ok: {String(parseOk)}
          </span>
        </div>
        {parseError && (
          <div className="text-red-400 whitespace-pre-wrap">{parseError}</div>
        )}
        {[
          { key: "system", label: "System", content: system },
          { key: "user", label: "User", content: user },
          { key: "raw", label: "Raw response", content: raw },
        ].map((row) => (
          <details key={row.key} className="border border-slate-800 rounded-lg bg-slate-950/40">
            <summary className="cursor-pointer px-3 py-2 text-slate-300 hover:text-white">
              {row.label} ({row.content.length} chars)
            </summary>
            <pre className="max-h-72 overflow-auto p-3 text-[10px] text-slate-500 whitespace-pre-wrap break-words border-t border-slate-800">
              {row.content || "—"}
            </pre>
          </details>
        ))}
      </div>
    </Card>
  );
}
