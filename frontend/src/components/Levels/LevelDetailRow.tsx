"use client";

/**
 * 关键位明细行（V3 全景页与历史快照页共用）
 *
 * 主行：紧凑表格行（图3 风格 + V3 字段补强）
 *   - 价位 / 类型 / 强度(tier+s_class+所数) / 状态 / 距当前
 *   - 共振分 / 级联(主分+4 子分 hover) / 时间框架
 *   - 简短"为什么强" chips（前 3 个）
 *
 * 展开行（点击行）：完整 V3 详情
 *   - 全部 explain_chips（fallback summarizeSources）
 *   - 失效条件 / 破位磁吸 / 主导杠杆 / 跨所共振
 *   - 矛盾扣分 + 原因 chips
 *   - 数据新鲜度（is_stale + age）
 *   - cascade 4 子分 mini bar
 *   - 历史验证白话
 *   - LifecyclePanel（复用大屏组件）
 */

import { useState } from "react";
import { formatPrice, formatCnUsd } from "@/lib/format";
import { summarizeSources } from "@/lib/sourceBrief";
import {
  bounceQualityBrief,
  breakoutStageBrief,
} from "@/lib/structureBrief";
import {
  cascadeBrief,
  displayScore,
  fmtUsdShort,
  historyBrief,
  relativeTime,
  sClassHint,
} from "@/lib/levelBrief";
import LifecyclePanel from "@/components/MainView/LifecyclePanel";
import LevelBehaviorPanel from "@/components/Levels/LevelBehaviorPanel";
import type { KeyLevelV2 } from "@/lib/types";

const STATE_LABELS: Record<string, { text: string; color: string }> = {
  idle: { text: "待观察", color: "text-slate-500" },
  approaching: { text: "正接近", color: "text-yellow-400" },
  testing: { text: "正测试", color: "text-amber-400" },
  swept: { text: "已扫取", color: "text-red-400" },
  bounced: { text: "已反弹", color: "text-green-400" },
  broken: { text: "已突破", color: "text-red-500" },
  flipped: { text: "已翻转", color: "text-purple-400" },
};

const TIER_STYLES: Record<string, { bg: string; text: string }> = {
  S: { bg: "bg-amber-500/20", text: "text-amber-400" },
  A: { bg: "bg-red-500/15", text: "text-red-400" },
  B: { bg: "bg-blue-500/15", text: "text-blue-400" },
  C: { bg: "bg-slate-500/15", text: "text-slate-400" },
};

