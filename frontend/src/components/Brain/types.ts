import type { BrainDominantRole } from "@/lib/types";

/** dominant_role 颜色映射（前端唯一来源；其他组件 import 此常量） */
export const ROLE_COLORS: Record<
  BrainDominantRole,
  { hex: string; bg: string; border: string; text: string; label: string }
> = {
  spot_defense: {
    hex: "#10b981",
    bg: "bg-emerald-500/15",
    border: "border-emerald-500/60",
    text: "text-emerald-300",
    label: "防守位",
  },
  futures_target: {
    hex: "#f59e0b",
    bg: "bg-amber-500/15",
    border: "border-amber-500/60",
    text: "text-amber-300",
    label: "目标位",
  },
  liquidation_magnet: {
    hex: "#a78bfa",
    bg: "bg-violet-500/15",
    border: "border-violet-500/60",
    text: "text-violet-300",
    label: "清算磁铁",
  },
  contested: {
    hex: "#ec4899",
    bg: "bg-pink-500/15",
    border: "border-pink-500/60",
    text: "text-pink-300",
    label: "争夺区",
  },
  key_level_only: {
    hex: "#60a5fa",
    bg: "bg-blue-500/15",
    border: "border-blue-500/60",
    text: "text-blue-300",
    label: "关键位",
  },
  other: {
    hex: "#64748b",
    bg: "bg-slate-700/40",
    border: "border-slate-600/60",
    text: "text-slate-400",
    label: "关注位",
  },
};

/** UI 转译方向词，禁止在 UI 里出现 long/short/buy/sell。 */
export function translateDirection(d: "long" | "short" | "neutral"): string {
  if (d === "long") return "做多观察";
  if (d === "short") return "做空观察";
  return "等待";
}

/** UI 转译 setup 状态。
 * 注意 confirmed 显示为「结构确认」而非「已确认」—— 避免被误读为"可以入场"。
 * 状态机的 confirmed 仅指"结构吸收信号已达成"，不含交易决策语义。 */
export function translateSetupState(name: string): string {
  const m: Record<string, string> = {
    forming: "酝酿中",
    waiting_for_trigger: "等待触发",
    triggered: "已触达",
    confirmation_pending: "等待确认",
    confirmed: "结构确认",
    invalidated: "结构失效",
    cancelled: "已取消",
    missed: "已错过",
    cooldown: "冷却中",
  };
  return m[name] ?? name;
}
