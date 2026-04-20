"use client";

import { useEffect, useState } from "react";
import { API_BASE } from "@/lib/constants";
import { formatPrice } from "@/lib/format";
import type {
  ExecutionPlan,
  ExecutionPlanResponse,
  TrafficLight,
} from "@/lib/types";

/**
 * D06 · 小白模式 ExecutionPlanCard
 *
 * 职责：
 *   - 把数学引擎 L4 的 ExecutionPlan 用一张卡片呈现给用户
 *   - 红绿灯 + tier + action 文案 + 核心交易参数 + 一句话解释
 *   - 触发 SafetyGate 时叠加警告条
 *
 * 数据：
 *   - REST 轮询 /api/execution-plan/{coin}（ExecutionPlan 在后端 _recompute 更新）
 *   - 默认 6s 轮询，与后端 recompute 节奏匹配
 */

const POLL_INTERVAL_MS = 6000;

const LIGHT_STYLE: Record<TrafficLight, { chip: string; ring: string; text: string; label: string }> = {
  green: {
    chip: "bg-green-500/20 text-green-300",
    ring: "border-green-500/40",
    text: "text-green-300",
    label: "🟢 可执行",
  },
  yellow: {
    chip: "bg-yellow-500/20 text-yellow-300",
    ring: "border-yellow-500/40",
    text: "text-yellow-300",
    label: "🟡 谨慎",
  },
  orange: {
    chip: "bg-orange-500/20 text-orange-300",
    ring: "border-orange-500/40",
    text: "text-orange-300",
    label: "🟠 减仓/观望",
  },
  red: {
    chip: "bg-red-500/20 text-red-300",
    ring: "border-red-500/40",
    text: "text-red-300",
    label: "🔴 回避",
  },
  gray: {
    chip: "bg-slate-600/20 text-slate-400",
    ring: "border-slate-600",
    text: "text-slate-400",
    label: "⚪ 数据中",
  },
};

const TIER_CHIP: Record<string, string> = {
  S: "bg-amber-500/25 text-amber-300",
  A: "bg-red-500/20 text-red-300",
  B: "bg-blue-500/20 text-blue-300",
  C: "bg-slate-500/20 text-slate-300",
};

const ACTION_CN: Record<string, string> = {
  long: "做多",
  short: "做空",
  wait: "观望",
  avoid: "回避",
};

const REGIME_CN: Record<string, string> = {
  trend_up: "上升趋势",
  trend_down: "下降趋势",
  range: "箱体震荡",
  squeeze: "蓄力收敛",
  high_vol_chop: "高波无序",
  extreme: "极端波动",
};

const SOURCE_CN: Record<string, string> = {
  tracker_v2: "关键位",
  range_signal: "均线箱体",
  levels: "狙击位",
  news_event: "新闻",
  geo_risk: "地缘",
};

function shortSources(sources: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const s of sources) {
    const prefix = s.split(".")[0] ?? s;
    if (!seen.has(prefix)) {
      seen.add(prefix);
      out.push(SOURCE_CN[prefix] ?? prefix);
    }
  }
  return out;
}

