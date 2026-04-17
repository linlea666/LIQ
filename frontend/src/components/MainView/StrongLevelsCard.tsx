"use client";

import { useMemo, useState } from "react";
import { formatPrice } from "@/lib/format";
import { summarizeSources } from "@/lib/sourceBrief";
import type { KeyLevelV2 } from "@/lib/types";

/**
 * 强支撑/强阻力白话卡片
 *
 * 展示从近到远的 TOP-3 强位，面向小白：
 *   - 星级 + 进度条直观表达"多强"
 *   - summarizeSources 归一化标签表达"为什么强"
 *   - test_count / sweep_usd / state_ts 包装成白话"历史验证"
 *
 * 选位策略：
 *   1) 先取 tier ∈ {S, A}，按距离从近到远
 *   2) 不足 3 个则补 tier B
 *   3) 再不足显示空态提示（避免用远位误导小白）
 *
 * Phase 2 后端补齐 final_score / historical_validity / bounce_count 后，
 * 排序与"历史验证"文案会自动升级（fallback 到当前字段不中断）。
 */

const STATE_TEXT: Record<string, { text: string; color: string }> = {
  approaching: { text: "正接近", color: "text-yellow-400" },
  testing: { text: "正测试", color: "text-amber-400" },
  swept: { text: "已扫取", color: "text-red-400" },
  bounced: { text: "已反弹", color: "text-green-400" },
  broken: { text: "已突破", color: "text-red-500" },
  flipped: { text: "已翻转", color: "text-purple-400" },
};

function pickTop(levels: KeyLevelV2[], side: "support" | "resistance", n = 3): KeyLevelV2[] {
  const sameSide = levels.filter((l) => l.side === side);
  const sortByDistance =
    side === "support"
      ? (a: KeyLevelV2, b: KeyLevelV2) => b.price - a.price // 支撑：从近到远 = 价格从高到低
      : (a: KeyLevelV2, b: KeyLevelV2) => a.price - b.price; // 阻力：从近到远 = 价格从低到高

  const strong = sameSide
    .filter((l) => l.strength_tier === "S" || l.strength_tier === "A")
    .sort(sortByDistance);
  if (strong.length >= n) return strong.slice(0, n);

  const fillB = sameSide
    .filter((l) => l.strength_tier === "B")
    .sort(sortByDistance)
    .slice(0, n - strong.length);
  return [...strong, ...fillB];
}

function scoreToStars(score: number): number {
  // 0-100 → 1-5 星；B 级也至少 2 星避免视觉全空
  const raw = Math.round(score / 20);
  return Math.max(1, Math.min(5, raw));
}

/** 拿真正用于排序/展示的分：优先 final_score，其次 confluence_score（Phase 2 前兼容） */
function displayScore(lv: KeyLevelV2): number {
  if (typeof lv.final_score === "number" && lv.final_score > 0) return lv.final_score;
  return lv.confluence_score;
}

