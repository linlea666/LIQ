"use client";

import { useMemo, useState } from "react";
import { formatPrice } from "@/lib/format";
import { summarizeSources } from "@/lib/sourceBrief";
import {
  bounceQualityBrief,
  breakoutStageBrief,
} from "@/lib/structureBrief";
import type { KeyLevelV2 } from "@/lib/types";

/**
 * 强支撑/强阻力白话卡片
 *
 * 展示近/中/远三段各 1 个代表位（TOP 3），面向小白：
 *   - 星级 + 进度条直观表达"多强"
 *   - summarizeSources 归一化标签表达"为什么强"
 *   - bounce_count / test_count / sweep_usd 包装成白话"历史验证"
 *
 * 选位策略（按"距离段"分桶，避免近位把远位挤掉）：
 *   桶 near [0, 1.5%]  ── 短线即时反应
 *   桶 mid  [1.5%, 4%] ── 当日挑战位
 *   桶 far  [4%, 10%]  ── 波段反弹/突破目标
 *   每桶：tier ∈ {S, A} 优先，按 final_score 取最高；无则降级 B（加"参考"徽标）
 *   桶完全空 → 不占位，显示"该距离段暂无强位"
 *
 * 后端 final_score / historical_validity / bounce_count 通过 displayScore /
 * historyBrief 自动 fallback，不可用时退回 confluence_score / test_count。
 */

const STATE_TEXT: Record<string, { text: string; color: string }> = {
  approaching: { text: "正接近", color: "text-yellow-400" },
  testing: { text: "正测试", color: "text-amber-400" },
  swept: { text: "已扫取", color: "text-red-400" },
  bounced: { text: "已反弹", color: "text-green-400" },
  broken: { text: "已突破", color: "text-red-500" },
  flipped: { text: "已翻转", color: "text-purple-400" },
};

type Bucket = "near" | "mid" | "far";

const BUCKET_DEFS: Array<{
  key: Bucket;
  minPct: number;
  maxPct: number;
  label: string;
  hint: string;
  dotColor: string;
  textColor: string;
  bgColor: string;
}> = [
  {
    key: "near",
    minPct: 0.25,
    maxPct: 1.5,
    label: "近距",
    hint: "0.25%~1.5% 短线即时反应位（<0.25% 属贴脸位，操作价值低，已过滤）",
    dotColor: "bg-sky-400",
    textColor: "text-sky-300",
    bgColor: "bg-sky-500/10",
  },
  {
    key: "mid",
    minPct: 1.5,
    maxPct: 4.0,
    label: "中距",
    hint: "1.5%~4% 当日挑战位",
    dotColor: "bg-violet-400",
    textColor: "text-violet-300",
    bgColor: "bg-violet-500/10",
  },
  {
    key: "far",
    minPct: 4.0,
    maxPct: 12.0,
    label: "远距",
    hint: "4%~12% 波段反弹/突破目标（BTC 约 ±$3k~$10k 空间）",
    dotColor: "bg-orange-400",
    textColor: "text-orange-300",
    bgColor: "bg-orange-500/10",
  },
];

interface Picked {
  level: KeyLevelV2 | null; // null 表示该距离段空缺
  bucket: Bucket;
  fallbackB: boolean; // 降级到 B 级（加"参考"徽标）
}

function tierRank(tier: string): number {
  if (tier === "S") return 3;
  if (tier === "A") return 2;
  if (tier === "B") return 1;
  return 0;
}

