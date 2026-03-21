export const API_BASE = process.env.NEXT_PUBLIC_API_BASE || "http://localhost:8000";
export const WS_URL = process.env.NEXT_PUBLIC_WS_URL || "http://localhost:8000";

export const SUPPORTED_COINS = ["BTC", "ETH", "SOL"] as const;
export type CoinType = (typeof SUPPORTED_COINS)[number];
export const DEFAULT_COIN: CoinType = "BTC";

export const COLORS = {
  bullish: "#22c55e",
  bearish: "#ef4444",
  warning: "#eab308",
  info: "#3b82f6",
  neutral: "#94a3b8",
  bgPrimary: "#0f172a",
  bgSecondary: "#1e293b",
  bgCard: "#1e293b",
  border: "#334155",
} as const;

export const TEMP_LABELS: Record<string, { emoji: string; color: string }> = {
  "极冷": { emoji: "❄️", color: "#3b82f6" },
  "偏冷": { emoji: "🟢", color: "#22c55e" },
  "中性": { emoji: "🟡", color: "#eab308" },
  "偏热": { emoji: "🟠", color: "#f97316" },
  "极热": { emoji: "🔥", color: "#ef4444" },
};

export const PIN_RISK_COLORS: Record<string, string> = {
  low: "#22c55e",
  attention: "#eab308",
  high: "#f97316",
  extreme: "#ef4444",
};