function relativeTime(ts: number): string {
  if (!ts || ts <= 0) return "";
  const diffSec = Math.floor(Date.now() / 1000 - ts);
  if (diffSec < 60) return `${diffSec}秒前`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}小时前`;
  return `${Math.floor(diffSec / 86400)}天前`;
}

function historyBrief(lv: KeyLevelV2): string {
  // 优先用 Phase 2 字段 bounce_count / historical_validity
  const parts: string[] = [];
  const bounce = lv.bounce_count ?? 0;
  if (bounce > 0) {
    parts.push(`成功反弹 ${bounce} 次`);
  }
  if (lv.test_count > 0) {
    parts.push(`被测试 ${lv.test_count} 次`);
  }
  if (lv.sweep_usd > 0) {
    const usdM = lv.sweep_usd / 1e6;
    parts.push(`有过 $${usdM.toFixed(usdM >= 10 ? 0 : 1)}M 资金撞击`);
  }
  const hv = lv.historical_validity ?? 0;
  if (parts.length === 0 && hv === 0) return "暂未被价格触碰（干净位）";
  if (parts.length === 0) return "暂未被价格触碰（干净位）";
  const hvLabel =
    hv >= 0.6 ? "（验证充分）" : hv >= 0.3 ? "（部分验证）" : "";
  return parts.join(" · ") + hvLabel;
}

const MEDAL = ["🥇", "🥈", "🥉"];

export default function StrongLevelsCard({
  levels,
  price,
  coin,
}: {
  levels: KeyLevelV2[];
  price: number;
  coin: string;
}) {
  const [tab, setTab] = useState<"support" | "resistance">("support");

  const picked = useMemo(() => pickTop(levels, tab, 3), [levels, tab]);

  const titleCn = tab === "support" ? "强支撑位" : "强阻力位";
  const sideColor = tab === "support" ? "text-green-400" : "text-red-400";
  const barColor = tab === "support" ? "bg-green-500" : "bg-red-500";
  const bgTint = tab === "support" ? "bg-green-950/10" : "bg-red-950/10";

  return (
    <div className="bg-slate-800/50 border border-slate-700 rounded-lg overflow-hidden">
      {/* 头部：tab + 说明 */}
      <div className="flex items-center justify-between px-4 py-2.5 border-b border-slate-700 bg-slate-800/80">
        <div className="flex items-center gap-2">
          <span className="text-base">💎</span>
          <h3 className="text-sm font-semibold text-slate-200">{titleCn}</h3>
          <span className="text-[10px] text-slate-500">从近到远 · TOP 3</span>
        </div>
        <div className="flex gap-1 bg-slate-900/60 rounded-md p-0.5">
          <button
            type="button"
            onClick={() => setTab("support")}
            className={`px-2.5 py-0.5 text-xs rounded transition-colors ${
              tab === "support"
                ? "bg-green-500/20 text-green-300 font-medium"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            强支撑
          </button>
          <button
            type="button"
            onClick={() => setTab("resistance")}
            className={`px-2.5 py-0.5 text-xs rounded transition-colors ${
              tab === "resistance"
                ? "bg-red-500/20 text-red-300 font-medium"
                : "text-slate-400 hover:text-slate-200"
            }`}
          >
            强阻力
          </button>
        </div>
      </div>

      {/* 内容 */}
      <div className={`divide-y divide-slate-700/50 ${bgTint}`}>
        {picked.length === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-slate-500">
            当前暂无满足条件的{titleCn}
            <br />
            <span className="text-[10px] text-slate-600">
              （价格可能处于关键位真空区，注意控制杠杆）
            </span>
          </div>
        ) : (
          picked.map((lv, i) => (
            <LevelBlock
              key={`${lv.side}-${lv.price}-${i}`}
              level={lv}
              rank={i}
              price={price}
              coin={coin}
              sideColor={sideColor}
              barColor={barColor}
            />
          ))
        )}
      </div>
    </div>
  );
}

