"use client";

/**
 * MarketActionView · Market Action Analyzer 主卡片
 *
 * 对齐 backend/models/market_action.py 的 MarketActionReport：
 *   Hero        → scenario / phase / confidence / bias / data_quality / continuity pill
 *   结论与推理  → market_conclusion / analyst_reasoning / confidence_rationale
 *   证据矩阵    → evidence_breakdown 按 supports (main/contrarian/neutral) 分三列
 *   对立视角    → alternative_scenario
 *   操作建议    → trading_implications + invalidation_conditions
 *   透明度抽屉  → PromptDebugDrawer（system/user/AI raw/AI CoT 4 tab）
 *
 * 数据源：
 *   - WebSocket `market_action_report` 实时推送（useWebSocket 订阅）
 *   - 首屏 / 切币后通过 REST /api/market-action/report 补拉（slim=0 带 prompt_debug）
 *   - 手动触发：POST /api/market-action/run（冲突 409 静默）
 */

import { useMemo, useState } from "react";
import { useMarketStore } from "@/stores/marketStore";
import type {
  MAABias,
  MAAContinuityStance,
  MAADataQuality,
  MAAEvidenceItem,
  MAAEvidenceSupports,
  MAAPhase,
  MAAScenario,
  MarketActionReport,
} from "@/lib/types";
import PromptDebugDrawer from "./PromptDebugDrawer";
import MAACalibrationPanel from "./MAACalibrationPanel";

// ─────────────────────── 文案字典 ───────────────────────

const SCENARIO_CN: Record<MAAScenario, { label: string; color: string; emoji: string }> = {
  trend_continuation_up: { label: "上行趋势延续", color: "#22c55e", emoji: "⬆" },
  trend_continuation_down: { label: "下行趋势延续", color: "#ef4444", emoji: "⬇" },
  short_squeeze_up: { label: "空头挤压上行", color: "#10b981", emoji: "🔺" },
  long_squeeze_down: { label: "多头挤压下行", color: "#f87171", emoji: "🔻" },
  fake_breakout_up: { label: "假突破上行", color: "#eab308", emoji: "⚠" },
  fake_breakdown_down: { label: "假跌破下行", color: "#eab308", emoji: "⚠" },
  exhaustion_top: { label: "顶部衰竭", color: "#f97316", emoji: "🏔" },
  exhaustion_bottom: { label: "底部衰竭", color: "#0ea5e9", emoji: "🕳" },
  range_bound: { label: "区间震荡", color: "#94a3b8", emoji: "↔" },
};

const PHASE_CN: Record<MAAPhase, string> = {
  accumulation: "吸筹",
  markup: "推升",
  distribution: "派发",
  markdown: "下跌",
  transition: "过渡",
};

const BIAS_CN: Record<MAABias, { label: string; color: string; bg: string }> = {
  long: { label: "做多", color: "#22c55e", bg: "rgba(34,197,94,0.15)" },
  short: { label: "做空", color: "#ef4444", bg: "rgba(239,68,68,0.15)" },
  neutral: { label: "中性", color: "#94a3b8", bg: "rgba(148,163,184,0.15)" },
  wait: { label: "观望", color: "#eab308", bg: "rgba(234,179,8,0.15)" },
};

const DQ_CN: Record<MAADataQuality, { label: string; color: string }> = {
  ok: { label: "数据充分", color: "#22c55e" },
  partial: { label: "部分数据", color: "#eab308" },
  insufficient: { label: "数据不足", color: "#ef4444" },
};

const STANCE_CN: Record<MAAContinuityStance, { label: string; color: string }> = {
  continuation: { label: "延续上版", color: "#22c55e" },
  refinement: { label: "细节修正", color: "#3b82f6" },
  reversal: { label: "方向反转", color: "#ef4444" },
  first_run: { label: "首次分析", color: "#94a3b8" },
};

