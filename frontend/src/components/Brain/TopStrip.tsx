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

// CVD 字段值的白话翻译（小白友好）
//   rising    → 净买入 ↑   （绿，多头主动）
//   declining → 净卖出 ↑   （红，空头主动）
//   flat      → 持平        （灰，多空胶着）
function cvdLabel(trend: string): { text: string; tone: "good" | "bad" | "neutral" } {
  if (trend === "rising") return { text: "净买入 ↑", tone: "good" };
  if (trend === "declining") return { text: "净卖出 ↑", tone: "bad" };
  return { text: "持平", tone: "neutral" };
}

// 市场结构灯：基于 CVD 现货 vs 合约 5 类组合，给出统一白话总评。
// 设计原则：永远显示一个结论（除非数据缺失），而不是仅在背离时报警，
// 让小白也能一眼看到当前市场是何种结构。
type StructureSignal = {
  label: string;
  hint: string;            // tooltip 完整解释
  tone: "good" | "bad" | "trend_up" | "trend_down" | "neutral";
  pulse: boolean;           // 仅背离时 pulse 强调
};

function structureSignal(spot: string, fut: string): StructureSignal | null {
  if (!spot || !fut) return null;          // 任一字段缺失：不显示（保守）
  if (spot === "rising" && fut === "declining") {
    return {
      label: "现货吸筹 · 杠杆退潮",
      hint: "真金白银现货在买、合约杠杆在抛 — 常见底部反弹信号（CVD 背离）",
      tone: "good",
      pulse: true,
    };
  }
  if (spot === "declining" && fut === "rising") {
    return {
      label: "杠杆追涨 · 现货抛压",
      hint: "杠杆资金在追多、现货在派发 — 常见顶部虚弱信号（CVD 背离）",
      tone: "bad",
      pulse: true,
    };
  }
  if (spot === "rising" && fut === "rising") {
    return {
      label: "共振买入",
      hint: "现货与合约都在主动买 — 趋势上行确认，方向一致",
      tone: "trend_up",
      pulse: false,
    };
  }
  if (spot === "declining" && fut === "declining") {
    return {
      label: "共振卖出",
      hint: "现货与合约都在主动卖 — 趋势下行 / 共振抛售，方向一致",
      tone: "trend_down",
      pulse: false,
    };
  }
  // 任一为 flat 或非三态值：胶着
  return {
    label: "多空胶着",
    hint: "现货或合约 CVD 趋势不明 — 没有占优方向，等待结构突破",
    tone: "neutral",
    pulse: false,
  };
}

export default function TopStrip({ snap, loading }: Props) {
  const c = snap.context;
  const dq = snap.data_quality;

  const cvdSpot = c.cvd_spot_trend ?? "";
  const cvdFut = c.cvd_contract_trend ?? "";
  const cvdSpotInfo = cvdLabel(cvdSpot);
  const cvdFutInfo = cvdLabel(cvdFut);
  const structure = structureSignal(cvdSpot, cvdFut);

  const oiTone =
    c.oi_delta_1h_pct == null
      ? "neutral"
      : c.oi_delta_1h_pct > 0.5
        ? "warn"
        : c.oi_delta_1h_pct < -0.5
          ? "warn"
          : "neutral";

  // 结构灯样式映射：4 类（背离 good/bad pulse、同向 trend_up/down 静态、胶着 neutral）
  const structureCls = structure
    ? {
        good: "border-emerald-500 bg-emerald-950/50 text-emerald-200 shadow-emerald-500/20",
        bad: "border-rose-500 bg-rose-950/50 text-rose-200 shadow-rose-500/20",
        trend_up: "border-sky-600/70 bg-sky-950/40 text-sky-200 shadow-sky-500/10",
        trend_down: "border-slate-500/70 bg-slate-800/60 text-slate-200",
        neutral: "border-zinc-700/60 bg-zinc-900/60 text-zinc-300",
      }[structure.tone]
    : "";
  const structureDot = structure
    ? {
        good: "bg-emerald-400",
        bad: "bg-rose-400",
        trend_up: "bg-sky-400",
        trend_down: "bg-slate-400",
        neutral: "bg-zinc-400",
      }[structure.tone]
    : "";

  return (
    <div className="flex flex-wrap items-center gap-1.5 px-3 py-1.5">
      {structure && (
        <div
          className={`flex items-center gap-1.5 rounded-md border-2 px-2.5 py-1 text-[12px] font-semibold shadow-lg ${structureCls}`}
          title={structure.hint}
        >
          <span className={`h-2 w-2 rounded-full ${structureDot} ${structure.pulse ? "animate-pulse" : ""}`} />
          {structure.label}
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
        <Chip label="合约 CVD" value={cvdFutInfo.text} tone={cvdFutInfo.tone} />
      )}
      {c.cvd_spot_trend && (
        <Chip label="现货 CVD" value={cvdSpotInfo.text} tone={cvdSpotInfo.tone} />
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
