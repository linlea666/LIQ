export function formatUSD(value: number, decimals = 2): string {
  if (Math.abs(value) >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
  if (Math.abs(value) >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  if (Math.abs(value) >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${value.toFixed(decimals)}`;
}

/**
 * 中文金额格式化 · 亿+万 两档（雪球/东财通用风格）
 *
 *   ≥ 1 亿       → "X.XX 亿"            （1.18 亿 / 12.00 亿）
 *   ≥ 100 万     → "X,XXX 万" 千分位整数 （680 万 / 5,620 万）
 *   ≥ 1 万       → "X.X 万"  保留 1 位   （1.5 万 / 99.9 万）
 *   < 1 万       → 千分位整数            （8,500）
 *   0 / NaN      → "0"
 *   负数         → 保留 "-" 前缀        （-680 万 / -1.18 亿）
 *
 * 设计权衡：
 *   - 不用"千万/百万"独立单位 — 中文金融用户习惯将 ≤ 9999 万都用"万"读
 *     （5,620 万 比 5.62 千万 直观），且单位不闪烁、纵向对齐时数字本身传达量级
 *   - "万"前部分 < 100 万用 1 位小数，避免 1.5 万被四舍五入成 2 万；
 *     ≥ 100 万用整数千分位，避免 5,620.0 万这种冗余精度
 */
export function formatCnUsd(value: number): string {
  if (!Number.isFinite(value) || value === 0) return "0";
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs >= 1e8) return `${sign}${(abs / 1e8).toFixed(2)} 亿`;
  if (abs >= 1e6) return `${sign}${Math.round(abs / 1e4).toLocaleString("en-US")} 万`;
  if (abs >= 1e4) return `${sign}${(abs / 1e4).toFixed(1)} 万`;
  return `${sign}${Math.round(abs).toLocaleString("en-US")}`;
}

export function formatPrice(price: number, coin = "BTC"): string {
  if (coin === "SOL") return `$${price.toFixed(3)}`;
  if (coin === "ETH") return `$${price.toFixed(2)}`;
  return `$${price.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatPct(value: number): string {
  const sign = value >= 0 ? "+" : "";
  return `${sign}${value.toFixed(2)}%`;
}

export function formatRate(value: number): string {
  return `${(value * 100).toFixed(4)}%`;
}

export function formatTime(ts: number): string {
  const d = new Date(ts > 1e12 ? ts : ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