const DIMENSION_COLOR: Record<string, string> = {
  PriceContext: "#a78bfa",
  OI: "#60a5fa",
  Funding: "#fbbf24",
  Basis: "#34d399",
  CVD: "#f472b6",
  Liquidation: "#f87171",
  LiqMap: "#fb923c",
  LiqSweep: "#f97316",
  Footprint: "#22d3ee",
  Taker: "#c084fc",
  Orderbook: "#94a3b8",
  Options: "#e879f9",
};

// ─────────────────────── 子组件 ───────────────────────

function Pill({
  children,
  color = "#94a3b8",
  bg,
  title,
}: {
  children: React.ReactNode;
  color?: string;
  bg?: string;
  title?: string;
}) {
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-md text-xs font-medium border"
      style={{
        color,
        borderColor: `${color}66`,
        backgroundColor: bg ?? `${color}1a`,
      }}
      title={title}
    >
      {children}
    </span>
  );
}

function ConfidenceBar({ value }: { value: number }) {
  const pct = Math.max(0, Math.min(100, value));
  const color = pct >= 75 ? "#22c55e" : pct >= 55 ? "#3b82f6" : pct >= 40 ? "#eab308" : "#ef4444";
  return (
    <div className="flex items-center gap-2 min-w-[140px]">
      <div className="flex-1 h-1.5 bg-slate-700 rounded overflow-hidden">
        <div
          className="h-full transition-all duration-500"
          style={{ width: `${pct}%`, backgroundColor: color }}
        />
      </div>
      <span className="text-xs font-mono font-semibold" style={{ color }}>
        {pct}
      </span>
    </div>
  );
}

function EvidenceCard({ item }: { item: MAAEvidenceItem }) {
  const color = DIMENSION_COLOR[item.dimension] ?? "#94a3b8";
  const weightDots = { high: "●●●", medium: "●●○", low: "●○○" }[item.weight] ?? "●○○";
  const supportBorder: Record<MAAEvidenceSupports, string> = {
    main: "border-l-emerald-500",
    contrarian: "border-l-rose-500",
    neutral: "border-l-slate-500",
  };
  return (
    <div
      className={`border-l-2 ${supportBorder[item.supports]} bg-slate-900/40 rounded-r p-2 space-y-1`}
    >
      <div className="flex items-center justify-between gap-2">
        <span
          className="text-[11px] font-semibold tracking-wide"
          style={{ color }}
        >
          {item.dimension}
        </span>
        <span
          className="text-[10px] font-mono text-slate-500"
          title={`权重：${item.weight}`}
        >
          {weightDots}
        </span>
      </div>
      <div className="text-[12px] text-slate-200 leading-relaxed">
        {item.observation}
      </div>
      {item.inference && (
        <div className="text-[11px] text-slate-400 leading-relaxed italic pl-2 border-l border-slate-700">
          → {item.inference}
        </div>
      )}
    </div>
  );
}

function EvidenceColumn({
  title,
  items,
  accent,
  emptyHint,
}: {
  title: string;
  items: MAAEvidenceItem[];
  accent: string;
  emptyHint: string;
}) {
  return (
    <div className="flex-1 min-w-[260px] bg-slate-900/30 border border-slate-800 rounded-lg p-3">
      <div className="flex items-center justify-between mb-2">
        <span className="text-sm font-semibold" style={{ color: accent }}>
          {title}
        </span>
        <span className="text-xs font-mono text-slate-500">{items.length}</span>
      </div>
      {items.length === 0 ? (
        <div className="text-xs text-slate-600 italic py-2">{emptyHint}</div>
      ) : (
        <div className="space-y-2">
          {items.map((it, i) => (
            <EvidenceCard key={i} item={it} />
          ))}
        </div>
      )}
    </div>
  );
}

