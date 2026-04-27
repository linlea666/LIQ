/**
 * 关键位详情共用渲染辅助（V3 全景页与历史快照页共用）
 *
 * 设计：从 StrongLevelsCard 抽取共用的纯函数（独立新写而非动大屏），
 * 只承担"数据 → 文案 + 颜色"的 UI 适配，无业务副作用。
 */

import type { KeyLevelV2 } from "@/lib/types";

/**
 * tier 排序权重：S > A > B > C，未识别为 0。
 * 用于排序"按强度优先"。
 */
export function tierRank(tier: string): number {
  if (tier === "S") return 4;
  if (tier === "A") return 3;
  if (tier === "B") return 2;
  if (tier === "C") return 1;
  return 0;
}

/**
 * 取真正用于排序/展示的分：优先 final_score，缺失时 fallback confluence_score。
 * 与大屏 StrongLevelsCard 行为一致。
 */
export function displayScore(lv: KeyLevelV2): number {
  if (typeof lv.final_score === "number" && lv.final_score > 0) return lv.final_score;
  return lv.confluence_score ?? 0;
}

/** 紧凑 USD 文案：>= 1B → $X.XB；>= 1M → $XM；>= 1K → $XK */
export function fmtUsdShort(usd: number): string {
  if (!usd || usd <= 0) return "-";
  if (usd >= 1e9) return `$${(usd / 1e9).toFixed(1)}B`;
  if (usd >= 1e6) return `$${(usd / 1e6).toFixed(0)}M`;
  if (usd >= 1e3) return `$${(usd / 1e3).toFixed(0)}K`;
  return `$${usd.toFixed(0)}`;
}

/**
 * 历史验证白话：bounce/test/sweep 拼接 + historical_validity 标注。
 * 与 StrongLevelsCard.historyBrief 保持一致语义。
 */
export function historyBrief(lv: KeyLevelV2): string {
  const parts: string[] = [];
  const bounce = lv.bounce_count ?? 0;
  if (bounce > 0) parts.push(`成功反弹 ${bounce} 次`);
  if ((lv.test_count ?? 0) > 0) parts.push(`被测试 ${lv.test_count} 次`);
  if ((lv.sweep_usd ?? 0) > 0) {
    const usdM = (lv.sweep_usd ?? 0) / 1e6;
    parts.push(`有过 $${usdM.toFixed(usdM >= 10 ? 0 : 1)}M 资金撞击`);
  }
  const hv = lv.historical_validity ?? 0;
  if (parts.length === 0) return "暂未被价格触碰（干净位）";
  const hvLabel = hv >= 0.6 ? "（验证充分）" : hv >= 0.3 ? "（部分验证）" : "";
  return parts.join(" · ") + hvLabel;
}

/** 相对时间（秒级时间戳） */
export function relativeTime(ts: number): string {
  if (!ts || ts <= 0) return "";
  const diffSec = Math.floor(Date.now() / 1000 - ts);
  if (diffSec < 60) return `${diffSec}秒前`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}分钟前`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}小时前`;
  return `${Math.floor(diffSec / 86400)}天前`;
}

/**
 * cascade 风险白话：>0.7 重红、>0.5 橙提示，否则不展示（避免噪声）。
 * 与 StrongLevelsCard.cascadeBrief 一致。
 */
export function cascadeBrief(
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
      text: isSupport ? "破位可能瀑布下跌" : "破位可能轧空暴涨",
      color: "text-red-400",
      hint: isSupport
        ? `下方还堆着 ${layers} 层多头强平盘（合计 ${usdLabel}）。一旦跌破会连锁触发。cascade_risk=${pctLabel}`
        : `上方还堆着 ${layers} 层空头止损（合计 ${usdLabel}）。一旦突破会连环轧空。cascade_risk=${pctLabel}`,
    };
  }
  return {
    text: isSupport ? "破位后可能快速下跌" : "破位后可能快速拉升",
    color: "text-orange-400",
    hint: isSupport
      ? `下方 ${layers} 层多头强平盘堆积（${usdLabel}）。cascade_risk=${pctLabel}`
      : `上方 ${layers} 层空头止损堆积（${usdLabel}）。cascade_risk=${pctLabel}`,
  };
}

/**
 * S-Class（4 分型）中文 hint。
 * 与 StrongLevelsCard 文案一致，全景页展开行也用。
 */
export function sClassHint(s: string): string {
  if (s === "S-Macro") return "宏观级关键位（长周期独占：周月线/200W SMA/CVDD 等）";
  if (s === "S-Liquidity") return "流动性级关键位（跨所清算共振：≥4 所 + ≥2 个清算证据组）";
  if (s === "S-Micro") return "微观级关键位（盘口微结构 + 资金流共振，TTL 较短）";
  if (s === "S-Composite") return "复合级关键位（≥3 独立证据组）";
  return s;
}

export type SortKey = "tier" | "distance" | "cascade";

/**
 * 关键位排序：默认 tier-first（强度优先 → final_score → 距离）。
 * 输入是同侧（support 或 resistance）的子集。
 */
export function sortLevels(
  levels: KeyLevelV2[],
  key: SortKey,
): KeyLevelV2[] {
  const arr = [...levels];
  if (key === "distance") {
    arr.sort((a, b) => Math.abs(a.distance_pct) - Math.abs(b.distance_pct));
    return arr;
  }
  if (key === "cascade") {
    arr.sort((a, b) => (b.cascade_risk ?? 0) - (a.cascade_risk ?? 0));
    return arr;
  }
  // tier-first（默认）
  arr.sort((a, b) => {
    const r = tierRank(b.strength_tier) - tierRank(a.strength_tier);
    if (r !== 0) return r;
    const s = displayScore(b) - displayScore(a);
    if (s !== 0) return s;
    return Math.abs(a.distance_pct) - Math.abs(b.distance_pct);
  });
  return arr;
}
