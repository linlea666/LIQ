import type { TradingBrainSnapshot } from "@/lib/types";
import { formatPrice } from "@/lib/format";

interface Props {
  snap: TradingBrainSnapshot;
  loading: boolean;
}

function Chip({ label, value, tone = "default" }: {
  label: string; value: React.ReactNode;
  tone?: "default" | "warn" | "good" | "bad" | "neutral";
}) {
  const toneCls = {
    default: "border-slate-700/80 bg-slate-900/70 text-slate-200",
    warn: "border-amber-700/60 bg-amber-950/30 text-amber-200",
    good: "border-emerald-700/60 bg-emerald-950/30 text-emerald-200",
    bad: "border-rose-700/60 bg-rose-950/30 text-rose-200",
    neutral: "border-slate-700/60 bg-slate-800/60 text-slate-300",
  }[tone];
  return (
    <div className={`flex items-baseline gap-1.5 rounded border px-2 py-1 text-[11px] tabular-nums ${toneCls}`}>
      <span className="text-[9px] uppercase tracking-wider opacity-60">{label}</span>
      <span className="font-medium">{value}</span>
    </div>
  );
}

export default function TopStrip({ snap, loading }: Props) {
  const c = snap.context;
  const dq = snap.data_quality;

  let cvdTone: "good" | "bad" | "neutral" = "neutral";
  if (c.cvd_contract_trend === "rising") cvdTone = "good";
  else if (c.cvd_contract_trend === "declining") cvdTone = "bad";

  // CVD 背离识别：spot 与 futures 方向相反 → 显著市场分歧灯
  // - 现强合弱 (spot rising + futures declining)：现货吸筹、杠杆退潮，常见底部强信号
  // - 合强现弱 (spot declining + futures rising)：现货抛压、杠杆追涨，常见顶部虚弱信号
  const cvdSpot = c.cvd_spot_trend ?? "";
  const cvdFut = c.cvd_contract_trend ?? "";
  let divergence: { label: string; tone: "good" | "bad" | "warn" } | null = null;
  if (cvdSpot === "rising" && cvdFut === "declining") {
    divergence = { label: "CVD 背离 · 现强合弱", tone: "good" };
  } else if (cvdSpot === "declining" && cvdFut === "rising") {
    divergence = { label: "CVD 背离 · 合强现弱", tone: "bad" };
  } else if (
    (cvdSpot === "rising" && cvdFut === "rising") ||
    (cvdSpot === "declining" && cvdFut === "declining")
  ) {
    divergence = null; // 同向不报警
  }

  const oiTone =
    c.oi_delta_1h_pct == null
      ? "neutral"
      : c.oi_delta_1h_pct > 0.5
        ? "warn"
        : c.oi_delta_1h_pct < -0.5
          ? "warn"
          : "neutral";

  return (
    <div className="flex flex-wrap items-center gap-1.5 px-3 py-1.5">
      {divergence && (
        <div
          className={`flex items-center gap-1.5 rounded-md border-2 px-2.5 py-1 text-[12px] font-semibold shadow-lg ${
            divergence.tone === "good"
              ? "border-emerald-500 bg-emerald-950/50 text-emerald-200 shadow-emerald-500/20"
              : "border-rose-500 bg-rose-950/50 text-rose-200 shadow-rose-500/20"
          }`}
          title="CVD 现货 vs 合约 出现方向背离 — 关注市场结构强弱拐点"
        >
          <span className={`h-2 w-2 rounded-full ${divergence.tone === "good" ? "bg-emerald-400" : "bg-rose-400"} animate-pulse`} />
          {divergence.label}
        </div>
      )}
      <Chip label="现价" value={formatPrice(snap.last_price, snap.coin)} />
      <Chip label="ATR" value={snap.atr.toFixed(2)} />
      {c.regime && (
        <Chip
          label="Regime"
          value={c.regime}
          tone={
            c.regime.toLowerCase().includes("trend")
              ? "warn"
              : "neutral"
          }
        />
      )}
      {c.cvd_contract_trend && (
        <Chip label="CVD合约" value={c.cvd_contract_trend} tone={cvdTone} />
      )}
      {c.cvd_spot_trend && (
        <Chip label="CVD现货" value={c.cvd_spot_trend} tone="neutral" />
      )}
      {c.oi_delta_1h_pct != null && (
        <Chip
          label="OI 1h"
          value={`${c.oi_delta_1h_pct >= 0 ? "+" : ""}${c.oi_delta_1h_pct.toFixed(3)}%`}
          tone={oiTone}
        />
      )}
      {c.funding_interpretation && (
        <Chip
          label="资金费"
          value={c.funding_interpretation}
          tone={
            c.funding_interpretation.includes("拥挤") ? "warn" : "neutral"
          }
        />
      )}
      {dq.usd_usdt_basis_pct != null && (
        <Chip
          label="USD/USDT"
          value={`${dq.usd_usdt_basis_pct.toFixed(4)}%`}
          tone={Math.abs(dq.usd_usdt_basis_pct) > 0.05 ? "warn" : "neutral"}
        />
      )}
      {c.nearest_magnet_below != null && (
        <Chip label="磁铁(下)" value={formatPrice(c.nearest_magnet_below, snap.coin)} tone="neutral" />
      )}
      {c.nearest_magnet_above != null && (
        <Chip label="磁铁(上)" value={formatPrice(c.nearest_magnet_above, snap.coin)} tone="neutral" />
      )}
      <span className="ml-auto text-[10px] text-slate-500">
        {loading ? "刷新中…" : `更新于 ${new Date(snap.ts * 1000).toLocaleTimeString("zh-CN")}`}
      </span>
    </div>
  );
}
