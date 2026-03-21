export function formatUSD(value: number, decimals = 2): string {
  if (Math.abs(value) >= 1e9) return `$${(value / 1e9).toFixed(1)}B`;
  if (Math.abs(value) >= 1e6) return `$${(value / 1e6).toFixed(1)}M`;
  if (Math.abs(value) >= 1e3) return `$${(value / 1e3).toFixed(1)}K`;
  return `$${value.toFixed(decimals)}`;
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