function fmtTs(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const pad = (n: number) => n.toString().padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function fmtNum(n: number | null | undefined, digits = 2): string {
  if (n == null || Number.isNaN(n)) return "—";
  if (Math.abs(n) >= 1000) return n.toLocaleString(undefined, { maximumFractionDigits: digits });
  return n.toFixed(digits);
}

// ─────────────────────── 主组件 ───────────────────────

export default function MarketActionView() {
  const coin = useMarketStore((s) => s.coin);
  const report = useMarketStore((s) => s.maaByCoin[coin]);
  const loading = useMarketStore((s) => s.maaLoadingByCoin[coin] ?? false);
  const errMsg = useMarketStore((s) => s.maaErrorByCoin[coin]);
  const triggerMAARun = useMarketStore((s) => s.triggerMAARun);
  const loadMAAReport = useMarketStore((s) => s.loadMAAReport);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const evidenceGroups = useMemo(() => {
    const groups: Record<MAAEvidenceSupports, MAAEvidenceItem[]> = {
      main: [],
      contrarian: [],
      neutral: [],
    };
    (report?.evidence_breakdown ?? []).forEach((e) => {
      groups[e.supports]?.push(e);
    });
    return groups;
  }, [report]);

  if (!report) {
    return (
      <div className="p-6 space-y-3 text-slate-300">
        <div className="text-lg font-semibold">{coin}/USDT · 市场动作分析</div>
        <div className="text-sm text-slate-400">
          {errMsg
            ? `加载失败：${errMsg}`
            : loading
            ? "正在加载首份报告…"
            : "暂无 AI 分析结果；后端默认每 10 分钟自动生成，也可手动触发。"}
        </div>
        <div className="flex gap-2">
          <button
            onClick={() => loadMAAReport(coin)}
            className="px-3 py-1.5 text-sm rounded border border-slate-700 hover:bg-slate-800 transition"
          >
            刷新
          </button>
          <button
            onClick={() => triggerMAARun(coin)}
            disabled={loading}
            className="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 transition"
          >
            {loading ? "分析中…" : "手动触发分析"}
          </button>
        </div>
      </div>
    );
  }

  const sc = SCENARIO_CN[report.scenario] ?? SCENARIO_CN.range_bound;
  const bias = BIAS_CN[report.trading_implications.bias] ?? BIAS_CN.wait;
  const dq = DQ_CN[report.data_quality] ?? DQ_CN.ok;
  const cont = report.continuity;
  const staleColor =
    report.stale_minutes >= 30 ? "#ef4444" : report.stale_minutes >= 15 ? "#eab308" : "#22c55e";

  return (
    <div className="space-y-4 text-slate-200">
      {/* ── Hero 带 ── */}
      <div className="bg-gradient-to-r from-slate-900 to-slate-800/60 rounded-lg border border-slate-700 p-4 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex items-center gap-3 flex-wrap">
            <span
              className="text-lg font-bold px-3 py-1 rounded-md"
              style={{
                color: sc.color,
                backgroundColor: `${sc.color}20`,
                border: `1px solid ${sc.color}66`,
              }}
            >
              {sc.emoji} {sc.label}
            </span>
            <Pill color="#a78bfa">{PHASE_CN[report.market_phase]}</Pill>
            <Pill color={bias.color} bg={bias.bg}>
              交易倾向 · {bias.label}
            </Pill>
            <Pill color={dq.color}>{dq.label}</Pill>
            {cont && (
              <Pill color={STANCE_CN[cont.stance].color}>
                {STANCE_CN[cont.stance].label}
                {cont.previous_scenario &&
                  cont.stance !== "first_run" &&
                  ` ← ${SCENARIO_CN[cont.previous_scenario]?.label ?? cont.previous_scenario}`}
              </Pill>
            )}
            <Pill color={staleColor} title="报告生成时间距当前分钟数">
              {report.stale_minutes < 1 ? "刚刚" : `${report.stale_minutes}m 前`}
            </Pill>
          </div>
          <div className="flex items-center gap-3">
            <div className="text-right">
              <div className="text-[10px] text-slate-500 uppercase tracking-wider">
                confidence
              </div>
              <ConfidenceBar value={report.confidence} />
            </div>
            <button
              onClick={() => setDrawerOpen(true)}
              className="px-2.5 py-1 text-xs rounded border border-slate-700 text-slate-300 hover:bg-slate-800 transition"
              title="查看本轮喂给 AI 的原始数据 + 思维链"
            >
              Prompt 透明度
            </button>
            <button
              onClick={() => triggerMAARun(coin)}
              disabled={loading}
              className="px-2.5 py-1 text-xs rounded bg-blue-600 hover:bg-blue-500 disabled:opacity-50 transition"
              title="手动触发新一轮分析（后台异步）"
            >
              {loading ? "分析中…" : "重新分析"}
            </button>
          </div>
        </div>
        <div className="text-sm leading-relaxed text-slate-100">
          {report.market_conclusion}
        </div>
      </div>

      {/* ── 推理层 ── */}
      {(report.analyst_reasoning || report.confidence_rationale) && (
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-3">
          {report.analyst_reasoning && (
            <div className="lg:col-span-2 bg-slate-900/40 border border-slate-800 rounded-lg p-3">
              <div className="flex items-center gap-2 mb-2">
                <span className="text-xs font-semibold text-blue-400 uppercase tracking-wider">
                  交易员思维链
                </span>
                <span className="text-[10px] text-slate-500">
                  Step 1 扫描 → Step 2 证据群/矛盾 → Step 3 主假设 → Step 4 反事实
                </span>
              </div>
              <div className="text-[13px] leading-relaxed text-slate-300 whitespace-pre-wrap">
                {report.analyst_reasoning}
              </div>
            </div>
          )}
          <div className="space-y-3">
            {report.confidence_rationale && (
              <div className="bg-slate-900/40 border border-slate-800 rounded-lg p-3">
                <div className="text-xs font-semibold text-emerald-400 uppercase tracking-wider mb-2">
                  置信度解释
                </div>
                <div className="text-[12px] leading-relaxed text-slate-300">
                  {report.confidence_rationale}
                </div>
              </div>
            )}
            {cont?.note && (
              <div className="bg-slate-900/40 border border-slate-800 rounded-lg p-3">
                <div className="text-xs font-semibold uppercase tracking-wider mb-2"
                     style={{ color: STANCE_CN[cont.stance].color }}>
                  时序连续性 · {STANCE_CN[cont.stance].label}
                </div>
                <div className="text-[12px] leading-relaxed text-slate-300">{cont.note}</div>
                {cont.previous_ts && (
                  <div className="text-[10px] text-slate-500 mt-1 font-mono">
                    上一份：{fmtTs(cont.previous_ts)}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      )}

      {/* ── 证据矩阵（3 列：支持 / 矛盾 / 中性） ── */}
      <div>
        <div className="flex items-center justify-between mb-2">
          <span className="text-sm font-semibold text-slate-300">
            证据矩阵
            <span className="text-xs text-slate-500 ml-2 font-normal">
              按立场分三列 · 反事实失败项必须标矛盾
            </span>
          </span>
          <span className="text-xs text-slate-500 font-mono">
            共 {report.evidence_breakdown.length} 条
          </span>
        </div>
        <div className="flex flex-wrap gap-3">
          <EvidenceColumn
            title="✓ 支持主结论"
            items={evidenceGroups.main}
            accent="#22c55e"
            emptyHint="无"
          />
          <EvidenceColumn
            title="✗ 矛盾证据"
            items={evidenceGroups.contrarian}
            accent="#f87171"
            emptyHint="AI 未发现与主假设冲突的证据"
          />
          <EvidenceColumn
            title="· 中性观察"
            items={evidenceGroups.neutral}
            accent="#94a3b8"
            emptyHint="无"
          />
        </div>
      </div>

      {/* ── 对立视角 + 操作建议 ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
        {/* 对立场景 */}
        {report.alternative_scenario && (
          <div className="bg-slate-900/40 border border-slate-800 rounded-lg p-3">
            <div className="flex items-center gap-2 mb-2">
              <span className="text-xs font-semibold text-amber-400 uppercase tracking-wider">
                对立视角 · 第二可能性
              </span>
            </div>
            <div className="flex items-baseline gap-2 mb-1.5">
              <span
                className="text-sm font-semibold"
                style={{
                  color:
                    SCENARIO_CN[report.alternative_scenario.scenario]?.color ?? "#94a3b8",
                }}
              >
                {SCENARIO_CN[report.alternative_scenario.scenario]?.label ??
                  report.alternative_scenario.scenario}
              </span>
              <span className="text-xs font-mono text-amber-300">
                {report.alternative_scenario.probability_pct}%
              </span>
            </div>
            <div className="text-[12px] text-slate-300 leading-relaxed">
              <span className="text-slate-500">触发条件：</span>
              {report.alternative_scenario.trigger}
            </div>
          </div>
        )}

        {/* 操作建议 */}
        <div className="bg-slate-900/40 border border-slate-800 rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-semibold text-slate-300 uppercase tracking-wider">
              操作建议
            </span>
            <Pill color={bias.color} bg={bias.bg}>
              {bias.label}
            </Pill>
          </div>

          {report.trading_implications.bias === "wait" ||
          report.trading_implications.bias === "neutral" ? (
            <div className="text-[12px] text-slate-400 italic">
              {report.trading_implications.notes ||
                "AI 建议当前观望；关注 invalidation 条件变化。"}
            </div>
          ) : (
            <div className="grid grid-cols-2 gap-x-3 gap-y-1 text-[12px]">
              {report.trading_implications.entry_zone && (
                <>
                  <span className="text-slate-500">入场区</span>
                  <span className="text-right font-mono text-slate-200">
                    {fmtNum(report.trading_implications.entry_zone[0])} –{" "}
                    {fmtNum(report.trading_implications.entry_zone[1])}
                  </span>
                </>
              )}
              {report.trading_implications.stop_loss_beyond != null && (
                <>
                  <span className="text-slate-500">止损突破</span>
                  <span className="text-right font-mono text-rose-400">
                    {fmtNum(report.trading_implications.stop_loss_beyond)}
                  </span>
                </>
              )}
              {report.trading_implications.take_profit_targets.length > 0 && (
                <>
                  <span className="text-slate-500">止盈目标</span>
                  <span className="text-right font-mono text-emerald-400">
                    {report.trading_implications.take_profit_targets
                      .map((p) => fmtNum(p))
                      .join(" / ")}
                  </span>
                </>
              )}
            </div>
          )}

          {report.trading_implications.trader_intuition && (
            <div className="mt-2 pt-2 border-t border-slate-800 text-[12px] italic text-slate-300 leading-relaxed">
              💡 {report.trading_implications.trader_intuition}
            </div>
          )}

          {report.invalidation_conditions.length > 0 && (
            <div className="mt-2 pt-2 border-t border-slate-800">
              <div className="text-[10px] text-rose-400 uppercase tracking-wider mb-1">
                失效条件（触发即推翻主假设）
              </div>
              <ul className="text-[11px] text-slate-300 space-y-0.5 list-disc pl-4">
                {report.invalidation_conditions.map((c, i) => (
                  <li key={i}>{c}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </div>

      {/* ── Parse error 警示（降级报告） ── */}
      {report.prompt_debug && !report.prompt_debug.parse_ok && (
        <div className="bg-rose-900/30 border border-rose-800 rounded p-3 text-xs text-rose-300">
          ⚠ 本轮为降级报告（{report.prompt_debug.parse_error ?? "AI 输出无法解析"}）。
          可点「重新分析」触发新一轮。
        </div>
      )}

      {/* ── Phase 5 · AI 校准面板（T+4h/8h/24h 兑现率 + Confidence 校准） ── */}
      <MAACalibrationPanel />

      {/* ── Prompt 透明度抽屉 ── */}
      <PromptDebugDrawer
        open={drawerOpen}
        onClose={() => setDrawerOpen(false)}
        report={report as MarketActionReport}
      />
    </div>
  );
}