/** 按距离段分桶选位：每桶独立选 tier 最高 + final_score 最高的代表 */
function pickByBuckets(levels: KeyLevelV2[], side: "support" | "resistance"): Picked[] {
  const sameSide = levels.filter((l) => l.side === side);
  const out: Picked[] = [];

  for (const b of BUCKET_DEFS) {
    const inBucket = sameSide.filter((l) => {
      const abs = Math.abs(l.distance_pct);
      return abs >= b.minPct && abs < b.maxPct;
    });
    if (inBucket.length === 0) {
      out.push({ level: null, bucket: b.key, fallbackB: false });
      continue;
    }

    const sorted = [...inBucket].sort((a, b2) => {
      const rd = tierRank(b2.strength_tier) - tierRank(a.strength_tier);
      if (rd !== 0) return rd;
      const sd = displayScore(b2) - displayScore(a);
      if (sd !== 0) return sd;
      // 同分时取距离更近的
      return Math.abs(a.distance_pct) - Math.abs(b2.distance_pct);
    });
    const pick = sorted[0];
    const fallbackB = pick.strength_tier === "B";
    out.push({ level: pick, bucket: b.key, fallbackB });
  }

  return out;
}

function getBucketDef(key: Bucket) {
  return BUCKET_DEFS.find((b) => b.key === key)!;
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

function fmtUsdShort(usd: number): string {
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(1)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(0)}M`;
  if (usd >= 1e3) return `$${(usd / 1e3).toFixed(0)}K`;
  return `$${usd.toFixed(0)}`;
}

/**
 * 把 cascade_risk（0-1 的清算级联风险分）翻译成小白能懂的文案。
 *
 * 后端定义：该关键位若被突破，下方/上方堆积的清算簇会被连锁扫取的剧烈程度。
 * 对支撑位 → 下方多头强平；对阻力位 → 上方空头止损，方向相反。
 *
 * 阈值与原行为对齐（>0.5 才显示），避免视觉噪声；tooltip 保留技术细节与原始百分比。
 */
function cascadeBrief(
  lv: KeyLevelV2,
): { text: string; color: string; hint: string } | null {
  const risk = lv.cascade_risk ?? 0;
  if (risk <= 0.5) return null;

  const layers = lv.cascade_layers ?? 0;
  const usdLabel = fmtUsdShort(lv.cascade_total_usd ?? 0);
  const isSupport = lv.side === "support";
  const pctLabel = `${Math.round(risk * 100)}%`;

  if (risk > 0.7) {
    return {
      text: isSupport ? "⚠️ 破位可能瀑布下跌" : "⚠️ 破位可能轧空暴涨",
      color: "text-red-400",
      hint: isSupport
        ? `该位下方还堆着 ${layers} 层多头强平盘（合计 ${usdLabel}）。一旦跌破会连锁触发，跌幅可能远超预期。技术分：cascade_risk=${pctLabel}`
        : `该位上方还堆着 ${layers} 层空头止损（合计 ${usdLabel}）。一旦突破会连环轧空，涨幅可能远超预期。技术分：cascade_risk=${pctLabel}`,
    };
  }

  return {
    text: isSupport ? "破位后可能快速下跌" : "破位后可能快速拉升",
    color: "text-orange-400",
    hint: isSupport
      ? `下方 ${layers} 层多头强平盘堆积（${usdLabel}），跌破后会加速下行。技术分：cascade_risk=${pctLabel}`
      : `上方 ${layers} 层空头止损堆积（${usdLabel}），突破后会加速上行。技术分：cascade_risk=${pctLabel}`,
  };
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

  const picked = useMemo(() => pickByBuckets(levels, tab), [levels, tab]);
  const validCount = picked.filter((p) => p.level !== null).length;

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
          <span className="text-[10px] text-slate-500">近 · 中 · 远 各 1 位</span>
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

      {/* 距离段说明条 */}
      <div className="px-4 py-1.5 bg-slate-900/40 border-b border-slate-700/60 flex items-center gap-3 text-[10px] text-slate-500">
        <span>💡 距离段：</span>
        {BUCKET_DEFS.map((b) => (
          <span key={b.key} className="inline-flex items-center gap-1" title={b.hint}>
            <span className={`w-1.5 h-1.5 rounded-full ${b.dotColor}`} />
            <span className={b.textColor}>{b.label}</span>
            <span className="text-slate-600">{b.minPct}-{b.maxPct}%</span>
          </span>
        ))}
      </div>

      {/* 内容 */}
      <div className={`divide-y divide-slate-700/50 ${bgTint}`}>
        {validCount === 0 ? (
          <div className="px-4 py-6 text-center text-xs text-slate-500">
            当前 ±10% 内暂无满足条件的{titleCn}
            <br />
            <span className="text-[10px] text-slate-600">
              （价格可能处于关键位真空区，注意控制杠杆）
            </span>
          </div>
        ) : (
          picked.map((p, i) => (
            <BucketBlock
              key={`${tab}-${p.bucket}-${p.level?.price ?? "empty"}-${i}`}
              picked={p}
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

function BucketBlock({
  picked,
  price,
  coin,
  sideColor,
  barColor,
}: {
  picked: Picked;
  price: number;
  coin: string;
  sideColor: string;
  barColor: string;
}) {
  const bucketDef = getBucketDef(picked.bucket);

  if (picked.level === null) {
    // 距离段空缺：仍显示标签行，告知用户"该段无强位"
    return (
      <div className="px-4 py-2 flex items-center gap-2 opacity-60">
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] ${bucketDef.bgColor} ${bucketDef.textColor} shrink-0`}
          title={bucketDef.hint}
        >
          <span className={`inline-block w-1.5 h-1.5 rounded-full ${bucketDef.dotColor} mr-1 align-middle`} />
          {bucketDef.label}
        </span>
        <span className="text-[11px] text-slate-500">该距离段暂无强位（跳过）</span>
      </div>
    );
  }

  return (
    <LevelBlock
      level={picked.level}
      bucketDef={bucketDef}
      fallbackB={picked.fallbackB}
      price={price}
      coin={coin}
      sideColor={sideColor}
      barColor={barColor}
    />
  );
}