export default function ExecutionPlanCard({ coin }: { coin: string }) {
  const [plan, setPlan] = useState<ExecutionPlan | null>(null);
  const [ready, setReady] = useState<boolean>(false);
  const [lastErr, setLastErr] = useState<string>("");

  useEffect(() => {
    let cancelled = false;

    const fetchOnce = async () => {
      try {
        const r = await fetch(`${API_BASE}/api/execution-plan/${coin}`);
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const j: ExecutionPlanResponse = await r.json();
        if (cancelled) return;
        setReady(Boolean(j.ready));
        setPlan(j.plan ?? null);
        setLastErr("");
      } catch (e) {
        if (cancelled) return;
        setLastErr(e instanceof Error ? e.message : "fetch error");
      }
    };

    fetchOnce();
    const t = setInterval(fetchOnce, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [coin]);

  if (!ready || !plan) {
    return (
      <div className="bg-slate-800/40 border border-slate-700/60 rounded-lg px-4 py-3 text-xs text-slate-500">
        <div className="flex items-center justify-between">
          <span className="text-slate-400">🚦 执行计划</span>
          <span>{lastErr ? `数据源异常：${lastErr}` : "等待首轮计算..."}</span>
        </div>
      </div>
    );
  }

  const light = LIGHT_STYLE[plan.traffic_light] ?? LIGHT_STYLE.gray;
  const tierChip = TIER_CHIP[plan.tier_hint] ?? TIER_CHIP.C;
  const actionCn = ACTION_CN[plan.action] ?? plan.action;
  const regimeCn = REGIME_CN[plan.regime] ?? plan.regime;
  const srcBadges = shortSources(plan.corroborating_sources ?? []);
  const safetyTriggered = plan.safety_gates?.triggered;

  return (
    <div
      className={`bg-slate-800/70 border-2 ${light.ring} rounded-xl p-4 space-y-3 shadow-lg`}
      data-testid="execution-plan-card"
    >
      {/* ── 顶部：红绿灯 + tier + action + 分数 ── */}
      <div className="flex items-center justify-between gap-2 flex-wrap">
        <div className="flex items-center gap-2">
          <span className={`px-2 py-0.5 rounded text-[11px] font-bold ${light.chip}`}>
            {light.label}
          </span>
          <span className={`px-2 py-0.5 rounded text-[11px] font-bold ${tierChip}`}>
            {plan.tier_hint} 级
          </span>
          <span className={`font-semibold text-sm ${light.text}`}>{actionCn}</span>
          <span className="text-[11px] text-slate-400">· {regimeCn}</span>
        </div>
        <div className="flex items-center gap-3">
          <span className="text-[11px] text-slate-500">分数</span>
          <span className={`font-mono text-lg font-bold ${light.text}`}>
            {plan.execution_score.toFixed(1)}
          </span>
        </div>
      </div>

      {/* ── 中段：入场/止损/止盈/RR/仓位 ── */}
      <div className="grid grid-cols-2 sm:grid-cols-5 gap-3 text-xs">
        <MetricTile label="入场区间" value={fmtZone(plan.entry_zone_low, plan.entry_zone_high, coin)} />
        <MetricTile
          label="止损"
          value={plan.stop_loss != null ? formatPrice(plan.stop_loss, coin) : "-"}
          accent="text-red-300"
        />
        <MetricTile
          label="止盈 1"
          value={plan.tp1 != null ? formatPrice(plan.tp1, coin) : "-"}
          accent="text-green-300"
        />
        <MetricTile
          label="盈亏比"
          value={plan.rr_ratio != null ? `1:${plan.rr_ratio.toFixed(2)}` : "-"}
          accent={plan.rr_ratio && plan.rr_ratio >= 2 ? "text-green-300" : "text-slate-300"}
        />
        <MetricTile
          label="建议仓位"
          value={
            plan.position_size_pct != null ? `${plan.position_size_pct.toFixed(0)}%` : "-"
          }
          accent={
            plan.position_size_pct && plan.position_size_pct >= 30
              ? "text-green-300"
              : plan.position_size_pct === 0
                ? "text-red-300"
                : "text-slate-300"
          }
        />
      </div>

      {/* ── 一句话解释 ── */}
      {plan.one_liner && (
        <div className="text-xs text-slate-300 bg-slate-900/40 border border-slate-700/40 rounded px-3 py-2">
          {plan.one_liner}
        </div>
      )}

      {/* ── SafetyGate 警告条 ── */}
      {safetyTriggered && (
        <SafetyBanner plan={plan} />
      )}

      {/* ── 底部：贡献源 + 回测样本 ── */}
      <div className="flex items-center justify-between text-[11px] text-slate-500 pt-2 border-t border-slate-700/50">
        <div className="flex items-center gap-1.5 flex-wrap">
          <span>来源：</span>
          {srcBadges.length > 0 ? (
            srcBadges.map((s) => (
              <span
                key={s}
                className="px-1.5 py-0.5 rounded bg-slate-700/70 text-slate-300 text-[10px]"
              >
                {s}
              </span>
            ))
          ) : (
            <span>（仅观望 · 无贡献源）</span>
          )}
        </div>
        <div className="flex items-center gap-3">
          {plan.historical_win_rate != null && plan.historical_sample_size >= 10 && (
            <span>
              历史胜率 {(plan.historical_win_rate * 100).toFixed(0)}%
              <span className="text-slate-600 ml-1">
                ({plan.historical_sample_size})
              </span>
            </span>
          )}
          <span className="text-slate-600">
            {new Date(plan.ts * 1000).toLocaleTimeString()}
          </span>
        </div>
      </div>
    </div>
  );
}

function MetricTile({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: string;
}) {
  return (
    <div className="bg-slate-900/30 rounded px-2 py-1.5 border border-slate-700/40">
      <div className="text-[10px] text-slate-500">{label}</div>
      <div className={`font-mono text-sm mt-0.5 ${accent ?? "text-slate-200"}`}>
        {value}
      </div>
    </div>
  );
}

function SafetyBanner({ plan }: { plan: ExecutionPlan }) {
  const hasBlock =
    plan.safety_gates.g1_extreme_vol === "block" ||
    plan.safety_gates.g2_macro_event === "block" ||
    plan.safety_gates.g3_liq_chaos === "block" ||
    plan.safety_gates.g4_api_degrade === "block" ||
    plan.safety_gates.g5_blackswan === "block";

  const color = hasBlock
    ? "bg-red-500/15 border-red-500/40 text-red-300"
    : "bg-yellow-500/15 border-yellow-500/40 text-yellow-300";

  const icon = hasBlock ? "🛑" : "⚠️";

  return (
    <div className={`rounded border ${color} px-3 py-2 text-xs`}>
      <div className="font-semibold mb-1">
        {icon} 安全护栏 {hasBlock ? "熔断" : "警告"}
      </div>
      <ul className="list-disc list-inside space-y-0.5 text-[11px] leading-relaxed">
        {plan.safety_gates.warnings.slice(0, 3).map((w, i) => (
          <li key={i}>{w}</li>
        ))}
        {plan.safety_gates.block_reason &&
          !plan.safety_gates.warnings.includes(plan.safety_gates.block_reason) && (
            <li>{plan.safety_gates.block_reason}</li>
          )}
      </ul>
    </div>
  );
}

function fmtZone(
  low: number | null,
  high: number | null,
  coin: string,
): string {
  if (low == null && high == null) return "-";
  if (low != null && high != null && Math.abs(low - high) < 1e-6) {
    return formatPrice(low, coin);
  }
  return `${low != null ? formatPrice(low, coin) : "?"} ~ ${high != null ? formatPrice(high, coin) : "?"}`;
}
