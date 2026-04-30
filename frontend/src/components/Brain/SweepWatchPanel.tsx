/**
 * W4-T1 阶段 4 · SweepWatchPanel
 *
 * 把后端 sweep_watch 的双侧 5 态机 + 3 派生分用直观面板展示。
 * 不改 BrainPriceZone / OpportunityBoard 任何字段，纯 UI 编排。
 *
 * 配套：
 *   - SweepWatchTracePanel（抽屉）：展示算法运行轨迹（点"运行轨迹"按钮触发）
 *   - 后端 trace 已落盘到 data/sweep_watch/{coin}/{date}.jsonl 供后验
 */
"use client";

import { useState } from "react";
import type {
  BrainSweepWatch,
  SweepPhase,
  SweepWatchSide,
} from "@/lib/types";
import { formatPrice } from "@/lib/format";
import SweepWatchTracePanel from "./SweepWatchTracePanel";

interface Props {
  coin: string;
  sweepWatch: BrainSweepWatch | null | undefined;
  /** 联动：选中代表 zone 时高亮 PriceAxisMap / ZoneDetailCard。 */
  onSelectZone?: (zoneId: string) => void;
}

// ─────────────────────────────────────────────────────────────────────
// 5 态机展示映射（颜色 + 中文 + 脉冲）
// ─────────────────────────────────────────────────────────────────────
const PHASE_META: Record<SweepPhase, {
  label: string;
  border: string;
  bg: string;
  text: string;
  dot: string;
  pulse: boolean;
  meaning: string;
}> = {
  waiting: {
    label: "⚪ 等待",
    border: "border-slate-600/50",
    bg: "bg-slate-900/50",
    text: "text-slate-300",
    dot: "bg-slate-400",
    pulse: false,
    meaning: "距止损带 > 1.5%，远端观察；尚不具备触发条件",
  },
  approaching: {
    label: "🟡 接近",
    border: "border-amber-600/60",
    bg: "bg-amber-950/30",
    text: "text-amber-200",
    dot: "bg-amber-400",
    pulse: false,
    meaning: "距止损带 ≤ 1.5%，但尚未发生扫单；保持观察、准备策略",
  },
  in_sweep: {
    label: "🔴 扫单进行",
    border: "border-rose-500",
    bg: "bg-rose-950/40",
    text: "text-rose-200",
    dot: "bg-rose-400",
    pulse: true,
    meaning: "最近 5min 有墙被消耗 / 价格穿破区间；正在被扫，等结构反应",
  },
  swept_reclaiming: {
    label: "🟢 已扫·收回",
    border: "border-emerald-500",
    bg: "bg-emerald-950/35",
    text: "text-emerald-200",
    dot: "bg-emerald-400",
    pulse: false,
    meaning: "扫单后价格已回到区间内 — 反转候选；关注是否站稳 ≥ 10min",
  },
  swept_continuing: {
    label: "🟠 已扫·延续",
    border: "border-orange-500",
    bg: "bg-orange-950/35",
    text: "text-orange-200",
    dot: "bg-orange-400",
    pulse: false,
    meaning: "扫单后未收回 + CVD 同向 — 高概率级联，等下一支撑/阻力 zone",
  },
};

// ─────────────────────────────────────────────────────────────────────
// 白话总结派生（A 项：前端纯展示派生，不依赖后端字段）
//
// 输出格式：「{phase 中文} · {RP/CR 失衡} → {行动建议}」
// 设计原则：1 行 ≤ 60 字；不出现交易指令性词汇；只描述"现状 + 关注什么"
// ─────────────────────────────────────────────────────────────────────
const RP_CR_BALANCED_DELTA = 0.15;
/** RP / CR 差距 < 此阈值 视为对称（balanced），不偏向反弹/延续。 */

const RP_CR_DOMINANT_DELTA = 0.3;
/** RP / CR 差距 ≥ 此阈值 用 ≫ 强符号；介于 balanced 和此之间用 > 弱符号。 */

const SA_HOT_TAG = 0.7;
const SA_COLD_TAG = 0.3;