/** 过期时间白话：3.2h / 1.5d / 45m */
function ageLabel(hours?: number | null): string {
  if (hours == null || hours < 0) return "—";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

export default function LevelDetailRow({
  lv,
  coin,
  price,
  colCount,
}: {
  lv: KeyLevelV2;
  coin: string;
  price: number;
  /** 表格总列数，决定 expand 行 colSpan */
  colCount: number;
}) {
  const [open, setOpen] = useState(false);

  const stateInfo = STATE_LABELS[lv.state] || { text: lv.state, color: "text-slate-400" };
  const tier = TIER_STYLES[lv.strength_tier] || TIER_STYLES.C;
  const isAbove = lv.price > price;
  const score = displayScore(lv);
  const cascadePct = (lv.cascade_risk ?? 0) * 100;
  const cascadeColor =
    cascadePct > 70
      ? "text-red-400"
      : cascadePct > 40
        ? "text-orange-400"
        : "text-slate-500";

  // 简短 chips：优先用后端 explain_chips（V3 直出），否则 fallback summarizeSources
  const chipsRaw =
    lv.explain_chips && lv.explain_chips.length > 0
      ? lv.explain_chips.map((s) => ({ label: s, hint: s }))
      : summarizeSources(lv.sources, 3).map((b) => ({ label: b.label, hint: b.hint }));
  const briefChips = chipsRaw.slice(0, 3);
  const moreChipCount = Math.max(0, chipsRaw.length - briefChips.length);

  const cb = cascadeBrief(lv);
  const bounce = bounceQualityBrief(lv.bounce_quality);
  const stage = breakoutStageBrief(lv.breakout_stage);
  const cascadeTooltip =
    cb?.hint ??
    (cascadePct > 0 ? `cascade_risk=${cascadePct.toFixed(0)}%` : "无显著级联风险");

  const cc = lv.cascade_components;

  return (
    <>
      <tr
        onClick={() => setOpen((v) => !v)}
        className={`border-b border-slate-800/50 cursor-pointer transition-colors ${
          open
            ? "bg-slate-800/40"
            : lv.state !== "idle"
              ? "bg-slate-800/20 hover:bg-slate-800/40"
              : "hover:bg-slate-800/30"
        }`}
      >
        {/* 展开标记 */}
        <td className="py-2.5 pl-2 pr-1 text-slate-500 text-xs select-none">
          {open ? "▼" : "▶"}
        </td>

        {/* 价位 */}
        <td className="py-2.5 pr-3 font-mono text-white whitespace-nowrap">
          {formatPrice(lv.price, coin)}
        </td>

        {/* 类型（支撑/阻力） */}
        <td className="py-2.5 pr-3">
          <span className={isAbove ? "text-red-400" : "text-green-400"}>
            {isAbove ? "阻力" : "支撑"}
          </span>
        </td>

        {/* 强度：tier badge + s_class chip + 跨所 */}
        <td className="py-2.5 pr-3">
          <div className="flex items-center gap-1 flex-wrap">
            <span
              className={`px-2 py-0.5 rounded text-xs font-bold ${tier.bg} ${tier.text}`}
            >
              {lv.strength_tier}
            </span>
            {lv.s_class && lv.strength_tier === "S" && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] font-bold bg-amber-700/20 text-amber-300"
                title={sClassHint(lv.s_class)}
              >
                {lv.s_class}
              </span>
            )}
            {(lv.exchange_count ?? 0) >= 2 && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] bg-cyan-500/15 text-cyan-300"
                title={`跨所共振 ${lv.exchange_count} 所；共识乘子 ${
                  lv.consensus_multiplier?.toFixed(2) ?? "?"
                }×`}
              >
                {lv.exchange_count}所
              </span>
            )}
          </div>
        </td>

        {/* 状态 */}
        <td className="py-2.5 pr-3 whitespace-nowrap">
          <span className={stateInfo.color}>{stateInfo.text}</span>
          {lv.state !== "idle" && lv.state_ts > 0 && (
            <span className="ml-1 text-[10px] text-slate-500">
              · {relativeTime(lv.state_ts)}
            </span>
          )}
        </td>

        {/* 距当前 */}
        <td
          className={`py-2.5 pr-3 text-right font-mono whitespace-nowrap ${
            isAbove ? "text-red-400" : "text-green-400"
          }`}
        >
          {lv.distance_pct > 0 ? "+" : ""}
          {lv.distance_pct.toFixed(2)}%
        </td>

        {/* 共振分 */}
        <td
          className="py-2.5 pr-3 text-right text-slate-300 font-mono"
          title={
            (lv.contradiction_penalty ?? 0) > 0
              ? `final_score=${score.toFixed(0)}（含矛盾扣分 -${lv.contradiction_penalty?.toFixed(0)}）；独立证据组=${lv.independent_group_count ?? "?"}`
              : `final_score=${score.toFixed(0)}；独立证据组=${lv.independent_group_count ?? "?"}`
          }
        >
          {score.toFixed(0)}
        </td>

        {/* 级联 */}
        <td
          className={`py-2.5 pr-3 text-right font-mono whitespace-nowrap ${cascadeColor}`}
          title={cascadeTooltip}
        >
          {cascadePct > 0 ? `${cascadePct.toFixed(0)}%` : "低"}
          {(lv.cascade_layers ?? 0) > 0 && (
            <span className="ml-1 text-[10px] text-slate-500">
              ·{lv.cascade_layers}层
            </span>
          )}
        </td>

        {/* 时间框架 */}
        <td className="py-2.5 pr-3 text-slate-500 text-xs whitespace-nowrap">
          {lv.timeframe || "-"}
        </td>

        {/* 为什么强（briefs） */}
        <td className="py-2.5 pr-3">
          <div className="flex items-center gap-1 flex-wrap">
            {briefChips.map((c, i) => (
              <span
                key={i}
                title={c.hint}
                className="px-1.5 py-0.5 rounded text-[10px] bg-slate-700/40 text-slate-300 whitespace-nowrap"
              >
                {c.label}
              </span>
            ))}
            {moreChipCount > 0 && (
              <span className="text-[10px] text-slate-500">+{moreChipCount}</span>
            )}
            {lv.is_stale && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] bg-rose-500/15 text-rose-300"
                title={`主源已过期（age=${ageLabel(lv.primary_source_age_hours)}）；final_score 已被软衰减`}
              >
                ⏳ 旧
              </span>
            )}
            {(lv.contradiction_penalty ?? 0) > 0 && (
              <span
                className="px-1.5 py-0.5 rounded text-[10px] bg-orange-500/15 text-orange-300"
                title={
                  (lv.contradiction_reasons ?? []).join(" · ") ||
                  "存在矛盾扣分"
                }
              >
                ⚠ -{lv.contradiction_penalty?.toFixed(0)}
              </span>
            )}
          </div>
        </td>
      </tr>

      {/* 展开详情 */}
      {open && (
        <tr className="bg-slate-900/60 border-b border-slate-800/50">
          <td colSpan={colCount} className="px-4 py-4">
            <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-xs">
              {/* 左列：完整证据 + 历史 + 杠杆 */}
              <div className="space-y-3">
                {/* 完整 explain_chips */}
                {chipsRaw.length > 0 && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">
                      📍 为什么强（共 {chipsRaw.length} 项）
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {chipsRaw.map((c, i) => (
                        <span
                          key={i}
                          title={c.hint}
                          className="px-2 py-0.5 rounded bg-slate-700/40 text-slate-300"
                        >
                          {c.label}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* 证据组 */}
                {lv.evidence_groups && lv.evidence_groups.length > 0 && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">
                      🧬 独立证据组（去重 {lv.independent_group_count ?? lv.evidence_groups.length} 组）
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {lv.evidence_groups.map((g, i) => (
                        <span
                          key={i}
                          className="px-2 py-0.5 rounded bg-blue-500/10 text-blue-300 text-[10px] font-mono"
                        >
                          {g}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* 历史验证 */}
                <div>
                  <div className="text-[10px] text-slate-500 mb-1">📊 历史验证</div>
                  <div className="text-slate-300">{historyBrief(lv)}</div>
                </div>

                {/* 主导杠杆 + 跨所共振（详） */}
                {(lv.dominant_leverage || lv.exchange_count) && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">⚙️ 杠杆与共识</div>
                    <div className="flex flex-wrap gap-2 text-slate-300">
                      {lv.dominant_leverage && (
                        <span>
                          主导杠杆：
                          <span className="text-amber-300 font-mono">
                            {lv.dominant_leverage}
                          </span>
                          {(lv.leverage_intensity ?? 0) > 0 && (
                            <span className="text-slate-500 ml-1">
                              (占比 {((lv.leverage_intensity ?? 0) * 100).toFixed(0)}%)
                            </span>
                          )}
                        </span>
                      )}
                      {(lv.exchange_count ?? 0) >= 2 && (
                        <span>
                          跨所共振：
                          <span className="text-cyan-300 font-mono">
                            {lv.exchange_count} 所
                          </span>
                          {lv.consensus_multiplier && (
                            <span className="text-slate-500 ml-1">
                              ×{lv.consensus_multiplier.toFixed(2)} 共识乘子
                            </span>
                          )}
                        </span>
                      )}
                    </div>
                  </div>
                )}

                {/* 当前状态附加（反弹质量 / 突破阶段） */}
                {(bounce || stage) && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">
                      ⚡ 当前状态附加
                    </div>
                    <div className="flex flex-wrap gap-2 text-slate-300">
                      {bounce && (
                        <span title={bounce.hint} className={bounce.color}>
                          {bounce.label}
                        </span>
                      )}
                      {stage && (
                        <span title={stage.hint} className={stage.color}>
                          {stage.label}
                        </span>
                      )}
                    </div>
                  </div>
                )}
              </div>

              {/* 右列：风险 + 失效 + 数据 + 生命周期 */}
              <div className="space-y-3">
                {/* 失效条件 */}
                {lv.invalidation_condition && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">⛔ 失效条件</div>
                    <div className="text-rose-300 font-mono">
                      {lv.invalidation_condition}
                    </div>
                    {lv.invalidation_atr_mult && (
                      <div className="text-[10px] text-slate-500 mt-0.5">
                        基于 {lv.invalidation_atr_mult.toFixed(1)}× ATR 自适应阈值
                      </div>
                    )}
                  </div>
                )}

                {/* 破位磁吸 */}
                {lv.next_magnet_price && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">💥 破位磁吸</div>
                    <div className="text-orange-300 font-mono">
                      {formatPrice(lv.next_magnet_price, coin)}
                      {(lv.vacuum_gap_pct ?? 0) > 0 && (
                        <span className="text-slate-500 ml-2">
                          真空 {lv.vacuum_gap_pct?.toFixed(2)}%
                        </span>
                      )}
                    </div>
                  </div>
                )}

                {/* cascade 4 子分 */}
                {cc && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">
                      🌊 级联风险拆解（cascade_components）
                    </div>
                    <div className="space-y-1">
                      <CascadeBar label="层数" value={cc.count_score} />
                      <CascadeBar label="累计 USD" value={cc.usd_score} />
                      <CascadeBar label="紧凑度" value={cc.velocity_score} />
                      <CascadeBar label="杠杆密度" value={cc.leverage_score} />
                    </div>
                    {(lv.cascade_total_usd ?? 0) > 0 && (
                      <div className="text-[10px] text-slate-500 mt-1">
                        累计强平规模：
                        <span className="text-slate-300 font-mono ml-1">
                          {formatCnUsd(lv.cascade_total_usd ?? 0)}
                        </span>
                      </div>
                    )}
                  </div>
                )}

                {/* 矛盾扣分 */}
                {(lv.contradiction_penalty ?? 0) > 0 && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">
                      ⚠ 矛盾扣分（-{lv.contradiction_penalty?.toFixed(0)}）
                    </div>
                    <div className="flex flex-wrap gap-1.5">
                      {(lv.contradiction_reasons ?? []).map((r, i) => (
                        <span
                          key={i}
                          className="px-2 py-0.5 rounded bg-orange-500/10 text-orange-300 text-[10px]"
                        >
                          {r}
                        </span>
                      ))}
                    </div>
                  </div>
                )}

                {/* 数据新鲜度 */}
                <div>
                  <div className="text-[10px] text-slate-500 mb-1">🕒 数据新鲜度</div>
                  {lv.is_stale ? (
                    <div className="text-rose-300">
                      主源已过期 · 主源年龄 {ageLabel(lv.primary_source_age_hours)}
                      {lv.regime_modifier_applied != null && (
                        <span className="text-slate-500 ml-2">
                          regime 乘子 ×{lv.regime_modifier_applied.toFixed(2)}
                        </span>
                      )}
                    </div>
                  ) : (
                    <div className="text-slate-300">
                      新鲜
                      {lv.primary_source_age_hours != null && (
                        <span className="text-slate-500 ml-2">
                          主源 {ageLabel(lv.primary_source_age_hours)}
                        </span>
                      )}
                      {lv.regime_modifier_applied != null && (
                        <span className="text-slate-500 ml-2">
                          regime ×{lv.regime_modifier_applied.toFixed(2)}
                          {lv.regime_at_score && ` (${lv.regime_at_score})`}
                        </span>
                      )}
                    </div>
                  )}
                </div>

                {/* sweep 详情兜底 */}
                {(lv.sweep_usd ?? 0) > 0 && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">🩸 资金撞击</div>
                    <div className="text-slate-300 font-mono">
                      累计 {fmtUsdShort(lv.sweep_usd ?? 0)}
                    </div>
                  </div>
                )}

                {/* M4 · 行为评估面板（V3 行为验证层 · 2026-04 · 纯观测） */}
                {lv.behavior && (
                  <LevelBehaviorPanel behavior={lv.behavior} state={lv.state} />
                )}

                {/* 生命周期 */}
                {lv.lifecycle_events && lv.lifecycle_events.length > 0 && (
                  <div>
                    <div className="text-[10px] text-slate-500 mb-1">
                      📜 生命周期事件
                    </div>
                    <LifecyclePanel
                      events={lv.lifecycle_events}
                      level_id={lv.level_id}
                    />
                  </div>
                )}

                {/* 原始 sources（开发参考） */}
                {lv.sources && lv.sources.length > 0 && (
                  <details className="text-[10px] text-slate-500">
                    <summary className="cursor-pointer hover:text-slate-300">
                      原始 sources 列表（{lv.sources.length} 项）
                    </summary>
                    <div className="mt-1 pl-3 text-slate-500 break-all">
                      {lv.sources.join(" · ")}
                    </div>
                  </details>
                )}
              </div>
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function CascadeBar({ label, value }: { label: string; value: number }) {
  const pct = Math.max(0, Math.min(100, Math.round(value * 100)));
  const color =
    pct > 70 ? "bg-red-500" : pct > 40 ? "bg-orange-500" : "bg-slate-500";
  return (
    <div className="flex items-center gap-2">
      <span className="text-slate-500 w-16 shrink-0">{label}</span>
      <div className="flex-1 h-1.5 bg-slate-800 rounded-full overflow-hidden">
        <div
          className={`h-full ${color} rounded-full transition-all`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className="text-slate-400 font-mono w-10 text-right text-[10px]">
        {pct}%
      </span>
    </div>
  );
}