function LevelBlock({
  level,
  rank,
  price,
  coin,
  sideColor,
  barColor,
}: {
  level: KeyLevelV2;
  rank: number;
  price: number;
  coin: string;
  sideColor: string;
  barColor: string;
}) {
  const score = displayScore(level);
  const stars = scoreToStars(score);
  const briefs = useMemo(() => summarizeSources(level.sources, 3), [level.sources]);
  const stateInfo = level.state !== "idle" ? STATE_TEXT[level.state] : null;
  const relTime = relativeTime(level.state_ts);
  const distPct = level.distance_pct;
  const barWidth = Math.min(100, score);

  return (
    <div className="px-4 py-3">
      {/* 第一行：奖牌 + 价格 + 距离 + 星级 */}
      <div className="flex items-center gap-3 mb-1.5">
        <span className="text-lg shrink-0 w-6 text-center">{MEDAL[rank] ?? "•"}</span>
        <span className={`text-lg font-mono font-bold ${sideColor} shrink-0`}>
          {formatPrice(level.price, coin)}
        </span>
        <span
          className={`text-xs font-mono shrink-0 ${
            distPct >= 0 ? "text-red-400" : "text-green-400"
          }`}
          title="距当前价百分比"
        >
          {distPct >= 0 ? "+" : ""}
          {distPct.toFixed(2)}%
        </span>
        <div className="flex-1" />
        <span
          className="text-xs tracking-tighter shrink-0"
          title={
            typeof level.final_score === "number" && level.final_score > 0
              ? `最终评分 ${score.toFixed(0)}/100（共振 ${level.confluence_score.toFixed(0)} × 时间 + 历史 + 屏障 ${level.barrier_score?.toFixed(1) ?? 0}）· 等级 ${level.strength_tier}`
              : `共振分 ${score.toFixed(0)}/100 · 等级 ${level.strength_tier}`
          }
        >
          {"★".repeat(stars)}
          <span className="text-slate-700">{"★".repeat(5 - stars)}</span>
        </span>
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] font-bold shrink-0 ${
            level.strength_tier === "S"
              ? "bg-amber-500/20 text-amber-400"
              : level.strength_tier === "A"
                ? "bg-red-500/15 text-red-400"
                : "bg-blue-500/15 text-blue-400"
          }`}
        >
          {level.strength_tier}
        </span>
      </div>

      {/* 进度条 */}
      <div className="h-1.5 bg-slate-700/50 rounded-full overflow-hidden mb-2">
        <div
          className={`h-full ${barColor} rounded-full transition-all`}
          style={{ width: `${barWidth}%`, opacity: 0.35 + barWidth * 0.0065 }}
        />
      </div>

      {/* 为什么强 */}
      {briefs.length > 0 && (
        <div className="flex items-start gap-1.5 mb-1">
          <span className="text-[10px] text-slate-500 shrink-0 pt-0.5">📍 为什么强：</span>
          <div className="flex flex-wrap gap-1.5 text-xs text-slate-300">
            {briefs.map((b, i) => (
              <span
                key={i}
                title={b.hint}
                className="px-1.5 py-0.5 rounded bg-slate-700/40 text-slate-200 cursor-help"
              >
                {b.label}
              </span>
            ))}
            {level.timeframe && (
              <span
                className="px-1.5 py-0.5 rounded bg-slate-700/20 text-slate-400 text-[11px]"
                title="该位最强的时间框架"
              >
                {level.timeframe}
              </span>
            )}
          </div>
        </div>
      )}

      {/* 历史验证 */}
      <div className="flex items-center gap-1.5 mb-1">
        <span className="text-[10px] text-slate-500 shrink-0">📊 历史验证：</span>
        <span className="text-xs text-slate-400">{historyBrief(level)}</span>
      </div>

      {/* 当前状态（仅非 idle 展示） */}
      {stateInfo && (
        <div className="flex items-center gap-1.5">
          <span className="text-[10px] text-slate-500 shrink-0">⚡ 当前状态：</span>
          <span className={`text-xs ${stateInfo.color}`}>{stateInfo.text}</span>
          {relTime && <span className="text-[10px] text-slate-500">· {relTime}</span>}
          {level.cascade_risk > 0.5 && (
            <span className="text-[10px] text-orange-400 ml-2">
              级联风险 {(level.cascade_risk * 100).toFixed(0)}%
            </span>
          )}
        </div>
      )}

      {/* 价格与当前价关系的迷你刻度（额外视觉锚点） */}
      {price > 0 && (
        <div className="mt-2 pt-2 border-t border-slate-700/30 flex items-center gap-2 text-[10px] text-slate-500">
          <span>与当前价 {formatPrice(price, coin)} 相比：</span>
          <span className={distPct >= 0 ? "text-red-400" : "text-green-400"}>
            {distPct >= 0 ? "↑ 上方" : "↓ 下方"} {Math.abs(distPct).toFixed(2)}%
          </span>
        </div>
      )}
    </div>
  );
}