function scoreCompare(rp: number, cr: number): {
  bias: "rebound" | "continuation" | "balanced";
  text: string;
} {
  const delta = rp - cr;
  if (Math.abs(delta) < RP_CR_BALANCED_DELTA) {
    return { bias: "balanced", text: `反弹 ${rp.toFixed(2)} ≈ 延续 ${cr.toFixed(2)}` };
  }
  if (delta > 0) {
    const op = delta >= RP_CR_DOMINANT_DELTA ? "≫" : ">";
    return { bias: "rebound", text: `反弹 ${rp.toFixed(2)} ${op} 延续 ${cr.toFixed(2)}` };
  }
  const op = -delta >= RP_CR_DOMINANT_DELTA ? "≫" : ">";
  return { bias: "continuation", text: `延续 ${cr.toFixed(2)} ${op} 反弹 ${rp.toFixed(2)}` };
}

function saTag(sa: number): string {
  if (sa >= SA_HOT_TAG) return "高招扫";
  if (sa < SA_COLD_TAG) return "低招扫，价格未必到";
  return "";
}

function actionAdvice(
  phase: SweepPhase,
  bias: "rebound" | "continuation" | "balanced",
  isBelow: boolean,
): string {
  if (phase === "waiting") return "远端观察，无需立即动作";
  if (phase === "approaching") {
    if (bias === "rebound") {
      return isBelow
        ? "扫到大概率假摔反弹，关注 5min 速回 + 现货买墙不撤"
        : "突破多半被压回，关注 5min 速回 + 现货卖墙不撤";
    }
    if (bias === "continuation") {
      return isBelow
        ? "扫到大概率继续杀，不建议提前接"
        : "突破多半延续上行，等下一档阻力";
    }
    return "反弹/延续概率相当，等扫单触发再判断";
  }
  if (phase === "in_sweep") {
    return isBelow
      ? "正在被扫，等 5min 内能否收回区间 + 现货买墙是否撤"
      : "正在被突破，等 5min 内能否跌回区间 + 现货卖墙是否撤";
  }
  if (phase === "swept_reclaiming") {
    return isBelow
      ? "已收回区间，反转候选；关注能否站稳 ≥ 10min"
      : "已跌回区间，反转候选；关注能否站稳 ≥ 10min";
  }
  if (phase === "swept_continuing") {
    return isBelow
      ? "未收回 + CVD 同向，等下一档支撑或 CVD 衰竭"
      : "未跌回 + CVD 同向，等下一档阻力或 CVD 衰竭";
  }
  return "";
}

function buildNarrative(side: SweepWatchSide): string {
  const phaseLabel = PHASE_META[side.sweep_phase].label;
  const { bias, text: scoreText } = scoreCompare(
    side.reversal_potential ?? 0,
    side.continuation_risk ?? 0,
  );
  const tag = saTag(side.sweep_attractiveness ?? 0);
  const action = actionAdvice(side.sweep_phase, bias, side.direction === "below");
  return `${phaseLabel} · ${scoreText}${tag ? `（${tag}）` : ""} → ${action}`;
}

// ─────────────────────────────────────────────────────────────────────
// ScoreBar（复刻 ZoneDetailCard 风格；独立写以保持组件独立）
// ─────────────────────────────────────────────────────────────────────
type ScoreKind = "trust" | "risk";

function trustColor(v: number) {
  if (v >= 0.7) return { bar: "bg-emerald-500/80", text: "text-emerald-300", level: "强" };
  if (v >= 0.4) return { bar: "bg-amber-500/80", text: "text-amber-300", level: "中" };
  return { bar: "bg-rose-500/70", text: "text-rose-300", level: "弱" };
}

function riskColor(v: number) {
  if (v >= 0.7) return { bar: "bg-rose-500/80", text: "text-rose-300", level: "高" };
  if (v >= 0.4) return { bar: "bg-amber-500/80", text: "text-amber-300", level: "中" };
  return { bar: "bg-emerald-500/70", text: "text-emerald-300", level: "低" };
}