function LevelBlock({
  level,
  bucketDef,
  fallbackB,
  price,
  coin,
  sideColor,
  barColor,
}: {
  level: KeyLevelV2;
  bucketDef: (typeof BUCKET_DEFS)[number];
  fallbackB: boolean;
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
      {/* 第一行：距离段徽标 + 价格 + 距离 + 星级 */}
      <div className="flex items-center gap-3 mb-1.5">
        <span
          className={`px-1.5 py-0.5 rounded text-[10px] ${bucketDef.bgColor} ${bucketDef.textColor} shrink-0 inline-flex items-center gap-1`}
          title={bucketDef.hint}
        >
          <span className={`w-1.5 h-1.5 rounded-full ${bucketDef.dotColor}`} />
          {bucketDef.label}
        </span>
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
        {fallbackB && (
          <span
            className="px-1.5 py-0.5 rounded text-[10px] bg-slate-600/30 text-slate-400 shrink-0"
            title="该距离段无 S/A 级强位，降级展示 B 级作参考"
          >
            参考
          </span>
        )}
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
      {stateInfo && (() => {
        const cascade = cascadeBrief(level);
        const bounce = bounceQualityBrief(level.bounce_quality);
        const stage = breakoutStageBrief(level.breakout_stage);
        return (
          <div className="flex items-center gap-1.5 flex-wrap">
            <span className="text-[10px] text-slate-500 shrink-0">⚡ 当前状态：</span>
            <span className={`text-xs ${stateInfo.color}`}>{stateInfo.text}</span>
            {relTime && <span className="text-[10px] text-slate-500">· {relTime}</span>}
            {bounce && (
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] cursor-help ${bounce.bg ?? ""} ${bounce.color}`}
                title={bounce.hint}
              >
                {bounce.label}
              </span>
            )}
            {stage && (
              <span
                className={`px-1.5 py-0.5 rounded text-[10px] cursor-help ${stage.bg ?? ""} ${stage.color}`}
                title={stage.hint}
              >
                {stage.label}
              </span>
            )}
            {cascade && (
              <span
                className={`text-[11px] ml-2 cursor-help ${cascade.color}`}
                title={cascade.hint}
              >
                · {cascade.text}
              </span>
            )}
          </div>
        );
      })()}

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