function ScoreBar({
  label, value, kind, hint,
}: {
  label: string; value: number; kind: ScoreKind; hint?: string;
}) {
  const v = Math.max(0, Math.min(1, value));
  const pct = v * 100;
  const c = kind === "trust" ? trustColor(v) : riskColor(v);
  const tip = hint
    ? `${hint}\n当前 ${v.toFixed(2)} (${c.level})`
    : `当前 ${v.toFixed(2)} (${c.level})`;
  return (
    <div title={tip}>
      <div className="flex items-baseline justify-between text-[10px]">
        <span className="text-slate-400">{label}</span>
        <span className={`tabular-nums ${c.text}`}>
          {v.toFixed(2)}
          <span className="ml-1 text-[9px] text-slate-500">{c.level}</span>
        </span>
      </div>
      <div className="mt-0.5 h-1 w-full overflow-hidden rounded bg-slate-800">
        <div className={`h-full ${c.bar}`} style={{ width: `${pct}%` }} />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 距离脉冲条（直观感受"还差多少 / 是否已扫破"）
// ─────────────────────────────────────────────────────────────────────
function DistancePulse({ side }: { side: SweepWatchSide }) {
  // 距离绝对值在 [0, 3]% 区间映射到 [0, 100]%；扫破后距离会变正/反向，颜色切换
  const abs = Math.min(3, Math.abs(side.distance_pct));
  const pct = (1 - abs / 3) * 100;
  const isSwept = side.sweep_phase === "in_sweep"
    || side.sweep_phase === "swept_reclaiming"
    || side.sweep_phase === "swept_continuing";
  const color = isSwept ? "bg-rose-400/80" : abs <= 1.5 ? "bg-amber-400/80" : "bg-slate-500/60";
  return (
    <div className="space-y-0.5">
      <div className="flex items-center justify-between text-[10px] text-slate-500">
        <span>距离</span>
        <span className="tabular-nums text-slate-300">
          {side.distance_pct >= 0 ? "+" : ""}{side.distance_pct.toFixed(2)}%
        </span>
      </div>
      <div className="h-1.5 w-full overflow-hidden rounded bg-slate-800">
        <div
          className={`h-full transition-all duration-300 ${color}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 单侧卡片
// ─────────────────────────────────────────────────────────────────────
function SideCard({
  coin, side, onSelectZone,
}: {
  coin: string; side: SweepWatchSide; onSelectZone?: (zoneId: string) => void;
}) {
  const meta = PHASE_META[side.sweep_phase];
  return (
    <div className={`flex flex-col gap-2.5 rounded-md border ${meta.border} ${meta.bg} p-3`}>
      {/* 头部：标题 + phase chip */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex flex-col">
          <span className="text-[12px] font-medium text-slate-200">{side.label}</span>
          {side.representative_zone_label && (
            <button
              type="button"
              onClick={() => onSelectZone?.(side.representative_zone_id)}
              className="text-left text-[10px] text-slate-400 hover:text-sky-300"
              title="点击在 PriceAxisMap / ZoneDetailCard 中高亮该区"
            >
              代表区：{side.representative_zone_label} →
            </button>
          )}
        </div>
        <div
          className={`flex items-center gap-1.5 rounded border px-2 py-1 text-[11px] font-semibold ${meta.border} ${meta.text}`}
          title={meta.meaning}
        >
          <span className={`h-2 w-2 rounded-full ${meta.dot} ${meta.pulse ? "animate-pulse" : ""}`} />
          {meta.label}
        </div>
      </div>

      {/* 价格区间 + 距离脉冲 */}
      <div className="space-y-1.5 rounded bg-slate-950/40 p-2">
        <div className="flex items-baseline justify-between text-[11px]">
          <span className="text-slate-500">区间</span>
          <span className="tabular-nums text-slate-200">
            {formatPrice(side.price_band[0], coin)} – {formatPrice(side.price_band[1], coin)}
          </span>
        </div>
        <DistancePulse side={side} />
      </div>

      {/* 3 派生分 */}
      <div className="grid grid-cols-1 gap-1.5">
        <ScoreBar
          label="扫单吸引"
          value={side.sweep_attractiveness}
          kind="risk"
          hint="OI / 清算密度 / 杠杆挂单越大越吸引扫单"
        />
        <ScoreBar
          label="反转潜力"
          value={side.reversal_potential}
          kind="trust"
          hint="支撑/阻力强度 + CVD 反向 + 数据可信度的综合得分"
        />
        <ScoreBar
          label="延续风险"
          value={side.continuation_risk}
          kind="risk"
          hint="打穿风险 + 扫单吸引 + CVD 同向 + 脆性的综合得分；高 = 不建议提前接"
        />
      </div>

      {/* 触发观察 */}
      {side.triggers.length > 0 && (
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">触发观察</div>
          <ul className="space-y-0.5 text-[11px] text-slate-300">
            {side.triggers.map((t, i) => (
              <li key={i} className="flex gap-1.5 leading-snug">
                <span className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full bg-sky-400/60" />
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 失效条件 */}
      {side.invalidations.length > 0 && (
        <div>
          <div className="mb-1 text-[10px] uppercase tracking-wider text-slate-500">失效条件</div>
          <ul className="space-y-0.5 text-[11px] text-rose-300/90">
            {side.invalidations.map((t, i) => (
              <li key={i} className="flex gap-1.5 leading-snug">
                <span className="mt-1 inline-block h-1 w-1 shrink-0 rounded-full bg-rose-400/60" />
                <span>{t}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* 白话总结：现状一句话 + 行动建议（A 项） */}
      <div
        className="mt-1 rounded border-l-2 border-sky-700/60 bg-slate-950/40 px-2 py-1.5"
        title="基于 phase + 反转潜力/延续风险/扫单吸引 派生的人类可读总结，不构成交易指令"
      >
        <div className="mb-0.5 text-[9px] uppercase tracking-wider text-slate-500">
          一句话
        </div>
        <div className="text-[11px] leading-relaxed text-slate-200">
          {buildNarrative(side)}
        </div>
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────────────
// 主面板
// ─────────────────────────────────────────────────────────────────────
export default function SweepWatchPanel({ coin, sweepWatch, onSelectZone }: Props) {
  const [traceOpen, setTraceOpen] = useState(false);

  if (!sweepWatch) {
    return (
      <div className="rounded border border-slate-800 bg-slate-900/40 p-3 text-[11px] text-slate-500">
        ⚪ 扫单观察 · 数据未就绪
      </div>
    );
  }

  const { below, above, trace_log } = sweepWatch;
  const traceCount = trace_log?.length ?? 0;

  return (
    <>
      <div className="rounded-md border border-slate-800 bg-slate-900/30 p-3">
        {/* 顶部标题栏 */}
        <div className="mb-2.5 flex items-center justify-between">
          <div className="flex items-center gap-2">
            <span className="text-[13px] font-semibold text-slate-200">扫单观察</span>
            <span className="text-[10px] text-slate-500">
              双向止损带 · 5 态机 · 3 派生分
            </span>
          </div>
          <button
            type="button"
            onClick={() => setTraceOpen(true)}
            className="rounded border border-slate-700 bg-slate-800/60 px-2.5 py-1 text-[10px] text-slate-300 hover:border-sky-700 hover:text-sky-300"
            title="查看本次构建的算法运行轨迹（trace）"
          >
            运行轨迹 ({traceCount})
          </button>
        </div>

        {/* 双列：below / above */}
        <div className="grid grid-cols-1 gap-2.5 md:grid-cols-2">
          {below ? (
            <SideCard coin={coin} side={below} onSelectZone={onSelectZone} />
          ) : (
            <div className="flex items-center justify-center rounded border border-dashed border-slate-700/60 bg-slate-900/30 p-4 text-[11px] text-slate-500">
              下方无强角色 zone（数据不足或全部为 other）
            </div>
          )}
          {above ? (
            <SideCard coin={coin} side={above} onSelectZone={onSelectZone} />
          ) : (
            <div className="flex items-center justify-center rounded border border-dashed border-slate-700/60 bg-slate-900/30 p-4 text-[11px] text-slate-500">
              上方无强角色 zone（数据不足或全部为 other）
            </div>
          )}
        </div>
      </div>

      <SweepWatchTracePanel
        open={traceOpen}
        onClose={() => setTraceOpen(false)}
        sweepWatch={sweepWatch}
      />
    </>
  );
}
